import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

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
PULL_REQUEST_STATE_OPEN = "OPEN"
PRUNE_ANSWER_YES = "y"
PRUNE_ANSWER_NO = "n"
PRUNE_ANSWER_ALL = "a"
PRUNE_ANSWER_QUIT = "q"
PRUNE_ANSWERS = (PRUNE_ANSWER_YES, PRUNE_ANSWER_NO, PRUNE_ANSWER_ALL, PRUNE_ANSWER_QUIT)

type PullRequestCache = dict[str | None, dict[str, Any] | None]


def state_directory() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    golem_state_directory = (
        state_home / "golem"
        if state_home.is_absolute()
        else Path.home() / ".local" / "state" / "golem"
    )
    golem_state_directory.mkdir(parents=True, exist_ok=True)
    return golem_state_directory


def ledger_path() -> Path:
    return state_directory() / "tasks.json"


def worktree_ledger_path() -> Path:
    return state_directory() / "worktrees.json"


def settings_path() -> Path:
    return state_directory() / "settings.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def ledger_temporary_prefix(task_ledger_path: Path) -> str:
    return f".{task_ledger_path.name}."


def load_tasks() -> dict[str, Any]:
    return load_json(ledger_path())


def load_worktrees() -> dict[str, Any]:
    return load_json(worktree_ledger_path())


def save_json(path: Path, value: dict[str, Any]) -> None:
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


def save_tasks(tasks: dict[str, Any]) -> None:
    save_json(ledger_path(), tasks)


def save_settings(settings: dict[str, Any]) -> None:
    save_json(settings_path(), settings)


@contextmanager
def locked_ledger() -> Generator[tuple[dict[str, Any], dict[str, Any]]]:
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
def locked_tasks() -> Generator[dict[str, Any]]:
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


def reserve_tasks(records: dict[str, Any]) -> None:
    with locked_tasks() as tasks:
        for task_id in records:
            if task_id in tasks:
                raise SystemExit(f"golem: task already exists: {task_id}")
        tasks.update(records)


def waiting_dependent_ids(tasks: dict[str, Any], parent_id: str) -> list[str]:
    return [
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent["status"] == STATUS_WAITING and dependent.get("after") == parent_id
    ]


def dependent_task_ids(tasks: dict[str, Any], parent_id: str) -> list[str]:
    return sorted(
        dependent_id
        for dependent_id, dependent in tasks.items()
        if isinstance(dependent, dict)
        and cast("dict[str, Any]", dependent).get("after") == parent_id
    )


def has_running_dependent(tasks: dict[str, Any], parent_id: str) -> bool:
    return any(
        dependent["status"] == STATUS_RUNNING and dependent.get("after") == parent_id
        for dependent in tasks.values()
    )


def has_unreviewed_dependent(tasks: dict[str, Any], parent_id: str) -> bool:
    return any(
        dependent["status"] in UNREVIEWED_STATUSES and dependent.get("after") == parent_id
        for dependent in tasks.values()
    )


def mark_ancestors_done(tasks: dict[str, Any], task: dict[str, Any]) -> None:
    ancestor = tasks.get(task.get("after", ""))
    while isinstance(ancestor, dict):
        parent = cast("dict[str, Any]", ancestor)
        if parent.get("status") == STATUS_NEEDS_REVIEW:
            parent["status"] = STATUS_DONE
        ancestor = tasks.get(parent.get("after", ""))


def reserve_dependent_task(records: dict[str, Any], parent_id: str) -> bool:
    with locked_tasks() as tasks:
        for task_id in records:
            if task_id in tasks:
                raise SystemExit(f"golem: task already exists: {task_id}")
        if parent_id not in tasks:
            raise SystemExit(f"golem: task not found: {parent_id}")
        parent_status = tasks[parent_id]["status"]
        if parent_status == STATUS_CANCELLED:
            raise SystemExit(f"golem: parent task is cancelled: {parent_id}")
        if parent_status in SUCCEEDED_STATUSES:
            return False
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


def task_environment(task: dict[str, Any]) -> dict[str, str]:
    inherited_environment = {
        name: value for name, value in os.environ.items() if not is_captured_variable(name)
    }
    return {**inherited_environment, **task.get("environment", {})}


def codex_environment(environment: dict[str, str]) -> dict[str, str]:
    environment = environment.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODEX_API_KEY", None)
    return environment


def task_backend(task: dict[str, Any]) -> str:
    return cast("str", task.get("backend", BACKEND_CLAUDE))


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


def recorded_worktree(tasks: dict[str, Any], task: dict[str, Any]) -> tuple[str, bool] | None:
    ancestor = task
    while True:
        if "worktree" in ancestor:
            return ancestor["worktree"], ancestor.get("worktree_pending") is True
        parent = tasks.get(ancestor.get("after", ""))
        if not isinstance(parent, dict):
            return None
        ancestor = cast("dict[str, Any]", parent)
        if ancestor.get("cwd") != task.get("cwd"):
            return None


def worktree_resource(worktrees: dict[str, Any], worktree: str | None) -> dict[str, Any] | None:
    if worktree is None:
        return None
    resource = worktrees.get(resolved_path(worktree))
    return cast("dict[str, Any]", resource) if isinstance(resource, dict) else None


def clear_worktree_release(
    tasks: dict[str, Any], worktrees: dict[str, Any], task: dict[str, Any]
) -> None:
    for worktree, resource in worktrees.items():
        if task_uses_worktree(tasks, task, worktree):
            resource.pop("released_at", None)


