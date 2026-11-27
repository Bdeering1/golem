import json
import sys
from datetime import UTC, datetime
from typing import cast

from golem import chains, ledger, locks, notifications, spawn, thread_settling
from golem.backends import claude_hooks, registry

GATE_TIMEOUT_SECONDS = 300
HOOK_GATE_TIMEOUT_SECONDS = 20


def gate_input_json(task_id: str, task: ledger.Task) -> str:
    transcript_path = next(iter(registry.task_backend(task).transcript_paths(task)), None)
    return json.dumps(
        {
            "task_id": task_id,
            "label": task.get("label"),
            "session_id": task["session_id"],
            "directory": ledger.task_directory(task),
            "started_at": task.get("started_at"),
            "exit_code": task.get("returncode"),
            "result": task.get("result"),
            "transcript_path": None if transcript_path is None else str(transcript_path),
        }
    )


def gate_stop_reason(
    task_id: str, task: ledger.Task, timeout_seconds: int | None = None
) -> str | None:
    gate = task.get("gate")
    if gate is None:
        return None
    if timeout_seconds is None:
        timeout_seconds = GATE_TIMEOUT_SECONDS
    stop_reason, _ = spawn.shell_command_outcome(
        gate, task_id, "gate", task, gate_input_json(task_id, task), timeout_seconds
    )
    return stop_reason


def run_pending_gate(task_id: str) -> None:
    tasks = ledger.load_tasks()
    if not chains.is_gate_pending(tasks, task_id):
        return
    stop_reason = gate_stop_reason(task_id, tasks[task_id])
    if stop_reason is None:
        return
    with ledger.locked_tasks() as locked_tasks_by_id:
        current = locked_tasks_by_id.get(task_id)
        if current is None:
            return
        if current.get("status") != ledger.STATUS_FINISHED:
            return
        current["status"] = ledger.STATUS_BLOCKED
        current["gate_stop_reason"] = stop_reason
        directory = ledger.task_directory(current)
        stopped_task = current.copy()
    claude_hooks.sync_hooks(directory)
    notifications.notify_task(task_id, stopped_task, notifications.OUTCOME_BLOCKED)


def continue_task(task_id: str, *, is_forced: bool) -> int:
    task = ledger.existing_task(ledger.load_tasks(), task_id)
    if "gate_stop_reason" not in task or task["status"] != ledger.STATUS_BLOCKED:
        raise SystemExit(f"golem: task is not stopped at a gate: {task_id}")
    if not is_forced:
        directory_lock_file = locks.acquire_task_directory_lock(task)
        if directory_lock_file is None:
            raise SystemExit(f"golem: directory is busy: {task_id}")
        try:
            stop_reason = gate_stop_reason(task_id, task)
        finally:
            locks.release_lock(directory_lock_file)
        if stop_reason is not None:
            with ledger.locked_tasks() as tasks:
                stopped_task = ledger.existing_task(tasks, task_id)
                if "gate_stop_reason" in stopped_task:
                    stopped_task["gate_stop_reason"] = stop_reason
            print(stop_reason, file=sys.stderr)
            return 1
    if not release_gate_stop(task_id):
        raise SystemExit(f"golem: task is not stopped at a gate: {task_id}")
    return 0


def release_gate_stop(task_id: str) -> bool:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None:
            return False
        if "gate_stop_reason" not in task or task["status"] != ledger.STATUS_BLOCKED:
            return False
        del task["gate_stop_reason"]
        task["status"] = ledger.STATUS_FINISHED
        task["finished_at"] = datetime.now(UTC).isoformat()
        task["gate_released"] = True
        thread_settling.mark_parent_pending(tasks, task)
    spawn.start_task_executor(task_id, task)
    return True


def read_hook_session_id() -> str | None:
    try:
        payload = cast("object", json.load(sys.stdin))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    session_id = cast("ledger.JsonObject", payload).get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def session_task_id(tasks: ledger.Tasks, session_id: str | None) -> str | None:
    if session_id is None:
        return None
    return next(
        (
            task_id
            for task_id, task in tasks.items()
            if session_id in {task.get("session_id"), task.get("native_session_id")}
        ),
        None,
    )


def hook_stop() -> int:
    tasks = ledger.load_tasks()
    task_id = session_task_id(tasks, read_hook_session_id())
    if task_id is None:
        return 0
    task = tasks[task_id]
    next_ids = chains.waiting_dependent_ids(tasks, task_id)
    if "gate_stop_reason" not in task or not next_ids:
        return 0
    directory_lock_file = locks.acquire_task_directory_lock(task)
    if directory_lock_file is None:
        return 0
    try:
        stop_reason = gate_stop_reason(task_id, task, HOOK_GATE_TIMEOUT_SECONDS)
    finally:
        locks.release_lock(directory_lock_file)
    if stop_reason is not None:
        return 0
    if not release_gate_stop(task_id):
        return 0
    print(json.dumps({"systemMessage": f"golem: continuing chain with {next_ids[0]}"}))
    return 0


def hook_pre_tool_use() -> int:
    tasks = ledger.load_tasks()
    task_id = session_task_id(tasks, read_hook_session_id())
    if task_id is None:
        return 0
    running_ids = chains.descendant_ids_with_status(tasks, task_id, {ledger.STATUS_RUNNING})
    if not running_ids:
        return 0
    reason = f"golem is running {running_ids[0]}; wait for it to finish"
    output = {
        "hookSpecificOutput": {
            "hookEventName": claude_hooks.HOOK_EVENT_PRE_TOOL_USE,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))
    return 0


def run_pending_gate_under_directory_lock(task_id: str) -> bool:
    tasks = ledger.load_tasks()
    if not chains.is_gate_pending(tasks, task_id):
        return True
    directory_lock_file = locks.acquire_task_directory_lock(tasks[task_id])
    if directory_lock_file is None:
        return False
    try:
        run_pending_gate(task_id)
    finally:
        locks.release_lock(directory_lock_file)
    return True
