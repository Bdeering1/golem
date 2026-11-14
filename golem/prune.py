import sys
from datetime import datetime

from golem import chains, delete, display, ledger, locks, t3_code

PRUNE_ANSWER_YES = "y"
PRUNE_ANSWER_NO = "n"
PRUNE_ANSWER_ALL = "a"
PRUNE_ANSWER_QUIT = "q"
PRUNE_ANSWERS = (PRUNE_ANSWER_YES, PRUNE_ANSWER_NO, PRUNE_ANSWER_ALL, PRUNE_ANSWER_QUIT)


def is_task_directory_busy(task: ledger.Task) -> bool:
    return locks.is_directory_busy(ledger.directory_to_lock(task, task.get("worktree")))


def prune_answer(task_ids: list[str], reason: str) -> str:
    prompt = f"Remove {', '.join(task_ids)} ({display.COLUMN_DONE}, {reason})? [{','.join(PRUNE_ANSWERS)}] "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            print()
            return PRUNE_ANSWER_QUIT
        if answer in PRUNE_ANSWERS:
            return answer


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


def prune_selection(
    tasks: ledger.Tasks, *, is_confirmed: bool, is_keeping_asked_chains: bool
) -> tuple[ledger.Tasks, bool, bool]:
    selected_tasks: ledger.Tasks = {}
    has_candidates = False
    has_kept_asked_chains = False
    answer = PRUNE_ANSWER_ALL if is_confirmed else None
    today = datetime.now().astimezone().date()
    root_task_ids = sorted(
        task_id for task_id, task in tasks.items() if task.get("after") not in tasks
    )
    for root_task_id in root_task_ids:
        chain_ids = chains.dependents_first_task_ids(tasks, [root_task_id])
        chain_id_set = set(chain_ids)
        column, reason = display.chain_column(tasks, chain_id_set, today)
        is_done = column == display.COLUMN_DONE and not any(
            is_task_directory_busy(tasks[chain_id]) for chain_id in chain_ids
        )
        is_asked = is_done and not ledger.are_pull_requests_finished(
            display.chain_pull_requests(tasks, chain_id_set)
        )
        has_candidates = has_candidates or is_done
        if is_asked and is_keeping_asked_chains:
            has_kept_asked_chains = True
            is_done = False
        elif is_asked and answer != PRUNE_ANSWER_ALL:
            if answer != PRUNE_ANSWER_QUIT:
                answer = prune_answer(
                    [task_id for task_id in tasks if task_id in chain_id_set], reason
                )
            is_done = answer in {PRUNE_ANSWER_YES, PRUNE_ANSWER_ALL}
        chain_selected_tasks = chain_prune_selection(tasks, chain_ids, is_chain_selected=is_done)
        has_candidates = has_candidates or bool(chain_selected_tasks)
        selected_tasks |= chain_selected_tasks
    return selected_tasks, has_candidates, has_kept_asked_chains


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
        selected_tasks, has_candidates, has_kept_asked_chains = prune_selection(
            tasks,
            is_confirmed=is_confirmed,
            is_keeping_asked_chains=not is_confirmed and not sys.stdin.isatty(),
        )
    except KeyboardInterrupt:
        print()
        return 130
    if has_kept_asked_chains:
        print(
            "golem: kept done chains without a terminal; use --yes to prune them", file=sys.stderr
        )
    elif not has_candidates:
        print("golem: no tasks to prune")
        return 0
    delete_pruned_tasks(selected_tasks)
    return 0
