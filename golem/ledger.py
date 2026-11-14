import argparse
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast, get_args

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_FINISHED = "finished"
STATUS_CANCELLED = "cancelled"
NOT_STARTED_STATUSES = {STATUS_SCHEDULED, STATUS_WAITING, STATUS_CANCELLED}
BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
BACKEND_T3 = "t3"
LIST_STATUS_ORDER = (
    STATUS_BLOCKED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    STATUS_WAITING,
    STATUS_FINISHED,
    STATUS_CANCELLED,
)
ALL_STATUSES: set[str] = set(LIST_STATUS_ORDER)
CLEAN_EXIT_STATUSES = {STATUS_BLOCKED, STATUS_FINISHED}
UNFINISHED_STATUSES = {STATUS_SCHEDULED, STATUS_RUNNING, STATUS_WAITING, STATUS_BLOCKED}
CHAIN_DONE_AGE_DAYS = 7
ThreadState = Literal["unsettled", "settled", "archived", "deleted"]
THREAD_STATE_UNSETTLED: ThreadState = "unsettled"
THREAD_STATE_SETTLED: ThreadState = "settled"
THREAD_STATE_ARCHIVED: ThreadState = "archived"
THREAD_STATE_DELETED: ThreadState = "deleted"
PullRequestState = Literal["open", "merged", "closed"]
PULL_REQUEST_STATES: tuple[PullRequestState, ...] = get_args(PullRequestState)
PULL_REQUEST_STATE_OPEN: PullRequestState = "open"
PULL_REQUEST_STATE_MERGED: PullRequestState = "merged"
PULL_REQUEST_STATE_CLOSED: PullRequestState = "closed"


class RepositoryBranch(TypedDict):
    repository: str
    branch: str


class PullRequest(TypedDict):
    url: str
    state: PullRequestState


class Task(TypedDict):
    cwd: str
    prompt: str
    agent_args: list[str]
    status: str
    session_id: str
    backend: str
    environment: NotRequired[dict[str, str]]
    after: NotRequired[str]
    due_at: NotRequired[str]
    worktree: NotRequired[str]
    worktree_pending: NotRequired[bool]
    thread_id: NotRequired[str]
    t3_code_thread_id: NotRequired[str]
    t3_code_thread_state: NotRequired[ThreadState]
    native_session_id: NotRequired[str]
    result: NotRequired[str]
    returncode: NotRequired[int]
    usage_limit_resume_count: NotRequired[int]
    started_at: NotRequired[str]
    finished_at: NotRequired[str]
    opened_at: NotRequired[str]
    closed_at: NotRequired[str]
    auto_pickup: NotRequired[bool]
    auto_pickup_client: NotRequired[str]
    no_worktree: NotRequired[bool]
    gate: NotRequired[str]
    gate_stop_reason: NotRequired[str]
    gate_released: NotRequired[bool]
    setup: NotRequired[str]
    setup_complete: NotRequired[bool]
    setup_failure_reason: NotRequired[str]
    setup_directory: NotRequired[str]
    setup_busy_retry_count: NotRequired[int]
    repository_branches: NotRequired[list[RepositoryBranch]]
    pull_requests: NotRequired[list[PullRequest]]
    account: NotRequired[int]


class WorktreeResource(TypedDict):
    owner: str
    session_id: str
    created_at: str
    repository: NotRequired[str]
    released_at: NotRequired[str]
    removed_at: NotRequired[str]
    removal_error: NotRequired[str]


class Settings(TypedDict):
    worktree_retention_days: NotRequired[int]
    agent_environment: NotRequired[dict[str, str]]


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
    client: str | None
    yes: bool
    off: bool
    action: str
    session_id: str
    remove_worktree: bool
    then: list[list[str]]
    env: list[str]
    auto_pickup: bool
    no_worktree: bool
    gate: str | None
    setup: str | None
    force: bool
    event: str
    ids: list[str]
    chain: bool
    directory: str | None
    account: str | None
    account_value: str


type Tasks = dict[str, Task]
type Worktrees = dict[str, WorktreeResource]
type JsonObject = dict[str, object]


def xdg_golem_directory(variable: str, fallback: Path) -> Path:
    base_directory = Path(os.environ.get(variable, ""))
    return (base_directory if base_directory.is_absolute() else fallback) / "golem"


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
    tasks = cast("dict[str, JsonObject]", load_json(ledger_path()))
    for task in tasks.values():
        # Older ledgers store gate_stop_reason under its former name, gate_reason.
        if "gate_reason" in task:
            task["gate_stop_reason"] = task.pop("gate_reason")
    return cast("Tasks", tasks)


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


def remove_interrupted_ledger_writes() -> None:
    task_ledger_path = ledger_path()
    for temporary_path in task_ledger_path.parent.glob(
        f"{ledger_temporary_prefix(task_ledger_path)}*"
    ):
        temporary_path.unlink(missing_ok=True)


def task_directory(task: Task) -> str:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        return worktree
    return task["cwd"]


def directory_to_lock(task: Task, worktree: str | None) -> str:
    return worktree if worktree is not None else task["cwd"]


def existing_task(tasks: Tasks, task_id: str) -> Task:
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    return tasks[task_id]


def are_pull_requests_finished(pull_requests: list[PullRequest]) -> bool:
    return bool(pull_requests) and all(
        pull_request["state"] != PULL_REQUEST_STATE_OPEN for pull_request in pull_requests
    )


def has_finished_pull_requests(task: Task) -> bool:
    return are_pull_requests_finished(task.get("pull_requests", []))


def has_marked_descendant(tasks: Tasks, task_id: str, field: str) -> bool:
    return field in tasks[task_id] or any(
        dependent.get("after") == task_id and has_marked_descendant(tasks, dependent_id, field)
        for dependent_id, dependent in tasks.items()
    )


def root_task_id(tasks: Tasks, task_id: str) -> str:
    while (parent_id := tasks[task_id].get("after")) is not None and parent_id in tasks:
        task_id = parent_id
    return task_id


def is_counted_opened(tasks: Tasks, task_id: str) -> bool:
    return has_marked_descendant(tasks, task_id, "opened_at") or has_marked_descendant(
        tasks, root_task_id(tasks, task_id), "closed_at"
    )
