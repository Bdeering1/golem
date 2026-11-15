import curses
import locale
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pexpect
import pyte
import pytest

from golem import board, cli, display, ledger, progress_log
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
XTERM_REP_TERMINFO_LINE = "\trep=%p1%c\\E[%p2%{1}%-%db,\n"
REPO_ROOT = Path(__file__).parent.parent
ARROW_KEYS = ("\x1bOD", "\x1bOC", "\x1bOA", "\x1bOB")
LETTER_KEYS = ("h", "l", "k", "j")
MERGED_PULL_REQUEST = {"url": "https://github.com/owner/repository/pull/2", "state": "merged"}


def frame_lines(tasks, width, height, state, glyphs=board.UNICODE_GLYPHS):
    snapshot = board.board_snapshot(tasks, NOW)
    lines = [[" "] * width for _ in range(height)]
    board.fit_state_to_layout(snapshot, width, height, state)
    for segment in board.board_frame(snapshot, width, height, state, glyphs):
        for offset, character in enumerate(segment.text):
            lines[segment.y][segment.x + offset] = character
    return ["".join(line).rstrip() for line in lines]


def selected_text(tasks, state, width=WIDE_WIDTH, height=HEIGHT):
    snapshot = board.board_snapshot(tasks, NOW)
    board.fit_state_to_layout(snapshot, width, height, state)
    segments = board.board_frame(snapshot, width, height, state)
    return "".join(
        segment.text
        for segment in segments
        if segment.background == board.SELECTION_BACKGROUND and segment.text.strip()
    )


def board_and_detail_lines(tasks, width=WIDE_WIDTH, height=HEIGHT, state=None):
    state = state or board.BoardState()
    lines = frame_lines(tasks, width, height, state)
    rule_y = max(y for y, line in enumerate(lines) if line.startswith(("─ ", "- ")))
    return lines[:rule_y], lines[rule_y:-1]


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
    ("pressed_keys", "done_columns"), [("", []), ("a", [("Done", {"merged"})])]
)
def test_board_columns_group_statuses_and_a_toggles_done(pressed_keys, done_columns):
    tasks = statuses_tasks() | {
        "merged": support.task_record(
            ledger.STATUS_FINISHED, task_id="merged", pull_requests=[MERGED_PULL_REQUEST]
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
        ("Review", {ledger.STATUS_BLOCKED, ledger.STATUS_FAILED, ledger.STATUS_FINISHED}),
        *done_columns,
    ]
    assert [
        (board_column.title, {row.task_id for row in column_rows})
        for board_column, column_rows in columns
    ] == expected


def test_board_shows_a_chain_as_one_card_with_its_step_and_list_detail():
    tasks = {
        "root": support.task_record(
            ledger.STATUS_BLOCKED, task_id="root", gate_stop_reason="no pull request yet"
        ),
        "child": support.task_record(ledger.STATUS_WAITING, task_id="child", after="root"),
    }

    board_lines, _ = board_and_detail_lines(tasks, state=board.BoardState(focus_index=2))
    board_text = "\n".join(board_lines)

    assert "1/2" in board_text
    assert "child" not in board_text
    assert "no pull request yet" in board_text


def test_board_shortens_card_text_and_shows_it_in_full_in_the_detail_pane():
    task_id = "a-very-long-task-identifier-that-is-shortened"
    directory = "/somewhere/deeply/nested/project-leaf"
    tasks = {
        task_id: support.task_record(
            ledger.STATUS_RUNNING, task_id=task_id, cwd=directory, prompt="Polish the board\nmore"
        )
    }

    board_lines, detail_lines = board_and_detail_lines(tasks, state=board.BoardState(focus_index=1))

    board_text = "\n".join(board_lines)
    detail_text = "\n".join(detail_lines)
    assert f"{task_id[:10]}" in board_text
    assert task_id not in board_text
    assert task_id in detail_lines[0]
    assert directory in detail_text
    assert "Polish the board" in detail_text


def test_board_detail_pane_keeps_the_end_of_a_long_directory():
    tasks = {
        "task": support.task_record(ledger.STATUS_RUNNING, cwd="/somewhere/deeply/nested/leaf")
    }

    _, detail_lines = board_and_detail_lines(
        tasks, NARROW_WIDTH, state=board.BoardState(focus_index=1)
    )

    assert "nested/leaf" in "".join(detail_lines)


@pytest.mark.parametrize(
    ("width", "shown_titles", "has_tabs"),
    [
        pytest.param(WIDE_WIDTH, ["Queued", "Running", "Review"], False, id="all-fit"),
        pytest.param(46, ["Queued", "Running"], True, id="two-fit"),
        pytest.param(NARROW_WIDTH, ["Queued"], True, id="one-fits"),
    ],
)
def test_board_shows_as_many_columns_as_fit(width, shown_titles, has_tabs):
    lines = frame_lines(statuses_tasks(), width, HEIGHT, board.BoardState())

    header_line = lines[board.board_layout(width, HEIGHT, (0, 0, 0)).header_y]
    assert [
        title for title, *_ in board.BOARD_COLUMNS if f"▌ {title}" in header_line
    ] == shown_titles[:1]
    assert all(title in header_line for title in shown_titles)
    assert (board.UNICODE_GLYPHS.right in header_line) == has_tabs


@pytest.mark.parametrize(
    ("width", "focus_index", "left_text", "right_text"),
    [
        pytest.param(46, 2, "←   Running 1", "→", id="both-sides"),
        pytest.param(40, 1, "← Queued 2  ▌ Running 1", "Review 3  D 0 →", id="shortens-farthest"),
        pytest.param(28, 3, "← +2  Review 3", "▌ Done 0", id="folds-left"),
        pytest.param(28, 0, "▌ Queued 2", "Running 1  +2 →", id="right-only"),
    ],
)
def test_board_lists_hidden_columns_on_the_side_they_scroll_in_from(
    width, focus_index, left_text, right_text
):
    state = board.BoardState(focus_index=focus_index, is_showing_all=True)
    lines = frame_lines(statuses_tasks(), width, HEIGHT, state)

    tab_line = lines[board.board_layout(width, HEIGHT, (0, 0, 0, 0)).header_y]
    assert tab_line.startswith(left_text)
    assert tab_line.endswith(right_text)


@pytest.mark.parametrize("width", [WIDE_WIDTH, 40, NARROW_WIDTH, 12])
def test_board_footer_shortens_to_fit_and_keeps_help_and_quit(width):
    footer = frame_lines(statuses_tasks(), width, HEIGHT, board.BoardState())[-1]

    assert len(footer) < width
    assert {"?", "q"} <= set(footer.split())


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(30, "<1m"), (7 * 60, "7m"), (3 * 3600 + 600, "3h 10m"), (2 * 86400 + 3600, "2d 1h")],
)
def test_board_durations_read_at_a_glance(seconds, text):
    assert board.duration_text(seconds) == text


