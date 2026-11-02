from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime

from golem import chains, git, ledger, progress_log, task_directories, worktree_cleanup
from golem.backends import claude_hooks, registry


@dataclass(frozen=True)
class ReplacedTasks:
    task_ids: list[str]
    session_ids: list[str]
    directories: set[str]


def delete_task_and_release_worktrees(
    tasks: ledger.Tasks, worktrees: ledger.Worktrees, task_ids: list[str]
) -> None:
    for session_id in pop_tasks_and_release_worktrees(tasks, worktrees, task_ids):
        progress_log.remove_progress(session_id)


def pop_tasks_and_release_worktrees(
    tasks: ledger.Tasks, worktrees: ledger.Worktrees, task_ids: list[str]
) -> list[str]:
    selected_tasks = [tasks[task_id] for task_id in task_ids]
    worktree_paths = [
        path
        for path in worktrees
        if any(task_directories.task_uses_worktree(tasks, task, path) for task in selected_tasks)
    ]
    session_ids = [tasks.pop(task_id)["session_id"] for task_id in task_ids]
    for path in worktree_paths:
        resource = worktrees[path]
        if (
            resource.get("owner") == task_directories.WORKTREE_OWNER_GOLEM
            and resource.get("removed_at") is None
            and not task_directories.worktree_task_ids(tasks, path)
        ):
            resource.setdefault("released_at", datetime.now(UTC).isoformat())
    return session_ids


def is_replaceable_task(task: ledger.Task) -> bool:
    return task["status"] == ledger.STATUS_DONE and ledger.has_finished_pull_requests(task)


def remove_replaceable_tasks(
    tasks: ledger.Tasks, worktrees: ledger.Worktrees, task_ids: list[str], parent_id: str | None
) -> ReplacedTasks:
    replaced_ids: set[str] = set()
    for task_id in task_ids:
        if task_id not in tasks:
            continue
        chain_ids = chains.chain_task_ids(tasks, task_id)
        if parent_id in chain_ids or not all(
            is_replaceable_task(tasks[chain_id]) for chain_id in chain_ids
        ):
            raise SystemExit(f"golem: task already exists: {task_id}")
        replaced_ids |= chain_ids
    ordered_ids = [task_id for task_id in tasks if task_id in replaced_ids]
    directories: set[str] = set()
    for task_id in ordered_ids:
        directories |= claude_hooks.hook_affected_directories(tasks, tasks[task_id])
    session_ids = pop_tasks_and_release_worktrees(tasks, worktrees, ordered_ids)
    return ReplacedTasks(ordered_ids, session_ids, directories)


def finish_task_replacement(replaced: ReplacedTasks) -> None:
    for session_id in replaced.session_ids:
        progress_log.remove_progress(session_id)
    for task_id in replaced.task_ids:
        print(f"golem: replaced task: {task_id}")
    for directory in replaced.directories:
        claude_hooks.sync_hooks(directory)


def selected_delete_task_ids(
    tasks: ledger.Tasks, task_ids: list[str], *, is_chain: bool
) -> tuple[list[str], list[str]]:
    selected_ids: set[str] = set()
    messages: list[str] = []
    for task_id in task_ids:
        if task_id not in tasks:
            messages.append(f"golem: task not found: {task_id}")
        elif is_chain:
            selected_ids.update(chains.chain_task_ids(tasks, task_id))
        else:
            selected_ids.add(task_id)
    return [task_id for task_id in tasks if task_id in selected_ids], messages


def task_worktrees_and_refusals(
    tasks: ledger.Tasks, task_ids: list[str], *, is_removing_worktree: bool
) -> tuple[dict[str, str | None], list[str]]:
    selected_id_set = set(task_ids)
    worktrees_by_task: dict[str, str | None] = {}
    messages: list[str] = []
    for task_id in task_ids:
        task = tasks[task_id]
        recorded = task_directories.recorded_worktree(tasks, task)
        worktree = git.resolved_path(recorded[0]) if recorded is not None else None
        worktrees_by_task[task_id] = worktree
        if task.get("status") == ledger.STATUS_RUNNING:
            messages.append(f"golem: task is running: {task_id}")
        dependent_ids = [
            dependent_id
            for dependent_id in chains.dependent_task_ids(tasks, task_id)
            if dependent_id not in selected_id_set
        ]
        if dependent_ids:
            messages.append(f"golem: task has dependents: {task_id}: {', '.join(dependent_ids)}")
        if is_removing_worktree and worktree is None:
            messages.append(f"golem: task has no worktree: {task_id}")
    return worktrees_by_task, messages


