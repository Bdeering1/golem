import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
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
USAGE_TIMEOUT_SECONDS = 30
USAGE_LIMIT_HTTP_STATUS = 429
FULL_UTILIZATION = 100
MAX_USAGE_LIMIT_RESUMES = 2
USAGE_LIMIT_RESUME_PROMPT = "The usage limit has reset. Continue the task."
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
VERBOSE_ARGUMENT = "--verbose"
WORKTREE_OWNER_GOLEM = "golem"
WORKTREE_OWNER_EXTERNAL = "external"
WORKTREE_OWNER_MARKER = "golem-owner"
CLAUDE_LOCK_PATTERN = re.compile(r"claude session (\S+) \(pid (\d+) start .+\)")


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


def reserve_task(task_id: str, record: str | dict[str, Any]) -> None:
    with locked_tasks() as tasks:
        if task_id in tasks:
            raise SystemExit(f"golem: task already exists: {task_id}")
        tasks[task_id] = record


def waiting_dependent_ids(tasks: dict[str, Any], parent_id: str) -> list[str]:
    return [
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent["status"] == STATUS_WAITING and dependent.get("after") == parent_id
    ]


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


def reserve_dependent_task(
    task_id: str,
    record: dict[str, Any],
    parent_id: str,
) -> bool:
    with locked_tasks() as tasks:
        if task_id in tasks:
            raise SystemExit(f"golem: task already exists: {task_id}")
        if parent_id not in tasks:
            raise SystemExit(f"golem: task not found: {parent_id}")
        parent_status = tasks[parent_id]["status"]
        if parent_status == STATUS_CANCELLED:
            raise SystemExit(f"golem: parent task is cancelled: {parent_id}")
        if parent_status in SUCCEEDED_STATUSES:
            return False
        tasks[task_id] = record
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
    return name in {"PATH", "LANG"} or name.startswith("LC_")


def captured_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if is_captured_variable(name)}


def task_environment(task: dict[str, Any]) -> dict[str, str]:
    inherited_environment = {
        name: value for name, value in os.environ.items() if not is_captured_variable(name)
    }
    return {**inherited_environment, **task.get("environment", {})}


def has_agent_argument(agent_args: list[str], names: set[str]) -> bool:
    return any(
        argument == name or argument.startswith(f"{name}=")
        for argument in agent_args
        for name in names
    )


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


def run_claude(
    task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str]:
    session_id = cast("str", task["session_id"])
    is_resuming = cast("int", task.get("usage_limit_resume_count", 0)) > 0
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
        env=task_environment(task),
        stdout=subprocess.PIPE,
        encoding="utf-8",
        pass_fds=lock_file_descriptors,
        check=False,
    )
    if is_creating_worktree:
        record_created_worktree_owner(task_id, task)
    return claude_process.returncode, claude_process.stdout


def new_task_record(
    cwd: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "cwd": cwd,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "status": STATUS_WAITING,
        "session_id": new_session_id(),
        "environment": captured_environment(),
    }
    if after is not None:
        record["after"] = after
    if when is not None:
        record["status"] = STATUS_SCHEDULED
        record["due_at"] = when.isoformat()
    return record


def pickup(task_id: str, *agent_args: str, is_app: bool, is_review_required: bool = False) -> int:
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


def reset_time_from_limit(limit: dict[str, Any]) -> datetime:
    reset_time = datetime.fromisoformat(limit["resets_at"])
    if reset_time.tzinfo is None or reset_time <= datetime.now(UTC):
        raise SystemExit(USAGE_ERROR)
    return reset_time


