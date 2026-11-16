import io
import sys

import pytest

from golem import ledger, task_lookup
from tests import support

OLDER_ID = "older"
NEWER_ID = "newer"


@pytest.fixture
def labelled_tasks(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            task_id: support.task_record(
                ledger.STATUS_FINISHED, task_id=task_id, label=support.LABEL
            )
            for task_id in (OLDER_ID, NEWER_ID)
        },
    )


def use_terminal(monkeypatch, answers, on_first_read=lambda: None):
    monkeypatch.setattr(sys, "stdin", support.TerminalInput(answers, on_first_read))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def interrupt():
    raise KeyboardInterrupt


def test_exact_task_id_wins_over_a_label(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_FINISHED),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.SIBLING_ID, label=support.TASK_ID
            ),
        },
    )

    assert task_lookup.resolved_task_id(support.TASK_ID) == support.TASK_ID


def test_unique_label_resolves_to_its_task(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, label=support.LABEL)

    assert task_lookup.resolved_task_id(support.LABEL) == support.TASK_ID


def test_unknown_argument_fails_like_an_unknown_id(labelled_tasks):
    with pytest.raises(SystemExit, match="golem: task not found: missing"):
        task_lookup.resolved_task_id("missing")


def test_shared_label_lists_matches_newest_first_and_picks_by_number(
    labelled_tasks, monkeypatch, capsys
):
    use_terminal(monkeypatch, "first\n3\n1\n")

    assert task_lookup.resolved_task_id(support.LABEL) == NEWER_ID
    listed_ids = [line.split()[1] for line in capsys.readouterr().out.splitlines()[:2]]
    assert listed_ids == [NEWER_ID, OLDER_ID]


@pytest.mark.parametrize(
    ("answers", "on_first_read"),
    [("\n", lambda: None), ("", lambda: None), ("1\n", interrupt)],
    ids=["empty", "eof", "interrupt"],
)
def test_picker_cancels(labelled_tasks, monkeypatch, answers, on_first_read):
    use_terminal(monkeypatch, answers, on_first_read)

    with pytest.raises(SystemExit, match="cancelled"):
        task_lookup.resolved_task_id(support.LABEL)


def test_shared_label_without_terminal_lists_matching_ids(labelled_tasks, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    with pytest.raises(SystemExit, match=f"{NEWER_ID}, {OLDER_ID}"):
        task_lookup.resolved_task_id(support.LABEL)


def test_unknown_argument_is_kept_when_requested(labelled_tasks):
    assert task_lookup.resolved_task_id("missing", is_unknown_kept=True) == "missing"
