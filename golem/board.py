import contextlib
import curses
import locale
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from golem import display, ledger

MIN_COLUMN_WIDTH = 20
COLUMN_GAP = 2
CARD_BORDER_WIDTH = 4
CARD_HEIGHT = 5
CARD_PITCH = CARD_HEIGHT + 1
TITLE_LINE_COUNT = 2
TAB_LINE_COUNT = 2
DETAIL_PANE_LINE_COUNT = 6
KEY_TIMEOUT_MILLISECONDS = 250
INTERRUPTED_EXIT_STATUS = 130
MIN_COLOR_COUNT = 256
BOARD_TITLE = "golem board"
NOT_A_TERMINAL_MESSAGE = "golem: board needs a terminal; use golem list"
STYLE_PLAIN = "plain"
STYLE_BOLD = "bold"
STYLE_DIM = "dim"
STYLE_BOLD_UNDERLINE = "bold underline"
STYLE_ATTRIBUTES = {
    STYLE_PLAIN: curses.A_NORMAL,
    STYLE_BOLD: curses.A_BOLD,
    STYLE_DIM: curses.A_DIM,
    STYLE_BOLD_UNDERLINE: curses.A_BOLD | curses.A_UNDERLINE,
}
LEFT_KEYS = {ord("h"), curses.KEY_LEFT}
RIGHT_KEYS = {ord("l"), curses.KEY_RIGHT}
DOWN_KEYS = {ord("j"), curses.KEY_DOWN}
UP_KEYS = {ord("k"), curses.KEY_UP}


class BoardColumn(NamedTuple):
    title: str
    statuses: tuple[str, ...]
    color: int


class Segment(NamedTuple):
    y: int
    x: int
    text: str
    style: str = STYLE_PLAIN
    color: int | None = None


class Glyphs(NamedTuple):
    corners: str
    heavy_corners: str
    horizontal: str
    heavy_horizontal: str
    vertical: str
    heavy_vertical: str
    focus_marker: str
    ellipsis: str
    separator: str
    up: str
    down: str
    column_keys: str
    select_keys: str


class BoardSnapshot(NamedTuple):
    rows: list[display.ListRow]
    prompts: dict[str, str]
    updated_at: str


