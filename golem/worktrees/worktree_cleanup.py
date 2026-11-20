import os
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

from golem import ledger, locks
from golem.worktrees import git, task_directories

CLAUDE_LOCK_PATTERN = re.compile(r"claude session (\S+) \(pid (\d+) start .+\)")


def is_verified_stale_worktree_lock(
    resource: ledger.WorktreeResource, tasks: ledger.Tasks, worktree: str, reason: str
) -> bool:
    match = CLAUDE_LOCK_PATTERN.fullmatch(reason)
    if match is None:
        return False
    session_id, process_id_text = match.groups()
    session_ids = {resource["session_id"]}
    session_ids.update(
        tasks[task_id]["session_id"]
        for task_id in task_directories.worktree_task_ids(tasks, worktree)
    )
    if session_id not in session_ids:
        return False
    try:
        os.kill(int(process_id_text), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def git_worktree_retention_reasons(
    resource: ledger.WorktreeResource,
    tasks: ledger.Tasks,
    worktree: str,
    entries: list[list[str]] | None,
) -> list[str]:
    if not Path(worktree).is_dir():
        return ["missing"]
    repository = resource.get("repository")
    if not isinstance(repository, str):
        return ["repository unknown"]
    if entries is None:
        return ["repository unavailable"]
    matching_entry = git.git_worktree_entry(entries, worktree)
    if matching_entry is None:
        return ["not a registered worktree"]
    if entries[0] == matching_entry:
        return ["original checkout"]
    if not task_directories.is_worktree_owner_verified(worktree, resource["session_id"]):
        return ["ownership cannot be verified"]
    reasons: list[str] = []
    lock_reason = git.git_worktree_lock_reason(matching_entry)
    if lock_reason is not None and not is_verified_stale_worktree_lock(
        resource, tasks, worktree, lock_reason
    ):
        reasons.append("worktree lock is active")
    status_result = git.run_git(worktree, "status", "--porcelain", "--untracked-files=all")
    if status_result.returncode or status_result.stdout:
        reasons.append("local changes")
    index_result = git.run_git(worktree, "ls-files", "-v", "-z")
    if index_result.returncode or any(
        entry[:1].islower() or entry.startswith("S ") for entry in index_result.stdout.split("\0")
    ):
        reasons.append("index flags may hide local changes")
    branch_result = git.run_git(worktree, "symbolic-ref", "-q", "HEAD")
    if branch_result.returncode:
        ref_result = git.run_git(
            repository, "for-each-ref", "--contains", matching_entry[1].removeprefix("HEAD ")
        )
        if ref_result.returncode or not ref_result.stdout.strip():
            reasons.append("unpublished commits")
    return reasons


def task_worktree_retention_reasons(
    tasks: ledger.Tasks, worktree: str, *, is_released: bool
) -> list[str]:
    reasons: list[str] = []
    worktree_task_ids = task_directories.worktree_task_ids(tasks, worktree)
    if any(tasks[task_id].get("status") == ledger.STATUS_RUNNING for task_id in worktree_task_ids):
        reasons.append("in use")
    for task_id in worktree_task_ids:
        status = tasks[task_id].get("status")
        if status in {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING, ledger.STATUS_BLOCKED}:
            reasons.append(f"task {status}")
        elif status == ledger.STATUS_FINISHED and not ledger.is_counted_opened(tasks, task_id):
            reasons.append("task not opened")
        elif status in {ledger.STATUS_FAILED, ledger.STATUS_CANCELLED} and not is_released:
            reasons.append("task can be retried (delete the task to override)")
    return reasons


def worktree_retention_reasons(
    tasks: ledger.Tasks,
    resource: ledger.WorktreeResource | None,
    worktree: str,
    entries: list[list[str]] | None,
    *,
    is_released: bool = False,
) -> list[str]:
    if resource is None:
        return ["ownership unknown"]
    if resource.get("owner") == task_directories.WORKTREE_OWNER_EXTERNAL:
        return ["not created by Golem"]
    if resource.get("removed_at") is not None:
        return ["already removed"]
    if resource.get("owner") != task_directories.WORKTREE_OWNER_GOLEM:
        return ["ownership unknown"]
    reasons: list[str] = []
    directories = {
        git.resolved_path(ledger.directory_to_lock(tasks[task_id], tasks[task_id].get("worktree")))
        for task_id in task_directories.worktree_task_ids(tasks, worktree)
    } - {git.resolved_path(worktree)}
    for directory in directories:
        directory_lock_file = locks.acquire_lock(locks.directory_lock_path(directory))
        if directory_lock_file is None:
            reasons.append("in use")
            break
        locks.release_lock(directory_lock_file)
    reasons.extend(
        task_worktree_retention_reasons(
            tasks,
            worktree,
            is_released=resource.get("released_at") is not None or is_released,
        )
    )
    if entries is not None and any(
        Path(worktree).resolve() in Path(entry[0].removeprefix("worktree ")).resolve().parents
        for entry in entries
    ):
        reasons.append("contains another worktree")
    reasons.extend(git_worktree_retention_reasons(resource, tasks, worktree, entries))
    return list(dict.fromkeys(reasons))


def record_worktree_removal_error(
    resource: ledger.WorktreeResource, error: str
) -> tuple[bool, list[str]]:
    resource["removal_error"] = error
    return False, [error]


def remove_inspected_worktree_resource(
    resource: ledger.WorktreeResource, entry: list[str], worktree: str
) -> tuple[bool, list[str]]:
    repository = resource.get("repository")
    if repository is None:
        return False, []
    commands = ["remove"]
    if git.git_worktree_lock_reason(entry) is not None:
        commands.insert(0, "unlock")
    for command in commands:
        result = git.run_git(
            repository,
            "-c",
            "status.showUntrackedFiles=all",
            "worktree",
            command,
            worktree,
        )
        if result.returncode:
            error = result.stderr.strip() or result.stdout.strip() or "git worktree command failed"
            return record_worktree_removal_error(resource, error)
    resource["removed_at"] = datetime.now(UTC).isoformat()
    resource.pop("removal_error", None)
    return True, []


@contextmanager
def locked_worktree_inspection(
    tasks: ledger.Tasks, worktrees: ledger.Worktrees, worktree: str, *, is_released: bool = False
) -> Generator[tuple[ledger.WorktreeResource | None, list[str] | None, list[str]]]:
    resource = task_directories.worktree_resource(worktrees, worktree)
    if resource is None:
        yield None, None, ["ownership unknown"]
        return
    directory_lock_file = locks.acquire_lock(locks.directory_lock_path(worktree))
    if directory_lock_file is None:
        yield resource, None, ["in use"]
        return
    try:
        repository = resource.get("repository")
        entries = git.git_worktree_entries(repository) if isinstance(repository, str) else None
        reasons = worktree_retention_reasons(
            tasks, resource, worktree, entries, is_released=is_released
        )
        entry = git.git_worktree_entry(entries, worktree) if entries else None
        yield resource, entry, reasons
    finally:
        locks.release_lock(directory_lock_file)


def remove_worktree_resource(
    tasks: ledger.Tasks, worktrees: ledger.Worktrees, worktree: str, *, is_released: bool = False
) -> tuple[bool, list[str]]:
    with locked_worktree_inspection(tasks, worktrees, worktree, is_released=is_released) as (
        resource,
        entry,
        reasons,
    ):
        if reasons or resource is None or entry is None:
            return False, reasons
        repository = resource.get("repository")
        if repository is None:
            return False, reasons
        return remove_inspected_worktree_resource(resource, entry, worktree)


def list_worktrees() -> int:
    tasks = ledger.load_tasks()
    worktrees = ledger.load_worktrees()
    worktree_paths = set(worktrees)
    for task in tasks.values():
        recorded = task_directories.recorded_worktree(tasks, task)
        if recorded is not None:
            worktree_paths.add(git.resolved_path(recorded[0]))
    if not worktree_paths:
        print("golem: no worktrees")
        return 0
    rows: list[tuple[str, str, str, str]] = []
    for worktree in sorted(worktree_paths):
        with locked_worktree_inspection(tasks, worktrees, worktree) as (resource, _, reasons):
            state = "eligible" if not reasons else "retained"
        if resource is not None and resource.get("removed_at") is not None:
            state = "removed"
            reasons = []
        removal_error = resource.get("removal_error") if resource is not None else None
        if isinstance(removal_error, str):
            reasons.append(f"last removal error: {removal_error}")
        rows.append(
            (
                worktree,
                ",".join(task_directories.worktree_task_ids(tasks, worktree)),
                state,
                "; ".join(reasons),
            )
        )
    headers = ("PATH", "TASKS", "STATE", "REASON")
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    for row in (headers, *rows):
        print(
            "  ".join(
                str(value).ljust(width) for value, width in zip(row, widths, strict=True)
            ).rstrip()
        )
    return 0


def print_retained_worktree(worktree: str, reasons: list[str]) -> None:
    print(f"golem: retained worktree: {worktree}: {'; '.join(reasons)}", file=sys.stderr)


def set_worktree_auto_remove(retention_days: int | None) -> int:
    settings = ledger.load_settings()
    if retention_days is None:
        settings.pop("worktree_retention_days", None)
    else:
        settings["worktree_retention_days"] = retention_days
    ledger.save_settings(settings)
    return 0


def set_worktree_auto_remove_from_arguments(arguments: ledger.Arguments) -> int:
    if arguments.off:
        return set_worktree_auto_remove(None)
    retention_days = arguments.after
    if not isinstance(retention_days, int):
        raise SystemExit("golem: --after is required")
    if retention_days < 0:
        raise SystemExit("golem: --after must be non-negative")
    return set_worktree_auto_remove(retention_days)


def auto_remove_worktrees() -> None:
    settings = ledger.load_settings()
    retention_days = settings.get("worktree_retention_days")
    if retention_days is None:
        return
    now = datetime.now(UTC)
    with ledger.locked_ledger() as (tasks, worktrees):
        for worktree, resource in worktrees.items():
            if (
                resource.get("owner") != task_directories.WORKTREE_OWNER_GOLEM
                or resource.get("removed_at") is not None
            ):
                continue
            if "released_at" not in resource:
                continue
            released_at = resource["released_at"]
            eligible_at = datetime.fromisoformat(released_at) + timedelta(days=retention_days)
            if now < eligible_at:
                continue
            try:
                is_removed, reasons = remove_worktree_resource(tasks, worktrees, worktree)
            except OSError as error:
                is_removed, reasons = record_worktree_removal_error(resource, str(error))
            if is_removed:
                print(
                    f"golem: removed worktree: {worktree}",
                    file=sys.stderr,
                )
            elif resource.get("removal_error") in reasons:
                print_retained_worktree(worktree, reasons)
