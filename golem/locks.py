import fcntl
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import io

from golem import ledger


def run_lock_directory() -> Path:
    lock_directory = ledger.ledger_path().parent / "running"
    lock_directory.mkdir(exist_ok=True)
    return lock_directory


def run_lock_path(session_id: str) -> Path:
    return run_lock_directory() / f"{session_id}.lock"


def remove_orphan_run_locks() -> None:
    for lock_path in run_lock_directory().glob("*.lock"):
        run_lock_file = acquire_run_lock(lock_path.stem)
        if run_lock_file is not None:
            release_run_lock(run_lock_file, lock_path.stem)


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
    lock_directory = ledger.ledger_path().parent / "directories"
    lock_directory.mkdir(exist_ok=True)
    resolved_directory = str(Path(directory).resolve())
    lock_name = hashlib.sha256(resolved_directory.encode()).hexdigest()
    return lock_directory / f"{lock_name}.lock"


def acquire_task_directory_lock(task: ledger.Task) -> io.TextIOWrapper | None:
    return acquire_lock(directory_lock_path(ledger.directory_to_lock(task, task.get("worktree"))))


def is_run_lock_free(session_id: str) -> bool:
    run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return False
    release_run_lock(run_lock_file, session_id)
    return True


def is_directory_busy(directory: str) -> bool:
    directory_lock_file = acquire_lock(directory_lock_path(directory))
    if directory_lock_file is None:
        return True
    release_lock(directory_lock_file)
    return False
