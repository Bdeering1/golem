import shlex
import stat
import sys
from contextlib import suppress
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from golem import chains, ledger, spawn

HOOK_STOP_TIMEOUT_SECONDS = 30
HOOK_ARGUMENT_STOP = "stop"
HOOK_ARGUMENT_PRE_TOOL_USE = "pre-tool-use"
HOOK_EVENT_STOP = "Stop"
HOOK_EVENT_PRE_TOOL_USE = "PreToolUse"
HOOK_EVENT_ARGUMENTS = {
    HOOK_EVENT_STOP: HOOK_ARGUMENT_STOP,
    HOOK_EVENT_PRE_TOOL_USE: HOOK_ARGUMENT_PRE_TOOL_USE,
}
# Claude Code treats exit status 2 as blocking, which golem's argument parser returns on misuse.
HOOK_FAILURE_SUFFIX = " || exit 1"
HOOK_COMMAND_SUFFIXES = tuple(
    f" _hook {argument}{HOOK_FAILURE_SUFFIX}" for argument in HOOK_EVENT_ARGUMENTS.values()
)
HOOK_TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"


class LocalSettings(TypedDict):
    hooks: NotRequired[dict[str, list[object]]]


def hook_command_line(argument: str) -> str:
    state_home = shlex.quote(str(ledger.state_directory().parent))
    command = shlex.join([*spawn.golem_command(), "_hook", argument])
    return f"XDG_STATE_HOME={state_home} {command}{HOOK_FAILURE_SUFFIX}"


def hook_group(event: str) -> ledger.JsonObject:
    entry: ledger.JsonObject = {
        "type": "command",
        "command": hook_command_line(HOOK_EVENT_ARGUMENTS[event]),
    }
    if event == HOOK_EVENT_STOP:
        entry["timeout"] = HOOK_STOP_TIMEOUT_SECONDS
        return {"hooks": [entry]}
    return {"matcher": HOOK_TOOL_MATCHER, "hooks": [entry]}


def group_entries(group: object) -> list[object]:
    entries = cast("ledger.JsonObject", group).get("hooks") if isinstance(group, dict) else None
    return cast("list[object]", entries) if isinstance(entries, list) else []


def is_golem_hook_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = cast("ledger.JsonObject", entry).get("command")
    return isinstance(command, str) and command.endswith(HOOK_COMMAND_SUFFIXES)


def local_settings_path(directory: str) -> Path:
    return Path(directory) / ".claude" / "settings.local.json"


def save_local_settings(path: Path, settings: LocalSettings) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    ledger.save_json(path, settings)
    if mode is not None:
        path.chmod(mode)


def read_local_settings(path: Path) -> LocalSettings | None:
    if path.is_symlink():
        return None
    try:
        loaded_settings = cast("object", ledger.load_json(path))
    except ValueError, OSError:
        return None
    if not isinstance(loaded_settings, dict):
        return None
    settings = cast("ledger.JsonObject", loaded_settings)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    hooks_by_event = cast("ledger.JsonObject", hooks)
    if not all(isinstance(hooks_by_event.get(event, []), list) for event in HOOK_EVENT_ARGUMENTS):
        return None
    return cast("LocalSettings", settings)


def groups_without_golem_entries(groups: list[object]) -> list[object]:
    kept_groups: list[object] = []
    for group in groups:
        entries = group_entries(group)
        kept_entries = [entry for entry in entries if not is_golem_hook_entry(entry)]
        if len(kept_entries) == len(entries):
            kept_groups.append(group)
        elif kept_entries:
            kept_groups.append({**cast("ledger.JsonObject", group), "hooks": kept_entries})
    return kept_groups


def install_hooks(directory: str) -> None:
    if not Path(directory).is_dir():
        return
    path = local_settings_path(directory)
    settings = read_local_settings(path)
    if settings is None:
        print(f"golem: hooks not installed: {path} is not usable settings", file=sys.stderr)
        return
    hooks = settings.setdefault("hooks", {})
    is_changed = False
    for event in HOOK_EVENT_ARGUMENTS:
        groups = hooks.get(event, [])
        current_group = hook_group(event)
        current_entry = group_entries(current_group)[0]
        if any(entry == current_entry for group in groups for entry in group_entries(group)):
            continue
        hooks[event] = [*groups_without_golem_entries(groups), current_group]
        is_changed = True
    if not is_changed:
        return
    try:
        path.parent.mkdir(exist_ok=True)
        save_local_settings(path, settings)
    except OSError as error:
        print(f"golem: hooks not installed: {error}", file=sys.stderr)


def remove_hooks(directory: str) -> None:
    path = local_settings_path(directory)
    if not path.exists():
        return
    settings = read_local_settings(path)
    if settings is None:
        return
    hooks = settings.get("hooks", {})
    is_changed = False
    for event in HOOK_EVENT_ARGUMENTS:
        groups = hooks.get(event, [])
        kept_groups = groups_without_golem_entries(groups)
        if kept_groups == groups:
            continue
        is_changed = True
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not is_changed:
        return
    if not hooks:
        del settings["hooks"]
    try:
        if settings:
            save_local_settings(path, settings)
            return
        path.unlink()
    except OSError as error:
        print(f"golem: hooks not removed: {error}", file=sys.stderr)
        return
    with suppress(OSError):
        path.parent.rmdir()


def has_pending_follow_up(tasks: ledger.Tasks, task_id: str) -> bool:
    return bool(
        chains.waiting_dependent_ids(tasks, task_id)
        or chains.descendant_ids_with_status(
            tasks, task_id, {ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING}
        )
    )


def is_hook_needed(tasks: ledger.Tasks, directory: str) -> bool:
    return any(
        "gate" in task
        and task["status"] in ledger.SUCCEEDED_STATUSES
        and ledger.task_directory(task) == directory
        and has_pending_follow_up(tasks, task_id)
        for task_id, task in tasks.items()
    )


def hook_affected_directories(tasks: ledger.Tasks, task: ledger.Task) -> set[str]:
    directories = {ledger.task_directory(task)}
    parent = tasks.get(task.get("after", ""))
    if parent is not None:
        directories.add(ledger.task_directory(parent))
    return directories


def sync_hooks(directory: str) -> None:
    with ledger.locked_tasks() as tasks:
        if is_hook_needed(tasks, directory):
            install_hooks(directory)
        else:
            remove_hooks(directory)
