import os
import signal
import time
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Generator

from golem import ledger, locks, t3_code

AGENT_STOP_SECONDS = 10
RUN_LOCK_WAIT_SECONDS = 10
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
        process.kill()
        return
    stop_command_process_group(process.pid)


def wait_for_run_lock(task_id: str, session_id: str) -> None:
    deadline = time.monotonic() + RUN_LOCK_WAIT_SECONDS
    while not locks.is_run_lock_free(session_id):
        if time.monotonic() >= deadline:
            raise SystemExit(f"golem: task cancelled, but it is still stopping: {task_id}")
        time.sleep(STOP_POLL_SECONDS)


def stop_task_processes(task_id: str, task: ledger.Task, previous_status: str) -> None:
    if previous_status not in {ledger.STATUS_RUNNING, ledger.STATUS_FINISHED}:
        return
    thread_id = task.get("t3_code_thread_id")
    if (
        task["backend"] == ledger.BACKEND_T3
        and previous_status == ledger.STATUS_RUNNING
        and thread_id is not None
    ):
        t3_code.stop_thread(thread_id)
    # A dead executor leaves its recorded PIDs behind, and another process may now hold them.
    if locks.is_run_lock_free(task["session_id"]):
        return
    command_process_group = task.get("command_process_group")
    if command_process_group is not None:
        stop_command_process_group(command_process_group)
    agent_pid = task.get("agent_pid")
    if agent_pid is not None:
        stop_agent(agent_pid)
    wait_for_run_lock(task_id, task["session_id"])


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
