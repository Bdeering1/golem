import json
import multiprocessing
import os
from typing import Any

import pytest

from golem import cli, ledger
from golem.backends import claude
from tests import support


@pytest.mark.parametrize("contender", ["scheduled", "pickup"])
def test_shared_worktree_blocks_contenders_and_allows_other_worktrees(
    fake_commands, ledger_path, monkeypatch, tmp_path, contender
):
    shared_worktree = tmp_path / "shared"
    other_worktree = tmp_path / "other"
    shared_worktree.mkdir()
    other_worktree.mkdir()
    contender_status = (
        ledger.STATUS_SCHEDULED if contender == "scheduled" else ledger.STATUS_NEEDS_REVIEW
    )
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(
                ledger.STATUS_SCHEDULED, worktree=str(shared_worktree)
            ),
            support.CHILD_ID: support.task_record(
                contender_status, task_id=support.CHILD_ID, worktree=str(shared_worktree)
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_SCHEDULED, task_id=support.SIBLING_ID, worktree=str(other_worktree)
            ),
        },
    )
    run_task_ids: list[str] = []

    def execute_contender() -> None:
        if contender == "pickup":
            with pytest.raises(SystemExit, match="directory is busy"):
                cli.main(["pickup", support.CHILD_ID])
            return
        child = support.read_task(ledger_path, support.CHILD_ID)
        assert cli.main(["_execute", support.CHILD_ID, child["session_id"]]) == 0

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        run_task_ids.append(task_id)
        if task_id == support.TASK_ID:
            sibling = support.read_task(ledger_path, support.SIBLING_ID)
            execute_contender()
            assert cli.main(["_execute", support.SIBLING_ID, sibling["session_id"]]) == 0
        return 0, json.dumps({"result": support.CLAUDE_RESULT}), ""

    monkeypatch.setattr(claude, "run_claude", run_claude)
    first = support.read_task(ledger_path)
    cli.main(["_execute", support.TASK_ID, first["session_id"]])

    assert (
        support.read_task(ledger_path, support.SIBLING_ID)["status"] == ledger.STATUS_NEEDS_REVIEW
    )
    if contender == "pickup":
        assert (
            support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_NEEDS_REVIEW
        )
        assert cli.main(["pickup", support.CHILD_ID]) == 0
        assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_DONE
        assert run_task_ids == [support.TASK_ID, support.SIBLING_ID]
        return
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_SCHEDULED
    child = support.read_task(ledger_path, support.CHILD_ID)
    assert cli.main(["_execute", support.CHILD_ID, child["session_id"]]) == 0
    assert run_task_ids == [support.TASK_ID, support.SIBLING_ID, support.CHILD_ID]


def test_pickup_holds_worktree_until_its_claude_process_exits(
    fake_commands, ledger_path, monkeypatch, tmp_path
):
    worktree = tmp_path / "shared"
    worktree.mkdir()
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    headless_task = support.task_record(
        ledger.STATUS_SCHEDULED,
        worktree=str(worktree),
        environment={"PATH": os.environ["PATH"], "GOLEM_CLAUDE_HOLD": ""},
    )
    support.write_tasks(
        ledger_path,
        {
            "pickup": support.task_record(
                ledger.STATUS_NEEDS_REVIEW, task_id="pickup", worktree=str(worktree)
            ),
            support.TASK_ID: headless_task,
        },
    )
    pickup_process = multiprocessing.get_context("fork").Process(
        target=lambda: cli.main(["pickup", "pickup"])
    )
    pickup_process.start()
    headless_session_id = str(headless_task["session_id"])
    try:
        support.wait_until(started_path.exists)
        assert cli.main(["_execute", support.TASK_ID, headless_session_id]) == 0
        assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED
        pickup_process.kill()
        pickup_process.join()
        assert cli.main(["_execute", support.TASK_ID, headless_session_id]) == 0
        assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED

        hold_path.unlink()

        support.wait_until(
            lambda: (
                cli.main(["_execute", support.TASK_ID, headless_session_id]) == 0
                and support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
            )
        )
    finally:
        hold_path.unlink(missing_ok=True)
        pickup_process.kill()
        pickup_process.join()
