import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from golem import chains, ledger

STATUS_COLORS = {
    ledger.STATUS_BLOCKED: 214,
    ledger.STATUS_FAILED: 196,
    ledger.STATUS_RUNNING: 39,
    ledger.STATUS_SCHEDULED: 141,
    ledger.STATUS_WAITING: 103,
    ledger.STATUS_FINISHED: 34,
    ledger.STATUS_CANCELLED: 244,
}
DETAIL_LINE_COLOR = 245
ANSI_RESET = "\033[0m"
LIST_HEADERS = ("ID", "LABEL", "STEP", "STATUS", "DIRECTORY", "DETAIL")
DETAIL_LINE_INDENT = "    "
COLUMN_QUEUED = "Queued"
COLUMN_RUNNING = "Running"
COLUMN_REVIEW = "Review"
COLUMN_DONE = "Done"
LIST_COLUMN_ORDER = (COLUMN_REVIEW, COLUMN_RUNNING, COLUMN_QUEUED, COLUMN_DONE)
STATUS_COLUMNS = {
    ledger.STATUS_BLOCKED: COLUMN_REVIEW,
    ledger.STATUS_FAILED: COLUMN_REVIEW,
    ledger.STATUS_RUNNING: COLUMN_RUNNING,
    ledger.STATUS_SCHEDULED: COLUMN_QUEUED,
    ledger.STATUS_WAITING: COLUMN_QUEUED,
}
DONE_THREAD_STATES = {
    ledger.THREAD_STATE_SETTLED,
    ledger.THREAD_STATE_ARCHIVED,
    ledger.THREAD_STATE_DELETED,
}


class ListRow(NamedTuple):
    task_id: str
    label: str
    step: str
    status: str
    directory: str
    detail: str
    column: str | None

    def cells(self) -> tuple[str, str, str, str, str, str]:
        return (self.task_id, self.label, self.step, self.status, self.directory, self.detail)


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


def clock_time(local_time: datetime) -> str:
    return local_time.strftime("%I:%M%p").lstrip("0").lower()


def display_time(timestamp: str, today: date) -> str:
    local_time = datetime.fromisoformat(timestamp).astimezone()
    day_text = f"{local_time:%b} {local_time.day}"
    if local_time.year != today.year:
        day_text = f"{day_text}, {local_time:%Y}"
    if local_time.date() == today:
        day_text = "today"
    elif local_time.date() == today + timedelta(days=1):
        day_text = "tomorrow"
    return f"{day_text} {clock_time(local_time)}"


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


def list_detail(task: ledger.Task, today: date, chain_reason: str) -> str:
    timestamp = detail_timestamp(task)
    status_detail = display_time(timestamp, today) if timestamp is not None else chain_reason
    if task["status"] == ledger.STATUS_WAITING:
        status_detail = task.get("after", "")
    return "; ".join(text for text in (status_detail, list_reason(task)) if text)


def chain_pull_requests(tasks: ledger.Tasks, chain_ids: set[str]) -> list[ledger.PullRequest]:
    pull_requests_by_url = {
        pull_request["url"]: pull_request
        for chain_id in sorted(chain_ids)
        for pull_request in tasks[chain_id].get("pull_requests", [])
    }
    return list(pull_requests_by_url.values())


def last_step_id(tasks: ledger.Tasks, chain_ids: set[str]) -> str:
    return max(
        (
            chain_id
            for chain_id in chain_ids
            if tasks[chain_id]["status"] != ledger.STATUS_CANCELLED
        ),
        key=lambda chain_id: (chains.task_depth(tasks, chain_id), chain_id),
    )


def is_chain_closed(tasks: ledger.Tasks, chain_ids: set[str]) -> bool:
    return any("closed_at" in tasks[chain_id] for chain_id in chain_ids)


def thread_deciding_step_id(tasks: ledger.Tasks, chain_ids: set[str]) -> str | None:
    if is_chain_closed(tasks, chain_ids) or chain_pull_requests(tasks, chain_ids):
        return None
    step_id = last_step_id(tasks, chain_ids)
    return step_id if "t3_code_thread_id" in tasks[step_id] else None


def finished_chain_column(tasks: ledger.Tasks, chain_ids: set[str], today: date) -> tuple[str, str]:
    if is_chain_closed(tasks, chain_ids):
        return COLUMN_DONE, "closed"
    pull_requests = chain_pull_requests(tasks, chain_ids)
    if pull_requests:
        column = COLUMN_DONE if ledger.are_pull_requests_finished(pull_requests) else COLUMN_REVIEW
        return column, pull_request_summary(pull_requests)
    thread_step_id = thread_deciding_step_id(tasks, chain_ids)
    if thread_step_id is not None:
        thread_state = tasks[thread_step_id].get(
            "t3_code_thread_state", ledger.THREAD_STATE_UNSETTLED
        )
        column = COLUMN_DONE if thread_state in DONE_THREAD_STATES else COLUMN_REVIEW
        return column, f"thread {thread_state}"
    finished_at = max(tasks[chain_id].get("finished_at", "") for chain_id in chain_ids)
    if not finished_at:
        return COLUMN_REVIEW, "no PR"
    age_days = (today - datetime.fromisoformat(finished_at).astimezone().date()).days
    column = COLUMN_DONE if age_days >= ledger.CHAIN_DONE_AGE_DAYS else COLUMN_REVIEW
    return column, f"no PR; finished {age_days}d ago"


def chain_column(tasks: ledger.Tasks, chain_ids: set[str], today: date) -> tuple[str | None, str]:
    shown_status = tasks[shown_task_id(tasks, chain_ids)]["status"]
    if shown_status == ledger.STATUS_FINISHED:
        return finished_chain_column(tasks, chain_ids, today)
    return STATUS_COLUMNS.get(shown_status), ""


def shown_task_id(tasks: ledger.Tasks, chain_ids: set[str]) -> str:
    return max(
        chain_ids,
        key=lambda chain_id: (
            -ledger.LIST_STATUS_ORDER.index(tasks[chain_id]["status"]),
            chains.task_depth(tasks, chain_id),
            chain_id,
        ),
    )


def list_row_sort_key(row: ListRow) -> tuple[int, int, str]:
    column_index = (
        LIST_COLUMN_ORDER.index(row.column) if row.column is not None else len(LIST_COLUMN_ORDER)
    )
    return column_index, ledger.LIST_STATUS_ORDER.index(row.status), row.task_id


def list_rows(tasks: ledger.Tasks, today: date) -> list[ListRow]:
    rows: list[ListRow] = []
    for chain_ids in chains.chain_id_sets(tasks):
        shown_id = shown_task_id(tasks, chain_ids)
        shown_task = tasks[shown_id]
        column, chain_reason = chain_column(tasks, chain_ids, today)
        step_count = max(chains.task_depth(tasks, chain_id) for chain_id in chain_ids)
        rows.append(
            ListRow(
                shown_id,
                shown_task.get("label", ""),
                f"{chains.task_depth(tasks, shown_id)}/{step_count}" if len(chain_ids) > 1 else "",
                shown_task["status"],
                shown_task["cwd"],
                list_detail(shown_task, today, chain_reason),
                column,
            )
        )
    rows.sort(key=list_row_sort_key)
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
    rows = [row for row in all_rows if is_showing_all or row.column != COLUMN_DONE]
    hidden_footnote = f"{len(all_rows) - len(rows)} done hidden; --all shows them"
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
