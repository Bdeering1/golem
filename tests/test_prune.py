import contextlib
import io
import itertools
import os
import sys
from pathlib import Path
from typing import override

import pytest

from golem import cli, ledger, locks, prune
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


@pytest.mark.parametrize(
    ("prune_args", "is_done_removed"), [(["prune"], False), (["prune", "--yes"], True)]
)
def test_prune_without_terminal_deletes_done_tasks_only_when_confirmed(
    ledger_path, monkeypatch, prune_args, is_done_removed
):
    statuses = [
        ledger.STATUS_SCHEDULED,
        ledger.STATUS_WAITING,
        ledger.STATUS_RUNNING,
        ledger.STATUS_NEEDS_REVIEW,
        ledger.STATUS_FAILED,
        ledger.STATUS_CANCELLED,
        ledger.STATUS_DONE,
    ]
    support.write_tasks(
        ledger_path, {status: support.task_record(status, task_id=status) for status in statuses}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    assert cli.main(prune_args) == 0

    removed_ids = (
        {ledger.STATUS_CANCELLED, ledger.STATUS_DONE}
        if is_done_removed
        else {ledger.STATUS_CANCELLED}
    )
    assert set(ledger.load_tasks()) == set(statuses) - removed_ids


@pytest.mark.parametrize(
    ("pull_requests", "idle_days", "is_removed"),
    [
        pytest.param([MERGED_PULL_REQUEST], 0, True, id="merged-pr"),
        pytest.param([CLOSED_PULL_REQUEST], 0, True, id="closed-pr"),
        pytest.param([OPEN_PULL_REQUEST], None, False, id="open-pr"),
        pytest.param(
            [MERGED_PULL_REQUEST, OPEN_PULL_REQUEST], None, False, id="open-and-merged-pr"
        ),
        pytest.param([], prune.PRUNE_IDLE_DAYS, True, id="no-pr-idle-session"),
        pytest.param([], prune.PRUNE_IDLE_DAYS - 1, False, id="no-pr-active-session"),
        pytest.param([], None, True, id="no-pr-missing-transcript"),
        pytest.param(None, prune.PRUNE_IDLE_DAYS, True, id="unchecked-idle-session"),
        pytest.param(None, prune.PRUNE_IDLE_DAYS - 1, False, id="unchecked-active-session"),
    ],
)
def test_prune_decides_done_tasks_by_stored_pull_requests_then_idle_time(
    ledger_path, pull_requests, idle_days, is_removed
):
    stored = {} if pull_requests is None else {"pull_requests": pull_requests}
    support.write_task(ledger_path, ledger.STATUS_DONE, **stored)
    if idle_days is not None:
        support.write_transcript(support.read_task(ledger_path)["session_id"], idle_days)

    assert cli.main(["prune", "--yes"]) == 0

    assert (support.TASK_ID not in ledger.load_tasks()) is is_removed


def test_prune_judges_no_worktree_task_by_stored_pull_requests(ledger_path):
    support.write_task(
        ledger_path, ledger.STATUS_DONE, no_worktree=True, pull_requests=[MERGED_PULL_REQUEST]
    )
    support.write_transcript(support.read_task(ledger_path)["session_id"], 0)

    assert cli.main(["prune", "--yes"]) == 0

    assert support.TASK_ID not in ledger.load_tasks()


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        pytest.param(
            {"pull_requests": [MERGED_PULL_REQUEST, MERGED_PULL_REQUEST]},
            "2 PRs merged",
            id="finished-pull-requests",
        ),
        pytest.param({"pull_requests": [CLOSED_PULL_REQUEST]}, "PR closed", id="closed"),
        pytest.param({"pull_requests": []}, "no transcript", id="no-pull-request"),
        pytest.param({"cwd": "missing"}, "directory missing", id="directory-missing"),
    ],
)
def test_prune_offers_done_task_with_its_reason(
    ledger_path, monkeypatch, capsys, tmp_path, fields, reason
):
    if "cwd" in fields:
        fields = {"cwd": str(tmp_path / fields["cwd"])}
    support.write_task(ledger_path, ledger.STATUS_DONE, **fields)
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n"))

    assert cli.main(["prune"]) == 0

    assert f"({ledger.STATUS_DONE}, {reason})" in capsys.readouterr().out
    assert support.TASK_ID not in ledger.load_tasks()


def test_prune_keeps_task_with_open_pull_request_and_missing_directory(ledger_path, tmp_path):
    support.write_task(
        ledger_path,
        ledger.STATUS_DONE,
        cwd=str(tmp_path / "missing"),
        pull_requests=[OPEN_PULL_REQUEST],
    )

    assert cli.main(["prune", "--yes"]) == 0

    assert support.TASK_ID in ledger.load_tasks()


