import os
import sys
import time
from datetime import date
from pathlib import Path

import pexpect
import pyte
import pytest

from golem import board, cli, display, ledger
from tests import support

TODAY = date(2030, 3, 10)
WIDE_WIDTH = 4 * board.MIN_COLUMN_WIDTH
NARROW_WIDTH = board.MIN_COLUMN_WIDTH + 8
HEIGHT = 24
PTY_WIDTH = 100
PTY_TIMEOUT_SECONDS = 5
SHOW_CURSOR = b"\x1b[?25h"
EXIT_ALTERNATE_SCREEN = b"\x1b[?1049l"
REPO_ROOT = Path(__file__).parent.parent
ARROW_KEYS = ("\x1bOD", "\x1bOC", "\x1bOA", "\x1bOB")
LETTER_KEYS = ("h", "l", "k", "j")


def frame_lines(rows, width, height, state):
    lines = [[" "] * width for _ in range(height)]
    board.fit_state_to_layout(rows, width, height, state)
    for segment in board.board_frame(rows, width, height, state):
        for offset, character in enumerate(segment.text):
            lines[segment.y][segment.x + offset] = character
    return ["".join(line).rstrip() for line in lines]


def frame_text(rows, width=WIDE_WIDTH, height=HEIGHT, state=None):
    return "\n".join(frame_lines(rows, width, height, state or board.BoardState()))


def running_tasks():
    return {
        f"task-{index:02}": support.task_record(ledger.STATUS_RUNNING, task_id=f"task-{index:02}")
        for index in range(10)
    }


def statuses_tasks():
    return {
        status: support.task_record(status, task_id=status)
        for status in sorted(ledger.ALL_STATUSES)
        if status != ledger.STATUS_WAITING
    } | {
        ledger.STATUS_WAITING: support.task_record(
            ledger.STATUS_WAITING, task_id=ledger.STATUS_WAITING, after="missing"
        )
    }


@pytest.mark.parametrize("is_done_shown", [False, True])
def test_board_columns_group_statuses_and_skip_cancelled(is_done_shown):
    rows = display.list_rows(statuses_tasks(), TODAY)

    columns = board.rows_by_column(rows, is_done_shown=is_done_shown)

    expected = [
        ("Queued", {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}),
        ("Running", {ledger.STATUS_RUNNING}),
        ("Review", {ledger.STATUS_NEEDS_REVIEW, ledger.STATUS_FAILED}),
    ]
    if is_done_shown:
        expected.append(("Done", {ledger.STATUS_DONE}))
    assert [
        (board_column.title, {row.task_id for row in column_rows})
        for board_column, column_rows in columns
    ] == expected


def test_board_shows_a_chain_as_one_card_with_its_step_and_list_detail():
    tasks = {
        "root": support.task_record(
            ledger.STATUS_NEEDS_REVIEW, task_id="root", gate_stop_reason="no pull request yet"
        ),
        "child": support.task_record(ledger.STATUS_WAITING, task_id="child", after="root"),
    }
    rows = display.list_rows(tasks, TODAY)

    text = frame_text(rows)

    assert len(rows) == 1
    assert "root  1/2" in text
    assert "child" not in text
    assert display.list_detail(tasks["root"], TODAY) in text


def test_board_wraps_fields_without_cutting_text_and_shortens_long_directories():
    task_id = "a-very-long-task-identifier-that-wraps"
    directory = "/somewhere/deeply/nested/inside/a/long/path/project-leaf"
    tasks = {
        task_id: support.task_record(
            ledger.STATUS_FAILED,
            task_id=task_id,
            cwd=directory,
            gate_stop_reason="the gate stopped because the tests failed in several places",
        )
    }
    rows = display.list_rows(tasks, TODAY)
    width = 24

    lines = frame_lines(rows, width, HEIGHT, board.BoardState(focus_index=2))

    body = "".join(line.strip() for line in lines[1:-1])
    assert task_id in body
    assert ".../project-leaf" in body
    assert "thegatestoppedbecausethetestsfailedinseveralplaces" in body.replace(" ", "")
    assert all(len(line) <= width - board.COLUMN_GAP for line in lines[1:-1])


def test_board_focus_stays_within_shown_columns():
    rows = display.list_rows(statuses_tasks(), TODAY)
    state = board.BoardState(is_done_shown=True)
    for _ in range(len(board.BOARD_COLUMNS)):
        board.apply_key(state, ord("l"))

    board.apply_key(state, ord("a"))

    assert frame_lines(rows, NARROW_WIDTH, HEIGHT, state)[0] == "Review (2)"


def test_board_scroll_stops_at_the_end_of_a_column():
    tasks = running_tasks()
    rows = display.list_rows(tasks, TODAY)
    state = board.BoardState(focus_index=1)
    height = 8

    for _ in range(100):
        board.apply_key(state, ord("j"))
    bottom_lines = frame_lines(rows, NARROW_WIDTH, height, state)
    board.apply_key(state, ord("k"))
    scrolled_up_lines = frame_lines(rows, NARROW_WIDTH, height, state)

    assert "task-09" in "\n".join(bottom_lines)
    assert bottom_lines[height - 2]
    assert scrolled_up_lines[2:-1] == bottom_lines[1:-2]


