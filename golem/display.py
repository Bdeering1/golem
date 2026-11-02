import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from golem import chains, ledger

STATUS_COLORS = {
    ledger.STATUS_NEEDS_REVIEW: 33,
    ledger.STATUS_FAILED: 196,
    ledger.STATUS_RUNNING: 39,
    ledger.STATUS_SCHEDULED: 208,
    ledger.STATUS_WAITING: 141,
    ledger.STATUS_DONE: 34,
    ledger.STATUS_CANCELLED: 244,
}
DETAIL_LINE_COLOR = 245
ANSI_RESET = "\033[0m"
LIST_HEADERS = ("ID", "STEP", "STATUS", "DIRECTORY", "DETAIL")
DETAIL_LINE_INDENT = "    "


class ListRow(NamedTuple):
    task_id: str
    step: str
    status: str
    directory: str
    detail: str
    is_detail_time: bool
    is_hidden: bool

    def cells(self) -> tuple[str, str, str, str, str]:
        return (self.task_id, self.step, self.status, self.directory, self.detail)


LIST_STATUS_COLUMN = ListRow._fields.index("status")


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


def shortened_directory(directory: str) -> str:
    shortened = f".../{Path(directory).name}"
    return shortened if len(shortened) < len(directory) else directory


def display_time(timestamp: str, today: date) -> str:
    local_time = datetime.fromisoformat(timestamp).astimezone()
    day_text = f"{local_time:%b} {local_time.day}"
    if local_time.year != today.year:
        day_text = f"{day_text}, {local_time:%Y}"
    if local_time.date() == today:
        day_text = "today"
    elif local_time.date() == today + timedelta(days=1):
        day_text = "tomorrow"
    time_text = local_time.strftime("%I:%M%p").lstrip("0").lower()
    return f"{day_text} {time_text}"


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


def detail_timestamp(task: ledger.Task) -> str | None:
    if task["status"] == ledger.STATUS_SCHEDULED:
        return task.get("due_at")
    if task["status"] == ledger.STATUS_RUNNING:
        return task.get("started_at")
    return None


def pull_request_summary(pull_requests: list[ledger.PullRequest]) -> str:
    if not pull_requests:
        return "no PR"
    state_counts = Counter(pull_request["state"] for pull_request in pull_requests)
    if len(state_counts) > 1:
        counts_text = ", ".join(
            f"{state_counts[state]} {state}"
            for state in ledger.PULL_REQUEST_STATES
            if state_counts[state]
        )
        return f"{len(pull_requests)} PRs: {counts_text}"
    state = pull_requests[0]["state"]
    return f"PR {state}" if len(pull_requests) == 1 else f"{len(pull_requests)} PRs {state}"


def list_detail(task: ledger.Task, today: date) -> str:
    timestamp = detail_timestamp(task)
    status_detail = display_time(timestamp, today) if timestamp is not None else ""
    if task["status"] == ledger.STATUS_WAITING:
        status_detail = task.get("after", "")
    if task["status"] == ledger.STATUS_DONE and "pull_requests" in task:
        status_detail = pull_request_summary(task["pull_requests"])
    return "; ".join(text for text in (status_detail, list_reason(task)) if text)


def list_rows(tasks: ledger.Tasks, today: date) -> list[ListRow]:
    rows: list[ListRow] = []
    listed_ids: set[str] = set()
    for task_id in tasks:
        if task_id in listed_ids:
            continue
        chain_ids = chains.chain_task_ids(tasks, task_id)
        listed_ids |= chain_ids
        depths = {chain_id: chains.task_depth(tasks, chain_id) for chain_id in chain_ids}
        shown_id = max(
            chain_ids,
            key=lambda chain_id: (
                -ledger.LIST_STATUS_ORDER.index(tasks[chain_id]["status"]),
                depths[chain_id],
                chain_id,
            ),
        )
        shown_task = tasks[shown_id]
        rows.append(
            ListRow(
                shown_id,
                f"{depths[shown_id]}/{max(depths.values())}" if len(chain_ids) > 1 else "",
                shown_task["status"],
                shown_task["cwd"],
                list_detail(shown_task, today),
                detail_timestamp(shown_task) is not None,
                all(
                    tasks[chain_id]["status"] == ledger.STATUS_DONE
                    and ledger.has_finished_pull_requests(tasks[chain_id])
                    for chain_id in chain_ids
                ),
            )
        )
    rows.sort(key=lambda row: (ledger.LIST_STATUS_ORDER.index(row.status), row.task_id))
    return rows


def detail_line(detail: str, *, is_color: bool) -> str:
    if not is_color:
        return f"{DETAIL_LINE_INDENT}{detail}"
    return f"{DETAIL_LINE_INDENT}\033[38;5;{DETAIL_LINE_COLOR}m{detail}{ANSI_RESET}"


def table_lines(
    rows: list[ListRow], *, is_color: bool, is_detail_on_own_line: bool = False
) -> list[str]:
    headers = LIST_HEADERS[:-1] if is_detail_on_own_line else LIST_HEADERS
    widths = [
        max(len(header), *(len(row.cells()[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    lines = ["  ".join(value.ljust(width) for value, width in zip(headers, widths, strict=True))]
    for row in rows:
        cells = [
            value.ljust(width)
            for value, width in zip(row.cells()[: len(headers)], widths, strict=True)
        ]
        cells[LIST_STATUS_COLUMN] = display_status(
            row.status, widths[LIST_STATUS_COLUMN], is_color=is_color
        )
        lines.append("  ".join(cells))
        if is_detail_on_own_line and row.detail:
            lines.append(detail_line(row.detail, is_color=is_color))
    return [line.rstrip() for line in lines]


def table_width(rows: list[ListRow], *, is_detail_on_own_line: bool) -> int:
    lines = table_lines(rows, is_color=False, is_detail_on_own_line=is_detail_on_own_line)
    return max(len(line) for line in lines)


def list_tasks(*, is_showing_all: bool) -> int:
    tasks = ledger.load_tasks()
    if not tasks:
        print("golem: no tasks")
        return 0
    all_rows = list_rows(tasks, datetime.now().astimezone().date())
    rows = [row for row in all_rows if is_showing_all or not row.is_hidden]
    hidden_footnote = f"{len(all_rows) - len(rows)} hidden; --all shows them"
    if not rows:
        print(hidden_footnote)
        return 0
    full_rows = [row._replace(directory=display_directory(row.directory)) for row in rows]
    shortened_rows = [
        row._replace(directory=shortened_directory(row.directory)) for row in full_rows
    ]
    layouts = [
        (full_rows, False),
        (shortened_rows, False),
        (full_rows, True),
        (shortened_rows, True),
    ]
    if not sys.stdout.isatty():
        layouts = layouts[:1]
    terminal_width = shutil.get_terminal_size().columns
    table_rows, is_detail_on_own_line = next(
        (
            (layout_rows, is_own_line)
            for layout_rows, is_own_line in layouts
            if table_width(layout_rows, is_detail_on_own_line=is_own_line) <= terminal_width
        ),
        layouts[-1],
    )
    for line in table_lines(
        table_rows, is_color=is_color_enabled(), is_detail_on_own_line=is_detail_on_own_line
    ):
        print(line)
    if len(rows) < len(all_rows):
        print(f"\n{hidden_footnote}")
    return 0


def print_task_result(result: str) -> None:
    print(result, end="" if result.endswith("\n") else "\n")
