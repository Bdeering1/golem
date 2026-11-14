import os
import subprocess
import sys
from datetime import UTC, datetime

from golem import accounts, chains, clients, ledger, locks, t3_code, task_directories
from golem.backends import registry


def open_app_and_record_opening(
    task_id: str, command: list[str], t3_code_thread_id: str | None = None
) -> int:
    returncode = subprocess.run(command, check=False).returncode
    if returncode != 0:
        return returncode
    with ledger.locked_tasks() as tasks:
        current_task = tasks.get(task_id)
        if current_task is not None:
            if t3_code_thread_id is not None:
                current_task["t3_code_thread_id"] = t3_code_thread_id
            current_task["opened_at"] = datetime.now(UTC).isoformat()
    return 0


def pickup(
    task_id: str, *agent_args: str, client_name: str | None = None, is_review_required: bool = False
) -> int:
    preflight_task = ledger.load_tasks().get(task_id)
    if preflight_task is None:
        raise SystemExit(f"golem: task not found: {task_id}")
    client = clients.task_client(task_id, preflight_task, client_name)
    client.validate_arguments(list(agent_args))
    environment = (
        client.environment(
            accounts.account_environment(os.environ.copy(), preflight_task.get("account"))
        )
        if client.is_terminal
        else {}
    )
    with ledger.locked_ledger() as (tasks, worktrees):
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if is_review_required and not chains.is_ready_to_open(tasks, task_id):
            return 0
        if task.get("status") in ledger.NOT_STARTED_STATUSES:
            raise SystemExit(f"golem: task has not started: {task_id}")
        task_directories.reject_missing_worktree(task, worktrees)
        directory = ledger.directory_to_lock(task, task.get("worktree"))
        directory_lock_file = locks.acquire_lock(locks.directory_lock_path(directory))
        if directory_lock_file is None and task.get("status") == ledger.STATUS_RUNNING:
            raise SystemExit(
                f"golem: task is running: {task_id}; use golem watch {task_id} to follow it"
            )
        if directory_lock_file is None:
            raise SystemExit(f"golem: directory is busy: {directory}")
        backend = registry.task_backend(task)
        conversation_id = backend.conversation_id(task)
        if conversation_id is None:
            locks.release_lock(directory_lock_file)
            raise SystemExit(f"golem: task has no {backend.NAME} conversation: {task_id}")
        task_directories.clear_worktree_release(tasks, worktrees, task)
        if client.is_terminal:
            task["opened_at"] = datetime.now(UTC).isoformat()
        cwd = ledger.task_directory(task)
    if not client.is_terminal:
        locks.release_lock(directory_lock_file)
        if client.name == clients.CLAUDE_DESKTOP_CLIENT and "account" in task:
            print(
                f"golem: {task_id} continues on the Claude desktop app's sign-in", file=sys.stderr
            )
        client.prepare(task, conversation_id)
        return open_app_and_record_opening(
            task_id,
            client.command(task, conversation_id, []),
            t3_code.task_thread_id(task, conversation_id) if client.has_t3_code_thread else None,
        )
    try:
        return subprocess.run(
            client.command(task, conversation_id, list(agent_args)),
            cwd=cwd,
            env=environment,
            pass_fds=(directory_lock_file.fileno(),),
            check=False,
        ).returncode
    finally:
        locks.release_lock(directory_lock_file)


def default_client_name(task_id: str, task: ledger.Task, default_clients: dict[str, str]) -> str:
    client_name = default_clients.get(task["backend"])
    if client_name is None:
        backend = registry.task_backend(task)
        raise SystemExit(f"golem: no default app client for {backend.NAME} tasks: {task_id}")
    return client_name


def pickup_all(client_name: str | None, default_clients: dict[str, str]) -> int:
    tasks = ledger.load_tasks()
    task_ids = [task_id for task_id in tasks if chains.is_ready_to_open(tasks, task_id)]
    if not task_ids:
        print("golem: no tasks to open")
        return 0

    has_failure = False
    for task_id in task_ids:
        try:
            task_client_name = client_name or default_client_name(
                task_id, tasks[task_id], default_clients
            )
            has_failure |= (
                pickup(task_id, client_name=task_client_name, is_review_required=True) != 0
            )
        except SystemExit as error:
            print(error, file=sys.stderr)
            has_failure = True
    return int(has_failure)


def auto_pickup_task(task_id: str) -> None:
    tasks = ledger.load_tasks()
    task = tasks.get(task_id)
    if task is None:
        return
    if task.get("status") == ledger.STATUS_FAILED:
        auto_pickup_task(task.get("after", ""))
        return
    client_name = clients.task_auto_pickup_client(task)
    if client_name is None or not chains.is_ready_to_open(tasks, task_id):
        return
    try:
        returncode = pickup(task_id, client_name=client_name, is_review_required=True)
    except SystemExit as error:
        print(error, file=sys.stderr)
        return
    if returncode != 0:
        print(f"golem: could not open {task_id} in {client_name}", file=sys.stderr)


def close(task_id: str) -> int:
    with ledger.locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if not chains.is_chain_finished(tasks, chains.chain_task_ids(tasks, task_id)):
            raise SystemExit(f"golem: chain is not finished: {task_id}")
        task.setdefault("closed_at", datetime.now(UTC).isoformat())
    return 0
