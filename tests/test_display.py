import os
import re
import shutil
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import override

import pytest

from golem import cli, display, ledger
from tests import support

ANSI_COLOR = re.compile(r"\033\[38;5;\d+m|\033\[0m")
TODAY = date(2030, 3, 10)
OPEN_PULL_REQUEST = {"url": "https://github.com/owner/repository/pull/1", "state": "open"}
MERGED_PULL_REQUEST = {"url": "https://github.com/owner/repository/pull/2", "state": "merged"}
CLOSED_PULL_REQUEST = {"url": "https://github.com/owner/repository/pull/3", "state": "closed"}
SECOND_MERGED_PULL_REQUEST = {
    "url": "https://github.com/owner/repository/pull/4",
    "state": "merged",
}
CLOSED_AT = datetime(2030, 3, 9, tzinfo=UTC).isoformat()


@pytest.fixture
def toronto_time():
    original_timezone = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    yield
    if original_timezone is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_timezone
    time.tzset()


def test_list_rows_order_by_column_then_status_then_id():
    tasks = {
        "z-failed": support.task_record(ledger.STATUS_FAILED, task_id="z-failed"),
        "a-failed": support.task_record(ledger.STATUS_FAILED, task_id="a-failed"),
        "blocked": support.task_record(ledger.STATUS_BLOCKED, task_id="blocked"),
        "scheduled": support.task_record(ledger.STATUS_SCHEDULED, task_id="scheduled"),
        "running": support.task_record(ledger.STATUS_RUNNING, task_id="running"),
        "review": support.task_record(
            ledger.STATUS_FINISHED, task_id="review", finished_at=finished_at(days_ago=0)
        ),
        "parent": support.task_record(ledger.STATUS_FINISHED, task_id="parent"),
        "waiting": support.task_record(ledger.STATUS_WAITING, task_id="waiting", after="parent"),
        "done": support.task_record(ledger.STATUS_FINISHED, task_id="done", closed_at=CLOSED_AT),
        "cancelled": support.task_record(ledger.STATUS_CANCELLED, task_id="cancelled"),
    }

    rows = display.list_rows(tasks, TODAY)

    assert [row.task_id for row in rows] == [
        "blocked",
        "a-failed",
        "z-failed",
        "review",
        "running",
        "scheduled",
        "waiting",
        "done",
        "cancelled",
    ]


def test_list_rows_show_each_chains_latest_task_among_equal_statuses():
    tasks = {
        "lone": support.task_record(ledger.STATUS_FINISHED, task_id="lone"),
        "root": support.task_record(ledger.STATUS_FINISHED, task_id="root"),
        "branch-a": support.task_record(ledger.STATUS_FINISHED, task_id="branch-a", after="root"),
        "branch-b": support.task_record(ledger.STATUS_FINISHED, task_id="branch-b", after="root"),
        "leaf": support.task_record(ledger.STATUS_FINISHED, task_id="leaf", after="branch-a"),
    }

    rows = display.list_rows(tasks, TODAY)

    assert [(row.task_id, row.step) for row in rows] == [("leaf", "3/3"), ("lone", "")]


@pytest.mark.parametrize(
    ("dependent_status", "parent_fields", "expected"),
    [
        pytest.param(
            ledger.STATUS_RUNNING, {}, ("dependent", "2/2", ledger.STATUS_RUNNING), id="running"
        ),
        pytest.param(
            ledger.STATUS_WAITING,
            {"gate_stop_reason": "tests failed", "status": ledger.STATUS_BLOCKED},
            ("parent", "1/2", ledger.STATUS_BLOCKED),
            id="blocked",
        ),
    ],
)
def test_list_rows_show_the_step_earliest_in_status_order(
    dependent_status, parent_fields, expected
):
    tasks = {
        "parent": support.task_record(ledger.STATUS_FINISHED, task_id="parent") | parent_fields,
        "dependent": support.task_record(dependent_status, task_id="dependent", after="parent"),
    }

    rows = display.list_rows(tasks, TODAY)

    assert [(row.task_id, row.step, row.status) for row in rows] == [expected]


