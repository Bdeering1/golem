import os
import subprocess
import sys
from typing import cast

from golem import chains, ledger, locks, task_directories
from golem.backends import codex


def open_app_session_and_mark_done(task_id: str, session_id: str) -> int:
    returncode = subprocess.run(
        ["open", f"claude://resume?session={session_id}"], check=False
    ).returncode
    if returncode != 0:
        return returncode
    with ledger.locked_tasks() as tasks:
        current_task = tasks.get(task_id)
        if current_task is not None:
            chains.mark_task_done(tasks, current_task)
    return 0


def pickup(task_id: str, *agent_args: str, is_app: bool, is_review_required: bool = False) -> int:
    preflight_task = ledger.load_tasks().get(task_id)
    is_codex = (
        preflight_task is not None and ledger.task_backend(preflight_task) == ledger.BACKEND_CODEX
    )
    if is_codex and is_app:
        raise SystemExit(f"golem: pickup --app does not support Codex tasks: {task_id}")
    if is_codex:
        codex.reject_unsupported_codex_arguments(list(agent_args), is_run=False)
        codex.require_codex_subscription(os.environ.copy())
    with ledger.locked_ledger() as (tasks, worktrees):
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if is_review_required and task.get("status") != ledger.STATUS_NEEDS_REVIEW:
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
        thread_id = task.get("thread_id")
        if is_codex and not isinstance(thread_id, str):
            locks.release_lock(directory_lock_file)
            raise SystemExit(f"golem: task has no Codex conversation: {task_id}")
        task_directories.clear_worktree_release(tasks, worktrees, task)
        if not is_app:
            chains.mark_task_done(tasks, task)
        cwd = ledger.task_directory(task)
        session_id = task["session_id"]
    if is_app:
        locks.release_lock(directory_lock_file)
        return open_app_session_and_mark_done(task_id, session_id)
    try:
        if is_codex:
            return subprocess.run(
                [
                    "codex",
                    "resume",
                    cast("str", thread_id),
                    *agent_args,
                    *codex.CODEX_PROVIDER_ARGUMENTS,
                ],
                cwd=cwd,
                env=codex.codex_environment(os.environ.copy()),
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
        locks.release_lock(directory_lock_file)


def is_ready_for_review(tasks: ledger.Tasks, task_id: str) -> bool:
    task = tasks[task_id]
    return task.get("status") == ledger.STATUS_NEEDS_REVIEW and (
        "gate_stop_reason" in task or not chains.has_unreviewed_dependent(tasks, task_id)
    )


def pickup_all() -> int:
    tasks = ledger.load_tasks()
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
    tasks = ledger.load_tasks()
    task = tasks.get(task_id)
    if task is None:
        return
    if task.get("status") == ledger.STATUS_FAILED:
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
    with ledger.locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if task.get("status") != ledger.STATUS_NEEDS_REVIEW:
            raise SystemExit(f"golem: task does not need review: {task_id}")
        task["status"] = ledger.STATUS_DONE
    return 0