@pytest.mark.parametrize(("width", "height"), [(10, 3), (1, 1), (40, 2)])
def test_board_frame_fits_tiny_terminals(width, height):
    rows = display.list_rows(statuses_tasks(), TODAY)

    segments = board.board_frame(rows, width, height, board.BoardState())

    assert all(0 <= segment.y < height for segment in segments)
    assert all(segment.x + len(segment.text) <= width for segment in segments)
    assert all(
        segment.x + len(segment.text) < width for segment in segments if segment.y == height - 1
    )


@pytest.mark.parametrize(
    ("is_tty", "term"), [pytest.param(False, "xterm", id="not-tty"), pytest.param(True, "dumb")]
)
def test_board_refuses_to_start_without_a_terminal(monkeypatch, is_tty, term):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("TERM", term)

    with pytest.raises(SystemExit, match="golem list"):
        cli.main(["board"])


class PtyBoard:
    def __init__(self, *, is_color=True):
        environment = dict(os.environ, TERM="xterm-256color")
        environment.pop("NO_COLOR", None)
        if not is_color:
            environment["NO_COLOR"] = "1"
        self.screen = pyte.Screen(PTY_WIDTH, HEIGHT)
        self.stream = pyte.ByteStream(self.screen)
        self.output = b""
        self.child = pexpect.spawn(
            sys.executable,
            ["-m", "golem", "board"],
            env=environment,
            dimensions=(HEIGHT, PTY_WIDTH),
            cwd=REPO_ROOT,
            timeout=PTY_TIMEOUT_SECONDS,
        )

    def text(self):
        return "\n".join(self.screen.display)

    def wait_until(self, predicate):
        deadline = time.monotonic() + PTY_TIMEOUT_SECONDS
        while not predicate():
            assert time.monotonic() < deadline, self.text()
            try:
                data = self.child.read_nonblocking(65536, timeout=0.05)
            except pexpect.TIMEOUT:
                continue
            self.output += data
            self.stream.feed(data)

    def wait_for_text(self, text):
        self.wait_until(lambda: text in self.text())

    def is_focused(self, title):
        header_line = self.screen.display[0]
        return title in header_line and self.screen.buffer[0][header_line.index(title)].reverse

    def send(self, keys):
        self.child.send(keys)

    def resize(self, width):
        self.screen.resize(HEIGHT, width)
        self.child.setwinsize(HEIGHT, width)

    def exit_status(self):
        self.output += self.child.read()
        self.child.close()
        return self.child.exitstatus


@pytest.fixture
def pty_board(ledger_path):
    boards = []

    def start(*, is_color=True, tasks=None) -> PtyBoard:
        support.write_tasks(ledger_path, tasks or statuses_tasks())
        pty_board = PtyBoard(is_color=is_color)
        boards.append(pty_board)
        pty_board.wait_for_text("q quit")
        return pty_board

    yield start
    for started_board in boards:
        started_board.child.close(force=True)


@pytest.mark.parametrize(("key", "status"), [("q", 0), ("\x03", board.INTERRUPTED_EXIT_STATUS)])
def test_board_exits_and_restores_the_terminal(pty_board, key, status):
    running_board = pty_board()

    running_board.send(key)

    assert running_board.exit_status() == status
    tail = running_board.output.rsplit(b"Queued", 1)[-1]
    assert EXIT_ALTERNATE_SCREEN in tail
    assert SHOW_CURSOR in tail


def test_board_redraws_after_a_ledger_change(pty_board):
    running_board = pty_board()
    tasks = statuses_tasks()
    tasks["added"] = support.task_record(ledger.STATUS_RUNNING, task_id="added")

    ledger.save_tasks(tasks)

    running_board.wait_for_text("Running (2)")


def test_board_switches_columns_after_resizing_to_one_column(pty_board):
    running_board = pty_board()

    running_board.resize(NARROW_WIDTH)
    running_board.wait_until(lambda: "Running" not in running_board.text())
    running_board.send("lll")
    running_board.wait_for_text("Review (2)")
    running_board.send("al")

    running_board.wait_for_text("Done (1)")


@pytest.mark.parametrize("keys", [ARROW_KEYS, LETTER_KEYS], ids=["arrows", "letters"])
def test_board_keys_switch_columns_and_scroll(pty_board, keys):
    left, right, up, down = keys
    tasks = statuses_tasks() | running_tasks()
    running_board = pty_board(tasks=tasks)
    top_lines = running_board.screen.display[1:]

    running_board.send(right)
    running_board.wait_until(lambda: running_board.is_focused("Running"))
    running_board.send(down)
    running_board.wait_until(lambda: running_board.screen.display[1:] != top_lines)
    running_board.send(up)
    running_board.wait_until(lambda: running_board.screen.display[1:] == top_lines)
    running_board.send(left)

    running_board.wait_until(lambda: running_board.is_focused("Queued"))


@pytest.mark.parametrize("is_color", [True, False])
def test_board_keeps_the_terminal_background(pty_board, is_color):
    running_board = pty_board(is_color=is_color)

    characters = [
        character for line in running_board.screen.buffer.values() for character in line.values()
    ]

    assert all(character.bg == "default" for character in characters)
    assert any(character.reverse for character in characters)
    assert any(character.bold for character in characters)
    has_foreground_color = any(character.fg != "default" for character in characters)
    assert has_foreground_color == is_color
