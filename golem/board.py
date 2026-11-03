import contextlib
import curses
import locale
import os
import signal
import sys
import textwrap
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
DETAIL_PANE_LINE_COUNT = 5
KEY_TIMEOUT_MILLISECONDS = 250
INTERRUPTED_EXIT_STATUS = 130
MIN_COLOR_COUNT = 256
BOARD_TITLE = "golem board"
NOT_A_TERMINAL_MESSAGE = "golem: board needs a terminal; use golem list"
STYLE_PLAIN = "plain"
STYLE_BOLD = "bold"
STYLE_DIM = "dim"
STYLE_ATTRIBUTES = {
    STYLE_PLAIN: curses.A_NORMAL,
    STYLE_BOLD: curses.A_BOLD,
    STYLE_DIM: curses.A_DIM,
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
    left: str
    right: str
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

REVIEW_STATUSES = (ledger.STATUS_NEEDS_REVIEW, ledger.STATUS_FAILED)
BOARD_COLUMNS = (
    BoardColumn("Queued", (ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING), 208),
    BoardColumn("Running", (ledger.STATUS_RUNNING,), 39),
    BoardColumn("Review", REVIEW_STATUSES, 170),
    BoardColumn("Done", (ledger.STATUS_DONE,), 34),
)
COLOR_PAIRS = {
    color: pair
    for pair, color in enumerate(
        sorted({*display.STATUS_COLORS.values(), *(column.color for column in BOARD_COLUMNS)}),
        start=1,
    )
}
UNICODE_GLYPHS = Glyphs(
    "╭╮╰╯", "┏┓┗┛", "─", "━", "│", "┃", "▌", "…", "·", "↑", "↓", "←", "→", "←→", "↑↓"
)
ASCII_GLYPHS = Glyphs(
    "++++", "####", "-", "=", "|", "#", ">", "~", "|", "^", "v", "<", ">", "h/l", "j/k"
)


@dataclass
class BoardState:
    focus_index: int = 0
    first_column_index: int = 0
    is_showing_all: bool = False
    selected_indices: list[int] = field(default_factory=lambda: [0] * len(BOARD_COLUMNS))
    selected_task_ids: list[str | None] = field(default_factory=lambda: [None] * len(BOARD_COLUMNS))
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
    detail_body_height = height - 1 - DETAIL_PANE_LINE_COUNT - 1 - body_top
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


type Part = tuple[str, str, int | None]


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


def title_segments(width: int, snapshot: BoardSnapshot, glyphs: Glyphs) -> list[Segment]:
    statuses = [row.status for row in snapshot.rows]
    review_count = sum(status in REVIEW_STATUSES for status in statuses)
    summary = f"  {statuses.count(ledger.STATUS_RUNNING)} running {glyphs.separator} {review_count} to review"
    updated = f"updated {snapshot.updated_at}"
    segments = [Segment(0, 0, BOARD_TITLE, STYLE_BOLD)]
    if len(BOARD_TITLE) + len(summary) < width:
        segments.append(Segment(0, len(BOARD_TITLE), summary, STYLE_DIM))
    if len(BOARD_TITLE) + len(summary) + 2 + len(updated) < width:
        segments.append(Segment(0, width - 1 - len(updated), updated, STYLE_DIM))
    return segments


def tail_shortened_directory(directory: str, width: int, glyphs: Glyphs) -> str:
    parts = directory.split("/")
    while len(directory) > width and len(parts) > 1:
        parts = parts[1:]
        directory = f"{glyphs.ellipsis}/{'/'.join(parts)}"
    return fitted(directory, width, glyphs)


def detail_segments(
    footer_y: int, width: int, row: display.ListRow | None, snapshot: BoardSnapshot, glyphs: Glyphs
) -> list[Segment]:
    line_width = width - 1
    lines: list[list[Part]] = [[("No task selected", STYLE_DIM, None)]]
    if row is not None:
        lines = detail_lines(row, line_width, snapshot, glyphs)
    rule_y = footer_y - 1 - len(lines)
    segments = [Segment(rule_y, 0, glyphs.horizontal * line_width, STYLE_DIM)]
    for line_offset, parts in enumerate(lines, start=1):
        segments += part_segments(rule_y + line_offset, 0, parts)
    return segments


def detail_lines(
    row: display.ListRow, line_width: int, snapshot: BoardSnapshot, glyphs: Glyphs
) -> list[list[Part]]:
    status_text = f"  {row.status}"
    step_text = f"  step {row.step}" if row.step else ""
    task_id = fitted(row.task_id, line_width - len(status_text) - len(step_text), glyphs)
    directory = display.display_directory(row.directory)
    lines: list[list[Part]] = [
        [
            (task_id, STYLE_BOLD, None),
            (status_text, STYLE_PLAIN, display.STATUS_COLORS[row.status]),
            (step_text, STYLE_DIM, None),
        ],
        [(tail_shortened_directory(directory, line_width, glyphs), STYLE_DIM, None)],
    ]
    if row.detail:
        lines.append([(fitted(row.detail, line_width, glyphs), STYLE_PLAIN, None)])
    prompt = " ".join(snapshot.prompts.get(row.task_id, "").split())
    if prompt and line_width > len(glyphs.ellipsis) + 1:
        prompt_lines = textwrap.wrap(
            prompt,
            line_width,
            max_lines=DETAIL_PANE_LINE_COUNT - 1 - len(lines),
            placeholder=f" {glyphs.ellipsis}",
        )
        prompt_parts: list[list[Part]] = [[(line, STYLE_DIM, None)] for line in prompt_lines]
        lines += prompt_parts
    return lines


def footer_segments(y: int, width: int, state: BoardState, glyphs: Glyphs) -> list[Segment]:
    finished_action = "hide" if state.is_showing_all else "show"
    key_sets = [
        [
            (glyphs.column_keys, "column"),
            (glyphs.select_keys, "select"),
            ("a", f"{finished_action} finished"),
            ("q", "quit"),
        ],
        [
            (glyphs.column_keys, "col"),
            (glyphs.select_keys, "sel"),
            ("a", "finished"),
            ("q", "quit"),
        ],
        [(glyphs.column_keys, ""), (glyphs.select_keys, ""), ("a", ""), ("q", "")],
    ]
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
        segments += tab_segments(TITLE_LINE_COUNT, width, columns, layout, state, glyphs)
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
        segments += detail_segments(height - 1, width, selected_row, snapshot, glyphs)
    segments += footer_segments(height - 1, width, state, glyphs)
    return clipped(segments, width, height)


def apply_key(state: BoardState, key: int) -> None:
    if key in LEFT_KEYS:
        state.focus_index = max(state.focus_index - 1, 0)
    elif key in RIGHT_KEYS:
        state.focus_index = min(state.focus_index + 1, len(BOARD_COLUMNS) - 1)
    elif key in DOWN_KEYS | UP_KEYS:
        state.selected_indices[state.focus_index] += 1 if key in DOWN_KEYS else -1
        state.selected_task_ids[state.focus_index] = None
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
        pressed_key = screen.getch()
        if pressed_key == ord("q"):
            return 0
        apply_key(state, pressed_key)
        is_redraw_needed = pressed_key != -1


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