@pytest.mark.parametrize(
    ("status", "fields", "detail"),
    [
        pytest.param(
            ledger.STATUS_SCHEDULED,
            {"due_at": datetime(2030, 3, 11, 13, tzinfo=UTC).isoformat()},
            "tomorrow 9:00am",
            id="scheduled-due-time",
        ),
        pytest.param(
            ledger.STATUS_RUNNING,
            {"started_at": datetime(2030, 3, 10, 13, tzinfo=UTC).isoformat()},
            "today 9:00am",
            id="running-start-time",
        ),
        pytest.param(ledger.STATUS_WAITING, {"after": "missing"}, "missing", id="waiting-after"),
        pytest.param(
            ledger.STATUS_BLOCKED,
            {"gate_stop_reason": "tests failed\ndetails"},
            "tests failed",
            id="gate-reason",
        ),
        pytest.param(
            ledger.STATUS_WAITING,
            {"after": "missing", "setup_failure_reason": "setup failed"},
            "missing; setup failed",
            id="status-value-and-reason",
        ),
        pytest.param(
            ledger.STATUS_FINISHED, {"pull_requests": [OPEN_PULL_REQUEST]}, "PR open", id="one-pr"
        ),
        pytest.param(
            ledger.STATUS_FINISHED,
            {"pull_requests": [MERGED_PULL_REQUEST, SECOND_MERGED_PULL_REQUEST]},
            "2 PRs merged",
            id="prs-same-state",
        ),
        pytest.param(
            ledger.STATUS_FINISHED,
            {"pull_requests": [MERGED_PULL_REQUEST, OPEN_PULL_REQUEST]},
            "2 PRs: 1 open, 1 merged",
            id="prs-different-states",
        ),
    ],
)
def test_list_rows_detail(toronto_time, status, fields, detail):
    tasks = {"task": support.task_record(status, task_id="task", **fields)}

    assert display.list_rows(tasks, TODAY)[0].detail == detail


def test_list_abbreviates_only_home_and_its_descendants(ledger_path, capsys):
    home_directory = Path.home()
    prefix_directory = str(home_directory.with_name(f"{home_directory.name}-other"))
    tasks = {
        "home": support.task_record(
            ledger.STATUS_FINISHED, task_id="home", cwd=str(home_directory)
        ),
        "descendant": support.task_record(
            ledger.STATUS_FINISHED, task_id="descendant", cwd=str(home_directory / "project")
        ),
        "prefix": support.task_record(
            ledger.STATUS_FINISHED, task_id="prefix", cwd=prefix_directory
        ),
    }
    support.write_tasks(ledger_path, tasks)

    output = support.list_output(capsys)

    assert "  ~" in support.list_row(output, "home")
    assert "  ~/project" in support.list_row(output, "descendant")
    assert prefix_directory in support.list_row(output, "prefix")


def test_list_formats_times_without_zone(ledger_path, capsys, monkeypatch, toronto_time):
    class FixedDateTime(datetime):
        @classmethod
        @override
        def now(cls, tz=None) -> datetime:
            now = datetime(2030, 3, 10, 12, tzinfo=UTC)
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(display, "datetime", FixedDateTime)
    tasks = {
        "today": support.task_record(
            ledger.STATUS_SCHEDULED,
            task_id="today",
            due_at=datetime(2030, 3, 11, 3, 30, tzinfo=UTC).isoformat(),
        ),
        "this-year": support.task_record(
            ledger.STATUS_SCHEDULED,
            task_id="this-year",
            due_at=datetime(2030, 11, 3, 5, 30, tzinfo=UTC).isoformat(),
        ),
        "next-year": support.task_record(
            ledger.STATUS_SCHEDULED,
            task_id="next-year",
            due_at=datetime(2031, 11, 3, 6, 30, tzinfo=UTC).isoformat(),
        ),
    }
    support.write_tasks(ledger_path, tasks)

    output = support.list_output(capsys)

    assert support.list_row(output, "today").endswith("today 11:30pm")
    assert support.list_row(output, "this-year").endswith("Nov 3 1:30am")
    assert support.list_row(output, "next-year").endswith("Nov 3, 2031 1:30am")


