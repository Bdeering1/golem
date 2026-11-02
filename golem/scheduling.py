import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from golem import chains, delete, launch_agent, ledger, progress_log, spawn, task_directories
from golem.backends import claude_hooks, common, registry


def new_session_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class RunPlacement:
    cwd: str
    is_auto_pickup: bool = False
    is_no_worktree: bool = False
    gate: str | None = None
    setup: str | None = None
    environment: dict[str, str] = field(default_factory=dict)


def new_task_record(
    placement: RunPlacement,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    backend: str,
    *,
    after: str | None = None,
) -> ledger.Task:
    record: ledger.Task = {
        "cwd": placement.cwd,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "status": ledger.STATUS_WAITING,
        "session_id": new_session_id(),
        "environment": {**spawn.captured_environment(), **placement.environment},
        "backend": backend,
    }
    if after is not None:
        record["after"] = after
    if placement.is_auto_pickup:
        record["auto_pickup"] = True
    if placement.is_no_worktree:
        record["no_worktree"] = True
    if placement.gate is not None:
        record["gate"] = placement.gate
    if placement.setup is not None:
        record["setup"] = placement.setup
    if when is not None:
        record["status"] = ledger.STATUS_SCHEDULED
        record["due_at"] = when.isoformat()
    return record


def new_follow_up_records(
    run_task_id: str,
    follow_ups: list[list[str]],
    agent_args: tuple[str, ...],
    backend: str,
    placement: RunPlacement,
) -> ledger.Tasks:
    records: ledger.Tasks = {}
    previous_task_id = run_task_id
    placement = replace(placement, setup=None)
    for task_id, prompt in follow_ups:
        if task_id == run_task_id or task_id in records:
            raise SystemExit(f"golem: task ID used more than once: {task_id}")
        records[task_id] = new_task_record(
            placement, prompt, agent_args, None, backend, after=previous_task_id
        )
        previous_task_id = task_id
    return records


def cancel_task(task_id: str) -> int:
    with ledger.locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if task.get("status") not in {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}:
            raise SystemExit(f"golem: task is not scheduled: {task_id}")
        task["status"] = ledger.STATUS_CANCELLED
        pending_task_ids = [task_id]
        while pending_task_ids:
            parent_id = pending_task_ids.pop()
            for waiting_task_id in chains.waiting_dependent_ids(tasks, parent_id):
                waiting_task = tasks[waiting_task_id]
                waiting_task["status"] = ledger.STATUS_CANCELLED
                pending_task_ids.append(waiting_task_id)
        directories = claude_hooks.hook_affected_directories(tasks, task)
    for directory in directories:
        claude_hooks.sync_hooks(directory)
    return 0


def scheduled_time(value: str) -> datetime:
    match = re.fullmatch(r"(1[0-2]|[1-9])(?::([0-5][0-9]))?(am|pm)( tomorrow)?", value.lower())
    if match is None:
        raise SystemExit('golem: --at expects a time like 9pm or "8:30am tomorrow"')
    hour, minute, period, tomorrow = match.groups()
    now = datetime.now(UTC)
    local_time = now.astimezone().replace(tzinfo=None)
    when = local_time.replace(
        hour=int(hour) % 12 + (12 if period == "pm" else 0),
        minute=int(minute or 0),
        second=0,
        microsecond=0,
    )
    if tomorrow or when.astimezone() <= now:
        when += timedelta(days=1)
    return when.astimezone()


def schedule_new_task(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None,
    *,
    backend: str,
    placement: RunPlacement,
    follow_up_records: ledger.Tasks,
) -> int:
    due_time = when or datetime.now(UTC).astimezone()
    record = new_task_record(placement, prompt, agent_args, due_time, backend, after=after)
    records = {task_id: record, **follow_up_records}
    with ledger.locked_ledger() as (tasks, worktrees):
        if after is not None and chains.is_dependent_reservable(tasks, after):
            raise SystemExit(f"golem: parent task changed: {after}")
        replaced = delete.remove_replaceable_tasks(tasks, worktrees, list(records), after)
        tasks.update(records)
    delete.finish_task_replacement(replaced)
    return launch_agent.wake_launch_agent_and_report(task_id, due_time)


def retryable_task(tasks: ledger.Tasks, task_id: str) -> ledger.Task:
    task = ledger.existing_task(tasks, task_id)
    if task.get("status") not in {ledger.STATUS_FAILED, ledger.STATUS_CANCELLED}:
        raise SystemExit(f"golem: task cannot be retried: {task_id}")
    return task


