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


def use_terminal(monkeypatch, answers):
    monkeypatch.setattr(sys, "stdin", support.TerminalInput(answers))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


@pytest.mark.parametrize(
    "argument", [support.TASK_ID, support.LABEL], ids=["exact-id-wins", "unique-label"]
)
def test_argument_resolves_to_one_task(ledger_path, argument):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, label=support.LABEL),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.SIBLING_ID, label=support.TASK_ID
            ),
        },
    )

    assert task_lookup.resolved_task_id(argument) == support.TASK_ID


def test_shared_label_lists_matches_newest_first_and_picks_by_number(
    labelled_tasks, monkeypatch, capsys
):
    use_terminal(monkeypatch, "first\n3\n1\n")

    assert task_lookup.resolved_task_id(support.LABEL) == NEWER_ID
    newer_line, older_line = capsys.readouterr().out.splitlines()[:2]
    assert newer_line.split()[:4] == ["1)", NEWER_ID, support.LABEL, ledger.STATUS_FINISHED]
    assert newer_line.endswith(support.PROMPT)
    assert older_line.split()[1] == OLDER_ID


@pytest.mark.parametrize("answers", ["\n", ""], ids=["empty", "eof"])
def test_picker_cancels(labelled_tasks, monkeypatch, answers):
    use_terminal(monkeypatch, answers)

    with pytest.raises(SystemExit, match="cancelled"):
        task_lookup.resolved_task_id(support.LABEL)


def test_shared_label_without_terminal_lists_matching_ids(labelled_tasks, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    with pytest.raises(SystemExit, match=f"{NEWER_ID}, {OLDER_ID}"):
        task_lookup.resolved_task_id(support.LABEL)
