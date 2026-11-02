import contextlib
import curses
import os
import signal
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, NamedTuple

from golem import display, ledger

if TYPE_CHECKING:
    from pathlib import Path

MIN_COLUMN_WIDTH = 32
COLUMN_GAP = 2
HEADER_AND_FOOTER_LINE_COUNT = 2
STEP_SEPARATOR = "  "
KEY_TIMEOUT_MILLISECONDS = 250
INTERRUPTED_EXIT_STATUS = 130
MIN_COLOR_COUNT = 256
NOT_A_TERMINAL_MESSAGE = "golem: board needs a terminal; use golem list"
STYLE_PLAIN = "plain"
STYLE_BOLD = "bold"
STYLE_DIM = "dim"
STYLE_REVERSE = "reverse"
STYLE_ATTRIBUTES = {
    STYLE_PLAIN: curses.A_NORMAL,
    STYLE_BOLD: curses.A_BOLD,
    STYLE_DIM: curses.A_DIM,
    STYLE_REVERSE: curses.A_REVERSE,
}
STATUS_COLOR_PAIRS = {status: pair for pair, status in enumerate(display.STATUS_COLORS, start=1)}
LEFT_KEYS = {ord("h"), curses.KEY_LEFT}
RIGHT_KEYS = {ord("l"), curses.KEY_RIGHT}
DOWN_KEYS = {ord("j"), curses.KEY_DOWN}
UP_KEYS = {ord("k"), curses.KEY_UP}


class BoardColumn(NamedTuple):
    title: str
    statuses: tuple[str, ...]


class Segment(NamedTuple):
    y: int
    x: int
    text: str
    style: str


type Line = list[tuple[str, str]]
type LedgerStat = tuple[int, int, int] | None

BOARD_COLUMNS = (
    BoardColumn("Queued", (ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING)),
    BoardColumn("Running", (ledger.STATUS_RUNNING,)),
    BoardColumn("Review", (ledger.STATUS_NEEDS_REVIEW, ledger.STATUS_FAILED)),
    BoardColumn("Done", (ledger.STATUS_DONE,)),
)


@dataclass
class BoardState:
    focus_index: int = 0
    first_column_index: int = 0
    is_showing_all: bool = False
    scroll_offsets: list[int] = field(default_factory=lambda: [0] * len(BOARD_COLUMNS))


def rows_by_column(
    rows: list[display.ListRow], *, is_showing_all: bool
) -> list[tuple[BoardColumn, list[display.ListRow]]]:
    return [
        (
            board_column,
            [
                row
                for row in rows
                if row.status in board_column.statuses and (is_showing_all or not row.is_hidden)
            ],
        )
        for board_column in BOARD_COLUMNS
    ]


def wrapped_lines(text: str, width: int, style: str) -> list[Line]:
    return [[(line, style)] for line in textwrap.wrap(text, width)]


def card_lines(row: display.ListRow, width: int) -> list[Line]:
    title_lines = wrapped_lines(row.task_id, width, STYLE_BOLD)
    step_text = f"{STEP_SEPARATOR}{row.step}"
    if row.step and len(title_lines[-1][0][0]) + len(step_text) <= width:
        title_lines[-1].append((step_text, STYLE_PLAIN))
    elif row.step:
        title_lines += wrapped_lines(row.step, width, STYLE_PLAIN)
    directory = display.display_directory(row.directory)
    if len(directory) > width:
        directory = display.shortened_directory(directory)
    return [
        *title_lines,
        *wrapped_lines(row.status, width, row.status),
        *wrapped_lines(directory, width, STYLE_PLAIN),
        *wrapped_lines(row.detail, width, STYLE_DIM),
    ]


def column_lines(rows: list[display.ListRow], width: int) -> list[Line]:
    lines: list[Line] = []
    for row in rows:
        if lines:
            lines.append([])
        lines += card_lines(row, width)
    return lines


def line_segments(y: int, x: int, line: Line) -> list[Segment]:
    segments: list[Segment] = []
    for text, style in line:
        segments.append(Segment(y, x, text, style))
        x += len(text)
    return segments


def footer_text(state: BoardState) -> str:
    finished_action = "hide" if state.is_showing_all else "show"
    return f"h/l column  j/k scroll  a {finished_action} finished  q quit"


class BoardLayout(NamedTuple):
    fit_count: int
    column_width: int
    content_width: int
    body_height: int