@pytest.mark.parametrize(
    ("is_tty", "terminal_width", "is_narrow_layout"),
    [
        pytest.param(True, 20, True, id="terminal-overflow"),
        pytest.param(True, 500, False, id="terminal-fits"),
        pytest.param(False, 20, False, id="not-a-terminal"),
    ],
)
def test_list_shortens_directories_and_moves_detail_only_when_terminal_overflows(
    ledger_path, capsys, monkeypatch, is_tty, terminal_width, is_narrow_layout
):
    home_directory = Path.home()
    tasks = {
        "home": support.task_record(ledger.STATUS_FAILED, task_id="home", cwd=str(home_directory)),
        "nested": support.task_record(
            ledger.STATUS_BLOCKED,
            task_id="nested",
            cwd=str(home_directory / "project" / "leaf"),
            gate_stop_reason="gate",
        ),
    }
    support.write_tasks(ledger_path, tasks)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((terminal_width, 24)))

    output = support.list_output(capsys)

    lines = ANSI_COLOR.sub("", output).splitlines()
    nested_index = next(index for index, line in enumerate(lines) if line.startswith("nested "))
    if is_narrow_layout:
        assert lines[nested_index].endswith("  .../leaf")
        assert lines[nested_index + 1] == f"{display.DETAIL_LINE_INDENT}gate"
    else:
        assert lines[nested_index].endswith("  ~/project/leaf  gate")
    assert support.list_row(output, "home").endswith("  ~")


def test_list_uses_distinct_status_colors_and_keeps_columns_aligned(
    ledger_path, capsys, monkeypatch
):
    tasks = {
        status: support.task_record(status, task_id=status)
        for status in ledger.LIST_STATUS_ORDER
        if status != ledger.STATUS_WAITING
    }
    tasks[ledger.STATUS_WAITING] = support.task_record(
        ledger.STATUS_WAITING, task_id=ledger.STATUS_WAITING
    )
    support.write_tasks(ledger_path, tasks)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((500, 24)))

    colored_output = support.list_output(capsys)

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    plain_output = support.list_output(capsys)
    colors = re.findall(r"\033\[38;5;(\d+)m", colored_output)

    assert len(colors) == len(ledger.LIST_STATUS_ORDER)
    assert len(set(colors)) == len(ledger.LIST_STATUS_ORDER)
    assert ANSI_COLOR.sub("", colored_output) == plain_output


def test_list_shows_label_column_with_the_chain_label(ledger_path, capsys):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID, label=support.LABEL
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_RUNNING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_RUNNING, task_id=support.SIBLING_ID
            ),
        },
    )

    header, *rows = support.list_output(capsys).splitlines()

    assert header.split()[:2] == ["ID", "LABEL"]
    assert [row.split()[:2] for row in rows] == [
        [support.CHILD_ID, support.LABEL],
        [support.SIBLING_ID, ledger.STATUS_RUNNING],
    ]


@pytest.mark.parametrize(
    ("is_tty", "term", "no_color"),
    [
        pytest.param(False, "xterm-256color", None, id="redirected"),
        pytest.param(True, "dumb", None, id="dumb-terminal"),
        pytest.param(True, "xterm-256color", "", id="no-color"),
    ],
)
def test_list_suppresses_status_colors(ledger_path, capsys, monkeypatch, is_tty, term, no_color):
    support.write_task(ledger_path, ledger.STATUS_FINISHED)
    monkeypatch.setenv("TERM", term)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)

    output = support.list_output(capsys)

    assert ANSI_COLOR.search(output) is None


