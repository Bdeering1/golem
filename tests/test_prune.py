import contextlib
import curses
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from golem import board, cli, ledger, locks
from tests import support

SIBLING_CHILD_ID = "sibling-child"


MERGED_PULL_REQUEST: ledger.PullRequest = {
    "url": "merged",
    "state": ledger.PULL_REQUEST_STATE_MERGED,
}
OPEN_PULL_REQUEST: ledger.PullRequest = {"url": "open", "state": ledger.PULL_REQUEST_STATE_OPEN}


CLOSED_AT = datetime(2030, 3, 9, tzinfo=UTC).isoformat()


def done_task(task_id: str = support.TASK_ID, **fields: object) -> ledger.Task:
    return support.task_record(
        ledger.STATUS_FINISHED, task_id=task_id, closed_at=CLOSED_AT, **fields
    )


def finished_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    ("prune_args", "removed_ids"),
    [
        (["prune"], {ledger.STATUS_CANCELLED, "merged"}),
        (["prune", "--yes"], {ledger.STATUS_CANCELLED, "merged", "closed"}),
    ],
)
def test_prune_without_terminal_asks_for_done_chains_without_finished_prs(
    ledger_path, monkeypatch, capsys, prune_args, removed_ids
):
    tasks = {status: support.task_record(status, task_id=status) for status in ledger.ALL_STATUSES}
    tasks["merged"] = support.task_record(
        ledger.STATUS_FINISHED, task_id="merged", pull_requests=[MERGED_PULL_REQUEST]
    )
    tasks["closed"] = done_task("closed")
    support.write_tasks(ledger_path, tasks)
    monkeypatch.setattr(board, "is_terminal", lambda: False)

    assert cli.main(prune_args) == 0

    assert set(ledger.load_tasks()) == set(tasks) - removed_ids
    assert ("--yes to prune them" in capsys.readouterr().err) is ("closed" not in removed_ids)


@pytest.mark.parametrize(
    ("fields", "is_removed"),
    [
        pytest.param({"pull_requests": [MERGED_PULL_REQUEST]}, True, id="merged-pr"),
        pytest.param({"pull_requests": [OPEN_PULL_REQUEST]}, False, id="open-pr"),
        pytest.param({"finished_at": finished_days_ago(7)}, True, id="no-pr-old"),
    ],
)
def test_prune_yes_deletes_only_done_chains_without_a_picker(
    ledger_path, monkeypatch, fields, is_removed
):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, **fields)
    support.use_fake_terminal(monkeypatch, [])

    assert cli.main(["prune", "--yes"]) == 0

    assert (support.TASK_ID not in ledger.load_tasks()) is is_removed


def test_prune_keeps_done_task_with_attached_pickup(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, pull_requests=[MERGED_PULL_REQUEST])
    pickup_lock_file = locks.acquire_lock(locks.directory_lock_path(str(Path.cwd())))
    assert pickup_lock_file is not None
    try:
        assert cli.main(["prune", "--yes"]) == 0
    finally:
        locks.release_lock(pickup_lock_file)

    assert support.TASK_ID in ledger.load_tasks()


SPACE_KEY = ord(" ")
ALL_KEY = ord("a")
ASKED_IDS = ["first", "second", "third"]


@pytest.mark.parametrize(
    ("keys", "status", "removed_ids"),
    [
        pytest.param(
            [ord("j"), SPACE_KEY, curses.KEY_DOWN, SPACE_KEY, support.ENTER_KEY],
            0,
            {"second", "third"},
            id="select-several",
        ),
        pytest.param([ALL_KEY, support.ENTER_KEY], 0, set(ASKED_IDS), id="all"),
        pytest.param([ALL_KEY, ALL_KEY, support.ENTER_KEY], 0, set(), id="all-cleared"),
        pytest.param([support.ENTER_KEY], 0, set(), id="nothing-selected"),
        pytest.param([ALL_KEY, *support.ESCAPE_KEYS], 1, None, id="escape"),
        pytest.param([ALL_KEY, support.interrupt], 130, None, id="ctrl-c"),
    ],
)
def test_prune_picker_prunes_selected_chains_with_unasked_ones(
    ledger_path, monkeypatch, keys, status, removed_ids
):
    tasks = {task_id: done_task(task_id) for task_id in ASKED_IDS}
    tasks[support.TASK_ID] = support.task_record(ledger.STATUS_CANCELLED)
    support.write_tasks(ledger_path, tasks)
    support.use_fake_terminal(monkeypatch, keys)

    assert cli.main(["prune"]) == status

    expected_removed_ids = set() if removed_ids is None else removed_ids | {support.TASK_ID}
    assert set(ledger.load_tasks()) == set(tasks) - expected_removed_ids


def test_prune_without_usable_terminal_keeps_asked_chains(ledger_path, monkeypatch, capsys):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_CANCELLED),
            support.SIBLING_ID: done_task(support.SIBLING_ID),
        },
    )
    support.use_terminal_failing_to_start(monkeypatch)

    assert cli.main(["prune"]) == 0

    assert set(ledger.load_tasks()) == {support.SIBLING_ID}
    assert "--yes to prune them" in capsys.readouterr().err


