import argparse
import os
import re
import shlex
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import io
    from collections.abc import Callable

from golem import (
    delete,
    display,
    executor,
    gates,
    launch_agent,
    ledger,
    progress_log,
    prune,
    review,
    scheduling,
    worktree_cleanup,
)
from golem.backends import claude_hooks, common, registry

SCHEDULE_DESCRIPTION = "Run at the next usage reset, or immediately if no usage window is active."
ALIAS_PLACEHOLDER_PATTERN = re.compile(r"\{(\d+)\}")
ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def config_path() -> Path:
    return ledger.xdg_golem_directory("XDG_CONFIG_HOME", Path.home() / ".config") / "config.toml"


def invalid_config_message_prefix() -> str:
    return f"golem: invalid config file {config_path()}"


def load_config() -> dict[str, object]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"{invalid_config_message_prefix()}: {error}") from error


def load_string_table(table_name: str, item_label: str) -> dict[str, str]:
    table = load_config().get(table_name)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise SystemExit(f"{invalid_config_message_prefix()}: {table_name} must be a table")
    entries = cast("dict[str, object]", table)
    validated_table: dict[str, str] = {}
    for name, value in entries.items():
        if not isinstance(value, str):
            raise SystemExit(
                f"{invalid_config_message_prefix()}: {item_label} {name} must be a string"
            )
        validated_table[name] = value
    return validated_table


def load_aliases() -> dict[str, str]:
    return load_string_table("aliases", "alias")


def load_environment() -> dict[str, str]:
    environment = load_string_table("env", "env")
    for name, value in environment.items():
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise SystemExit(f"{invalid_config_message_prefix()}: env name {name} is invalid")
        if "\0" in value:
            raise SystemExit(f"{invalid_config_message_prefix()}: env {name} contains a null byte")
    return environment


def task_environment(assignments: list[str]) -> dict[str, str]:
    environment = load_environment()
    for assignment in assignments:
        name, separator, value = assignment.partition("=")
        if not separator:
            raise SystemExit(f"golem: --env expects NAME=VALUE, got: {assignment}")
        if not ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise SystemExit(f"golem: --env name is invalid: {name}")
        environment[name] = value
    return environment


def expand_alias(
    name: str, template: str, alias_args: list[str], command_line_agent_args: list[str]
) -> tuple[list[str], list[str]]:
    try:
        template_args = shlex.split(template)
    except ValueError as error:
        raise SystemExit(f"golem: invalid alias {name}: {error}") from error
    separator_index = template_args.index("--") if "--" in template_args else len(template_args)
    command_args = template_args[:separator_index]
    template_agent_args = template_args[separator_index + 1 :]
    placeholder_numbers = [
        int(match.group(1))
        for argument in template_args
        for match in ALIAS_PLACEHOLDER_PATTERN.finditer(argument)
    ]
    if 0 in placeholder_numbers:
        raise SystemExit(f"golem: alias {name} has placeholder {{0}}; placeholders start at {{1}}")
    required_arguments = max(placeholder_numbers, default=0)
    if len(alias_args) < required_arguments:
        raise SystemExit(
            f"golem: alias {name} needs {required_arguments} arguments, got {len(alias_args)}"
        )

    def substitute(argument: str) -> str:
        return ALIAS_PLACEHOLDER_PATTERN.sub(
            lambda match: alias_args[int(match.group(1)) - 1], argument
        )

    return (
        [*(substitute(argument) for argument in command_args), *alias_args[required_arguments:]],
        [
            *(substitute(argument) for argument in template_agent_args),
            *command_line_agent_args,
        ],
    )


def add_scheduling_arguments(add_argument: Callable[..., argparse.Action]) -> None:
    add_argument("--now", action="store_true", help="run immediately")
    add_argument(
        "--at",
        metavar="TIME",
        help='local time, e.g. 9pm or "8:30am tomorrow"; rolls forward if passed',
    )


def set_command_handler(
    command_parser: argparse.ArgumentParser, handler: Callable[[ledger.Arguments], int]
) -> None:
    command_parser.set_defaults(handler=handler)


def task_id_from_arguments(arguments: ledger.Arguments) -> str:
    if arguments.id is None:
        raise SystemExit("golem: task ID is required")
    return arguments.id


