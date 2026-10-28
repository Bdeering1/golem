import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import queue
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    import io
    from collections.abc import Callable, Generator

STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
NOT_STARTED_STATUSES = {STATUS_SCHEDULED, STATUS_WAITING, STATUS_CANCELLED}
BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
LIST_STATUS_ORDER = (
    STATUS_NEEDS_REVIEW,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    STATUS_WAITING,
    STATUS_DONE,
    STATUS_CANCELLED,
)
STATUS_COLORS = {
    STATUS_NEEDS_REVIEW: 33,
    STATUS_FAILED: 196,
    STATUS_RUNNING: 39,
    STATUS_SCHEDULED: 208,
    STATUS_WAITING: 141,
    STATUS_DONE: 34,
    STATUS_CANCELLED: 244,
}
ANSI_RESET = "\033[0m"
SUCCEEDED_STATUSES = {STATUS_NEEDS_REVIEW, STATUS_DONE}
UNREVIEWED_STATUSES = {STATUS_SCHEDULED, STATUS_RUNNING, STATUS_WAITING, STATUS_NEEDS_REVIEW}
AGENT_LOG_MAX_BYTES = 1024 * 1024
PROGRESS_LINE_WIDTH = 120
PROGRESS_TOOL_INPUT_KEYS = ("command", "file_path", "pattern", "url", "description")
WATCH_POLL_SECONDS = 0.2
USAGE_ERROR = "golem: usage lookup unavailable"
CODEX_ACCOUNT_ERROR = "golem: Codex account lookup unavailable"
CODEX_SUBSCRIPTION_ERROR = (
    "golem: Codex must be signed in with a ChatGPT subscription; run codex login"
)
USAGE_TIMEOUT_SECONDS = 30
GIT_FETCH_TIMEOUT_SECONDS = 5
GIT_FETCH_MAX_AGE_SECONDS = 24 * 60 * 60
ORIGIN_HEAD = "refs/remotes/origin/HEAD"
USAGE_LIMIT_HTTP_STATUS = 429
FULL_UTILIZATION = 100
MAX_USAGE_LIMIT_RESUMES = 2
USAGE_LIMIT_RESUME_PROMPT = "The usage limit has reset. Continue the task."
BACKGROUND_WAIT_CEILING_VARIABLE = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
BACKGROUND_WORK_STOPPED_PATTERN = re.compile(
    r"^Background tasks still running .*; stopping them\..*$", re.MULTILINE
)
SCHEDULE_DESCRIPTION = "Run at the next usage reset, or immediately if no usage window is active."
LAUNCH_AGENT_ERROR = "golem: the agent is not installed or is out of date; run golem agent install"
LAUNCH_AGENT_LABEL = "com.github.bdeering1.golem"
LAUNCH_AGENT_INTERVAL_SECONDS = 300
LAUNCH_AGENT_POLL_SECONDS = 10
WORKTREE_ARGUMENTS = {"-w", "--worktree"}
NAME_ARGUMENTS = {"-n", "--name"}
PERMISSION_ARGUMENTS = {
    "--allow-dangerously-skip-permissions",
    "--dangerously-skip-permissions",
    "--permission-mode",
}
CODEX_PERMISSION_ARGUMENTS = {
    "-s",
    "--sandbox",
    "--approve-for-me",
    "--not-so-yolo",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "-a",
    "--ask-for-approval",
}
CODEX_CONFIG_ARGUMENTS = {"-c", "--config"}
CODEX_UNSUPPORTED_ARGUMENTS = {
    "--oss",
    "--local-provider",
    "--remote",
    "--remote-auth-token-env",
}
CODEX_PROVIDER_OVERRIDE = 'model_provider="openai"'
CODEX_PROVIDER_ARGUMENTS = ("-c", CODEX_PROVIDER_OVERRIDE)
VERBOSE_ARGUMENT = "--verbose"
WORKTREE_OWNER_GOLEM = "golem"
WORKTREE_OWNER_EXTERNAL = "external"
WORKTREE_OWNER_MARKER = "golem-owner"
CLAUDE_LOCK_PATTERN = re.compile(r"claude session (\S+) \(pid (\d+) start .+\)")
PRUNE_IDLE_DAYS = 7
PULL_REQUEST_TIMEOUT_SECONDS = 30
GATE_TIMEOUT_SECONDS = 300
SETUP_OUTPUT_ERROR = (
    "setup output must be a JSON object with optional string directory and prompt_suffix"
)
HOOK_GATE_TIMEOUT_SECONDS = 20
HOOK_STOP_TIMEOUT_SECONDS = 30
HOOK_ARGUMENT_STOP = "stop"
HOOK_ARGUMENT_PRE_TOOL_USE = "pre-tool-use"
HOOK_EVENT_STOP = "Stop"
HOOK_EVENT_PRE_TOOL_USE = "PreToolUse"
HOOK_EVENT_ARGUMENTS = {
    HOOK_EVENT_STOP: HOOK_ARGUMENT_STOP,
    HOOK_EVENT_PRE_TOOL_USE: HOOK_ARGUMENT_PRE_TOOL_USE,
}
# Claude Code treats exit status 2 as blocking, which a missing golem.py would return.
HOOK_FAILURE_SUFFIX = " || exit 1"
HOOK_COMMAND_SUFFIXES = tuple(
    f" _hook {argument}{HOOK_FAILURE_SUFFIX}" for argument in HOOK_EVENT_ARGUMENTS.values()
)
HOOK_TOOL_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"
PULL_REQUEST_STATE_OPEN = "OPEN"
PRUNE_ANSWER_YES = "y"
PRUNE_ANSWER_NO = "n"
PRUNE_ANSWER_ALL = "a"
PRUNE_ANSWER_QUIT = "q"
PRUNE_ANSWERS = (PRUNE_ANSWER_YES, PRUNE_ANSWER_NO, PRUNE_ANSWER_ALL, PRUNE_ANSWER_QUIT)
ALIAS_PLACEHOLDER_PATTERN = re.compile(r"\{(\d+)\}")


class Task(TypedDict):
    cwd: str
    prompt: str
    agent_args: list[str]
    status: str
    session_id: str
    environment: NotRequired[dict[str, str]]
    backend: NotRequired[str]
    after: NotRequired[str]
    due_at: NotRequired[str]
    worktree: NotRequired[str]
    worktree_pending: NotRequired[bool]
    thread_id: NotRequired[str]
    result: NotRequired[str]
    returncode: NotRequired[int]
    usage_limit_resume_count: NotRequired[int]
    started_at: NotRequired[str]
    auto_pickup: NotRequired[bool]
    no_worktree: NotRequired[bool]
    gate: NotRequired[str]
    gate_reason: NotRequired[str]
    setup: NotRequired[str]
    setup_complete: NotRequired[bool]
    setup_reason: NotRequired[str]


class WorktreeResource(TypedDict):
    owner: str
    session_id: str
    created_at: str
    repository: NotRequired[str]
    released_at: NotRequired[str]
    removed_at: NotRequired[str]
    removal_error: NotRequired[str]


class LocalSettings(TypedDict):
    hooks: NotRequired[dict[str, list[object]]]


class Settings(TypedDict):
    worktree_retention_days: NotRequired[int]


class PullRequest(TypedDict):
    number: int
    state: str


class Arguments(argparse.Namespace):
    command: str
    handler: Callable[[Arguments], int]
    agent_args: list[str]
    id: str | None
    prompt: str | None
    file: str | None
    backend: str
    after: str | int | None
    now: bool
    at: str | None
    all: bool
    app: bool
    yes: bool
    off: bool
    action: str
    session_id: str
    remove_worktree: bool
    then: list[list[str]]
    auto_pickup: bool
    no_worktree: bool
    gate: str | None
    setup: str | None
    force: bool
    event: str
    ids: list[str]
    chain: bool
    directory: str | None


class ClaudeRateLimit(TypedDict):
    utilization: int | float
    resets_at: str | None


class ClaudeUsageResponse(TypedDict):
    rate_limits_available: bool
    rate_limits: dict[str, ClaudeRateLimit | None]


class ClaudeControlResponse(TypedDict):
    request_id: str
    subtype: str
    response: ClaudeUsageResponse


class ClaudeControlFrame(TypedDict):
    type: str
    response: ClaudeControlResponse


type Tasks = dict[str, Task]
type Worktrees = dict[str, WorktreeResource]
type PullRequestCache = dict[str | None, PullRequest | None]
type JsonObject = dict[str, object]
type AppServerRequests = list[JsonObject]
type AppServerResponses = dict[int, JsonObject]


def xdg_golem_directory(variable: str, fallback: Path) -> Path:
    base_directory = Path(os.environ.get(variable, ""))
    return (base_directory if base_directory.is_absolute() else fallback) / "golem"


def config_path() -> Path:
    return xdg_golem_directory("XDG_CONFIG_HOME", Path.home() / ".config") / "config.toml"


def load_aliases() -> dict[str, str]:
    path = config_path()
    if not path.exists():
        return {}
    invalid_config_error = f"golem: invalid config file {path}"
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"{invalid_config_error}: {error}") from error
    aliases = config.get("aliases")
    if aliases is None:
        return {}
    if not isinstance(aliases, dict):
        raise SystemExit(f"{invalid_config_error}: aliases must be a table")
    alias_table = cast("dict[str, object]", aliases)
    validated_aliases: dict[str, str] = {}
    for name, template in alias_table.items():
        if not isinstance(template, str):
            raise SystemExit(f"{invalid_config_error}: alias {name} must be a string")
        validated_aliases[name] = template
    return validated_aliases


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


def state_directory() -> Path:
    golem_state_directory = xdg_golem_directory("XDG_STATE_HOME", Path.home() / ".local" / "state")
    golem_state_directory.mkdir(parents=True, exist_ok=True)
    return golem_state_directory


def ledger_path() -> Path:
    return state_directory() / "tasks.json"


def worktree_ledger_path() -> Path:
    return state_directory() / "worktrees.json"


def settings_path() -> Path:
    return state_directory() / "settings.json"


def load_json(path: Path) -> JsonObject:
    if not path.exists():
        return {}
    return cast("JsonObject", json.loads(path.read_text(encoding="utf-8")))


def ledger_temporary_prefix(task_ledger_path: Path) -> str:
    return f".{task_ledger_path.name}."


def load_tasks() -> Tasks:
    return cast("Tasks", load_json(ledger_path()))


def load_worktrees() -> Worktrees:
    return cast("Worktrees", load_json(worktree_ledger_path()))


def load_settings() -> Settings:
    return cast("Settings", load_json(settings_path()))


def save_json(path: Path, value: object) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=ledger_temporary_prefix(path),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(json.dumps(value, indent=2) + "\n")
        temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def save_tasks(tasks: Tasks) -> None:
    save_json(ledger_path(), tasks)


def save_settings(settings: Settings) -> None:
    save_json(settings_path(), settings)


@contextmanager
def locked_ledger() -> Generator[tuple[Tasks, Worktrees]]:
    lock_path = ledger_path().with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        tasks = load_tasks()
        worktrees = load_worktrees()
        original_tasks_text = json.dumps(tasks)
        original_worktrees_text = json.dumps(worktrees)
        yield tasks, worktrees
        if json.dumps(tasks) != original_tasks_text:
            save_tasks(tasks)
        if json.dumps(worktrees) != original_worktrees_text:
            save_json(worktree_ledger_path(), worktrees)


@contextmanager
def locked_tasks() -> Generator[Tasks]:
    with locked_ledger() as (tasks, _):
        yield tasks


def run_lock_directory() -> Path:
    lock_directory = ledger_path().parent / "running"
    lock_directory.mkdir(exist_ok=True)
    return lock_directory


def run_lock_path(session_id: str) -> Path:
    return run_lock_directory() / f"{session_id}.lock"


def remove_orphan_run_locks() -> None:
    for lock_path in run_lock_directory().glob("*.lock"):
        run_lock_file = acquire_run_lock(lock_path.stem)
        if run_lock_file is not None:
            release_run_lock(run_lock_file, lock_path.stem)


def remove_interrupted_ledger_writes() -> None:
    task_ledger_path = ledger_path()
    for temporary_path in task_ledger_path.parent.glob(
        f"{ledger_temporary_prefix(task_ledger_path)}*"
    ):
        temporary_path.unlink(missing_ok=True)


def acquire_lock(lock_path: Path) -> io.TextIOWrapper | None:
    lock_file = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def release_lock(lock_file: io.TextIOWrapper) -> None:
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()


def acquire_run_lock(session_id: str) -> io.TextIOWrapper | None:
    return acquire_lock(run_lock_path(session_id))


def release_run_lock(run_lock_file: io.TextIOWrapper, session_id: str) -> None:
    run_lock_path(session_id).unlink(missing_ok=True)
    release_lock(run_lock_file)


def directory_lock_path(directory: str) -> Path:
    lock_directory = ledger_path().parent / "directories"
    lock_directory.mkdir(exist_ok=True)
    resolved_directory = str(Path(directory).resolve())
    lock_name = hashlib.sha256(resolved_directory.encode()).hexdigest()
    return lock_directory / f"{lock_name}.lock"


def reserve_tasks(records: Tasks) -> None:
    with locked_tasks() as tasks:
        for task_id in records:
            if task_id in tasks:
                raise SystemExit(f"golem: task already exists: {task_id}")
        tasks.update(records)


def can_start_dependents(task: Task) -> bool:
    return task.get("status") in SUCCEEDED_STATUSES and "gate_reason" not in task


def waiting_dependent_ids(tasks: Tasks, parent_id: str) -> list[str]:
    return [
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent["status"] == STATUS_WAITING and dependent.get("after") == parent_id
    ]


def dependent_task_ids(tasks: Tasks, parent_id: str) -> list[str]:
    return sorted(
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent.get("after") == parent_id
    )


def descendant_ids_with_status(tasks: Tasks, task_id: str, statuses: set[str]) -> list[str]:
    matching_ids: list[str] = []
    pending_ids = [task_id]
    while pending_ids:
        for dependent_id in dependent_task_ids(tasks, pending_ids.pop()):
            pending_ids.append(dependent_id)
            if tasks[dependent_id]["status"] in statuses:
                matching_ids.append(dependent_id)
    return matching_ids


def has_running_dependent(tasks: Tasks, parent_id: str) -> bool:
    return any(
        dependent["status"] == STATUS_RUNNING and dependent.get("after") == parent_id
        for dependent in tasks.values()
    )


def has_unreviewed_dependent(tasks: Tasks, parent_id: str) -> bool:
    return any(
        dependent["status"] in UNREVIEWED_STATUSES and dependent.get("after") == parent_id
        for dependent in tasks.values()
    )


def mark_ancestors_done(tasks: Tasks, task: Task) -> None:
    ancestor = tasks.get(task.get("after", ""))
    while ancestor is not None:
        parent = ancestor
        if parent.get("status") == STATUS_NEEDS_REVIEW:
            parent["status"] = STATUS_DONE
        ancestor = tasks.get(parent.get("after", ""))


