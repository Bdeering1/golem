import os
from datetime import UTC, datetime, timedelta

import pytest

from golem import cli, git, launch_agent, ledger, task_directories, worktree_cleanup
from tests import support


@pytest.mark.parametrize(
    ("owner", "expected_reason"),
    [
        pytest.param(None, "ownership unknown", id="legacy"),
        pytest.param(
            task_directories.WORKTREE_OWNER_EXTERNAL, "not created by Golem", id="external"
        ),
    ],
)
def test_worktree_list_retains_non_golem_worktrees(
    capsys, git_repository, ledger_path, owner, expected_reason
):
    worktree = support.git_worktree(git_repository)
    support.write_task(ledger_path, ledger.STATUS_FINISHED, worktree=str(worktree))
    if owner is not None:
        support.write_worktrees(
            {
                str(worktree.resolve()): {
                    "owner": owner,
                    "repository": git.git_common_directory(str(git_repository)),
                    "session_id": "other",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            }
        )

    assert cli.main(["worktree", "list"]) == 0

    assert expected_reason in capsys.readouterr().out
    assert cli.main(["delete", support.TASK_ID]) == 0
    if owner is not None:
        resource = ledger.load_worktrees()[str(worktree.resolve())]
        assert "released_at" not in resource
        assert "removed_at" not in resource


def test_removed_worktree_is_reported_by_pickup_and_retry(
    installed_launch_agent, owned_worktree, ledger_path
):
    worktree = owned_worktree()
    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 0
    support.write_task(ledger_path, ledger.STATUS_FINISHED, worktree=str(worktree))

    with pytest.raises(SystemExit, match="worktree was removed"):
        cli.main(["pickup", support.TASK_ID])
    support.write_task(ledger_path, ledger.STATUS_FAILED, worktree=str(worktree))
    with pytest.raises(SystemExit, match="worktree was removed"):
        cli.main(["retry", support.TASK_ID, "--now"])


@pytest.mark.parametrize(
    ("retention_days", "released_ago", "expected_exists"),
    [
        pytest.param(None, timedelta(days=2), True, id="disabled"),
        pytest.param(1, None, True, id="unreleased"),
        pytest.param(1, timedelta(hours=23), True, id="before-retention"),
        pytest.param(1, timedelta(days=1), False, id="at-retention"),
    ],
)
def test_auto_remove_requires_release_and_retention(
    owned_worktree, retention_days, released_ago, expected_exists
):
    released_at = (
        (datetime.now(UTC) - released_ago).isoformat() if released_ago is not None else None
    )
    worktree = owned_worktree(released_at=released_at)
    if retention_days is not None:
        worktree_cleanup.set_worktree_auto_remove(retention_days)

    worktree_cleanup.auto_remove_worktrees()
    assert worktree.is_dir() is expected_exists


def test_launch_agent_removes_worktree_released_by_delete(owned_worktree):
    worktree = owned_worktree(ledger.STATUS_FAILED)
    assert cli.main(["worktree", "auto-remove", "--after", "0"]) == 0
    assert cli.main(["delete", support.TASK_ID]) == 0

    assert launch_agent.run_launch_agent() == 0

    assert not worktree.exists()


@pytest.mark.parametrize(
    ("status", "is_opened"),
    [
        pytest.param(ledger.STATUS_FAILED, True, id="retryable"),
        pytest.param(ledger.STATUS_BLOCKED, True, id="blocked"),
        pytest.param(ledger.STATUS_FINISHED, False, id="finished-not-opened"),
    ],
)
def test_worktree_list_retains_worktree_needing_attention(
    capsys, owned_worktree, status, is_opened
):
    owned_worktree(status, is_opened=is_opened)

    assert cli.main(["worktree", "list"]) == 0

    assert "retained" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "command", "is_nested"),
    [
        pytest.param(ledger.STATUS_FINISHED, ["pickup", support.TASK_ID], False, id="pickup"),
        pytest.param(ledger.STATUS_FAILED, ["retry", support.TASK_ID, "--now"], False, id="retry"),
        pytest.param(ledger.STATUS_FINISHED, ["pickup", support.TASK_ID], True, id="nested-pickup"),
        pytest.param(
            ledger.STATUS_FAILED, ["retry", support.TASK_ID, "--now"], True, id="nested-retry"
        ),
    ],
)
def test_task_reuse_revokes_worktree_release(
    installed_launch_agent, owned_worktree, ledger_path, status, command, is_nested
):
    worktree = owned_worktree(status, released_at=support.RESET_TIME.isoformat())
    if is_nested:
        nested = worktree / "nested"
        nested.mkdir()
        support.write_task(ledger_path, status, cwd=str(nested))

    assert cli.main(command) == 0

    assert "released_at" not in ledger.load_worktrees()[str(worktree.resolve())]


@pytest.mark.parametrize(
    ("state", "is_removed"),
    [("created", True), ("retried", True), ("replaced", False), ("preexisting", False)],
)
def test_cleanup_verifies_original_worktree_ownership(
    installed_launch_agent, git_repository, ledger_path, monkeypatch, tmp_path, state, is_removed
):
    monkeypatch.chdir(git_repository)
    claude_path = tmp_path / "claude"
    claude_path.write_text(
        """#!/bin/sh
if [ "${GOLEM_CLAUDE_EXIT:-0}" != 0 ]; then
    exit "$GOLEM_CLAUDE_EXIT"
fi
while [ "$#" -gt 0 ]; do
    if [ "$1" = '--worktree' ]; then
        shift
        /usr/bin/git worktree add -qb created ".claude/worktrees/$1" || exit 1
    fi
    shift
done
printf '%s' '{"result":"done"}'
""",
        encoding="utf-8",
    )
    claude_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    task = support.read_task(ledger_path)
    worktree = git_repository / ".claude" / "worktrees" / task["session_id"]
    resource_before_retry = None

    if state == "preexisting":
        support.git_output(git_repository, "worktree", "add", "-qb", "external", str(worktree))
    if state == "retried":
        monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
        assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 1
        resource_before_retry = ledger.load_worktrees()[str(worktree.resolve())]
        monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
        assert cli.main(["retry", support.TASK_ID, "--now"]) == 0
        retried_task = support.read_task(ledger_path)
        assert retried_task["session_id"] != task["session_id"]
        assert retried_task["worktree"] == str(worktree)
        task = retried_task

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    if state == "retried":
        assert ledger.load_worktrees()[str(worktree.resolve())] == resource_before_retry
    assert cli.main(["close", support.TASK_ID]) == 0
    if state == "replaced":
        support.git_output(git_repository, "worktree", "remove", str(worktree))
        support.git_output(git_repository, "worktree", "add", "-qb", "external", str(worktree))

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed


def test_close_does_not_release_worktree(owned_worktree):
    worktree = owned_worktree(ledger.STATUS_FINISHED)

    assert cli.main(["close", support.TASK_ID]) == 0

    assert "released_at" not in ledger.load_worktrees()[str(worktree.resolve())]