def board_layout(width: int, height: int) -> BoardLayout:
    fit_count = min(len(BOARD_COLUMNS), max(1, width // MIN_COLUMN_WIDTH))
    column_width = width // fit_count
    return BoardLayout(
        fit_count,
        column_width,
        max(1, column_width - COLUMN_GAP),
        height - HEADER_AND_FOOTER_LINE_COUNT,
    )


def fit_state_to_layout(
    rows: list[display.ListRow], width: int, height: int, state: BoardState
) -> None:
    layout = board_layout(width, height)
    state.first_column_index = min(
        max(
            state.focus_index - layout.fit_count + 1,
            min(state.first_column_index, state.focus_index),
        ),
        len(BOARD_COLUMNS) - layout.fit_count,
    )
    columns = rows_by_column(rows, is_showing_all=state.is_showing_all)
    for column_index, (_, column_rows) in enumerate(columns):
        line_count = len(column_lines(column_rows, layout.content_width))
        state.scroll_offsets[column_index] = min(
            max(state.scroll_offsets[column_index], 0), max(0, line_count - layout.body_height)
        )


def board_frame(
    rows: list[display.ListRow], width: int, height: int, state: BoardState
) -> list[Segment]:
    # The bottom-right cell is left empty because curses raises after writing it.
    footer = Segment(height - 1, 0, footer_text(state)[: width - 1], STYLE_DIM)
    if height < HEADER_AND_FOOTER_LINE_COUNT:
        return [footer]
    layout = board_layout(width, height)
    columns = rows_by_column(rows, is_showing_all=state.is_showing_all)
    segments: list[Segment] = []
    for offset in range(layout.fit_count):
        column_index = state.first_column_index + offset
        board_column, column_rows = columns[column_index]
        x = offset * layout.column_width
        header_style = STYLE_REVERSE if column_index == state.focus_index else STYLE_BOLD
        header = f"{board_column.title} ({len(column_rows)})"
        segments.append(Segment(0, x, header[: layout.content_width], header_style))
        lines = column_lines(column_rows, layout.content_width)
        scroll_offset = state.scroll_offsets[column_index]
        for y, line in enumerate(
            lines[scroll_offset : scroll_offset + layout.body_height], start=1
        ):
            segments += line_segments(y, x, line)
    return [*segments, footer]


def apply_key(state: BoardState, key: int) -> None:
    if key in LEFT_KEYS:
        state.focus_index = max(state.focus_index - 1, 0)
    elif key in RIGHT_KEYS:
        state.focus_index = min(state.focus_index + 1, len(BOARD_COLUMNS) - 1)
    elif key in DOWN_KEYS:
        state.scroll_offsets[state.focus_index] += 1
    elif key in UP_KEYS:
        state.scroll_offsets[state.focus_index] -= 1
    elif key == ord("a"):
        state.is_showing_all = not state.is_showing_all


def segment_attribute(style: str, *, is_color: bool) -> int:
    if style not in STATUS_COLOR_PAIRS:
        return STYLE_ATTRIBUTES[style]
    return curses.color_pair(STATUS_COLOR_PAIRS[style]) if is_color else curses.A_NORMAL


def draw_board(
    screen: curses.window, rows: list[display.ListRow], state: BoardState, *, is_color: bool
) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    fit_state_to_layout(rows, width, height, state)
    for segment in board_frame(rows, width, height, state):
        with contextlib.suppress(curses.error):
            screen.addstr(
                segment.y,
                segment.x,
                segment.text,
                segment_attribute(segment.style, is_color=is_color),
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
    for status, pair in STATUS_COLOR_PAIRS.items():
        curses.init_pair(pair, display.STATUS_COLORS[status], -1)
    return True


def ledger_stat(task_ledger_path: Path) -> LedgerStat:
    try:
        stat_result = task_ledger_path.stat()
    except FileNotFoundError:
        return None
    return (stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size)


def board_loop(screen: curses.window, *, is_color: bool) -> int:
    task_ledger_path = ledger.ledger_path()
    state = BoardState()
    shown_redraw_key: tuple[LedgerStat, date] | None = None
    rows: list[display.ListRow] = []
    is_redraw_needed = True
    while True:
        today = datetime.now().astimezone().date()
        redraw_key = (ledger_stat(task_ledger_path), today)
        if redraw_key != shown_redraw_key:
            shown_redraw_key = redraw_key
            rows = display.list_rows(ledger.load_tasks(), today)
            is_redraw_needed = True
        if is_redraw_needed:
            draw_board(screen, rows, state, is_color=is_color)
        key = screen.getch()
        if key == ord("q"):
            return 0
        apply_key(state, key)
        is_redraw_needed = key != -1


def is_terminal() -> bool:
    is_tty = sys.stdout.isatty() and sys.stdin.isatty()
    return is_tty and os.environ.get("TERM", "") not in {"", "dumb"}


def run_board() -> int:
    if not is_terminal():
        raise SystemExit(NOT_A_TERMINAL_MESSAGE)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        screen = curses.initscr()
    except curses.error:
        raise SystemExit(NOT_A_TERMINAL_MESSAGE) from None
    try:
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)  # noqa: FBT003 - curses only takes the flag positionally
        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        is_color = start_colors()
        screen.timeout(KEY_TIMEOUT_MILLISECONDS)
        return board_loop(screen, is_color=is_color)
    except KeyboardInterrupt:
        return INTERRUPTED_EXIT_STATUS
    finally:
        curses.endwin()
