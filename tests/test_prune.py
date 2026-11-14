import contextlib
import io
import itertools
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override

import pytest

from golem import cli, ledger, locks
from tests import support

SIBLING_CHILD_ID = "sibling-child"


class TerminalInput(io.StringIO):
    def __init__(self, answers, on_first_read=lambda: None):
        super().__init__(answers)
        self.on_first_read = on_first_read

    @override
    def isatty(self):
        return True

    @override
    def readline(self, size: int = -1) -> str:
        self.on_first_read()
        self.on_first_read = lambda: None
        return super().readline(size)


MERGED_PULL_REQUEST: ledger.PullRequest = {
    "url": "merged",
    "state": ledger.PULL_REQUEST_STATE_MERGED,
}
CLOSED_PULL_REQUEST: ledger.PullRequest = {
    "url": "closed",
    "state": ledger.PULL_REQUEST_STATE_CLOSED,
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
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    assert cli.main(prune_args) == 0

    assert set(ledger.load_tasks()) == set(tasks) - removed_ids
    assert ("--yes to prune them" in capsys.readouterr().err) is ("closed" not in removed_ids)


@pytest.mark.parametrize(
    ("fields", "is_removed"),
    [
        pytest.param({"pull_requests": [MERGED_PULL_REQUEST]}, True, id="merged-pr"),
        pytest.param({"pull_requests": [CLOSED_PULL_REQUEST]}, True, id="closed-pr"),
        pytest.param({"pull_requests": [OPEN_PULL_REQUEST]}, False, id="open-pr"),
        pytest.param({"finished_at": finished_days_ago(7)}, True, id="no-pr-old"),
        pytest.param({"finished_at": finished_days_ago(1)}, False, id="no-pr-recent"),
    ],
)
def test_prune_deletes_only_done_chains(ledger_path, fields, is_removed):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, **fields)

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


@pytest.mark.parametrize(
    ("answers", "removed_ids"),
    [
        pytest.param("y\nn\ny\n", {"first", "third"}, id="yes-and-no"),
        pytest.param("n\na\n", {"second", "third"}, id="all"),
        pytest.param("y\nq\ny\n", {"first"}, id="quit"),
        pytest.param("y\n", {"first"}, id="end-of-input"),
    ],
)
def test_prune_applies_each_answer_in_turn(ledger_path, monkeypatch, answers, removed_ids):
    task_ids = ["first", "second", "third"]
    support.write_tasks(
        ledger_path,
        {task_id: done_task(task_id) for task_id in task_ids},
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))

    assert cli.main(["prune"]) == 0

    assert set(ledger.load_tasks()) == set(task_ids) - removed_ids


def interrupt():
    raise KeyboardInterrupt


def test_prune_interrupted_at_prompt_deletes_nothing(ledger_path, monkeypatch):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_CANCELLED),
            support.SIBLING_ID: done_task(support.SIBLING_ID),
        },
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n", interrupt))

    assert cli.main(["prune"]) == 130

    assert set(ledger.load_tasks()) == {support.TASK_ID, support.SIBLING_ID}


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
    ("answers", "remaining_ids"),
    [
        pytest.param("y\n", set(), id="yes"),
        pytest.param("n\n", {support.PARENT_ID, support.CHILD_ID}, id="no"),
    ],
)
def test_prune_asks_once_per_chain(ledger_path, monkeypatch, capsys, answers, remaining_ids):
    write_chain(
        ledger_path, [ledger.STATUS_FINISHED, ledger.STATUS_FINISHED, ledger.STATUS_CANCELLED]
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))

    assert cli.main(["prune"]) == 0

    prompt = capsys.readouterr().out
    assert prompt.count("Remove ") == 1
    assert (
        f"{support.PARENT_ID}, {support.CHILD_ID}, {support.GRANDCHILD_ID} (Done, closed)" in prompt
    )
    assert set(ledger.load_tasks()) == remaining_ids


def test_prune_keeps_cancelled_parent_of_declined_task(ledger_path, monkeypatch, capsys):
    write_chain(ledger_path, [ledger.STATUS_CANCELLED, ledger.STATUS_FINISHED])
    monkeypatch.setattr(sys, "stdin", TerminalInput("n\n"))

    assert cli.main(["prune"]) == 0

    assert "no longer eligible" not in capsys.readouterr().err
    assert set(ledger.load_tasks()) == {support.PARENT_ID, support.CHILD_ID}


@pytest.mark.parametrize(
    ("answers", "prompt_count", "remaining_ids"),
    [
        pytest.param("a\n", 1, set(), id="all"),
        pytest.param("y\nq\n", 2, {support.SIBLING_ID, SIBLING_CHILD_ID}, id="quit"),
    ],
)
def test_prune_applies_all_and_quit_per_chain(
    ledger_path, monkeypatch, capsys, answers, prompt_count, remaining_ids
):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: done_task(support.PARENT_ID),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: done_task(support.SIBLING_ID),
            SIBLING_CHILD_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=SIBLING_CHILD_ID, after=support.SIBLING_ID
            ),
            support.TASK_ID: support.task_record(ledger.STATUS_CANCELLED),
        },
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))

    assert cli.main(["prune"]) == 0

    assert capsys.readouterr().out.count("Remove ") == prompt_count
    assert set(ledger.load_tasks()) == remaining_ids


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
        monkeypatch.setattr(
            sys, "stdin", TerminalInput("y\ny\n", lambda: change_task(ledger_path, exit_stack))
        )
        assert cli.main(["prune"]) == 0

    tasks = ledger.load_tasks()
    assert support.TASK_ID in tasks
    assert support.SIBLING_ID not in tasks
