import sys
from datetime import datetime
from typing import NamedTuple

from golem import board, chains, delete, display, ledger, locks, picker, t3_code


class ChainCandidate(NamedTuple):
    chain_ids: list[str]
    reason: str
    is_prunable: bool
    is_asked: bool


def is_task_directory_busy(task: ledger.Task) -> bool:
    return locks.is_directory_busy(ledger.directory_to_lock(task, task.get("worktree")))


def chain_prune_selection(
    tasks: ledger.Tasks, chain_ids: list[str], *, is_chain_selected: bool
) -> ledger.Tasks:
    selected_tasks: ledger.Tasks = {}
    for task_id in chain_ids:
        task = tasks[task_id]
        if any(
            dependent_id not in selected_tasks
            for dependent_id in chains.dependent_task_ids(tasks, task_id)
        ):
            continue
        if is_chain_selected or task["status"] == ledger.STATUS_CANCELLED:
            selected_tasks[task_id] = task
    return selected_tasks


def chain_candidates(tasks: ledger.Tasks) -> list[ChainCandidate]:
    today = datetime.now().astimezone().date()
    root_task_ids = sorted(
        task_id for task_id, task in tasks.items() if task.get("after") not in tasks
    )
    candidates: list[ChainCandidate] = []
    for root_task_id in root_task_ids:
        chain_ids = chains.dependents_first_task_ids(tasks, [root_task_id])
        chain_id_set = set(chain_ids)
        column, reason = display.chain_column(tasks, chain_id_set, today)
        is_prunable_done_chain = column == display.COLUMN_DONE and not any(
            is_task_directory_busy(tasks[chain_id]) for chain_id in chain_ids
        )
        is_asked = is_prunable_done_chain and not ledger.are_pull_requests_finished(
            display.chain_pull_requests(tasks, chain_id_set)
        )
        candidates.append(ChainCandidate(chain_ids, reason, is_prunable_done_chain, is_asked))
    return candidates


def chain_row(tasks: ledger.Tasks, candidate: ChainCandidate) -> tuple[str, ...]:
    root_task = tasks[candidate.chain_ids[-1]]
    return (
        ", ".join(
            ledger.task_name(task_id, task)
            for task_id, task in tasks.items()
            if task_id in candidate.chain_ids
        ),
        candidate.reason,
        display.display_directory(root_task["cwd"]),
        display.first_line(root_task["prompt"]),
    )


def picked_asked_indices(
    tasks: ledger.Tasks, candidates: list[ChainCandidate], asked_indices: list[int]
) -> set[int] | None:
    picked_indices = picker.picked_indices(
        "Pick which tasks to prune",
        [chain_row(tasks, candidates[index]) for index in asked_indices],
        "prune",
        is_multi_select=True,
    )
    if picked_indices is None:
        return None
    return {asked_indices[index] for index in picked_indices}


def prune_selection(
    tasks: ledger.Tasks, candidates: list[ChainCandidate], accepted_indices: set[int]
) -> tuple[ledger.Tasks, bool]:
    selected_tasks: ledger.Tasks = {}
    has_candidates = False
    for index, candidate in enumerate(candidates):
        is_chain_selected = candidate.is_prunable and (
            not candidate.is_asked or index in accepted_indices
        )
        chain_selected_tasks = chain_prune_selection(
            tasks, candidate.chain_ids, is_chain_selected=is_chain_selected
        )
        has_candidates = has_candidates or candidate.is_prunable or bool(chain_selected_tasks)
        selected_tasks |= chain_selected_tasks
    return selected_tasks, has_candidates


def delete_pruned_tasks(selected_tasks: ledger.Tasks) -> None:
    deleted_tasks: list[ledger.Task] = []
    with ledger.locked_ledger() as (tasks, worktrees):
        for task_id, evaluated_task in selected_tasks.items():
            if (
                tasks.get(task_id) != evaluated_task
                or chains.dependent_task_ids(tasks, task_id)
                or (
                    evaluated_task["status"] != ledger.STATUS_CANCELLED
                    and is_task_directory_busy(evaluated_task)
                )
            ):
                print(f"golem: kept {task_id}: no longer eligible", file=sys.stderr)
                continue
            if "t3_code_thread_id" in evaluated_task and not t3_code.is_running():
                print(f"golem: kept {task_id}: T3 Code is not running", file=sys.stderr)
                continue
            delete.delete_task_and_release_worktrees(tasks, worktrees, [task_id])
            deleted_tasks.append(evaluated_task)
            print(f"golem: deleted task: {task_id}")
    t3_code.delete_threads(t3_code.recorded_thread_ids(deleted_tasks))


def prune_tasks(*, is_confirmed: bool) -> int:
    tasks = ledger.load_tasks()
    try:
        candidates = chain_candidates(tasks)
        asked_indices = [index for index, candidate in enumerate(candidates) if candidate.is_asked]
        accepted_indices: set[int] | None = set(asked_indices) if is_confirmed else set()
        is_asking = bool(asked_indices) and not is_confirmed
        has_kept_asked_chains = is_asking and not board.is_terminal()
        if is_asking and not has_kept_asked_chains:
            accepted_indices = picked_asked_indices(tasks, candidates, asked_indices)
    except KeyboardInterrupt:
        print()
        return board.INTERRUPTED_EXIT_STATUS
    if accepted_indices is None:
        print(picker.CANCELLED_MESSAGE, file=sys.stderr)
        return 1
    selected_tasks, has_candidates = prune_selection(tasks, candidates, accepted_indices)
    if has_kept_asked_chains:
        print(
            "golem: kept done chains without a terminal; use --yes to prune them", file=sys.stderr
        )
    elif not has_candidates:
        print("golem: no tasks to prune")
        return 0
    delete_pruned_tasks(selected_tasks)
    return 0