def run_task_from_arguments(arguments: ledger.Arguments) -> int:
    backend = registry.BACKENDS[arguments.backend]
    if arguments.auto_pickup and not backend.IS_AUTO_PICKUP_SUPPORTED:
        raise SystemExit(f"golem: --auto-pickup does not support {backend.NAME} tasks")
    if arguments.gate is not None and not backend.IS_GATE_SUPPORTED:
        raise SystemExit(f"golem: --gate does not support {backend.NAME} tasks")
    launch_agent.require_launch_agent()
    task_id = task_id_from_arguments(arguments)
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8") if arguments.file else arguments.prompt
    )
    if prompt_text is None:
        raise SystemExit("golem: a prompt is required")
    task_args = tuple(arguments.agent_args)
    placement = scheduling.RunPlacement(
        str(Path.cwd()),
        environment=task_environment(arguments.env),
        is_auto_pickup=arguments.auto_pickup,
        is_no_worktree=arguments.no_worktree,
        gate=arguments.gate,
        setup=arguments.setup,
    )
    has_worktree_argument = common.has_agent_argument(list(task_args), common.WORKTREE_ARGUMENTS)
    parent = ledger.load_tasks().get(arguments.after) if isinstance(arguments.after, str) else None
    if parent is not None and parent.get("no_worktree"):
        placement = replace(placement, cwd=parent["cwd"], is_no_worktree=True)
        if has_worktree_argument:
            raise SystemExit(
                f"golem: {arguments.after} runs in place; -w/--worktree is not allowed"
            )
    if placement.is_no_worktree and has_worktree_argument:
        raise SystemExit("golem: --no-worktree does not accept -w/--worktree")
    if placement.setup is not None and not placement.is_no_worktree:
        raise SystemExit("golem: --setup requires --no-worktree")
    follow_up_records = scheduling.new_follow_up_records(
        task_id,
        arguments.then,
        task_args,
        arguments.backend,
        placement,
    )
    backend.validate_run_arguments(list(task_args))
    exit_code = scheduling.schedule_run_task(
        arguments, task_id, prompt_text, task_args, follow_up_records, placement
    )
    for task_id, record in follow_up_records.items():
        print(f"golem: {task_id} will run after {record.get('after')}")
    return exit_code


def add_worktree_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    worktree_command = add_parser("worktree")
    worktree_commands = worktree_command.add_subparsers(dest="worktree_command", required=True)
    set_command_handler(
        worktree_commands.add_parser("list"), lambda _: worktree_cleanup.list_worktrees()
    )
    auto_remove_worktree_command = worktree_commands.add_parser("auto-remove")
    auto_remove_group = auto_remove_worktree_command.add_mutually_exclusive_group(required=True)
    auto_remove_group.add_argument("--after", type=int, metavar="DAYS")
    auto_remove_group.add_argument("--off", action="store_true")
    set_command_handler(
        auto_remove_worktree_command, worktree_cleanup.set_worktree_auto_remove_from_arguments
    )


def add_prune_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    prune_command = add_parser(
        "prune",
        description=(
            "Delete cancelled tasks, and done tasks whose PR is merged or closed or whose "
            f"session has been idle for {prune.PRUNE_IDLE_DAYS} days, after asking."
        ),
    )
    prune_command.add_argument(
        "--yes", action="store_true", help="delete done tasks that qualify without asking"
    )
    set_command_handler(
        prune_command, lambda arguments: prune.prune_tasks(is_confirmed=arguments.yes)
    )


def add_continue_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    continue_command = add_parser(
        "continue", description="Re-run a stopped task's gate and start its follow-ups."
    )
    continue_command.add_argument("id")
    continue_command.add_argument("--force", action="store_true", help="skip the gate")
    set_command_handler(
        continue_command,
        lambda arguments: gates.continue_task(
            task_id_from_arguments(arguments), is_forced=arguments.force
        ),
    )


def add_watch_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    watch_command = add_parser("watch")
    watch_command.add_argument("id")
    set_command_handler(
        watch_command, lambda arguments: progress_log.watch_task(task_id_from_arguments(arguments))
    )


def add_hook_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    hook_command = add_parser("_hook")
    hook_command.add_argument("event", choices=tuple(claude_hooks.HOOK_EVENT_ARGUMENTS.values()))
    set_command_handler(
        hook_command,
        lambda arguments: (
            gates.hook_stop()
            if arguments.event == claude_hooks.HOOK_ARGUMENT_STOP
            else gates.hook_pre_tool_use()
        ),
    )


def list_aliases(command_parsers: dict[str, argparse.ArgumentParser]) -> int:
    aliases = load_aliases()
    if not aliases:
        print("golem: no aliases")
        return 0
    widest_name = max(map(len, aliases))
    for name, template in aliases.items():
        shadowed_suffix = "  (shadowed by built-in command)" if name in command_parsers else ""
        print(f"{name:<{widest_name}}  {template}{shadowed_suffix}")
    return 0


