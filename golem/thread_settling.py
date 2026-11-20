import sys

from golem import chains, ledger, t3_code


def mark_parent_pending(tasks: ledger.Tasks, task: ledger.Task) -> None:
    parent = tasks.get(task.get("after", ""))
    if parent is None or "t3_code_thread_id" not in parent or "t3_code_thread_settle" in parent:
        return
    parent["t3_code_thread_settle"] = ledger.THREAD_SETTLE_PENDING


def mark_parent_of_succeeded_task(task_id: str) -> None:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is not None and chains.can_start_dependents(task):
            mark_parent_pending(tasks, task)


def print_settle_failure(thread_id: str, error: Exception) -> None:
    print(f"golem: could not settle T3 Code thread {thread_id}: {error}", file=sys.stderr)


def settle_task_thread(origin: str, token: str, thread_id: str, finished_at: str) -> bool:
    try:
        projection = t3_code.thread_projection(origin, token, thread_id)
    except t3_code.CONNECTION_ERRORS:
        return False
    except t3_code.T3_CODE_ERRORS as error:
        if not t3_code.is_thread_missing(thread_id):
            print_settle_failure(thread_id, error)
        return True
    try:
        t3_code.settle_idle_thread(origin, token, thread_id, projection, finished_at)
    except t3_code.T3_CODE_ERRORS as error:
        print_settle_failure(thread_id, error)
    return True


def settle_pending_threads() -> None:
    if not t3_code.is_running():
        return
    tasks = ledger.load_tasks()
    pending_threads = {
        task_id: (thread_id, finished_at)
        for task_id, task in tasks.items()
        if task.get("t3_code_thread_settle") == ledger.THREAD_SETTLE_PENDING
        and (thread_id := task.get("t3_code_thread_id")) is not None
        and (finished_at := task.get("finished_at")) is not None
    }
    if not pending_threads:
        return
    try:
        origin = t3_code.server_origin()
        token = t3_code.issue_token()
    except (*t3_code.CONNECTION_ERRORS, t3_code.T3CodeError):
        return
    except t3_code.T3_CODE_ERRORS as error:
        for thread_id, _ in pending_threads.values():
            print_settle_failure(thread_id, error)
        settled_ids = list(pending_threads)
    else:
        settled_ids = [
            task_id
            for task_id, (thread_id, finished_at) in pending_threads.items()
            if settle_task_thread(origin, token, thread_id, finished_at)
        ]
    with ledger.locked_tasks() as locked_tasks_by_id:
        for task_id in settled_ids:
            task = locked_tasks_by_id.get(task_id)
            if task is not None and task["session_id"] == tasks[task_id]["session_id"]:
                task["t3_code_thread_settle"] = ledger.THREAD_SETTLE_DONE
