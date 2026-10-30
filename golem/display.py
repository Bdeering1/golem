import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from golem import ledger

STATUS_COLORS = {
    ledger.STATUS_NEEDS_REVIEW: 33,
    ledger.STATUS_FAILED: 196,
    ledger.STATUS_RUNNING: 39,
    ledger.STATUS_SCHEDULED: 208,
    ledger.STATUS_WAITING: 141,
    ledger.STATUS_DONE: 34,
    ledger.STATUS_CANCELLED: 244,
}
ANSI_RESET = "\033[0m"


def show_task_result(task_id: str) -> int:
    tasks = ledger.load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    task = tasks[task_id]
    if "gate_stop_reason" in task:
        print(f"golem: gate stopped: {task['gate_stop_reason']}", file=sys.stderr)
    if "setup_failure_reason" in task:
        print(f"golem: {task['setup_failure_reason']}", file=sys.stderr)
        return 1
    if task.get("setup_complete"):
        print(f"golem: directory: {task['cwd']}", file=sys.stderr)
        print(f"golem: prompt:\n{task['prompt']}", file=sys.stderr)
    result = task.get("result")
    if not isinstance(result, str):
        raise SystemExit(f"golem: task has no result: {task_id}")
    print_task_result(result)
    return 0


def display_directory(directory: str) -> str:
    try:
        return str(Path("~") / Path(directory).relative_to(Path.home()))
    except ValueError:
        return directory


def display_due_at(due_at: str, today: date) -> str:
    local_due_at = datetime.fromisoformat(due_at).astimezone()
    day_text = f"{local_due_at:%b} {local_due_at.day}, {local_due_at:%Y}"
    if local_due_at.date() == today:
        day_text = "today"
    elif local_due_at.date() == today + timedelta(days=1):
        day_text = "tomorrow"
    time_text = local_due_at.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day_text} {time_text} {local_due_at:%Z}"


def is_color_enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


def display_status(status: str, width: int, *, is_color: bool) -> str:
    padded_status = status.ljust(width)
    if not is_color:
        return padded_status
    return f"\033[38;5;{STATUS_COLORS[status]}m{padded_status}{ANSI_RESET}"


def first_line(text: str) -> str:
    return next(iter(text.splitlines()), "")


def list_reason(task: ledger.Task) -> str:
    reason = task.get("gate_stop_reason") or task.get("setup_failure_reason")
    if reason is not None:
        return first_line(reason)
    if "setup_busy_retry_count" in task and "setup_directory" in task:
        return f"waiting for directory: {task['setup_directory']}"
    return ""


def list_tasks() -> int:
    tasks = ledger.load_tasks()
    if not tasks:
        print("golem: no tasks")
        return 0
    today = datetime.now().astimezone().date()
    rows: list[tuple[str, str, str, str, str, str]] = [
        (
            task_id,
            task["status"],
            display_directory(task["cwd"]),
            display_due_at(task["due_at"], today) if "due_at" in task else "",
            task.get("after", "") if task["status"] == ledger.STATUS_WAITING else "",
            list_reason(task),
        )
        for task_id, task in tasks.items()
    ]
    rows.sort(key=lambda row: (ledger.LIST_STATUS_ORDER.index(row[1]), row[0]))
    headers = ("ID", "STATUS", "DIRECTORY", "DUE AT", "AFTER", "REASON")
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    is_color = is_color_enabled()
    print(
        "  ".join(value.ljust(width) for value, width in zip(headers, widths, strict=True)).rstrip()
    )
    for task_id, status, directory, due_at, after, reason in rows:
        print(
            "  ".join(
                (
                    task_id.ljust(widths[0]),
                    display_status(status, widths[1], is_color=is_color),
                    directory.ljust(widths[2]),
                    due_at.ljust(widths[3]),
                    after.ljust(widths[4]),
                    reason.ljust(widths[5]),
                )
            ).rstrip()
        )
    return 0


def print_task_result(result: str) -> None:
    print(result, end="" if result.endswith("\n") else "\n")
