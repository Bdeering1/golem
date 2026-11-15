import contextlib
import curses
import locale
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, override

from golem import chains, display, ledger, progress_log, review

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import FrameType

MIN_COLUMN_WIDTH = 20
HIDDEN_COLUMN_MARGIN = 2
COLUMN_GAP = 2
RAIL_WIDTH = 1
REVIEW_SHARE_PERCENT = 40
MAX_REVIEW_SHARE_COLUMN_COUNT = 3
CARD_INDENT = 2
BOX_BORDER_WIDTH = 4
MIN_BOXED_CARD_WIDTH = 30
TAB_LINE_Y = 1
SPACED_HEIGHT = 24
DETAIL_LINE_COUNT = 3
MAX_DETAIL_LINE_COUNT = 6
PRIMARY_LINE_COUNT = 2
MAX_WORD_SNAP = 3
DAYS_PER_YEAR = 365
MIN_WHOLE_HOURS = 6
MIN_DETAIL_HEIGHT = 20
MIN_SUMMARY_HEIGHT = 14
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
REMEMBERED_COLOR = 237
SELECTION_BACKGROUND = 238
SELECTED_MUTED_COLOR = 250
SELECTED_TITLE_COLOR = 255
WIDE_REP_FIX_NCURSES_VERSION = (6, 0, 20170729)
REP_LINE_PREFIX = b"\trep="
TOOL_LINE_PREFIX = "[tool] "
SHELL_LINE_PREFIXES = ("[tool] command ", "[tool] Bash ")
FINISHED_AGE_SEPARATOR = "; finished "
TERMINFO_VARIABLE = "TERMINFO"
TERMINFO_SOURCE_NAME = "terminal.terminfo"
BOARD_TITLE = "golem"
NOT_A_TERMINAL_MESSAGE = "golem: board needs a terminal; use golem list"
HELP_CLOSE_HINT = " esc closes "
MIN_CARD_REASON_WIDTH = 10
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
STYLE_BADGE = "badge"
STYLE_ATTRIBUTES = {
    STYLE_PLAIN: curses.A_NORMAL,
    STYLE_BOLD: curses.A_BOLD,
    STYLE_DIM: curses.A_DIM,
    STYLE_BADGE: curses.A_BOLD | curses.A_REVERSE,
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
ACTION_CLOSE_ALL = "close-all"
ESCAPE_KEY = 27
CONTROL_D = 4
CONTROL_U = 21
ESCAPE_SEQUENCE_KEYS = {
    **dict.fromkeys(("[1~", "[7~", "[H", "OH"), curses.KEY_HOME),
    **dict.fromkeys(("[4~", "[8~", "[F", "OF"), curses.KEY_END),
}
ESCAPE_SEQUENCE_FINAL_KEYS = range(0x40, 0x7F)
ESCAPE_SEQUENCE_STARTS = "[O"
ENTER_KEYS = {ord("\n"), ord("\r"), curses.KEY_ENTER}
LEFT_KEYS = {ord("h"), curses.KEY_LEFT}
RIGHT_KEYS = {ord("l"), curses.KEY_RIGHT}
TAB_KEYS = {ord("\t"), curses.KEY_BTAB}
DOWN_KEYS = {ord("j"), curses.KEY_DOWN}
UP_KEYS = {ord("k"), curses.KEY_UP}
FIRST_KEYS = {ord("g"), curses.KEY_HOME}
LAST_KEYS = {ord("G"), curses.KEY_END}
PAGE_DOWN_KEYS = {curses.KEY_NPAGE, CONTROL_D, ord(" ")}
PAGE_UP_KEYS = {curses.KEY_PPAGE, CONTROL_U}
BACKSPACE_KEYS = {curses.KEY_BACKSPACE, 127, 8}
BACK_KEYS = {ESCAPE_KEY, ord("q"), *BACKSPACE_KEYS, *ENTER_KEYS}
FILTER_KEY = ord("/")
NEXT_ATTENTION_KEY = ord("n")
PRINTABLE_KEYS = range(32, 127)
ACTION_KEYS = {
    ACTION_PICKUP: "p",
    ACTION_WATCH: "w",
    ACTION_OPEN_PULL_REQUEST: "o",
    ACTION_RETRY: "r",
    ACTION_RERUN_GATE: "r",
    ACTION_CLOSE: "c",
    ACTION_CLOSE_ALL: "C",
    ACTION_CANCEL: "x",
    ACTION_COPY: "y",
}
CONFIRMED_ACTIONS = {ACTION_CLOSE, ACTION_CLOSE_ALL, ACTION_CANCEL, ACTION_RETRY}
ACTION_LABELS = {
    ACTION_PICKUP: "pickup",
    ACTION_WATCH: "log",
    ACTION_OPEN_PULL_REQUEST: "PR",
    ACTION_RETRY: "retry",
    ACTION_RERUN_GATE: "recheck",
    ACTION_CLOSE: "close",
    ACTION_CLOSE_ALL: "close all",
    ACTION_CANCEL: "cancel",
    ACTION_COPY: "copy id",
}
ACTION_DONE_VERBS = {ACTION_CLOSE: "Closed", ACTION_CANCEL: "Cancelled"}
FOOTER_HIDDEN_ACTIONS = {ACTION_COPY, ACTION_CLOSE_ALL}
STOPPED_STATUSES = {ledger.STATUS_BLOCKED, ledger.STATUS_FAILED}
BOARD_STATUS_ORDER = (
    ledger.STATUS_FAILED,
    *ledger.LIST_STATUS_ORDER[:1],
    *ledger.LIST_STATUS_ORDER[2:],
)
LIVE_STATUSES = {ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING}
EXIT_PREFIX_PATTERN = re.compile(r"^[^:]{1,40} exited (?:with status )?\d+:\s*")
PATH_PREFIX_PATTERN = re.compile(r"[~/]\S*/")
COMMAND_RUNNER_PATTERN = re.compile(
    r"^\$ (?:uv run|poetry run|pipenv run|npx|bunx|pnpm exec|bundle exec) "
)
SHORT_REASON_PATTERNS = (
    (re.compile(r"^(?:fatal|error): "), ""),
    (re.compile(r"a branch named '[^']+' already exists"), "branch exists"),
    (re.compile(r"\b(\w+) found (\d+)"), r"\1: \2"),
)
GATE_COUNT_PATTERN = re.compile(r"(\d+) failed, (\d+) passed")
COUNT_NOUN_PATTERN = re.compile(r"(\d+) (?:problem|error|warning|issue)s?\b")
DURATION_PART_PATTERN = re.compile(r"(\d+)([a-z])")
DURATION_UNIT_SIZES = {"m": 60, "h": 24}
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
NO_ACTIVITY_TEXT = "No activity yet"
GATE_CHECK_TEXT = "rechecking the gate…"
NO_CHANGE_TEXT = "No change"
LEDGER_ERROR_TEXT = "Can't read the ledger, showing the last good state: "
STALE_TEXT = "stale"
NOTHING_NEEDS_YOU_TEXT = "Nothing needs you right now"
WAITING_DIRECTORY_TEXT = "waiting for directory:"
BUSY_DIRECTORY_TEXT = "dir busy:"
SHORT_BUSY_TEXT = "busy:"
STUCK_PREFIX = "stuck "
THREAD_STATE_TEXTS = {"thread unsettled": "thread open, no PR"}
SHORT_REASON_TEXTS = {"thread open, no PR": "thread, no PR"}
EMPTY_BOARD_LINES = ("No tasks yet", "Queue one with", 'golem run <id> "<prompt>"')
UNAVAILABLE_ACTION_TEXTS = {
    ACTION_PICKUP: "Only blocked, failed or finished tasks can be picked up",
    ACTION_WATCH: "This task has no log yet",
    ACTION_OPEN_PULL_REQUEST: "This task has no pull request",
    ACTION_RETRY: "Only failed, cancelled or blocked tasks can be retried",
    ACTION_CLOSE: "Only finished tasks in Review can be closed",
    ACTION_CLOSE_ALL: "Review has no finished tasks to close",
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
    background: int | None = None


class CardStyle(NamedTuple):
    height: int
    pitch: int
    is_boxed: bool


class Glyphs(NamedTuple):
    horizontal: str
    heavy_horizontal: str
    focus_marker: str
    remembered_marker: str
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
    heavy_corners: str
    heavy_vertical: str
    cursor: str
    new_mark: str
    idle_mark: str


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
    checking_task_ids: frozenset[str] = frozenset()


class BoardLayout(NamedTuple):
    fit_count: int
    column_spans: tuple[tuple[int, int], ...]
    header_y: int
    body_top: int
    body_height: int
    detail_line_count: int
    footer_y: int
    card_styles: tuple[CardStyle, ...]

    def visible_card_count(self, column_index: int, row_count: int) -> int:
        return visible_count(row_count, self.card_styles[column_index], self.body_height)


def visible_count(row_count: int, style: CardStyle, body_height: int) -> int:
    gap_height = style.pitch - style.height
    if row_count * style.pitch - gap_height <= body_height:
        return row_count
    # One row stays free under the last card for the "more" marker.
    return max(1, (body_height - 1 + gap_height) // style.pitch)


def column_used_height(row_count: int, style: CardStyle, body_height: int) -> int:
    shown_count = visible_count(row_count, style, body_height)
    marker_height = 1 if shown_count < row_count else 0
    return max(1, shown_count * style.pitch - (style.pitch - style.height) + marker_height)


type LedgerStat = tuple[int, int, int] | None
type Part = tuple[str, str, int | None]

BOXED_CARD = CardStyle(height=4, pitch=5, is_boxed=True)
STACKED_BOXED_CARD = CardStyle(height=4, pitch=4, is_boxed=True)
TALL_CARD = CardStyle(height=3, pitch=4, is_boxed=False)
SPACED_CARD = CardStyle(height=2, pitch=3, is_boxed=False)
DENSE_CARD = CardStyle(height=2, pitch=2, is_boxed=False)

BOARD_COLUMNS = (
    BoardColumn(display.COLUMN_QUEUED, display.STATUS_COLORS[ledger.STATUS_SCHEDULED]),
    BoardColumn(display.COLUMN_RUNNING, display.STATUS_COLORS[ledger.STATUS_RUNNING]),
    BoardColumn(display.COLUMN_REVIEW, display.STATUS_COLORS[ledger.STATUS_BLOCKED]),
    BoardColumn(display.COLUMN_DONE, display.STATUS_COLORS[ledger.STATUS_FINISHED]),
)
REVIEW_COLUMN_INDEX = [column.title for column in BOARD_COLUMNS].index(display.COLUMN_REVIEW)
PAIR_COLORS = sorted(
    {
        *display.STATUS_COLORS.values(),
        *(column.color for column in BOARD_COLUMNS),
        MUTED_COLOR,
        FAINT_COLOR,
        REMEMBERED_COLOR,
        SELECTED_MUTED_COLOR,
        SELECTED_TITLE_COLOR,
    }
)
COLOR_PAIRS: dict[tuple[int | None, int | None], int] = {
    color_key: pair
    for pair, color_key in enumerate(
        [
            *((color, None) for color in PAIR_COLORS),
            *((color, SELECTION_BACKGROUND) for color in [None, *PAIR_COLORS]),
        ],
        start=1,
    )
}
UNICODE_GLYPHS = Glyphs(
    horizontal="─",
    heavy_horizontal="━",
    focus_marker="▌",
    remembered_marker="▏",
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
    heavy_corners="┏┓┗┛",
    heavy_vertical="┃",
    cursor="▏",
    new_mark="●",
    idle_mark="○",
)
ASCII_GLYPHS = Glyphs(
    horizontal="-",
    heavy_horizontal="=",
    focus_marker=">",
    remembered_marker="'",
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
    heavy_corners="####",
    heavy_vertical="#",
    cursor="_",
    new_mark="*",
    idle_mark="o",
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
    stale_snapshot_time: datetime | None = None
    focus_before_done: int = 0
    changed_until: dict[str, float] = field(default_factory=dict[str, float])
    gate_checks: dict[str, subprocess.Popen[str]] = field(
        default_factory=dict[str, "subprocess.Popen[str]"]
    )


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
    status_index = BOARD_STATUS_ORDER.index(row.status)
    if row.column == display.COLUMN_QUEUED:
        moment = timestamp_seconds(task.get("due_at"))
    elif row.column == display.COLUMN_RUNNING:
        moment = timestamp_seconds(task.get("started_at"))
    else:
        moment = -timestamp_seconds(task.get("finished_at") or task.get("started_at"))
    return column_index, status_index, moment, row.task_id


def board_snapshot(tasks: ledger.Tasks, now: datetime) -> BoardSnapshot:
    rows = sorted(display.list_rows(tasks, now.date()), key=lambda row: board_sort_key(row, tasks))
    activity = {
        row.task_id: recent_activity(tasks[row.task_id]["session_id"])
        for row in rows
        if row.status == ledger.STATUS_RUNNING
    }
    return BoardSnapshot(rows, tasks, activity, rows)


def is_matching(row: display.ListRow, tasks: ledger.Tasks, query: str) -> bool:
    searched_text = " ".join(
        f"{task_id} {tasks[task_id].get('prompt', '')}"
        for task_id in chains.chain_task_ids(tasks, row.task_id)
    )
    return query.casefold() in f"{row.directory} {searched_text}".casefold()


def filtered_snapshot(snapshot: BoardSnapshot, state: BoardState) -> BoardSnapshot:
    if not state.filter_text:
        return snapshot
    rows = [row for row in snapshot.rows if is_matching(row, snapshot.tasks, state.filter_text)]
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
        return 1 if height >= MIN_SUMMARY_HEIGHT else 0
    return DETAIL_LINE_COUNT


def card_style(row_count: int, body_height: int, *, can_box: bool, can_stack: bool) -> CardStyle:
    styles = (BOXED_CARD, STACKED_BOXED_CARD, SPACED_CARD) if can_box else (TALL_CARD, SPACED_CARD)
    if not can_stack:
        styles = tuple(style for style in styles if style != TALL_CARD)
    for style in styles:
        if row_count * style.pitch - (style.pitch - style.height) <= body_height:
            return style
    return DENSE_CARD


def attention_column_index(rows: list[display.ListRow]) -> int:
    columns = rows_by_column(rows, is_showing_all=False)
    attention_order = (display.COLUMN_REVIEW, display.COLUMN_RUNNING, display.COLUMN_QUEUED)
    titles = [board_column.title for board_column, _ in columns]
    for title in attention_order:
        if columns[titles.index(title)][1]:
            return titles.index(title)
    return 0


def column_spans(
    width: int, fit_count: int, column_count: int, first_index: int
) -> tuple[tuple[int, int], ...]:
    is_split = fit_count > 1
    left_margin = HIDDEN_COLUMN_MARGIN if is_split and first_index > 0 else RAIL_WIDTH
    has_right_hidden = first_index + fit_count < column_count
    right_margin = HIDDEN_COLUMN_MARGIN if is_split and has_right_hidden else 0
    total_width = width - left_margin - right_margin - (fit_count - 1) * COLUMN_GAP
    widths = [total_width // fit_count] * fit_count
    review_offset = REVIEW_COLUMN_INDEX - first_index
    if 1 < fit_count <= MAX_REVIEW_SHARE_COLUMN_COUNT and 0 <= review_offset < fit_count:
        review_width = max(
            widths[0],
            min(
                total_width * REVIEW_SHARE_PERCENT // 100,
                total_width - (fit_count - 1) * MIN_BOXED_CARD_WIDTH,
            ),
        )
        other_width = (total_width - review_width) // (fit_count - 1)
        widths = [other_width] * fit_count
        widths[review_offset] = review_width
    spans: list[tuple[int, int]] = []
    x = left_margin
    for column_width in widths:
        spans.append((x, column_width))
        x += column_width + COLUMN_GAP
    return tuple(spans)


def board_layout(
    width: int,
    height: int,
    column_sizes: tuple[int, ...],
    *,
    first_index: int = 0,
) -> BoardLayout:
    column_count = len(column_sizes)
    fit_count = min(column_count, max(1, (width + COLUMN_GAP) // (MIN_COLUMN_WIDTH + COLUMN_GAP)))
    first_index = min(first_index, column_count - fit_count)
    spans = column_spans(width, fit_count, column_count, first_index)
    spacer_row_count = 1 if height >= SPACED_HEIGHT else 0
    header_y = TAB_LINE_Y + spacer_row_count
    footer_y = height - (2 if fit_count == 1 else 1)
    line_count = detail_line_count(height)
    detail_height = line_count + 1 + spacer_row_count if line_count else 0
    body_top = header_y + 2 + spacer_row_count
    body_height = footer_y - detail_height - body_top
    can_box = min(span_width for _, span_width in spans) >= MIN_BOXED_CARD_WIDTH
    done_index = len(BOARD_COLUMNS) - 1
    styles = tuple(
        card_style(size, body_height, can_box=can_box, can_stack=index != done_index)
        for index, size in enumerate(column_sizes)
    )
    shown_indices = range(first_index, first_index + fit_count)
    for packed_style, spaced_style in ((DENSE_CARD, SPACED_CARD), (STACKED_BOXED_CARD, BOXED_CARD)):
        if any(styles[index] == packed_style for index in shown_indices):
            styles = tuple(
                packed_style if index in shown_indices and style == spaced_style else style
                for index, style in enumerate(styles)
            )
    if line_count:
        used_height = max(
            column_used_height(column_sizes[index], styles[index], body_height)
            for index in shown_indices
        )
        grown_count = min(max(0, body_height - used_height), MAX_DETAIL_LINE_COUNT - line_count)
        line_count += grown_count
        body_height -= grown_count
    return BoardLayout(
        fit_count, spans, header_y, body_top, body_height, line_count, footer_y, styles
    )


def snapshot_layout(
    snapshot: BoardSnapshot, width: int, height: int, state: BoardState
) -> BoardLayout:
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    return board_layout(
        width,
        height,
        tuple(len(column_rows) for _, column_rows in columns),
        first_index=state.first_column_index,
    )


def fit_state_to_layout(
    snapshot: BoardSnapshot, width: int, height: int, state: BoardState
) -> None:
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    state.focus_index = min(state.focus_index, len(columns) - 1)
    if state.filter_text and not columns[state.focus_index][1]:
        state.focus_index = next(
            (index for index, (_, column_rows) in enumerate(columns) if column_rows),
            state.focus_index,
        )
    layout = snapshot_layout(snapshot, width, height, state)
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
        visible_count = layout.visible_card_count(column_index, len(column_rows))
        scroll_offset = min(state.scroll_offsets[column_index], selected_index)
        scroll_offset = max(scroll_offset, selected_index - visible_count + 1)
        scroll_offset = min(scroll_offset, max(0, len(column_rows) - visible_count))
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
    return text[: max(0, width - 1)].rstrip() + glyphs.ellipsis


def fitted_words(text: str, width: int, glyphs: Glyphs) -> str:
    if len(text) <= width:
        return text
    head = text[: max(0, width - 1)]
    clause_end = max(head.rfind(", "), head.rfind(f" {glyphs.separator} "))
    if clause_end > width // 2:
        return head[:clause_end] + glyphs.ellipsis
    word_end = head.rfind(" ")
    is_word_cut = text[len(head)] != " "
    is_label_kept = not head[:word_end].endswith(":")
    if is_word_cut and is_label_kept and word_end > 0 and len(head) - word_end <= MAX_WORD_SNAP:
        head = head[:word_end]
    head = head.rstrip(f" ,:{glyphs.separator}")
    last_word_start = head.rfind(" ") + 1
    if last_word_start and head[last_word_start:].isdigit():
        head = head[:last_word_start].rstrip(f" ,:{glyphs.separator}")
    return head + glyphs.ellipsis


def fitted_task_id(task_id: str, width: int, glyphs: Glyphs) -> str:
    tail = task_id[task_id.rfind("-") :] if "-" in task_id else ""
    if len(task_id) <= width or not tail or len(tail) > width // 2:
        return fitted(task_id, width, glyphs)
    head = task_id[: width - len(tail) - len(glyphs.ellipsis)]
    word_end = head.rfind("-")
    is_word_cut = task_id[len(head)] != "-"
    if is_word_cut and word_end > 0 and len(head) - word_end <= MAX_WORD_SNAP:
        head = head[:word_end]
    return f"{head.rstrip('-')}{glyphs.ellipsis}{tail}"


def duration_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if not minutes:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes}m"
    days, hours = divmod(hours, 24)
    if not days:
        if hours >= MIN_WHOLE_HOURS:
            return f"{hours}h"
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
    stopped_at = finished_at or started_at
    if status in PICKUP_STATUSES and stopped_at is not None:
        return f"{duration_text(seconds_since(stopped_at, now))} ago"
    return ""


def row_when_text(row: display.ListRow, task: ledger.Task, now: datetime) -> str:
    if is_stuck(row, task, now):
        return f"{STUCK_PREFIX}{short_age_text(task.get('due_at'), now)}"
    return when_text(task, now)


def status_label_and_when(
    row: display.ListRow, task: ledger.Task, now: datetime
) -> tuple[str, str]:
    if is_stuck(row, task, now):
        return row_when_text(row, task, now), ""
    return row.status, row_when_text(row, task, now)


def activity_text(line: str, glyphs: Glyphs) -> str:
    shell_prefix = next((prefix for prefix in SHELL_LINE_PREFIXES if line.startswith(prefix)), None)
    if shell_prefix is not None:
        return f"$ {line.removeprefix(shell_prefix)}"
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
        reason = display.list_reason(task).replace(str(Path.home()), "~")
        return reason.replace(WAITING_DIRECTORY_TEXT, BUSY_DIRECTORY_TEXT)
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


def short_pull_request_text(pull_requests: list[ledger.PullRequest]) -> str:
    if len(pull_requests) == 1:
        return pull_request_text(pull_requests)
    states = Counter(pull_request["state"] for pull_request in pull_requests)
    if len(states) > 1 and ledger.PULL_REQUEST_STATE_OPEN in states:
        return f"{len(pull_requests)} PRs, {states[ledger.PULL_REQUEST_STATE_OPEN]} open"
    return "PRs " + ", ".join(f"{count} {state}" for state, count in states.items())


def short_reason_text(row: display.ListRow, reason: str, snapshot: BoardSnapshot) -> str:
    pull_requests = chain_pull_requests(snapshot, row.task_id)
    if row.status == ledger.STATUS_FINISHED and pull_requests:
        return short_pull_request_text(pull_requests)
    reason = SHORT_REASON_TEXTS.get(reason, reason).replace(
        WAITING_DIRECTORY_TEXT, BUSY_DIRECTORY_TEXT
    )
    reason = EXIT_PREFIX_PATTERN.sub("", reason)
    for pattern, replacement in SHORT_REASON_PATTERNS:
        reason = pattern.sub(replacement, reason)
    return reason


def card_facts(row: display.ListRow, context: FrameContext) -> tuple[str, str, str, str]:
    snapshot, glyphs, now = context.snapshot, context.glyphs, context.now
    task = snapshot.tasks[row.task_id]
    activity = snapshot.activity.get(row.task_id)
    reason = (
        COMMAND_RUNNER_PATTERN.sub("$ ", activity_text(activity[-1], glyphs))
        if activity
        else row_reason(row, snapshot)
    )
    if row.task_id in context.checking_task_ids:
        reason = GATE_CHECK_TEXT
    if row.status == ledger.STATUS_SCHEDULED:
        reason = PATH_PREFIX_PATTERN.sub("", reason)
    if row.status in LIVE_STATUSES:
        time_text = when_text(task, now)
    elif row.status == ledger.STATUS_WAITING:
        time_text = ""
    else:
        time_text = short_age_text(task.get("finished_at") or task.get("started_at"), now)
    if is_stuck(row, task, now):
        time_text = f"{STUCK_PREFIX}{short_age_text(task.get('due_at'), now)}"
    short_reason = short_reason_text(row, reason, snapshot)
    return time_text, reason, short_reason, Path(row.directory).name


def coarse_time_text(time_text: str) -> str:
    words = time_text.split()
    matches = [DURATION_PART_PATTERN.fullmatch(word) for word in words[-2:]]
    if len(words) <= 1 or None in matches:
        return time_text
    (count, unit), (part_count, part_unit) = (match.groups() for match in matches if match)
    is_countdown = bool(words[:-2])
    is_rounding_up = is_countdown and int(part_count) * 2 >= DURATION_UNIT_SIZES.get(
        part_unit, sys.maxsize
    )
    return " ".join([*words[:-2], f"{int(count) + is_rounding_up}{unit}"])


def card_detail(
    facts: tuple[str, str, str, str],
    width: int,
    glyphs: Glyphs,
    *,
    is_live: bool,
) -> str:
    time_text, reason, short_reason, repository = facts
    separator = f" {glyphs.separator} "
    timed_facts = separator.join(part for part in (time_text, repository) if part)
    coarse_facts = separator.join(
        part for part in (coarse_time_text(time_text), repository) if part
    )
    fitted_facts = next(
        (option for option in (timed_facts, coarse_facts) if len(option) <= width), time_text
    )
    if not reason:
        return fitted_facts
    if not is_live:
        compact_reason = COUNT_NOUN_PATTERN.sub(
            r"\1", GATE_COUNT_PATTERN.sub(r"\1 fail/\2 pass", short_reason)
        )
        options = [
            separator.join(part for part in parts if part)
            for parts in (
                (time_text, reason, repository),
                (time_text, short_reason, repository),
                (time_text, compact_reason, repository),
                (time_text, reason),
                (time_text, short_reason),
                (time_text, compact_reason),
            )
        ]
        if short_reason.startswith(BUSY_DIRECTORY_TEXT) and time_text:
            busy_text = short_reason.replace(BUSY_DIRECTORY_TEXT, SHORT_BUSY_TEXT)
            options.append(separator.join((time_text, busy_text)))
        return next((option for option in options if len(option) <= width), options[-1])
    if not timed_facts:
        return reason
    reason_width = width - len(timed_facts) - len(separator)
    if reason_width < MIN_CARD_REASON_WIDTH:
        return fitted_facts
    return f"{timed_facts}{separator}{fitted(reason, reason_width, glyphs)}"


def is_stuck(row: display.ListRow, task: ledger.Task, now: datetime) -> bool:
    due_at = task.get("due_at")
    if row.status != ledger.STATUS_SCHEDULED or due_at is None:
        return False
    return bool(display.list_reason(task)) and seconds_since(due_at, now) > 0


def status_mark(status: str, glyphs: Glyphs, now: datetime) -> str:
    if status == ledger.STATUS_RUNNING:
        frame = int(now.timestamp() * 1000) // TICK_MILLISECONDS
        return glyphs.spinner[frame % len(glyphs.spinner)]
    return glyphs.status_marks[status]


def is_new_for_review(snapshot: BoardSnapshot, row: display.ListRow) -> bool:
    if row.column != display.COLUMN_REVIEW:
        return False
    if row.status == ledger.STATUS_FAILED:
        return not ledger.is_counted_opened(snapshot.tasks, row.task_id)
    return chains.is_ready_to_open(snapshot.tasks, row.task_id)


def status_time_text(row: display.ListRow, context: FrameContext, time_text: str) -> str:
    task = context.snapshot.tasks[row.task_id]
    if is_stuck(row, task, context.now):
        return time_text
    return when_text(task, context.now).removesuffix(" ago") or time_text


def is_live_card(row: display.ListRow, context: FrameContext) -> bool:
    task = context.snapshot.tasks[row.task_id]
    return row.status in LIVE_STATUSES and not is_stuck(row, task, context.now)


def card_badge_parts(row: display.ListRow, context: FrameContext) -> list[Part]:
    parts: list[Part] = [(row.step, STYLE_DIM, None)] if row.step else []
    if is_new_for_review(context.snapshot, row):
        parts += [
            (" " if parts else "", STYLE_PLAIN, None),
            (context.glyphs.new_mark, STYLE_BOLD, display.STATUS_COLORS[row.status]),
        ]
    return parts


def is_status_implied(row: display.ListRow, context: FrameContext) -> bool:
    is_done = row.column == display.COLUMN_DONE and row.status == ledger.STATUS_FINISHED
    return is_done or is_stuck(row, context.snapshot.tasks[row.task_id], context.now)


def card_colors(
    row: display.ListRow, context: FrameContext, *, is_selected: bool
) -> tuple[int, int]:
    color = display.STATUS_COLORS[row.status]
    if is_stuck(row, context.snapshot.tasks[row.task_id], context.now):
        color = display.STATUS_COLORS[ledger.STATUS_BLOCKED]
    is_quiet = row.column == display.COLUMN_DONE and not is_selected
    return color, MUTED_COLOR if is_quiet else color


def card_title_style(row: display.ListRow, context: FrameContext, *, is_selected: bool) -> str:
    is_loud = (
        row.status in LOUD_STATUSES
        or row.task_id in context.changed_task_ids
        or is_new_for_review(context.snapshot, row)
        or is_stuck(row, context.snapshot.tasks[row.task_id], context.now)
    )
    return (
        STYLE_BOLD
        if is_selected or (is_loud and row.column != display.COLUMN_DONE)
        else STYLE_PLAIN
    )


def highlighted(segments: list[Segment], background: int | None) -> list[Segment]:
    return [segment._replace(background=background) for segment in segments]


def card_segments(
    y: int,
    x: int,
    width: int,
    row: display.ListRow,
    context: FrameContext,
    *,
    is_selected: bool,
    is_tall: bool = False,
) -> list[Segment]:
    glyphs, now, changed_task_ids = context.glyphs, context.now, context.changed_task_ids
    color, mark_color = card_colors(row, context, is_selected=is_selected)
    right_parts = card_badge_parts(row, context)
    badge_gap = 2 if row.step else 1
    right_width = parts_width(right_parts) + (badge_gap if right_parts else 0)
    title = fitted_task_id(row.task_id, width - CARD_INDENT, glyphs)
    detail_width = width - CARD_INDENT - right_width
    time_text, reason, short_reason, repository = card_facts(row, context)
    is_live = is_live_card(row, context)
    detail_facts = ("" if is_tall else time_text, reason, short_reason, repository)
    detail_y = y + 2 if is_tall else y + 1
    detail_text_width = width - CARD_INDENT if is_tall else detail_width
    detail = fitted_words(
        card_detail(detail_facts, detail_text_width, glyphs, is_live=is_live),
        detail_text_width,
        glyphs,
    )
    is_warning = is_stuck(row, context.snapshot.tasks[row.task_id], now)
    stuck_ages = (time_text, coarse_time_text(time_text.removeprefix(STUCK_PREFIX)))
    warning_text = next(
        (age for age in stuck_ages if is_warning and age and detail.startswith(age)), ""
    )
    detail_parts: list[Part] = [
        (warning_text, STYLE_PLAIN, mark_color),
        (detail.removeprefix(warning_text), STYLE_DIM, None),
    ]
    is_changed = row.task_id in changed_task_ids
    title_color = color if is_changed else (SELECTED_TITLE_COLOR if is_selected else None)
    background = SELECTION_BACKGROUND if is_selected else None
    line_count = 3 if is_tall else 2
    fill = (
        [Segment(y + offset, x, " " * width) for offset in range(line_count)] if is_selected else []
    )
    mark_style = STYLE_BADGE if is_changed else STYLE_BOLD
    status_parts: list[Part] = []
    if is_tall:
        status_time = status_time_text(row, context, time_text)
        time_part = f" {glyphs.separator} {status_time}" if status_time else ""
        status_parts = [(row.status, STYLE_PLAIN, mark_color), (time_part, STYLE_DIM, None)]
        if is_status_implied(row, context):
            status_parts = [
                (status_time, STYLE_PLAIN, mark_color)
                if is_warning
                else (status_time, STYLE_DIM, None)
            ]
    rail = (
        [
            Segment(y + offset, x - RAIL_WIDTH, glyphs.focus_marker, color=color)
            for offset in range(line_count)
        ]
        if is_selected
        else []
    )
    return rail + highlighted(
        [
            *fill,
            Segment(y, x, status_mark(row.status, glyphs, now), mark_style, mark_color),
            Segment(
                y,
                x + CARD_INDENT,
                title,
                card_title_style(row, context, is_selected=is_selected),
                title_color,
            ),
            *part_segments(y + 1, x + CARD_INDENT, status_parts),
            *part_segments(detail_y, x + CARD_INDENT, detail_parts),
            *part_segments(y + 1, x + width - parts_width(right_parts), right_parts),
        ],
        background,
    )


def boxed_card_segments(
    y: int,
    x: int,
    width: int,
    row: display.ListRow,
    context: FrameContext,
    *,
    is_selected: bool,
) -> list[Segment]:
    glyphs, now, changed_task_ids = context.glyphs, context.now, context.changed_task_ids
    color, frame_color = card_colors(row, context, is_selected=is_selected)
    corners = glyphs.heavy_corners if is_selected else glyphs.corners
    horizontal = glyphs.heavy_horizontal if is_selected else glyphs.horizontal
    vertical = glyphs.heavy_vertical if is_selected else glyphs.vertical
    content_width = width - BOX_BORDER_WIDTH
    title = fitted_task_id(row.task_id, content_width, glyphs)
    is_changed = row.task_id in changed_task_ids
    title_style = card_title_style(row, context, is_selected=is_selected)
    top_parts: list[Part] = [
        (f"{corners[0]} ", STYLE_PLAIN, frame_color),
        (title, title_style, color if is_changed else None),
        (f" {horizontal * (content_width - len(title))}{corners[1]}", STYLE_PLAIN, frame_color),
    ]
    time_text, reason, short_reason, repository = card_facts(row, context)
    is_warning = is_stuck(row, context.snapshot.tasks[row.task_id], now)
    right_parts = card_badge_parts(row, context)
    status_width = content_width - parts_width(right_parts) - (1 if right_parts else 0)
    status_time = status_time_text(row, context, time_text)
    time_part = f" {glyphs.separator} {status_time}" if status_time else ""
    status_options = [
        (f" {row.status}", time_part),
        ("", f" {status_time}"),
        (f" {row.status}", ""),
    ]
    if is_status_implied(row, context) and time_text:
        status_options = status_options[1:]
    label, time_part = next(
        (option for option in status_options if 1 + len("".join(option)) <= status_width),
        status_options[-1],
    )
    status_parts: list[Part] = [
        (
            status_mark(row.status, glyphs, now),
            STYLE_BADGE if is_changed else STYLE_BOLD,
            frame_color,
        ),
        (fitted(label, status_width - 1, glyphs), STYLE_PLAIN, frame_color),
        (time_part, STYLE_PLAIN, frame_color) if is_warning else (time_part, STYLE_DIM, None),
    ]
    detail = card_detail(
        ("", reason, short_reason, repository),
        content_width,
        glyphs,
        is_live=is_live_card(row, context),
    )
    background = SELECTION_BACKGROUND if is_selected else None
    segments = part_segments(y, x, top_parts)
    for offset in (1, 2):
        segments += [
            Segment(y + offset, x, vertical, color=frame_color),
            *highlighted([Segment(y + offset, x + 1, " " * (width - 2))], background),
            Segment(y + offset, x + width - 1, vertical, color=frame_color),
        ]
    segments += highlighted(
        [
            *part_segments(y + 1, x + 2, status_parts),
            *part_segments(y + 1, x + width - 2 - parts_width(right_parts), right_parts),
            Segment(y + 2, x + 2, fitted_words(detail, content_width, glyphs), STYLE_DIM),
        ],
        background,
    )
    bottom = f"{corners[2]}{horizontal * (width - 2)}{corners[3]}"
    return [*segments, Segment(y + 3, x, bottom, color=frame_color)]


def column_segments(
    layout: BoardLayout,
    column_index: int,
    column: tuple[BoardColumn, list[display.ListRow]],
    state: BoardState,
    context: FrameContext,
) -> list[Segment]:
    glyphs = context.glyphs
    board_column, column_rows = column
    x, column_width = layout.column_spans[column_index - state.first_column_index]
    is_focused = column_index == state.focus_index
    rule = glyphs.heavy_horizontal if is_focused else glyphs.horizontal
    rule_x = 0 if layout.fit_count == 1 else x
    segments = [
        Segment(
            layout.header_y + 1,
            rule_x,
            rule * (column_width + x - rule_x),
            color=board_column.color,
        )
    ]
    visible_columns = rows_by_column(context.snapshot.rows, is_showing_all=state.is_showing_all)
    if not any(rows for _, rows in visible_columns):
        return segments
    if not column_rows:
        empty_text = (
            NO_MATCHES_TEXT if state.filter_text else EMPTY_COLUMN_TEXTS[board_column.title]
        )
        empty_text = fitted(empty_text, column_width - 2, glyphs)
        return [*segments, Segment(layout.body_top, x + 2, empty_text, STYLE_DIM)]
    scroll_offset = state.scroll_offsets[column_index]
    if scroll_offset:
        more_text = f" {glyphs.up} {scroll_offset} more "
        segments.append(Segment(layout.header_y + 1, x + 1, more_text, STYLE_DIM))
    visible_count = layout.visible_card_count(column_index, len(column_rows))
    shown_rows = column_rows[scroll_offset : scroll_offset + visible_count]
    style = layout.card_styles[column_index]
    for card_index, row in enumerate(shown_rows):
        is_selected = (
            is_focused and scroll_offset + card_index == state.selected_indices[column_index]
        )
        card_y = layout.body_top + card_index * style.pitch
        if not is_focused and scroll_offset + card_index == state.selected_indices[column_index]:
            segments.append(
                Segment(card_y, x - RAIL_WIDTH, glyphs.remembered_marker, color=REMEMBERED_COLOR)
            )
        if style.is_boxed:
            segments += boxed_card_segments(
                card_y, x, column_width, row, context, is_selected=is_selected
            )
            continue
        segments += card_segments(
            card_y,
            x,
            column_width,
            row,
            context,
            is_selected=is_selected,
            is_tall=style == TALL_CARD,
        )
    hidden_count = len(column_rows) - scroll_offset - len(shown_rows)
    if hidden_count > 0:
        marker_y = layout.body_top + len(shown_rows) * style.pitch - (style.pitch - style.height)
        more_text = f"{glyphs.down} {hidden_count} more"
        segments.append(Segment(marker_y, x + CARD_INDENT, more_text, STYLE_DIM))
    return segments


def header_row_segments(
    width: int,
    layout: BoardLayout,
    columns: list[tuple[BoardColumn, list[display.ListRow]]],
    state: BoardState,
    glyphs: Glyphs,
) -> list[Segment]:
    last_index = state.first_column_index + layout.fit_count - 1
    segments: list[Segment] = []
    if state.first_column_index:
        left_color = columns[state.first_column_index - 1][0].color
        segments.append(Segment(layout.header_y, 0, glyphs.left, STYLE_BOLD, left_color))
    header_end = 0
    for offset, (column_x, _) in enumerate(layout.column_spans):
        column_index = state.first_column_index + offset
        board_column, column_rows = columns[column_index]
        header_parts = column_header_parts(
            board_column, len(column_rows), glyphs, is_focused=column_index == state.focus_index
        )
        segments += part_segments(layout.header_y, column_x, header_parts)
        header_end = column_x + parts_width(header_parts)
    if last_index < len(columns) - 1:
        hidden_column, hidden_rows = columns[last_index + 1]
        hidden_parts: list[Part] = [
            (hidden_column.title, STYLE_PLAIN, hidden_column.color),
            (f" {len(hidden_rows)} ", STYLE_DIM, None),
            (glyphs.right, STYLE_BOLD, hidden_column.color),
        ]
        if header_end + COLUMN_GAP + parts_width(hidden_parts) > width:
            hidden_parts = hidden_parts[-1:]
        segments += part_segments(layout.header_y, width - parts_width(hidden_parts), hidden_parts)
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
    nearest_first = hidden_columns[::-1] if is_left else hidden_columns
    column_count = len(nearest_first)
    shapes = [
        (folded_count, is_farthest_short)
        for folded_count in range(column_count + 1)
        for is_farthest_short in (False, True)
        if folded_count < column_count or not is_farthest_short
    ]
    for folded_count, is_farthest_short in shapes:
        shown_columns = nearest_first[: column_count - folded_count]
        labels: list[list[Part]] = [
            [
                (
                    board_column.title[0]
                    if is_farthest_short and index == len(shown_columns) - 1
                    else board_column.title,
                    STYLE_PLAIN,
                    board_column.color,
                ),
                (f" {len(column_rows)}", STYLE_DIM, None),
            ]
            for index, (board_column, column_rows) in enumerate(shown_columns)
        ]
        folded: list[list[Part]] = [[(f"+{folded_count}", STYLE_DIM, None)]] if folded_count else []
        if is_left:
            left_arrow: list[Part] = [(glyphs.left, STYLE_DIM, None)]
            options.append(joined_parts([left_arrow, *folded, *labels[::-1]], 0))
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
        + (parts_width(right_options[right_index]) + 2 if right_options[right_index] else 0)
        > width
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
        *part_segments(y, middle_x + middle_width + 2, right_parts),
    ]


def summary_options(snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime) -> list[list[Part]]:
    columns = dict(rows_by_column(snapshot.rows, is_showing_all=True))
    counts = {board_column.title: len(column_rows) for board_column, column_rows in columns.items()}
    review_statuses = [row.status for row in columns[BOARD_COLUMNS[2]]]
    running_count = counts[display.COLUMN_RUNNING]
    running_mark = (
        status_mark(ledger.STATUS_RUNNING, glyphs, now) if running_count else glyphs.idle_mark
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
    tight: list[Part] = []
    numbered: list[Part] = []
    for mark, count, label, status, is_always_shown in items:
        if not count and not is_always_shown:
            continue
        mark_color = display.STATUS_COLORS[status] if count else MUTED_COLOR
        count_style = STYLE_PLAIN if count else STYLE_DIM
        mark_text = f"{mark} " if mark else ""
        worded += [
            (f"  {mark_text}", STYLE_PLAIN, mark_color),
            (f"{count} {label}", count_style, None),
        ]
        tight += [
            (f" {mark_text}", STYLE_PLAIN, mark_color),
            (f"{count} {label}", count_style, None),
        ]
        numbered += [(f" {mark_text}", STYLE_PLAIN, mark_color), (str(count), count_style, None)]
    return [worded, tight, numbered, []]


def title_segments(
    width: int, snapshot: BoardSnapshot, state: BoardState, glyphs: Glyphs, now: datetime
) -> list[Segment]:
    right_text = f"/{state.filter_text}" if state.filter_text else display.clock_time(now)
    right_style = STYLE_BOLD if state.filter_text else STYLE_DIM
    right_color = BOARD_COLUMNS[1].color if state.filter_text else None
    if state.stale_snapshot_time is not None:
        right_text, right_style = STALE_TEXT, STYLE_BOLD
        right_color = display.STATUS_COLORS[ledger.STATUS_BLOCKED]
    title_parts: list[Part] = [(BOARD_TITLE, STYLE_BOLD, None)]
    summaries = summary_options(snapshot, glyphs, now)
    options = [(option, right_text) for option in summaries[:-1]]
    options += [(option, "") for option in summaries[:-1]]
    options += [(summaries[-1], right_text), (summaries[-1], "")]
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
    line_width = width - 1
    text_width = line_width - CARD_INDENT
    lines: list[tuple[str, str]] = []
    is_id_in_title = row is not None and is_id_in_rule(row, line_width, context)
    id_lines: list[str] = []
    if row is not None and not is_id_in_title:
        id_lines = textwrap.wrap(row.task_id, text_width)
        is_id_in_title = len(id_lines) > 1 or line_count - len(id_lines) < PRIMARY_LINE_COUNT
        if is_id_in_title:
            id_lines = []
    if row is not None:
        content_count = line_count - len(id_lines)
        content = detail_lines(row, text_width, content_count, context.snapshot, context.glyphs)
        lines = [*((line, STYLE_BOLD) for line in id_lines), *content]
    rule_y = footer_y - 1 - max(1, line_count)
    segments = detail_title_segments(rule_y, line_width, row, context, is_id_shown=is_id_in_title)
    for line_offset, (text, style) in enumerate(lines, start=1):
        segments.append(Segment(rule_y + line_offset, CARD_INDENT, text, style))
    return segments


def compact_status_text(row: display.ListRow, context: FrameContext) -> str:
    label, when = status_label_and_when(row, context.snapshot.tasks[row.task_id], context.now)
    return when.removesuffix(" ago") or label


def is_id_in_rule(row: display.ListRow, line_width: int, context: FrameContext) -> bool:
    compact_width = len(compact_status_text(row, context)) + 4
    return CARD_INDENT + len(row.task_id) + 2 + compact_width + 2 <= line_width


def detail_title_segments(
    rule_y: int,
    line_width: int,
    row: display.ListRow | None,
    context: FrameContext,
    *,
    is_id_shown: bool,
) -> list[Segment]:
    snapshot, glyphs, now = context.snapshot, context.glyphs, context.now
    rule = glyphs.horizontal
    segments = [Segment(rule_y, 0, rule * line_width, color=FAINT_COLOR)]
    if row is None:
        return [*segments, Segment(rule_y, CARD_INDENT, " No task selected ", STYLE_DIM)]
    task = snapshot.tasks[row.task_id]
    color, _ = card_colors(row, context, is_selected=True)
    label, when = status_label_and_when(row, task, now)
    status_text = f" {status_mark(row.status, glyphs, now)} {label}"
    when_text_part = f" {glyphs.separator} {when}" if when else ""
    right_options: list[list[Part]] = [
        [(status_text, STYLE_PLAIN, color), (f"{when_text_part} ", STYLE_DIM, None)],
        [
            (status_text[:2], STYLE_PLAIN, color),
            (f" {compact_status_text(row, context)} ", STYLE_DIM, None),
        ],
    ]
    title_width = len(row.task_id) + 2 if is_id_shown else 0
    right_parts = next(
        (
            option
            for option in right_options
            if CARD_INDENT + title_width + parts_width(option) + 2 <= line_width
        ),
        right_options[-1],
    )
    id_width = max(1, line_width - CARD_INDENT - 2 - parts_width(right_parts) - 2)
    title_parts: list[Part] = [
        (f" {fitted_task_id(row.task_id, id_width, glyphs)} ", STYLE_BOLD, None)
    ]
    right_x = line_width - 1 - parts_width(right_parts)
    if is_id_shown:
        segments += part_segments(rule_y, CARD_INDENT - 1, title_parts)
    return [*segments, *part_segments(rule_y, right_x, right_parts)]


def paragraph_lines(text: str, width: int) -> list[str]:
    return [
        line
        for paragraph in text.splitlines()
        for line in textwrap.wrap(paragraph, width, break_on_hyphens=False)
    ]


def clipped_lines(lines: list[str], line_count: int, width: int, glyphs: Glyphs) -> list[str]:
    shown_lines = lines[:line_count]
    if len(lines) > line_count and shown_lines:
        shown_lines[-1] = fitted(f"{shown_lines[-1]} {lines[line_count]}", width, glyphs)
    return shown_lines


def detail_meta_text(row: display.ListRow, width: int, task: ledger.Task, glyphs: Glyphs) -> str:
    separator = f" {glyphs.separator} "
    facts = [f"step {row.step}" if row.step else "", task["backend"]]
    facts_text = "".join(f"{separator}{fact}" for fact in facts if fact)
    directory = display.display_directory(row.directory)
    directory_width = max(len(Path(row.directory).name), width - len(facts_text))
    return fitted(
        tail_shortened_directory(directory, directory_width, glyphs) + facts_text, width, glyphs
    )


def activity_lines(activity: list[str], width: int, row_count: int, glyphs: Glyphs) -> list[str]:
    texts = [activity_text(entry, glyphs) for entry in activity]
    wrapped_lines: list[str] = []
    shown_count = 0
    for text in reversed(texts):
        entry_lines = paragraph_lines(text, width)
        if len(wrapped_lines) + len(entry_lines) > row_count:
            break
        wrapped_lines = [*entry_lines, *wrapped_lines]
        shown_count += 1
    if shown_count == len(texts) or shown_count > 1:
        return wrapped_lines
    return [fitted(text, width, glyphs) for text in texts]


def detail_lines(
    row: display.ListRow,
    width: int,
    line_count: int,
    snapshot: BoardSnapshot,
    glyphs: Glyphs,
) -> list[tuple[str, str]]:
    task = snapshot.tasks[row.task_id]
    wrap_width = max(1, width)
    activity = snapshot.activity.get(row.task_id, [])
    reason = detail_reason_text(row, snapshot)
    primary_lines = paragraph_lines(reason, wrap_width)
    prompt_lines = paragraph_lines(" ".join(task.get("prompt", "").split()), wrap_width)
    if len(primary_lines) > PRIMARY_LINE_COUNT:
        short_reason = PATH_PREFIX_PATTERN.sub("", EXIT_PREFIX_PATTERN.sub("", reason))
        primary_lines = paragraph_lines(
            f" {glyphs.separator} ".join(short_reason.splitlines()), wrap_width
        )
    if activity:
        primary_lines = activity_lines(activity, wrap_width, max(1, line_count - 1), glyphs)
    primary_count = min(len(primary_lines), PRIMARY_LINE_COUNT, line_count)
    meta_count = min(1, line_count - primary_count)
    prompt_count = min(
        len(prompt_lines), 0 if activity else 1, line_count - primary_count - meta_count
    )
    spare_count = line_count - primary_count - prompt_count - meta_count
    extra_primary_count = min(len(primary_lines) - primary_count, spare_count)
    primary_count += extra_primary_count
    prompt_count += spare_count - extra_primary_count
    if activity:
        shown_primary = primary_lines[len(primary_lines) - primary_count :]
    else:
        shown_primary = clipped_lines(primary_lines, primary_count, width, glyphs)
    shown_prompt = clipped_lines(prompt_lines, prompt_count, width, glyphs)
    meta_lines = [detail_meta_text(row, width, task, glyphs)][:meta_count]
    lines = [
        *((line, STYLE_PLAIN) for line in shown_primary),
        *((line, STYLE_DIM) for line in shown_prompt),
        *((line, STYLE_DIM) for line in meta_lines),
    ]
    return lines[:line_count]


def task_heading_parts(
    row: display.ListRow, line_width: int, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[Part]:
    task = snapshot.tasks[row.task_id]
    color = display.STATUS_COLORS[row.status]
    label, when = status_label_and_when(row, task, now)
    status_text = f"  {status_mark(row.status, glyphs, now)} {label}"
    facts = [text for text in (when, f"step {row.step}" if row.step else "") if text]
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


def detail_reason_text(row: display.ListRow, snapshot: BoardSnapshot) -> str:
    task = snapshot.tasks[row.task_id]
    if row.status in STOPPED_STATUSES:
        return task.get("gate_stop_reason") or task.get("setup_failure_reason") or row.detail
    if row.status in {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}:
        return waiting_reason_text(row, snapshot)
    result = task.get("result", "").strip()
    if row.status == ledger.STATUS_FINISHED and result:
        return f"{row_reason(row, snapshot)}\n{result}"
    return row_reason(row, snapshot)


def chain_pull_requests(snapshot: BoardSnapshot, task_id: str) -> list[ledger.PullRequest]:
    chain_ids = chains.chain_task_ids(snapshot.tasks, task_id)
    return display.chain_pull_requests(snapshot.tasks, chain_ids)


def closable_task_ids(snapshot: BoardSnapshot) -> list[str]:
    return [
        row.task_id
        for row in snapshot.rows
        if row.column == display.COLUMN_REVIEW and row.status == ledger.STATUS_FINISHED
    ]


def available_actions(snapshot: BoardSnapshot, row: display.ListRow | None) -> list[str]:
    if row is None:
        return []
    actions: list[str] = []
    if row.status in PICKUP_STATUSES:
        actions.append(ACTION_PICKUP)
    session_id = snapshot.tasks[row.task_id]["session_id"]
    if row.status == ledger.STATUS_RUNNING or progress_log.progress_path(session_id).exists():
        actions.append(ACTION_WATCH)
    if chain_pull_requests(snapshot, row.task_id):
        actions.append(ACTION_OPEN_PULL_REQUEST)
    if row.status in RETRY_STATUSES:
        actions.append(ACTION_RETRY)
    if row.status == ledger.STATUS_BLOCKED:
        actions.append(ACTION_RERUN_GATE)
    if row.column == display.COLUMN_REVIEW and row.status == ledger.STATUS_FINISHED:
        actions.append(ACTION_CLOSE)
    if closable_task_ids(snapshot):
        actions.append(ACTION_CLOSE_ALL)
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
    y: int,
    width: int,
    state: BoardState,
    row: display.ListRow | None,
    glyphs: Glyphs,
    snapshot: BoardSnapshot,
) -> list[Segment] | None:
    if state.pending_action is not None and row is not None:
        verb = ACTION_LABELS[state.pending_action].capitalize()
        subject = row.task_id
        if state.pending_action == ACTION_CLOSE_ALL:
            verb, subject = "Close", f"all {len(closable_task_ids(snapshot))} finished tasks"
        answer = CONFIRM_ANSWER_TEXT
        if len(verb) + len(subject) + len(answer) + 4 >= width:
            answer = CONFIRM_SHORT_ANSWER_TEXT
        subject = fitted(subject, width - len(verb) - len(answer) - 5, glyphs)
        prompt = f"{verb} {subject}? "
        return [
            Segment(y, 0, prompt, STYLE_BOLD),
            Segment(y, len(prompt), "y", STYLE_BOLD, BOARD_COLUMNS[0].color),
            Segment(y, len(prompt) + 1, answer, STYLE_DIM),
        ]
    if state.flash_text and time.monotonic() < state.flash_until:
        return [Segment(y, 0, fitted(state.flash_text, width - 1, glyphs), STYLE_BOLD)]
    return None


def match_count_parts(snapshot: BoardSnapshot, state: BoardState) -> tuple[str, str]:
    titles = {
        board_column.title for board_column in visible_columns(is_showing_all=state.is_showing_all)
    }
    shown_rows, unfiltered_rows = [
        [row for row in rows if row.column in titles]
        for rows in (snapshot.rows, snapshot.unfiltered_rows)
    ]
    count_text = f"{len(shown_rows)}/{len(unfiltered_rows)}"
    done_count = sum(row.column == display.COLUMN_DONE for row in snapshot.rows)
    if state.is_showing_all or not done_count:
        return count_text, ""
    return count_text, f", {done_count} in Done"


def filter_input_segments(
    y: int, width: int, state: BoardState, snapshot: BoardSnapshot, glyphs: Glyphs
) -> list[Segment]:
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


def done_hint(state: BoardState) -> tuple[str, str]:
    return ("a", "hide done" if state.is_showing_all else "show done")


def narrow_footer_segments(
    y: int,
    width: int,
    state: BoardState,
    snapshot: BoardSnapshot,
    row: display.ListRow | None,
    glyphs: Glyphs,
) -> list[Segment]:
    if state.is_filtering:
        return filter_input_segments(y, width, state, snapshot, glyphs)
    if not snapshot.unfiltered_rows:
        status_segments = status_line_segments(y, width, state, row, glyphs, snapshot) or []
        return [
            *status_segments,
            *key_hint_segments(y + 1, width, [[("?", "help"), ("q", "quit")]]),
        ]
    is_flashing = state.flash_text and time.monotonic() < state.flash_until
    if is_flashing and state.pending_action is None and len(state.flash_text) >= width:
        flash_lines = textwrap.wrap(state.flash_text, width - 1)[:2]
        return [
            Segment(y + offset, 0, fitted(line, width - 1, glyphs), STYLE_BOLD)
            for offset, line in enumerate(flash_lines)
        ]
    action_segments = status_line_segments(y, width, state, row, glyphs, snapshot) or []
    if state.pending_action is not None and action_segments:
        return action_segments
    if not action_segments:
        open_hint = [(glyphs.enter_key, "open")] if row is not None else []
        action_hints = footer_action_hints(snapshot, row)
        bare_hints = [(key, "") for key, _ in action_hints]
        action_segments = key_hint_segments(
            y,
            width,
            [
                [*open_hint, *action_hints, done_hint(state)],
                [*open_hint, *footer_action_hints(snapshot, row, is_complete=True)],
                [*open_hint, *action_hints],
                action_hints,
                [*open_hint, *bare_hints],
                bare_hints,
            ],
        )
    move_hint = (f"{glyphs.column_keys}{glyphs.select_keys}", "move")
    help_hint, quit_hint = ("?", "help"), ("q", "quit")
    if state.filter_text:
        count_text, done_text = match_count_parts(snapshot, state)
        filter_hint = (f"/{state.filter_text}", count_text + done_text)
        short_filter_hint = (f"/{state.filter_text}", count_text)
        done_count = sum(row.column == display.COLUMN_DONE for row in snapshot.rows)
        done_hints = [("a", f"{done_count} in Done")] if done_text else []
        filter_sets = [
            [filter_hint, ("esc", "clear"), help_hint, quit_hint],
            [short_filter_hint, *done_hints, ("esc", "clear"), help_hint, quit_hint],
            [short_filter_hint, *done_hints, ("esc", "clear"), ("?", ""), ("q", "")],
            [short_filter_hint, *done_hints, ("esc", "clear")],
            [short_filter_hint, ("esc", "clear"), help_hint, quit_hint],
            [short_filter_hint, ("esc", "clear"), ("?", ""), ("q", "")],
            [short_filter_hint, ("esc", "")],
        ]
        return action_segments + key_hint_segments(y + 1, width, filter_sets)
    navigation_sets = [
        [
            (glyphs.column_keys, "column"),
            (glyphs.select_keys, "select"),
            ("/", "filter"),
            done_hint(state),
            help_hint,
            quit_hint,
        ],
        [move_hint, ("/", "filter"), done_hint(state), help_hint, quit_hint],
        [(move_hint[0], ""), ("/", "filter"), ("n", "next"), help_hint, quit_hint],
        [move_hint, ("/", "filter"), help_hint, quit_hint],
        [("/", "filter"), done_hint(state), help_hint, quit_hint],
        [move_hint, help_hint, quit_hint],
        [help_hint, quit_hint],
        [("?", ""), ("q", "")],
    ]
    return action_segments + key_hint_segments(y + 1, width, navigation_sets)


def footer_segments(
    y: int,
    width: int,
    state: BoardState,
    snapshot: BoardSnapshot,
    row: display.ListRow | None,
    glyphs: Glyphs,
) -> list[Segment]:
    if state.is_filtering:
        return filter_input_segments(y, width, state, snapshot, glyphs)
    status_segments = status_line_segments(y, width, state, row, glyphs, snapshot)
    if status_segments is not None:
        return status_segments
    if not snapshot.unfiltered_rows:
        return key_hint_segments(y, width, [[("?", "help"), ("q", "quit")]])
    action_hints = footer_action_hints(snapshot, row)
    complete_hints = footer_action_hints(snapshot, row, is_complete=True)
    open_hint = [(glyphs.enter_key, "open")] if row is not None else []
    count_text, done_text = match_count_parts(snapshot, state)
    toggle_hint = done_hint(state)
    filter_hint = (
        [(f"/{state.filter_text}", count_text + done_text), ("esc", "clear")]
        if state.filter_text
        else []
    )
    toggle_hints = [toggle_hint]
    if state.filter_text and done_text:
        filter_hint.append(toggle_hint)
        toggle_hints = []
    filter_key_hint = [] if state.filter_text else [("/", "filter")]
    column_hint, select_hint = (glyphs.column_keys, "column"), (glyphs.select_keys, "select")
    move_hint = (f"{glyphs.column_keys}{glyphs.select_keys}", "move")
    help_hint, quit_hint = ("?", "help"), ("q", "quit")
    next_hint = ("n", "next")
    key_sets = [
        [
            *filter_hint,
            column_hint,
            select_hint,
            *open_hint,
            next_hint,
            *complete_hints,
            *toggle_hints,
            *filter_key_hint,
            help_hint,
            quit_hint,
        ],
        [
            *filter_hint,
            column_hint,
            select_hint,
            *open_hint,
            next_hint,
            *action_hints,
            *toggle_hints,
            *filter_key_hint,
            help_hint,
            quit_hint,
        ],
        [
            *filter_hint,
            move_hint,
            *open_hint,
            next_hint,
            *action_hints,
            *toggle_hints,
            *filter_key_hint,
            help_hint,
            quit_hint,
        ],
        [
            *filter_hint,
            move_hint,
            *open_hint,
            next_hint,
            *action_hints,
            *filter_key_hint,
            help_hint,
            quit_hint,
        ],
        [
            *filter_hint,
            move_hint,
            *open_hint,
            *action_hints,
            *toggle_hints,
            *filter_key_hint,
            help_hint,
            quit_hint,
        ],
        [*filter_hint, move_hint, *open_hint, next_hint, *action_hints, help_hint, quit_hint],
        [
            *filter_hint,
            (move_hint[0], ""),
            *open_hint,
            next_hint,
            *action_hints,
            help_hint,
            quit_hint,
        ],
        [
            *filter_hint,
            move_hint,
            *open_hint,
            *action_hints,
            *filter_key_hint,
            help_hint,
            quit_hint,
        ],
        [*filter_hint, *open_hint, *action_hints, *filter_key_hint, help_hint, quit_hint],
        [*filter_hint, *open_hint, *action_hints, help_hint, quit_hint],
        [*filter_hint[:2], *open_hint, *action_hints, help_hint, quit_hint],
        [*filter_hint[:2], *action_hints, help_hint, quit_hint],
        [*filter_hint, *open_hint, *[(key, "") for key, _ in action_hints], help_hint, quit_hint],
        [*filter_hint, *action_hints, help_hint, quit_hint],
        [*filter_hint, *[(key, "") for key, _ in action_hints], help_hint, quit_hint],
        [*filter_hint, help_hint, quit_hint],
        [help_hint, quit_hint],
        [("?", ""), ("q", "")],
    ]
    return key_hint_segments(y, width, key_sets)


def empty_board_segments(
    layout: BoardLayout, width: int, state: BoardState, snapshot: BoardSnapshot
) -> list[Segment]:
    middle_y = layout.body_top + max(0, layout.body_height // 2 - 1)
    lines = EMPTY_BOARD_LINES
    done_count = sum(row.column == display.COLUMN_DONE for row in snapshot.rows)
    if state.filter_text:
        lines = (f"No tasks match {state.filter_text!r}", "esc clears the filter")
    if state.filter_text and done_count:
        lines = (lines[0], f"{done_count} in Done, a shows it", lines[1])
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
    else:
        segments += header_row_segments(width, layout, columns, state, glyphs)
    body_bottom = layout.body_top + layout.body_height
    changed_task_ids = frozenset(
        task_id for task_id, until in state.changed_until.items() if until > time.monotonic()
    )
    context = FrameContext(snapshot, glyphs, now, changed_task_ids, frozenset(state.gate_checks))
    for offset in range(layout.fit_count):
        column_index = state.first_column_index + offset
        segments += [
            segment
            for segment in column_segments(
                layout, column_index, columns[column_index], state, context
            )
            if segment.y < body_bottom
        ]
    has_visible_rows = any(column_rows for _, column_rows in columns)
    if not has_visible_rows:
        segments += empty_board_segments(layout, width, state, snapshot)
    row = selected_row(snapshot, state)
    if layout.detail_line_count and has_visible_rows:
        segments += detail_segments(
            layout.footer_y,
            width,
            layout.detail_line_count,
            row,
            context,
        )
    if layout.fit_count == 1:
        return segments + narrow_footer_segments(
            layout.footer_y, width, state, snapshot, row, glyphs
        )
    return segments + footer_segments(layout.footer_y, width, state, snapshot, row, glyphs)


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


def waiting_reason_text(row: display.ListRow, snapshot: BoardSnapshot) -> str:
    if row.status not in {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}:
        return ""
    reason = row_reason(row, snapshot)
    retry_count = snapshot.tasks[row.task_id].get("setup_busy_retry_count")
    return f"{reason} (checked {retry_count} times)" if reason and retry_count else reason


def task_fields(
    row: display.ListRow, snapshot: BoardSnapshot, glyphs: Glyphs, now: datetime
) -> list[tuple[str, str]]:
    task = snapshot.tasks[row.task_id]
    due_at = task.get("due_at") if row.status == ledger.STATUS_SCHEDULED else None
    fields: list[tuple[str, str]] = [
        ("Directory", display.display_directory(row.directory)),
        ("Worktree", display.display_directory(task.get("worktree", ""))),
        ("Backend", task["backend"]),
        ("Session", task.get("session_id", "")),
        ("Due", moment_text(due_at, now)),
        ("Started", moment_text(task.get("started_at"), now)),
        (
            "Finished" if row.status == ledger.STATUS_FINISHED else "Stopped",
            moment_text(task.get("finished_at"), now),
        ),
        ("Gate", task.get("gate", "")),
        ("Setup", task.get("setup", "")),
        ("Reason", waiting_reason_text(row, snapshot)),
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
    return [(label, value) for label, value in fields if value]


class PathWrapper(textwrap.TextWrapper):
    @override
    def _split(self, text: str) -> list[str]:
        return [chunk for chunk in re.split(r"(\s+)|(?<=/)", text) if chunk]


def field_lines(fields: list[tuple[str, str]], width: int) -> list[list[Part]]:
    if not fields:
        return []
    label_width = max(len(label) for label, _ in fields) + 2
    lines: list[list[Part]] = []
    for label, value in fields:
        value_lines = PathWrapper(max(1, width - label_width)).wrap(value) or [""]
        lines.append(
            [(label.ljust(label_width), STYLE_DIM, None), (value_lines[0], STYLE_PLAIN, None)]
        )
        continuation_lines: list[list[Part]] = [
            [(" " * label_width + line, STYLE_PLAIN, None)] for line in value_lines[1:]
        ]
        lines += continuation_lines
    return lines


def task_view_lines(
    row: display.ListRow,
    width: int,
    snapshot: BoardSnapshot,
    glyphs: Glyphs,
    now: datetime,
) -> list[list[Part]]:
    task = snapshot.tasks[row.task_id]
    lines: list[list[Part]] = field_lines(task_fields(row, snapshot, glyphs, now), width)
    sections = [
        (
            "Stop reason",
            task.get("gate_stop_reason", "") or task.get("setup_failure_reason", ""),
            STYLE_PLAIN,
            "",
        ),
        ("Prompt", task.get("prompt", ""), STYLE_PLAIN, ""),
        (
            "Activity",
            "\n".join(activity_text(line, glyphs) for line in task_activity(row, snapshot))
            or (NO_ACTIVITY_TEXT if row.status == ledger.STATUS_RUNNING else ""),
            STYLE_DIM,
            "  ",
        ),
        ("Result", task.get("result", ""), STYLE_PLAIN, ""),
    ]
    for title, text, style, hanging_indent in sections:
        if not text.strip():
            continue
        title_line: list[Part] = [
            (f"{glyphs.horizontal} ", STYLE_PLAIN, FAINT_COLOR),
            (title, STYLE_BOLD, None),
            (f" {glyphs.horizontal * (width - len(title) - 3)}", STYLE_PLAIN, FAINT_COLOR),
        ]
        lines += [[], title_line, *wrapped_lines(text, width, style, hanging_indent)]
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
    header_lines: list[list[Part]] = [
        *heading_lines(row, width - 1, snapshot, glyphs, now),
        [(glyphs.horizontal * (width - 1), STYLE_PLAIN, FAINT_COLOR)],
    ]
    lines = task_view_lines(row, width - 1, snapshot, glyphs, now)
    body_height = max(1, height - 1 - len(header_lines))
    if state.is_following:
        state.task_scroll = len(lines)
    state.task_scroll = max(0, min(state.task_scroll, len(lines) - body_height))
    shown_lines = [*header_lines, *lines[state.task_scroll : state.task_scroll + body_height]]
    segments: list[Segment] = []
    for offset, parts in enumerate(shown_lines):
        segments += part_segments(offset, 0, parts)
    status_segments = status_line_segments(height - 1, width, state, row, glyphs, snapshot)
    if status_segments is not None:
        return segments + status_segments
    action_hints = [
        hint
        for hint in footer_action_hints(snapshot, row, is_complete=True)
        if hint[0] not in {ACTION_KEYS[ACTION_WATCH], ACTION_KEYS[ACTION_CLOSE_ALL]}
    ]
    is_scrollable = len(lines) > body_height
    position = f"{min(len(lines), state.task_scroll + body_height)}/{len(lines)}"
    position_hints = [(position, "")] if is_scrollable else []
    scroll_hints = [(glyphs.select_keys, "scroll")] if is_scrollable else []
    bare_scroll_hints = [(glyphs.select_keys, "")] if is_scrollable else []
    follow_hint = (
        [("w", "following" if state.is_following else "follow")]
        if row.status == ledger.STATUS_RUNNING
        else []
    )
    column_row_count = len(
        rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)[state.focus_index][1]
    )
    task_number = min(state.selected_indices[state.focus_index], column_row_count - 1) + 1
    step_hint = [(glyphs.column_keys, f"task {task_number} of {column_row_count}")]
    short_step_hint = [(glyphs.column_keys, f"{task_number}/{column_row_count}")]
    core_action_hints = [hint for hint in action_hints if hint[0] != ACTION_KEYS[ACTION_COPY]]
    key_sets = [
        [*scroll_hints, *step_hint, *follow_hint, *action_hints, ("esc", "back"), *position_hints],
        [*scroll_hints, *step_hint, *follow_hint, *action_hints, ("esc", "back")],
        [*bare_scroll_hints, *short_step_hint, *follow_hint, *action_hints, ("esc", "back")],
        [*bare_scroll_hints, *short_step_hint, *follow_hint, *core_action_hints, ("esc", "back")],
        [*short_step_hint, *follow_hint, *action_hints, ("esc", "back")],
        [*short_step_hint, *follow_hint, *core_action_hints, ("esc", "back")],
        [*bare_scroll_hints, *short_step_hint, *follow_hint, *core_action_hints, ("esc", "")],
        [*follow_hint, *core_action_hints, ("esc", "back")],
        [*short_step_hint, *follow_hint, *core_action_hints, ("esc", "")],
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
                ("Space", "page down"),
                ("1-4", "jump to column"),
                ("n", "next task needing you"),
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
            "Task",
            [
                (glyphs.enter_key, "open details"),
                ("p", "pick up its session"),
                ("w", "follow its live log"),
                ("o", "open its pull request"),
                ("r", "retry, or recheck gate"),
                ("c", "close (move to Done)"),
                ("C", "close all finished"),
                ("x", "cancel (if queued)"),
                ("y", "copy its id"),
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
                (glyphs.new_mark, "new, not picked up yet"),
                ("1/2", "step in its chain"),
                ("stuck", "overdue, still waiting"),
                ("bold name", "needs your attention"),
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
    top_text = f"{corners[0]}{line} Keys "
    bottom_text = f"{corners[2]}{line}{more_below}"
    close_hints = [HELP_CLOSE_HINT]
    if state.help_scroll_limit:
        scroll_hint = f" {glyphs.up}{glyphs.down} scroll"
        close_hints = [
            f"{scroll_hint}  spc page  esc close ",
            f" {glyphs.up}{glyphs.down} spc scroll  esc close ",
            f"{scroll_hint}  esc close ",
            f"{scroll_hint} ",
            "",
        ]
    close_hint = next(
        (hint for hint in close_hints if len(bottom_text) + len(hint) + 2 <= box_width), ""
    )
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
    mark_colors = {
        **{mark: display.STATUS_COLORS[status] for status, mark in glyphs.status_marks.items()},
        glyphs.spinner[0]: display.STATUS_COLORS[ledger.STATUS_RUNNING],
    }
    for column_index, rows in enumerate(columns):
        x = left + 2 + column_index * (column_width + HELP_SECTION_GAP)
        shown_rows = rows[state.help_scroll : state.help_scroll + visible_count]
        for offset, (key, label, is_title) in enumerate(shown_rows):
            if is_title:
                segments.append(
                    Segment(first_row_y + offset, x, key, STYLE_BOLD, BOARD_COLUMNS[1].color)
                )
                continue
            label_width = left + box_width - 2 - x - key_width
            segments += [
                Segment(first_row_y + offset, x, key, STYLE_BOLD, mark_colors.get(key)),
                Segment(
                    first_row_y + offset,
                    x + key_width,
                    fitted(label, label_width, glyphs),
                    STYLE_PLAIN,
                ),
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
    now = state.stale_snapshot_time or now or datetime.now().astimezone()
    if state.view == VIEW_TASK:
        segments = task_view_segments(snapshot, width, height, state, glyphs, now)
    else:
        segments = board_view_segments(snapshot, width, height, state, glyphs, now)
    if state.is_showing_help:
        segments = help_view_segments(width, height, glyphs, state)
    return clipped(segments, width, height)


def move_selection(state: BoardState, column_row_count: int, target_index: int) -> None:
    state.selected_indices[state.focus_index] = max(0, min(target_index, column_row_count - 1))
    state.selected_task_ids[state.focus_index] = None


def moved_focus_index(focus_index: int, key: int, column_count: int) -> int:
    if key in TAB_KEYS:
        return (focus_index + (1 if key == ord("\t") else -1)) % column_count
    step = 1 if key in RIGHT_KEYS else -1
    return max(0, min(focus_index + step, column_count - 1))


def toggle_done(state: BoardState) -> None:
    state.is_showing_all = not state.is_showing_all
    done_index = len(BOARD_COLUMNS) - 1
    if state.is_showing_all:
        state.focus_before_done, state.focus_index = state.focus_index, done_index
    elif state.focus_index == done_index:
        state.focus_index = state.focus_before_done


def apply_board_key(state: BoardState, key: int, column_row_count: int, page_size: int) -> None:
    column_count = len(visible_columns(is_showing_all=state.is_showing_all))
    selected_index = state.selected_indices[state.focus_index]
    if key in LEFT_KEYS | RIGHT_KEYS | TAB_KEYS:
        state.focus_index = moved_focus_index(state.focus_index, key, column_count)
    elif ord("1") <= key < ord("1") + len(BOARD_COLUMNS):
        state.is_showing_all = state.is_showing_all or key - ord("1") >= column_count
        state.focus_index = key - ord("1")
    elif key in DOWN_KEYS | UP_KEYS:
        move_selection(state, column_row_count, selected_index + (1 if key in DOWN_KEYS else -1))
    elif key in FIRST_KEYS | LAST_KEYS:
        move_selection(state, column_row_count, 0 if key in FIRST_KEYS else column_row_count - 1)
    elif key in PAGE_DOWN_KEYS | PAGE_UP_KEYS:
        step = page_size if key in PAGE_DOWN_KEYS else -page_size
        move_selection(state, column_row_count, selected_index + step)
        scroll_offset = state.scroll_offsets[state.focus_index]
        state.scroll_offsets[state.focus_index] = max(0, scroll_offset + step)
    elif key == ord("a"):
        toggle_done(state)
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


def apply_task_key(state: BoardState, key: int, page_size: int, column_row_count: int) -> None:
    state.is_following = state.is_following and key in DOWN_KEYS | LAST_KEYS
    if key in BACK_KEYS:
        state.view = VIEW_BOARD
    elif key in LEFT_KEYS | RIGHT_KEYS:
        step = 1 if key in RIGHT_KEYS else -1
        move_selection(state, column_row_count, state.selected_indices[state.focus_index] + step)
        state.task_scroll = 0
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
    if key in FIRST_KEYS | LAST_KEYS:
        state.help_scroll = state.help_scroll_limit if key in LAST_KEYS else 0
        return
    if key in scroll_steps:
        state.help_scroll = max(
            0, min(state.help_scroll + scroll_steps[key], state.help_scroll_limit)
        )
        return
    state.is_showing_help = False
    state.help_scroll = 0


def select_next_attention(state: BoardState, snapshot: BoardSnapshot) -> None:
    now = datetime.now().astimezone()
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    positions = [
        (column_index, row_index)
        for column_index, (_, column_rows) in enumerate(columns)
        for row_index, row in enumerate(column_rows)
        if row.status in STOPPED_STATUSES
        or is_new_for_review(snapshot, row)
        or is_stuck(row, snapshot.tasks[row.task_id], now)
    ]
    if not positions:
        flash(state, NOTHING_NEEDS_YOU_TEXT)
        return
    current = (state.focus_index, state.selected_indices[state.focus_index])
    column_index, row_index = next(
        (position for position in positions if position > current), positions[0]
    )
    state.focus_index = column_index
    state.selected_indices[column_index] = row_index
    state.selected_task_ids[column_index] = None


def apply_key(
    state: BoardState, key: int, snapshot: BoardSnapshot | None = None, height: int = 0
) -> None:
    rows = snapshot.rows if snapshot is not None else []
    columns = rows_by_column(rows, is_showing_all=state.is_showing_all)
    column_row_count = len(columns[min(state.focus_index, len(columns) - 1)][1])
    page_size = max(1, (height - 12) // DENSE_CARD.pitch)
    if state.is_showing_help:
        apply_help_key(state, key, max(1, height - 6))
    elif state.is_filtering:
        apply_filter_key(state, key)
    elif state.view == VIEW_TASK:
        apply_task_key(state, key, max(1, height - 2), column_row_count or 1)
    elif key == NEXT_ATTENTION_KEY and snapshot is not None:
        select_next_attention(state, snapshot)
    else:
        apply_board_key(state, key, column_row_count or sys.maxsize, page_size)


def segment_attribute(segment: Segment, *, is_color: bool) -> int:
    attribute = STYLE_ATTRIBUTES[segment.style]
    if not is_color:
        return attribute | (curses.A_REVERSE if segment.background is not None else 0)
    color = segment.color
    if segment.style == STYLE_DIM:
        # Many terminfo entries, including macOS xterm-256color, have no dim capability.
        muted_color = MUTED_COLOR if segment.background is None else SELECTED_MUTED_COLOR
        attribute, color = curses.A_NORMAL, color or muted_color
    if color is None and segment.background is None:
        return attribute
    return attribute | curses.color_pair(COLOR_PAIRS[color, segment.background])


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
    for (color, background), pair in COLOR_PAIRS.items():
        curses.init_pair(
            pair, -1 if color is None else color, -1 if background is None else background
        )
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
        state.is_following = state.view != VIEW_TASK or not state.is_following
        state.view = VIEW_TASK
        return
    if action == ACTION_PICKUP:
        run_attached(screen, [*GOLEM_COMMAND, action, row.task_id])
        return
    if action == ACTION_RERUN_GATE:
        state.gate_checks[row.task_id] = subprocess.Popen(
            [*GOLEM_COMMAND, action, row.task_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        flash(state, f"Rechecking the gate for {row.task_id}")
        return
    if action == ACTION_RETRY:
        returncode, output = run_captured([*GOLEM_COMMAND, action, row.task_id])
        report = output.removeprefix(GOLEM_PREFIX)
        flash(state, report[:1].upper() + report[1:])
        return
    if action == ACTION_CLOSE_ALL:
        task_ids = closable_task_ids(snapshot)
        errors: list[str] = []
        for task_id in task_ids:
            try:
                review.close(task_id)
            except SystemExit as error:
                errors.append(str(error))
        closed_count = len(task_ids) - len(errors)
        flash(state, errors[0] if errors else f"Closed {closed_count} tasks")
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


def report_gate_checks(state: BoardState) -> None:
    for task_id, process in list(state.gate_checks.items()):
        if process.poll() is None:
            continue
        del state.gate_checks[task_id]
        _, error_output = process.communicate()
        reason = display.first_line(error_output.strip()).removeprefix(GOLEM_PREFIX)
        if process.returncode == 0:
            flash(state, f"Gate passed for {task_id}; follow-ups started")
        else:
            flash(state, f"Gate still failing for {task_id}: {reason}")


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
        flash(state, NO_CHANGE_TEXT)
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
    current_positions = row_positions(current)
    until = time.monotonic() + CHANGE_HIGHLIGHT_SECONDS
    for task_id, position in current_positions.items():
        if previous_positions.get(task_id) != position:
            state.changed_until[task_id] = until
    selected_task_id = state.selected_task_ids[state.focus_index]
    if selected_task_id not in previous_positions or selected_task_id not in current_positions:
        return
    previous_column, _ = previous_positions[selected_task_id]
    column, status = current_positions[selected_task_id]
    titles = [
        board_column.title for board_column in visible_columns(is_showing_all=state.is_showing_all)
    ]
    if column == previous_column or column not in titles:
        return
    state.focus_index = titles.index(column)
    state.selected_task_ids[state.focus_index] = selected_task_id
    flash(state, f"{selected_task_id} {status}, now in {column}")


def board_loop(screen: curses.window, *, is_color: bool, glyphs: Glyphs) -> int:
    task_ledger_path = ledger.ledger_path()
    state = BoardState()
    shown_source_key: tuple[LedgerStat, tuple[LedgerStat, ...], date] | None = None
    snapshot = BoardSnapshot([], {}, {}, [])
    snapshot_time = datetime.now().astimezone()
    shown_size = screen.getmaxyx()
    while True:
        now = datetime.now().astimezone()
        source_key = (file_stat(task_ledger_path), progress_stats(snapshot), now.date())
        if source_key != shown_source_key:
            is_first_load = not snapshot.unfiltered_rows
            previous_snapshot = snapshot
            try:
                snapshot = board_snapshot(ledger.load_tasks(), now)
            except (ValueError, KeyError, TypeError) as error:
                flash(state, f"{LEDGER_ERROR_TEXT}{error}")
                state.stale_snapshot_time = snapshot_time
                shown_source_key = source_key
                continue
            state.stale_snapshot_time = None
            snapshot_time = now
            if is_first_load:
                state.focus_index = attention_column_index(snapshot.rows)
            else:
                mark_changed_tasks(state, previous_snapshot, snapshot)
            shown_source_key = (source_key[0], progress_stats(snapshot), now.date())
        report_gate_checks(state)
        if screen.getmaxyx() != shown_size:
            shown_size = screen.getmaxyx()
            screen.clear()
        shown_snapshot = filtered_snapshot(snapshot, state)
        draw_board(screen, shown_snapshot, state, is_color=is_color, glyphs=glyphs)
        pressed_key = read_key(screen)
        if pressed_key in {-1, curses.KEY_RESIZE}:
            continue
        state.flash_until = 0.0
        if is_quit_key(state, pressed_key):
            return 0
        if handle_action_key(screen, state, shown_snapshot, pressed_key):
            shown_source_key = None
            continue
        apply_key(state, pressed_key, shown_snapshot, shown_size[0])


def read_key(screen: curses.window) -> int:
    key = screen.getch()
    if key != ESCAPE_KEY:
        return key
    screen.timeout(0)
    sequence = ""
    while (char := screen.getch()) != -1:
        if not sequence and chr(char) not in ESCAPE_SEQUENCE_STARTS:
            curses.ungetch(char)
            break
        sequence += chr(char)
        if len(sequence) > 1 and char in ESCAPE_SEQUENCE_FINAL_KEYS:
            break
    screen.timeout(TICK_MILLISECONDS)
    if not sequence:
        return ESCAPE_KEY
    return ESCAPE_SEQUENCE_KEYS.get(sequence, -1)


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