def test_board_running_card_shows_the_latest_agent_activity(ledger_path):
    tasks = {"task": support.task_record(ledger.STATUS_RUNNING, started_at=NOW.isoformat())}
    progress_path = progress_log.progress_path(tasks["task"]["session_id"])
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text("Reading the auth module\n[tool] Edit app/auth/reset.py\n")

    text = frame_text(tasks, state=board.BoardState(focus_index=1))

    assert "Edit app/auth/reset.py" in text


def test_board_selection_moves_within_a_column_and_scrolls_to_stay_visible():
    state = board.BoardState(focus_index=1)

    for _ in range(100):
        board.apply_key(state, ord("j"))
    bottom_selection = selected_text(running_tasks(), state)
    board.apply_key(state, ord("k"))
    scrolled_up_selection = selected_text(running_tasks(), state)

    assert "task-09" in bottom_selection
    assert "task-08" in scrolled_up_selection


def test_board_selection_stays_on_its_task_when_a_task_is_added_above_it():
    tasks = running_tasks()
    added_task = tasks.pop("task-00")
    state = board.BoardState(focus_index=1)
    board.apply_key(state, ord("j"))
    frame_text(tasks, state=state)

    assert "task-02" in selected_text(tasks | {"task-00": added_task}, state)


def test_board_returns_focus_from_done_when_it_hides():
    state = board.BoardState(focus_index=1)

    board.apply_key(state, ord("a"))
    board.apply_key(state, ord("a"))
    frame_text({}, state=state)

    assert state.focus_index == 1


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
    def __init__(self, *, is_color=True, environment=None):
        environment = dict(os.environ, TERM="xterm-256color") | (environment or {})
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
        rule_line = next(
            line for line in reversed(self.screen.display) if line.startswith(("─ ", "- "))
        )
        return rule_line.split()[1]

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

    def start(*, is_color=True, tasks=None, environment=None) -> PtyBoard:
        support.write_tasks(ledger_path, tasks or statuses_tasks())
        pty_board = PtyBoard(is_color=is_color, environment=environment)
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
    running_board.wait_until(lambda: "queued" not in running_board.screen.display[0])
    running_board.send("lll")

    running_board.wait_until(lambda: running_board.is_focused("Review"))


