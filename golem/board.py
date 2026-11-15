import contextlib
import curses
import locale
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from golem import chains, display, ledger, progress_log

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import FrameType

MIN_COLUMN_WIDTH = 20
COLUMN_GAP = 2
CARD_INDENT = 2
CARD_LINE_COUNT = 2
CARD_PITCH = CARD_LINE_COUNT + 1
COMPACT_CARD_PITCH = CARD_LINE_COUNT
TAB_LINE_Y = 1
MIN_DETAIL_LINE_COUNT = 4
MAX_DETAIL_LINE_COUNT = 6
SHRUNK_DETAIL_LINE_COUNT = 3
GROWN_DETAIL_LINE_COUNT = 14
DAYS_PER_YEAR = 365
NARROW_DETAIL_LINE_COUNT = 4
DETAIL_HEIGHT_DIVISOR = 6
MIN_DETAIL_HEIGHT = 20
TICK_MILLISECONDS = 250
ESCAPE_DELAY_MILLISECONDS = 25
FLASH_SECONDS = 3
CHANGE_HIGHLIGHT_SECONDS = 10
ACTIVITY_TAIL_BYTES = 16384
ACTIVITY_LINE_COUNT = 200
INTERRUPTED_EXIT_STATUS = 130
MIN_COLOR_COUNT = 256
MUTED_COLOR = 245
FAINT_COLOR = 239
WIDE_REP_FIX_NCURSES_VERSION = (6, 0, 20170729)
REP_LINE_PREFIX = b"\trep="
TOOL_LINE_PREFIX = "[tool] "
FINISHED_AGE_SEPARATOR = "; finished "
TERMINFO_VARIABLE = "TERMINFO"
TERMINFO_SOURCE_NAME = "terminal.terminfo"
BOARD_TITLE = "golem"
NOT_A_TERMINAL_MESSAGE = "golem: board needs a terminal; use golem list"
HELP_CLOSE_HINT = " any key closes "
MIN_CARD_REASON_WIDTH = 8
CONFIRM_ANSWER_TEXT = " yes  any other key: no"
CONFIRM_SHORT_ANSWER_TEXT = "/N"
HELP_SECTION_GAP = 4
TASK_VIEW_INDENT = "  "
RETURN_PROMPT = "golem: press Enter to return to the board"
GOLEM_COMMAND = (sys.executable, "-m", "golem")
GOLEM_PREFIX = "golem: "
OPEN_COMMAND = "open"
COPY_COMMAND = "pbcopy"
STYLE_PLAIN = "plain"
STYLE_BOLD = "bold"
STYLE_DIM = "dim"
STYLE_SELECTED = "selected"
STYLE_ATTRIBUTES = {
    STYLE_PLAIN: curses.A_NORMAL,
    STYLE_BOLD: curses.A_BOLD,
    STYLE_DIM: curses.A_DIM,
    STYLE_SELECTED: curses.A_BOLD | curses.A_REVERSE,
}
VIEW_BOARD = "board"
VIEW_TASK = "task"
ACTION_PICKUP = "pickup"
ACTION_WATCH = "watch"
ACTION_OPEN_PULL_REQUEST = "pr"
ACTION_CLOSE = "close"
ACTION_CANCEL = "cancel"
ACTION_COPY = "copy"
ACTION_RETRY = "retry"
ACTION_RERUN_GATE = "continue"
ESCAPE_KEY = 27
CONTROL_D = 4
CONTROL_U = 21
ENTER_KEYS = {ord("\n"), ord("\r"), curses.KEY_ENTER}
LEFT_KEYS = {ord("h"), curses.KEY_LEFT, curses.KEY_BTAB}
RIGHT_KEYS = {ord("l"), curses.KEY_RIGHT, ord("\t")}
DOWN_KEYS = {ord("j"), curses.KEY_DOWN}
UP_KEYS = {ord("k"), curses.KEY_UP}
FIRST_KEYS = {ord("g"), curses.KEY_HOME}
LAST_KEYS = {ord("G"), curses.KEY_END}
PAGE_DOWN_KEYS = {curses.KEY_NPAGE, CONTROL_D, ord(" ")}
PAGE_UP_KEYS = {curses.KEY_PPAGE, CONTROL_U}
BACKSPACE_KEYS = {curses.KEY_BACKSPACE, 127, 8}
BACK_KEYS = {ESCAPE_KEY, ord("q"), *BACKSPACE_KEYS, *ENTER_KEYS}
FILTER_KEY = ord("/")
PRINTABLE_KEYS = range(32, 127)
ACTION_KEYS = {
    ACTION_PICKUP: "p",
    ACTION_WATCH: "w",
    ACTION_OPEN_PULL_REQUEST: "o",
    ACTION_RETRY: "r",
    ACTION_RERUN_GATE: "r",
    ACTION_CLOSE: "c",
    ACTION_CANCEL: "x",
    ACTION_COPY: "y",
}
CONFIRMED_ACTIONS = {ACTION_CLOSE, ACTION_CANCEL, ACTION_RETRY}
ACTION_LABELS = {
    ACTION_PICKUP: "pickup",
    ACTION_WATCH: "log",
    ACTION_OPEN_PULL_REQUEST: "PR",
    ACTION_RETRY: "retry",
    ACTION_RERUN_GATE: "re-run gate",
    ACTION_CLOSE: "close",
    ACTION_CANCEL: "cancel",
    ACTION_COPY: "copy id",
}
ACTION_DONE_VERBS = {ACTION_CLOSE: "Closed", ACTION_CANCEL: "Cancelled"}
FOOTER_HIDDEN_ACTIONS = {ACTION_COPY}
STOPPED_STATUSES = {ledger.STATUS_BLOCKED, ledger.STATUS_FAILED}
REASON_FIRST_STATUSES = {*STOPPED_STATUSES, ledger.STATUS_WAITING}
LOUD_STATUSES = {*STOPPED_STATUSES, ledger.STATUS_RUNNING}
PICKUP_STATUSES = {ledger.STATUS_BLOCKED, ledger.STATUS_FAILED, ledger.STATUS_FINISHED}
CANCEL_STATUSES = {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}
RETRY_STATUSES = {ledger.STATUS_FAILED, ledger.STATUS_CANCELLED}
EMPTY_COLUMN_TEXTS = {
    display.COLUMN_QUEUED: "Nothing queued",
    display.COLUMN_RUNNING: "Nothing running",
    display.COLUMN_REVIEW: "Nothing to review",
    display.COLUMN_DONE: "Nothing done yet",
}
NO_MATCHES_TEXT = "No matches"
THREAD_STATE_TEXTS = {"thread unsettled": "thread open, no PR"}
EMPTY_BOARD_LINES = ("No tasks yet", "Queue one with", 'golem run <id> "<prompt>"')
REASON_LINE_COUNT = 2
UNAVAILABLE_ACTION_TEXTS = {
    ACTION_PICKUP: "Only blocked, failed or finished tasks can be picked up",
    ACTION_WATCH: "This task hasn't started, so it has no log yet",
    ACTION_OPEN_PULL_REQUEST: "This task has no pull request",
    ACTION_RETRY: "Only failed, cancelled or blocked tasks can be retried",
    ACTION_CLOSE: "Only finished tasks in Review can be closed",
    ACTION_CANCEL: "Only queued tasks can be cancelled",
    ACTION_COPY: "",
}


class BoardColumn(NamedTuple):
    title: str
    color: int


class Segment(NamedTuple):
    y: int
    x: int
    text: str
    style: str = STYLE_PLAIN
    color: int | None = None


class Glyphs(NamedTuple):
    horizontal: str
    heavy_horizontal: str
    focus_marker: str
    ellipsis: str
    separator: str
    up: str
    down: str
    left: str
    right: str
    column_keys: str
    select_keys: str
    enter_key: str
    tool: str
    spinner: str
    status_marks: dict[str, str]
    corners: str
    vertical: str
    cursor: str
    new_mark: str


class BoardSnapshot(NamedTuple):
    rows: list[display.ListRow]
    tasks: ledger.Tasks
    activity: dict[str, list[str]]
    unfiltered_rows: list[display.ListRow]


class FrameContext(NamedTuple):
    snapshot: BoardSnapshot
    glyphs: Glyphs
    now: datetime
    changed_task_ids: frozenset[str] = frozenset()