def mark_task_done(tasks: Tasks, task: Task) -> None:
    if task.get("status") != STATUS_NEEDS_REVIEW:
        return
    task["status"] = STATUS_DONE
    mark_ancestors_done(tasks, task)


def open_app_session_and_mark_done(task_id: str, session_id: str) -> int:
    returncode = subprocess.run(
        ["open", f"claude://resume?session={session_id}"], check=False
    ).returncode
    if returncode != 0:
        return returncode
    with locked_tasks() as tasks:
        current_task = tasks.get(task_id)
        if current_task is not None:
            mark_task_done(tasks, current_task)
    return 0


def reserve_dependent_task(records: Tasks, parent_id: str) -> bool:
    with locked_tasks() as tasks:
        for task_id in records:
            if task_id in tasks:
                raise SystemExit(f"golem: task already exists: {task_id}")
        if parent_id not in tasks:
            raise SystemExit(f"golem: task not found: {parent_id}")
        parent_status = tasks[parent_id]["status"]
        if parent_status == STATUS_CANCELLED:
            raise SystemExit(f"golem: parent task is cancelled: {parent_id}")
        if can_start_dependents(tasks[parent_id]) and not is_gate_pending(tasks, parent_id):
            return False
        if tasks[parent_id].get("auto_pickup"):
            for record in records.values():
                if record.get("backend") == BACKEND_CLAUDE:
                    record["auto_pickup"] = True
        tasks.update(records)
        return True


def launch_agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def golem_command() -> list[str]:
    return [str(Path(sys.executable).absolute()), str(Path(__file__).resolve())]


def agent_log_path() -> Path:
    return ledger_path().parent / "agent.log"


def rotate_agent_log() -> None:
    log_path = agent_log_path()
    if log_path.exists() and log_path.stat().st_size > AGENT_LOG_MAX_BYTES:
        shutil.copyfile(log_path, log_path.with_name(f"{log_path.name}.1"))
        log_path.write_text("", encoding="utf-8")


def launch_agent_plist_data() -> bytes:
    log_path = str(agent_log_path())
    plist: dict[str, object] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", *golem_command(), "_agent"],
        "RunAtLoad": True,
        "StartInterval": LAUNCH_AGENT_INTERVAL_SECONDS,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    state_home = os.environ.get("XDG_STATE_HOME", "")
    if Path(state_home).is_absolute():
        plist["EnvironmentVariables"] = {"XDG_STATE_HOME": state_home}
    return plistlib.dumps(plist)


def launch_agent_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"


def run_launchctl(arguments: list[str]) -> str | None:
    result = subprocess.run(
        ["launchctl", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or result.stdout.strip() or f"launchctl {arguments[0]} failed"


def install_launch_agent() -> int:
    plist_path = launch_agent_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(launch_agent_plist_data())
    run_launchctl(["bootout", launch_agent_target()])
    error = run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])
    if error is not None:
        plist_path.unlink()
        raise SystemExit(f"golem: could not install agent: {error}; run golem agent install again")
    return 0


def uninstall_launch_agent() -> int:
    run_launchctl(["bootout", launch_agent_target()])
    launch_agent_plist_path().unlink(missing_ok=True)
    return 0


def require_launch_agent() -> None:
    plist_path = launch_agent_plist_path()
    if not plist_path.exists() or plist_path.read_bytes() != launch_agent_plist_data():
        raise SystemExit(LAUNCH_AGENT_ERROR)


def wake_launch_agent_and_report(task_id: str, when: datetime) -> int:
    error = run_launchctl(["kickstart", launch_agent_target()])
    if error is not None:
        print(f"golem: could not wake agent: {error}", file=sys.stderr)
    if when <= datetime.now(UTC):
        print(f"golem: queued {task_id}")
        return 0
    print(f"golem: scheduled {task_id} for {when:%Y-%m-%d %H:%M %Z %z}")
    return 0


def new_session_id() -> str:
    return str(uuid.uuid4())


def is_captured_variable(name: str) -> bool:
    return name in {"PATH", "LANG", "CODEX_HOME"} or name.startswith("LC_")


def captured_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if is_captured_variable(name)}


def task_environment(task: Task) -> dict[str, str]:
    inherited_environment = {
        name: value for name, value in os.environ.items() if not is_captured_variable(name)
    }
    return {**inherited_environment, **task.get("environment", {})}


def codex_environment(environment: dict[str, str]) -> dict[str, str]:
    environment = environment.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODEX_API_KEY", None)
    return environment


def task_backend(task: Task) -> str:
    return task.get("backend", BACKEND_CLAUDE)


def has_agent_argument(agent_args: list[str], names: set[str]) -> bool:
    return any(
        argument == name or argument.startswith(f"{name}=")
        for argument in agent_args
        for name in names
    )


def codex_config_key(argument: str) -> str:
    return argument.split("=", 1)[0].strip()


def codex_config_values(agent_args: list[str]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(agent_args):
        argument = agent_args[index]
        if argument in CODEX_CONFIG_ARGUMENTS and index + 1 < len(agent_args):
            values.append(agent_args[index + 1])
            index += 2
            continue
        if argument.startswith("--config="):
            values.append(argument.removeprefix("--config="))
        index += 1
    return values


def has_codex_permission_argument(agent_args: list[str]) -> bool:
    return has_agent_argument(agent_args, CODEX_PERMISSION_ARGUMENTS) or any(
        codex_config_key(value) in {"sandbox_mode", "approval_policy"}
        for value in codex_config_values(agent_args)
    )


def reject_unsupported_codex_arguments(agent_args: list[str], *, is_run: bool) -> None:
    unsupported: set[str] = CODEX_UNSUPPORTED_ARGUMENTS | (WORKTREE_ARGUMENTS if is_run else set())
    for argument in agent_args:
        if argument in unsupported or any(argument.startswith(f"{name}=") for name in unsupported):
            raise SystemExit(f"golem: Codex argument is not supported: {argument}")
    for value in codex_config_values(agent_args):
        if codex_config_key(value) in {"model_provider", "forced_login_method"}:
            raise SystemExit(f"golem: Codex argument is not supported: {value}")


def resolved_path(path: str) -> str:
    return str(Path(path).resolve())


def run_git(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", directory, *arguments],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def git_common_directory(directory: str) -> str | None:
    result = run_git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode:
        return None
    return result.stdout.strip()


def git_has_head(directory: str) -> bool:
    return run_git(directory, "rev-parse", "--verify", "HEAD").returncode == 0


def recorded_worktree(tasks: Tasks, task: Task) -> tuple[str, bool] | None:
    if task.get("no_worktree"):
        return None
    ancestor = task
    while True:
        if "worktree" in ancestor:
            return ancestor["worktree"], ancestor.get("worktree_pending") is True
        parent = tasks.get(ancestor.get("after", ""))
        if parent is None:
            return None
        ancestor = parent
        if ancestor.get("cwd") != task.get("cwd"):
            return None


def worktree_resource(worktrees: Worktrees, worktree: str | None) -> WorktreeResource | None:
    if worktree is None:
        return None
    return worktrees.get(resolved_path(worktree))


def clear_worktree_release(tasks: Tasks, worktrees: Worktrees, task: Task) -> None:
    for worktree, resource in worktrees.items():
        if task_uses_worktree(tasks, task, worktree):
            resource.pop("released_at", None)


def record_worktree_resource(
    worktrees: Worktrees,
    worktree: str,
    session_id: str,
    *,
    owner: str = WORKTREE_OWNER_EXTERNAL,
    repository: str | None = None,
) -> None:
    if owner == WORKTREE_OWNER_EXTERNAL:
        repository = git_common_directory(worktree)
    resource: WorktreeResource = {
        "owner": owner,
        "session_id": session_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    if repository is not None:
        resource["repository"] = repository
    worktrees.setdefault(resolved_path(worktree), resource)


def worktree_owner_marker_path(worktree: str) -> Path | None:
    result = run_git(worktree, "rev-parse", "--absolute-git-dir")
    if result.returncode:
        return None
    return Path(result.stdout.strip()) / WORKTREE_OWNER_MARKER


def is_worktree_owner_verified(worktree: str, session_id: str) -> bool:
    marker_path = worktree_owner_marker_path(worktree)
    if marker_path is None:
        return False
    try:
        return marker_path.read_text(encoding="utf-8") == session_id
    except OSError:
        return False


def record_created_worktree_owner(task_id: str, task: Task) -> None:
    worktree = task.get("worktree")
    if worktree is None:
        return
    with locked_ledger() as (tasks, worktrees):
        current_task = tasks.get(task_id)
        resource = worktree_resource(worktrees, worktree)
        if (
            current_task is None
            or current_task.get("session_id") != task["session_id"]
            or current_task.get("worktree") != worktree
            or resource is None
            or resource.get("owner") != WORKTREE_OWNER_GOLEM
            or git_common_directory(worktree) != resource.get("repository")
        ):
            return
        marker_path = worktree_owner_marker_path(worktree)
        if marker_path is None:
            return
        try:
            with marker_path.open("x", encoding="utf-8") as marker_file:
                marker_file.write(resource["session_id"])
        except FileExistsError:
            return
        except OSError as error:
            print(f"golem: could not record worktree ownership: {error}", file=sys.stderr)


def is_worktree_left_to_agent(task: Task) -> bool:
    return bool(task.get("no_worktree")) or has_agent_argument(
        task["agent_args"], WORKTREE_ARGUMENTS
    )


def resolved_worktree(tasks: Tasks, task: Task) -> tuple[str | None, bool]:
    recorded = recorded_worktree(tasks, task)
    if recorded is not None:
        return recorded
    if is_worktree_left_to_agent(task):
        return None, False
    common_directory = git_common_directory(task["cwd"])
    if common_directory is None or not git_has_head(task["cwd"]):
        return None, False
    return str(Path(common_directory).parent / ".claude" / "worktrees" / task["session_id"]), True


def task_directory(task: Task) -> str:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        return worktree
    return task["cwd"]


def directory_to_lock(task: Task, worktree: str | None) -> str:
    return worktree if worktree is not None else task["cwd"]


def worktree_missing_message(
    worktree: str | None,
    worktrees: Worktrees,
    *,
    is_worktree_pending: bool,
) -> str | None:
    if worktree is None or is_worktree_pending or Path(worktree).is_dir():
        return None
    resource = worktree_resource(worktrees, worktree)
    if resource is not None and resource.get("removed_at") is not None:
        return f"golem: worktree was removed: {worktree}"
    return f"golem: worktree is missing: {worktree}"


def reject_missing_worktree(task: Task, worktrees: Worktrees) -> None:
    missing_worktree = worktree_missing_message(
        task.get("worktree"), worktrees, is_worktree_pending=task.get("worktree_pending") is True
    )
    if missing_worktree is not None:
        raise SystemExit(missing_worktree)


def clear_worktree_pending_if_created(task: Task) -> None:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        task.pop("worktree_pending", None)


def record_resolved_worktree(
    task: Task, worktree: str | None, *, is_worktree_pending: bool
) -> None:
    if worktree is None:
        return
    task["worktree"] = worktree
    if is_worktree_pending:
        task["worktree_pending"] = True
    else:
        task.pop("worktree_pending", None)
    clear_worktree_pending_if_created(task)


def is_usage_limit_resume(task: Task) -> bool:
    return task.get("usage_limit_resume_count", 0) > 0


def progress_path(session_id: str) -> Path:
    return state_directory() / "progress" / f"{session_id}.log"


def rotated_progress_path(session_id: str) -> Path:
    path = progress_path(session_id)
    return path.with_name(f"{path.name}.1")


def remove_progress(session_id: str) -> None:
    progress_path(session_id).unlink(missing_ok=True)
    rotated_progress_path(session_id).unlink(missing_ok=True)


@contextmanager
def progress_writer(session_id: str) -> Generator[Callable[[str], None]]:
    path = progress_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    progress_file = path.open("a", encoding="utf-8", errors="replace")

    def record_progress(text: str) -> None:
        nonlocal progress_file
        if progress_file.closed:
            return
        try:
            if progress_file.tell() > AGENT_LOG_MAX_BYTES:
                progress_file.close()
                path.replace(rotated_progress_path(session_id))
                progress_file = path.open("a", encoding="utf-8", errors="replace")
            progress_file.write(text if text.endswith("\n") else f"{text}\n")
            progress_file.flush()
        except OSError:
            with suppress(OSError):
                progress_file.close()

    try:
        yield record_progress
    finally:
        progress_file.close()


def tool_call_progress(name: str, argument: object) -> str:
    line = (
        f"[tool] {name} {first_line(argument)}"
        if isinstance(argument, str) and argument
        else f"[tool] {name}"
    )
    return line[:PROGRESS_LINE_WIDTH]


def claude_event_progress(event: object) -> list[str]:
    if not isinstance(event, dict):
        return []
    event_fields = cast("dict[str, object]", event)
    if (
        event_fields.get("type") != "assistant"
        or event_fields.get("parent_tool_use_id") is not None
    ):
        return []
    message = event_fields.get("message")
    content = (
        cast("dict[str, object]", message).get("content") if isinstance(message, dict) else None
    )
    if not isinstance(content, list):
        return []
    progress: list[str] = []
    for item in cast("list[object]", content):
        if not isinstance(item, dict):
            continue
        item_fields = cast("dict[str, object]", item)
        text = item_fields.get("text")
        name = item_fields.get("name")
        if item_fields.get("type") == "text" and isinstance(text, str):
            progress.append(text)
        elif item_fields.get("type") == "tool_use" and isinstance(name, str):
            tool_input = item_fields.get("input")
            input_fields = (
                cast("dict[str, object]", tool_input) if isinstance(tool_input, dict) else {}
            )
            argument = next(
                (
                    input_fields[key]
                    for key in PROGRESS_TOOL_INPUT_KEYS
                    if isinstance(input_fields.get(key), str)
                ),
                None,
            )
            progress.append(tool_call_progress(name, argument))
    return progress


def run_claude(
    task_id: str, task: Task, lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str, str]:
    session_id = task["session_id"]
    is_resuming = is_usage_limit_resume(task)
    command = (
        ["claude", "--resume", session_id, "--print", USAGE_LIMIT_RESUME_PROMPT]
        if is_resuming
        else ["claude", "--session-id", session_id, "--print", task["prompt"]]
    )
    agent_args = task["agent_args"]
    default_args: list[str] = []
    if VERBOSE_ARGUMENT not in agent_args:
        default_args.append(VERBOSE_ARGUMENT)
    if not has_agent_argument(agent_args, NAME_ARGUMENTS):
        default_args += ["--name", task_id]
    worktree = task.get("worktree")
    is_creating_worktree = worktree is not None and not Path(worktree).is_dir()
    if worktree is not None and is_creating_worktree:
        default_args += ["--worktree", Path(worktree).name]
    if not has_agent_argument(agent_args, PERMISSION_ARGUMENTS):
        default_args.append("--dangerously-skip-permissions")
    stdout_lines: list[str] = []
    with (
        progress_writer(session_id) as record_progress,
        tempfile.TemporaryFile("w+", encoding="utf-8") as stderr_file,
    ):
        claude_process = subprocess.Popen(
            [
                *command,
                "--output-format",
                "stream-json",
                *agent_args,
                *default_args,
            ],
            cwd=task_directory(task),
            env={**task_environment(task), BACKGROUND_WAIT_CEILING_VARIABLE: "0"},
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            encoding="utf-8",
            pass_fds=lock_file_descriptors,
        )
        with cast("io.TextIOWrapper", claude_process.stdout) as output:
            try:
                for line in output:
                    stdout_lines.append(line)
                    with suppress(json.JSONDecodeError):
                        for progress in claude_event_progress(cast("object", json.loads(line))):
                            record_progress(progress)
                returncode = claude_process.wait()
            except BaseException:
                claude_process.kill()
                claude_process.wait()
                raise
        stderr_file.seek(0)
        stderr = stderr_file.read()
    sys.stderr.write(stderr)
    if is_creating_worktree:
        record_created_worktree_owner(task_id, task)
    return returncode, "".join(stdout_lines), stderr


def fetch_stale_default_branch(cwd: str) -> None:
    default_branch = run_git(cwd, "symbolic-ref", "--quiet", "--short", ORIGIN_HEAD)
    fetch_head = run_git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD")
    if default_branch.returncode or fetch_head.returncode:
        return
    fetch_head_path = Path(fetch_head.stdout.strip())
    if (
        fetch_head_path.exists()
        and time.time() - fetch_head_path.stat().st_mtime < GIT_FETCH_MAX_AGE_SECONDS
    ):
        return
    with suppress(subprocess.TimeoutExpired):
        subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                default_branch.stdout.strip().removeprefix("origin/"),
            ],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GIT_FETCH_TIMEOUT_SECONDS,
            check=False,
        )


def create_codex_worktree(task_id: str, task: Task) -> str | None:
    worktree = task.get("worktree")
    if worktree is None:
        return "golem: task has no worktree"
    cwd = task["cwd"]
    fetch_stale_default_branch(cwd)
    base = ORIGIN_HEAD
    if run_git(cwd, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode:
        base = "HEAD"
    result = run_git(
        cwd,
        "worktree",
        "add",
        "--no-track",
        "-b",
        f"worktree-{Path(worktree).name}",
        worktree,
        base,
    )
    if result.returncode:
        error = result.stderr.strip() or result.stdout.strip()
        return f"golem: could not create worktree: {error}"
    common_directory = cast("str", git_common_directory(cwd))
    exclude_path = Path(common_directory) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_text = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if ".claude/worktrees/" not in exclude_text.splitlines():
        separator = "\n" if exclude_text and not exclude_text.endswith("\n") else ""
        with exclude_path.open("a", encoding="utf-8") as exclude_file:
            exclude_file.write(f"{separator}.claude/worktrees/\n")
    record_created_worktree_owner(task_id, task)
    return None


def record_codex_thread(task_id: str, session_id: str, thread_id: str) -> None:
    with locked_tasks() as tasks:
        stored_task = tasks.get(task_id)
        if stored_task is not None:
            task = stored_task
            if task.get("session_id") == session_id:
                task["thread_id"] = thread_id


def codex_event_message(event: dict[str, object]) -> str | None:
    message = event.get("message")
    return message if isinstance(message, str) else None


def codex_command(task: Task) -> tuple[list[str], str]:
    agent_args = task["agent_args"]
    default_args = (
        []
        if has_codex_permission_argument(agent_args)
        else ["--dangerously-bypass-approvals-and-sandbox"]
    )
    command = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        *agent_args,
        *default_args,
        *CODEX_PROVIDER_ARGUMENTS,
    ]
    prompt = task["prompt"]
    if is_usage_limit_resume(task):
        thread_id = task.get("thread_id")
        if thread_id is None:
            raise KeyError("thread_id")
        command += ["resume", thread_id, "-"]
        return command, USAGE_LIMIT_RESUME_PROMPT
    command.append("-")
    return command, prompt


@dataclass
class CodexEventState:
    last_agent_message: str | None = None
    failed_message: str | None = None
    last_error_message: str | None = None


def codex_item_progress(item: dict[str, object]) -> str | None:
    item_type = item.get("type")
    if item_type == "agent_message":
        text = item.get("text")
        return text if isinstance(text, str) else None
    if item_type == "command_execution":
        return tool_call_progress("command", item.get("command"))
    if item_type == "file_change":
        changes = item.get("changes")
        paths = [
            cast("dict[str, object]", change).get("path")
            for change in (cast("list[object]", changes) if isinstance(changes, list) else [])
            if isinstance(change, dict)
        ]
        return tool_call_progress(
            "file_change", " ".join(path for path in paths if isinstance(path, str))
        )
    if item_type == "mcp_tool_call":
        return tool_call_progress("mcp", item.get("tool"))
    if item_type == "web_search":
        return tool_call_progress("web_search", item.get("query"))
    return None


def update_codex_event_state(
    event: dict[str, object],
    task_id: str,
    session_id: str,
    state: CodexEventState,
    record_progress: Callable[[str], None],
) -> None:
    event_type = event.get("type")
    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str):
            record_codex_thread(task_id, session_id, thread_id)
        return
    if event_type == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_fields = cast("dict[str, object]", item)
        if progress := codex_item_progress(item_fields):
            record_progress(progress)
        text = item_fields.get("text") if item_fields.get("type") == "agent_message" else None
        if isinstance(text, str):
            state.last_agent_message = text
        return
    if event_type == "turn.failed":
        error = event.get("error")
        if isinstance(error, dict):
            state.failed_message = codex_event_message(cast("dict[str, object]", error))
        return
    if event_type == "error":
        state.last_error_message = codex_event_message(event)