@pytest.mark.parametrize("keys", [ARROW_KEYS, LETTER_KEYS], ids=["arrows", "letters"])
def test_board_keys_switch_columns_and_select_tasks(pty_board, keys):
    left, right, up, down = keys
    tasks = statuses_tasks() | running_tasks()
    running_board = pty_board(tasks=tasks)

    running_board.send(left)
    running_board.wait_until(lambda: running_board.selected_task_id() == ledger.STATUS_RUNNING)
    running_board.send(down)
    running_board.wait_until(lambda: running_board.selected_task_id() == "task-00")
    running_board.send(up)
    running_board.wait_until(lambda: running_board.selected_task_id() == ledger.STATUS_RUNNING)
    running_board.send(right)

    running_board.wait_until(lambda: running_board.is_focused("Review"))


@pytest.mark.parametrize("is_color", [True, False])
def test_board_paints_no_background_beyond_the_selection(pty_board, is_color):
    running_board = pty_board(is_color=is_color)

    characters = [
        character for line in running_board.screen.buffer.values() for character in line.values()
    ]

    highlighted_count = sum(character.bg != "default" for character in characters)
    assert highlighted_count <= 2 * PTY_WIDTH
    assert any(character.bold for character in characters)
    has_foreground_color = any(character.fg != "default" for character in characters)
    assert has_foreground_color == is_color


def test_board_keeps_unicode_rules_when_the_terminal_declares_rep(pty_board, tmp_path):
    source = subprocess.run(
        ["/usr/bin/infocmp", "-x", "-1", "xterm-256color"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    name_line, *capabilities = source.splitlines(keepends=True)[1:]
    (tmp_path / "xterm-rep.terminfo").write_text(
        name_line + XTERM_REP_TERMINFO_LINE + "".join(capabilities)
    )
    subprocess.run(
        ["/usr/bin/tic", "-x", "-o", ".", "xterm-rep.terminfo"], cwd=tmp_path, check=True
    )

    running_board = pty_board(environment={"TERMINFO": str(tmp_path)})

    assert any(line.startswith("──") for line in running_board.screen.display)


def test_board_closes_a_finished_chain_only_after_confirmation(pty_board, ledger_path):
    tasks = {"done": support.task_record(ledger.STATUS_FINISHED, task_id="done")}
    running_board = pty_board(tasks=tasks)

    running_board.send("3c")
    running_board.wait_for_text("Close done?")
    running_board.send("y")

    running_board.wait_until(lambda: "closed_at" in ledger.load_tasks()["done"])


def test_board_opens_a_task_and_returns_to_the_board(pty_board):
    running_board = pty_board(tasks={"task": support.task_record(ledger.STATUS_RUNNING)})

    running_board.send("2\r")
    running_board.wait_for_text("Backend")
    running_board.send("\x1b")

    running_board.wait_for_text("q quit")


def test_board_filter_narrows_cards_and_escape_clears_it():
    snapshot = board.board_snapshot(running_tasks(), NOW)
    state = board.BoardState(focus_index=1)

    for key in "/TASK-03\r":
        board.apply_key(state, ord(key), snapshot)
    filtered_ids = [row.task_id for row in board.filtered_snapshot(snapshot, state).rows]
    board.apply_key(state, 27, snapshot)

    assert filtered_ids == ["task-03"]
    assert board.filtered_snapshot(snapshot, state).rows == snapshot.rows


@pytest.mark.parametrize(
    ("statuses", "focused_title"),
    [
        ((ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING, ledger.STATUS_BLOCKED), "Review"),
        ((ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING), "Running"),
        ((ledger.STATUS_SCHEDULED,), "Queued"),
    ],
)
def test_board_starts_on_the_column_that_needs_attention(statuses, focused_title):
    tasks = {status: support.task_record(status, task_id=status) for status in statuses}
    rows = board.board_snapshot(tasks, NOW).rows

    column_index = board.attention_column_index(rows)

    assert board.BOARD_COLUMNS[column_index].title == focused_title


def test_board_outlines_cards_that_fit_and_packs_crowded_columns():
    layout = board.board_layout(120, HEIGHT, (2, 20, 0))

    assert layout.card_styles[0].is_boxed
    assert layout.card_styles[1] == board.DENSE_CARD
    assert layout.visible_card_count(1, 20) < 20


@pytest.mark.parametrize(
    ("narrow_keys", "wide_width"), [("", WIDE_WIDTH), ("aa", NARROW_WIDTH), ("aa", WIDE_WIDTH)]
)
def test_board_redraws_after_its_columns_scroll_back_into_view(narrow_keys, wide_width):
    state = board.BoardState(focus_index=2)
    frame_lines(statuses_tasks(), NARROW_WIDTH, HEIGHT, state)
    for key in narrow_keys:
        board.apply_key(state, ord(key))
        frame_lines(
            statuses_tasks(), NARROW_WIDTH if key == narrow_keys[0] else wide_width, HEIGHT, state
        )

    lines = frame_lines(statuses_tasks(), wide_width, HEIGHT, state)

    assert "Review" in "\n".join(lines)
