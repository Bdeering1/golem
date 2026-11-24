import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from golem import (
    accounts,
    chains,
    launch_agent,
    ledger,
    progress_log,
    spawn,
    t3_code,
    task_ids,
    task_processes,
    usage_windows,
)
from golem.backends import claude_hooks, common, registry
from golem.worktrees import task_directories


def new_session_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class RunPlacement:
    cwd: str
    auto_pickup_client: str | None = None
    is_no_worktree: bool = False
    gate: str | None = None
    setup: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    account: int | None = None


def new_task_record(
    placement: RunPlacement,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    backend: str,
    *,
    after: str | None = None,
    label: str | None = None,
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
    if label is not None:
        record["label"] = label
    if after is not None:
        record["after"] = after
    if placement.auto_pickup_client is not None:
        record["auto_pickup_client"] = placement.auto_pickup_client
    if placement.is_no_worktree:
        record["no_worktree"] = True
    if placement.gate is not None:
        record["gate"] = placement.gate
    if placement.setup is not None:
        record["setup"] = placement.setup
    if placement.account is not None:
        record["account"] = placement.account
    if when is not None:
        record["status"] = ledger.STATUS_SCHEDULED
        record["due_at"] = when.isoformat()
    return record


def new_follow_up_records(
    follow_ups: list[tuple[str | None, str]],
    agent_args: tuple[str, ...],
    backend: str,
    placement: RunPlacement,
) -> list[ledger.Task]:
    placement = replace(placement, setup=None)
    return [
        new_task_record(placement, prompt, agent_args, None, backend, label=label)
        for label, prompt in follow_ups
    ]


def print_dependents(records: ledger.Tasks) -> None:
    for task_id, record in records.items():
        print(f"golem: {task_id} will run after {record.get('after')}")


def cancel_task(task_id: str) -> int:
    with ledger.locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = tasks[task_id]
        if not ledger.is_cancellable(task):
            raise SystemExit(f"golem: task cannot be cancelled: {task_id}")
        previous_status = task["status"]
        task["status"] = ledger.STATUS_CANCELLED
        pending_task_ids = [task_id]
        while pending_task_ids:
            parent_id = pending_task_ids.pop()
            for waiting_task_id in chains.waiting_dependent_ids(tasks, parent_id):
                waiting_task = tasks[waiting_task_id]
                waiting_task["status"] = ledger.STATUS_CANCELLED
                pending_task_ids.append(waiting_task_id)
        directories = claude_hooks.hook_affected_directories(tasks, task)
        cancelled_task = task.copy()
    for directory in directories:
        claude_hooks.sync_hooks(directory)
    task_processes.stop_task_processes(task_id, cancelled_task, previous_status)
    return 0


def scheduled_time(value: str) -> datetime:
    parsed = usage_windows.parsed_time(value)
    if parsed is None:
        raise SystemExit('golem: --at expects a time like 9pm or "8:30am tomorrow"')
    time_of_day, is_tomorrow = parsed
    now = datetime.now(UTC)
    when = datetime.combine(now.astimezone().date(), time_of_day)
    if is_tomorrow or when.astimezone() <= now:
        when += timedelta(days=1)
    return when.astimezone()


def schedule_new_task(
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None,
    *,
    backend: str,
    placement: RunPlacement,
    follow_up_records: list[ledger.Task],
    label: str | None,
) -> int:
    due_time = when or datetime.now(UTC).astimezone()
    record = new_task_record(placement, prompt, agent_args, due_time, backend, label=label)
    prefix = (
        task_ids.task_id_prefix(task_ids.repository_name(placement.cwd)) if after is None else ""
    )
    with ledger.locked_tasks() as tasks:
        if after is not None and chains.is_dependent_reservable(tasks, after):
            raise SystemExit(f"golem: parent task changed: {after}")
        if after is None:
            task_id = task_ids.new_root_task_id(tasks, prefix)
            tasks[task_id] = record
        else:
            task_id = next(iter(task_ids.insert_dependents(tasks, [record], after)))
        inserted_follow_ups = task_ids.insert_dependents(tasks, follow_up_records, task_id)
    exit_code = launch_agent.wake_launch_agent_and_report(task_id, due_time)
    print_dependents(inserted_follow_ups)
    return exit_code


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
    usage_environment = accounts.account_environment(os.environ.copy(), retryable.get("account"))
    when = (
        planned_execution_time(
            registry.task_usage_backend(retryable),
            usage_environment,
            is_now=is_now,
            at_text=at_text,
        )
        or datetime.now(UTC).astimezone()
    )
    t3_code.open_to_delete(t3_code.recorded_thread_ids([retryable]))

    with ledger.locked_ledger() as (tasks, worktrees):
        task = retryable_task(tasks, task_id)
        task_directories.reject_missing_worktree(task, worktrees)
        t3_code.require_running_to_delete(t3_code.recorded_thread_ids([task]))
        record = new_task_record(
            RunPlacement(task["cwd"], account=task.get("account")),
            task["prompt"],
            tuple(task["agent_args"]),
            when,
            task["backend"],
            after=task.get("after"),
            label=task.get("label"),
        )
        if "environment" in task:
            record["environment"] = task["environment"]
        if "worktree" in task:
            record["worktree"] = task["worktree"]
        if "worktree_pending" in task:
            record["worktree_pending"] = task["worktree_pending"]
        if "auto_pickup_client" in task:
            record["auto_pickup_client"] = task["auto_pickup_client"]
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
        previous_thread_ids = t3_code.recorded_thread_ids([task])
        progress_log.remove_progress(task["session_id"])
        task_directories.clear_worktree_release(tasks, worktrees, record)
        directories = claude_hooks.hook_affected_directories(tasks, record)
    t3_code.delete_threads(previous_thread_ids)
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
        planned_execution_time(
            registry.task_usage_backend(task),
            accounts.account_environment(os.environ.copy(), task.get("account")),
            is_now=is_now,
            at_text=at_text,
        )
        or datetime.now(UTC).astimezone()
    )

    with ledger.locked_tasks() as tasks:
        task = scheduled_task(tasks, task_id)
        task["due_at"] = when.isoformat()
    return launch_agent.wake_launch_agent_and_report(task_id, when)


