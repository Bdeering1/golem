import contextlib
import curses
import fcntl
import os
import pty
import struct
import sys
import termios

import pytest

from golem import board, picker
from tests import support

NARROW_WIDTH = 40
LABEL = "GH-1"
LOCAL_MODES_INDEX = 3
LINE_MODES = termios.ECHO | termios.ICANON
LONG_PROMPT = "rewrite the scheduler so queued tasks start in the order they were added"


def picker_rows(row_count):
    return [(f"t{index}", LABEL, "finished", "~/golem", LONG_PROMPT) for index in range(row_count)]


def test_picker_scrolls_to_keep_highlight_visible(monkeypatch):
    screen = support.use_fake_terminal(
        monkeypatch, [ord("j")] * 6 + [support.ENTER_KEY], size=(5, 80)
    )

    assert picker.picked_indices("Pick", picker_rows(10), "pick", is_multi_select=False) == {6}
    title, *shown_lines, _ = screen.frames[-1]
    assert "7/10" in title
    assert [line.split()[0] for line in shown_lines] == ["t4", "t5", "t6"]


def test_picker_fits_narrow_terminal_by_cutting_prompt_first(monkeypatch):
    screen = support.use_fake_terminal(monkeypatch, [support.ENTER_KEY], size=(10, NARROW_WIDTH))

    picker.picked_indices("Pick", picker_rows(3), "pick", is_multi_select=False)

    frame = screen.frames[0]
    assert all(len(line) <= NARROW_WIDTH for line in frame)
    assert frame[1].split()[:4] == ["t0", LABEL, "finished", "~/golem"]
    assert frame[1].rstrip().endswith(board.ASCII_GLYPHS.ellipsis)


@pytest.mark.parametrize(
    ("typed", "key"),
    [
        pytest.param(b"\x1b[A", curses.KEY_UP, id="up"),
        pytest.param(b"\x1bOB", curses.KEY_DOWN, id="application-down"),
        pytest.param(b"\x1b", board.ESCAPE_KEY, id="escape"),
        pytest.param(b"\x1bj", board.ESCAPE_KEY, id="escape-then-key"),
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


@pytest.mark.parametrize("key", [lambda: ord("q"), support.interrupt], ids=["cancel", "ctrl-c"])
def test_picker_restores_terminal_on_exit(monkeypatch, key):
    controller_fd, terminal_fd = pty.openpty()
    fcntl.ioctl(terminal_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    original_local_modes = termios.tcgetattr(terminal_fd)[LOCAL_MODES_INDEX] & LINE_MODES
    written = []
    with os.fdopen(terminal_fd, "w", encoding="utf-8") as terminal:
        monkeypatch.setattr(sys, "stdin", terminal)
        monkeypatch.setattr(sys, "stdout", terminal)
        monkeypatch.setattr(picker, "read_key", key)
        monkeypatch.setattr(picker, "write_terminal", written.append)
        with contextlib.suppress(KeyboardInterrupt):
            picker.picked_indices("Pick", picker_rows(3), "pick", is_multi_select=False)

        local_modes = termios.tcgetattr(terminal_fd)[LOCAL_MODES_INDEX] & LINE_MODES
        assert local_modes == original_local_modes
    os.close(controller_fd)
    assert written[-1].endswith(picker.SHOW_CURSOR)