def write_chain(ledger_path, statuses):
    task_ids = [support.PARENT_ID, support.CHILD_ID, support.GRANDCHILD_ID][: len(statuses)]
    tasks = {
        task_id: support.task_record(status, task_id=task_id, closed_at=CLOSED_AT)
        for task_id, status in zip(task_ids, statuses, strict=True)
    }
    for parent_id, task_id in itertools.pairwise(task_ids):
        tasks[task_id]["after"] = parent_id
    support.write_tasks(ledger_path, tasks)


@pytest.mark.parametrize(
    ("keys", "remaining_ids"),
    [
        pytest.param([SPACE_KEY, support.ENTER_KEY], set(), id="selected"),
        pytest.param([support.ENTER_KEY], {support.PARENT_ID, support.CHILD_ID}, id="declined"),
    ],
)
def test_prune_offers_each_chain_as_one_row(ledger_path, monkeypatch, keys, remaining_ids):
    write_chain(
        ledger_path, [ledger.STATUS_FINISHED, ledger.STATUS_FINISHED, ledger.STATUS_CANCELLED]
    )
    screen = support.use_fake_terminal(monkeypatch, keys)

    assert cli.main(["prune"]) == 0

    chain_rows = [line for line in screen.frames[0][1:-1] if line.strip()]
    assert len(chain_rows) == 1
    assert f"{support.PARENT_ID}, {support.CHILD_ID}, {support.GRANDCHILD_ID}" in chain_rows[0]
    assert set(ledger.load_tasks()) == remaining_ids


def test_prune_keeps_cancelled_parent_of_declined_task(ledger_path, monkeypatch, capsys):
    write_chain(ledger_path, [ledger.STATUS_CANCELLED, ledger.STATUS_FINISHED])
    support.use_fake_terminal(monkeypatch, [support.ENTER_KEY])

    assert cli.main(["prune"]) == 0

    assert "no longer eligible" not in capsys.readouterr().err
    assert set(ledger.load_tasks()) == {support.PARENT_ID, support.CHILD_ID}


def test_prune_never_offers_review_chain(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_CANCELLED, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.GRANDCHILD_ID: support.task_record(
                ledger.STATUS_CANCELLED, task_id=support.GRANDCHILD_ID, after=support.CHILD_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_FAILED, task_id=support.SIBLING_ID, after=support.PARENT_ID
            ),
        },
    )

    assert cli.main(["prune", "--yes"]) == 0

    assert set(ledger.load_tasks()) == {support.PARENT_ID, support.SIBLING_ID}


def test_prune_releases_worktree_after_its_last_task(owned_worktree):
    worktree = owned_worktree(ledger.STATUS_CANCELLED)

    assert cli.main(["prune", "--yes"]) == 0

    assert support.TASK_ID not in ledger.load_tasks()
    assert worktree.is_dir()
    assert ledger.load_worktrees()[str(worktree.resolve())].get("released_at")


@pytest.mark.parametrize("command", [["delete", support.TASK_ID], ["prune", "--yes"]])
def test_delete_and_prune_leave_in_place_directory_alone(
    installed_launch_agent, git_repository, ledger_path, monkeypatch, command
):
    monkeypatch.chdir(git_repository)
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--no-worktree"])
    cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]])
    tasks = support.read_tasks(ledger_path)
    tasks[support.TASK_ID]["status"] = ledger.STATUS_CANCELLED
    support.write_tasks(ledger_path, tasks)

    assert cli.main(command) == 0

    assert support.TASK_ID not in ledger.load_tasks()
    assert ledger.load_worktrees() == {}


def attach_pickup(ledger_path, exit_stack):
    pickup_lock_file = locks.acquire_lock(locks.directory_lock_path(str(Path.cwd())))
    assert pickup_lock_file is not None
    exit_stack.callback(locks.release_lock, pickup_lock_file)


def add_dependent_task(ledger_path, exit_stack):
    tasks = support.read_tasks(ledger_path)
    tasks[support.CHILD_ID] = support.task_record(
        ledger.STATUS_SCHEDULED, task_id=support.CHILD_ID, after=support.TASK_ID
    )
    support.write_tasks(ledger_path, tasks)


def replace_task(ledger_path, exit_stack):
    tasks = support.read_tasks(ledger_path)
    tasks[support.TASK_ID] = done_task(session_id="replacement-session")
    support.write_tasks(ledger_path, tasks)


@pytest.mark.parametrize("change_task", [attach_pickup, add_dependent_task, replace_task])
def test_prune_keeps_only_task_that_changes_while_prompting(
    ledger_path, monkeypatch, tmp_path, change_task
):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: done_task(),
            support.SIBLING_ID: done_task(support.SIBLING_ID, cwd=str(tmp_path)),
        },
    )

    with contextlib.ExitStack() as exit_stack:
        support.use_fake_terminal(
            monkeypatch,
            [lambda: change_task(ledger_path, exit_stack) or ALL_KEY, support.ENTER_KEY],
        )
        assert cli.main(["prune"]) == 0

    tasks = ledger.load_tasks()
    assert support.TASK_ID in tasks
    assert support.SIBLING_ID not in tasks