@pytest.mark.parametrize(
    ("idle_days", "is_removed"),
    [
        pytest.param(prune.PRUNE_IDLE_DAYS, True, id="idle-conversation"),
        pytest.param(prune.PRUNE_IDLE_DAYS - 1, False, id="active-conversation"),
        pytest.param(None, True, id="missing-rollout"),
    ],
)
def test_prune_uses_codex_conversation_idle_time(ledger_path, tmp_path, idle_days, is_removed):
    codex_home = tmp_path / "saved-codex-home"
    support.write_task(
        ledger_path,
        ledger.STATUS_DONE,
        backend=ledger.BACKEND_CODEX,
        thread_id="codex-thread",
        environment={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
    )
    if idle_days is not None:
        rollout_name = "rollout-2030-01-01T00-00-00-codex-thread.jsonl"
        support.write_idle_file(
            codex_home / "sessions" / "2030" / "01" / "01" / rollout_name, idle_days
        )

    assert cli.main(["prune", "--yes"]) == 0

    assert (support.TASK_ID not in ledger.load_tasks()) is is_removed


def test_prune_keeps_done_task_with_attached_pickup(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_DONE, pull_requests=[MERGED_PULL_REQUEST])
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
        {task_id: support.task_record(ledger.STATUS_DONE, task_id=task_id) for task_id in task_ids},
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
            support.SIBLING_ID: support.task_record(ledger.STATUS_DONE, task_id=support.SIBLING_ID),
        },
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n", interrupt))

    assert cli.main(["prune"]) == 130

    assert set(ledger.load_tasks()) == {support.TASK_ID, support.SIBLING_ID}


def write_chain(ledger_path, statuses):
    task_ids = [support.PARENT_ID, support.CHILD_ID, support.GRANDCHILD_ID][: len(statuses)]
    tasks = {
        task_id: support.task_record(status, task_id=task_id)
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
    write_chain(ledger_path, [ledger.STATUS_DONE, ledger.STATUS_DONE, ledger.STATUS_CANCELLED])
    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))

    assert cli.main(["prune"]) == 0

    prompt = capsys.readouterr().out
    assert prompt.count("Remove ") == 1
    assert (
        f"{support.PARENT_ID}, {support.CHILD_ID} ({ledger.STATUS_DONE}, no transcript)" in prompt
    )
    assert set(ledger.load_tasks()) == remaining_ids


def test_prune_keeps_cancelled_parent_of_declined_task(ledger_path, monkeypatch, capsys):
    write_chain(ledger_path, [ledger.STATUS_CANCELLED, ledger.STATUS_DONE])
    monkeypatch.setattr(sys, "stdin", TerminalInput("n\n"))

    assert cli.main(["prune"]) == 0

    assert "no longer eligible" not in capsys.readouterr().err
    assert set(ledger.load_tasks()) == {support.PARENT_ID, support.CHILD_ID}


def test_prune_offers_only_eligible_tasks_in_chain(ledger_path, monkeypatch, capsys):
    write_chain(ledger_path, [ledger.STATUS_DONE, ledger.STATUS_DONE, ledger.STATUS_DONE])
    support.write_transcript(f"{support.PARENT_ID}-session", 0)
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n"))

    assert cli.main(["prune"]) == 0

    prompt = capsys.readouterr().out
    assert (
        f"Remove {support.CHILD_ID}, {support.GRANDCHILD_ID} ({ledger.STATUS_DONE}, no transcript)?"
        in prompt
    )
    assert set(ledger.load_tasks()) == {support.PARENT_ID}


def test_prune_shows_each_reason_when_chain_reasons_differ(ledger_path, monkeypatch, capsys):
    write_chain(ledger_path, [ledger.STATUS_DONE, ledger.STATUS_DONE])
    support.write_transcript(f"{support.PARENT_ID}-session", prune.PRUNE_IDLE_DAYS)
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n"))

    assert cli.main(["prune"]) == 0

    prompt = capsys.readouterr().out
    assert f"{support.PARENT_ID} ({ledger.STATUS_DONE}, idle {prune.PRUNE_IDLE_DAYS}d)" in prompt
    assert f"{support.CHILD_ID} ({ledger.STATUS_DONE}, no transcript)" in prompt
    assert not ledger.load_tasks()


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
            support.PARENT_ID: support.task_record(ledger.STATUS_DONE, task_id=support.PARENT_ID),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_DONE, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(ledger.STATUS_DONE, task_id=support.SIBLING_ID),
            SIBLING_CHILD_ID: support.task_record(
                ledger.STATUS_DONE, task_id=SIBLING_CHILD_ID, after=support.SIBLING_ID
            ),
            support.TASK_ID: support.task_record(ledger.STATUS_CANCELLED),
        },
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))

    assert cli.main(["prune"]) == 0

    assert capsys.readouterr().out.count("Remove ") == prompt_count
    assert set(ledger.load_tasks()) == remaining_ids


def test_prune_keeps_parent_of_remaining_task(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(ledger.STATUS_DONE, task_id=support.PARENT_ID),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_CANCELLED, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.GRANDCHILD_ID: support.task_record(
                ledger.STATUS_DONE, task_id=support.GRANDCHILD_ID, after=support.CHILD_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_FAILED, task_id=support.SIBLING_ID, after=support.PARENT_ID
            ),
        },
    )

    assert cli.main(["prune", "--yes"]) == 0

    assert set(ledger.load_tasks()) == {support.PARENT_ID, support.SIBLING_ID}


def test_prune_releases_worktree_after_its_last_task(owned_worktree):
    worktree = owned_worktree()

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
    tasks[support.TASK_ID] = support.task_record(
        ledger.STATUS_DONE, session_id="replacement-session"
    )
    support.write_tasks(ledger_path, tasks)


@pytest.mark.parametrize("change_task", [attach_pickup, add_dependent_task, replace_task])
def test_prune_keeps_only_task_that_changes_while_prompting(
    ledger_path, monkeypatch, tmp_path, change_task
):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_DONE),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_DONE, task_id=support.SIBLING_ID, cwd=str(tmp_path)
            ),
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
