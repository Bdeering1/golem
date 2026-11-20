import contextlib
import curses
import os
import select
import signal
import sys
import termios
import tty
from typing import TYPE_CHECKING

from golem import board, display

if TYPE_CHECKING:
    from collections.abc import Generator

CANCELLED_MESSAGE = "golem: cancelled"
CHROME_LINE_COUNT = 2
MAX_VISIBLE_ROW_COUNT = 10
MIN_PROMPT_WIDTH = 8
CANCEL_KEYS = {board.ESCAPE_KEY, ord("q")}
TOGGLE_KEY = ord(" ")
TOGGLE_ALL_KEY = ord("a")
SELECTED_MARK = "[x]"
UNSELECTED_MARK = "[ ]"
CANCEL_LABEL = "cancel"
ESCAPE_DELAY_SECONDS = board.ESCAPE_DELAY_MILLISECONDS / 1000
RESIZE_POLL_SECONDS = 0.2
ARROW_KEYS = {
    "[A": curses.KEY_UP,
    "OA": curses.KEY_UP,
    "[B": curses.KEY_DOWN,
    "OB": curses.KEY_DOWN,
}
STYLE_CODES = {board.STYLE_PLAIN: [], board.STYLE_BOLD: ["1"], board.STYLE_DIM: ["2"]}
SAVE_CURSOR = "\x1b7"
RESTORE_CURSOR = "\x1b8"
CLEAR_TO_END = "\x1b[J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"


def fitted_lines(rows: list[tuple[str, ...]], width: int, glyphs: board.Glyphs) -> list[str]:
    leading_lines = display.aligned_lines([row[:-1] for row in rows])
    leading_width = max(len(line) for line in leading_lines)
    prompt_width = width - leading_width - board.COLUMN_GAP
    lines: list[str] = []
    for leading_line, row in zip(leading_lines, rows, strict=True):
        line = leading_line
        if prompt_width >= MIN_PROMPT_WIDTH and row[-1]:
            prompt = board.fitted(row[-1], prompt_width, glyphs)
            line = f"{leading_line.ljust(leading_width)}{' ' * board.COLUMN_GAP}{prompt}"
        lines.append(board.fitted(line, width, glyphs))
    return lines


def visible_row_count(height: int) -> int:
    return max(1, height - CHROME_LINE_COUNT)


def key_hint_sets(
    glyphs: board.Glyphs, enter_label: str, *, is_multi_select: bool
) -> list[list[tuple[str, str]]]:
    if not is_multi_select:
        return [
            [(glyphs.select_keys, "move"), (glyphs.enter_key, enter_label), ("q", CANCEL_LABEL)],
            [(glyphs.enter_key, enter_label), ("q", CANCEL_LABEL)],
        ]
    return [
        [
            (glyphs.select_keys, "move"),
            ("space", "select"),
            ("a", "all"),
            (glyphs.enter_key, enter_label),
            ("q", CANCEL_LABEL),
        ],
        [("space", "select"), ("a", "all"), (glyphs.enter_key, enter_label), ("q", CANCEL_LABEL)],
        [("space", ""), ("a", ""), (glyphs.enter_key, ""), ("q", "")],
    ]


def picker_segments(
    title: str,
    lines: list[str],
    highlight_index: int,
    top_index: int,
    size: tuple[int, int],
    key_sets: list[list[tuple[str, str]]],
) -> list[board.Segment]:
    height, width = size
    segments = [board.Segment(0, 0, title, board.STYLE_BOLD)]
    visible_lines = lines[top_index : top_index + visible_row_count(height)]
    for offset, line in enumerate(visible_lines, start=top_index):
        is_highlighted = offset == highlight_index
        segments.append(
            board.Segment(
                1 + offset - top_index,
                0,
                line.ljust(width) if is_highlighted else line,
                background=board.SELECTION_BACKGROUND if is_highlighted else None,
            )
        )
    segments += board.key_hint_segments(height - 1, width, key_sets)
    return board.clipped(segments, width, height)


def style_codes(segment: board.Segment, *, is_color: bool) -> list[str]:
    codes = STYLE_CODES[segment.style]
    if not is_color:
        return codes + (["7"] if segment.background is not None else [])
    color = segment.color
    if segment.style == board.STYLE_DIM:
        muted_color = (
            board.MUTED_COLOR if segment.background is None else board.SELECTED_MUTED_COLOR
        )
        codes, color = [], color or muted_color
    if color is not None:
        codes = [*codes, f"38;5;{color}"]
    if segment.background is not None:
        codes = [*codes, f"48;5;{segment.background}"]
    return codes


def styled_lines(segments: list[board.Segment], height: int, *, is_color: bool) -> list[str]:
    lines = [""] * height
    line_widths = [0] * height
    for segment in sorted(segments, key=lambda segment: (segment.y, segment.x)):
        gap = " " * (segment.x - line_widths[segment.y])
        codes = ";".join(style_codes(segment, is_color=is_color))
        lines[segment.y] += f"{gap}\x1b[{codes}m{segment.text}\x1b[m"
        line_widths[segment.y] = segment.x + len(segment.text)
    return lines