class BoardLayout(NamedTuple):
    fit_count: int
    column_width: int
    header_y: int
    body_top: int
    body_height: int
    is_detail_shown: bool

    @property
    def visible_card_count(self) -> int:
        return max(1, self.body_height // CARD_PITCH)


type LedgerStat = tuple[int, int, int] | None

BOARD_COLUMNS = (
    BoardColumn("Queued", (ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING), 208),
    BoardColumn("Running", (ledger.STATUS_RUNNING,), 39),
    BoardColumn("Review", (ledger.STATUS_NEEDS_REVIEW, ledger.STATUS_FAILED), 170),
    BoardColumn("Done", (ledger.STATUS_DONE,), 34),
)
COLOR_PAIRS = {
    color: pair
    for pair, color in enumerate(
        sorted({*display.STATUS_COLORS.values(), *(column.color for column in BOARD_COLUMNS)}),
        start=1,
    )
}
UNICODE_GLYPHS = Glyphs("╭╮╰╯", "┏┓┗┛", "─", "━", "│", "┃", "▌", "…", "·", "↑", "↓", "←→", "↑↓")
ASCII_GLYPHS = Glyphs("++++", "####", "-", "=", "|", "#", ">", "~", "|", "^", "v", "h/l", "j/k")


@dataclass
class BoardState:
    focus_index: int = 0
    first_column_index: int = 0
    is_showing_all: bool = False
    selected_indices: list[int] = field(default_factory=lambda: [0] * len(BOARD_COLUMNS))
    scroll_offsets: list[int] = field(default_factory=lambda: [0] * len(BOARD_COLUMNS))


def board_snapshot(tasks: ledger.Tasks, now: datetime) -> BoardSnapshot:
    return BoardSnapshot(
        display.list_rows(tasks, now.date()),
        {task_id: task.get("prompt", "") for task_id, task in tasks.items()},
        display.clock_time(now),
    )


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


def board_layout(width: int, height: int) -> BoardLayout:
    fit_count = min(
        len(BOARD_COLUMNS), max(1, (width + COLUMN_GAP) // (MIN_COLUMN_WIDTH + COLUMN_GAP))
    )
    column_width = (width - (fit_count - 1) * COLUMN_GAP) // fit_count
    header_y = TITLE_LINE_COUNT + (TAB_LINE_COUNT if fit_count < len(BOARD_COLUMNS) else 0)
    body_top = header_y + 3
    detail_body_height = height - DETAIL_PANE_LINE_COUNT - 1 - body_top
    is_detail_shown = detail_body_height >= CARD_HEIGHT
    body_height = detail_body_height if is_detail_shown else height - 1 - body_top
    return BoardLayout(fit_count, column_width, header_y, body_top, body_height, is_detail_shown)


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
        selected_index = min(
            max(state.selected_indices[column_index], 0), max(0, len(column_rows) - 1)
        )
        scroll_offset = min(state.scroll_offsets[column_index], selected_index)
        scroll_offset = max(scroll_offset, selected_index - layout.visible_card_count + 1)
        state.selected_indices[column_index] = selected_index
        state.scroll_offsets[column_index] = scroll_offset


def fitted(text: str, width: int, glyphs: Glyphs) -> str:
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + glyphs.ellipsis


def card_segments(
    y: int, x: int, width: int, row: display.ListRow, glyphs: Glyphs, *, is_selected: bool
) -> list[Segment]:
    color = display.STATUS_COLORS[row.status]
    corners = glyphs.heavy_corners if is_selected else glyphs.corners
    horizontal = glyphs.heavy_horizontal if is_selected else glyphs.horizontal
    vertical = glyphs.heavy_vertical if is_selected else glyphs.vertical
    content_width = width - CARD_BORDER_WIDTH
    id_width = content_width - len(row.step) - 1 if row.step else content_width
    task_id = fitted(row.task_id, id_width, glyphs)
    content_lines = [
        [Segment(0, 0, task_id, STYLE_BOLD), Segment(0, id_width + 1, row.step, STYLE_DIM)],
        [Segment(0, 0, fitted(row.status, content_width, glyphs), color=color)],
        [
            Segment(
                0,
                0,
                fitted(row.detail or Path(row.directory).name, content_width, glyphs),
                STYLE_DIM,
            )
        ],
    ]
    segments = [Segment(y, x, corners[0] + horizontal * (width - 2) + corners[1], color=color)]
    for line_offset, line in enumerate(content_lines, start=1):
        segments.append(Segment(y + line_offset, x, vertical, color=color))
        segments += [
            segment._replace(y=y + line_offset, x=x + 2 + segment.x)
            for segment in line
            if segment.text
        ]
        segments.append(Segment(y + line_offset, x + width - 1, vertical, color=color))
    bottom_y = y + len(content_lines) + 1
    segments.append(
        Segment(bottom_y, x, corners[2] + horizontal * (width - 2) + corners[3], color=color)
    )
    return segments


def column_segments(
    layout: BoardLayout,
    column_index: int,
    column: tuple[BoardColumn, list[display.ListRow]],
    state: BoardState,
    glyphs: Glyphs,
) -> list[Segment]:
    board_column, column_rows = column
    x = (column_index - state.first_column_index) * (layout.column_width + COLUMN_GAP)
    is_focused = column_index == state.focus_index
    marker = f"{glyphs.focus_marker} " if is_focused else "  "
    title_style = STYLE_BOLD if is_focused else STYLE_PLAIN
    rule = glyphs.heavy_horizontal if is_focused else glyphs.horizontal
    segments = [
        Segment(layout.header_y, x, marker, color=board_column.color),
        Segment(layout.header_y, x + 2, board_column.title, title_style, board_column.color),
        Segment(layout.header_y, x + 3 + len(board_column.title), str(len(column_rows)), STYLE_DIM),
        Segment(layout.header_y + 1, x, rule * layout.column_width, color=board_column.color),
    ]
    if not column_rows:
        return [*segments, Segment(layout.body_top, x + 2, "No tasks", STYLE_DIM)]
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
            card_y, x, layout.column_width, row, glyphs, is_selected=is_selected
        )
    hidden_below_count = len(column_rows) - scroll_offset - len(shown_rows)
    below_y = layout.body_top + len(shown_rows) * CARD_PITCH - 1
    if hidden_below_count and below_y < layout.body_top + layout.body_height:
        segments.append(
            Segment(below_y, x + 2, f"{glyphs.down} {hidden_below_count} more", STYLE_DIM)
        )
    return segments


def tab_segments(
    y: int,
    width: int,
    columns: list[tuple[BoardColumn, list[display.ListRow]]],
    state: BoardState,
    glyphs: Glyphs,
) -> list[Segment]:
    labels = [f"{board_column.title} {len(column_rows)}" for board_column, column_rows in columns]
    separator = f" {glyphs.separator} "
    if len(separator.join(labels)) >= width:
        labels = [
            label
            if column_index == state.focus_index
            else f"{board_column.title[0]} {len(column_rows)}"
            for column_index, (label, (board_column, column_rows)) in enumerate(
                zip(labels, columns, strict=True)
            )
        ]
    segments: list[Segment] = []
    x = 0
    for column_index, label in enumerate(labels):
        if column_index:
            segments.append(Segment(y, x, separator, STYLE_DIM))
            x += len(separator)
        if column_index == state.focus_index:
            segments.append(
                Segment(y, x, label, STYLE_BOLD_UNDERLINE, columns[column_index][0].color)
            )
        else:
            segments.append(Segment(y, x, label, STYLE_DIM))
        x += len(label)
    return segments


def title_segments(width: int, snapshot: BoardSnapshot, glyphs: Glyphs) -> list[Segment]:
    statuses = [row.status for row in snapshot.rows]
    review_count = statuses.count(ledger.STATUS_NEEDS_REVIEW) + statuses.count(ledger.STATUS_FAILED)
    summary = f"  {statuses.count(ledger.STATUS_RUNNING)} running {glyphs.separator} {review_count} to review"
    updated = f"updated {snapshot.updated_at}"
    segments = [
        Segment(0, 0, BOARD_TITLE, STYLE_BOLD),
        Segment(0, len(BOARD_TITLE), summary, STYLE_DIM),
    ]
    if len(BOARD_TITLE) + len(summary) + 2 + len(updated) < width:
        segments.append(Segment(0, width - 1 - len(updated), updated, STYLE_DIM))
    return segments


def detail_segments(
    y: int, width: int, row: display.ListRow | None, snapshot: BoardSnapshot, glyphs: Glyphs
) -> list[Segment]:
    rule = glyphs.horizontal * (width - 1)
    segments = [Segment(y, 0, rule, STYLE_DIM), Segment(y + 4, 0, rule, STYLE_DIM)]
    if row is None:
        return [*segments, Segment(y + 1, 0, "No task selected", STYLE_DIM)]
    step_text = f"step {row.step} {glyphs.separator} " if row.step else ""
    location_width = width - 1 - len(row.task_id) - len(row.status) - 4
    directory = display.display_directory(row.directory)
    if len(step_text) + len(directory) > location_width:
        directory = display.shortened_directory(directory)
    location_text = f"  {fitted(step_text + directory, location_width, glyphs)}"
    first_line = [
        (row.task_id, STYLE_BOLD, None),
        (f"  {row.status}", STYLE_PLAIN, display.STATUS_COLORS[row.status]),
        (location_text, STYLE_DIM, None),
    ]
    x = 0
    for text, style, color in first_line:
        segments.append(Segment(y + 1, x, text, style, color))
        x += len(text)
    prompt_line = snapshot.prompts.get(row.task_id, "").strip().partition("\n")[0]
    detail_lines = [(row.detail, STYLE_PLAIN), (prompt_line, STYLE_DIM)]
    for line_offset, (text, style) in enumerate(
        [line for line in detail_lines if line[0]], start=2
    ):
        segments.append(Segment(y + line_offset, 0, fitted(text, width - 1, glyphs), style))
    return segments


def footer_segments(y: int, state: BoardState, glyphs: Glyphs) -> list[Segment]:
    finished_action = "hide" if state.is_showing_all else "show"
    keys = [
        (glyphs.column_keys, "column"),
        (glyphs.select_keys, "select"),
        ("a", f"{finished_action} finished"),
        ("q", "quit"),
    ]
    segments: list[Segment] = []
    x = 0
    for key, label in keys:
        segments += [Segment(y, x, key, STYLE_BOLD), Segment(y, x + len(key) + 1, label, STYLE_DIM)]
        x += len(key) + len(label) + 3
    return segments


def clipped(segments: list[Segment], width: int, height: int) -> list[Segment]:
    clipped_segments: list[Segment] = []
    for segment in segments:
        # The bottom-right cell is left empty because curses raises after writing it.
        line_width = width - 1 if segment.y == height - 1 else width
        text = segment.text[: max(0, line_width - segment.x)]
        if 0 <= segment.y < height and segment.x >= 0 and text:
            clipped_segments.append(segment._replace(text=text))
    return clipped_segments


def board_frame(
    snapshot: BoardSnapshot,
    width: int,
    height: int,
    state: BoardState,
    glyphs: Glyphs = UNICODE_GLYPHS,
) -> list[Segment]:
    layout = board_layout(width, height)
    columns = rows_by_column(snapshot.rows, is_showing_all=state.is_showing_all)
    segments = title_segments(width, snapshot, glyphs)
    if layout.fit_count < len(BOARD_COLUMNS):
        segments += tab_segments(TITLE_LINE_COUNT, width, columns, state, glyphs)
    body_bottom = layout.body_top + layout.body_height
    for offset in range(layout.fit_count):
        column_index = state.first_column_index + offset
        segments += [
            segment
            for segment in column_segments(
                layout, column_index, columns[column_index], state, glyphs
            )
            if segment.y < body_bottom
        ]
    if layout.is_detail_shown:
        focused_rows = columns[state.focus_index][1]
        selected_row = (
            focused_rows[state.selected_indices[state.focus_index]] if focused_rows else None
        )
        segments += detail_segments(
            height - DETAIL_PANE_LINE_COUNT, width, selected_row, snapshot, glyphs
        )
    segments += footer_segments(height - 1, state, glyphs)
    return clipped(segments, width, height)


def apply_key(state: BoardState, key: int) -> None:
    if key in LEFT_KEYS:
        state.focus_index = max(state.focus_index - 1, 0)
    elif key in RIGHT_KEYS:
        state.focus_index = min(state.focus_index + 1, len(BOARD_COLUMNS) - 1)
    elif key in DOWN_KEYS:
        state.selected_indices[state.focus_index] += 1
    elif key in UP_KEYS:
        state.selected_indices[state.focus_index] -= 1
    elif key == ord("a"):
        state.is_showing_all = not state.is_showing_all


def segment_attribute(segment: Segment, *, is_color: bool) -> int:
    attribute = STYLE_ATTRIBUTES[segment.style]
    if is_color and segment.color is not None:
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


def terminal_glyphs() -> Glyphs:
    codeset = locale.nl_langinfo(locale.CODESET).lower().replace("-", "")
    return UNICODE_GLYPHS if codeset == "utf8" else ASCII_GLYPHS


def ledger_stat(task_ledger_path: Path) -> LedgerStat:
    try:
        stat_result = task_ledger_path.stat()
    except FileNotFoundError:
        return None
    return (stat_result.st_ino, stat_result.st_mtime_ns, stat_result.st_size)


def board_loop(screen: curses.window, *, is_color: bool, glyphs: Glyphs) -> int:
    task_ledger_path = ledger.ledger_path()
    state = BoardState()
    shown_redraw_key: tuple[LedgerStat, date] | None = None
    snapshot = BoardSnapshot([], {}, "")
    is_redraw_needed = True
    while True:
        now = datetime.now().astimezone()
        redraw_key = (ledger_stat(task_ledger_path), now.date())
        if redraw_key != shown_redraw_key:
            shown_redraw_key = redraw_key
            snapshot = board_snapshot(ledger.load_tasks(), now)
            is_redraw_needed = True
        if is_redraw_needed:
            draw_board(screen, snapshot, state, is_color=is_color, glyphs=glyphs)
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
        return board_loop(screen, is_color=is_color, glyphs=terminal_glyphs())
    except KeyboardInterrupt:
        return INTERRUPTED_EXIT_STATUS
    finally:
        curses.endwin()