@pytest.mark.parametrize(
    ("list_args", "listed_ids", "footnote"),
    [
        pytest.param(
            ["list"],
            ["none", "open", "partial-child"],
            "2 done hidden; --all shows them",
            id="default",
        ),
        pytest.param(
            ["list", "--all"],
            ["none", "open", "partial-child", "finished-child", "merged"],
            None,
            id="all",
        ),
    ],
)
def test_list_hides_done_chains(ledger_path, capsys, list_args, listed_ids, footnote):
    def finished_task(task_id: str, **fields: object) -> ledger.Task:
        return support.task_record(ledger.STATUS_FINISHED, task_id=task_id, **fields)

    support.write_tasks(
        ledger_path,
        {
            "merged": finished_task("merged", pull_requests=[MERGED_PULL_REQUEST]),
            "open": finished_task("open", pull_requests=[MERGED_PULL_REQUEST, OPEN_PULL_REQUEST]),
            "none": finished_task("none", pull_requests=[]),
            "finished-parent": finished_task(
                "finished-parent", pull_requests=[MERGED_PULL_REQUEST]
            ),
            "finished-child": finished_task(
                "finished-child", after="finished-parent", pull_requests=[CLOSED_PULL_REQUEST]
            ),
            "partial-parent": finished_task("partial-parent", pull_requests=[MERGED_PULL_REQUEST]),
            "partial-child": finished_task(
                "partial-child", after="partial-parent", pull_requests=[OPEN_PULL_REQUEST]
            ),
        },
    )

    assert cli.main(list_args) == 0

    table, *footnotes = capsys.readouterr().out.split("\n\n")
    assert [line.split()[0] for line in table.splitlines()[1:]] == listed_ids
    assert footnotes == ([f"{footnote}\n"] if footnote is not None else [])


def test_list_shows_only_hidden_footnote_when_every_row_is_hidden(ledger_path, capsys):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, pull_requests=[MERGED_PULL_REQUEST])

    assert support.list_output(capsys) == "1 done hidden; --all shows them\n"


def finished_step(task_id: str, **fields: object) -> ledger.Task:
    default_fields: dict[str, object] = {"finished_at": finished_at(days_ago=1)}
    return support.task_record(ledger.STATUS_FINISHED, task_id=task_id, **(default_fields | fields))


def finished_at(*, days_ago: int) -> str:
    return datetime(2030, 3, 10 - days_ago, 12, tzinfo=UTC).isoformat()


@pytest.mark.parametrize(
    ("child_fields", "column", "reason"),
    [
        pytest.param({"closed_at": CLOSED_AT}, display.COLUMN_DONE, "closed", id="closed"),
        pytest.param(
            {"pull_requests": [MERGED_PULL_REQUEST, CLOSED_PULL_REQUEST]},
            display.COLUMN_DONE,
            "2 PRs: 1 merged, 1 closed",
            id="prs-merged-or-closed",
        ),
        pytest.param(
            {
                "pull_requests": [OPEN_PULL_REQUEST],
                "t3_code_thread_id": "thread",
                "t3_code_thread_state": ledger.THREAD_STATE_SETTLED,
                "finished_at": finished_at(days_ago=9),
            },
            display.COLUMN_REVIEW,
            "PR open",
            id="pr-ignores-thread-and-age",
        ),
        *(
            pytest.param(
                {"t3_code_thread_id": "thread", "t3_code_thread_state": state},
                display.COLUMN_DONE,
                f"thread {state}",
                id=f"thread-{state}",
            )
            for state in (
                ledger.THREAD_STATE_SETTLED,
                ledger.THREAD_STATE_ARCHIVED,
                ledger.THREAD_STATE_DELETED,
            )
        ),
        pytest.param(
            {"t3_code_thread_id": "thread", "t3_code_thread_state": ledger.THREAD_STATE_UNSETTLED},
            display.COLUMN_REVIEW,
            "thread unsettled",
            id="thread-unsettled",
        ),
        pytest.param(
            {"finished_at": finished_at(days_ago=7)},
            display.COLUMN_DONE,
            "no PR; finished 7d ago",
            id="age",
        ),
        pytest.param(
            {"finished_at": finished_at(days_ago=6)},
            display.COLUMN_REVIEW,
            "no PR; finished 6d ago",
            id="young",
        ),
    ],
)
def test_finished_chain_column_and_reason(child_fields, column, reason):
    tasks = {
        "parent": finished_step("parent", finished_at=finished_at(days_ago=8)),
        "child": finished_step("child", after="parent") | child_fields,
    }

    row = display.list_rows(tasks, TODAY)[0]

    assert (row.status, row.column, row.detail) == (ledger.STATUS_FINISHED, column, reason)


def test_finished_chain_counts_pull_request_shared_by_steps_once():
    tasks = {
        "parent": finished_step("parent", pull_requests=[OPEN_PULL_REQUEST]),
        "child": finished_step("child", after="parent", pull_requests=[OPEN_PULL_REQUEST]),
    }

    assert display.list_rows(tasks, TODAY)[0].detail == "PR open"
