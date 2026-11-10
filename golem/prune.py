import sys
import time
from datetime import timedelta
from pathlib import Path

from golem import chains, delete, display, ledger, locks, t3_code
from golem.backends import registry

PRUNE_IDLE_DAYS = 7
PRUNE_ANSWER_YES = "y"
PRUNE_ANSWER_NO = "n"
PRUNE_ANSWER_ALL = "a"
PRUNE_ANSWER_QUIT = "q"
PRUNE_ANSWERS = (PRUNE_ANSWER_YES, PRUNE_ANSWER_NO, PRUNE_ANSWER_ALL, PRUNE_ANSWER_QUIT)


def session_idle_reason(task: ledger.Task) -> str | None:
    transcript_modified_time = max(
        (
            transcript_path.stat().st_mtime
            for transcript_path in registry.task_backend(task).transcript_paths(task)
        ),
        default=None,
    )
    if transcript_modified_time is None:
        return "no transcript"
    idle_days = timedelta(seconds=time.time() - transcript_modified_time).days
    if idle_days < PRUNE_IDLE_DAYS:
        return None
    return f"idle {idle_days}d"


def done_task_prune_reason(task: ledger.Task) -> str | None:
    directory = ledger.directory_to_lock(task, task.get("worktree"))
    if locks.is_directory_busy(directory):
        return None
    pull_requests = task.get("pull_requests", [])
    if ledger.has_finished_pull_requests(task):
        return display.pull_request_summary(pull_requests)
    if any(
        pull_request["state"] == ledger.PULL_REQUEST_STATE_OPEN for pull_request in pull_requests
    ):
        return None
    if not Path(directory).is_dir():
        return "directory missing"
    return session_idle_reason(task)


def prune_question(candidates: list[tuple[str, str]]) -> str:
    reasons = {reason for _, reason in candidates}
    if len(reasons) == 1:
        task_ids = ", ".join(task_id for task_id, _ in candidates)
        removals = f"{task_ids} ({ledger.STATUS_DONE}, {reasons.pop()})"
    else:
        removals = ", ".join(
            f"{task_id} ({ledger.STATUS_DONE}, {reason})" for task_id, reason in candidates
        )
    return f"Remove {removals}? [{','.join(PRUNE_ANSWERS)}] "


def prune_answer(candidates: list[tuple[str, str]]) -> str:
    prompt = prune_question(candidates)
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            print()
            return PRUNE_ANSWER_QUIT
        if answer in PRUNE_ANSWERS:
            return answer


def chain_prune_selection(
    tasks: ledger.Tasks,
    chain_ids: list[str],
    *,
    is_offering_done_tasks: bool,
) -> tuple[ledger.Tasks, dict[str, str]]:
    selected_tasks: ledger.Tasks = {}
    done_reasons: dict[str, str] = {}
    for task_id in chain_ids:
        task = tasks[task_id]
        if any(
            dependent_id not in selected_tasks
            for dependent_id in chains.dependent_task_ids(tasks, task_id)
        ):
            continue
        if task["status"] == ledger.STATUS_CANCELLED:
            selected_tasks[task_id] = task
            continue
        if task["status"] != ledger.STATUS_DONE or not is_offering_done_tasks:
            continue
        reason = done_task_prune_reason(task)
        if reason is None:
            continue
        done_reasons[task_id] = reason
        selected_tasks[task_id] = task
    return selected_tasks, done_reasons


def prune_selection(
    tasks: ledger.Tasks,
    *,
    is_confirmed: bool,
    is_keeping_done_tasks: bool,
) -> tuple[ledger.Tasks, bool]:
    selected_tasks: ledger.Tasks = {}
    has_candidates = False
    answer = PRUNE_ANSWER_ALL if is_confirmed else None
    root_task_ids = sorted(
        task_id for task_id, task in tasks.items() if task.get("after") not in tasks
    )
    for root_task_id in root_task_ids:
        chain_ids = chains.dependents_first_task_ids(tasks, [root_task_id])
        is_offering_done_tasks = not is_keeping_done_tasks and answer != PRUNE_ANSWER_QUIT
        chain_selected_tasks, done_reasons = chain_prune_selection(
            tasks, chain_ids, is_offering_done_tasks=is_offering_done_tasks
        )
        has_candidates = has_candidates or bool(chain_selected_tasks)
        if done_reasons and answer != PRUNE_ANSWER_ALL:
            answer = prune_answer(
                [(task_id, done_reasons[task_id]) for task_id in tasks if task_id in done_reasons]
            )
            if answer not in {PRUNE_ANSWER_YES, PRUNE_ANSWER_ALL}:
                chain_selected_tasks, _ = chain_prune_selection(
                    tasks, chain_ids, is_offering_done_tasks=False
                )
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
                    evaluated_task["status"] == ledger.STATUS_DONE
                    and done_task_prune_reason(evaluated_task) is None
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
    is_keeping_done_tasks = not is_confirmed and not sys.stdin.isatty()
    try:
        selected_tasks, has_candidates = prune_selection(
            tasks,
            is_confirmed=is_confirmed,
            is_keeping_done_tasks=is_keeping_done_tasks,
        )
    except KeyboardInterrupt:
        print()
        return 130
    if is_keeping_done_tasks and any(
        task["status"] == ledger.STATUS_DONE for task in tasks.values()
    ):
        print("golem: kept done tasks without a terminal; use --yes to prune them", file=sys.stderr)
    elif not has_candidates:
        print("golem: no tasks to prune")
        return 0
    delete_pruned_tasks(selected_tasks)
    return 0