def add_run_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    run_command = add_parser(
        "run",
        description=SCHEDULE_DESCRIPTION,
    )
    run_command.add_argument("id")
    run_command.add_argument(
        "--backend",
        choices=tuple(registry.BACKENDS),
        default=ledger.BACKEND_CLAUDE,
    )
    prompt_group = run_command.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("prompt", nargs="?")
    prompt_group.add_argument("--file")
    run_command.add_argument(
        "--then",
        action="append",
        nargs=2,
        default=[],
        metavar=("ID", "PROMPT"),
        help="run PROMPT as task ID after the previous task succeeds; repeatable",
    )
    run_command.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set an environment variable for the task; repeatable; follow-ups get it too",
    )
    run_command.add_argument(
        "--auto-pickup",
        action="store_true",
        help="open the session in the Claude desktop app when the task succeeds; follow-ups get it too",
    )
    run_command.add_argument(
        "--no-worktree",
        action="store_true",
        help="run in the task directory instead of a new worktree; follow-ups get it too",
    )
    run_command.add_argument(
        "--gate",
        metavar="CMD",
        help=(
            "shell command run after the task succeeds with a waiting follow-up; "
            "a non-zero exit stops the chain; follow-ups get it too"
        ),
    )
    run_command.add_argument(
        "--setup",
        metavar="CMD",
        help=(
            "shell command run when the task starts; its JSON output can set the task "
            "directory and append to the prompt; follow-ups get the directory"
        ),
    )
    run_schedule_group = run_command.add_mutually_exclusive_group()
    add_scheduling_arguments(run_schedule_group.add_argument)
    run_schedule_group.add_argument("--after", metavar="PARENT", help="run after PARENT succeeds")
    set_command_handler(run_command, run_task_from_arguments)


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(prog="golem")
    parser.add_argument(
        "-C", dest="directory", metavar="DIR", help="run as if golem was started in DIR"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    add_run_command(commands.add_parser)
    pickup_command = commands.add_parser("pickup")
    pickup_command.add_argument("id", nargs="?")
    pickup_command.add_argument(
        "--all", action="store_true", help="open every task that needs review"
    )
    pickup_command.add_argument(
        "--app",
        action="store_true",
        help="open the session in the Claude desktop app, in the app's default permission mode",
    )
    set_command_handler(
        pickup_command,
        lambda arguments: (
            review.pickup_all()
            if arguments.all
            else review.pickup(
                task_id_from_arguments(arguments), *arguments.agent_args, is_app=arguments.app
            )
        ),
    )
    close_command = commands.add_parser("close")
    close_command.add_argument("id")
    set_command_handler(
        close_command, lambda arguments: review.close(task_id_from_arguments(arguments))
    )
    add_continue_command(commands.add_parser)
    show_command = commands.add_parser("show")
    show_command.add_argument("id")
    set_command_handler(
        show_command, lambda arguments: display.show_task_result(task_id_from_arguments(arguments))
    )
    add_watch_command(commands.add_parser)
    cancel_command = commands.add_parser("cancel")
    cancel_command.add_argument("id")
    set_command_handler(
        cancel_command, lambda arguments: scheduling.cancel_task(task_id_from_arguments(arguments))
    )
    retry_command = commands.add_parser(
        "retry",
        description=SCHEDULE_DESCRIPTION,
    )
    retry_command.add_argument("id")
    retry_schedule_group = retry_command.add_mutually_exclusive_group()
    add_scheduling_arguments(retry_schedule_group.add_argument)
    set_command_handler(
        retry_command,
        lambda arguments: scheduling.retry_task(
            task_id_from_arguments(arguments), is_now=arguments.now, at_text=arguments.at
        ),
    )
    reschedule_command = commands.add_parser(
        "reschedule",
        description=SCHEDULE_DESCRIPTION,
    )
    reschedule_command.add_argument("id")
    reschedule_schedule_group = reschedule_command.add_mutually_exclusive_group()
    add_scheduling_arguments(reschedule_schedule_group.add_argument)
    set_command_handler(
        reschedule_command,
        lambda arguments: scheduling.reschedule_task(
            task_id_from_arguments(arguments), is_now=arguments.now, at_text=arguments.at
        ),
    )
    delete_command = commands.add_parser("delete")
    delete_command.add_argument("ids", nargs="+")
    delete_command.add_argument("--chain", action="store_true")
    delete_command.add_argument("--remove-worktree", action="store_true")
    set_command_handler(
        delete_command,
        lambda arguments: delete.delete_tasks(
            arguments.ids,
            is_chain=arguments.chain,
            is_removing_worktree=arguments.remove_worktree,
        ),
    )
    add_prune_command(commands.add_parser)
    set_command_handler(commands.add_parser("list"), lambda _: display.list_tasks())
    alias_command = commands.add_parser("alias")
    alias_commands = alias_command.add_subparsers(dest="alias_command", required=True)
    set_command_handler(alias_commands.add_parser("list"), lambda _: list_aliases(commands.choices))
    add_worktree_commands(commands.add_parser)
    launch_agent_command = commands.add_parser(
        "agent",
        description=(
            "Agent logs are written to agent.log in the state directory; when it exceeds "
            f"{progress_log.AGENT_LOG_MAX_BYTES // 1024**2} MiB it is copied to agent.log.1, replacing "
            "the previous copy, and truncated."
        ),
    )
    launch_agent_command.add_argument("action", choices=("install", "uninstall"))
    set_command_handler(
        launch_agent_command,
        lambda arguments: (
            launch_agent.install_launch_agent()
            if arguments.action == "install"
            else launch_agent.uninstall_launch_agent()
        ),
    )
    set_command_handler(commands.add_parser("_agent"), lambda _: launch_agent.run_launch_agent())
    execute_command = commands.add_parser("_execute")
    execute_command.add_argument("id")
    execute_command.add_argument("session_id")
    set_command_handler(
        execute_command,
        lambda arguments: executor.execute_or_drain_task(
            task_id_from_arguments(arguments), arguments.session_id
        ),
    )
    add_hook_command(commands.add_parser)
    return parser, commands.choices


def change_directory(directory: str | None) -> None:
    if directory is None:
        return
    try:
        os.chdir(directory)
    except FileNotFoundError, NotADirectoryError:
        raise SystemExit(f"golem: directory not found: {directory}") from None
    except OSError as error:
        raise SystemExit(
            f"golem: cannot change to directory: {directory}: {error.strerror}"
        ) from None


def apply_leading_directory_options(golem_args: list[str]) -> list[str]:
    while golem_args:
        option = golem_args[0]
        if option == "-C" and golem_args[1:]:
            change_directory(golem_args[1])
            golem_args = golem_args[2:]
        elif option.startswith("-C") and option != "-C":
            change_directory(option[2:].removeprefix("="))
            golem_args = golem_args[1:]
        else:
            break
    return golem_args


def main(argv: list[str] | None = None) -> int:
    cast("io.TextIOWrapper", sys.stdout).reconfigure(encoding="utf-8")
    golem_args = sys.argv[1:] if argv is None else argv
    agent_args: list[str] = []
    if "--" in golem_args:
        separator_index = golem_args.index("--")
        agent_args = golem_args[separator_index + 1 :]
        golem_args = golem_args[:separator_index]
    golem_args = apply_leading_directory_options(golem_args)

    parser, command_parsers = build_parser()
    if golem_args and not golem_args[0].startswith("-") and golem_args[0] not in command_parsers:
        aliases = load_aliases()
        alias_name = golem_args[0]
        template = aliases.get(alias_name)
        if template is not None:
            golem_args, agent_args = expand_alias(alias_name, template, golem_args[1:], agent_args)
            if golem_args and golem_args[0] not in command_parsers and golem_args[0] in aliases:
                raise SystemExit(
                    f"golem: alias {alias_name} expands to alias {golem_args[0]}; "
                    "aliases cannot use other aliases"
                )
    arguments = parser.parse_args(golem_args, ledger.Arguments(agent_args=agent_args))
    change_directory(arguments.directory)
    if agent_args and arguments.command == "retry":
        parser.error("retry does not accept agent arguments")
    if agent_args and arguments.command == "reschedule":
        parser.error("reschedule does not accept agent arguments")
    if agent_args and arguments.command == "pickup" and arguments.app:
        parser.error("pickup --app does not accept agent arguments")
    if arguments.command == "pickup":
        if arguments.all and not arguments.app:
            parser.error("pickup --all requires --app")
        if arguments.all and arguments.id is not None:
            parser.error("pickup --all does not accept an ID")
        if arguments.id is None and not arguments.all:
            parser.error("pickup requires an ID or --all")
    return arguments.handler(arguments)