class BoardLayout(NamedTuple):
    fit_count: int
    column_width: int
    header_y: int
    body_top: int
    body_height: int
    detail_line_count: int
    card_pitch: int = CARD_PITCH

    @property
    def visible_card_count(self) -> int:
        # Without a detail pane, one row stays free under the last card for the "more" marker.
        marker_row_count = 0 if self.detail_line_count else 1
        free_row_count = self.body_height - marker_row_count + self.card_pitch - CARD_LINE_COUNT
        return max(1, free_row_count // self.card_pitch)


type LedgerStat = tuple[int, int, int] | None
type Part = tuple[str, str, int | None]

BOARD_COLUMNS = (
    BoardColumn(display.COLUMN_QUEUED, display.STATUS_COLORS[ledger.STATUS_SCHEDULED]),
    BoardColumn(display.COLUMN_RUNNING, display.STATUS_COLORS[ledger.STATUS_RUNNING]),
    BoardColumn(display.COLUMN_REVIEW, display.STATUS_COLORS[ledger.STATUS_BLOCKED]),
    BoardColumn(display.COLUMN_DONE, display.STATUS_COLORS[ledger.STATUS_FINISHED]),
)
COLOR_PAIRS = {
    color: pair
    for pair, color in enumerate(
        sorted(
            {
                *display.STATUS_COLORS.values(),
                *(column.color for column in BOARD_COLUMNS),
                MUTED_COLOR,
                FAINT_COLOR,
            }
        ),
        start=1,
    )
}
UNICODE_GLYPHS = Glyphs(
    horizontal="─",
    heavy_horizontal="━",
    focus_marker="▌",
    ellipsis="…",
    separator="·",
    up="↑",
    down="↓",
    left="←",
    right="→",
    column_keys="←→",
    select_keys="↑↓",
    enter_key="⏎",
    tool="▸",
    spinner="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    status_marks={
        ledger.STATUS_SCHEDULED: "◷",
        ledger.STATUS_WAITING: "◌",
        ledger.STATUS_BLOCKED: "■",
        ledger.STATUS_FAILED: "✗",
        ledger.STATUS_FINISHED: "✓",
        ledger.STATUS_CANCELLED: "○",
    },
    corners="╭╮╰╯",
    vertical="│",
    cursor="▏",
    new_mark="●",
)
ASCII_GLYPHS = Glyphs(
    horizontal="-",
    heavy_horizontal="=",
    focus_marker=">",
    ellipsis="~",
    separator="|",
    up="^",
    down="v",
    left="<",
    right=">",
    column_keys="h/l",
    select_keys="j/k",
    enter_key="enter",
    tool=">",
    spinner="|/-\\",
    status_marks={
        ledger.STATUS_SCHEDULED: "o",
        ledger.STATUS_WAITING: ".",
        ledger.STATUS_BLOCKED: "!",
        ledger.STATUS_FAILED: "x",
        ledger.STATUS_FINISHED: "+",
        ledger.STATUS_CANCELLED: "-",
    },
    corners="++++",
    vertical="|",
    cursor="_",
    new_mark="*",
)


@dataclass
class BoardState:
    focus_index: int = 0
    first_column_index: int = 0
    is_showing_all: bool = False
    selected_indices: list[int] = field(default_factory=lambda: [0] * len(BOARD_COLUMNS))
    selected_task_ids: list[str | None] = field(default_factory=lambda: [None] * len(BOARD_COLUMNS))
    scroll_offsets: list[int] = field(default_factory=lambda: [0] * len(BOARD_COLUMNS))
    view: str = VIEW_BOARD
    task_scroll: int = 0
    pending_action: str | None = None
    flash_text: str = ""
    flash_until: float = 0.0
    filter_text: str = ""
    is_filtering: bool = False
    is_following: bool = False
    is_showing_help: bool = False
    help_scroll: int = 0
    help_scroll_limit: int = 0
    changed_until: dict[str, float] = field(default_factory=dict[str, float])


def recent_activity(session_id: str) -> list[str]:
    try:
        with progress_log.progress_path(session_id).open("rb") as progress_file:
            size = progress_file.seek(0, os.SEEK_END)
            progress_file.seek(max(0, size - ACTIVITY_TAIL_BYTES))
            tail = progress_file.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = tail.splitlines()
    if len(tail.encode()) >= ACTIVITY_TAIL_BYTES:
        lines = lines[1:]
    return [" ".join(line.split()) for line in lines if line.strip()][-ACTIVITY_LINE_COUNT:]


def timestamp_seconds(timestamp: str | None) -> float:
    return datetime.fromisoformat(timestamp).timestamp() if timestamp else 0.0


def board_sort_key(row: display.ListRow, tasks: ledger.Tasks) -> tuple[int, int, float, str]:
    task = tasks[row.task_id]
    titles = [board_column.title for board_column in BOARD_COLUMNS]
    column_index = titles.index(row.column) if row.column in titles else len(titles)
    status_index = ledger.LIST_STATUS_ORDER.index(row.status)
    if row.column == display.COLUMN_QUEUED:
        moment = timestamp_seconds(task.get("due_at"))
    elif row.column == display.COLUMN_RUNNING:
        moment = timestamp_seconds(task.get("started_at"))
    else:
        moment = -timestamp_seconds(task.get("finished_at"))
    return column_index, status_index, moment, row.task_id


def board_snapshot(tasks: ledger.Tasks, now: datetime) -> BoardSnapshot:
    rows = sorted(display.list_rows(tasks, now.date()), key=lambda row: board_sort_key(row, tasks))
    activity = {
        row.task_id: recent_activity(tasks[row.task_id]["session_id"])
        for row in rows
        if row.status == ledger.STATUS_RUNNING
    }
    return BoardSnapshot(rows, tasks, activity, rows)


def is_matching(row: display.ListRow, task: ledger.Task, query: str) -> bool:
    searched_text = f"{row.task_id} {row.directory} {task.get('prompt', '')}"
    return query.casefold() in searched_text.casefold()


def filtered_snapshot(snapshot: BoardSnapshot, state: BoardState) -> BoardSnapshot:
    if not state.filter_text:
        return snapshot
    rows = [
        row
        for row in snapshot.rows
        if is_matching(row, snapshot.tasks[row.task_id], state.filter_text)
    ]
    return snapshot._replace(rows=rows)


def visible_columns(*, is_showing_all: bool) -> tuple[BoardColumn, ...]:
    return BOARD_COLUMNS if is_showing_all else BOARD_COLUMNS[:-1]


def rows_by_column(
    rows: list[display.ListRow], *, is_showing_all: bool
) -> list[tuple[BoardColumn, list[display.ListRow]]]:
    return [
        (board_column, [row for row in rows if row.column == board_column.title])
        for board_column in visible_columns(is_showing_all=is_showing_all)
    ]