def read_codex_events(
    process: subprocess.Popen[str],
    task_id: str,
    session_id: str,
    record_progress: Callable[[str], None],
) -> CodexEventState:
    state = CodexEventState()
    output = cast("io.TextIOWrapper", process.stdout)
    try:
        for line in output:
            try:
                event = cast("object", json.loads(line))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                update_codex_event_state(
                    cast("dict[str, object]", event), task_id, session_id, state, record_progress
                )
    finally:
        output.close()
    return state


def run_codex(
    task_id: str, task: Task, lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str | None, bool]:
    session_id = task["session_id"]
    with progress_writer(session_id) as record_progress:
        try:
            require_codex_subscription(task_environment(task))
        except SystemExit as error:
            return 1, str(error), False
        worktree = task.get("worktree")
        if worktree is not None and not Path(worktree).is_dir():
            if task.get("worktree_pending") is not True:
                return 1, f"golem: worktree is missing: {worktree}", False
            if error := create_codex_worktree(task_id, task):
                return 1, error, False
        if is_usage_limit_resume(task) and not isinstance(task.get("thread_id"), str):
            return 1, "golem: task has no Codex conversation", False
        command, prompt = codex_command(task)
        process = subprocess.Popen(
            command,
            cwd=task_directory(task),
            env=codex_environment(task_environment(task)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            pass_fds=lock_file_descriptors,
        )
        standard_input = cast("io.TextIOWrapper", process.stdin)
        try:
            with suppress(BrokenPipeError):
                standard_input.write(prompt)
        finally:
            with suppress(BrokenPipeError):
                standard_input.close()
        try:
            event_state = read_codex_events(process, task_id, session_id, record_progress)
            returncode = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
    result = (
        event_state.failed_message
        or event_state.last_agent_message
        or event_state.last_error_message
    )
    is_usage_limit = bool(
        event_state.failed_message
        and event_state.failed_message.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").startswith(
            "You've hit your usage limit"
        )
    )
    return returncode, result, is_usage_limit


@dataclass(frozen=True)
class RunPlacement:
    cwd: str
    is_auto_pickup: bool = False
    is_no_worktree: bool = False
    gate: str | None = None
    setup: str | None = None


def new_task_record(
    placement: RunPlacement,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    backend: str,
    *,
    after: str | None = None,
) -> Task:
    record: Task = {
        "cwd": placement.cwd,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "status": STATUS_WAITING,
        "session_id": new_session_id(),
        "environment": captured_environment(),
        "backend": backend,
    }
    if after is not None:
        record["after"] = after
    if placement.is_auto_pickup:
        record["auto_pickup"] = True
    if placement.is_no_worktree:
        record["no_worktree"] = True
    if placement.gate is not None:
        record["gate"] = placement.gate
    if placement.setup is not None:
        record["setup"] = placement.setup
    if when is not None:
        record["status"] = STATUS_SCHEDULED
        record["due_at"] = when.isoformat()
    return record


def new_follow_up_records(
    run_task_id: str,
    follow_ups: list[list[str]],
    agent_args: tuple[str, ...],
    backend: str,
    placement: RunPlacement,
) -> Tasks:
    records: Tasks = {}
    previous_task_id = run_task_id
    placement = replace(placement, setup=None)
    for task_id, prompt in follow_ups:
        if task_id == run_task_id or task_id in records:
            raise SystemExit(f"golem: task ID used more than once: {task_id}")
        records[task_id] = new_task_record(
            placement, prompt, agent_args, None, backend, after=previous_task_id
        )
        previous_task_id = task_id
    return records


def pickup(task_id: str, *agent_args: str, is_app: bool, is_review_required: bool = False) -> int:
    preflight_task = load_tasks().get(task_id)
    is_codex = preflight_task is not None and task_backend(preflight_task) == BACKEND_CODEX
    if is_codex and is_app:
        raise SystemExit(f"golem: pickup --app does not support Codex tasks: {task_id}")
    if is_codex:
        reject_unsupported_codex_arguments(list(agent_args), is_run=False)
        require_codex_subscription(os.environ.copy())
    with locked_ledger() as (tasks, worktrees):
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if is_review_required and task.get("status") != STATUS_NEEDS_REVIEW:
            return 0
        if task.get("status") in NOT_STARTED_STATUSES:
            raise SystemExit(f"golem: task has not started: {task_id}")
        reject_missing_worktree(task, worktrees)
        directory = directory_to_lock(task, task.get("worktree"))
        directory_lock_file = acquire_lock(directory_lock_path(directory))
        if directory_lock_file is None and task.get("status") == STATUS_RUNNING:
            raise SystemExit(
                f"golem: task is running: {task_id}; use golem watch {task_id} to follow it"
            )
        if directory_lock_file is None:
            raise SystemExit(f"golem: directory is busy: {directory}")
        thread_id = task.get("thread_id")
        if is_codex and not isinstance(thread_id, str):
            release_lock(directory_lock_file)
            raise SystemExit(f"golem: task has no Codex conversation: {task_id}")
        clear_worktree_release(tasks, worktrees, task)
        if not is_app:
            mark_task_done(tasks, task)
        cwd = task_directory(task)
        session_id = task["session_id"]
    if is_app:
        release_lock(directory_lock_file)
        return open_app_session_and_mark_done(task_id, session_id)
    try:
        if is_codex:
            return subprocess.run(
                [
                    "codex",
                    "resume",
                    cast("str", thread_id),
                    *agent_args,
                    *CODEX_PROVIDER_ARGUMENTS,
                ],
                cwd=cwd,
                env=codex_environment(os.environ.copy()),
                pass_fds=(directory_lock_file.fileno(),),
                check=False,
            ).returncode
        return subprocess.run(
            ["claude", "--resume", session_id, *agent_args],
            cwd=cwd,
            pass_fds=(directory_lock_file.fileno(),),
            check=False,
        ).returncode
    finally:
        release_lock(directory_lock_file)


def is_ready_for_review(tasks: Tasks, task_id: str) -> bool:
    task = tasks[task_id]
    return task.get("status") == STATUS_NEEDS_REVIEW and (
        "gate_reason" in task or not has_unreviewed_dependent(tasks, task_id)
    )


def pickup_all() -> int:
    tasks = load_tasks()
    task_ids = [task_id for task_id in tasks if is_ready_for_review(tasks, task_id)]
    if not task_ids:
        print("golem: no tasks ready for review")
        return 0

    has_failure = False
    for task_id in task_ids:
        try:
            has_failure |= pickup(task_id, is_app=True, is_review_required=True) != 0
        except SystemExit as error:
            print(error, file=sys.stderr)
            has_failure = True
    return int(has_failure)


def auto_pickup_task(task_id: str) -> None:
    tasks = load_tasks()
    task = tasks.get(task_id)
    if task is None:
        return
    if task.get("status") == STATUS_FAILED:
        auto_pickup_task(task.get("after", ""))
        return
    if not task.get("auto_pickup") or not is_ready_for_review(tasks, task_id):
        return
    try:
        returncode = pickup(task_id, is_app=True, is_review_required=True)
    except SystemExit as error:
        print(error, file=sys.stderr)
        return
    if returncode != 0:
        print(f"golem: could not open {task_id} in the desktop app", file=sys.stderr)


