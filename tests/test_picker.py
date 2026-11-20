import curses
import os
import sys

import pytest

from golem import board, picker
from tests import support

NARROW_WIDTH = 40
LABEL = "GH-1"
LONG_PROMPT = "rewrite the scheduler so queued tasks start in the order they were added"


def picker_rows(row_count):
    return [(f"t{index}", LABEL, "finished", "~/golem", LONG_PROMPT) for index in range(row_count)]


def test_picker_scrolls_to_keep_highlight_visible(monkeypatch):
    screen = support.use_fake_terminal(
        monkeypatch, [ord("j")] * 6 + [support.ENTER_KEY], size=(5, 80)
    )

    assert picker.picked_indices("Pick", picker_rows(10), "pick", is_multi_select=False) == {6}
    shown_ids = [line.split()[0] for line in screen.frames[-1][1:-1]]
    assert shown_ids == ["t4", "t5", "t6"]


def test_picker_fits_narrow_ascii_terminal_by_cutting_prompt_first(monkeypatch):
    screen = support.use_fake_terminal(monkeypatch, [support.ENTER_KEY], size=(10, NARROW_WIDTH))

    picker.picked_indices("Pick", picker_rows(3), "pick", is_multi_select=False)

    frame = screen.frames[0]
    assert all(len(line) <= NARROW_WIDTH and line.isascii() for line in frame)
    assert frame[1].split()[:4] == ["t0", LABEL, "finished", "~/golem"]
    assert frame[1].rstrip().endswith(board.ASCII_GLYPHS.ellipsis)


@pytest.mark.parametrize(
    ("typed", "key"),
    [
        pytest.param(b"\x1b[A", curses.KEY_UP, id="up"),
        pytest.param(b"\x1bOB", curses.KEY_DOWN, id="application-down"),
        pytest.param(b"\x1b", board.ESCAPE_KEY, id="escape"),
        pytest.param(b"\x1b[6~", None, id="unmapped-sequence"),
    ],
)
def test_read_key_tells_escape_from_arrow_sequences(monkeypatch, typed, key):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, typed)
    with os.fdopen(read_fd, "rb", buffering=0) as typed_input:
        monkeypatch.setattr(sys, "stdin", typed_input)

        assert picker.read_key() == key
    os.close(write_fd)