def detail_line_count(height: int) -> int:
    if height < MIN_DETAIL_HEIGHT:
        return 0
    return max(MIN_DETAIL_LINE_COUNT, min(MAX_DETAIL_LINE_COUNT, height // DETAIL_HEIGHT_DIVISOR))


def attention_column_index(rows: list[display.ListRow]) -> int:
    columns = rows_by_column(rows, is_showing_all=False)
    attention_order = (display.COLUMN_REVIEW, display.COLUMN_RUNNING, display.COLUMN_QUEUED)
    titles = [board_column.title for board_column, _ in columns]
    for title in attention_order:
        if columns[titles.index(title)][1]:
            return titles.index(title)
    return 0


def board_layout(
    width: int, height: int, column_count: int, largest_column_size: int = 0
) -> BoardLayout:
    fit_count = min(column_count, max(1, (width + COLUMN_GAP) // (MIN_COLUMN_WIDTH + COLUMN_GAP)))
    column_width = (width - (fit_count - 1) * COLUMN_GAP) // fit_count
    header_y = TAB_LINE_Y + 1 if 1 < fit_count < column_count else TAB_LINE_Y
    free_row_count = height - 1 - header_y - 2
    roomy_row_count = largest_column_size * CARD_PITCH
    compact_row_count = largest_column_size * COMPACT_CARD_PITCH
    line_count = detail_line_count(height)
    if fit_count == 1:
        line_count = min(line_count, NARROW_DETAIL_LINE_COUNT)
    if line_count and free_row_count - roomy_row_count - 1 >= SHRUNK_DETAIL_LINE_COUNT:
        line_count = min(GROWN_DETAIL_LINE_COUNT, free_row_count - roomy_row_count - 1)
    elif line_count and free_row_count - compact_row_count - 1 >= SHRUNK_DETAIL_LINE_COUNT:
        line_count = min(line_count, free_row_count - compact_row_count - 1)
    detail_height = line_count + 1 if line_count else 0
    is_roomy = roomy_row_count <= free_row_count - detail_height
    body_top = header_y + 2 + (1 if is_roomy else 0)
    body_height = height - 1 - detail_height - body_top
    pitch = CARD_PITCH if is_roomy else COMPACT_CARD_PITCH
    return BoardLayout(fit_count, column_width, header_y, body_top, body_height, line_count, pitch)


def snapshot_layout(
    snapshot: BoardSnapshot, width: int, height: int, state: BoardState
) -> BoardLayout:
    columns = rows_by_column(snapshot.unfiltered_rows, is_showing_all=state.is_showing_all)
    largest_column_size = max(len(column_rows) for _, column_rows in columns)
    return board_layout(width, height, len(columns), largest_column_size)


def fit_state_to_layout(
    snapshot: BoardSnapshot, width: int, height: int, state: BoardState
) -> None:
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    layout = snapshot_layout(snapshot, width, height, state)
    state.focus_index = min(state.focus_index, len(columns) - 1)
    state.first_column_index = min(
        max(
            state.focus_index - layout.fit_count + 1,
            min(state.first_column_index, state.focus_index),
        ),
        len(columns) - layout.fit_count,
    )
    for column_index, (_, column_rows) in enumerate(columns):
        column_task_ids = [row.task_id for row in column_rows]
        selected_task_id = state.selected_task_ids[column_index]
        selected_index = min(
            max(state.selected_indices[column_index], 0), max(0, len(column_rows) - 1)
        )
        if selected_task_id in column_task_ids:
            selected_index = column_task_ids.index(selected_task_id)
        scroll_offset = min(state.scroll_offsets[column_index], selected_index)
        scroll_offset = max(scroll_offset, selected_index - layout.visible_card_count + 1)
        scroll_offset = min(scroll_offset, max(0, len(column_rows) - layout.visible_card_count))
        state.selected_indices[column_index] = selected_index
        state.selected_task_ids[column_index] = (
            column_task_ids[selected_index] if column_task_ids else None
        )
        state.scroll_offsets[column_index] = scroll_offset


def selected_row(snapshot: BoardSnapshot, state: BoardState) -> display.ListRow | None:
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    focused_rows = columns[min(state.focus_index, len(columns) - 1)][1]
    if not focused_rows:
        return None
    return focused_rows[min(state.selected_indices[state.focus_index], len(focused_rows) - 1)]


def fitted(text: str, width: int, glyphs: Glyphs) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + glyphs.ellipsis


def fitted_task_id(task_id: str, width: int, glyphs: Glyphs) -> str:
    tail = task_id[task_id.rfind("-") :] if "-" in task_id else ""
    if len(task_id) <= width or not tail or len(tail) > width // 2:
        return fitted(task_id, width, glyphs)
    head = task_id[: width - len(tail) - len(glyphs.ellipsis)].rstrip("-")
    return f"{head}{glyphs.ellipsis}{tail}"


def duration_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if not minutes:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    days, hours = divmod(hours, 24)
    if not days:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if days >= DAYS_PER_YEAR:
        return f"{days // DAYS_PER_YEAR}y"
    return f"{days}d {hours}h" if hours else f"{days}d"


def seconds_since(timestamp: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(timestamp)).total_seconds()


def when_text(task: ledger.Task, now: datetime) -> str:
    status = task["status"]
    due_at = task.get("due_at")
    started_at = task.get("started_at")
    finished_at = task.get("finished_at")
    if status == ledger.STATUS_SCHEDULED and due_at is not None:
        seconds = -seconds_since(due_at, now)
        verb = "resumes" if progress_log.is_usage_limit_pause(task) else "in"
        return f"{verb} {duration_text(seconds)}" if seconds > 0 else "due now"
    if status == ledger.STATUS_RUNNING and started_at is not None:
        return duration_text(seconds_since(started_at, now))
    if status in PICKUP_STATUSES and finished_at is not None:
        return f"{duration_text(seconds_since(finished_at, now))} ago"
    return ""


def activity_text(line: str, glyphs: Glyphs) -> str:
    if line.startswith(TOOL_LINE_PREFIX):
        return f"{glyphs.tool} {line.removeprefix(TOOL_LINE_PREFIX)}"
    return line


def pull_request_text(pull_requests: list[ledger.PullRequest]) -> str:
    labels = [
        f"#{pull_request['url'].rstrip('/').rsplit('/', 1)[-1]} {pull_request['state']}"
        for pull_request in pull_requests
    ]
    return f"PR {labels[0]}" if len(labels) == 1 else f"PRs {', '.join(labels)}"


def row_reason(row: display.ListRow, snapshot: BoardSnapshot) -> str:
    task = snapshot.tasks[row.task_id]
    if row.status in {ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING}:
        return display.list_reason(task).replace(str(Path.home()), "~")
    if row.status == ledger.STATUS_WAITING:
        return f"after {row.detail}" if row.detail else ""
    if row.status != ledger.STATUS_FINISHED:
        return row.detail
    pull_requests = chain_pull_requests(snapshot, row.task_id)
    if pull_requests:
        return pull_request_text(pull_requests)
    detail = row.detail.split(FINISHED_AGE_SEPARATOR)[0]
    return THREAD_STATE_TEXTS.get(detail, detail)


def short_age_text(timestamp: str | None, now: datetime) -> str:
    if timestamp is None:
        return ""
    return duration_text(seconds_since(timestamp, now)).split()[0]


def card_detail(row: display.ListRow, context: FrameContext, width: int) -> str:
    snapshot, glyphs, now, _ = context
    task = snapshot.tasks[row.task_id]
    activity = snapshot.activity.get(row.task_id)
    reason = activity_text(activity[-1], glyphs) if activity else row_reason(row, snapshot)
    repository = Path(row.directory).name
    if row.status in {ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING}:
        time_text = when_text(task, now)
    elif row.status == ledger.STATUS_WAITING:
        time_text = ""
    else:
        time_text = short_age_text(task.get("finished_at") or task.get("started_at"), now)
    separator = f" {glyphs.separator} "
    facts = separator.join(part for part in (time_text, repository) if part)
    if not reason:
        return facts
    if row.status in REASON_FIRST_STATUSES:
        options = [
            separator.join(part for part in parts if part)
            for parts in ((time_text, repository, reason), (time_text, reason))
        ]
        return next((option for option in options if len(option) <= width), reason)
    if not facts:
        return reason
    if width - len(facts) - len(separator) < MIN_CARD_REASON_WIDTH:
        return facts
    return f"{facts}{separator}{reason}"


def status_mark(status: str, glyphs: Glyphs, now: datetime) -> str:
    if status == ledger.STATUS_RUNNING:
        frame = int(now.timestamp() * 1000) // TICK_MILLISECONDS
        return glyphs.spinner[frame % len(glyphs.spinner)]
    return glyphs.status_marks[status]


def is_new_for_review(snapshot: BoardSnapshot, row: display.ListRow) -> bool:
    return row.column == display.COLUMN_REVIEW and chains.is_ready_to_open(
        snapshot.tasks, row.task_id
    )


def card_segments(
    y: int,
    x: int,
    width: int,
    row: display.ListRow,
    context: FrameContext,
    *,
    is_selected: bool,
) -> list[Segment]:
    snapshot, glyphs, now, changed_task_ids = context
    color = display.STATUS_COLORS[row.status]
    right_parts: list[Part] = [(row.step, STYLE_DIM, None)] if row.step else []
    if is_new_for_review(snapshot, row):
        right_parts += [(" " if right_parts else "", STYLE_PLAIN, None)]
        right_parts += [(glyphs.new_mark, STYLE_BOLD, color)]
    right_width = parts_width(right_parts) + (1 if right_parts else 0)
    title = fitted_task_id(row.task_id, width - CARD_INDENT, glyphs)
    detail_width = width - CARD_INDENT - right_width
    detail = fitted(card_detail(row, context, detail_width), detail_width, glyphs)
    mark = status_mark(row.status, glyphs, now)
    if is_selected:
        right_text = "".join(text for text, _, _ in right_parts)
        second_line = f"{' ' * CARD_INDENT}{detail}".ljust(width - len(right_text)) + right_text
        return [
            Segment(y, x, f"{mark} {title}".ljust(width), STYLE_SELECTED),
            Segment(y + 1, x, second_line, STYLE_SELECTED),
        ]
    is_done = row.column == display.COLUMN_DONE
    is_changed = row.task_id in changed_task_ids
    is_loud = row.status in LOUD_STATUSES or is_changed or is_new_for_review(snapshot, row)
    title_color = color if is_changed else None
    title_style = STYLE_BOLD if is_loud and not is_done else STYLE_PLAIN
    return [
        Segment(y, x, mark, STYLE_BOLD, MUTED_COLOR if is_done else color),
        Segment(y, x + CARD_INDENT, title, title_style, title_color),
        Segment(y + 1, x + CARD_INDENT, detail, STYLE_DIM),
        *part_segments(y + 1, x + width - parts_width(right_parts), right_parts),
    ]


def column_segments(
    layout: BoardLayout,
    column_index: int,
    column: tuple[BoardColumn, list[display.ListRow]],
    state: BoardState,
    context: FrameContext,
) -> list[Segment]:
    glyphs = context.glyphs
    board_column, column_rows = column
    x = (column_index - state.first_column_index) * (layout.column_width + COLUMN_GAP)
    is_focused = column_index == state.focus_index
    rule = glyphs.heavy_horizontal if is_focused else glyphs.horizontal
    rule_color = board_column.color if is_focused else FAINT_COLOR
    segments = [Segment(layout.header_y + 1, x, rule * layout.column_width, color=rule_color)]
    if layout.fit_count > 1:
        header_parts = column_header_parts(
            board_column, len(column_rows), glyphs, is_focused=is_focused
        )
        segments += part_segments(layout.header_y, x, header_parts)
    if not context.snapshot.rows:
        return segments
    if not column_rows:
        empty_text = (
            NO_MATCHES_TEXT if state.filter_text else EMPTY_COLUMN_TEXTS[board_column.title]
        )
        empty_text = fitted(empty_text, layout.column_width - 2, glyphs)
        return [*segments, Segment(layout.body_top, x + 2, empty_text, STYLE_DIM)]
    scroll_offset = state.scroll_offsets[column_index]
    if scroll_offset:
        more_text = f" {glyphs.up} {scroll_offset} more "
        segments.append(Segment(layout.header_y + 1, x + 1, more_text, STYLE_DIM))
    shown_rows = column_rows[scroll_offset : scroll_offset + layout.visible_card_count]
    for card_index, row in enumerate(shown_rows):
        is_selected = (
            is_focused and scroll_offset + card_index == state.selected_indices[column_index]
        )
        card_y = layout.body_top + card_index * layout.card_pitch
        segments += card_segments(
            card_y, x, layout.column_width, row, context, is_selected=is_selected
        )
    return segments


def column_header_parts(
    board_column: BoardColumn, row_count: int, glyphs: Glyphs, *, is_focused: bool
) -> list[Part]:
    marker = f"{glyphs.focus_marker} " if is_focused else "  "
    return [
        (marker, STYLE_PLAIN, board_column.color),
        (board_column.title, STYLE_BOLD if is_focused else STYLE_PLAIN, board_column.color),
        (f" {row_count}", STYLE_DIM, None),
    ]


def more_below_segments(
    layout: BoardLayout,
    columns: list[tuple[BoardColumn, list[display.ListRow]]],
    state: BoardState,
    glyphs: Glyphs,
) -> list[Segment]:
    marker_y = layout.body_top + layout.body_height - (0 if layout.detail_line_count else 1)
    segments: list[Segment] = []
    for offset in range(layout.fit_count):
        column_index = state.first_column_index + offset
        shown_count = len(columns[column_index][1]) - state.scroll_offsets[column_index]
        hidden_count = shown_count - layout.visible_card_count
        if hidden_count > 0:
            x = offset * (layout.column_width + COLUMN_GAP) + 1
            segments.append(Segment(marker_y, x, f" {glyphs.down} {hidden_count} more ", STYLE_DIM))
    return segments


def parts_width(parts: list[Part]) -> int:
    return sum(len(text) for text, _, _ in parts)


def part_segments(y: int, x: int, parts: list[Part]) -> list[Segment]:
    segments: list[Segment] = []
    for text, style, color in parts:
        segments.append(Segment(y, x, text, style, color))
        x += len(text)
    return segments


def joined_parts(tokens: list[list[Part]], arrow_index: int) -> list[Part]:
    parts: list[Part] = []
    for token_index, token in enumerate(tokens):
        if token_index:
            is_beside_arrow = arrow_index in {token_index, token_index - 1}
            parts.append((" " if is_beside_arrow else "  ", STYLE_PLAIN, None))
        parts += token
    return parts


def hidden_side_options(
    hidden_columns: list[tuple[BoardColumn, list[display.ListRow]]],
    glyphs: Glyphs,
    *,
    is_left: bool,
) -> list[list[Part]]:
    options: list[list[Part]] = []
    for shown_count in range(len(hidden_columns), -1, -1):
        folded_count = len(hidden_columns) - shown_count
        shown_columns = hidden_columns[folded_count:] if is_left else hidden_columns[:shown_count]
        labels: list[list[Part]] = [
            [
                (board_column.title, STYLE_PLAIN, board_column.color),
                (f" {len(column_rows)}", STYLE_DIM, None),
            ]
            for board_column, column_rows in shown_columns
        ]
        folded: list[list[Part]] = [[(f"+{folded_count}", STYLE_DIM, None)]] if folded_count else []
        if is_left:
            left_arrow: list[Part] = [(glyphs.left, STYLE_DIM, None)]
            options.append(joined_parts([left_arrow, *folded, *labels], 0))
        else:
            right_arrow: list[Part] = [(glyphs.right, STYLE_DIM, None)]
            right_tokens = [*labels, *folded, right_arrow]
            options.append(joined_parts(right_tokens, len(right_tokens) - 1))
    return options


def tab_segments(
    y: int,
    width: int,
    columns: list[tuple[BoardColumn, list[display.ListRow]]],
    layout: BoardLayout,
    state: BoardState,
    glyphs: Glyphs,
    *,
    middle_parts: list[Part],
) -> list[Segment]:
    last_shown_index = state.first_column_index + layout.fit_count
    hidden_sides = [columns[: state.first_column_index], columns[last_shown_index:]]
    no_hidden_options: list[list[Part]] = [[]]
    left_options, right_options = [
        hidden_side_options(hidden_columns, glyphs, is_left=is_left)
        if hidden_columns
        else no_hidden_options
        for hidden_columns, is_left in zip(hidden_sides, (True, False), strict=True)
    ]
    left_index = right_index = 0
    middle_width = parts_width(middle_parts)
    while (
        parts_width(left_options[left_index])
        + (2 if left_options[left_index] and middle_width else 0)
        + middle_width
        + parts_width(right_options[right_index])
        + 2
        >= width
    ):
        can_shrink_left = left_index < len(left_options) - 1
        can_shrink_right = right_index < len(right_options) - 1
        if not can_shrink_left and not can_shrink_right:
            break
        is_left_wider = parts_width(left_options[left_index]) > parts_width(
            right_options[right_index]
        )
        if can_shrink_left and (is_left_wider or not can_shrink_right):
            left_index += 1
        else:
            right_index += 1
    left_parts, right_parts = left_options[left_index], right_options[right_index]
    middle_x = parts_width(left_parts) + (2 if left_parts else 0)
    return [
        *part_segments(y, 0, left_parts),
        *part_segments(y, middle_x, middle_parts),
        *part_segments(y, width - 1 - parts_width(right_parts), right_parts),
    ]


def summary_options(snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime) -> list[list[Part]]:
    columns = dict(rows_by_column(snapshot.unfiltered_rows, is_showing_all=True))
    counts = {board_column.title: len(column_rows) for board_column, column_rows in columns.items()}
    review_statuses = [row.status for row in columns[BOARD_COLUMNS[2]]]
    running_count = counts[display.COLUMN_RUNNING]
    running_mark = (
        status_mark(ledger.STATUS_RUNNING, glyphs, now) if running_count else glyphs.spinner[0]
    )
    items = [
        (running_mark, running_count, "running", ledger.STATUS_RUNNING, True),
        (
            glyphs.status_marks[ledger.STATUS_BLOCKED],
            review_statuses.count(ledger.STATUS_BLOCKED),
            "blocked",
            ledger.STATUS_BLOCKED,
            False,
        ),
        (
            glyphs.status_marks[ledger.STATUS_FAILED],
            review_statuses.count(ledger.STATUS_FAILED),
            "failed",
            ledger.STATUS_FAILED,
            False,
        ),
        (
            glyphs.status_marks[ledger.STATUS_FINISHED],
            review_statuses.count(ledger.STATUS_FINISHED),
            "finished",
            ledger.STATUS_FINISHED,
            False,
        ),
        (
            glyphs.status_marks[ledger.STATUS_SCHEDULED],
            counts[display.COLUMN_QUEUED],
            "queued",
            ledger.STATUS_SCHEDULED,
            True,
        ),
    ]
    worded: list[Part] = []
    numbered: list[Part] = []
    for mark, count, label, status, is_always_shown in items:
        if not count and not is_always_shown:
            continue
        mark_color = display.STATUS_COLORS[status] if count else MUTED_COLOR
        count_style = STYLE_PLAIN if count else STYLE_DIM
        worded += [(f"  {mark} ", STYLE_PLAIN, mark_color), (f"{count} {label}", count_style, None)]
        numbered += [(f" {mark}", STYLE_PLAIN, mark_color), (str(count), count_style, None)]
    return [worded, numbered, []]


def title_segments(
    width: int, snapshot: BoardSnapshot, state: BoardState, glyphs: Glyphs, now: datetime
) -> list[Segment]:
    right_text = f"/{state.filter_text}" if state.filter_text else display.clock_time(now)
    right_style = STYLE_BOLD if state.filter_text else STYLE_DIM
    right_color = BOARD_COLUMNS[1].color if state.filter_text else None
    title_parts: list[Part] = [(BOARD_TITLE, STYLE_BOLD, None)]
    options = [
        (option, shown_right_text)
        for option in summary_options(snapshot, glyphs, now)
        for shown_right_text in (right_text, "")
    ]
    summary, right_text = next(
        (
            (option, shown_right_text)
            for option, shown_right_text in options
            if len(BOARD_TITLE) + parts_width(option) + len(shown_right_text) + 2 < width
        ),
        options[-1],
    )
    segments = part_segments(0, 0, [*title_parts, *summary])
    if len(BOARD_TITLE) + parts_width(summary) + len(right_text) + 2 < width:
        segments.append(
            Segment(0, width - 1 - len(right_text), right_text, right_style, right_color)
        )
    return segments


def tail_shortened_directory(directory: str, width: int, glyphs: Glyphs) -> str:
    parts = directory.split("/")
    while len(directory) > width and len(parts) > 1:
        parts = parts[1:]
        directory = f"{glyphs.ellipsis}/{'/'.join(parts)}"
    return fitted(directory, width, glyphs)


def detail_segments(
    footer_y: int,
    width: int,
    line_count: int,
    row: display.ListRow | None,
    context: FrameContext,
) -> list[Segment]:
    snapshot, glyphs, now, _ = context
    line_width = width - 1
    lines: list[list[Part]] = [[("No task selected", STYLE_DIM, None)]]
    if row is not None and line_count > MAX_DETAIL_LINE_COUNT:
        lines = task_view_lines(row, line_width, snapshot, glyphs, now, line_count)
    elif row is not None:
        lines = detail_lines(row, line_width, line_count, snapshot, glyphs, now)
    rule_y = footer_y - 1 - line_count
    segments = [Segment(rule_y, 0, glyphs.horizontal * line_width, color=FAINT_COLOR)]
    for line_offset, parts in enumerate(lines[:line_count], start=1):
        segments += part_segments(rule_y + line_offset, 0, parts)
    return segments


def task_heading_parts(
    row: display.ListRow, line_width: int, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[Part]:
    task = snapshot.tasks[row.task_id]
    color = display.STATUS_COLORS[row.status]
    status_text = f"  {status_mark(row.status, glyphs, now)} {row.status}"
    when = when_text(task, now)
    facts = [
        text for text in (when, f"step {row.step}" if row.step else "", task["backend"]) if text
    ]
    facts_text = f" {glyphs.separator} ".join(facts)
    facts_text = f"  {facts_text}" if facts_text else ""
    task_id = fitted(row.task_id, line_width - len(status_text), glyphs)
    facts_text = fitted(facts_text, max(0, line_width - len(task_id) - len(status_text)), glyphs)
    return [
        (task_id, STYLE_BOLD, None),
        (status_text, STYLE_PLAIN, color),
        (facts_text, STYLE_DIM, None),
    ]


def heading_lines(
    row: display.ListRow, width: int, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[list[Part]]:
    heading_parts = task_heading_parts(row, sys.maxsize, snapshot, glyphs, now)
    if parts_width(heading_parts) <= width:
        return [heading_parts]
    (task_id, id_style, id_color), (status_text, status_style, status_color), facts = heading_parts
    status_text = status_text.lstrip()
    id_lines: list[list[Part]] = [
        [(line, id_style, id_color)] for line in textwrap.wrap(task_id, max(1, width))
    ]
    facts_text = fitted(facts[0], max(0, width - len(status_text)), glyphs)
    return [*id_lines, [(status_text, status_style, status_color), (facts_text, *facts[1:])]]


def detail_lines(
    row: display.ListRow,
    line_width: int,
    line_count: int,
    snapshot: BoardSnapshot,
    glyphs: Glyphs,
    now: datetime,
) -> list[list[Part]]:
    directory = display.display_directory(row.directory)
    lines: list[list[Part]] = [
        *heading_lines(row, line_width, snapshot, glyphs, now),
        [(tail_shortened_directory(directory, line_width, glyphs), STYLE_DIM, None)],
    ]
    task = snapshot.tasks[row.task_id]
    reason = row_reason(row, snapshot)
    if reason and line_width > len(glyphs.ellipsis) + 1 and len(lines) < line_count:
        reason_lines = textwrap.wrap(
            reason,
            line_width,
            max_lines=min(REASON_LINE_COUNT, line_count - len(lines)),
            placeholder=f" {glyphs.ellipsis}",
        )
        reason_parts: list[list[Part]] = [[(line, STYLE_PLAIN, None)] for line in reason_lines]
        lines += reason_parts
    remaining_count = line_count - len(lines)
    activity = snapshot.activity.get(row.task_id, [])
    if activity:
        activity_parts: list[list[Part]] = [
            [(fitted(activity_text(line, glyphs), line_width, glyphs), STYLE_DIM, None)]
            for line in activity[len(activity) - max(0, remaining_count) :]
        ]
        return lines + activity_parts
    prompt = " ".join(task.get("prompt", "").split())
    if prompt and line_width > len(glyphs.ellipsis) + 1:
        prompt_lines = textwrap.wrap(
            prompt, line_width, max_lines=remaining_count, placeholder=f" {glyphs.ellipsis}"
        )
        prompt_parts: list[list[Part]] = [[(line, STYLE_DIM, None)] for line in prompt_lines]
        lines += prompt_parts
    return lines


def chain_pull_requests(snapshot: BoardSnapshot, task_id: str) -> list[ledger.PullRequest]:
    chain_ids = chains.chain_task_ids(snapshot.tasks, task_id)
    return display.chain_pull_requests(snapshot.tasks, chain_ids)


def available_actions(snapshot: BoardSnapshot, row: display.ListRow | None) -> list[str]:
    if row is None:
        return []
    actions: list[str] = []
    if row.status in PICKUP_STATUSES:
        actions.append(ACTION_PICKUP)
    if row.status not in ledger.NOT_STARTED_STATUSES:
        actions.append(ACTION_WATCH)
    if chain_pull_requests(snapshot, row.task_id):
        actions.append(ACTION_OPEN_PULL_REQUEST)
    if row.status in RETRY_STATUSES:
        actions.append(ACTION_RETRY)
    if row.status == ledger.STATUS_BLOCKED:
        actions.append(ACTION_RERUN_GATE)
    if row.column == display.COLUMN_REVIEW and row.status == ledger.STATUS_FINISHED:
        actions.append(ACTION_CLOSE)
    if row.status in CANCEL_STATUSES:
        actions.append(ACTION_CANCEL)
    actions.append(ACTION_COPY)
    return actions


def footer_action_hints(
    snapshot: BoardSnapshot, row: display.ListRow | None, *, is_complete: bool = False
) -> list[tuple[str, str]]:
    return [
        (action_key(action), ACTION_LABELS[action])
        for action in available_actions(snapshot, row)
        if is_complete or action not in FOOTER_HIDDEN_ACTIONS
    ]


def action_key(action: str) -> str:
    return ACTION_KEYS[action]


def key_hint_segments(y: int, width: int, key_sets: list[list[tuple[str, str]]]) -> list[Segment]:
    keys = next(
        (
            key_set
            for key_set in key_sets
            if sum(len(key) + (len(label) + 1 if label else 0) + 2 for key, label in key_set) - 2
            < width
        ),
        key_sets[-1],
    )
    segments: list[Segment] = []
    x = 0
    for key, label in keys:
        segments.append(Segment(y, x, key, STYLE_BOLD))
        if label:
            segments.append(Segment(y, x + len(key) + 1, label, STYLE_DIM))
            x += len(label) + 1
        x += len(key) + 2
    return segments


def status_line_segments(
    y: int, width: int, state: BoardState, row: display.ListRow | None, glyphs: Glyphs
) -> list[Segment] | None:
    if state.pending_action is not None and row is not None:
        verb = ACTION_LABELS[state.pending_action].capitalize()
        answer = CONFIRM_ANSWER_TEXT
        if len(verb) + len(row.task_id) + len(answer) + 4 >= width:
            answer = CONFIRM_SHORT_ANSWER_TEXT
        task_id = fitted(row.task_id, width - len(verb) - len(answer) - 5, glyphs)
        prompt = f"{verb} {task_id}? "
        return [
            Segment(y, 0, prompt, STYLE_BOLD),
            Segment(y, len(prompt), "y", STYLE_BOLD, BOARD_COLUMNS[0].color),
            Segment(y, len(prompt) + 1, answer, STYLE_DIM),
        ]
    if state.flash_text and time.monotonic() < state.flash_until:
        return [Segment(y, 0, fitted(state.flash_text, width - 1, glyphs), STYLE_BOLD)]
    return None


def match_count_parts(snapshot: BoardSnapshot, state: BoardState) -> tuple[str, str]:
    if state.is_showing_all:
        return f"{len(snapshot.rows)}/{len(snapshot.unfiltered_rows)}", ""
    shown_rows, unfiltered_rows = [
        [row for row in rows if row.column != display.COLUMN_DONE]
        for rows in (snapshot.rows, snapshot.unfiltered_rows)
    ]
    count_text = f"{len(shown_rows)}/{len(unfiltered_rows)}"
    done_count = len(snapshot.rows) - len(shown_rows)
    return count_text, f", {done_count} in Done" if done_count else ""


def footer_segments(
    y: int,
    width: int,
    state: BoardState,
    snapshot: BoardSnapshot,
    row: display.ListRow | None,
    glyphs: Glyphs,
) -> list[Segment]:
    if state.is_filtering:
        query = f"/{state.filter_text}{glyphs.cursor}"
        count_text, done_text = match_count_parts(snapshot, state)
        hints = (
            f"  {count_text} match{done_text}  {glyphs.enter_key} keep  esc clear",
            f"  {count_text}{done_text}  {glyphs.enter_key} keep  esc clear",
            f"  {count_text}  esc clear",
        )
        hint = next((hint for hint in hints if len(query) + len(hint) < width), hints[-1])
        return [
            Segment(y, 0, query, STYLE_BOLD, BOARD_COLUMNS[1].color),
            Segment(y, len(query), hint, STYLE_DIM),
        ]
    status_segments = status_line_segments(y, width, state, row, glyphs)
    if status_segments is not None:
        return status_segments
    action_hints = footer_action_hints(snapshot, row)
    open_hint = [(glyphs.enter_key, "open")] if row is not None else []
    match_text = "".join(match_count_parts(snapshot, state))
    filter_hint = [(f"/{state.filter_text}", match_text)] if state.filter_text else []
    done_hint = ("a", "hide done" if state.is_showing_all else "show done")
    short_done_hint = ("a", "done")
    column_hint, select_hint = (glyphs.column_keys, "column"), (glyphs.select_keys, "select")
    key_sets = [
        [
            *filter_hint,
            column_hint,
            select_hint,
            *open_hint,
            *footer_action_hints(snapshot, row, is_complete=True),
            done_hint,
            ("?", "help"),
            ("q", "quit"),
        ],
        [
            *filter_hint,
            column_hint,
            select_hint,
            *open_hint,
            *action_hints,
            short_done_hint,
            ("?", "help"),
            ("q", "quit"),
        ],
        [
            *filter_hint,
            column_hint,
            select_hint,
            *open_hint,
            *action_hints,
            short_done_hint,
            ("?", "help"),
            ("q", ""),
        ],
        [
            *filter_hint,
            column_hint,
            select_hint,
            *action_hints,
            short_done_hint,
            ("?", "help"),
            ("q", ""),
        ],
        [
            *filter_hint,
            (glyphs.column_keys, ""),
            (glyphs.select_keys, ""),
            *open_hint,
            *action_hints,
            short_done_hint,
            ("?", "help"),
            ("q", ""),
        ],
        [
            *filter_hint,
            (glyphs.column_keys, ""),
            (glyphs.select_keys, ""),
            *open_hint,
            *action_hints,
            ("?", "help"),
            ("q", "quit"),
        ],
        [*filter_hint, *open_hint, *action_hints, ("?", "help"), ("q", "quit")],
        [*filter_hint, *open_hint, *action_hints, ("?", ""), ("q", "")],
        [*filter_hint, *action_hints, ("?", ""), ("q", "")],
        [
            *filter_hint,
            (glyphs.column_keys, ""),
            (glyphs.select_keys, ""),
            *open_hint,
            *[(key, "") for key, _ in action_hints],
            ("?", "help"),
            ("q", "quit"),
        ],
        [*filter_hint, *open_hint, *[(key, "") for key, _ in action_hints], ("?", ""), ("q", "")],
        [*filter_hint, ("?", ""), ("q", "")],
        [("?", ""), ("q", "")],
    ]
    return key_hint_segments(y, width, key_sets)


def empty_board_segments(layout: BoardLayout, width: int, state: BoardState) -> list[Segment]:
    middle_y = layout.body_top + max(0, layout.body_height // 2 - 1)
    lines = EMPTY_BOARD_LINES
    if state.filter_text:
        lines = (f"No tasks match {state.filter_text!r}", "esc clears the filter")
    return [
        Segment(
            middle_y + offset,
            max(0, (width - len(text)) // 2),
            text,
            STYLE_BOLD if offset == 0 else STYLE_DIM,
        )
        for offset, text in enumerate(lines)
    ]


def clipped(segments: list[Segment], width: int, height: int) -> list[Segment]:
    clipped_segments: list[Segment] = []
    for segment in segments:
        # The bottom-right cell is left empty because curses raises after writing it.
        line_width = width - 1 if segment.y == height - 1 else width
        text = segment.text[: max(0, line_width - segment.x)]
        if 0 <= segment.y < height and segment.x >= 0 and text:
            clipped_segments.append(segment._replace(text=text))
    return clipped_segments


def board_view_segments(
    snapshot: BoardSnapshot,
    width: int,
    height: int,
    state: BoardState,
    glyphs: Glyphs,
    now: datetime,
) -> list[Segment]:
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    layout = snapshot_layout(snapshot, width, height, state)
    segments = title_segments(width, snapshot, state, glyphs, now)
    if layout.fit_count == 1:
        board_column, column_rows = columns[state.focus_index]
        header_parts = column_header_parts(board_column, len(column_rows), glyphs, is_focused=True)
        segments += tab_segments(
            layout.header_y, width, columns, layout, state, glyphs, middle_parts=header_parts
        )
    elif layout.fit_count < len(columns):
        segments += tab_segments(TAB_LINE_Y, width, columns, layout, state, glyphs, middle_parts=[])
    body_bottom = layout.body_top + layout.body_height
    changed_task_ids = frozenset(
        task_id for task_id, until in state.changed_until.items() if until > time.monotonic()
    )
    context = FrameContext(snapshot, glyphs, now, changed_task_ids)
    for offset in range(layout.fit_count):
        column_index = state.first_column_index + offset
        segments += [
            segment
            for segment in column_segments(
                layout, column_index, columns[column_index], state, context
            )
            if segment.y < body_bottom
        ]
    if not snapshot.rows:
        segments += empty_board_segments(layout, width, state)
    row = selected_row(snapshot, state)
    if layout.detail_line_count and snapshot.rows:
        segments += detail_segments(height - 1, width, layout.detail_line_count, row, context)
    segments += more_below_segments(layout, columns, state, glyphs)
    segments += footer_segments(height - 1, width, state, snapshot, row, glyphs)
    return segments


def wrapped_lines(text: str, width: int, style: str, hanging_indent: str = "") -> list[list[Part]]:
    lines: list[list[Part]] = []
    for paragraph in text.splitlines() or [""]:
        wrapped = textwrap.wrap(
            paragraph,
            max(len(TASK_VIEW_INDENT + hanging_indent) + 1, width),
            initial_indent=TASK_VIEW_INDENT,
            subsequent_indent=TASK_VIEW_INDENT + hanging_indent,
        ) or [TASK_VIEW_INDENT]
        paragraph_lines: list[list[Part]] = [[(line, style, None)] for line in wrapped]
        lines += paragraph_lines
    return lines


def task_activity(row: display.ListRow, snapshot: BoardSnapshot) -> list[str]:
    if row.task_id in snapshot.activity:
        return snapshot.activity[row.task_id]
    if row.status in ledger.NOT_STARTED_STATUSES:
        return []
    return recent_activity(snapshot.tasks[row.task_id]["session_id"])


def moment_text(timestamp: str | None, now: datetime) -> str:
    if timestamp is None:
        return ""
    seconds = seconds_since(timestamp, now)
    relative = f"in {duration_text(-seconds)}" if seconds < 0 else f"{duration_text(seconds)} ago"
    return f"{display.display_time(timestamp, now.date())} ({relative})"


def task_view_lines(
    row: display.ListRow,
    width: int,
    snapshot: BoardSnapshot,
    glyphs: Glyphs,
    now: datetime,
    line_count: int | None = None,
) -> list[list[Part]]:
    task = snapshot.tasks[row.task_id]
    due_at = task.get("due_at") if row.status == ledger.STATUS_SCHEDULED else None
    fields: list[tuple[str, str]] = [
        ("Directory", display.display_directory(row.directory)),
        ("Worktree", display.display_directory(task.get("worktree", ""))),
        ("Backend", task["backend"]),
        ("Due", moment_text(due_at, now)),
        ("Started", moment_text(task.get("started_at"), now)),
        ("Finished", moment_text(task.get("finished_at"), now)),
        ("Gate", task.get("gate", "")),
        ("Setup", task.get("setup", "")),
    ]
    chain_ids = chains.chain_task_ids(snapshot.tasks, row.task_id)
    if len(chain_ids) > 1:
        ordered_ids = sorted(
            chain_ids, key=lambda chain_id: chains.task_depth(snapshot.tasks, chain_id)
        )
        steps = f" {glyphs.right} ".join(
            f"{chain_id} ({snapshot.tasks[chain_id]['status']})" for chain_id in ordered_ids
        )
        fields.append(("Chain", steps))
    fields += [
        ("PR", f"{pull_request['url']} ({pull_request['state']})")
        for pull_request in chain_pull_requests(snapshot, row.task_id)
    ]
    label_width = max(len(label) for label, _ in fields) + 2
    rule_lines: list[list[Part]] = (
        [[(glyphs.horizontal * width, STYLE_PLAIN, FAINT_COLOR)]] if line_count is None else []
    )
    lines = [*heading_lines(row, width, snapshot, glyphs, now), *rule_lines]
    for label, value in fields:
        if not value:
            continue
        value_lines = textwrap.wrap(value, max(1, width - label_width)) or [""]
        lines.append(
            [(label.ljust(label_width), STYLE_DIM, None), (value_lines[0], STYLE_PLAIN, None)]
        )
        continuation_lines: list[list[Part]] = [
            [(" " * label_width + line, STYLE_PLAIN, None)] for line in value_lines[1:]
        ]
        lines += continuation_lines
    sections = [
        (
            "Stopped",
            task.get("gate_stop_reason", "") or task.get("setup_failure_reason", ""),
            STYLE_PLAIN,
            "",
        ),
        ("Prompt", task.get("prompt", ""), STYLE_PLAIN, ""),
        (
            "Activity",
            "\n".join(activity_text(line, glyphs) for line in task_activity(row, snapshot)),
            STYLE_DIM,
            "  ",
        ),
        ("Result", task.get("result", ""), STYLE_PLAIN, ""),
    ]
    for title, text, style, hanging_indent in sections:
        if not text.strip():
            continue
        title_line: list[Part] = [(title, STYLE_BOLD, None)]
        section_lines = wrapped_lines(text, width, style, hanging_indent)
        if title == "Activity" and line_count is not None:
            section_lines = section_lines[-max(1, line_count - len(lines) - 2) :]
        lines += [[], title_line, *section_lines]
    return lines


def task_view_segments(
    snapshot: BoardSnapshot,
    width: int,
    height: int,
    state: BoardState,
    glyphs: Glyphs,
    now: datetime,
) -> list[Segment]:
    row = selected_row(snapshot, state)
    if row is None:
        state.view = VIEW_BOARD
        return board_view_segments(snapshot, width, height, state, glyphs, now)
    lines = task_view_lines(row, width - 1, snapshot, glyphs, now)
    body_height = max(1, height - 1)
    if state.is_following:
        state.task_scroll = len(lines)
    state.task_scroll = max(0, min(state.task_scroll, len(lines) - body_height))
    segments: list[Segment] = []
    for offset, parts in enumerate(lines[state.task_scroll : state.task_scroll + body_height]):
        segments += part_segments(offset, 0, parts)
    status_segments = status_line_segments(height - 1, width, state, row, glyphs)
    if status_segments is not None:
        return segments + status_segments
    action_hints = footer_action_hints(snapshot, row)
    position = f"{min(len(lines), state.task_scroll + body_height)}/{len(lines)}"
    follow_hint = (
        [("G", "following" if state.is_following else "follow")]
        if row.status == ledger.STATUS_RUNNING
        else []
    )
    key_sets = [
        [
            (glyphs.select_keys, "scroll"),
            *follow_hint,
            *action_hints,
            ("esc", "back"),
            (position, ""),
        ],
        [*follow_hint, *action_hints, ("esc", "back")],
        [("esc", "back")],
    ]
    return segments + key_hint_segments(height - 1, width, key_sets)


def help_sections(glyphs: Glyphs) -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            "Move",
            [
                (f"{glyphs.left}{glyphs.right} h l Tab", "switch column"),
                (f"{glyphs.up}{glyphs.down} j k", "select task"),
                ("g G", "first, last"),
                ("PgUp PgDn", "page"),
                ("1-4", "jump to column"),
            ],
        ),
        (
            "Task",
            [
                (glyphs.enter_key, "open details"),
                ("p", "pick up its session"),
                ("w", "follow its live log"),
                ("o", "open its pull request"),
                ("r", "retry or re-run gate"),
                ("c", "close (move to Done)"),
                ("x", "cancel (if queued)"),
                ("y", "copy its id"),
            ],
        ),
        (
            "Board",
            [
                ("/", "filter tasks"),
                ("a", "show or hide Done"),
                ("?", "this help"),
                ("q", "quit"),
            ],
        ),
        (
            "Status",
            [
                (glyphs.status_marks[ledger.STATUS_SCHEDULED], "scheduled"),
                (glyphs.status_marks[ledger.STATUS_WAITING], "waiting on a step"),
                (glyphs.spinner[0], "running"),
                (glyphs.status_marks[ledger.STATUS_BLOCKED], "blocked by its gate"),
                (glyphs.status_marks[ledger.STATUS_FAILED], "failed"),
                (glyphs.status_marks[ledger.STATUS_FINISHED], "finished"),
                (glyphs.new_mark, "not opened yet"),
            ],
        ),
    ]


type HelpRow = tuple[str, str, bool]


def stacked_rows(section_rows: list[list[HelpRow]], *, is_spaced: bool) -> list[HelpRow]:
    spacer: list[HelpRow] = [("", "", False)]
    rows: list[HelpRow] = []
    for index, section in enumerate(section_rows):
        rows += (spacer if index and is_spaced else []) + section
    return rows


def balanced_groups(
    section_rows: list[list[HelpRow]], group_count: int
) -> list[list[list[HelpRow]]]:
    if group_count == 1:
        return [section_rows]
    splits = [
        [section_rows[:index], *balanced_groups(section_rows[index:], group_count - 1)]
        for index in range(1, len(section_rows) - group_count + 2)
    ]
    return min(splits, key=lambda groups: max(sum(map(len, group)) for group in groups))


def help_columns(
    sections: list[tuple[str, list[tuple[str, str]]]], width: int, height: int, column_width: int
) -> list[list[HelpRow]]:
    section_rows: list[list[HelpRow]] = [
        [(title, "", True), *[(key, label, False) for key, label in bindings]]
        for title, bindings in sections
    ]
    fitting_count = max(1, (width - 4 + HELP_SECTION_GAP) // (column_width + HELP_SECTION_GAP))
    groups = balanced_groups(section_rows, min(len(section_rows), fitting_count))
    is_spaced = all(sum(map(len, group)) + len(group) + 1 <= height - 1 for group in groups)
    return [stacked_rows(group, is_spaced=is_spaced) for group in groups]


def help_view_segments(width: int, height: int, glyphs: Glyphs, state: BoardState) -> list[Segment]:
    sections = help_sections(glyphs)
    bindings = [binding for _, section_bindings in sections for binding in section_bindings]
    key_width = max(len(key) for key, _ in bindings) + 3
    column_width = key_width + max(len(label) for _, label in bindings)
    columns = help_columns(sections, width, height, column_width)
    content_height = max(map(len, columns))
    padding = 1 if content_height + 4 <= height - 1 else 0
    box_width = min(width, len(columns) * (column_width + HELP_SECTION_GAP) - HELP_SECTION_GAP + 4)
    box_height = min(height - 1, content_height + 2 + 2 * padding)
    left = max(0, (width - box_width) // 2)
    top = max(0, (height - 1 - box_height) // 2)
    visible_count = box_height - 2 - 2 * padding
    state.help_scroll_limit = max(0, content_height - visible_count)
    state.help_scroll = min(state.help_scroll, state.help_scroll_limit)
    corners, line = glyphs.corners, glyphs.horizontal
    more_above = f" {glyphs.up} more {line}" if state.help_scroll else ""
    more_below = f" {glyphs.down} more " if state.help_scroll < state.help_scroll_limit else ""
    close_hint = HELP_CLOSE_HINT
    if state.help_scroll_limit:
        close_hint = f" {glyphs.up}{glyphs.down} scroll  other keys close "
    top_text = f"{corners[0]}{line} Keys "
    bottom_text = f"{corners[2]}{line}{more_below}"
    top_rule = line * max(0, box_width - len(top_text) - len(more_above) - 1)
    bottom_rule = line * max(0, box_width - len(bottom_text) - len(close_hint) - 2)
    segments = [
        Segment(y, 0, " " * width, STYLE_PLAIN)
        for y in range(max(0, top - 1), min(height - 1, top + box_height + 1))
    ]
    segments += [
        Segment(top, left, f"{top_text}{top_rule}{more_above}{corners[1]}", STYLE_DIM),
        Segment(
            top + box_height - 1,
            left,
            f"{bottom_text}{bottom_rule}{close_hint}{line}{corners[3]}",
            STYLE_DIM,
        ),
    ]
    for offset in range(1, box_height - 1):
        segments += [
            Segment(top + offset, left, glyphs.vertical + " " * (box_width - 2), STYLE_DIM),
            Segment(top + offset, left + box_width - 1, glyphs.vertical, STYLE_DIM),
        ]
    first_row_y = top + 1 + padding
    for column_index, rows in enumerate(columns):
        x = left + 2 + column_index * (column_width + HELP_SECTION_GAP)
        shown_rows = rows[state.help_scroll : state.help_scroll + visible_count]
        for offset, (key, label, is_title) in enumerate(shown_rows):
            if is_title:
                segments.append(
                    Segment(first_row_y + offset, x, key, STYLE_BOLD, BOARD_COLUMNS[1].color)
                )
                continue
            segments += [
                Segment(first_row_y + offset, x, key, STYLE_BOLD),
                Segment(first_row_y + offset, x + key_width, label, STYLE_PLAIN),
            ]
    return segments


def board_frame(
    snapshot: BoardSnapshot,
    width: int,
    height: int,
    state: BoardState,
    glyphs: Glyphs = UNICODE_GLYPHS,
    now: datetime | None = None,
) -> list[Segment]:
    now = now or datetime.now().astimezone()
    if state.view == VIEW_TASK:
        segments = task_view_segments(snapshot, width, height, state, glyphs, now)
    else:
        segments = board_view_segments(snapshot, width, height, state, glyphs, now)
    if state.is_showing_help:
        backdrop = [
            Segment(segment.y, segment.x, segment.text, STYLE_PLAIN, FAINT_COLOR)
            for segment in segments
            if segment.y < height - 1
        ]
        segments = backdrop + help_view_segments(width, height, glyphs, state)
    return clipped(segments, width, height)


def move_selection(state: BoardState, column_row_count: int, target_index: int) -> None:
    state.selected_indices[state.focus_index] = max(0, min(target_index, column_row_count - 1))
    state.selected_task_ids[state.focus_index] = None


def apply_board_key(state: BoardState, key: int, column_row_count: int, page_size: int) -> None:
    column_count = len(visible_columns(is_showing_all=state.is_showing_all))
    selected_index = state.selected_indices[state.focus_index]
    if key in LEFT_KEYS:
        state.focus_index = max(state.focus_index - 1, 0)
    elif key in RIGHT_KEYS:
        state.focus_index = min(state.focus_index + 1, column_count - 1)
    elif ord("1") <= key < ord("1") + column_count:
        state.focus_index = key - ord("1")
    elif key in DOWN_KEYS | UP_KEYS:
        move_selection(state, column_row_count, selected_index + (1 if key in DOWN_KEYS else -1))
    elif key in FIRST_KEYS | LAST_KEYS:
        move_selection(state, column_row_count, 0 if key in FIRST_KEYS else column_row_count - 1)
    elif key in PAGE_DOWN_KEYS | PAGE_UP_KEYS:
        step = page_size if key in PAGE_DOWN_KEYS else -page_size
        move_selection(state, column_row_count, selected_index + step)
    elif key == ord("a"):
        state.is_showing_all = not state.is_showing_all
    elif key == FILTER_KEY:
        state.is_filtering = True
    elif key == ESCAPE_KEY:
        state.filter_text = ""
    elif key in ENTER_KEYS:
        state.view = VIEW_TASK
        state.task_scroll = 0
    elif key == ord("?"):
        state.is_showing_help = True


def apply_filter_key(state: BoardState, key: int) -> None:
    if key == ESCAPE_KEY:
        state.filter_text = ""
        state.is_filtering = False
    elif key in ENTER_KEYS:
        state.is_filtering = False
    elif key in BACKSPACE_KEYS:
        state.filter_text = state.filter_text[:-1]
    elif key in PRINTABLE_KEYS:
        state.filter_text += chr(key)


def apply_task_key(state: BoardState, key: int, page_size: int) -> None:
    state.is_following = key in LAST_KEYS or (state.is_following and key in DOWN_KEYS)
    if key in BACK_KEYS:
        state.view = VIEW_BOARD
    elif key in DOWN_KEYS | UP_KEYS:
        state.task_scroll += 1 if key in DOWN_KEYS else -1
    elif key in PAGE_DOWN_KEYS | PAGE_UP_KEYS:
        state.task_scroll += page_size if key in PAGE_DOWN_KEYS else -page_size
    elif key in FIRST_KEYS:
        state.task_scroll = 0
    elif key in LAST_KEYS:
        state.task_scroll = sys.maxsize
    elif key == ord("?"):
        state.is_showing_help = True


def apply_help_key(state: BoardState, key: int, page_size: int) -> None:
    scroll_steps = {
        **dict.fromkeys(DOWN_KEYS, 1),
        **dict.fromkeys(UP_KEYS, -1),
        **dict.fromkeys(PAGE_DOWN_KEYS, page_size),
        **dict.fromkeys(PAGE_UP_KEYS, -page_size),
    }
    if state.help_scroll_limit and key in scroll_steps:
        state.help_scroll = max(
            0, min(state.help_scroll + scroll_steps[key], state.help_scroll_limit)
        )
        return
    state.is_showing_help = False
    state.help_scroll = 0


def apply_key(
    state: BoardState, key: int, snapshot: BoardSnapshot | None = None, height: int = 0
) -> None:
    rows = snapshot.rows if snapshot is not None else []
    columns = rows_by_column(rows, is_showing_all=state.is_showing_all)
    column_row_count = len(columns[min(state.focus_index, len(columns) - 1)][1])
    page_size = max(1, (height - 12) // COMPACT_CARD_PITCH)
    if state.is_showing_help:
        apply_help_key(state, key, max(1, height - 6))
    elif state.is_filtering:
        apply_filter_key(state, key)
    elif state.view == VIEW_TASK:
        apply_task_key(state, key, max(1, height - 2))
    else:
        apply_board_key(state, key, column_row_count or sys.maxsize, page_size)


def segment_attribute(segment: Segment, *, is_color: bool) -> int:
    if not is_color:
        return STYLE_ATTRIBUTES[segment.style]
    if segment.style == STYLE_DIM:
        # Many terminfo entries, including macOS xterm-256color, have no dim capability.
        return curses.color_pair(COLOR_PAIRS[segment.color or MUTED_COLOR])
    attribute = STYLE_ATTRIBUTES[segment.style]
    if segment.color is not None:
        attribute |= curses.color_pair(COLOR_PAIRS[segment.color])
    return attribute


def draw_board(
    screen: curses.window,
    snapshot: BoardSnapshot,
    state: BoardState,
    *,
    is_color: bool,
    glyphs: Glyphs,
) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    fit_state_to_layout(snapshot, width, height, state)
    for segment in board_frame(snapshot, width, height, state, glyphs):
        with contextlib.suppress(curses.error):
            screen.addstr(
                segment.y, segment.x, segment.text, segment_attribute(segment, is_color=is_color)
            )
    screen.refresh()


def start_colors() -> bool:
    if "NO_COLOR" in os.environ or not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        return False
    if curses.COLORS < MIN_COLOR_COUNT:
        return False
    for color, pair in COLOR_PAIRS.items():
        curses.init_pair(pair, color, -1)
    return True


def is_ncurses_mangling_wide_rep() -> bool:
    # Old ncurses emits runs of a wide char as its low byte plus the terminfo `rep` capability.
    return curses.ncurses_version < WIDE_REP_FIX_NCURSES_VERSION


def has_wide_rep_bug() -> bool:
    return is_ncurses_mangling_wide_rep() and curses.tigetstr("rep") is not None


def terminal_glyphs() -> Glyphs:
    codeset = locale.nl_langinfo(locale.CODESET).lower().replace("-", "")
    if codeset != "utf8" or has_wide_rep_bug():
        return ASCII_GLYPHS
    return UNICODE_GLYPHS


def file_stat(path: Path) -> LedgerStat:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return (stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size)


def progress_stats(snapshot: BoardSnapshot) -> tuple[LedgerStat, ...]:
    return tuple(
        file_stat(progress_log.progress_path(snapshot.tasks[task_id]["session_id"]))
        for task_id in snapshot.activity
    )


def ignore_interrupt(_signal_number: int, _frame: FrameType | None) -> None:
    return


@contextlib.contextmanager
def suspended_screen(screen: curses.window) -> Generator[None]:
    curses.def_prog_mode()
    curses.endwin()
    previous_handler = signal.signal(signal.SIGINT, ignore_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        curses.reset_prog_mode()
        screen.clear()


def run_attached(screen: curses.window, command: list[str]) -> None:
    with suspended_screen(screen):
        try:
            returncode = subprocess.run(command, check=False).returncode
        except OSError as error:
            print(f"golem: {error}", file=sys.stderr)
            returncode = 1
        if returncode not in {0, INTERRUPTED_EXIT_STATUS}:
            with contextlib.suppress(EOFError):
                input(RETURN_PROMPT)


def run_captured(command: list[str], input_text: str = "") -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, input=input_text, capture_output=True, text=True, check=False
        )
    except OSError as error:
        return 1, str(error)
    output = (result.stderr or result.stdout).strip()
    return result.returncode, display.first_line(output)


def flash(state: BoardState, text: str) -> None:
    state.flash_text = text
    state.flash_until = time.monotonic() + FLASH_SECONDS


def run_action(
    screen: curses.window,
    state: BoardState,
    snapshot: BoardSnapshot,
    action: str,
    row: display.ListRow,
) -> None:
    if action == ACTION_WATCH:
        state.view = VIEW_TASK
        state.is_following = True
        return
    if action in {ACTION_PICKUP, ACTION_RERUN_GATE}:
        run_attached(screen, [*GOLEM_COMMAND, action, row.task_id])
        return
    if action == ACTION_RETRY:
        returncode, output = run_captured([*GOLEM_COMMAND, action, row.task_id])
        report = output.removeprefix(GOLEM_PREFIX)
        flash(state, report[:1].upper() + report[1:])
        return
    if action == ACTION_COPY:
        returncode, output = run_captured([COPY_COMMAND], row.task_id)
        flash(state, f"Copied {row.task_id}" if returncode == 0 else output)
        return
    if action == ACTION_OPEN_PULL_REQUEST:
        pull_requests = chain_pull_requests(snapshot, row.task_id)
        pull_request = next(
            (item for item in pull_requests if item["state"] == ledger.PULL_REQUEST_STATE_OPEN),
            pull_requests[0],
        )
        returncode, output = run_captured([OPEN_COMMAND, pull_request["url"]])
        opened_text = (
            f"Opened {pull_request_text([pull_request]).removesuffix(pull_request['state'])}"
        )
        flash(state, opened_text.strip() if returncode == 0 else output)
        return
    returncode, output = run_captured([*GOLEM_COMMAND, action, row.task_id])
    flash(state, f"{ACTION_DONE_VERBS[action]} {row.task_id}" if returncode == 0 else output)


def handle_action_key(
    screen: curses.window, state: BoardState, snapshot: BoardSnapshot, key: int
) -> bool:
    if state.is_showing_help or state.is_filtering:
        return False
    row = selected_row(snapshot, state)
    if state.pending_action is not None:
        action, state.pending_action = state.pending_action, None
        if key == ord("y") and row is not None:
            run_action(screen, state, snapshot, action, row)
        return True
    key_actions = [action for action, char in ACTION_KEYS.items() if ord(char) == key]
    if not key_actions or row is None:
        return False
    actions = available_actions(snapshot, row)
    action = next((action for action in key_actions if action in actions), None)
    if action is None:
        flash(state, UNAVAILABLE_ACTION_TEXTS[key_actions[0]])
        return True
    if action in CONFIRMED_ACTIONS:
        state.pending_action = action
        return True
    run_action(screen, state, snapshot, action, row)
    return True


def is_quit_key(state: BoardState, key: int) -> bool:
    is_idle = state.pending_action is None and not state.is_filtering and not state.is_showing_help
    return state.view == VIEW_BOARD and is_idle and key == ord("q")


def row_positions(snapshot: BoardSnapshot) -> dict[str, tuple[str | None, str]]:
    return {row.task_id: (row.column, row.status) for row in snapshot.unfiltered_rows}


def mark_changed_tasks(state: BoardState, previous: BoardSnapshot, current: BoardSnapshot) -> None:
    previous_positions = row_positions(previous)
    until = time.monotonic() + CHANGE_HIGHLIGHT_SECONDS
    for task_id, position in row_positions(current).items():
        if previous_positions.get(task_id) != position:
            state.changed_until[task_id] = until


def board_loop(screen: curses.window, *, is_color: bool, glyphs: Glyphs) -> int:
    task_ledger_path = ledger.ledger_path()
    state = BoardState()
    shown_source_key: tuple[LedgerStat, tuple[LedgerStat, ...], date] | None = None
    snapshot = BoardSnapshot([], {}, {}, [])
    shown_size = screen.getmaxyx()
    while True:
        now = datetime.now().astimezone()
        source_key = (file_stat(task_ledger_path), progress_stats(snapshot), now.date())
        if source_key != shown_source_key:
            is_first_load = shown_source_key is None and not snapshot.unfiltered_rows
            previous_snapshot = snapshot
            snapshot = board_snapshot(ledger.load_tasks(), now)
            if is_first_load:
                state.focus_index = attention_column_index(snapshot.rows)
            else:
                mark_changed_tasks(state, previous_snapshot, snapshot)
            shown_source_key = (source_key[0], progress_stats(snapshot), now.date())
        if screen.getmaxyx() != shown_size:
            shown_size = screen.getmaxyx()
            screen.clear()
        shown_snapshot = filtered_snapshot(snapshot, state)
        draw_board(screen, shown_snapshot, state, is_color=is_color, glyphs=glyphs)
        pressed_key = screen.getch()
        if pressed_key in {-1, curses.KEY_RESIZE}:
            continue
        state.flash_until = 0.0
        if is_quit_key(state, pressed_key):
            return 0
        if handle_action_key(screen, state, shown_snapshot, pressed_key):
            shown_source_key = None
            continue
        apply_key(state, pressed_key, shown_snapshot, shown_size[0])


def is_terminal() -> bool:
    is_tty = sys.stdout.isatty() and sys.stdin.isatty()
    return is_tty and os.environ.get("TERM", "") not in {"", "dumb"}


def terminfo_source_without_rep() -> bytes | None:
    try:
        result = subprocess.run(
            ["infocmp", "-x", "-1"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines(keepends=True)
    if not any(line.startswith(REP_LINE_PREFIX) for line in lines):
        return None
    return b"".join(line for line in lines if not line.startswith(REP_LINE_PREFIX))


def compile_terminfo(source: bytes, directory: Path) -> bool:
    source_path = directory / TERMINFO_SOURCE_NAME
    try:
        source_path.write_bytes(source)
        result = subprocess.run(
            ["tic", "-x", "-o", str(directory), str(source_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


@contextlib.contextmanager
def terminfo_without_rep() -> Generator[None]:
    source = None
    if is_ncurses_mangling_wide_rep():
        source = terminfo_source_without_rep()
    if source is None:
        yield
        return
    with tempfile.TemporaryDirectory() as directory:
        if not compile_terminfo(source, Path(directory)):
            yield
            return
        previous_terminfo = os.environ.get(TERMINFO_VARIABLE)
        os.environ[TERMINFO_VARIABLE] = directory
        try:
            yield
        finally:
            if previous_terminfo is None:
                del os.environ[TERMINFO_VARIABLE]
            else:
                os.environ[TERMINFO_VARIABLE] = previous_terminfo


def start_screen() -> curses.window:
    try:
        with terminfo_without_rep():
            return curses.initscr()
    except curses.error:
        # A tic newer than this ncurses can write an entry it can't read; use the terminal's own.
        return curses.initscr()


def run_board() -> int:
    if not is_terminal():
        raise SystemExit(NOT_A_TERMINAL_MESSAGE)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        screen = start_screen()
    except curses.error:
        raise SystemExit(NOT_A_TERMINAL_MESSAGE) from None
    try:
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)  # noqa: FBT003 - curses only takes the flag positionally
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        curses.set_escdelay(ESCAPE_DELAY_MILLISECONDS)
        is_color = start_colors()
        screen.timeout(TICK_MILLISECONDS)
        return board_loop(screen, is_color=is_color, glyphs=terminal_glyphs())
    except KeyboardInterrupt:
        return INTERRUPTED_EXIT_STATUS
    finally:
        curses.endwin()