def close(task_id: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if task.get("status") != STATUS_NEEDS_REVIEW:
            raise SystemExit(f"golem: task does not need review: {task_id}")
        task["status"] = STATUS_DONE
    return 0


def show_task_result(task_id: str) -> int:
    tasks = load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    task = tasks[task_id]
    if "gate_reason" in task:
        print(f"golem: gate stopped: {task['gate_reason']}", file=sys.stderr)
    if "setup_reason" in task:
        print(f"golem: setup failed: {task['setup_reason']}", file=sys.stderr)
    if task.get("setup_complete"):
        print(f"golem: directory: {task['cwd']}", file=sys.stderr)
        print(f"golem: prompt:\n{task['prompt']}", file=sys.stderr)
    result = task.get("result")
    if not isinstance(result, str):
        raise SystemExit(f"golem: task has no result: {task_id}")
    print_task_result(result)
    return 0


def is_usage_limit_pause(task: Task) -> bool:
    return task.get("status") == STATUS_SCHEDULED and is_usage_limit_resume(task)


def task_end_line(task_id: str, task: Task) -> str:
    due_at = task.get("due_at")
    if due_at is not None and is_usage_limit_pause(task):
        resume_time = display_due_at(due_at, datetime.now().astimezone().date())
        return f"golem: task paused at the usage limit, resumes {resume_time}: {task_id}"
    return f"golem: task {task['status']}: {task_id}"


def print_new_progress(progress_file: BinaryIO) -> None:
    new_bytes = progress_file.read()
    complete_lines = new_bytes[: new_bytes.rfind(b"\n") + 1]
    progress_file.seek(len(complete_lines) - len(new_bytes), os.SEEK_CUR)
    sys.stdout.write(complete_lines.decode("utf-8"))
    sys.stdout.flush()


def follow_progress_until_stopped(task_id: str, path: Path, progress_file: BinaryIO) -> Task:
    try:
        while True:
            task = existing_task(load_tasks(), task_id)
            print_new_progress(progress_file)
            with suppress(FileNotFoundError):
                while path.stat().st_ino != os.fstat(progress_file.fileno()).st_ino:
                    print_new_progress(progress_file)
                    rotated_file = progress_file
                    progress_file = path.open("rb")
                    rotated_file.close()
                    print_new_progress(progress_file)
            if task.get("status") != STATUS_RUNNING:
                return task
            time.sleep(WATCH_POLL_SECONDS)
    finally:
        progress_file.close()


def watch_task(task_id: str) -> int:
    task = existing_task(load_tasks(), task_id)
    if task.get("status") in NOT_STARTED_STATUSES and not is_usage_limit_pause(task):
        raise SystemExit(f"golem: task has not started: {task_id}")
    session_id = task["session_id"]
    rotated_path = rotated_progress_path(session_id)
    path = progress_path(session_id)
    try:
        progress_file = path.open("rb")
    except FileNotFoundError:
        raise SystemExit(f"golem: task has no recorded progress: {task_id}") from None
    try:
        with suppress(FileNotFoundError), rotated_path.open("rb") as rotated_file:
            if os.fstat(rotated_file.fileno()).st_ino != os.fstat(progress_file.fileno()).st_ino:
                print_new_progress(rotated_file)
        print(task_end_line(task_id, follow_progress_until_stopped(task_id, path, progress_file)))
    except KeyboardInterrupt:
        return 130
    return 0


def cancel_task(task_id: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if task.get("status") not in {STATUS_SCHEDULED, STATUS_WAITING}:
            raise SystemExit(f"golem: task is not scheduled: {task_id}")
        task["status"] = STATUS_CANCELLED
        pending_task_ids = [task_id]
        while pending_task_ids:
            parent_id = pending_task_ids.pop()
            for waiting_task_id in waiting_dependent_ids(tasks, parent_id):
                waiting_task = tasks[waiting_task_id]
                waiting_task["status"] = STATUS_CANCELLED
                pending_task_ids.append(waiting_task_id)
        directories = hook_affected_directories(tasks, task)
    for directory in directories:
        sync_hooks(directory)
    return 0


def display_directory(directory: str) -> str:
    try:
        return str(Path("~") / Path(directory).relative_to(Path.home()))
    except ValueError:
        return directory


def display_due_at(due_at: str, today: date) -> str:
    local_due_at = datetime.fromisoformat(due_at).astimezone()
    day_text = f"{local_due_at:%b} {local_due_at.day}, {local_due_at:%Y}"
    if local_due_at.date() == today:
        day_text = "today"
    elif local_due_at.date() == today + timedelta(days=1):
        day_text = "tomorrow"
    time_text = local_due_at.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day_text} {time_text} {local_due_at:%Z}"


def is_color_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def display_status(status: str, width: int, *, is_color: bool) -> str:
    padded_status = status.ljust(width)
    if not is_color:
        return padded_status
    return f"\033[38;5;{STATUS_COLORS[status]}m{padded_status}{ANSI_RESET}"


def first_line(text: str) -> str:
    return next(iter(text.splitlines()), "")


def list_tasks() -> int:
    tasks = load_tasks()
    if not tasks:
        print("golem: no tasks")
        return 0
    today = datetime.now().astimezone().date()
    rows: list[tuple[str, str, str, str, str, str]] = [
        (
            task_id,
            task["status"],
            display_directory(task["cwd"]),
            display_due_at(task["due_at"], today) if "due_at" in task else "",
            task.get("after", "") if task["status"] == STATUS_WAITING else "",
            first_line(task.get("gate_reason") or task.get("setup_reason", "")),
        )
        for task_id, task in tasks.items()
    ]
    rows.sort(key=lambda row: (LIST_STATUS_ORDER.index(row[1]), row[0]))
    headers = ("ID", "STATUS", "DIRECTORY", "DUE AT", "AFTER", "REASON")
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    is_color = is_color_enabled()
    print(
        "  ".join(value.ljust(width) for value, width in zip(headers, widths, strict=True)).rstrip()
    )
    for task_id, status, directory, due_at, after, reason in rows:
        print(
            "  ".join(
                (
                    task_id.ljust(widths[0]),
                    display_status(status, widths[1], is_color=is_color),
                    directory.ljust(widths[2]),
                    due_at.ljust(widths[3]),
                    after.ljust(widths[4]),
                    reason.ljust(widths[5]),
                )
            ).rstrip()
        )
    return 0


def scheduled_time(value: str) -> datetime:
    match = re.fullmatch(r"(1[0-2]|[1-9])(?::([0-5][0-9]))?(am|pm)( tomorrow)?", value.lower())
    if match is None:
        raise SystemExit('golem: --at expects a time like 9pm or "8:30am tomorrow"')
    hour, minute, period, tomorrow = match.groups()
    now = datetime.now(UTC)
    local_time = now.astimezone().replace(tzinfo=None)
    when = local_time.replace(
        hour=int(hour) % 12 + (12 if period == "pm" else 0),
        minute=int(minute or 0),
        second=0,
        microsecond=0,
    )
    if tomorrow or when.astimezone() <= now:
        when += timedelta(days=1)
    return when.astimezone()


def next_reset_time() -> datetime | None:
    requests = (
        {
            "type": "control_request",
            "request_id": "init",
            "request": {"subtype": "initialize"},
        },
        {
            "type": "control_request",
            "request_id": "usage",
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        },
    )
    child_environment = os.environ.copy()
    child_environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                "haiku",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--no-session-persistence",
                VERBOSE_ARGUMENT,
            ],
            input="".join(json.dumps(request) + "\n" for request in requests),
            env=child_environment,
            capture_output=True,
            encoding="utf-8",
            timeout=USAGE_TIMEOUT_SECONDS,
            check=False,
        )
        frames = [
            cast("ClaudeControlFrame", json.loads(line)) for line in result.stdout.splitlines()
        ]
        responses = {
            frame["response"]["request_id"]: frame["response"]
            for frame in frames
            if frame["type"] == "control_response"
        }
        initialized = responses["init"]["subtype"] == "success"
        usage = responses["usage"]
        if (
            result.returncode
            or not initialized
            or usage["subtype"] != "success"
            or usage["response"]["rate_limits_available"] is not True
        ):
            raise SystemExit(USAGE_ERROR)
        rate_limits = usage["response"]["rate_limits"]
        five_hour_limit = rate_limits["five_hour"]
        seven_day_limit = rate_limits.get("seven_day")
        exhausted_limits = [
            limit
            for limit in (five_hour_limit, seven_day_limit)
            if limit is not None and limit["utilization"] >= FULL_UTILIZATION
        ]
        if exhausted_limits:
            return max(reset_time_from_limit(limit) for limit in exhausted_limits)
        if five_hour_limit is None or five_hour_limit["resets_at"] is None:
            return None
        return reset_time_from_limit(five_hour_limit)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
    ) as error:
        raise SystemExit(USAGE_ERROR) from error


def codex_app_server_requests(*, has_rate_limits: bool) -> AppServerRequests:
    requests: AppServerRequests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "golem", "version": "0.1.0"}},
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/read", "params": {}},
    ]
    if has_rate_limits:
        requests.append({"id": 3, "method": "account/rateLimits/read"})
    return requests


def read_codex_app_server_output(
    output: io.TextIOWrapper, response_lines: queue.Queue[str | None]
) -> None:
    for line in output:
        response_lines.put(line)
    response_lines.put(None)


def codex_app_server_response(
    response_lines: queue.Queue[str | None], deadline: float
) -> JsonObject:
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise subprocess.TimeoutExpired("codex app-server", USAGE_TIMEOUT_SECONDS)
    try:
        line = response_lines.get(timeout=timeout)
    except queue.Empty as error:
        raise subprocess.TimeoutExpired("codex app-server", USAGE_TIMEOUT_SECONDS) from error
    if not line:
        raise OSError("Codex app-server exited before responding")
    response = cast("object", json.loads(line))
    if not isinstance(response, dict):
        raise TypeError("invalid Codex app-server response")
    return cast("JsonObject", response)


def validate_codex_app_server(
    process: subprocess.Popen[str], responses: AppServerResponses
) -> None:
    if process.returncode:
        raise OSError("Codex app-server failed")
    if any("error" in response or "result" not in response for response in responses.values()):
        raise ValueError("Codex app-server returned an error")


def codex_app_server_responses(
    environment: dict[str, str], *, has_rate_limits: bool
) -> AppServerResponses:
    process: subprocess.Popen[str] | None = None
    response_reader: threading.Thread | None = None
    standard_output: io.TextIOWrapper | None = None
    try:
        process = subprocess.Popen(
            ["codex", "app-server", *CODEX_PROVIDER_ARGUMENTS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=codex_environment(environment),
            encoding="utf-8",
        )
        standard_input = cast("io.TextIOWrapper", process.stdin)
        standard_output = cast("io.TextIOWrapper", process.stdout)
        response_lines: queue.Queue[str | None] = queue.Queue()
        response_reader = threading.Thread(
            target=read_codex_app_server_output,
            args=(standard_output, response_lines),
            daemon=True,
        )
        response_reader.start()
        requests = codex_app_server_requests(has_rate_limits=has_rate_limits)
        standard_input.write("".join(json.dumps(request) + "\n" for request in requests))
        standard_input.flush()
        responses: AppServerResponses = {}
        required_ids = {request["id"] for request in requests if isinstance(request.get("id"), int)}
        deadline = time.monotonic() + USAGE_TIMEOUT_SECONDS
        while required_ids - responses.keys():
            response = codex_app_server_response(response_lines, deadline)
            response_id = response.get("id")
            if isinstance(response_id, int):
                responses[response_id] = response
        standard_input.close()
        process.wait(timeout=max(0, deadline - time.monotonic()))
        validate_codex_app_server(process, responses)
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
    ) as error:
        raise SystemExit(CODEX_ACCOUNT_ERROR) from error
    else:
        return responses
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            with suppress(BrokenPipeError):
                cast("io.TextIOWrapper", process.stdin).close()
        if response_reader is not None:
            response_reader.join()
        if standard_output is not None:
            standard_output.close()


def validate_codex_subscription(responses: AppServerResponses) -> None:
    account_result = responses[2]["result"]
    if not isinstance(account_result, dict):
        raise SystemExit(CODEX_ACCOUNT_ERROR)
    account_result_fields = cast("dict[str, object]", account_result)
    account = account_result_fields.get("account")
    account_fields = cast("dict[str, object]", account) if isinstance(account, dict) else None
    if (
        account_fields is None
        or account_fields.get("type") != "chatgpt"
        or account_result_fields.get("requiresOpenaiAuth") is not True
    ):
        raise SystemExit(CODEX_SUBSCRIPTION_ERROR)


def require_codex_subscription(environment: dict[str, str]) -> None:
    validate_codex_subscription(codex_app_server_responses(environment, has_rate_limits=False))


def codex_rate_limits() -> JsonObject:
    responses = codex_app_server_responses(os.environ.copy(), has_rate_limits=True)
    validate_codex_subscription(responses)
    rate_limits_result = responses[3]["result"]
    rate_limits_fields = (
        cast("dict[str, object]", rate_limits_result)
        if isinstance(rate_limits_result, dict)
        else None
    )
    if rate_limits_fields is None or not isinstance(rate_limits_fields.get("rateLimits"), dict):
        raise SystemExit(CODEX_ACCOUNT_ERROR)
    return cast("JsonObject", rate_limits_fields["rateLimits"])


def codex_reset_time_from_limit(limit: JsonObject) -> datetime:
    resets_at = limit.get("resetsAt")
    if not isinstance(resets_at, int | float):
        raise SystemExit(USAGE_ERROR)
    return validated_reset_time(datetime.fromtimestamp(resets_at, UTC))


def required_codex_primary_limit(rate_limits: JsonObject) -> JsonObject | None:
    if "primary" not in rate_limits:
        raise SystemExit(USAGE_ERROR)
    primary = rate_limits["primary"]
    return cast("JsonObject", primary) if isinstance(primary, dict) else None


def codex_active_reset_time(primary: JsonObject) -> datetime | None:
    used_percent = primary.get("usedPercent")
    if primary.get("resetsAt") is None or used_percent == 0:
        return None
    if not isinstance(used_percent, int | float):
        raise SystemExit(USAGE_ERROR)
    return codex_reset_time_from_limit(primary)


def next_codex_reset_time() -> datetime | None:
    try:
        rate_limits = codex_rate_limits()
        primary = required_codex_primary_limit(rate_limits)
        secondary = rate_limits.get("secondary")
        limits = [
            cast("JsonObject", limit) for limit in (primary, secondary) if isinstance(limit, dict)
        ]
        exhausted_limits = [
            limit
            for limit in limits
            if isinstance(used_percent := limit.get("usedPercent"), int | float)
            and used_percent >= FULL_UTILIZATION
        ]
        if exhausted_limits:
            return max(codex_reset_time_from_limit(limit) for limit in exhausted_limits)
        if primary is None:
            return None
        return codex_active_reset_time(primary)
    except SystemExit as error:
        if str(error) == CODEX_SUBSCRIPTION_ERROR:
            raise
        raise SystemExit(USAGE_ERROR) from error


def validated_reset_time(reset_time: datetime) -> datetime:
    if reset_time.tzinfo is None or reset_time <= datetime.now(UTC):
        raise SystemExit(USAGE_ERROR)
    return reset_time


def reset_time_from_limit(limit: ClaudeRateLimit) -> datetime:
    resets_at = limit["resets_at"]
    if resets_at is None:
        raise SystemExit(USAGE_ERROR)
    return validated_reset_time(datetime.fromisoformat(resets_at))


def schedule_new_task(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None,
    *,
    backend: str,
    placement: RunPlacement,
    follow_up_records: Tasks,
) -> int:
    due_time = when or datetime.now(UTC).astimezone()
    record = new_task_record(placement, prompt, agent_args, due_time, backend, after=after)
    reserve_tasks({task_id: record, **follow_up_records})
    return wake_launch_agent_and_report(task_id, due_time)


def parsed_claude_output(output: str) -> object:
    with suppress(json.JSONDecodeError):
        return cast("object", json.loads(output))
    events: list[object] = []
    for line in output.splitlines():
        with suppress(json.JSONDecodeError):
            events.append(cast("object", json.loads(line)))
    return events


def claude_result_and_usage_limit(output: str) -> tuple[str, bool]:
    parsed_response = parsed_claude_output(output)
    if isinstance(parsed_response, list):
        parsed_response = next(
            (
                cast("dict[str, object]", message)
                for message in reversed(cast("list[object]", parsed_response))
                if isinstance(message, dict)
                and cast("dict[str, object]", message).get("type") == "result"
            ),
            None,
        )
    if not isinstance(parsed_response, dict):
        return output, False
    response = cast("dict[str, object]", parsed_response)
    result = response.get("result")
    if not isinstance(result, str):
        return output, False
    return result, response.get("api_error_status") == USAGE_LIMIT_HTTP_STATUS