def schedule_new_task(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None,
) -> int:
    due_time = when or datetime.now(UTC).astimezone()
    record = new_task_record(str(Path.cwd()), prompt, agent_args, due_time, after)
    reserve_task(task_id, record)
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
        when = planned_execution_time(is_now=False, at_text=None)
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
            STATUS_NEEDS_REVIEW if returncode == 0 and not is_usage_limit else STATUS_FAILED
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
    try:
        returncode, output = run_claude(task_id, task, lock_file_descriptors)
    except OSError as error:
        print(f"golem: could not execute task: {error}", file=sys.stderr)
        returncode = 127
        output = ""
    result, is_usage_limit = claude_result_and_usage_limit(output)
    working_directory = claude_working_directory(output)
    if working_directory is not None and working_directory != task["cwd"]:
        record_worktree(task_id, working_directory)
    clear_recorded_worktree_pending(task_id)
    usage_limit_resume_count = cast("int", task.get("usage_limit_resume_count", 0))
    if (
        is_usage_limit
        and usage_limit_resume_count < MAX_USAGE_LIMIT_RESUMES
        and mark_task_waiting_for_reset(task_id, result)
    ):
        return 0
    finish_task(
        task_id,
        result if output else None,
        returncode,
        is_usage_limit=is_usage_limit,
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
        commands = ["remove"]
        if git_worktree_lock_reason(cast("list[str]", entry)) is not None:
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
                error = (
                    result.stderr.strip() or result.stdout.strip() or "git worktree command failed"
                )
                return record_worktree_removal_error(resource, error)
        resource["removed_at"] = datetime.now(UTC).isoformat()
        resource.pop("removal_error", None)
        return True, []


def is_known_directory_busy(tasks: dict[str, Any], task: dict[str, Any]) -> bool:
    recorded = recorded_worktree(tasks, task)
    if recorded is not None:
        directory = recorded[0]
    elif has_agent_argument(task["agent_args"], WORKTREE_ARGUMENTS):
        directory = task["cwd"]
    else:
        return False
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


def delete_task(task_id: str, *, is_removing_worktree: bool) -> int:
    with locked_ledger() as (tasks, worktrees):
        stored_task = tasks.get(task_id)
        if not isinstance(stored_task, dict):
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", stored_task)
        if task.get("status") == STATUS_RUNNING:
            raise SystemExit(f"golem: task is running: {task_id}")
        dependent_ids = sorted(
            dependent_id
            for dependent_id, dependent in tasks.items()
            if isinstance(dependent, dict)
            and cast("dict[str, Any]", dependent).get("after") == task_id
        )
        if dependent_ids:
            raise SystemExit(f"golem: task has dependents: {task_id}: {', '.join(dependent_ids)}")
        remaining_tasks = {id_: record for id_, record in tasks.items() if id_ != task_id}
        recorded = recorded_worktree(tasks, task)
        worktree = resolved_path(recorded[0]) if recorded is not None else None
        is_removed = False
        if is_removing_worktree:
            if worktree is None:
                raise SystemExit(f"golem: task has no worktree: {task_id}")
            resource = worktree_resource(worktrees, worktree)
            if resource is None or resource.get("removed_at") is None:
                is_removed, reasons = remove_worktree_resource(remaining_tasks, worktrees, worktree)
                if not is_removed:
                    print(
                        f"golem: retained worktree: {worktree}: {'; '.join(reasons)}",
                        file=sys.stderr,
                    )
                    return 1
        del tasks[task_id]
        for path, resource in worktrees.items():
            if (
                task_uses_worktree(tasks, task, path)
                and resource.get("owner") == WORKTREE_OWNER_GOLEM
                and resource.get("removed_at") is None
                and not worktree_task_ids(tasks, path)
            ):
                resource.setdefault("released_at", datetime.now(UTC).isoformat())
        print(f"golem: deleted task: {task_id}")
        print(f"golem: session: {task['session_id']}")
        if worktree is not None:
            print(f"golem: worktree: {worktree}")
        if is_removed:
            print(f"golem: removed worktree: {worktree}")
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
    when = planned_execution_time(is_now=is_now, at_text=at_text) or datetime.now(UTC).astimezone()

    with locked_ledger() as (tasks, worktrees):
        task = retryable_task(tasks, task_id)
        reject_missing_worktree(task, worktrees)
        record = new_task_record(
            cast("str", task["cwd"]),
            cast("str", task["prompt"]),
            tuple(cast("list[str]", task["agent_args"])),
            when,
            task.get("after"),
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
    scheduled_task(load_tasks(), task_id)
    require_launch_agent()
    when = planned_execution_time(is_now=is_now, at_text=at_text) or datetime.now(UTC).astimezone()

    with locked_tasks() as tasks:
        task = scheduled_task(tasks, task_id)
        task["due_at"] = when.isoformat()
    return wake_launch_agent_and_report(task_id, when)


def planned_execution_time(*, is_now: bool, at_text: str | None) -> datetime | None:
    if is_now:
        return None
    when = next_reset_time() if at_text is None else scheduled_time(at_text)
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


def run_task_from_arguments(arguments: argparse.Namespace) -> int:
    require_launch_agent()
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8") if arguments.file else arguments.prompt
    )
    task_args = tuple(arguments.agent_args)
    if arguments.after is not None:
        dependent_record = new_task_record(
            str(Path.cwd()), prompt_text, task_args, None, arguments.after
        )
        if reserve_dependent_task(arguments.id, dependent_record, arguments.after):
            print(f"golem: {arguments.id} will run after {arguments.after}")
            return 0
    when = planned_execution_time(is_now=arguments.now, at_text=arguments.at)
    return schedule_new_task(arguments.id, prompt_text, task_args, when, arguments.after)


def add_worktree_commands(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    worktree_command = add_parser("worktree")
    worktree_commands = worktree_command.add_subparsers(dest="worktree_command", required=True)
    set_command_handler(worktree_commands.add_parser("list"), lambda _: list_worktrees())
    auto_remove_worktree_command = worktree_commands.add_parser("auto-remove")
    auto_remove_group = auto_remove_worktree_command.add_mutually_exclusive_group(required=True)
    auto_remove_group.add_argument("--after", type=int, metavar="DAYS")
    auto_remove_group.add_argument("--off", action="store_true")
    set_command_handler(auto_remove_worktree_command, set_worktree_auto_remove_from_arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="golem")
    commands = parser.add_subparsers(dest="command", required=True)

    run_command = commands.add_parser(
        "run",
        description=SCHEDULE_DESCRIPTION,
    )
    run_command.add_argument("id")
    prompt_group = run_command.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("prompt", nargs="?")
    prompt_group.add_argument("--file")
    run_schedule_group = run_command.add_mutually_exclusive_group()
    add_scheduling_arguments(run_schedule_group.add_argument)
    run_schedule_group.add_argument("--after", metavar="PARENT", help="run after PARENT succeeds")
    set_command_handler(run_command, run_task_from_arguments)

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
    delete_command.add_argument("id")
    delete_command.add_argument("--remove-worktree", action="store_true")
    set_command_handler(
        delete_command,
        lambda arguments: delete_task(arguments.id, is_removing_worktree=arguments.remove_worktree),
    )
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
