import curses
import locale
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pexpect
import pyte
import pytest

from golem import board, cli, display, ledger
from tests import support

TODAY = date(2030, 3, 10)
NOW = datetime(2030, 3, 10, 12).astimezone()
WIDE_WIDTH = 88
NARROW_WIDTH = board.MIN_COLUMN_WIDTH + 8
HEIGHT = 24
PTY_WIDTH = 100
PTY_TIMEOUT_SECONDS = 5
SHOW_CURSOR = b"\x1b[?25h"
EXIT_ALTERNATE_SCREEN = b"\x1b[?1049l"
XTERM_REP_CAPABILITY = b"%p1%c\x1b[%p2%{1}%-%db"
REPO_ROOT = Path(__file__).parent.parent
ARROW_KEYS = ("\x1bOD", "\x1bOC", "\x1bOA", "\x1bOB")
LETTER_KEYS = ("h", "l", "k", "j")
MERGED_PULL_REQUEST = {"url": "https://github.com/owner/repository/pull/2", "state": "merged"}


def frame_lines(tasks, width, height, state, glyphs=board.UNICODE_GLYPHS):
    snapshot = board.board_snapshot(tasks, NOW)
    lines = [[" "] * width for _ in range(height)]
    board.fit_state_to_layout(snapshot.rows, width, height, state)
    for segment in board.board_frame(snapshot, width, height, state, glyphs):
        for offset, character in enumerate(segment.text):
            lines[segment.y][segment.x + offset] = character
    return ["".join(line).rstrip() for line in lines]


def frame_text(tasks, width=WIDE_WIDTH, height=HEIGHT, state=None):
    return "\n".join(frame_lines(tasks, width, height, state or board.BoardState()))


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


@pytest.mark.parametrize(
    ("pressed_keys", "done_ids"),
    [("", {ledger.STATUS_DONE}), ("a", {ledger.STATUS_DONE, "merged"})],
)
def test_board_columns_group_statuses_and_a_toggles_what_list_hides(pressed_keys, done_ids):
    tasks = statuses_tasks() | {
        "merged": support.task_record(
            ledger.STATUS_DONE, task_id="merged", pull_requests=[MERGED_PULL_REQUEST]
        )
    }
    rows = display.list_rows(tasks, TODAY)
    state = board.BoardState()
    for pressed_key in pressed_keys:
        board.apply_key(state, ord(pressed_key))

    columns = board.rows_by_column(rows, is_showing_all=state.is_showing_all)

    expected = [
        ("Queued", {ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING}),
        ("Running", {ledger.STATUS_RUNNING}),
        ("Review", {ledger.STATUS_NEEDS_REVIEW, ledger.STATUS_FAILED}),
        ("Done", done_ids),
    ]
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

    text = frame_text(tasks, state=board.BoardState(focus_index=2))

    assert "1/2" in text
    assert "child" not in text
    assert display.list_detail(tasks["root"], TODAY) in text


def test_board_shortens_card_text_and_shows_it_in_full_in_the_detail_pane():
    task_id = "a-very-long-task-identifier-that-is-shortened"
    directory = "/somewhere/deeply/nested/project-leaf"
    tasks = {
        task_id: support.task_record(
            ledger.STATUS_RUNNING, task_id=task_id, cwd=directory, prompt="Polish the board\nmore"
        )
    }

    lines = frame_lines(tasks, WIDE_WIDTH, HEIGHT, board.BoardState(focus_index=1))

    assert "a-very-long-tas…" in "\n".join(lines)
    assert lines[-4:-1] == [f"{task_id}  running", directory, "Polish the board more"]


def test_board_detail_pane_keeps_the_end_of_a_long_directory():
    tasks = {
        "task": support.task_record(ledger.STATUS_RUNNING, cwd="/somewhere/deeply/nested/leaf")
    }

    lines = frame_lines(tasks, NARROW_WIDTH, HEIGHT, board.BoardState(focus_index=1))

    assert "…/deeply/nested/leaf" in lines


