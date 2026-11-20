import os
import signal
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Generator

from golem import ledger

AGENT_STOP_SECONDS = 10
STOP_POLL_SECONDS = 0.1
type ProcessField = Literal["agent_pid", "command_process_group"]
FIELD_AGENT_PID: ProcessField = "agent_pid"
FIELD_COMMAND_PROCESS_GROUP: ProcessField = "command_process_group"


def stop_command_process_group(process_group: int) -> None:
    with suppress(OSError):
        os.killpg(process_group, signal.SIGKILL)


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_agent(pid: int) -> None:
    with suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + AGENT_STOP_SECONDS
    while is_process_alive(pid):
        if time.monotonic() >= deadline:
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            return
        time.sleep(STOP_POLL_SECONDS)


def stop_started_process(field: ProcessField, process: subprocess.Popen[str]) -> None:
    if field == FIELD_AGENT_PID:
        process.terminate()
        return
    stop_command_process_group(process.pid)


@contextmanager
def recorded_process(
    task_id: str, session_id: str, field: ProcessField, process: subprocess.Popen[str]
) -> Generator[None]:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        is_recorded = False
        if task is not None and task["session_id"] == session_id:
            if task["status"] == ledger.STATUS_CANCELLED:
                stop_started_process(field, process)
            else:
                task[field] = process.pid
                is_recorded = True
    try:
        yield
    finally:
        if is_recorded:
            with ledger.locked_tasks() as tasks:
                task = tasks.get(task_id)
                if task is not None and task.get(field) == process.pid:
                    del task[field]