def claude_working_directory(output: str) -> str | None:
    parsed_response = parsed_claude_output(output)
    if not isinstance(parsed_response, list):
        return None
    for message in cast("list[object]", parsed_response):
        if not isinstance(message, dict):
            continue
        message_fields = cast("dict[str, object]", message)
        working_directory = message_fields.get("cwd")
        if message_fields.get("subtype") == "init" and isinstance(working_directory, str):
            return working_directory
    return None


def record_worktree(task_id: str, worktree: str) -> None:
    with locked_ledger() as (tasks, worktrees):
        tasks[task_id]["worktree"] = worktree
        tasks[task_id].pop("worktree_pending", None)
        task = tasks[task_id]
        record_worktree_resource(worktrees, worktree, task["session_id"])


def clear_recorded_worktree_pending(task_id: str) -> None:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is not None:
            clear_worktree_pending_if_created(task)


def mark_task_waiting_for_reset(task_id: str, result: str) -> bool:
    try:
        task = load_tasks().get(task_id)
        if task is None:
            return False
        when = planned_execution_time(task_backend(task), is_now=False, at_text=None)
    except SystemExit:
        return False
    if when is None:
        return False
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None:
            return False
        task_record = task
        if task_record.get("status") != STATUS_RUNNING:
            return False
        task_record["result"] = result
        task_record["status"] = STATUS_SCHEDULED
        task_record["due_at"] = when.isoformat()
        task_record["usage_limit_resume_count"] = task_record.get("usage_limit_resume_count", 0) + 1
        return True


def finish_task(
    task_id: str,
    result: str | None,
    returncode: int,
    *,
    is_usage_limit: bool,
    is_background_work_stopped: bool,
) -> None:
    with locked_tasks() as tasks:
        stored_task = tasks.get(task_id)
        if stored_task is None:
            return
        task_record = stored_task
        if task_record.get("status") != STATUS_RUNNING:
            return
        if result is not None:
            task_record["result"] = result
        task_record["status"] = (
            STATUS_NEEDS_REVIEW
            if returncode == 0 and not is_usage_limit and not is_background_work_stopped
            else STATUS_FAILED
        )
        task_record["returncode"] = returncode
        stored_parent = tasks.get(task_record.get("after", ""))
        if task_record["status"] != STATUS_NEEDS_REVIEW or stored_parent is None:
            return
        parent = stored_parent
        if parent.get("status") == STATUS_NEEDS_REVIEW:
            parent["status"] = STATUS_DONE


def claim_task(
    tasks: Tasks,
    worktrees: Worktrees,
    task_id: str,
    session_id: str,
    expected_status: str,
) -> tuple[Task, io.TextIOWrapper, io.TextIOWrapper] | None:
    task = tasks.get(task_id)
    if task is None or task["status"] != expected_status or task["session_id"] != session_id:
        return None
    due_at = task.get("due_at")
    if (
        expected_status == STATUS_SCHEDULED
        and due_at is not None
        and datetime.fromisoformat(due_at) > datetime.now(UTC)
    ):
        return None
    run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return None
    recorded = recorded_worktree(tasks, task)
    worktree, is_worktree_pending = resolved_worktree(tasks, task)
    if missing_worktree := worktree_missing_message(
        worktree, worktrees, is_worktree_pending=is_worktree_pending
    ):
        record_resolved_worktree(task, worktree, is_worktree_pending=is_worktree_pending)
        task["status"] = STATUS_FAILED
        task["result"] = missing_worktree
        release_run_lock(run_lock_file, session_id)
        return None
    directory_lock_file = acquire_lock(directory_lock_path(directory_to_lock(task, worktree)))
    if directory_lock_file is None:
        release_run_lock(run_lock_file, session_id)
        return None
    record_resolved_worktree(task, worktree, is_worktree_pending=is_worktree_pending)
    if recorded is None and worktree is not None:
        repository = git_common_directory(task["cwd"])
        if repository is not None:
            if task.get("worktree_pending") is True:
                record_worktree_resource(
                    worktrees,
                    worktree,
                    session_id,
                    owner=WORKTREE_OWNER_GOLEM,
                    repository=repository,
                )
            else:
                record_worktree_resource(worktrees, worktree, session_id)
    clear_worktree_release(tasks, worktrees, task)
    progress_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    progress_path(session_id).touch()
    task["status"] = STATUS_RUNNING
    task["started_at"] = datetime.now(UTC).isoformat()
    return task.copy(), run_lock_file, directory_lock_file


def claim_next_dependent(
    task_id: str, parent_session_id: str
) -> tuple[str, Task, io.TextIOWrapper, io.TextIOWrapper] | None:
    with locked_ledger() as (tasks, worktrees):
        parent_record = tasks.get(task_id)
        if parent_record is None:
            return None
        parent = parent_record
        if parent.get("session_id") != parent_session_id or not can_start_dependents(parent):
            return None
        for dependent_id in waiting_dependent_ids(tasks, task_id):
            dependent_session_id = tasks[dependent_id]["session_id"]
            claim = claim_task(tasks, worktrees, dependent_id, dependent_session_id, STATUS_WAITING)
            if claim is not None:
                return dependent_id, *claim
            if tasks[dependent_id]["status"] == STATUS_WAITING:
                return None
    return None


def print_task_result(result: str) -> None:
    print(result, end="" if result.endswith("\n") else "\n")


def drain_dependents(task_id: str, session_id: str) -> None:
    while next_dependent := claim_next_dependent(task_id, session_id):
        execute_claimed_task(*next_dependent)
    auto_pickup_task(task_id)
    drained_task = load_tasks().get(task_id)
    if drained_task is not None:
        sync_hooks(task_directory(drained_task))


def execute_task(task_id: str, task: Task, lock_file_descriptors: tuple[int, ...]) -> int:
    stderr = ""
    try:
        if task_backend(task) == BACKEND_CODEX:
            returncode, result, is_usage_limit = run_codex(task_id, task, lock_file_descriptors)
            output = "" if result is None else result
        else:
            returncode, output, stderr = run_claude(task_id, task, lock_file_descriptors)
            result, is_usage_limit = claude_result_and_usage_limit(output)
    except OSError as error:
        print(f"golem: could not execute task: {error}", file=sys.stderr)
        returncode = 127
        output = ""
        result = None
        is_usage_limit = False
    if task_backend(task) == BACKEND_CLAUDE and not task.get("no_worktree"):
        working_directory = claude_working_directory(output)
        if working_directory is not None and working_directory != task["cwd"]:
            record_worktree(task_id, working_directory)
    clear_recorded_worktree_pending(task_id)
    usage_limit_resume_count = task.get("usage_limit_resume_count", 0)
    if (
        is_usage_limit
        and usage_limit_resume_count < MAX_USAGE_LIMIT_RESUMES
        and result is not None
        and mark_task_waiting_for_reset(task_id, result)
    ):
        return 0
    stored_result = result if output else None
    stop_line_match = BACKGROUND_WORK_STOPPED_PATTERN.search(stderr)
    if stop_line_match is not None:
        stop_line = stop_line_match.group()
        stored_result = f"{result}\n\n{stop_line}" if output else stop_line
    finish_task(
        task_id,
        stored_result,
        returncode,
        is_usage_limit=is_usage_limit,
        is_background_work_stopped=stop_line_match is not None,
    )
    return returncode


def gate_input_json(task_id: str, task: Task) -> str:
    transcript_path = next(iter(transcript_paths(task)), None)
    return json.dumps(
        {
            "task_id": task_id,
            "session_id": task["session_id"],
            "directory": task_directory(task),
            "started_at": task.get("started_at"),
            "exit_code": task.get("returncode"),
            "result": task.get("result"),
            "transcript_path": None if transcript_path is None else str(transcript_path),
        }
    )


def shell_command_outcome(
    command: str, label: str, task: Task, input_json: str, timeout_seconds: float
) -> tuple[str | None, str]:
    # Files, not pipes: a background process the command starts can hold them open after it exits.
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stdout_file,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=task_directory(task),
                env=task_environment(task),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                encoding="utf-8",
                errors="replace",
                process_group=0,
            )
        except OSError as error:
            return str(error), ""
        with process:
            try:
                process.communicate(input_json, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                return f"{label} timed out after {timeout_seconds}s", ""
            finally:
                # Unreaped, so the group ID is still ours; macOS fails with EPERM once only zombies remain.
                if process.returncode is None:
                    with suppress(OSError):
                        os.killpg(process.pid, signal.SIGKILL)
        stdout_file.seek(0)
        stdout = stdout_file.read()
        if process.returncode == 0:
            return None, stdout
        stderr_file.seek(0)
        reason = stderr_file.read().strip() or f"{label} exited with status {process.returncode}"
        return reason, stdout


def gate_stop_reason(task_id: str, task: Task, timeout_seconds: int | None = None) -> str | None:
    gate = task.get("gate")
    if gate is None:
        return None
    if timeout_seconds is None:
        timeout_seconds = GATE_TIMEOUT_SECONDS
    stop_reason, _ = shell_command_outcome(
        gate, "gate", task, gate_input_json(task_id, task), timeout_seconds
    )
    return stop_reason


def setup_input_json(task_id: str, task: Task) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "session_id": task["session_id"] if task_backend(task) == BACKEND_CLAUDE else None,
            "directory": task_directory(task),
            "prompt": task["prompt"],
        }
    )


def setup_directory_and_prompt(task_id: str, task: Task, setup: str) -> tuple[str, str] | str:
    failure_reason, stdout = shell_command_outcome(
        setup, "setup", task, setup_input_json(task_id, task), GATE_TIMEOUT_SECONDS
    )
    if failure_reason is not None:
        return failure_reason
    try:
        output = cast("object", json.loads(stdout))
    except json.JSONDecodeError as error:
        return f"setup output is not valid JSON: {error}"
    if not isinstance(output, dict):
        return SETUP_OUTPUT_ERROR
    setup_output = cast("JsonObject", output)
    directory = setup_output.get("directory")
    prompt_suffix = setup_output.get("prompt_suffix")
    if not isinstance(directory, str | None) or not isinstance(prompt_suffix, str | None):
        return SETUP_OUTPUT_ERROR
    if directory is not None:
        directory = resolved_path(str(Path(task["cwd"], directory)))
        if not Path(directory).is_dir():
            return f"setup directory does not exist: {directory}"
    prompt = task["prompt"] if prompt_suffix is None else f"{task['prompt']}\n\n{prompt_suffix}"
    return directory or task["cwd"], prompt


def fail_setup(task_id: str, reason: str) -> None:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None or task.get("status") != STATUS_RUNNING:
            return
        task["status"] = STATUS_FAILED
        task["setup_reason"] = reason


def record_setup(task_id: str, directory: str, prompt: str) -> None:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None:
            return
        task["cwd"] = directory
        task["prompt"] = prompt
        task["setup_complete"] = True
        for descendant_id in descendant_ids_with_status(tasks, task_id, set(LIST_STATUS_ORDER)):
            tasks[descendant_id]["cwd"] = directory


def prepared_task(
    task_id: str, task: Task, directory_lock_file: io.TextIOWrapper
) -> tuple[Task, io.TextIOWrapper] | None:
    setup = task.get("setup")
    if setup is None or task.get("setup_complete"):
        return task, directory_lock_file
    outcome = setup_directory_and_prompt(task_id, task, setup)
    if isinstance(outcome, str):
        fail_setup(task_id, outcome)
        return None
    directory, prompt = outcome
    if directory_lock_path(directory) != directory_lock_path(task["cwd"]):
        moved_lock_file = acquire_lock(directory_lock_path(directory))
        if moved_lock_file is None:
            fail_setup(task_id, f"directory is busy: {directory}")
            return None
        release_lock(directory_lock_file)
        directory_lock_file = moved_lock_file
    record_setup(task_id, directory, prompt)
    prepared = task.copy()
    prepared["cwd"] = directory
    prepared["prompt"] = prompt
    prepared["setup_complete"] = True
    return prepared, directory_lock_file


def is_gate_pending(tasks: Tasks, task_id: str) -> bool:
    task = tasks.get(task_id)
    if task is None:
        return False
    return (
        task.get("status") == STATUS_NEEDS_REVIEW
        and "gate" in task
        and "gate_reason" not in task
        and bool(waiting_dependent_ids(tasks, task_id))
    )


def acquire_task_directory_lock(task: Task) -> io.TextIOWrapper | None:
    return acquire_lock(directory_lock_path(directory_to_lock(task, task.get("worktree"))))


def run_pending_gate(task_id: str) -> None:
    tasks = load_tasks()
    if not is_gate_pending(tasks, task_id):
        return
    stop_reason = gate_stop_reason(task_id, tasks[task_id])
    if stop_reason is None:
        return
    with locked_tasks() as locked_tasks_by_id:
        current = locked_tasks_by_id.get(task_id)
        if current is None:
            return
        if current.get("status") not in SUCCEEDED_STATUSES:
            return
        current["gate_reason"] = stop_reason
        directory = task_directory(current)
    sync_hooks(directory)


def continue_task(task_id: str, *, is_forced: bool) -> int:
    task = existing_task(load_tasks(), task_id)
    if "gate_reason" not in task:
        raise SystemExit(f"golem: task is not stopped at a gate: {task_id}")
    if not is_forced:
        directory_lock_file = acquire_task_directory_lock(task)
        if directory_lock_file is None:
            raise SystemExit(f"golem: directory is busy: {task_id}")
        try:
            stop_reason = gate_stop_reason(task_id, task)
        finally:
            release_lock(directory_lock_file)
        if stop_reason is not None:
            with locked_tasks() as tasks:
                stopped_task = existing_task(tasks, task_id)
                if "gate_reason" in stopped_task:
                    stopped_task["gate_reason"] = stop_reason
            print(stop_reason, file=sys.stderr)
            return 1
    if not release_gate_stop(task_id):
        raise SystemExit(f"golem: task is not stopped at a gate: {task_id}")
    return 0


def release_gate_stop(task_id: str, *, stderr: int | None = None) -> bool:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None:
            return False
        if "gate_reason" not in task:
            return False
        del task["gate_reason"]
        task["status"] = STATUS_DONE
    start_task_executor(task_id, task, stderr=stderr)
    return True


def hook_command_line(argument: str) -> str:
    state_home = shlex.quote(str(state_directory().parent))
    command = shlex.join([*golem_command(), "_hook", argument])
    return f"XDG_STATE_HOME={state_home} {command}{HOOK_FAILURE_SUFFIX}"


def hook_group(event: str) -> JsonObject:
    entry: JsonObject = {
        "type": "command",
        "command": hook_command_line(HOOK_EVENT_ARGUMENTS[event]),
    }
    if event == HOOK_EVENT_STOP:
        entry["timeout"] = HOOK_STOP_TIMEOUT_SECONDS
        return {"hooks": [entry]}
    return {"matcher": HOOK_TOOL_MATCHER, "hooks": [entry]}


