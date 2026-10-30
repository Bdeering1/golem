import os
import sys
import time
from contextlib import contextmanager, suppress
from datetime import datetime
from typing import TYPE_CHECKING, BinaryIO

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

from golem import display, ledger
from golem.backends import common

AGENT_LOG_MAX_BYTES = 1024 * 1024
WATCH_POLL_SECONDS = 0.2


def progress_path(session_id: str) -> Path:
    return ledger.state_directory() / "progress" / f"{session_id}.log"


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


def is_usage_limit_pause(task: ledger.Task) -> bool:
    return task.get("status") == ledger.STATUS_SCHEDULED and common.is_usage_limit_resume(task)


def task_end_line(task_id: str, task: ledger.Task) -> str:
    due_at = task.get("due_at")
    if due_at is not None and is_usage_limit_pause(task):
        resume_time = display.display_due_at(due_at, datetime.now().astimezone().date())
        return f"golem: task paused at the usage limit, resumes {resume_time}: {task_id}"
    return f"golem: task {task['status']}: {task_id}"


def print_new_progress(progress_file: BinaryIO) -> None:
    new_bytes = progress_file.read()
    complete_lines = new_bytes[: new_bytes.rfind(b"\n") + 1]
    progress_file.seek(len(complete_lines) - len(new_bytes), os.SEEK_CUR)
    sys.stdout.write(complete_lines.decode("utf-8"))
    sys.stdout.flush()


def follow_progress_until_stopped(task_id: str, path: Path, progress_file: BinaryIO) -> ledger.Task:
    try:
        while True:
            task = ledger.existing_task(ledger.load_tasks(), task_id)
            print_new_progress(progress_file)
            with suppress(FileNotFoundError):
                while path.stat().st_ino != os.fstat(progress_file.fileno()).st_ino:
                    print_new_progress(progress_file)
                    rotated_file = progress_file
                    progress_file = path.open("rb")
                    rotated_file.close()
                    print_new_progress(progress_file)
            if task.get("status") != ledger.STATUS_RUNNING:
                return task
            time.sleep(WATCH_POLL_SECONDS)
    finally:
        progress_file.close()


def watch_task(task_id: str) -> int:
    task = ledger.existing_task(ledger.load_tasks(), task_id)
    if task.get("status") in ledger.NOT_STARTED_STATUSES and not is_usage_limit_pause(task):
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
