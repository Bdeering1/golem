import os
import re
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import io

from golem import (
    accounts,
    chains,
    gates,
    ledger,
    locks,
    notifications,
    progress_log,
    review,
    scheduling,
    task_setup,
    thread_settling,
)
from golem.backends import claude_hooks, common, registry
from golem.worktrees import branch_pull_requests, git, task_directories

MAX_USAGE_LIMIT_RESUMES = 2
BACKGROUND_WORK_STOPPED_PATTERN = re.compile(
    r"^Background tasks still running .*; stopping them\..*$", re.MULTILINE
)


def record_worktree(task_id: str, worktree: str) -> None:
    with ledger.locked_ledger() as (tasks, worktrees):
        tasks[task_id]["worktree"] = worktree
        tasks[task_id].pop("worktree_pending", None)
        task = tasks[task_id]
        task_directories.record_worktree_resource(worktrees, worktree, task["session_id"])


def clear_recorded_worktree_pending(task_id: str) -> None:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is not None:
            task_directories.clear_worktree_pending_if_created(task)


def mark_task_waiting_for_reset(task_id: str, result: str, reset_time: datetime | None) -> bool:
    try:
        task = ledger.load_tasks().get(task_id)
        if task is None:
            return False
        when = (
            scheduling.rounded_execution_time(reset_time)
            if reset_time is not None
            else scheduling.planned_execution_time(
                registry.task_usage_backend(task),
                accounts.account_environment(os.environ.copy(), task.get("account")),
                is_now=False,
                at_text=None,
            )
        )
    except SystemExit:
        return False
    if when is None:
        return False
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None:
            return False
        task_record = task
        if task_record.get("status") != ledger.STATUS_RUNNING:
            return False
        task_record["result"] = result
        task_record["status"] = ledger.STATUS_SCHEDULED
        task_record["due_at"] = when.isoformat()
        task_record["usage_limit_resume_count"] = task_record.get("usage_limit_resume_count", 0) + 1
        return True


def finish_task(
    task_id: str,
    result: str | None,
    returncode: int,
    repository_branches: list[ledger.RepositoryBranch] | None,
    *,
    is_usage_limit: bool,
    is_background_work_stopped: bool,
    stop_reason: str | None = None,
) -> None:
    with ledger.locked_tasks() as tasks:
        stored_task = tasks.get(task_id)
        if stored_task is None:
            return
        task_record = stored_task
        if task_record.get("status") != ledger.STATUS_RUNNING:
            return
        if result is not None:
            task_record["result"] = result
        if repository_branches is not None:
            task_record["repository_branches"] = repository_branches
        is_succeeded = returncode == 0 and not is_usage_limit and not is_background_work_stopped
        task_record["returncode"] = returncode
        if stop_reason is not None:
            task_record["status"] = ledger.STATUS_BLOCKED
            task_record["gate_stop_reason"] = stop_reason
        elif is_succeeded:
            task_record["status"] = ledger.STATUS_FINISHED
            task_record["finished_at"] = datetime.now(UTC).isoformat()
        else:
            task_record["status"] = ledger.STATUS_FAILED


def claim_task(
    tasks: ledger.Tasks,
    worktrees: ledger.Worktrees,
    task_id: str,
    session_id: str,
    expected_status: str,
    failed_claims: list[tuple[str, ledger.Task]],
) -> tuple[ledger.Task, io.TextIOWrapper, io.TextIOWrapper] | None:
    task = tasks.get(task_id)
    if task is None or task["status"] != expected_status or task["session_id"] != session_id:
        return None
    due_at = task.get("due_at")
    if (
        expected_status == ledger.STATUS_SCHEDULED
        and due_at is not None
        and datetime.fromisoformat(due_at) > datetime.now(UTC)
    ):
        return None
    run_lock_file = locks.acquire_run_lock(session_id)
    if run_lock_file is None:
        return None
    recorded = task_directories.recorded_worktree(tasks, task)
    worktree, is_worktree_pending = task_directories.resolved_worktree(tasks, task)
    if missing_worktree := task_directories.worktree_missing_message(
        worktree, worktrees, is_worktree_pending=is_worktree_pending
    ):
        task_directories.record_resolved_worktree(
            task, worktree, is_worktree_pending=is_worktree_pending
        )
        task["status"] = ledger.STATUS_FAILED
        task["result"] = missing_worktree
        failed_claims.append((task_id, task.copy()))
        locks.release_run_lock(run_lock_file, session_id)
        return None
    directory_lock_file = locks.acquire_lock(
        locks.directory_lock_path(ledger.directory_to_lock(task, worktree))
    )
    if directory_lock_file is None:
        locks.release_run_lock(run_lock_file, session_id)
        return None
    task_directories.record_resolved_worktree(
        task, worktree, is_worktree_pending=is_worktree_pending
    )
    if worktree is not None and (recorded is None or recorded[0] != worktree):
        repository = git.git_common_directory(task["cwd"])
        if repository is not None:
            is_golem_owned = (
                task.get("worktree_pending") is True
                and task_directories.named_codex_worktree_directory_name(task) is None
            )
            task_directories.record_worktree_resource(
                worktrees,
                worktree,
                session_id,
                owner=task_directories.WORKTREE_OWNER_GOLEM
                if is_golem_owned
                else task_directories.WORKTREE_OWNER_EXTERNAL,
                repository=repository,
            )
    task_directories.clear_worktree_release(tasks, worktrees, task)
    progress_log.progress_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    progress_log.progress_path(session_id).touch()
    task["status"] = ledger.STATUS_RUNNING
    if not common.is_usage_limit_resume(task):
        task["started_at"] = datetime.now(UTC).isoformat()
    return task.copy(), run_lock_file, directory_lock_file