@pytest.mark.parametrize(
    ("width", "shown_titles", "has_tabs"),
    [
        pytest.param(WIDE_WIDTH, ["Queued", "Running", "Review", "Done"], False, id="all-fit"),
        pytest.param(46, ["Queued", "Running"], True, id="two-fit"),
        pytest.param(NARROW_WIDTH, ["Queued"], True, id="one-fits"),
    ],
)
def test_board_shows_as_many_columns_as_fit(width, shown_titles, has_tabs):
    lines = frame_lines(statuses_tasks(), width, HEIGHT, board.BoardState())

    header_y = board.board_layout(width, HEIGHT).header_y
    header_line = lines[header_y]
    assert [title for title, *_ in board.BOARD_COLUMNS if title in header_line] == shown_titles
    assert (header_y > board.TITLE_LINE_COUNT and bool(lines[board.TITLE_LINE_COUNT])) == has_tabs


@pytest.mark.parametrize(
    ("width", "focus_index", "left_text", "right_text"),
    [
        pytest.param(46, 2, "← Queued 2", "Done 1 →", id="both-sides"),
        pytest.param(28, 1, "← Queued 2", "Review 2  +1 →", id="folds-farthest"),
        pytest.param(28, 0, "", "Running 1  Review 2  +1 →", id="right-only"),
    ],
)
def test_board_lists_hidden_columns_on_the_side_they_scroll_in_from(
    width, focus_index, left_text, right_text
):
    lines = frame_lines(statuses_tasks(), width, HEIGHT, board.BoardState(focus_index=focus_index))

    tab_line = lines[board.TITLE_LINE_COUNT]
    assert tab_line.startswith(left_text)
    assert tab_line.endswith(right_text)


@pytest.mark.parametrize(
    ("width", "footer"),
    [
        (WIDE_WIDTH, "←→ column  ↑↓ select  a show finished  q quit"),
        (40, "←→ col  ↑↓ sel  a finished  q quit"),
        (NARROW_WIDTH, "←→  ↑↓  a  q"),
    ],
)
def test_board_footer_shortens_to_fit(width, footer):
    assert frame_lines(statuses_tasks(), width, HEIGHT, board.BoardState())[-1] == footer


def test_board_selection_moves_within_a_column_and_scrolls_to_stay_visible():
    state = board.BoardState(focus_index=1)

    for _ in range(100):
        board.apply_key(state, ord("j"))
    bottom_text = frame_text(running_tasks(), state=state)
    board.apply_key(state, ord("k"))
    scrolled_up_text = frame_text(running_tasks(), state=state)

    assert f"{board.UNICODE_GLYPHS.heavy_vertical} task-09" in bottom_text
    assert f"{board.UNICODE_GLYPHS.heavy_vertical} task-08" in scrolled_up_text


def test_board_selection_stays_on_its_task_when_a_task_is_added_above_it():
    tasks = running_tasks()
    added_task = tasks.pop("task-00")
    state = board.BoardState(focus_index=1)
    board.apply_key(state, ord("j"))
    frame_text(tasks, state=state)

    text = frame_text(tasks | {"task-00": added_task}, state=state)

    assert "task-02  running" in text


def test_board_marks_empty_columns():
    assert "No tasks" in frame_text({})


@pytest.mark.parametrize(("width", "height"), [(10, 3), (1, 1), (40, 2), (WIDE_WIDTH, 9)])
def test_board_frame_fits_tiny_terminals(width, height):
    snapshot = board.board_snapshot(statuses_tasks(), NOW)

    segments = board.board_frame(snapshot, width, height, board.BoardState())

    assert all(0 <= segment.y < height for segment in segments)
    assert all(segment.x + len(segment.text) <= width for segment in segments)
    assert all(
        segment.x + len(segment.text) < width for segment in segments if segment.y == height - 1
    )