def group_entries(group: object) -> list[object]:
    entries = cast("JsonObject", group).get("hooks") if isinstance(group, dict) else None
    return cast("list[object]", entries) if isinstance(entries, list) else []


def is_golem_hook_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command = cast("JsonObject", entry).get("command")
    return isinstance(command, str) and command.endswith(HOOK_COMMAND_SUFFIXES)


def local_settings_path(directory: str) -> Path:
    return Path(directory) / ".claude" / "settings.local.json"


def save_local_settings(path: Path, settings: LocalSettings) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    save_json(path, settings)
    if mode is not None:
        path.chmod(mode)


def read_local_settings(path: Path) -> LocalSettings | None:
    if path.is_symlink():
        return None
    try:
        loaded_settings = cast("object", load_json(path))
    except ValueError, OSError:
        return None
    if not isinstance(loaded_settings, dict):
        return None
    settings = cast("JsonObject", loaded_settings)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return None
    hooks_by_event = cast("JsonObject", hooks)
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
            kept_groups.append({**cast("JsonObject", group), "hooks": kept_entries})
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


def has_pending_follow_up(tasks: Tasks, task_id: str) -> bool:
    return bool(
        waiting_dependent_ids(tasks, task_id)
        or descendant_ids_with_status(tasks, task_id, {STATUS_SCHEDULED, STATUS_RUNNING})
    )


def is_hook_needed(tasks: Tasks, directory: str) -> bool:
    return any(
        "gate" in task
        and task["status"] in SUCCEEDED_STATUSES
        and task_directory(task) == directory
        and has_pending_follow_up(tasks, task_id)
        for task_id, task in tasks.items()
    )


def hook_affected_directories(tasks: Tasks, task: Task) -> set[str]:
    directories = {task_directory(task)}
    parent = tasks.get(task.get("after", ""))
    if parent is not None:
        directories.add(task_directory(parent))
    return directories


def sync_hooks(directory: str) -> None:
    with locked_tasks() as tasks:
        if is_hook_needed(tasks, directory):
            install_hooks(directory)
        else:
            remove_hooks(directory)


def read_hook_session_id() -> str | None:
    try:
        payload = cast("object", json.load(sys.stdin))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    session_id = cast("JsonObject", payload).get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def session_task_id(tasks: Tasks, session_id: str | None) -> str | None:
    if session_id is None:
        return None
    return next(
        (task_id for task_id, task in tasks.items() if task.get("session_id") == session_id),
        None,
    )


def hook_stop() -> int:
    tasks = load_tasks()
    task_id = session_task_id(tasks, read_hook_session_id())
    if task_id is None:
        return 0
    task = tasks[task_id]
    next_ids = waiting_dependent_ids(tasks, task_id)
    if "gate_reason" not in task or not next_ids:
        return 0
    directory_lock_file = acquire_task_directory_lock(task)
    if directory_lock_file is None:
        return 0
    try:
        stop_reason = gate_stop_reason(task_id, task, HOOK_GATE_TIMEOUT_SECONDS)
    finally:
        release_lock(directory_lock_file)
    if stop_reason is not None:
        return 0
    with agent_log_path().open("a", encoding="utf-8") as log_file:
        if not release_gate_stop(task_id, stderr=log_file.fileno()):
            return 0
    print(json.dumps({"systemMessage": f"golem: continuing chain with {next_ids[0]}"}))
    return 0


def hook_pre_tool_use() -> int:
    tasks = load_tasks()
    task_id = session_task_id(tasks, read_hook_session_id())
    if task_id is None:
        return 0
    running_ids = descendant_ids_with_status(tasks, task_id, {STATUS_RUNNING})
    if not running_ids:
        return 0
    reason = f"golem is running {running_ids[0]}; wait for it to finish"
    output = {
        "hookSpecificOutput": {
            "hookEventName": HOOK_EVENT_PRE_TOOL_USE,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))
    return 0


def execute_claimed_task(
    task_id: str,
    task: Task,
    run_lock_file: io.TextIOWrapper,
    directory_lock_file: io.TextIOWrapper,
) -> int:
    try:
        try:
            prepared = prepared_task(task_id, task, directory_lock_file)
            if prepared is None:
                returncode = 1
            else:
                task, directory_lock_file = prepared
                returncode = execute_task(
                    task_id,
                    task,
                    (run_lock_file.fileno(), directory_lock_file.fileno()),
                )
                run_pending_gate(task_id)
        finally:
            release_lock(directory_lock_file)
        drain_dependents(task_id, task["session_id"])
        return returncode
    finally:
        release_run_lock(run_lock_file, task["session_id"])


def execute_scheduled_task(task_id: str, session_id: str) -> int:
    with locked_ledger() as (tasks, worktrees):
        claim = claim_task(tasks, worktrees, task_id, session_id, STATUS_SCHEDULED)
    if claim is None:
        return 0
    return execute_claimed_task(task_id, *claim)


def run_pending_gate_under_directory_lock(task_id: str) -> bool:
    tasks = load_tasks()
    if not is_gate_pending(tasks, task_id):
        return True
    directory_lock_file = acquire_task_directory_lock(tasks[task_id])
    if directory_lock_file is None:
        return False
    try:
        run_pending_gate(task_id)
    finally:
        release_lock(directory_lock_file)
    return True


def drain_succeeded_task(task_id: str, session_id: str) -> None:
    with locked_tasks():
        run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return
    try:
        if not run_pending_gate_under_directory_lock(task_id):
            return
        drain_dependents(task_id, session_id)
    finally:
        release_run_lock(run_lock_file, session_id)


def execute_or_drain_task(task_id: str, session_id: str) -> int:
    task = load_tasks().get(task_id)
    if task is None or task["session_id"] != session_id or task["status"] not in SUCCEEDED_STATUSES:
        return execute_scheduled_task(task_id, session_id)
    drain_succeeded_task(task_id, session_id)
    return 0


def start_task_executor(
    task_id: str, task: Task, *, stderr: int | None = None
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*golem_command(), "_execute", task_id, task["session_id"]],
        env=task_environment(task),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )


def is_run_lock_free(session_id: str) -> bool:
    run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return False
    release_run_lock(run_lock_file, session_id)
    return True


def task_uses_worktree(tasks: Tasks, task: Task, worktree: str) -> bool:
    recorded = recorded_worktree(tasks, task)
    directories = [task["cwd"]]
    if recorded is not None:
        directories.append(recorded[0])
    worktree_path = Path(worktree).resolve()
    return any(Path(directory).resolve().is_relative_to(worktree_path) for directory in directories)


def worktree_task_ids(tasks: Tasks, worktree: str) -> list[str]:
    return [task_id for task_id, task in tasks.items() if task_uses_worktree(tasks, task, worktree)]


def git_worktree_entries(repository: str) -> list[list[str]] | None:
    result = run_git(repository, "worktree", "list", "--porcelain")
    if result.returncode:
        return None
    return [entry.splitlines() for entry in result.stdout.strip().split("\n\n") if entry]


def git_worktree_entry(entries: list[list[str]], worktree: str) -> list[str] | None:
    return next(
        (entry for entry in entries if entry[0] == f"worktree {resolved_path(worktree)}"), None
    )


def git_worktree_lock_reason(entry: list[str]) -> str | None:
    for line in entry[1:]:
        if line == "locked":
            return ""
        if line.startswith("locked "):
            return line.removeprefix("locked ")
    return None


