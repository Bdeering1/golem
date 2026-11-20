import curses

from golem import board, display

CANCELLED_MESSAGE = "golem: cancelled"
CHROME_LINE_COUNT = 2
MIN_PROMPT_WIDTH = 8
CANCEL_KEYS = {board.ESCAPE_KEY, ord("q")}
TOGGLE_KEY = ord(" ")
TOGGLE_ALL_KEY = ord("a")
SELECTED_MARK = "[x]"
UNSELECTED_MARK = "[ ]"
CANCEL_LABEL = "cancel"


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


def picked_indices(
    title: str, rows: list[tuple[str, ...]], enter_label: str, *, is_multi_select: bool
) -> set[int] | None:
    highlight_index = 0
    top_index = 0
    selected_indices: set[int] = set()
    with board.terminal_screen() as (screen, is_color, glyphs):
        key_sets = key_hint_sets(glyphs, enter_label, is_multi_select=is_multi_select)
        while True:
            height, width = screen.getmaxyx()
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
            board.draw_segments(screen, segments, is_color=is_color)
            key = board.read_key(screen)
            if key == curses.KEY_RESIZE:
                screen.clear()
            elif key in board.UP_KEYS:
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