def record_worktree_resource(
    worktrees: dict[str, Any],
    worktree: str,
    session_id: str,
    *,
    owner: str = WORKTREE_OWNER_EXTERNAL,
    repository: str | None = None,
) -> None:
    if owner == WORKTREE_OWNER_EXTERNAL:
        repository = git_common_directory(worktree)
    resource: dict[str, Any] = {
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


def record_created_worktree_owner(task_id: str, task: dict[str, Any]) -> None:
    worktree = cast("str", task["worktree"])
    with locked_ledger() as (tasks, worktrees):
        current_task = cast("dict[str, Any]", tasks.get(task_id, {}))
        resource = worktree_resource(worktrees, worktree)
        if (
            current_task.get("session_id") != task["session_id"]
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
                marker_file.write(cast("str", resource["session_id"]))
        except FileExistsError:
            return
        except OSError as error:
            print(f"golem: could not record worktree ownership: {error}", file=sys.stderr)


def resolved_worktree(tasks: dict[str, Any], task: dict[str, Any]) -> tuple[str | None, bool]:
    recorded = recorded_worktree(tasks, task)
    if recorded is not None:
        return recorded
    if has_agent_argument(task["agent_args"], WORKTREE_ARGUMENTS):
        return None, False
    common_directory = git_common_directory(task["cwd"])
    if common_directory is None or not git_has_head(task["cwd"]):
        return None, False
    return str(Path(common_directory).parent / ".claude" / "worktrees" / task["session_id"]), True


def task_directory(task: dict[str, Any]) -> str:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        return worktree
    return task["cwd"]


def directory_to_lock(task: dict[str, Any], worktree: str | None) -> str:
    return worktree if worktree is not None else task["cwd"]


def worktree_missing_message(
    worktree: str | None,
    worktrees: dict[str, Any],
    *,
    is_worktree_pending: bool,
) -> str | None:
    if worktree is None or is_worktree_pending or Path(worktree).is_dir():
        return None
    resource = worktree_resource(worktrees, worktree)
    if resource is not None and resource.get("removed_at") is not None:
        return f"golem: worktree was removed: {worktree}"
    return f"golem: worktree is missing: {worktree}"


def reject_missing_worktree(task: dict[str, Any], worktrees: dict[str, Any]) -> None:
    missing_worktree = worktree_missing_message(
        task.get("worktree"), worktrees, is_worktree_pending=task.get("worktree_pending") is True
    )
    if missing_worktree is not None:
        raise SystemExit(missing_worktree)


def clear_worktree_pending_if_created(task: dict[str, Any]) -> None:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        task.pop("worktree_pending", None)


def record_resolved_worktree(
    task: dict[str, Any], worktree: str | None, *, is_worktree_pending: bool
) -> None:
    if worktree is None:
        return
    task["worktree"] = worktree
    if is_worktree_pending:
        task["worktree_pending"] = True
    else:
        task.pop("worktree_pending", None)
    clear_worktree_pending_if_created(task)


def is_usage_limit_resume(task: dict[str, Any]) -> bool:
    return cast("int", task.get("usage_limit_resume_count", 0)) > 0


def run_claude(
    task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str, str]:
    session_id = cast("str", task["session_id"])
    is_resuming = is_usage_limit_resume(task)
    command = (
        ["claude", "--resume", session_id, "--print", USAGE_LIMIT_RESUME_PROMPT]
        if is_resuming
        else ["claude", "--session-id", session_id, "--print", cast("str", task["prompt"])]
    )
    agent_args = cast("list[str]", task["agent_args"])
    default_args: list[str] = []
    if VERBOSE_ARGUMENT not in agent_args:
        default_args.append(VERBOSE_ARGUMENT)
    if not has_agent_argument(agent_args, NAME_ARGUMENTS):
        default_args += ["--name", task_id]
    worktree = task.get("worktree")
    is_creating_worktree = worktree is not None and not Path(worktree).is_dir()
    if is_creating_worktree:
        default_args += ["--worktree", Path(cast("str", worktree)).name]
    if not has_agent_argument(agent_args, PERMISSION_ARGUMENTS):
        default_args.append("--dangerously-skip-permissions")
    claude_process = subprocess.run(
        [
            *command,
            "--output-format",
            "json",
            *agent_args,
            *default_args,
        ],
        cwd=task_directory(task),
        env={**task_environment(task), BACKGROUND_WAIT_CEILING_VARIABLE: "0"},
        capture_output=True,
        encoding="utf-8",
        pass_fds=lock_file_descriptors,
        check=False,
    )
    sys.stderr.write(claude_process.stderr)
    if is_creating_worktree:
        record_created_worktree_owner(task_id, task)
    return claude_process.returncode, claude_process.stdout, claude_process.stderr


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


def create_codex_worktree(task_id: str, task: dict[str, Any]) -> str | None:
    worktree = cast("str", task["worktree"])
    cwd = cast("str", task["cwd"])
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
        if isinstance(stored_task, dict):
            task = cast("dict[str, Any]", stored_task)
            if task.get("session_id") == session_id:
                task["thread_id"] = thread_id


def codex_event_message(event: dict[str, object]) -> str | None:
    message = event.get("message")
    return message if isinstance(message, str) else None


def codex_command(task: dict[str, Any]) -> tuple[list[str], str]:
    agent_args = cast("list[str]", task["agent_args"])
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
    prompt = cast("str", task["prompt"])
    if is_usage_limit_resume(task):
        command += ["resume", cast("str", task["thread_id"]), "-"]
        return command, USAGE_LIMIT_RESUME_PROMPT
    command.append("-")
    return command, prompt


@dataclass
class CodexEventState:
    last_agent_message: str | None = None
    failed_message: str | None = None
    last_error_message: str | None = None


def update_codex_event_state(
    event: dict[str, object], task_id: str, session_id: str, state: CodexEventState
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
    process: subprocess.Popen[str], task_id: str, session_id: str
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
                    cast("dict[str, object]", event), task_id, session_id, state
                )
    finally:
        output.close()
    return state


def run_codex(
    task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str | None, bool]:
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
    event_state = read_codex_events(process, task_id, cast("str", task["session_id"]))
    returncode = process.wait()
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


def new_task_record(
    cwd: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    backend: str,
    *,
    after: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "cwd": cwd,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "status": STATUS_WAITING,
        "session_id": new_session_id(),
        "environment": captured_environment(),
        "backend": backend,
    }
    if after is not None:
        record["after"] = after
    if when is not None:
        record["status"] = STATUS_SCHEDULED
        record["due_at"] = when.isoformat()
    return record


def new_follow_up_records(
    run_task_id: str, follow_ups: list[list[str]], agent_args: tuple[str, ...], backend: str
) -> dict[str, Any]:
    records: dict[str, Any] = {}
    previous_task_id = run_task_id
    for task_id, prompt in follow_ups:
        if task_id == run_task_id or task_id in records:
            raise SystemExit(f"golem: task ID used more than once: {task_id}")
        records[task_id] = new_task_record(
            str(Path.cwd()), prompt, agent_args, None, backend, after=previous_task_id
        )
        previous_task_id = task_id
    return records


def pickup(task_id: str, *agent_args: str, is_app: bool, is_review_required: bool = False) -> int:
    preflight_task = load_tasks().get(task_id)
    is_codex = isinstance(preflight_task, dict) and (
        task_backend(cast("dict[str, Any]", preflight_task)) == BACKEND_CODEX
    )
    if is_codex and is_app:
        raise SystemExit(f"golem: pickup --app does not support Codex tasks: {task_id}")
    if is_codex:
        reject_unsupported_codex_arguments(list(agent_args), is_run=False)
        require_codex_subscription(os.environ.copy())
    with locked_ledger() as (tasks, worktrees):
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if is_review_required and task.get("status") != STATUS_NEEDS_REVIEW:
            return 0
        if task.get("status") in {STATUS_SCHEDULED, STATUS_WAITING, STATUS_CANCELLED}:
            raise SystemExit(f"golem: task has not started: {task_id}")
        reject_missing_worktree(task, worktrees)
        directory = directory_to_lock(task, task.get("worktree"))
        directory_lock_file = acquire_lock(directory_lock_path(directory))
        if directory_lock_file is None:
            raise SystemExit(f"golem: directory is busy: {directory}")
        thread_id = task.get("thread_id")
        if is_codex and not isinstance(thread_id, str):
            release_lock(directory_lock_file)
            raise SystemExit(f"golem: task has no Codex conversation: {task_id}")
        clear_worktree_release(tasks, worktrees, task)
        if task.get("status") == STATUS_NEEDS_REVIEW:
            task["status"] = STATUS_DONE
            mark_ancestors_done(tasks, task)
        cwd = task_directory(task)
        session_id = cast("str", task["session_id"])
    if is_app:
        release_lock(directory_lock_file)
        return subprocess.run(
            ["open", f"claude://resume?session={session_id}"], check=False
        ).returncode
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


def pickup_all() -> int:
    tasks = load_tasks()
    task_ids = [
        task_id
        for task_id, task in tasks.items()
        if task.get("status") == STATUS_NEEDS_REVIEW
        and not has_unreviewed_dependent(tasks, task_id)
    ]
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


def close(task_id: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if task.get("status") != STATUS_NEEDS_REVIEW:
            raise SystemExit(f"golem: task does not need review: {task_id}")
        task["status"] = STATUS_DONE
    return 0


def show_task_result(task_id: str) -> int:
    tasks = load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    task = cast("dict[str, Any]", tasks[task_id])
    result = task.get("result")
    if not isinstance(result, str):
        raise SystemExit(f"golem: task has no result: {task_id}")
    print_task_result(result)
    return 0


def cancel_task(task_id: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if task.get("status") not in {STATUS_SCHEDULED, STATUS_WAITING}:
            raise SystemExit(f"golem: task is not scheduled: {task_id}")
        task["status"] = STATUS_CANCELLED
        pending_task_ids = [task_id]
        while pending_task_ids:
            parent_id = pending_task_ids.pop()
            for waiting_task_id in waiting_dependent_ids(tasks, parent_id):
                waiting_task = cast("dict[str, Any]", tasks[waiting_task_id])
                waiting_task["status"] = STATUS_CANCELLED
                pending_task_ids.append(waiting_task_id)
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


def list_tasks() -> int:
    tasks = load_tasks()
    if not tasks:
        print("golem: no tasks")
        return 0
    today = datetime.now().astimezone().date()
    rows = [
        (
            task_id,
            task["status"],
            display_directory(task["cwd"]),
            display_due_at(task["due_at"], today) if "due_at" in task else "",
            task["after"] if task["status"] == STATUS_WAITING else "",
        )
        for task_id, task in tasks.items()
    ]
    rows.sort(key=lambda row: (LIST_STATUS_ORDER.index(row[1]), row[0]))
    headers = ("ID", "STATUS", "DIRECTORY", "DUE AT", "AFTER")
    widths = [
        max(len(str(value)) for value in column) for column in zip(headers, *rows, strict=True)
    ]
    is_color = is_color_enabled()
    print(
        "  ".join(value.ljust(width) for value, width in zip(headers, widths, strict=True)).rstrip()
    )
    for task_id, status, directory, due_at, after in rows:
        print(
            "  ".join(
                (
                    task_id.ljust(widths[0]),
                    display_status(status, widths[1], is_color=is_color),
                    directory.ljust(widths[2]),
                    due_at.ljust(widths[3]),
                    after.ljust(widths[4]),
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
        frames = [json.loads(line) for line in result.stdout.splitlines()]
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


def codex_app_server_requests(*, has_rate_limits: bool) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
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
) -> dict[str, Any]:
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
    return cast("dict[str, Any]", response)


def validate_codex_app_server(
    process: subprocess.Popen[str], responses: dict[int, dict[str, Any]]
) -> None:
    if process.returncode:
        raise OSError("Codex app-server failed")
    if any("error" in response or "result" not in response for response in responses.values()):
        raise ValueError("Codex app-server returned an error")


def codex_app_server_responses(
    environment: dict[str, str], *, has_rate_limits: bool
) -> dict[int, dict[str, Any]]:
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
        responses: dict[int, dict[str, Any]] = {}
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


def validate_codex_subscription(responses: dict[int, dict[str, Any]]) -> None:
    account_result = cast("object", responses[2]["result"])
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


def codex_rate_limits() -> dict[str, Any]:
    responses = codex_app_server_responses(os.environ.copy(), has_rate_limits=True)
    validate_codex_subscription(responses)
    rate_limits_result = cast("object", responses[3]["result"])
    rate_limits_fields = (
        cast("dict[str, object]", rate_limits_result)
        if isinstance(rate_limits_result, dict)
        else None
    )
    if rate_limits_fields is None or not isinstance(rate_limits_fields.get("rateLimits"), dict):
        raise SystemExit(CODEX_ACCOUNT_ERROR)
    return cast("dict[str, Any]", rate_limits_fields["rateLimits"])


def codex_reset_time_from_limit(limit: dict[str, Any]) -> datetime:
    resets_at = limit.get("resetsAt")
    if not isinstance(resets_at, int | float):
        raise SystemExit(USAGE_ERROR)
    return validated_reset_time(datetime.fromtimestamp(resets_at, UTC))


def required_codex_primary_limit(rate_limits: dict[str, Any]) -> dict[str, Any] | None:
    if "primary" not in rate_limits:
        raise SystemExit(USAGE_ERROR)
    primary = rate_limits["primary"]
    return cast("dict[str, Any]", primary) if isinstance(primary, dict) else None


def codex_active_reset_time(primary: dict[str, Any]) -> datetime | None:
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
            cast("dict[str, Any]", limit)
            for limit in (primary, secondary)
            if isinstance(limit, dict)
        ]
        exhausted_limits = [
            limit
            for limit in limits
            if isinstance(limit.get("usedPercent"), int | float)
            and limit["usedPercent"] >= FULL_UTILIZATION
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


def reset_time_from_limit(limit: dict[str, Any]) -> datetime:
    return validated_reset_time(datetime.fromisoformat(limit["resets_at"]))


def schedule_new_task(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None,
    *,
    backend: str,
    follow_up_records: dict[str, Any],
) -> int:
    due_time = when or datetime.now(UTC).astimezone()
    record = new_task_record(str(Path.cwd()), prompt, agent_args, due_time, backend, after=after)
    reserve_tasks({task_id: record, **follow_up_records})
    return wake_launch_agent_and_report(task_id, due_time)


def claude_result_and_usage_limit(output: str) -> tuple[str, bool]:
    try:
        parsed_response = cast("object", json.loads(output))
    except json.JSONDecodeError:
        return output, False
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
    try:
        parsed_response = cast("object", json.loads(output))
    except json.JSONDecodeError:
        return None
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
        task = cast("dict[str, Any]", tasks[task_id])
        record_worktree_resource(worktrees, worktree, cast("str", task["session_id"]))


def clear_recorded_worktree_pending(task_id: str) -> None:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if isinstance(task, dict):
            clear_worktree_pending_if_created(cast("dict[str, Any]", task))


def mark_task_waiting_for_reset(task_id: str, result: str) -> bool:
    try:
        task = load_tasks().get(task_id)
        if not isinstance(task, dict):
            return False
        when = planned_execution_time(
            task_backend(cast("dict[str, Any]", task)), is_now=False, at_text=None
        )
    except SystemExit:
        return False
    if when is None:
        return False
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return False
        task_record = cast("dict[str, Any]", task)
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
        if not isinstance(stored_task, dict):
            return
        task_record = cast("dict[str, Any]", stored_task)
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


def claim_task(
    tasks: dict[str, Any],
    worktrees: dict[str, Any],
    task_id: str,
    session_id: str,
    expected_status: str,
) -> tuple[dict[str, Any], io.TextIOWrapper, io.TextIOWrapper] | None:
    task = tasks.get(task_id)
    if task is None or task["status"] != expected_status or task["session_id"] != session_id:
        return None
    if expected_status == STATUS_SCHEDULED and datetime.fromisoformat(
        task["due_at"]
    ) > datetime.now(UTC):
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
    task["status"] = STATUS_RUNNING
    return task.copy(), run_lock_file, directory_lock_file


def claim_next_dependent(
    task_id: str, parent_session_id: str
) -> tuple[str, dict[str, Any], io.TextIOWrapper, io.TextIOWrapper] | None:
    with locked_ledger() as (tasks, worktrees):
        parent_record = tasks.get(task_id)
        if not isinstance(parent_record, dict):
            return None
        parent = cast("dict[str, Any]", parent_record)
        if (
            parent.get("session_id") != parent_session_id
            or parent.get("status") not in SUCCEEDED_STATUSES
        ):
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


def execute_task(task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]) -> int:
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
    if task_backend(task) == BACKEND_CLAUDE:
        working_directory = claude_working_directory(output)
        if working_directory is not None and working_directory != task["cwd"]:
            record_worktree(task_id, working_directory)
    clear_recorded_worktree_pending(task_id)
    usage_limit_resume_count = cast("int", task.get("usage_limit_resume_count", 0))
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


def execute_claimed_task(
    task_id: str,
    task: dict[str, Any],
    run_lock_file: io.TextIOWrapper,
    directory_lock_file: io.TextIOWrapper,
) -> int:
    try:
        try:
            returncode = execute_task(
                task_id,
                task,
                (run_lock_file.fileno(), directory_lock_file.fileno()),
            )
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


def drain_succeeded_task(task_id: str, session_id: str) -> None:
    with locked_tasks():
        run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return
    try:
        drain_dependents(task_id, session_id)
    finally:
        release_run_lock(run_lock_file, session_id)


def execute_or_drain_task(task_id: str, session_id: str) -> int:
    task = load_tasks().get(task_id)
    if task is None or task["session_id"] != session_id or task["status"] not in SUCCEEDED_STATUSES:
        return execute_scheduled_task(task_id, session_id)
    drain_succeeded_task(task_id, session_id)
    return 0


def start_task_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*golem_command(), "_execute", task_id, task["session_id"]],
        env=task_environment(task),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )


def is_run_lock_free(session_id: str) -> bool:
    run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return False
    release_run_lock(run_lock_file, session_id)
    return True


def task_uses_worktree(tasks: dict[str, Any], task: dict[str, Any], worktree: str) -> bool:
    recorded = recorded_worktree(tasks, task)
    directories = [cast("str", task["cwd"])]
    if recorded is not None:
        directories.append(recorded[0])
    worktree_path = Path(worktree).resolve()
    return any(Path(directory).resolve().is_relative_to(worktree_path) for directory in directories)


def worktree_task_ids(tasks: dict[str, Any], worktree: str) -> list[str]:
    return [
        task_id
        for task_id, task in tasks.items()
        if isinstance(task, dict)
        and task_uses_worktree(tasks, cast("dict[str, Any]", task), worktree)
    ]


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
    resource: dict[str, Any], tasks: dict[str, Any], worktree: str, reason: str
) -> bool:
    match = CLAUDE_LOCK_PATTERN.fullmatch(reason)
    if match is None:
        return False
    session_id, process_id_text = match.groups()
    session_ids = {cast("str", resource["session_id"])}
    session_ids.update(
        cast("str", tasks[task_id]["session_id"]) for task_id in worktree_task_ids(tasks, worktree)
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
    resource: dict[str, Any],
    tasks: dict[str, Any],
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
    if not is_worktree_owner_verified(worktree, cast("str", resource["session_id"])):
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


def task_worktree_retention_reasons(
    tasks: dict[str, Any], worktree: str, *, is_released: bool
) -> list[str]:
    reasons: list[str] = []
    worktree_tasks = [
        cast("dict[str, Any]", tasks[task_id]) for task_id in worktree_task_ids(tasks, worktree)
    ]
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
    tasks: dict[str, Any],
    resource: dict[str, Any] | None,
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


def record_worktree_removal_error(resource: dict[str, Any], error: str) -> tuple[bool, list[str]]:
    resource["removal_error"] = error
    return False, [error]


def remove_inspected_worktree_resource(
    resource: dict[str, Any], entry: list[str], worktree: str
) -> tuple[bool, list[str]]:
    commands = ["remove"]
    if git_worktree_lock_reason(entry) is not None:
        commands.insert(0, "unlock")
    for command in commands:
        result = run_git(
            resource["repository"],
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
    tasks: dict[str, Any], worktrees: dict[str, Any], worktree: str, *, is_released: bool = False
) -> Generator[tuple[dict[str, Any] | None, list[str] | None, list[str]]]:
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
    tasks: dict[str, Any], worktrees: dict[str, Any], worktree: str, *, is_released: bool = False
) -> tuple[bool, list[str]]:
    with locked_worktree_inspection(tasks, worktrees, worktree, is_released=is_released) as (
        resource,
        entry,
        reasons,
    ):
        if reasons:
            return False, reasons
        resource = cast("dict[str, Any]", resource)
        return remove_inspected_worktree_resource(resource, cast("list[str]", entry), worktree)


def is_known_directory_busy(tasks: dict[str, Any], task: dict[str, Any]) -> bool:
    recorded = recorded_worktree(tasks, task)
    if recorded is not None:
        directory = recorded[0]
    elif has_agent_argument(task["agent_args"], WORKTREE_ARGUMENTS):
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
    due_tasks: list[tuple[str, dict[str, Any]]] = []
    with locked_tasks() as tasks:
        remove_orphan_run_locks()
        remove_interrupted_ledger_writes()
        for task_id, task in tasks.items():
            if task["status"] == STATUS_RUNNING:
                if is_run_lock_free(task["session_id"]):
                    task["status"] = STATUS_FAILED
            elif (
                task["status"] == STATUS_SCHEDULED
                and datetime.fromisoformat(task["due_at"]) <= now
                and not is_known_directory_busy(tasks, task)
            ) or (
                task["status"] in SUCCEEDED_STATUSES
                and (dependent_ids := waiting_dependent_ids(tasks, task_id))
                and not has_running_dependent(tasks, task_id)
                and not is_known_directory_busy(tasks, tasks[dependent_ids[0]])
                and is_run_lock_free(task["session_id"])
            ):
                due_tasks.append((task_id, task.copy()))
    processes: list[subprocess.Popen[bytes]] = []
    for task_id, task in due_tasks:
        processes.append(start_task_executor(task_id, task))
    return processes


def has_pending_tasks(tasks: dict[str, Any]) -> bool:
    return any(
        task["status"] in {STATUS_SCHEDULED, STATUS_RUNNING}
        or (
            task["status"] == STATUS_WAITING
            and tasks.get(task.get("after", ""), {}).get("status") in SUCCEEDED_STATUSES
        )
        for task in tasks.values()
    )


def list_worktrees() -> int:
    tasks = load_tasks()
    worktrees = load_worktrees()
    worktree_paths = set(worktrees)
    for task in tasks.values():
        if isinstance(task, dict):
            recorded = recorded_worktree(tasks, cast("dict[str, Any]", task))
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
        if resource is not None and isinstance(resource.get("removal_error"), str):
            reasons.append(f"last removal error: {resource['removal_error']}")
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
        max(len(str(value)) for value in column) for column in zip(headers, *rows, strict=True)
    ]
    for row in (headers, *rows):
        print(
            "  ".join(
                str(value).ljust(width) for value, width in zip(row, widths, strict=True)
            ).rstrip()
        )
    return 0


def delete_task_and_release_worktrees(
    tasks: dict[str, Any], worktrees: dict[str, Any], task_ids: list[str]
) -> None:
    selected_tasks = [cast("dict[str, Any]", tasks[task_id]) for task_id in task_ids]
    worktree_paths = [
        path
        for path in worktrees
        if any(task_uses_worktree(tasks, task, path) for task in selected_tasks)
    ]
    for task_id in task_ids:
        tasks.pop(task_id)
    for path in worktree_paths:
        resource = cast("dict[str, Any]", worktrees[path])
        if (
            resource.get("owner") == WORKTREE_OWNER_GOLEM
            and resource.get("removed_at") is None
            and not worktree_task_ids(tasks, path)
        ):
            resource.setdefault("released_at", datetime.now(UTC).isoformat())


def chain_task_ids(tasks: dict[str, Any], task_id: str) -> set[str]:
    chain_ids = {task_id}
    pending_ids = [task_id]
    while pending_ids:
        current_id = pending_ids.pop()
        current_task = cast("dict[str, Any]", tasks[current_id])
        adjacent_ids = dependent_task_ids(tasks, current_id)
        parent_id = current_task.get("after")
        if isinstance(parent_id, str) and isinstance(tasks.get(parent_id), dict):
            adjacent_ids.append(parent_id)
        for adjacent_id in adjacent_ids:
            if adjacent_id not in chain_ids:
                chain_ids.add(adjacent_id)
                pending_ids.append(adjacent_id)
    return chain_ids


def selected_delete_task_ids(
    tasks: dict[str, Any], task_ids: list[str], *, is_chain: bool
) -> tuple[list[str], list[str]]:
    selected_ids: set[str] = set()
    messages: list[str] = []
    for task_id in task_ids:
        if not isinstance(tasks.get(task_id), dict):
            messages.append(f"golem: task not found: {task_id}")
        elif is_chain:
            selected_ids.update(chain_task_ids(tasks, task_id))
        else:
            selected_ids.add(task_id)
    return [task_id for task_id in tasks if task_id in selected_ids], messages


def delete_task_checks(
    tasks: dict[str, Any], task_ids: list[str], *, is_removing_worktree: bool
) -> tuple[dict[str, str | None], list[str]]:
    selected_id_set = set(task_ids)
    worktrees_by_task: dict[str, str | None] = {}
    messages: list[str] = []
    for task_id in task_ids:
        task = cast("dict[str, Any]", tasks[task_id])
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


def remove_selected_worktrees(
    tasks: dict[str, Any],
    worktrees: dict[str, Any],
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
                print(
                    f"golem: retained worktree: {worktree}: {'; '.join(reasons)}",
                    file=sys.stderr,
                )
            return None
        removed_worktrees: set[str] = set()
        for worktree, resource, entry, _ in inspections:
            is_removed, reasons = remove_inspected_worktree_resource(
                cast("dict[str, Any]", resource), cast("list[str]", entry), worktree
            )
            if not is_removed:
                print(
                    f"golem: retained worktree: {worktree}: {'; '.join(reasons)}",
                    file=sys.stderr,
                )
                return None
            removed_worktrees.add(worktree)
    return removed_worktrees


def delete_tasks(task_ids: list[str], *, is_chain: bool, is_removing_worktree: bool) -> int:
    with locked_ledger() as (tasks, worktrees):
        selected_task_ids, messages = selected_delete_task_ids(tasks, task_ids, is_chain=is_chain)
        worktrees_by_task, check_messages = delete_task_checks(
            tasks, selected_task_ids, is_removing_worktree=is_removing_worktree
        )
        if messages or check_messages:
            raise SystemExit("\n".join([*messages, *check_messages]))
        removed_worktrees: set[str] = set()
        if is_removing_worktree:
            removal_result = remove_selected_worktrees(tasks, worktrees, worktrees_by_task)
            if removal_result is None:
                return 1
            removed_worktrees = removal_result
        selected_tasks = [
            (task_id, cast("dict[str, Any]", tasks[task_id])) for task_id in selected_task_ids
        ]
        delete_task_and_release_worktrees(tasks, worktrees, selected_task_ids)
        for task_id, task in selected_tasks:
            worktree = worktrees_by_task[task_id]
            print_deleted_task(task_id, task, worktree, is_removed=worktree in removed_worktrees)
    return 0


def print_deleted_task(
    task_id: str, task: dict[str, Any], worktree: str | None, *, is_removed: bool
) -> None:
    print(f"golem: deleted task: {task_id}")
    session = task.get("thread_id") if task_backend(task) == BACKEND_CODEX else task["session_id"]
    if isinstance(session, str):
        print(f"golem: session: {session}")
    if worktree is not None:
        print(f"golem: worktree: {worktree}")
    if is_removed:
        print(f"golem: removed worktree: {worktree}")


def dependents_first_task_ids(tasks: dict[str, Any], task_ids: list[str]) -> list[str]:
    ordered_task_ids: list[str] = []
    for task_id in task_ids:
        ordered_task_ids += dependents_first_task_ids(tasks, dependent_task_ids(tasks, task_id))
        ordered_task_ids.append(task_id)
    return ordered_task_ids


def branch_pull_request(worktree: str | None) -> dict[str, Any] | None:
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
    pull_requests = cast("list[dict[str, Any]]", json.loads(result.stdout))
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


def transcript_paths(task: dict[str, Any]) -> list[Path]:
    if task_backend(task) == BACKEND_CLAUDE:
        return list((Path.home() / ".claude" / "projects").glob(f"*/{task['session_id']}.jsonl"))
    thread_id = task.get("thread_id")
    if not isinstance(thread_id, str):
        return []
    codex_home = Path(task.get("environment", {}).get("CODEX_HOME", Path.home() / ".codex"))
    return list((codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"))


def session_idle_reason(task: dict[str, Any]) -> str | None:
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


def done_task_prune_reason(task: dict[str, Any], pull_requests: PullRequestCache) -> str | None:
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


def prune_answer(task_id: str, reason: str) -> str:
    prompt = f"Remove {task_id} ({STATUS_DONE}, {reason})? [{','.join(PRUNE_ANSWERS)}] "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            print()
            return PRUNE_ANSWER_QUIT
        if answer in PRUNE_ANSWERS:
            return answer


def prune_selection(
    tasks: dict[str, Any],
    pull_requests: PullRequestCache,
    *,
    is_confirmed: bool,
    is_keeping_done_tasks: bool,
) -> tuple[dict[str, dict[str, Any]], int]:
    selected_tasks: dict[str, dict[str, Any]] = {}
    candidate_count = 0
    answer = PRUNE_ANSWER_ALL if is_confirmed else None
    root_task_ids = sorted(
        task_id for task_id, task in tasks.items() if task.get("after") not in tasks
    )
    for task_id in dependents_first_task_ids(tasks, root_task_ids):
        task = cast("dict[str, Any]", tasks[task_id])
        if any(
            dependent_id not in selected_tasks
            for dependent_id in dependent_task_ids(tasks, task_id)
        ):
            continue
        if task["status"] == STATUS_CANCELLED:
            candidate_count += 1
            selected_tasks[task_id] = task
            continue
        if task["status"] != STATUS_DONE or is_keeping_done_tasks or answer == PRUNE_ANSWER_QUIT:
            continue
        reason = done_task_prune_reason(task, pull_requests)
        if reason is None:
            continue
        candidate_count += 1
        if answer != PRUNE_ANSWER_ALL:
            answer = prune_answer(task_id, reason)
        if answer in {PRUNE_ANSWER_YES, PRUNE_ANSWER_ALL}:
            selected_tasks[task_id] = task
    return selected_tasks, candidate_count


def delete_pruned_tasks(
    selected_tasks: dict[str, dict[str, Any]],
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
        selected_tasks, candidate_count = prune_selection(
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
    elif not candidate_count:
        print("golem: no tasks to prune")
        return 0
    delete_pruned_tasks(selected_tasks, pull_requests)
    return 0


def set_worktree_auto_remove(retention_days: int | None) -> int:
    settings = load_json(settings_path())
    if retention_days is None:
        settings.pop("worktree_retention_days", None)
    else:
        settings["worktree_retention_days"] = retention_days
    save_settings(settings)
    return 0


def set_worktree_auto_remove_from_arguments(arguments: argparse.Namespace) -> int:
    if arguments.off:
        return set_worktree_auto_remove(None)
    retention_days = cast("int", arguments.after)
    if retention_days < 0:
        raise SystemExit("golem: --after must be non-negative")
    return set_worktree_auto_remove(retention_days)


def auto_remove_worktrees() -> None:
    settings = load_json(settings_path())
    retention_days = cast("int | None", settings.get("worktree_retention_days"))
    if retention_days is None:
        return
    now = datetime.now(UTC)
    with locked_ledger() as (tasks, worktrees):
        for worktree, resource in cast("dict[str, dict[str, Any]]", worktrees).items():
            if (
                resource.get("owner") != WORKTREE_OWNER_GOLEM
                or resource.get("removed_at") is not None
            ):
                continue
            if "released_at" not in resource:
                continue
            released_at = cast("str", resource["released_at"])
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
                print(
                    f"golem: retained worktree: {worktree}: {'; '.join(reasons)}",
                    file=sys.stderr,
                )


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


def existing_task(tasks: dict[str, Any], task_id: str) -> dict[str, Any]:
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    return cast("dict[str, Any]", tasks[task_id])


def retryable_task(tasks: dict[str, Any], task_id: str) -> dict[str, Any]:
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
            cast("str", task["cwd"]),
            cast("str", task["prompt"]),
            tuple(cast("list[str]", task["agent_args"])),
            when,
            task_backend(task),
            after=task.get("after"),
        )
        for name in ("environment", "worktree", "worktree_pending"):
            if name in task:
                record[name] = task[name]
        tasks[task_id] = record
        clear_worktree_release(tasks, worktrees, record)
    return wake_launch_agent_and_report(task_id, when)


def scheduled_task(tasks: dict[str, Any], task_id: str) -> dict[str, Any]:
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
    command_parser: argparse.ArgumentParser, handler: Callable[[argparse.Namespace], int]
) -> None:
    command_parser.set_defaults(handler=handler)


def schedule_run_task(
    arguments: argparse.Namespace,
    prompt_text: str,
    task_args: tuple[str, ...],
    follow_up_records: dict[str, Any],
) -> int:
    if arguments.after is not None:
        dependent_record = new_task_record(
            str(Path.cwd()), prompt_text, task_args, None, arguments.backend, after=arguments.after
        )
        if reserve_dependent_task(
            {arguments.id: dependent_record, **follow_up_records}, arguments.after
        ):
            print(f"golem: {arguments.id} will run after {arguments.after}")
            return 0
    when = planned_execution_time(arguments.backend, is_now=arguments.now, at_text=arguments.at)
    return schedule_new_task(
        arguments.id,
        prompt_text,
        task_args,
        when,
        arguments.after,
        backend=arguments.backend,
        follow_up_records=follow_up_records,
    )


def run_task_from_arguments(arguments: argparse.Namespace) -> int:
    require_launch_agent()
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8") if arguments.file else arguments.prompt
    )
    task_args = tuple(arguments.agent_args)
    follow_up_records = new_follow_up_records(
        arguments.id, arguments.then, task_args, arguments.backend
    )
    if arguments.backend == BACKEND_CODEX:
        reject_unsupported_codex_arguments(list(task_args), is_run=True)
        require_codex_subscription(os.environ.copy())
    exit_code = schedule_run_task(arguments, prompt_text, task_args, follow_up_records)
    for task_id, record in follow_up_records.items():
        print(f"golem: {task_id} will run after {record['after']}")
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
    run_schedule_group = run_command.add_mutually_exclusive_group()
    add_scheduling_arguments(run_schedule_group.add_argument)
    run_schedule_group.add_argument("--after", metavar="PARENT", help="run after PARENT succeeds")
    set_command_handler(run_command, run_task_from_arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="golem")
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
            else pickup(arguments.id, *arguments.agent_args, is_app=arguments.app)
        ),
    )
    close_command = commands.add_parser("close")
    close_command.add_argument("id")
    set_command_handler(close_command, lambda arguments: close(arguments.id))
    show_command = commands.add_parser("show")
    show_command.add_argument("id")
    set_command_handler(show_command, lambda arguments: show_task_result(arguments.id))
    cancel_command = commands.add_parser("cancel")
    cancel_command.add_argument("id")
    set_command_handler(cancel_command, lambda arguments: cancel_task(arguments.id))
    retry_command = commands.add_parser(
        "retry",
        description=SCHEDULE_DESCRIPTION,
    )
    retry_command.add_argument("id")
    retry_schedule_group = retry_command.add_mutually_exclusive_group()
    add_scheduling_arguments(retry_schedule_group.add_argument)
    set_command_handler(
        retry_command,
        lambda arguments: retry_task(arguments.id, is_now=arguments.now, at_text=arguments.at),
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
        lambda arguments: reschedule_task(arguments.id, is_now=arguments.now, at_text=arguments.at),
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
        lambda arguments: execute_or_drain_task(arguments.id, arguments.session_id),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    cast("io.TextIOWrapper", sys.stdout).reconfigure(encoding="utf-8")
    golem_args = sys.argv[1:] if argv is None else argv
    agent_args: list[str] = []
    if "--" in golem_args:
        separator_index = golem_args.index("--")
        agent_args = golem_args[separator_index + 1 :]
        golem_args = golem_args[:separator_index]

    parser = build_parser()
    arguments = parser.parse_args(golem_args, argparse.Namespace(agent_args=agent_args))
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