def is_verified_stale_worktree_lock(
    resource: WorktreeResource, tasks: Tasks, worktree: str, reason: str
) -> bool:
    match = CLAUDE_LOCK_PATTERN.fullmatch(reason)
    if match is None:
        return False
    session_id, process_id_text = match.groups()
    session_ids = {resource["session_id"]}
    session_ids.update(
        tasks[task_id]["session_id"] for task_id in worktree_task_ids(tasks, worktree)
    )
    if session_id not in session_ids:
        return False
    try:
        os.kill(int(process_id_text), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def git_worktree_retention_reasons(
    resource: WorktreeResource,
    tasks: Tasks,
    worktree: str,
    entries: list[list[str]] | None,
) -> list[str]:
    if not Path(worktree).is_dir():
        return ["missing"]
    repository = resource.get("repository")
    if not isinstance(repository, str):
        return ["repository unknown"]
    if entries is None:
        return ["repository unavailable"]
    matching_entry = git_worktree_entry(entries, worktree)
    if matching_entry is None:
        return ["not a registered worktree"]
    if entries[0] == matching_entry:
        return ["original checkout"]
    if not is_worktree_owner_verified(worktree, resource["session_id"]):
        return ["ownership cannot be verified"]
    reasons: list[str] = []
    lock_reason = git_worktree_lock_reason(matching_entry)
    if lock_reason is not None and not is_verified_stale_worktree_lock(
        resource, tasks, worktree, lock_reason
    ):
        reasons.append("worktree lock is active")
    status_result = run_git(worktree, "status", "--porcelain", "--untracked-files=all")
    if status_result.returncode or status_result.stdout:
        reasons.append("local changes")
    index_result = run_git(worktree, "ls-files", "-v", "-z")
    if index_result.returncode or any(
        entry[:1].islower() or entry.startswith("S ") for entry in index_result.stdout.split("\0")
    ):
        reasons.append("index flags may hide local changes")
    branch_result = run_git(worktree, "symbolic-ref", "-q", "HEAD")
    if branch_result.returncode:
        ref_result = run_git(
            repository, "for-each-ref", "--contains", matching_entry[1].removeprefix("HEAD ")
        )
        if ref_result.returncode or not ref_result.stdout.strip():
            reasons.append("unpublished commits")
    return reasons


def task_worktree_retention_reasons(tasks: Tasks, worktree: str, *, is_released: bool) -> list[str]:
    reasons: list[str] = []
    worktree_tasks = [tasks[task_id] for task_id in worktree_task_ids(tasks, worktree)]
    if any(task.get("status") == STATUS_RUNNING for task in worktree_tasks):
        reasons.append("in use")
    for task in worktree_tasks:
        status = task.get("status")
        if status in {STATUS_SCHEDULED, STATUS_WAITING}:
            reasons.append(f"task {status}")
        elif status == STATUS_NEEDS_REVIEW:
            reasons.append("task needs review")
        elif status in {STATUS_FAILED, STATUS_CANCELLED} and not is_released:
            reasons.append("task can be retried (delete the task to override)")
    return reasons


def worktree_retention_reasons(
    tasks: Tasks,
    resource: WorktreeResource | None,
    worktree: str,
    entries: list[list[str]] | None,
    *,
    is_released: bool = False,
) -> list[str]:
    if resource is None:
        return ["ownership unknown"]
    if resource.get("owner") == WORKTREE_OWNER_EXTERNAL:
        return ["not created by Golem"]
    if resource.get("removed_at") is not None:
        return ["already removed"]
    if resource.get("owner") != WORKTREE_OWNER_GOLEM:
        return ["ownership unknown"]
    reasons: list[str] = []
    directories = {
        resolved_path(directory_to_lock(tasks[task_id], tasks[task_id].get("worktree")))
        for task_id in worktree_task_ids(tasks, worktree)
    } - {resolved_path(worktree)}
    for directory in directories:
        directory_lock_file = acquire_lock(directory_lock_path(directory))
        if directory_lock_file is None:
            reasons.append("in use")
            break
        release_lock(directory_lock_file)
    reasons.extend(
        task_worktree_retention_reasons(
            tasks,
            worktree,
            is_released=resource.get("released_at") is not None or is_released,
        )
    )
    if entries is not None and any(
        Path(worktree).resolve() in Path(entry[0].removeprefix("worktree ")).resolve().parents
        for entry in entries
    ):
        reasons.append("contains another worktree")
    reasons.extend(git_worktree_retention_reasons(resource, tasks, worktree, entries))
    return list(dict.fromkeys(reasons))


def record_worktree_removal_error(resource: WorktreeResource, error: str) -> tuple[bool, list[str]]:
    resource["removal_error"] = error
    return False, [error]


def remove_inspected_worktree_resource(
    resource: WorktreeResource, entry: list[str], worktree: str
) -> tuple[bool, list[str]]:
    repository = resource.get("repository")
    if repository is None:
        return False, []
    commands = ["remove"]
    if git_worktree_lock_reason(entry) is not None:
        commands.insert(0, "unlock")
    for command in commands:
        result = run_git(
            repository,
            "-c",
            "status.showUntrackedFiles=all",
            "worktree",
            command,
            worktree,
        )
        if result.returncode:
            error = result.stderr.strip() or result.stdout.strip() or "git worktree command failed"
            return record_worktree_removal_error(resource, error)
    resource["removed_at"] = datetime.now(UTC).isoformat()
    resource.pop("removal_error", None)
    return True, []


@contextmanager
def locked_worktree_inspection(
    tasks: Tasks, worktrees: Worktrees, worktree: str, *, is_released: bool = False
) -> Generator[tuple[WorktreeResource | None, list[str] | None, list[str]]]:
    resource = worktree_resource(worktrees, worktree)
    if resource is None:
        yield None, None, ["ownership unknown"]
        return
    directory_lock_file = acquire_lock(directory_lock_path(worktree))
    if directory_lock_file is None:
        yield resource, None, ["in use"]
        return
    try:
        repository = resource.get("repository")
        entries = git_worktree_entries(repository) if isinstance(repository, str) else None
        reasons = worktree_retention_reasons(
            tasks, resource, worktree, entries, is_released=is_released
        )
        entry = git_worktree_entry(entries, worktree) if entries else None
        yield resource, entry, reasons
    finally:
        release_lock(directory_lock_file)


def remove_worktree_resource(
    tasks: Tasks, worktrees: Worktrees, worktree: str, *, is_released: bool = False
) -> tuple[bool, list[str]]:
    with locked_worktree_inspection(tasks, worktrees, worktree, is_released=is_released) as (
        resource,
        entry,
        reasons,
    ):
        if reasons or resource is None or entry is None:
            return False, reasons
        repository = resource.get("repository")
        if repository is None:
            return False, reasons
        return remove_inspected_worktree_resource(resource, entry, worktree)


def is_known_directory_busy(tasks: Tasks, task: Task) -> bool:
    recorded = recorded_worktree(tasks, task)
    if recorded is not None:
        directory = recorded[0]
    elif is_worktree_left_to_agent(task):
        directory = task["cwd"]
    else:
        return False
    return is_directory_busy(directory)


def is_directory_busy(directory: str) -> bool:
    directory_lock_file = acquire_lock(directory_lock_path(directory))
    if directory_lock_file is None:
        return True
    release_lock(directory_lock_file)
    return False


def run_launch_agent_pass() -> list[subprocess.Popen[bytes]]:
    rotate_agent_log()
    now = datetime.now(UTC)
    due_tasks: list[tuple[str, Task]] = []
    hook_directories: set[str] = set()
    with locked_tasks() as tasks:
        remove_orphan_run_locks()
        remove_interrupted_ledger_writes()
        for task_id, task in tasks.items():
            if task["status"] == STATUS_RUNNING:
                if is_run_lock_free(task["session_id"]):
                    task["status"] = STATUS_FAILED
                    hook_directories |= hook_affected_directories(tasks, task)
            elif (
                task["status"] == STATUS_SCHEDULED
                and (due_at := task.get("due_at")) is not None
                and datetime.fromisoformat(due_at) <= now
                and not is_known_directory_busy(tasks, task)
            ) or (
                can_start_dependents(task)
                and (dependent_ids := waiting_dependent_ids(tasks, task_id))
                and not has_running_dependent(tasks, task_id)
                and not is_known_directory_busy(tasks, tasks[dependent_ids[0]])
                and is_run_lock_free(task["session_id"])
            ):
                due_tasks.append((task_id, task.copy()))
    for directory in hook_directories:
        sync_hooks(directory)
    processes: list[subprocess.Popen[bytes]] = []
    for task_id, task in due_tasks:
        processes.append(start_task_executor(task_id, task))
    return processes


def has_pending_tasks(tasks: Tasks) -> bool:
    return any(
        task["status"] in {STATUS_SCHEDULED, STATUS_RUNNING}
        or (
            task["status"] == STATUS_WAITING
            and (parent := tasks.get(task.get("after", ""))) is not None
            and can_start_dependents(parent)
        )
        for task in tasks.values()
    )


def list_worktrees() -> int:
    tasks = load_tasks()
    worktrees = load_worktrees()
    worktree_paths = set(worktrees)
    for task in tasks.values():
        recorded = recorded_worktree(tasks, task)
        if recorded is not None:
            worktree_paths.add(resolved_path(recorded[0]))
    if not worktree_paths:
        print("golem: no worktrees")
        return 0
    rows: list[tuple[str, str, str, str]] = []
    for worktree in sorted(worktree_paths):
        with locked_worktree_inspection(tasks, worktrees, worktree) as (resource, _, reasons):
            state = "eligible" if not reasons else "retained"
        if resource is not None and resource.get("removed_at") is not None:
            state = "removed"
            reasons = []
        removal_error = resource.get("removal_error") if resource is not None else None
        if isinstance(removal_error, str):
            reasons.append(f"last removal error: {removal_error}")
        rows.append(
            (
                worktree,
                ",".join(worktree_task_ids(tasks, worktree)),
                state,
                "; ".join(reasons),
            )
        )
    headers = ("PATH", "TASKS", "STATE", "REASON")
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    for row in (headers, *rows):
        print(
            "  ".join(
                str(value).ljust(width) for value, width in zip(row, widths, strict=True)
            ).rstrip()
        )
    return 0


def delete_task_and_release_worktrees(
    tasks: Tasks, worktrees: Worktrees, task_ids: list[str]
) -> None:
    selected_tasks = [tasks[task_id] for task_id in task_ids]
    worktree_paths = [
        path
        for path in worktrees
        if any(task_uses_worktree(tasks, task, path) for task in selected_tasks)
    ]
    for task_id in task_ids:
        remove_progress(tasks.pop(task_id)["session_id"])
    for path in worktree_paths:
        resource = worktrees[path]
        if (
            resource.get("owner") == WORKTREE_OWNER_GOLEM
            and resource.get("removed_at") is None
            and not worktree_task_ids(tasks, path)
        ):
            resource.setdefault("released_at", datetime.now(UTC).isoformat())


def chain_task_ids(tasks: Tasks, task_id: str) -> set[str]:
    chain_ids = {task_id}
    pending_ids = [task_id]
    while pending_ids:
        current_id = pending_ids.pop()
        current_task = tasks[current_id]
        adjacent_ids = dependent_task_ids(tasks, current_id)
        parent_id = current_task.get("after")
        if parent_id is not None and parent_id in tasks:
            adjacent_ids.append(parent_id)
        for adjacent_id in adjacent_ids:
            if adjacent_id not in chain_ids:
                chain_ids.add(adjacent_id)
                pending_ids.append(adjacent_id)
    return chain_ids


def selected_delete_task_ids(
    tasks: Tasks, task_ids: list[str], *, is_chain: bool
) -> tuple[list[str], list[str]]:
    selected_ids: set[str] = set()
    messages: list[str] = []
    for task_id in task_ids:
        if task_id not in tasks:
            messages.append(f"golem: task not found: {task_id}")
        elif is_chain:
            selected_ids.update(chain_task_ids(tasks, task_id))
        else:
            selected_ids.add(task_id)
    return [task_id for task_id in tasks if task_id in selected_ids], messages


def task_worktrees_and_refusals(
    tasks: Tasks, task_ids: list[str], *, is_removing_worktree: bool
) -> tuple[dict[str, str | None], list[str]]:
    selected_id_set = set(task_ids)
    worktrees_by_task: dict[str, str | None] = {}
    messages: list[str] = []
    for task_id in task_ids:
        task = tasks[task_id]
        recorded = recorded_worktree(tasks, task)
        worktree = resolved_path(recorded[0]) if recorded is not None else None
        worktrees_by_task[task_id] = worktree
        if task.get("status") == STATUS_RUNNING:
            messages.append(f"golem: task is running: {task_id}")
        dependent_ids = [
            dependent_id
            for dependent_id in dependent_task_ids(tasks, task_id)
            if dependent_id not in selected_id_set
        ]
        if dependent_ids:
            messages.append(f"golem: task has dependents: {task_id}: {', '.join(dependent_ids)}")
        if is_removing_worktree and worktree is None:
            messages.append(f"golem: task has no worktree: {task_id}")
    return worktrees_by_task, messages


def print_retained_worktree(worktree: str, reasons: list[str]) -> None:
    print(f"golem: retained worktree: {worktree}: {'; '.join(reasons)}", file=sys.stderr)


def remove_selected_worktrees(
    tasks: Tasks,
    worktrees: Worktrees,
    worktrees_by_task: dict[str, str | None],
) -> set[str] | None:
    selected_worktrees = list(
        dict.fromkeys(
            worktree
            for worktree in worktrees_by_task.values()
            if worktree is not None
            and (
                (resource := worktree_resource(worktrees, worktree)) is None
                or resource.get("removed_at") is None
            )
        )
    )
    selected_id_set = set(worktrees_by_task)
    remaining_tasks = {
        task_id: task for task_id, task in tasks.items() if task_id not in selected_id_set
    }
    with ExitStack() as stack:
        inspections = [
            (
                worktree,
                *stack.enter_context(
                    locked_worktree_inspection(remaining_tasks, worktrees, worktree)
                ),
            )
            for worktree in selected_worktrees
        ]
        refusals = [(worktree, reasons) for worktree, _, _, reasons in inspections if reasons]
        if refusals:
            for worktree, reasons in refusals:
                print_retained_worktree(worktree, reasons)
            return None
        removed_worktrees: set[str] = set()
        for worktree, resource, entry, _ in inspections:
            if resource is None or entry is None:
                continue
            is_removed, reasons = remove_inspected_worktree_resource(resource, entry, worktree)
            if not is_removed:
                for removed_worktree in removed_worktrees:
                    print(f"golem: removed worktree: {removed_worktree}")
                print_retained_worktree(worktree, reasons)
                return None
            removed_worktrees.add(worktree)
    return removed_worktrees


def delete_tasks(task_ids: list[str], *, is_chain: bool, is_removing_worktree: bool) -> int:
    directories: set[str] = set()
    with locked_ledger() as (tasks, worktrees):
        selected_task_ids, messages = selected_delete_task_ids(tasks, task_ids, is_chain=is_chain)
        worktrees_by_task, check_messages = task_worktrees_and_refusals(
            tasks, selected_task_ids, is_removing_worktree=is_removing_worktree
        )
        if messages or check_messages:
            raise SystemExit("\n".join([*messages, *check_messages]))
        for task_id in selected_task_ids:
            directories |= hook_affected_directories(tasks, tasks[task_id])
        removed_worktrees: set[str] | None = set()
        if is_removing_worktree:
            # golem's own settings file would otherwise count as local changes.
            for task_id in selected_task_ids:
                remove_hooks(task_directory(tasks[task_id]))
            removed_worktrees = remove_selected_worktrees(tasks, worktrees, worktrees_by_task)
        if removed_worktrees is not None:
            selected_tasks = [(task_id, tasks[task_id]) for task_id in selected_task_ids]
            delete_task_and_release_worktrees(tasks, worktrees, selected_task_ids)
            for task_id, task in selected_tasks:
                worktree = worktrees_by_task[task_id]
                print_deleted_task(
                    task_id, task, worktree, is_removed=worktree in removed_worktrees
                )
    for directory in directories:
        sync_hooks(directory)
    return 0 if removed_worktrees is not None else 1


def print_deleted_task(task_id: str, task: Task, worktree: str | None, *, is_removed: bool) -> None:
    print(f"golem: deleted task: {task_id}")
    session = task.get("thread_id") if task_backend(task) == BACKEND_CODEX else task["session_id"]
    if isinstance(session, str):
        print(f"golem: session: {session}")
    if worktree is not None:
        print(f"golem: worktree: {worktree}")
    if is_removed:
        print(f"golem: removed worktree: {worktree}")


def dependents_first_task_ids(tasks: Tasks, task_ids: list[str]) -> list[str]:
    ordered_task_ids: list[str] = []
    for task_id in task_ids:
        ordered_task_ids += dependents_first_task_ids(tasks, dependent_task_ids(tasks, task_id))
        ordered_task_ids.append(task_id)
    return ordered_task_ids


def branch_pull_request(worktree: str | None) -> PullRequest | None:
    if worktree is None:
        return None
    branch = run_git(worktree, "symbolic-ref", "--short", "HEAD")
    if branch.returncode:
        return None
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch.stdout.strip(),
                "--state",
                "all",
                "--json",
                "number,state",
            ],
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=PULL_REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    pull_requests = cast("list[PullRequest]", json.loads(result.stdout))
    open_pull_request = next(
        (
            pull_request
            for pull_request in pull_requests
            if pull_request["state"] == PULL_REQUEST_STATE_OPEN
        ),
        None,
    )
    if open_pull_request is not None:
        return open_pull_request
    return max(pull_requests, key=lambda pull_request: pull_request["number"], default=None)


