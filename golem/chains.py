from golem import ledger
from golem.backends import registry


def can_start_dependents(task: ledger.Task) -> bool:
    return task.get("status") in ledger.SUCCEEDED_STATUSES and "gate_stop_reason" not in task


def waiting_dependent_ids(tasks: ledger.Tasks, parent_id: str) -> list[str]:
    return [
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent["status"] == ledger.STATUS_WAITING and dependent.get("after") == parent_id
    ]


def dependent_task_ids(tasks: ledger.Tasks, parent_id: str) -> list[str]:
    return sorted(
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent.get("after") == parent_id
    )


def descendant_ids_with_status(tasks: ledger.Tasks, task_id: str, statuses: set[str]) -> list[str]:
    matching_ids: list[str] = []
    pending_ids = [task_id]
    while pending_ids:
        for dependent_id in dependent_task_ids(tasks, pending_ids.pop()):
            pending_ids.append(dependent_id)
            if tasks[dependent_id]["status"] in statuses:
                matching_ids.append(dependent_id)
    return matching_ids


def has_running_dependent(tasks: ledger.Tasks, parent_id: str) -> bool:
    return any(
        dependent["status"] == ledger.STATUS_RUNNING and dependent.get("after") == parent_id
        for dependent in tasks.values()
    )


def has_unreviewed_dependent(tasks: ledger.Tasks, parent_id: str) -> bool:
    return any(
        dependent["status"] in ledger.UNREVIEWED_STATUSES and dependent.get("after") == parent_id
        for dependent in tasks.values()
    )


def mark_ancestors_done(tasks: ledger.Tasks, task: ledger.Task) -> None:
    ancestor = tasks.get(task.get("after", ""))
    while ancestor is not None:
        parent = ancestor
        if parent.get("status") == ledger.STATUS_NEEDS_REVIEW:
            parent["status"] = ledger.STATUS_DONE
        ancestor = tasks.get(parent.get("after", ""))


def mark_task_done(tasks: ledger.Tasks, task: ledger.Task) -> None:
    if task.get("status") != ledger.STATUS_NEEDS_REVIEW:
        return
    task["status"] = ledger.STATUS_DONE
    mark_ancestors_done(tasks, task)


def is_dependent_reservable(tasks: ledger.Tasks, parent_id: str) -> bool:
    if parent_id not in tasks:
        raise SystemExit(f"golem: task not found: {parent_id}")
    parent_status = tasks[parent_id]["status"]
    if parent_status == ledger.STATUS_CANCELLED:
        raise SystemExit(f"golem: parent task is cancelled: {parent_id}")
    return not can_start_dependents(tasks[parent_id]) or is_gate_pending(tasks, parent_id)


def add_dependent_records(tasks: ledger.Tasks, records: ledger.Tasks, parent_id: str) -> None:
    if tasks[parent_id].get("auto_pickup"):
        for record in records.values():
            if registry.task_backend(record).IS_AUTO_PICKUP_SUPPORTED:
                record["auto_pickup"] = True
    tasks.update(records)


def is_gate_pending(tasks: ledger.Tasks, task_id: str) -> bool:
    task = tasks.get(task_id)
    if task is None:
        return False
    return (
        task.get("status") == ledger.STATUS_NEEDS_REVIEW
        and "gate" in task
        and "gate_stop_reason" not in task
        and bool(waiting_dependent_ids(tasks, task_id))
    )


def chain_task_ids(tasks: ledger.Tasks, task_id: str) -> set[str]:
    chain_ids = {task_id}
    pending_ids = [task_id]
    while pending_ids:
        current_id = pending_ids.pop()
        current_task = tasks[current_id]
        adjacent_ids = dependent_task_ids(tasks, current_id)
        parent_id = current_task.get("after")
        if parent_id is not None and parent_id in tasks:
            adjacent_ids.append(parent_id)
        for adjacent_id in adjacent_ids:
            if adjacent_id not in chain_ids:
                chain_ids.add(adjacent_id)
                pending_ids.append(adjacent_id)
    return chain_ids


def dependents_first_task_ids(tasks: ledger.Tasks, task_ids: list[str]) -> list[str]:
    ordered_task_ids: list[str] = []
    for task_id in task_ids:
        ordered_task_ids += dependents_first_task_ids(tasks, dependent_task_ids(tasks, task_id))
        ordered_task_ids.append(task_id)
    return ordered_task_ids


def task_depth(tasks: ledger.Tasks, task_id: str) -> int:
    depth = 1
    parent_id = tasks[task_id].get("after")
    while parent_id is not None and parent_id in tasks:
        depth += 1
        parent_id = tasks[parent_id].get("after")
    return depth
