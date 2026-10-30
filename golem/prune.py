import json
import subprocess
import sys
import time
from datetime import timedelta
from typing import TypedDict, cast

from golem import chains, delete, git, ledger, locks
from golem.backends import common

PRUNE_IDLE_DAYS = 7
PULL_REQUEST_TIMEOUT_SECONDS = 30
PULL_REQUEST_STATE_OPEN = "OPEN"
PRUNE_ANSWER_YES = "y"
PRUNE_ANSWER_NO = "n"
PRUNE_ANSWER_ALL = "a"
PRUNE_ANSWER_QUIT = "q"
PRUNE_ANSWERS = (PRUNE_ANSWER_YES, PRUNE_ANSWER_NO, PRUNE_ANSWER_ALL, PRUNE_ANSWER_QUIT)


class PullRequest(TypedDict):
    number: int
    state: str


type PullRequestCache = dict[str | None, PullRequest | None]


def branch_pull_request(worktree: str | None) -> PullRequest | None:
    if worktree is None:
        return None
    branch = git.run_git(worktree, "symbolic-ref", "--short", "HEAD")
    if branch.returncode:
        return None
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch.stdout.strip(),
                "--state",
                "all",
                "--json",
                "number,state",
            ],
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=PULL_REQUEST_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    pull_requests = cast("list[PullRequest]", json.loads(result.stdout))
    open_pull_request = next(
        (
            pull_request
            for pull_request in pull_requests
            if pull_request["state"] == PULL_REQUEST_STATE_OPEN
        ),
        None,
    )
    if open_pull_request is not None:
        return open_pull_request
    return max(pull_requests, key=lambda pull_request: pull_request["number"], default=None)


def session_idle_reason(task: ledger.Task) -> str | None:
    transcript_modified_time = max(
        (transcript_path.stat().st_mtime for transcript_path in common.transcript_paths(task)),
        default=None,
    )
    if transcript_modified_time is None:
        return "no transcript"
    idle_days = timedelta(seconds=time.time() - transcript_modified_time).days
    if idle_days < PRUNE_IDLE_DAYS:
        return None
    return f"idle {idle_days}d"


def done_task_prune_reason(task: ledger.Task, pull_requests: PullRequestCache) -> str | None:
    worktree = task.get("worktree")
    if locks.is_directory_busy(ledger.directory_to_lock(task, worktree)):
        return None
    if worktree not in pull_requests:
        pull_requests[worktree] = branch_pull_request(worktree)
    pull_request = pull_requests[worktree]
    if pull_request is None:
        return session_idle_reason(task)
    if pull_request["state"] == PULL_REQUEST_STATE_OPEN:
        return None
    return f"PR #{pull_request['number']} {pull_request['state'].lower()}"


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
    pull_requests: PullRequestCache,
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
        reason = done_task_prune_reason(task, pull_requests)
        if reason is None:
            continue
        done_reasons[task_id] = reason
        selected_tasks[task_id] = task
    return selected_tasks, done_reasons


def prune_selection(
    tasks: ledger.Tasks,
    pull_requests: PullRequestCache,
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
            tasks, chain_ids, pull_requests, is_offering_done_tasks=is_offering_done_tasks
        )
        has_candidates = has_candidates or bool(chain_selected_tasks)
        if done_reasons and answer != PRUNE_ANSWER_ALL:
            answer = prune_answer(
                [(task_id, done_reasons[task_id]) for task_id in tasks if task_id in done_reasons]
            )
            if answer not in {PRUNE_ANSWER_YES, PRUNE_ANSWER_ALL}:
                chain_selected_tasks, _ = chain_prune_selection(
                    tasks, chain_ids, pull_requests, is_offering_done_tasks=False
                )
        selected_tasks |= chain_selected_tasks
    return selected_tasks, has_candidates


def delete_pruned_tasks(
    selected_tasks: ledger.Tasks,
    pull_requests: PullRequestCache,
) -> None:
    with ledger.locked_ledger() as (tasks, worktrees):
        for task_id, evaluated_task in selected_tasks.items():
            if (
                tasks.get(task_id) != evaluated_task
                or chains.dependent_task_ids(tasks, task_id)
                or (
                    evaluated_task["status"] == ledger.STATUS_DONE
                    and done_task_prune_reason(evaluated_task, pull_requests) is None
                )
            ):
                print(f"golem: kept {task_id}: no longer eligible", file=sys.stderr)
                continue
            delete.delete_task_and_release_worktrees(tasks, worktrees, [task_id])
            print(f"golem: deleted task: {task_id}")


def prune_tasks(*, is_confirmed: bool) -> int:
    tasks = ledger.load_tasks()
    is_keeping_done_tasks = not is_confirmed and not sys.stdin.isatty()
    pull_requests: PullRequestCache = {}
    try:
        selected_tasks, has_candidates = prune_selection(
            tasks,
            pull_requests,
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
    delete_pruned_tasks(selected_tasks, pull_requests)
    return 0