def transcript_paths(task: Task) -> list[Path]:
    if task_backend(task) == BACKEND_CLAUDE:
        return list((Path.home() / ".claude" / "projects").glob(f"*/{task['session_id']}.jsonl"))
    thread_id = task.get("thread_id")
    if not isinstance(thread_id, str):
        return []
    codex_home = Path(task.get("environment", {}).get("CODEX_HOME", Path.home() / ".codex"))
    return list((codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"))


def session_idle_reason(task: Task) -> str | None:
    transcript_modified_time = max(
        (transcript_path.stat().st_mtime for transcript_path in transcript_paths(task)),
        default=None,
    )
    if transcript_modified_time is None:
        return "no transcript"
    idle_days = timedelta(seconds=time.time() - transcript_modified_time).days
    if idle_days < PRUNE_IDLE_DAYS:
        return None
    return f"idle {idle_days}d"


def done_task_prune_reason(task: Task, pull_requests: PullRequestCache) -> str | None:
    worktree = task.get("worktree")
    if is_directory_busy(directory_to_lock(task, worktree)):
        return None
    if worktree not in pull_requests:
        pull_requests[worktree] = branch_pull_request(worktree)
    pull_request = pull_requests[worktree]
    if pull_request is None:
        return session_idle_reason(task)
    if pull_request["state"] == PULL_REQUEST_STATE_OPEN:
        return None
    return f"PR #{pull_request['number']} {pull_request['state'].lower()}"


def prune_question(candidates: list[tuple[str, str]]) -> str:
    reasons = {reason for _, reason in candidates}
    if len(reasons) == 1:
        task_ids = ", ".join(task_id for task_id, _ in candidates)
        removals = f"{task_ids} ({STATUS_DONE}, {reasons.pop()})"
    else:
        removals = ", ".join(
            f"{task_id} ({STATUS_DONE}, {reason})" for task_id, reason in candidates
        )
    return f"Remove {removals}? [{','.join(PRUNE_ANSWERS)}] "


def prune_answer(candidates: list[tuple[str, str]]) -> str:
    prompt = prune_question(candidates)
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            print()
            return PRUNE_ANSWER_QUIT
        if answer in PRUNE_ANSWERS:
            return answer


def chain_prune_selection(
    tasks: Tasks,
    chain_ids: list[str],
    pull_requests: PullRequestCache,
    *,
    is_offering_done_tasks: bool,
) -> tuple[Tasks, dict[str, str]]:
    selected_tasks: Tasks = {}
    done_reasons: dict[str, str] = {}
    for task_id in chain_ids:
        task = tasks[task_id]
        if any(
            dependent_id not in selected_tasks
            for dependent_id in dependent_task_ids(tasks, task_id)
        ):
            continue
        if task["status"] == STATUS_CANCELLED:
            selected_tasks[task_id] = task
            continue
        if task["status"] != STATUS_DONE or not is_offering_done_tasks:
            continue
        reason = done_task_prune_reason(task, pull_requests)
        if reason is None:
            continue
        done_reasons[task_id] = reason
        selected_tasks[task_id] = task
    return selected_tasks, done_reasons


def prune_selection(
    tasks: Tasks,
    pull_requests: PullRequestCache,
    *,
    is_confirmed: bool,
    is_keeping_done_tasks: bool,
) -> tuple[Tasks, bool]:
    selected_tasks: Tasks = {}
    has_candidates = False
    answer = PRUNE_ANSWER_ALL if is_confirmed else None
    root_task_ids = sorted(
        task_id for task_id, task in tasks.items() if task.get("after") not in tasks
    )
    for root_task_id in root_task_ids:
        chain_ids = dependents_first_task_ids(tasks, [root_task_id])
        is_offering_done_tasks = not is_keeping_done_tasks and answer != PRUNE_ANSWER_QUIT
        chain_selected_tasks, done_reasons = chain_prune_selection(
            tasks, chain_ids, pull_requests, is_offering_done_tasks=is_offering_done_tasks
        )
        has_candidates = has_candidates or bool(chain_selected_tasks)
        if done_reasons and answer != PRUNE_ANSWER_ALL:
            answer = prune_answer(
                [(task_id, done_reasons[task_id]) for task_id in tasks if task_id in done_reasons]
            )
            if answer not in {PRUNE_ANSWER_YES, PRUNE_ANSWER_ALL}:
                chain_selected_tasks, _ = chain_prune_selection(
                    tasks, chain_ids, pull_requests, is_offering_done_tasks=False
                )
        selected_tasks |= chain_selected_tasks
    return selected_tasks, has_candidates


def delete_pruned_tasks(
    selected_tasks: Tasks,
    pull_requests: PullRequestCache,
) -> None:
    with locked_ledger() as (tasks, worktrees):
        for task_id, evaluated_task in selected_tasks.items():
            if (
                tasks.get(task_id) != evaluated_task
                or dependent_task_ids(tasks, task_id)
                or (
                    evaluated_task["status"] == STATUS_DONE
                    and done_task_prune_reason(evaluated_task, pull_requests) is None
                )
            ):
                print(f"golem: kept {task_id}: no longer eligible", file=sys.stderr)
                continue
            delete_task_and_release_worktrees(tasks, worktrees, [task_id])
            print(f"golem: deleted task: {task_id}")


def prune_tasks(*, is_confirmed: bool) -> int:
    tasks = load_tasks()
    is_keeping_done_tasks = not is_confirmed and not sys.stdin.isatty()
    pull_requests: PullRequestCache = {}
    try:
        selected_tasks, has_candidates = prune_selection(
            tasks,
            pull_requests,
            is_confirmed=is_confirmed,
            is_keeping_done_tasks=is_keeping_done_tasks,
        )
    except KeyboardInterrupt:
        print()
        return 130
    if is_keeping_done_tasks and any(task["status"] == STATUS_DONE for task in tasks.values()):
        print("golem: kept done tasks without a terminal; use --yes to prune them", file=sys.stderr)
    elif not has_candidates:
        print("golem: no tasks to prune")
        return 0
    delete_pruned_tasks(selected_tasks, pull_requests)
    return 0


def set_worktree_auto_remove(retention_days: int | None) -> int:
    settings = load_settings()
    if retention_days is None:
        settings.pop("worktree_retention_days", None)
    else:
        settings["worktree_retention_days"] = retention_days
    save_settings(settings)
    return 0


def set_worktree_auto_remove_from_arguments(arguments: Arguments) -> int:
    if arguments.off:
        return set_worktree_auto_remove(None)
    retention_days = arguments.after
    if not isinstance(retention_days, int):
        raise SystemExit("golem: --after is required")
    if retention_days < 0:
        raise SystemExit("golem: --after must be non-negative")
    return set_worktree_auto_remove(retention_days)


def auto_remove_worktrees() -> None:
    settings = load_settings()
    retention_days = settings.get("worktree_retention_days")
    if retention_days is None:
        return
    now = datetime.now(UTC)
    with locked_ledger() as (tasks, worktrees):
        for worktree, resource in worktrees.items():
            if (
                resource.get("owner") != WORKTREE_OWNER_GOLEM
                or resource.get("removed_at") is not None
            ):
                continue
            if "released_at" not in resource:
                continue
            released_at = resource["released_at"]
            eligible_at = datetime.fromisoformat(released_at) + timedelta(days=retention_days)
            if now < eligible_at:
                continue
            try:
                is_removed, reasons = remove_worktree_resource(tasks, worktrees, worktree)
            except OSError as error:
                is_removed, reasons = record_worktree_removal_error(resource, str(error))
            if is_removed:
                print(
                    f"golem: removed worktree: {worktree}",
                    file=sys.stderr,
                )
            elif resource.get("removal_error") in reasons:
                print_retained_worktree(worktree, reasons)


def reap_finished_executors(
    processes: list[subprocess.Popen[bytes]],
) -> list[subprocess.Popen[bytes]]:
    return [process for process in processes if process.poll() is None]


def run_launch_agent() -> int:
    auto_remove_worktrees()
    processes = run_launch_agent_pass()
    while has_pending_tasks(load_tasks()):
        time.sleep(LAUNCH_AGENT_POLL_SECONDS)
        processes = reap_finished_executors(processes)
        processes.extend(run_launch_agent_pass())
    return 0


def existing_task(tasks: Tasks, task_id: str) -> Task:
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    return tasks[task_id]


def retryable_task(tasks: Tasks, task_id: str) -> Task:
    task = existing_task(tasks, task_id)
    if task.get("status") not in {STATUS_FAILED, STATUS_CANCELLED}:
        raise SystemExit(f"golem: task cannot be retried: {task_id}")
    return task


def retry_task(task_id: str, *, is_now: bool, at_text: str | None) -> int:
    tasks = load_tasks()
    retryable = retryable_task(tasks, task_id)
    reject_missing_worktree(retryable, load_worktrees())
    require_launch_agent()
    when = (
        planned_execution_time(task_backend(retryable), is_now=is_now, at_text=at_text)
        or datetime.now(UTC).astimezone()
    )

    with locked_ledger() as (tasks, worktrees):
        task = retryable_task(tasks, task_id)
        reject_missing_worktree(task, worktrees)
        record = new_task_record(
            RunPlacement(task["cwd"]),
            task["prompt"],
            tuple(task["agent_args"]),
            when,
            task_backend(task),
            after=task.get("after"),
        )
        if "environment" in task:
            record["environment"] = task["environment"]
        if "worktree" in task:
            record["worktree"] = task["worktree"]
        if "worktree_pending" in task:
            record["worktree_pending"] = task["worktree_pending"]
        if "auto_pickup" in task:
            record["auto_pickup"] = task["auto_pickup"]
        if "no_worktree" in task:
            record["no_worktree"] = task["no_worktree"]
        if "gate" in task:
            record["gate"] = task["gate"]
        if "setup" in task:
            record["setup"] = task["setup"]
        if "setup_complete" in task:
            record["setup_complete"] = task["setup_complete"]
        tasks[task_id] = record
        remove_progress(task["session_id"])
        clear_worktree_release(tasks, worktrees, record)
        directories = hook_affected_directories(tasks, record)
    for directory in directories:
        sync_hooks(directory)
    return wake_launch_agent_and_report(task_id, when)


def scheduled_task(tasks: Tasks, task_id: str) -> Task:
    task = existing_task(tasks, task_id)
    if task.get("status") != STATUS_SCHEDULED:
        raise SystemExit(f"golem: task is not scheduled: {task_id}")
    return task


def reschedule_task(task_id: str, *, is_now: bool, at_text: str | None) -> int:
    task = scheduled_task(load_tasks(), task_id)
    require_launch_agent()
    when = (
        planned_execution_time(task_backend(task), is_now=is_now, at_text=at_text)
        or datetime.now(UTC).astimezone()
    )

    with locked_tasks() as tasks:
        task = scheduled_task(tasks, task_id)
        task["due_at"] = when.isoformat()
    return wake_launch_agent_and_report(task_id, when)


def planned_execution_time(backend: str, *, is_now: bool, at_text: str | None) -> datetime | None:
    if is_now:
        return None
    when = (
        (next_codex_reset_time() if backend == BACKEND_CODEX else next_reset_time())
        if at_text is None
        else scheduled_time(at_text)
    )
    if when is None:
        return None
    when = when.astimezone(UTC)
    if when.second or when.microsecond:
        when += timedelta(minutes=1)
    return when.replace(second=0, microsecond=0).astimezone()


def add_scheduling_arguments(add_argument: Callable[..., argparse.Action]) -> None:
    add_argument("--now", action="store_true", help="run immediately")
    add_argument(
        "--at",
        metavar="TIME",
        help='local time, e.g. 9pm or "8:30am tomorrow"; rolls forward if passed',
    )


def set_command_handler(
    command_parser: argparse.ArgumentParser, handler: Callable[[Arguments], int]
) -> None:
    command_parser.set_defaults(handler=handler)


def task_id_from_arguments(arguments: Arguments) -> str:
    if arguments.id is None:
        raise SystemExit("golem: task ID is required")
    return arguments.id


def schedule_run_task(
    arguments: Arguments,
    task_id: str,
    prompt_text: str,
    task_args: tuple[str, ...],
    follow_up_records: Tasks,
    placement: RunPlacement,
) -> int:
    after = arguments.after if isinstance(arguments.after, str) else None
    if after is not None:
        dependent_record = new_task_record(
            placement, prompt_text, task_args, None, arguments.backend, after=after
        )
        if reserve_dependent_task({task_id: dependent_record, **follow_up_records}, after):
            sync_hooks(task_directory(existing_task(load_tasks(), after)))
            print(f"golem: {task_id} will run after {after}")
            return 0
    when = planned_execution_time(arguments.backend, is_now=arguments.now, at_text=arguments.at)
    return schedule_new_task(
        task_id,
        prompt_text,
        task_args,
        when,
        after,
        backend=arguments.backend,
        placement=placement,
        follow_up_records=follow_up_records,
    )


def run_task_from_arguments(arguments: Arguments) -> int:
    if arguments.auto_pickup and arguments.backend == BACKEND_CODEX:
        raise SystemExit("golem: --auto-pickup does not support Codex tasks")
    if arguments.gate is not None and arguments.backend == BACKEND_CODEX:
        raise SystemExit("golem: --gate does not support Codex tasks")
    require_launch_agent()
    task_id = task_id_from_arguments(arguments)
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8") if arguments.file else arguments.prompt
    )
    if prompt_text is None:
        raise SystemExit("golem: a prompt is required")
    task_args = tuple(arguments.agent_args)
    placement = RunPlacement(
        str(Path.cwd()),
        is_auto_pickup=arguments.auto_pickup,
        is_no_worktree=arguments.no_worktree,
        gate=arguments.gate,
        setup=arguments.setup,
    )
    has_worktree_argument = has_agent_argument(list(task_args), WORKTREE_ARGUMENTS)
    parent = load_tasks().get(arguments.after) if isinstance(arguments.after, str) else None
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
    follow_up_records = new_follow_up_records(
        task_id,
        arguments.then,
        task_args,
        arguments.backend,
        placement,
    )
    if arguments.backend == BACKEND_CODEX:
        reject_unsupported_codex_arguments(list(task_args), is_run=True)
        require_codex_subscription(os.environ.copy())
    exit_code = schedule_run_task(
        arguments, task_id, prompt_text, task_args, follow_up_records, placement
    )
    for task_id, record in follow_up_records.items():
        print(f"golem: {task_id} will run after {record.get('after')}")
    return exit_code


def add_worktree_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    worktree_command = add_parser("worktree")
    worktree_commands = worktree_command.add_subparsers(dest="worktree_command", required=True)
    set_command_handler(worktree_commands.add_parser("list"), lambda _: list_worktrees())
    auto_remove_worktree_command = worktree_commands.add_parser("auto-remove")
    auto_remove_group = auto_remove_worktree_command.add_mutually_exclusive_group(required=True)
    auto_remove_group.add_argument("--after", type=int, metavar="DAYS")
    auto_remove_group.add_argument("--off", action="store_true")
    set_command_handler(auto_remove_worktree_command, set_worktree_auto_remove_from_arguments)


def add_prune_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    prune_command = add_parser(
        "prune",
        description=(
            "Delete cancelled tasks, and done tasks whose PR is merged or closed or whose "
            f"session has been idle for {PRUNE_IDLE_DAYS} days, after asking."
        ),
    )
    prune_command.add_argument(
        "--yes", action="store_true", help="delete done tasks that qualify without asking"
    )
    set_command_handler(prune_command, lambda arguments: prune_tasks(is_confirmed=arguments.yes))


def add_continue_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    continue_command = add_parser(
        "continue", description="Re-run a stopped task's gate and start its follow-ups."
    )
    continue_command.add_argument("id")
    continue_command.add_argument("--force", action="store_true", help="skip the gate")
    set_command_handler(
        continue_command,
        lambda arguments: continue_task(
            task_id_from_arguments(arguments), is_forced=arguments.force
        ),
    )


def add_watch_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    watch_command = add_parser("watch")
    watch_command.add_argument("id")
    set_command_handler(
        watch_command, lambda arguments: watch_task(task_id_from_arguments(arguments))
    )


def add_hook_command(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    hook_command = add_parser("_hook")
    hook_command.add_argument("event", choices=tuple(HOOK_EVENT_ARGUMENTS.values()))
    set_command_handler(
        hook_command,
        lambda arguments: (
            hook_stop() if arguments.event == HOOK_ARGUMENT_STOP else hook_pre_tool_use()
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
        "--backend", choices=(BACKEND_CLAUDE, BACKEND_CODEX), default=BACKEND_CLAUDE
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
            pickup_all()
            if arguments.all
            else pickup(
                task_id_from_arguments(arguments), *arguments.agent_args, is_app=arguments.app
            )
        ),
    )
    close_command = commands.add_parser("close")
    close_command.add_argument("id")
    set_command_handler(close_command, lambda arguments: close(task_id_from_arguments(arguments)))
    add_continue_command(commands.add_parser)
    show_command = commands.add_parser("show")
    show_command.add_argument("id")
    set_command_handler(
        show_command, lambda arguments: show_task_result(task_id_from_arguments(arguments))
    )
    add_watch_command(commands.add_parser)
    cancel_command = commands.add_parser("cancel")
    cancel_command.add_argument("id")
    set_command_handler(
        cancel_command, lambda arguments: cancel_task(task_id_from_arguments(arguments))
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
        lambda arguments: retry_task(
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
        lambda arguments: reschedule_task(
            task_id_from_arguments(arguments), is_now=arguments.now, at_text=arguments.at
        ),
    )
    delete_command = commands.add_parser("delete")
    delete_command.add_argument("ids", nargs="+")
    delete_command.add_argument("--chain", action="store_true")
    delete_command.add_argument("--remove-worktree", action="store_true")
    set_command_handler(
        delete_command,
        lambda arguments: delete_tasks(
            arguments.ids,
            is_chain=arguments.chain,
            is_removing_worktree=arguments.remove_worktree,
        ),
    )
    add_prune_command(commands.add_parser)
    set_command_handler(commands.add_parser("list"), lambda _: list_tasks())
    alias_command = commands.add_parser("alias")
    alias_commands = alias_command.add_subparsers(dest="alias_command", required=True)
    set_command_handler(alias_commands.add_parser("list"), lambda _: list_aliases(commands.choices))
    add_worktree_commands(commands.add_parser)
    launch_agent_command = commands.add_parser(
        "agent",
        description=(
            "Agent logs are written to agent.log in the state directory; when it exceeds "
            f"{AGENT_LOG_MAX_BYTES // 1024**2} MiB it is copied to agent.log.1, replacing "
            "the previous copy, and truncated."
        ),
    )
    launch_agent_command.add_argument("action", choices=("install", "uninstall"))
    set_command_handler(
        launch_agent_command,
        lambda arguments: (
            install_launch_agent() if arguments.action == "install" else uninstall_launch_agent()
        ),
    )
    set_command_handler(commands.add_parser("_agent"), lambda _: run_launch_agent())
    execute_command = commands.add_parser("_execute")
    execute_command.add_argument("id")
    execute_command.add_argument("session_id")
    set_command_handler(
        execute_command,
        lambda arguments: execute_or_drain_task(
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
    arguments = parser.parse_args(golem_args, Arguments(agent_args=agent_args))
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


if __name__ == "__main__":
    raise SystemExit(main())