def remove_selected_worktrees(
    tasks: ledger.Tasks,
    worktrees: ledger.Worktrees,
    worktrees_by_task: dict[str, str | None],
) -> set[str] | None:
    selected_worktrees = list(
        dict.fromkeys(
            worktree
            for worktree in worktrees_by_task.values()
            if worktree is not None
            and (
                (resource := task_directories.worktree_resource(worktrees, worktree)) is None
                or resource.get("removed_at") is None
            )
        )
    )
    selected_id_set = set(worktrees_by_task)
    remaining_tasks = {
        task_id: task for task_id, task in tasks.items() if task_id not in selected_id_set
    }
    with ExitStack() as stack:
        inspections = [
            (
                worktree,
                *stack.enter_context(
                    worktree_cleanup.locked_worktree_inspection(
                        remaining_tasks, worktrees, worktree
                    )
                ),
            )
            for worktree in selected_worktrees
        ]
        refusals = [(worktree, reasons) for worktree, _, _, reasons in inspections if reasons]
        if refusals:
            for worktree, reasons in refusals:
                worktree_cleanup.print_retained_worktree(worktree, reasons)
            return None
        removed_worktrees: set[str] = set()
        for worktree, resource, entry, _ in inspections:
            if resource is None or entry is None:
                continue
            is_removed, reasons = worktree_cleanup.remove_inspected_worktree_resource(
                resource, entry, worktree
            )
            if not is_removed:
                for removed_worktree in removed_worktrees:
                    print(f"golem: removed worktree: {removed_worktree}")
                worktree_cleanup.print_retained_worktree(worktree, reasons)
                return None
            removed_worktrees.add(worktree)
    return removed_worktrees


def delete_tasks(task_ids: list[str], *, is_chain: bool, is_removing_worktree: bool) -> int:
    directories: set[str] = set()
    with ledger.locked_ledger() as (tasks, worktrees):
        selected_task_ids, messages = selected_delete_task_ids(tasks, task_ids, is_chain=is_chain)
        worktrees_by_task, check_messages = task_worktrees_and_refusals(
            tasks, selected_task_ids, is_removing_worktree=is_removing_worktree
        )
        if messages or check_messages:
            raise SystemExit("\n".join([*messages, *check_messages]))
        for task_id in selected_task_ids:
            directories |= claude_hooks.hook_affected_directories(tasks, tasks[task_id])
        removed_worktrees: set[str] | None = set()
        if is_removing_worktree:
            # golem's own settings file would otherwise count as local changes.
            for task_id in selected_task_ids:
                claude_hooks.remove_hooks(ledger.task_directory(tasks[task_id]))
            removed_worktrees = remove_selected_worktrees(tasks, worktrees, worktrees_by_task)
        if removed_worktrees is not None:
            selected_tasks = [(task_id, tasks[task_id]) for task_id in selected_task_ids]
            delete_task_and_release_worktrees(tasks, worktrees, selected_task_ids)
            for task_id, task in selected_tasks:
                worktree = worktrees_by_task[task_id]
                print_deleted_task(
                    task_id, task, worktree, is_removed=worktree in removed_worktrees
                )
    for directory in directories:
        claude_hooks.sync_hooks(directory)
    return 0 if removed_worktrees is not None else 1


def print_deleted_task(
    task_id: str, task: ledger.Task, worktree: str | None, *, is_removed: bool
) -> None:
    print(f"golem: deleted task: {task_id}")
    session = registry.task_backend(task).conversation_id(task)
    if session is not None:
        print(f"golem: session: {session}")
    if worktree is not None:
        print(f"golem: worktree: {worktree}")
    if is_removed:
        print(f"golem: removed worktree: {worktree}")