def test_board_draws_only_ascii_with_ascii_glyphs():
    lines = frame_lines(running_tasks(), WIDE_WIDTH, HEIGHT, board.BoardState(), board.ASCII_GLYPHS)

    assert all(line.isascii() for line in lines)


@pytest.mark.parametrize(
    ("ncurses_version", "rep_capability", "expected_glyphs"),
    [
        pytest.param((6, 0, 20150808), XTERM_REP_CAPABILITY, board.ASCII_GLYPHS, id="old-rep"),
        pytest.param((6, 0, 20150808), None, board.UNICODE_GLYPHS, id="old-no-rep"),
        pytest.param(
            board.WIDE_REP_FIX_NCURSES_VERSION,
            XTERM_REP_CAPABILITY,
            board.UNICODE_GLYPHS,
            id="fixed-rep",
        ),
        pytest.param((6, 1, 20180127), XTERM_REP_CAPABILITY, board.UNICODE_GLYPHS, id="newer"),
    ],
)
def test_terminal_glyphs_avoid_unicode_when_ncurses_mangles_wide_rep(
    monkeypatch, ncurses_version, rep_capability, expected_glyphs
):
    monkeypatch.setattr(locale, "nl_langinfo", lambda _item: "UTF-8")
    monkeypatch.setattr(curses, "ncurses_version", ncurses_version)
    monkeypatch.setattr(curses, "tigetstr", lambda _name: rep_capability)

    assert board.terminal_glyphs() == expected_glyphs


@pytest.mark.parametrize(
    ("is_stdout_tty", "is_stdin_tty", "term"),
    [
        pytest.param(False, True, "xterm", id="stdout-not-tty"),
        pytest.param(True, False, "xterm", id="stdin-not-tty"),
        pytest.param(True, True, "dumb", id="dumb"),
    ],
)
def test_board_refuses_to_start_without_a_terminal(monkeypatch, is_stdout_tty, is_stdin_tty, term):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_stdout_tty)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: is_stdin_tty)
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
        return any(
            self.screen.buffer[y][x].bold and not self.screen.buffer[y][x].underscore
            for y, line in enumerate(self.screen.display)
            for x in range(len(line))
            if line.startswith(f"{title} ", x)
        )

    def selected_task_id(self):
        lines = self.screen.display
        rule_y = max(y for y, line in enumerate(lines) if line.startswith(("──", "--")))
        return lines[rule_y + 1].split()[0]

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

    running_board.wait_for_text("Running 2")


def test_board_switches_columns_after_resizing_to_one_column(pty_board):
    running_board = pty_board()

    running_board.resize(NARROW_WIDTH)
    running_board.wait_until(lambda: "Done 1" not in running_board.text())
    running_board.send("lll")

    running_board.wait_until(lambda: running_board.is_focused("Done"))


@pytest.mark.parametrize("keys", [ARROW_KEYS, LETTER_KEYS], ids=["arrows", "letters"])
def test_board_keys_switch_columns_and_select_tasks(pty_board, keys):
    left, right, up, down = keys
    tasks = statuses_tasks() | running_tasks()
    running_board = pty_board(tasks=tasks)

    running_board.send(right)
    running_board.wait_until(lambda: running_board.selected_task_id() == ledger.STATUS_RUNNING)
    running_board.send(down)
    running_board.wait_until(lambda: running_board.selected_task_id() == "task-00")
    running_board.send(up)
    running_board.wait_until(lambda: running_board.selected_task_id() == ledger.STATUS_RUNNING)
    running_board.send(left)

    running_board.wait_until(lambda: running_board.is_focused("Queued"))


@pytest.mark.parametrize("is_color", [True, False])
def test_board_keeps_the_terminal_background(pty_board, is_color):
    running_board = pty_board(is_color=is_color)

    characters = [
        character for line in running_board.screen.buffer.values() for character in line.values()
    ]

    assert all(character.bg == "default" for character in characters)
    assert any(character.bold for character in characters)
    has_foreground_color = any(character.fg != "default" for character in characters)
    assert has_foreground_color == is_color