def rounded_execution_time(when: datetime) -> datetime:
    when = when.astimezone(UTC)
    if when.second or when.microsecond:
        when += timedelta(minutes=1)
    return when.replace(second=0, microsecond=0).astimezone()


def planned_execution_time(
    backend: common.UsageBackend,
    environment: dict[str, str],
    *,
    is_now: bool,
    at_text: str | None,
) -> datetime | None:
    if is_now:
        return None
    when = backend.next_reset_time(environment) if at_text is None else scheduled_time(at_text)
    return None if when is None else rounded_execution_time(when)


def reserve_dependent_task(
    records: list[ledger.Task], parent_id: str, default_clients: dict[str, str]
) -> ledger.Tasks | None:
    with ledger.locked_tasks() as tasks:
        if not chains.is_dependent_reservable(tasks, parent_id):
            return None
        chains.add_parent_auto_pickup(tasks, records, parent_id, default_clients)
        return task_ids.insert_dependents(tasks, records, parent_id)


def schedule_run_task(
    arguments: ledger.Arguments,
    prompt_text: str,
    task_args: tuple[str, ...],
    follow_up_records: list[ledger.Task],
    placement: RunPlacement,
    *,
    default_clients: dict[str, str],
) -> int:
    after = arguments.after if isinstance(arguments.after, str) else None
    if after is not None:
        dependent_record = new_task_record(
            placement, prompt_text, task_args, None, arguments.backend, label=arguments.label
        )
        inserted = reserve_dependent_task(
            [dependent_record, *follow_up_records], after, default_clients
        )
        if inserted is not None:
            claude_hooks.sync_hooks(
                ledger.task_directory(ledger.existing_task(ledger.load_tasks(), after))
            )
            print_dependents(inserted)
            return 0
    when = planned_execution_time(
        registry.usage_backend(arguments.backend, list(task_args)),
        accounts.account_environment(os.environ.copy(), placement.account),
        is_now=arguments.now,
        at_text=arguments.at,
    )
    return schedule_new_task(
        prompt_text,
        task_args,
        when,
        after,
        backend=arguments.backend,
        placement=placement,
        follow_up_records=follow_up_records,
        label=arguments.label,
    )
