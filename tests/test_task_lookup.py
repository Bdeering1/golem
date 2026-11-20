import pytest

from golem import board, ledger, task_lookup
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


def test_shared_label_lists_newest_first_and_picks_highlighted_task(labelled_tasks, monkeypatch):
    screen = support.use_fake_terminal(monkeypatch, [ord("j"), support.ENTER_KEY], size=(24, 200))

    assert task_lookup.resolved_task_id(support.LABEL) == OLDER_ID
    newer_line, older_line = screen.frames[0][1:3]
    assert newer_line.split()[:3] == [NEWER_ID, support.LABEL, ledger.STATUS_FINISHED]
    assert newer_line.rstrip().endswith(support.PROMPT)
    assert older_line.split()[0] == OLDER_ID


@pytest.mark.parametrize(
    ("keys", "exit_code"),
    [
        pytest.param([ord("q")], "golem: cancelled", id="q"),
        pytest.param([support.interrupt], board.INTERRUPTED_EXIT_STATUS, id="ctrl-c"),
    ],
)
def test_label_picker_cancels(labelled_tasks, monkeypatch, keys, exit_code):
    support.use_fake_terminal(monkeypatch, keys)

    with pytest.raises(SystemExit) as raised:
        task_lookup.resolved_task_id(support.LABEL)

    assert raised.value.code == exit_code


@pytest.mark.parametrize(
    "use_unusable_terminal",
    [
        lambda monkeypatch: monkeypatch.setattr(board, "is_terminal", lambda: False),
        support.use_terminal_failing_to_start,
    ],
    ids=["no-terminal", "curses-fails"],
)
def test_shared_label_without_usable_terminal_lists_matching_ids(
    labelled_tasks, monkeypatch, use_unusable_terminal
):
    use_unusable_terminal(monkeypatch)

    with pytest.raises(SystemExit, match=f"{NEWER_ID}, {OLDER_ID}"):
        task_lookup.resolved_task_id(support.LABEL)