def retry_task(task_id: str, *, is_now: bool, at_text: str | None) -> int:
    tasks = ledger.load_tasks()
    retryable = retryable_task(tasks, task_id)
    task_directories.reject_missing_worktree(retryable, ledger.load_worktrees())
    launch_agent.require_launch_agent()
    when = (
        planned_execution_time(registry.task_backend(retryable), is_now=is_now, at_text=at_text)
        or datetime.now(UTC).astimezone()
    )

    with ledger.locked_ledger() as (tasks, worktrees):
        task = retryable_task(tasks, task_id)
        task_directories.reject_missing_worktree(task, worktrees)
        record = new_task_record(
            RunPlacement(task["cwd"]),
            task["prompt"],
            tuple(task["agent_args"]),
            when,
            ledger.task_backend(task),
            after=task.get("after"),
        )
        if "environment" in task:
            record["environment"] = task["environment"]
        if "worktree" in task:
            record["worktree"] = task["worktree"]
        if "worktree_pending" in task:
            record["worktree_pending"] = task["worktree_pending"]
        if "auto_pickup" in task:
            record["auto_pickup"] = task["auto_pickup"]
        if "no_worktree" in task:
            record["no_worktree"] = task["no_worktree"]
        if "gate" in task:
            record["gate"] = task["gate"]
        if "setup" in task:
            record["setup"] = task["setup"]
        if "setup_complete" in task:
            record["setup_complete"] = task["setup_complete"]
        if "setup_directory" in task:
            record["setup_directory"] = task["setup_directory"]
        tasks[task_id] = record
        progress_log.remove_progress(task["session_id"])
        task_directories.clear_worktree_release(tasks, worktrees, record)
        directories = claude_hooks.hook_affected_directories(tasks, record)
    for directory in directories:
        claude_hooks.sync_hooks(directory)
    return launch_agent.wake_launch_agent_and_report(task_id, when)


def scheduled_task(tasks: ledger.Tasks, task_id: str) -> ledger.Task:
    task = ledger.existing_task(tasks, task_id)
    if task.get("status") != ledger.STATUS_SCHEDULED:
        raise SystemExit(f"golem: task is not scheduled: {task_id}")
    return task


def reschedule_task(task_id: str, *, is_now: bool, at_text: str | None) -> int:
    task = scheduled_task(ledger.load_tasks(), task_id)
    launch_agent.require_launch_agent()
    when = (
        planned_execution_time(registry.task_backend(task), is_now=is_now, at_text=at_text)
        or datetime.now(UTC).astimezone()
    )

    with ledger.locked_tasks() as tasks:
        task = scheduled_task(tasks, task_id)
        task["due_at"] = when.isoformat()
    return launch_agent.wake_launch_agent_and_report(task_id, when)


def planned_execution_time(
    backend: common.Backend, *, is_now: bool, at_text: str | None
) -> datetime | None:
    if is_now:
        return None
    when = backend.next_reset_time() if at_text is None else scheduled_time(at_text)
    if when is None:
        return None
    when = when.astimezone(UTC)
    if when.second or when.microsecond:
        when += timedelta(minutes=1)
    return when.replace(second=0, microsecond=0).astimezone()


def reserve_dependent_task(records: ledger.Tasks, parent_id: str) -> bool:
    with ledger.locked_ledger() as (tasks, worktrees):
        if not chains.is_dependent_reservable(tasks, parent_id):
            return False
        replaced = delete.remove_replaceable_tasks(tasks, worktrees, list(records), parent_id)
        chains.add_dependent_records(tasks, records, parent_id)
    delete.finish_task_replacement(replaced)
    return True


def schedule_run_task(
    arguments: ledger.Arguments,
    task_id: str,
    prompt_text: str,
    task_args: tuple[str, ...],
    follow_up_records: ledger.Tasks,
    placement: RunPlacement,
) -> int:
    after = arguments.after if isinstance(arguments.after, str) else None
    if after is not None:
        dependent_record = new_task_record(
            placement, prompt_text, task_args, None, arguments.backend, after=after
        )
        if reserve_dependent_task({task_id: dependent_record, **follow_up_records}, after):
            claude_hooks.sync_hooks(
                ledger.task_directory(ledger.existing_task(ledger.load_tasks(), after))
            )
            print(f"golem: {task_id} will run after {after}")
            return 0
    when = planned_execution_time(
        registry.BACKENDS[arguments.backend], is_now=arguments.now, at_text=arguments.at
    )
    return schedule_new_task(
        task_id,
        prompt_text,
        task_args,
        when,
        after,
        backend=arguments.backend,
        placement=placement,
        follow_up_records=follow_up_records,
    )
