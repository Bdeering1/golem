import json

import pytest

from golem import cli, executor, ledger
from golem.backends import claude
from tests import support


@pytest.mark.parametrize(
    ("exit_code", "title"),
    [
        pytest.param(0, f"golem: {support.TASK_ID} needs review", id="needs-review"),
        pytest.param(1, f"golem: {support.TASK_ID} failed", id="failed"),
    ],
)
def test_finished_task_notifies_with_result(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, exit_code, title
):
    result = "finished the task\nwith details"
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", str(exit_code))
    support.write_claude_output(fake_commands, result)
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == exit_code

    assert [call[-2:] for call in support.logged_osascript_calls(fake_commands)] == [
        [title, "finished the task"]
    ]


def test_only_final_chain_task_notifies_when_ready_for_review(
    installed_launch_agent, fake_commands, ledger_path
):
    cli.main(
        [
            "run",
            support.PARENT_ID,
            support.PARENT_PROMPT,
            "--now",
            "--then",
            support.CHILD_ID,
            support.CHILD_PROMPT,
        ]
    )
    parent = support.read_task(ledger_path, support.PARENT_ID)

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0
    assert [notification[-2] for notification in support.logged_osascript_calls(fake_commands)] == [
        f"golem: {support.CHILD_ID} needs review"
    ]


def test_failed_notification_delivery_keeps_task_failed(ledger_path, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        claude,
        "run_claude",
        lambda *_: (1, json.dumps({"result": "failed task"}), ""),
    )
    support.write_task(ledger_path, ledger.STATUS_RUNNING)

    assert executor.execute_task(support.TASK_ID, support.read_task(ledger_path), ()) == 1
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FAILED


def test_scheduled_task_with_missing_worktree_notifies_failure(
    fake_commands, ledger_path, tmp_path
):
    missing_worktree = tmp_path / "missing"
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, worktree=str(missing_worktree))

    assert (
        cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]]) == 0
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FAILED
    assert [call[-2:] for call in support.logged_osascript_calls(fake_commands)] == [
        [f"golem: {support.TASK_ID} failed", f"golem: worktree is missing: {missing_worktree}"]
    ]
