import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import override

import pytest

from golem import display, ledger
from tests import support

ANSI_COLOR = re.compile(r"\033\[38;5;\d+m|\033\[0m")


def test_list_groups_visible_tasks_by_status_and_id(ledger_path, capsys):
    tasks = {
        "z-failed": support.task_record(ledger.STATUS_FAILED, task_id="z-failed"),
        "a-failed": support.task_record(ledger.STATUS_FAILED, task_id="a-failed"),
        "scheduled": support.task_record(ledger.STATUS_SCHEDULED, task_id="scheduled"),
        "running": support.task_record(ledger.STATUS_RUNNING, task_id="running"),
        "review": support.task_record(ledger.STATUS_NEEDS_REVIEW, task_id="review"),
        "parent": support.task_record(ledger.STATUS_NEEDS_REVIEW, task_id="parent"),
        "waiting": support.task_record(ledger.STATUS_WAITING, task_id="waiting", after="parent"),
        "done": support.task_record(ledger.STATUS_DONE, task_id="done"),
        "cancelled": support.task_record(ledger.STATUS_CANCELLED, task_id="cancelled"),
    }
    support.write_tasks(ledger_path, tasks)

    output = support.list_output(capsys)

    assert [line.split()[0] for line in output.splitlines()[1:]] == [
        "parent",
        "review",
        "a-failed",
        "z-failed",
        "running",
        "scheduled",
        "waiting",
        "done",
        "cancelled",
    ]
    assert support.list_row(output, "waiting").split()[-1] == "parent"


def test_list_abbreviates_only_home_and_its_descendants(ledger_path, capsys):
    home_directory = Path.home()
    prefix_directory = str(home_directory.with_name(f"{home_directory.name}-other"))
    tasks = {
        "home": support.task_record(ledger.STATUS_DONE, task_id="home", cwd=str(home_directory)),
        "descendant": support.task_record(
            ledger.STATUS_DONE, task_id="descendant", cwd=str(home_directory / "project")
        ),
        "prefix": support.task_record(ledger.STATUS_DONE, task_id="prefix", cwd=prefix_directory),
    }
    support.write_tasks(ledger_path, tasks)

    output = support.list_output(capsys)

    assert "  ~" in support.list_row(output, "home")
    assert "  ~/project" in support.list_row(output, "descendant")
    assert prefix_directory in support.list_row(output, "prefix")


def test_list_formats_due_times_in_local_time(ledger_path, capsys, monkeypatch):
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
        "tomorrow": support.task_record(
            ledger.STATUS_SCHEDULED,
            task_id="tomorrow",
            due_at=datetime(2030, 3, 11, 13, tzinfo=UTC).isoformat(),
        ),
        "daylight": support.task_record(
            ledger.STATUS_SCHEDULED,
            task_id="daylight",
            due_at=datetime(2030, 11, 3, 5, 30, tzinfo=UTC).isoformat(),
        ),
        "standard": support.task_record(
            ledger.STATUS_SCHEDULED,
            task_id="standard",
            due_at=datetime(2030, 11, 3, 6, 30, tzinfo=UTC).isoformat(),
        ),
        "no-due": support.task_record(ledger.STATUS_SCHEDULED, task_id="no-due"),
    }
    tasks["no-due"].pop("due_at")
    support.write_tasks(ledger_path, tasks)

    original_timezone = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    try:
        output = support.list_output(capsys)
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert "today 11:30pm EDT" in support.list_row(output, "today")
    assert "tomorrow 9:00am EDT" in support.list_row(output, "tomorrow")
    assert "Nov 3, 2030 1:30am EDT" in support.list_row(output, "daylight")
    assert "Nov 3, 2030 1:30am EST" in support.list_row(output, "standard")
    assert support.list_row(output, "no-due").endswith(str(Path.cwd()))


def test_list_uses_distinct_status_colors_and_keeps_columns_aligned(
    ledger_path, capsys, monkeypatch
):
    tasks = {
        status: support.task_record(status, task_id=status)
        for status in ledger.LIST_STATUS_ORDER
        if status != ledger.STATUS_WAITING
    }
    tasks[ledger.STATUS_WAITING] = support.task_record(
        ledger.STATUS_WAITING,
        task_id=ledger.STATUS_WAITING,
        after=ledger.STATUS_NEEDS_REVIEW,
    )
    support.write_tasks(ledger_path, tasks)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    colored_output = support.list_output(capsys)

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    plain_output = support.list_output(capsys)
    colors = re.findall(r"\033\[38;5;(\d+)m", colored_output)

    assert len(colors) == len(ledger.LIST_STATUS_ORDER)
    assert len(set(colors)) == len(ledger.LIST_STATUS_ORDER)
    assert ANSI_COLOR.sub("", colored_output) == plain_output


@pytest.mark.parametrize(
    ("is_tty", "term", "no_color"),
    [
        pytest.param(False, "xterm-256color", None, id="redirected"),
        pytest.param(True, "dumb", None, id="dumb-terminal"),
        pytest.param(True, "xterm-256color", "", id="no-color"),
    ],
)
def test_list_suppresses_status_colors(ledger_path, capsys, monkeypatch, is_tty, term, no_color):
    support.write_task(ledger_path, ledger.STATUS_DONE)
    monkeypatch.setenv("TERM", term)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)

    output = support.list_output(capsys)

    assert ANSI_COLOR.search(output) is None
