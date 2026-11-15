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
CARD_LINE_COUNT = 3
CARD_PITCH = CARD_LINE_COUNT + 1
TITLE_LINE_COUNT = 2
TAB_LINE_COUNT = 2
DETAIL_PANE_LINE_COUNT = 5
MIN_DETAIL_HEIGHT = 20
TICK_MILLISECONDS = 250
ESCAPE_DELAY_MILLISECONDS = 25
FLASH_SECONDS = 4
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
RETURN_PROMPT = "golem: press Enter to return to the board"
GOLEM_COMMAND = (sys.executable, "-m", "golem")
OPEN_COMMAND = "open"
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
VIEW_HELP = "help"
ACTION_PICKUP = "pickup"
ACTION_WATCH = "watch"
ACTION_OPEN_PULL_REQUEST = "pr"
ACTION_CLOSE = "close"
ACTION_CANCEL = "cancel"
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
BACK_KEYS = {ESCAPE_KEY, ord("q"), curses.KEY_BACKSPACE, *ENTER_KEYS}
ACTION_KEYS = {
    ord("p"): ACTION_PICKUP,
    ord("w"): ACTION_WATCH,
    ord("o"): ACTION_OPEN_PULL_REQUEST,
    ord("c"): ACTION_CLOSE,
    ord("x"): ACTION_CANCEL,
}
CONFIRMED_ACTIONS = {ACTION_CLOSE, ACTION_CANCEL}
ACTION_LABELS = {
    ACTION_PICKUP: "pickup",
    ACTION_WATCH: "watch",
    ACTION_OPEN_PULL_REQUEST: "PR",
    ACTION_CLOSE: "close",
    ACTION_CANCEL: "cancel",
}
ACTION_DONE_VERBS = {ACTION_CLOSE: "Closed", ACTION_CANCEL: "Cancelled"}
PICKUP_STATUSES = {ledger.STATUS_BLOCKED, ledger.STATUS_FAILED, ledger.STATUS_FINISHED}
CANCEL_STATUSES = {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}
EMPTY_COLUMN_TEXTS = {
    display.COLUMN_QUEUED: "Nothing queued",
    display.COLUMN_RUNNING: "Nothing running",
    display.COLUMN_REVIEW: "Nothing to review",
    display.COLUMN_DONE: "Nothing done yet",
}
EMPTY_BOARD_LINES = ("No tasks yet", 'Queue one with  golem run <id> "<prompt>"')
UNAVAILABLE_ACTION_TEXTS = {
    ACTION_PICKUP: "Only blocked, failed or finished tasks can be picked up",
    ACTION_WATCH: "Only running tasks can be watched",
    ACTION_OPEN_PULL_REQUEST: "This task has no pull request",
    ACTION_CLOSE: "Only finished tasks in Review can be closed",
    ACTION_CANCEL: "Only queued tasks can be cancelled",
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
    card_bar: str
    selected_card_bar: str
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


class BoardSnapshot(NamedTuple):
    rows: list[display.ListRow]
    tasks: ledger.Tasks
    activity: dict[str, list[str]]


class FrameContext(NamedTuple):
    snapshot: BoardSnapshot
    glyphs: Glyphs
    now: datetime


class BoardLayout(NamedTuple):
    fit_count: int
    column_width: int
    header_y: int
    body_top: int
    body_height: int
    is_detail_shown: bool

    @property
    def visible_card_count(self) -> int:
        return max(1, (self.body_height + 1) // CARD_PITCH)


type LedgerStat = tuple[int, int, int] | None
type Part = tuple[str, str, int | None]

BOARD_COLUMNS = (
    BoardColumn(display.COLUMN_QUEUED, 208),
    BoardColumn(display.COLUMN_RUNNING, 39),
    BoardColumn(display.COLUMN_REVIEW, 170),
    BoardColumn(display.COLUMN_DONE, 34),
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
    card_bar="▎",
    selected_card_bar="▌",
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
)
ASCII_GLYPHS = Glyphs(
    horizontal="-",
    heavy_horizontal="=",
    card_bar="|",
    selected_card_bar="#",
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


def board_snapshot(tasks: ledger.Tasks, now: datetime) -> BoardSnapshot:
    rows = display.list_rows(tasks, now.date())
    activity = {
        row.task_id: recent_activity(tasks[row.task_id]["session_id"])
        for row in rows
        if row.status == ledger.STATUS_RUNNING
    }
    return BoardSnapshot(rows, tasks, activity)


def visible_columns(*, is_showing_all: bool) -> tuple[BoardColumn, ...]:
    return BOARD_COLUMNS if is_showing_all else BOARD_COLUMNS[:-1]


def rows_by_column(
    rows: list[display.ListRow], *, is_showing_all: bool
) -> list[tuple[BoardColumn, list[display.ListRow]]]:
    return [
        (board_column, [row for row in rows if row.column == board_column.title])
        for board_column in visible_columns(is_showing_all=is_showing_all)
    ]


def board_layout(width: int, height: int, column_count: int) -> BoardLayout:
    fit_count = min(column_count, max(1, (width + COLUMN_GAP) // (MIN_COLUMN_WIDTH + COLUMN_GAP)))
    column_width = (width - (fit_count - 1) * COLUMN_GAP) // fit_count
    header_y = TITLE_LINE_COUNT + (TAB_LINE_COUNT if fit_count < column_count else 0)
    body_top = header_y + 3
    is_detail_shown = height >= MIN_DETAIL_HEIGHT
    detail_height = DETAIL_PANE_LINE_COUNT + 1 if is_detail_shown else 0
    body_height = height - 1 - detail_height - body_top
    return BoardLayout(fit_count, column_width, header_y, body_top, body_height, is_detail_shown)


def fit_state_to_layout(
    rows: list[display.ListRow], width: int, height: int, state: BoardState
) -> None:
    columns = rows_by_column(rows, is_showing_all=state.is_showing_all)
    layout = board_layout(width, height, len(columns))
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


def row_reason(row: display.ListRow, task: ledger.Task) -> str:
    if row.status in {ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING}:
        return display.list_reason(task)
    if row.status == ledger.STATUS_WAITING:
        return f"after {row.detail}" if row.detail else ""
    if row.status == ledger.STATUS_FINISHED:
        return row.detail.split(FINISHED_AGE_SEPARATOR)[0]
    return row.detail


def card_detail(row: display.ListRow, snapshot: BoardSnapshot, glyphs: Glyphs) -> str:
    activity = snapshot.activity.get(row.task_id)
    if activity:
        return activity_text(activity[-1], glyphs)
    return row_reason(row, snapshot.tasks[row.task_id]) or Path(row.directory).name


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
    snapshot, glyphs, now = context
    color = display.STATUS_COLORS[row.status]
    task = snapshot.tasks[row.task_id]
    content_x = x + CARD_INDENT
    content_width = width - CARD_INDENT
    bar = glyphs.selected_card_bar if is_selected else glyphs.card_bar
    step_text = f" {row.step}" if row.step else ""
    id_width = content_width - len(step_text)
    title = fitted(row.task_id, id_width, glyphs)
    title_style = STYLE_SELECTED if is_selected else STYLE_BOLD
    segments = [Segment(y + offset, x, bar, color=color) for offset in range(CARD_LINE_COUNT)]
    if is_selected:
        segments.append(Segment(y, content_x, title.ljust(content_width), title_style, color))
        segments.append(Segment(y, content_x + id_width, step_text, STYLE_SELECTED, color))
    else:
        segments.append(Segment(y, content_x, title, title_style))
        segments.append(Segment(y, content_x + id_width, step_text, STYLE_DIM))
    mark = status_mark(row.status, glyphs, now)
    status_text = f"{mark} {row.status}"
    when = when_text(task, now)
    when_suffix = f" {glyphs.separator} {when}" if when else ""
    segments.append(
        Segment(y + 1, content_x, fitted(status_text, content_width, glyphs), color=color)
    )
    segments.append(
        Segment(
            y + 1,
            content_x + len(status_text),
            fitted(when_suffix, max(0, content_width - len(status_text)), glyphs),
            STYLE_DIM,
        )
    )
    if is_new_for_review(snapshot, row):
        new_x = content_x + len(status_text) + len(when_suffix) + 1
        if new_x + 3 <= content_x + content_width:
            segments.append(Segment(y + 1, new_x, "new", STYLE_BOLD, color))
    detail = card_detail(row, snapshot, glyphs)
    segments.append(Segment(y + 2, content_x, fitted(detail, content_width, glyphs), STYLE_DIM))
    return segments


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
    marker = f"{glyphs.focus_marker} " if is_focused else "  "
    title_style = STYLE_BOLD if is_focused else STYLE_PLAIN
    rule = glyphs.heavy_horizontal if is_focused else glyphs.horizontal
    rule_color = board_column.color if is_focused else FAINT_COLOR
    segments = [
        Segment(layout.header_y, x, marker, color=board_column.color),
        Segment(layout.header_y, x + 2, board_column.title, title_style, board_column.color),
        Segment(layout.header_y, x + 3 + len(board_column.title), str(len(column_rows)), STYLE_DIM),
        Segment(layout.header_y + 1, x, rule * layout.column_width, color=rule_color),
    ]
    if not column_rows:
        empty_text = fitted(EMPTY_COLUMN_TEXTS[board_column.title], layout.column_width - 2, glyphs)
        return [*segments, Segment(layout.body_top, x + 2, empty_text, STYLE_DIM)]
    scroll_offset = state.scroll_offsets[column_index]
    if scroll_offset:
        segments.append(
            Segment(layout.body_top - 1, x + 2, f"{glyphs.up} {scroll_offset} more", STYLE_DIM)
        )
    shown_rows = column_rows[scroll_offset : scroll_offset + layout.visible_card_count]
    for card_index, row in enumerate(shown_rows):
        is_selected = (
            is_focused and scroll_offset + card_index == state.selected_indices[column_index]
        )
        card_y = layout.body_top + card_index * CARD_PITCH
        segments += card_segments(
            card_y, x, layout.column_width, row, context, is_selected=is_selected
        )
    hidden_below_count = len(column_rows) - scroll_offset - len(shown_rows)
    below_y = layout.body_top + len(shown_rows) * CARD_PITCH - 1
    if hidden_below_count and below_y < layout.body_top + layout.body_height:
        segments.append(
            Segment(below_y, x + 2, f"{glyphs.down} {hidden_below_count} more", STYLE_DIM)
        )
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
    while (
        parts_width(left_options[left_index]) + parts_width(right_options[right_index]) + 2 >= width
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
    right_parts = right_options[right_index]
    return [
        *part_segments(y, 0, left_options[left_index]),
        *part_segments(y, width - 1 - parts_width(right_parts), right_parts),
    ]


def summary_options(snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime) -> list[list[Part]]:
    columns = rows_by_column(snapshot.rows, is_showing_all=True)
    counts = {board_column.title: len(column_rows) for board_column, column_rows in columns}
    items = [
        (
            status_mark(ledger.STATUS_RUNNING, glyphs, now),
            counts[display.COLUMN_RUNNING],
            "running",
            display.STATUS_COLORS[ledger.STATUS_RUNNING],
        ),
        (
            glyphs.status_marks[ledger.STATUS_BLOCKED],
            counts[display.COLUMN_REVIEW],
            "to review",
            BOARD_COLUMNS[2].color,
        ),
        (
            glyphs.status_marks[ledger.STATUS_SCHEDULED],
            counts[display.COLUMN_QUEUED],
            "queued",
            BOARD_COLUMNS[0].color,
        ),
    ]
    worded: list[Part] = []
    numbered: list[Part] = []
    for mark, count, label, color in items:
        worded += [(f"  {mark} ", STYLE_PLAIN, color), (f"{count} {label}", STYLE_PLAIN, None)]
        numbered += [(f" {mark}", STYLE_PLAIN, color), (str(count), STYLE_PLAIN, None)]
    return [worded, numbered, []]


def title_segments(
    width: int, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[Segment]:
    clock = display.clock_time(now)
    title_parts: list[Part] = [(BOARD_TITLE, STYLE_BOLD, None)]
    options = summary_options(snapshot, glyphs, now)
    summary = next(
        (
            option
            for option in options
            if len(BOARD_TITLE) + parts_width(option) + len(clock) + 2 < width
        ),
        options[-1],
    )
    segments = part_segments(0, 0, [*title_parts, *summary])
    if len(BOARD_TITLE) + parts_width(summary) + len(clock) + 2 < width:
        segments.append(Segment(0, width - 1 - len(clock), clock, STYLE_DIM))
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
    row: display.ListRow | None,
    snapshot: BoardSnapshot,
    glyphs: Glyphs,
    now: datetime,
) -> list[Segment]:
    line_width = width - 1
    lines: list[list[Part]] = [[("No task selected", STYLE_DIM, None)]]
    if row is not None:
        lines = detail_lines(row, line_width, snapshot, glyphs, now)
    rule_y = footer_y - 1 - DETAIL_PANE_LINE_COUNT
    segments = [Segment(rule_y, 0, glyphs.horizontal * line_width, color=FAINT_COLOR)]
    for line_offset, parts in enumerate(lines, start=1):
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


def detail_lines(
    row: display.ListRow, line_width: int, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[list[Part]]:
    directory = display.display_directory(row.directory)
    lines: list[list[Part]] = [
        task_heading_parts(row, line_width, snapshot, glyphs, now),
        [(tail_shortened_directory(directory, line_width, glyphs), STYLE_DIM, None)],
    ]
    task = snapshot.tasks[row.task_id]
    reason = row_reason(row, task)
    if reason:
        lines.append([(fitted(reason, line_width, glyphs), STYLE_PLAIN, None)])
    remaining_count = DETAIL_PANE_LINE_COUNT - len(lines)
    activity = snapshot.activity.get(row.task_id, [])
    if activity:
        activity_parts: list[list[Part]] = [
            [(fitted(activity_text(line, glyphs), line_width, glyphs), STYLE_DIM, None)]
            for line in activity[-remaining_count:]
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
    if row.status == ledger.STATUS_RUNNING:
        actions.append(ACTION_WATCH)
    if chain_pull_requests(snapshot, row.task_id):
        actions.append(ACTION_OPEN_PULL_REQUEST)
    if row.column == display.COLUMN_REVIEW and row.status == ledger.STATUS_FINISHED:
        actions.append(ACTION_CLOSE)
    if row.status in CANCEL_STATUSES:
        actions.append(ACTION_CANCEL)
    return actions


def action_key(action: str) -> str:
    return next(chr(key) for key, key_action in ACTION_KEYS.items() if key_action == action)


def key_hint_segments(y: int, width: int, key_sets: list[list[tuple[str, str]]]) -> list[Segment]:
    keys = next(
        (
            key_set
            for key_set in key_sets
            if sum(len(key) + len(label) + 3 for key, label in key_set) - 2 < width
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
        prompt = f"{verb} {row.task_id}? "
        return [
            Segment(y, 0, prompt, STYLE_BOLD),
            Segment(y, len(prompt), "y", STYLE_BOLD, BOARD_COLUMNS[0].color),
            Segment(y, len(prompt) + 1, " confirm  any other key cancels", STYLE_DIM),
        ]
    if state.flash_text and time.monotonic() < state.flash_until:
        return [Segment(y, 0, fitted(state.flash_text, width - 1, glyphs), STYLE_BOLD)]
    return None


def footer_segments(
    y: int,
    width: int,
    state: BoardState,
    snapshot: BoardSnapshot,
    row: display.ListRow | None,
    glyphs: Glyphs,
) -> list[Segment]:
    status_segments = status_line_segments(y, width, state, row, glyphs)
    if status_segments is not None:
        return status_segments
    action_hints = [
        (action_key(action), ACTION_LABELS[action]) for action in available_actions(snapshot, row)
    ]
    open_hint = [(glyphs.enter_key, "open")] if row is not None else []
    key_sets = [
        [
            (glyphs.column_keys, "column"),
            (glyphs.select_keys, "select"),
            *open_hint,
            *action_hints,
            ("?", "help"),
            ("q", "quit"),
        ],
        [*open_hint, *action_hints, ("?", "help"), ("q", "quit")],
        [*open_hint, *[(key, "") for key, _ in action_hints], ("?", "help"), ("q", "quit")],
        [("?", "help"), ("q", "quit")],
        [("?", ""), ("q", "")],
    ]
    return key_hint_segments(y, width, key_sets)


def empty_board_segments(layout: BoardLayout, width: int) -> list[Segment]:
    middle_y = layout.body_top + max(0, layout.body_height // 2 - 1)
    return [
        Segment(
            middle_y + offset,
            max(0, (width - len(text)) // 2),
            text,
            STYLE_BOLD if offset == 0 else STYLE_DIM,
        )
        for offset, text in enumerate(EMPTY_BOARD_LINES)
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
    layout = board_layout(width, height, len(columns))
    segments = title_segments(width, snapshot, glyphs, now)
    if layout.fit_count < len(columns):
        segments += tab_segments(TITLE_LINE_COUNT, width, columns, layout, state, glyphs)
    body_bottom = layout.body_top + layout.body_height
    context = FrameContext(snapshot, glyphs, now)
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
        segments += empty_board_segments(layout, width)
    row = selected_row(snapshot, state)
    if layout.is_detail_shown:
        segments += detail_segments(height - 1, width, row, snapshot, glyphs, now)
    segments += footer_segments(height - 1, width, state, snapshot, row, glyphs)
    return segments


def wrapped_lines(text: str, width: int, style: str, indent: str = "  ") -> list[list[Part]]:
    lines: list[list[Part]] = []
    for paragraph in text.splitlines() or [""]:
        wrapped = textwrap.wrap(paragraph, max(1, width - len(indent))) or [""]
        paragraph_lines: list[list[Part]] = [[(f"{indent}{line}", style, None)] for line in wrapped]
        lines += paragraph_lines
    return lines


def moment_text(timestamp: str | None, now: datetime) -> str:
    if timestamp is None:
        return ""
    seconds = seconds_since(timestamp, now)
    relative = f"in {duration_text(-seconds)}" if seconds < 0 else f"{duration_text(seconds)} ago"
    return f"{display.display_time(timestamp, now.date())} ({relative})"


def task_view_lines(
    row: display.ListRow, width: int, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[list[Part]]:
    task = snapshot.tasks[row.task_id]
    due_at = task.get("due_at") if row.status == ledger.STATUS_SCHEDULED else None
    fields: list[tuple[str, str]] = [
        ("Directory", display.display_directory(row.directory)),
        ("Worktree", display.display_directory(task.get("worktree", ""))),
        ("Backend", task["backend"]),
        ("Scheduled", moment_text(due_at, now)),
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
    lines: list[list[Part]] = [task_heading_parts(row, width, snapshot, glyphs, now), []]
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
        ),
        ("Prompt", task.get("prompt", ""), STYLE_PLAIN),
        (
            "Activity",
            "\n".join(
                activity_text(line, glyphs) for line in snapshot.activity.get(row.task_id, [])
            ),
            STYLE_DIM,
        ),
        ("Result", task.get("result", ""), STYLE_PLAIN),
    ]
    for title, text, style in sections:
        if not text.strip():
            continue
        title_line: list[Part] = [(title, STYLE_BOLD, None)]
        lines += [[], title_line, *wrapped_lines(text, width, style)]
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
    state.task_scroll = max(0, min(state.task_scroll, len(lines) - body_height))
    segments: list[Segment] = []
    for offset, parts in enumerate(lines[state.task_scroll : state.task_scroll + body_height]):
        segments += part_segments(offset, 0, parts)
    status_segments = status_line_segments(height - 1, width, state, row, glyphs)
    if status_segments is not None:
        return segments + status_segments
    action_hints = [
        (action_key(action), ACTION_LABELS[action]) for action in available_actions(snapshot, row)
    ]
    position = f"{min(len(lines), state.task_scroll + body_height)}/{len(lines)}"
    key_sets = [
        [(glyphs.select_keys, "scroll"), *action_hints, ("esc", "back"), (position, "")],
        [*action_hints, ("esc", "back")],
        [("esc", "back")],
    ]
    return segments + key_hint_segments(height - 1, width, key_sets)


def help_sections(glyphs: Glyphs) -> list[tuple[str, list[tuple[str, str]]]]:
    return [
        (
            "Move",
            [
                (f"{glyphs.column_keys} h l", "switch column"),
                (f"{glyphs.select_keys} j k", "select task"),
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
                ("w", "watch its progress"),
                ("o", "open its pull request"),
                ("c", "close (move to Done)"),
                ("x", "cancel (if queued)"),
            ],
        ),
        ("Board", [("a", "show or hide Done"), ("?", "this help"), ("q", "quit")]),
    ]


def help_view_segments(width: int, height: int, glyphs: Glyphs) -> list[Segment]:
    rows: list[tuple[str, str, bool]] = []
    for section_index, (title, bindings) in enumerate(help_sections(glyphs)):
        if section_index:
            rows.append(("", "", False))
        rows.append((title, "", True))
        rows += [(key, label, False) for key, label in bindings]
    key_width = max(len(key) for key, _, is_title in rows if not is_title) + 3
    inner_width = key_width + max(len(label) for _, label, _ in rows) + 2
    box_width = min(width, inner_width + 4)
    box_height = min(height - 1, len(rows) + 4)
    left = max(0, (width - box_width) // 2)
    top = max(0, (height - 1 - box_height) // 2)
    corners = glyphs.corners
    title = " Keys "
    top_rule = glyphs.horizontal * max(0, box_width - 2 - len(title) - 1)
    bottom_rule = glyphs.horizontal * max(0, box_width - 3 - len(HELP_CLOSE_HINT))
    segments = [
        Segment(
            top, left, f"{corners[0]}{glyphs.horizontal}{title}{top_rule}{corners[1]}", STYLE_DIM
        ),
        Segment(
            top + box_height - 1,
            left,
            f"{corners[2]}{bottom_rule}{HELP_CLOSE_HINT}{glyphs.horizontal}{corners[3]}",
            STYLE_DIM,
        ),
    ]
    for offset in range(1, box_height - 1):
        segments += [
            Segment(top + offset, left, glyphs.vertical + " " * (box_width - 2), STYLE_DIM),
            Segment(top + offset, left + box_width - 1, glyphs.vertical, STYLE_DIM),
        ]
    for offset, (key, label, is_title) in enumerate(rows[: box_height - 4], start=2):
        if is_title:
            segments.append(
                Segment(top + offset, left + 2, key, STYLE_BOLD, BOARD_COLUMNS[1].color)
            )
            continue
        segments += [
            Segment(top + offset, left + 2, key, STYLE_BOLD),
            Segment(top + offset, left + 2 + key_width, label, STYLE_PLAIN),
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
    if state.view == VIEW_HELP:
        segments += help_view_segments(width, height, glyphs)
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
    elif key in ENTER_KEYS:
        state.view = VIEW_TASK
        state.task_scroll = 0
    elif key == ord("?"):
        state.view = VIEW_HELP


def apply_task_key(state: BoardState, key: int, page_size: int) -> None:
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
        state.view = VIEW_HELP


def apply_key(
    state: BoardState, key: int, snapshot: BoardSnapshot | None = None, height: int = 0
) -> None:
    rows = snapshot.rows if snapshot is not None else []
    columns = rows_by_column(rows, is_showing_all=state.is_showing_all)
    column_row_count = len(columns[min(state.focus_index, len(columns) - 1)][1])
    page_size = max(1, (height - 12) // CARD_PITCH)
    if state.view == VIEW_HELP:
        state.view = VIEW_BOARD
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
    fit_state_to_layout(snapshot.rows, width, height, state)
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


def run_captured(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False
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
    if action in {ACTION_PICKUP, ACTION_WATCH}:
        run_attached(screen, [*GOLEM_COMMAND, action, row.task_id])
        return
    if action == ACTION_OPEN_PULL_REQUEST:
        pull_requests = chain_pull_requests(snapshot, row.task_id)
        pull_request = next(
            (item for item in pull_requests if item["state"] == ledger.PULL_REQUEST_STATE_OPEN),
            pull_requests[0],
        )
        returncode, output = run_captured([OPEN_COMMAND, pull_request["url"]])
        flash(state, f"Opened {pull_request['url']}" if returncode == 0 else output)
        return
    returncode, output = run_captured([*GOLEM_COMMAND, action, row.task_id])
    flash(state, f"{ACTION_DONE_VERBS[action]} {row.task_id}" if returncode == 0 else output)


def handle_action_key(
    screen: curses.window, state: BoardState, snapshot: BoardSnapshot, key: int
) -> bool:
    if state.view == VIEW_HELP:
        return False
    row = selected_row(snapshot, state)
    if state.pending_action is not None:
        action, state.pending_action = state.pending_action, None
        if key == ord("y") and row is not None:
            run_action(screen, state, snapshot, action, row)
        return True
    action = ACTION_KEYS.get(key)
    if action is None or row is None:
        return False
    if action not in available_actions(snapshot, row):
        flash(state, UNAVAILABLE_ACTION_TEXTS[action])
        return True
    if action in CONFIRMED_ACTIONS:
        state.pending_action = action
        return True
    run_action(screen, state, snapshot, action, row)
    return True


def is_quit_key(state: BoardState, key: int) -> bool:
    return state.view == VIEW_BOARD and state.pending_action is None and key == ord("q")


def board_loop(screen: curses.window, *, is_color: bool, glyphs: Glyphs) -> int:
    task_ledger_path = ledger.ledger_path()
    state = BoardState()
    shown_source_key: tuple[LedgerStat, tuple[LedgerStat, ...], date] | None = None
    snapshot = BoardSnapshot([], {}, {})
    while True:
        now = datetime.now().astimezone()
        source_key = (file_stat(task_ledger_path), progress_stats(snapshot), now.date())
        if source_key != shown_source_key:
            snapshot = board_snapshot(ledger.load_tasks(), now)
            shown_source_key = (source_key[0], progress_stats(snapshot), now.date())
        draw_board(screen, snapshot, state, is_color=is_color, glyphs=glyphs)
        pressed_key = screen.getch()
        if pressed_key == -1:
            continue
        if pressed_key == curses.KEY_RESIZE:
            screen.clear()
            continue
        if is_quit_key(state, pressed_key):
            return 0
        if handle_action_key(screen, state, snapshot, pressed_key):
            shown_source_key = None
            continue
        apply_key(state, pressed_key, snapshot, screen.getmaxyx()[0])


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