def claim_next_dependent(
    task_id: str, parent_session_id: str
) -> tuple[str, ledger.Task, io.TextIOWrapper, io.TextIOWrapper] | None:
    failed_claims: list[tuple[str, ledger.Task]] = []
    claimed_task: tuple[str, ledger.Task, io.TextIOWrapper, io.TextIOWrapper] | None = None
    with ledger.locked_ledger() as (tasks, worktrees):
        parent_record = tasks.get(task_id)
        if parent_record is None:
            return None
        parent = parent_record
        if parent.get("session_id") != parent_session_id or not chains.can_start_dependents(parent):
            return None
        for dependent_id in chains.waiting_dependent_ids(tasks, task_id):
            dependent_session_id = tasks[dependent_id]["session_id"]
            claim = claim_task(
                tasks,
                worktrees,
                dependent_id,
                dependent_session_id,
                ledger.STATUS_WAITING,
                failed_claims,
            )
            if claim is not None:
                claimed_task = dependent_id, *claim
                break
            if tasks[dependent_id]["status"] == ledger.STATUS_WAITING:
                break
    notifications.notify_failed_tasks(failed_claims)
    return claimed_task


def drain_dependents(task_id: str, session_id: str) -> None:
    while next_dependent := claim_next_dependent(task_id, session_id):
        execute_claimed_task(*next_dependent)
    review.auto_pickup_task(task_id)
    drained_task = ledger.load_tasks().get(task_id)
    if drained_task is not None:
        claude_hooks.sync_hooks(ledger.task_directory(drained_task))


def execute_task(task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]) -> int:
    try:
        outcome = registry.task_backend(task).run(task_id, task, lock_file_descriptors)
    except OSError as error:
        print(f"golem: could not execute task: {error}", file=sys.stderr)
        outcome = common.RunOutcome(127, None, is_usage_limit=False)
    if outcome.working_directory is not None and outcome.working_directory != task["cwd"]:
        record_worktree(task_id, outcome.working_directory)
    clear_recorded_worktree_pending(task_id)
    usage_limit_resume_count = task.get("usage_limit_resume_count", 0)
    if (
        outcome.is_usage_limit
        and usage_limit_resume_count < MAX_USAGE_LIMIT_RESUMES
        and outcome.result is not None
        and mark_task_waiting_for_reset(task_id, outcome.result, outcome.reset_time)
    ):
        return 0
    stored_result = outcome.result
    stop_line_match = BACKGROUND_WORK_STOPPED_PATTERN.search(outcome.stderr)
    if stop_line_match is not None:
        stop_line = stop_line_match.group()
        stored_result = stop_line if outcome.result is None else f"{outcome.result}\n\n{stop_line}"
    finish_task(
        task_id,
        stored_result,
        outcome.returncode,
        branch_pull_requests.task_branches(task_id),
        is_usage_limit=outcome.is_usage_limit,
        is_background_work_stopped=stop_line_match is not None,
        stop_reason=outcome.stop_reason,
    )
    tasks = ledger.load_tasks()
    stored_task = tasks.get(task_id)
    if stored_task is not None:
        if stored_task.get("status") == ledger.STATUS_FAILED:
            notifications.notify_task(task_id, stored_task, notifications.OUTCOME_FAILED)
        elif stored_task.get("status") == ledger.STATUS_BLOCKED:
            notifications.notify_task(task_id, stored_task, notifications.OUTCOME_BLOCKED)
        elif chains.is_ready_to_open(tasks, task_id):
            notifications.notify_task(task_id, stored_task, notifications.OUTCOME_FINISHED)
    return outcome.returncode


def execute_claimed_task(
    task_id: str,
    task: ledger.Task,
    run_lock_file: io.TextIOWrapper,
    directory_lock_file: io.TextIOWrapper,
) -> int:
    try:
        try:
            prepared = task_setup.run_setup(task_id, task, directory_lock_file)
            if prepared is None:
                returncode = 1
            else:
                task, directory_lock_file = prepared
                returncode = execute_task(
                    task_id,
                    task,
                    (run_lock_file.fileno(), directory_lock_file.fileno()),
                )
                gates.run_pending_gate(task_id)
        finally:
            locks.release_lock(directory_lock_file)
        thread_settling.mark_parent_of_succeeded_task(task_id)
        thread_settling.settle_pending_threads()
        drain_dependents(task_id, task["session_id"])
        return returncode
    finally:
        locks.release_run_lock(run_lock_file, task["session_id"])


def execute_scheduled_task(task_id: str, session_id: str) -> int:
    failed_claims: list[tuple[str, ledger.Task]] = []
    with ledger.locked_ledger() as (tasks, worktrees):
        claim = claim_task(
            tasks, worktrees, task_id, session_id, ledger.STATUS_SCHEDULED, failed_claims
        )
    notifications.notify_failed_tasks(failed_claims)
    if claim is None:
        return 0
    return execute_claimed_task(task_id, *claim)


def drain_succeeded_task(task_id: str, session_id: str) -> None:
    with ledger.locked_tasks():
        run_lock_file = locks.acquire_run_lock(session_id)
    if run_lock_file is None:
        return
    try:
        if not gates.run_pending_gate_under_directory_lock(task_id):
            return
        thread_settling.mark_parent_of_succeeded_task(task_id)
        thread_settling.settle_pending_threads()
        drain_dependents(task_id, session_id)
    finally:
        locks.release_run_lock(run_lock_file, session_id)


def execute_or_drain_task(task_id: str, session_id: str) -> int:
    task = ledger.load_tasks().get(task_id)
    if (
        task is None
        or task["session_id"] != session_id
        or task["status"] not in ledger.CLEAN_EXIT_STATUSES
    ):
        return execute_scheduled_task(task_id, session_id)
    drain_succeeded_task(task_id, session_id)
    return 0