def write_terminal(text: str) -> None:
    os.write(sys.stdout.fileno(), text.encode(sys.stdout.encoding, "replace"))


def is_input_ready(timeout_seconds: float) -> bool:
    return bool(select.select([sys.stdin], [], [], timeout_seconds)[0])


def read_byte() -> int:
    return os.read(sys.stdin.fileno(), 1)[0]


def read_key(timeout_seconds: float = RESIZE_POLL_SECONDS) -> int | None:
    if not is_input_ready(timeout_seconds):
        return None
    key = read_byte()
    if key != board.ESCAPE_KEY:
        return key
    sequence = ""
    while is_input_ready(ESCAPE_DELAY_SECONDS):
        char = read_byte()
        sequence += chr(char)
        if sequence[0] not in board.ESCAPE_SEQUENCE_STARTS:
            break
        if len(sequence) > 1 and char in board.ESCAPE_SEQUENCE_FINAL_KEYS:
            break
    if not sequence:
        return board.ESCAPE_KEY
    return ARROW_KEYS.get(sequence)


class InlineTerminal:
    def __init__(self, line_count: int) -> None:
        self.line_count = line_count

    def size(self) -> tuple[int, int]:
        width, height = os.get_terminal_size(sys.stdout.fileno())
        return min(self.line_count, height), width

    def read_key(self) -> int | None:
        return read_key()

    def draw(self, segments: list[board.Segment], *, is_color: bool) -> None:
        lines = styled_lines(segments, self.size()[0], is_color=is_color)
        write_terminal(RESTORE_CURSOR + CLEAR_TO_END + "\r\n".join(lines))


def is_color_terminal() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    try:
        curses.setupterm(fd=sys.stdout.fileno())
    except curses.error:
        return False
    return curses.tigetnum("colors") >= board.MIN_COLOR_COUNT


def picker_line_count(row_count: int, terminal_height: int) -> int:
    return min(min(row_count, MAX_VISIBLE_ROW_COUNT) + CHROME_LINE_COUNT, terminal_height)


@contextlib.contextmanager
def inline_terminal(row_count: int) -> Generator[tuple[InlineTerminal, bool, board.Glyphs]]:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    line_count = picker_line_count(row_count, os.get_terminal_size(sys.stdout.fileno()).lines)
    glyphs = board.UNICODE_GLYPHS if board.is_utf8_locale() else board.ASCII_GLYPHS
    is_color = is_color_terminal()
    sys.stdout.flush()
    saved_attributes = tty.setcbreak(sys.stdin.fileno())
    # Scroll room for the picker first so the saved cursor position stays valid.
    write_terminal(f"{HIDE_CURSOR}{'\n' * (line_count - 1)}\x1b[{line_count - 1}A\r{SAVE_CURSOR}")
    try:
        yield InlineTerminal(line_count), is_color, glyphs
    finally:
        write_terminal(RESTORE_CURSOR + CLEAR_TO_END + SHOW_CURSOR)
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_attributes)


def picked_indices(
    title: str, rows: list[tuple[str, ...]], enter_label: str, *, is_multi_select: bool
) -> set[int] | None:
    highlight_index = 0
    top_index = 0
    selected_indices: set[int] = set()
    shown_segments: list[board.Segment] = []
    with inline_terminal(len(rows)) as (terminal, is_color, glyphs):
        key_sets = key_hint_sets(glyphs, enter_label, is_multi_select=is_multi_select)
        while True:
            height, width = terminal.size()
            row_count = visible_row_count(height)
            top_index = min(max(top_index, highlight_index - row_count + 1), highlight_index)
            marked_rows = rows
            if is_multi_select:
                marked_rows = [
                    (SELECTED_MARK if index in selected_indices else UNSELECTED_MARK, *row)
                    for index, row in enumerate(rows)
                ]
            segments = picker_segments(
                board.fitted(title, width, glyphs),
                fitted_lines(marked_rows, width, glyphs),
                highlight_index,
                top_index,
                (height, width),
                key_sets,
            )
            if segments != shown_segments:
                terminal.draw(segments, is_color=is_color)
                shown_segments = segments
            key = terminal.read_key()
            if key in board.UP_KEYS:
                highlight_index = max(0, highlight_index - 1)
            elif key in board.DOWN_KEYS:
                highlight_index = min(len(rows) - 1, highlight_index + 1)
            elif key in CANCEL_KEYS:
                return None
            elif key in board.ENTER_KEYS:
                return selected_indices if is_multi_select else {highlight_index}
            elif is_multi_select and key == TOGGLE_KEY:
                selected_indices ^= {highlight_index}
            elif is_multi_select and key == TOGGLE_ALL_KEY:
                is_all_selected = len(selected_indices) == len(rows)
                selected_indices = set() if is_all_selected else set(range(len(rows)))
