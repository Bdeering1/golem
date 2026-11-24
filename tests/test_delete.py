import multiprocessing
import os
import subprocess
from pathlib import Path

import pytest

from golem import cli, executor, ledger, review
from golem.backends import claude_hooks
from golem.worktrees import git, task_directories
from tests import support

UNRELATED_ID = "unrelated"


def test_delete_refuses_running_task(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_RUNNING)

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["delete", support.TASK_ID])

    assert support.TASK_ID in ledger.load_tasks()


def test_delete_by_label_reports_every_missing_id(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, label=support.LABEL)

    with pytest.raises(SystemExit) as error:
        cli.main(["delete", support.LABEL, "first", "second"])

    assert str(error.value) == "golem: task not found: first\ngolem: task not found: second"

    assert support.TASK_ID in ledger.load_tasks()


def test_delete_refuses_and_lists_dependents(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_FINISHED),
            "first": support.task_record(
                ledger.STATUS_CANCELLED, task_id="first", after=support.TASK_ID
            ),
            "second": support.task_record(
                ledger.STATUS_FINISHED, task_id="second", after=support.TASK_ID
            ),
        },
    )

    with pytest.raises(SystemExit, match=r"first.*second"):
        cli.main(["delete", support.TASK_ID])

    assert support.TASK_ID in ledger.load_tasks()


def test_delete_removes_several_tasks_including_parent_and_child(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.SIBLING_ID
            ),
        },
    )

    assert cli.main(["delete", support.PARENT_ID, support.CHILD_ID]) == 0

    assert set(ledger.load_tasks()) == {support.SIBLING_ID}


def test_delete_chain_removes_ancestors_dependents_and_branches(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.GRANDCHILD_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.GRANDCHILD_ID, after=support.CHILD_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.SIBLING_ID, after=support.PARENT_ID
            ),
            UNRELATED_ID: support.task_record(ledger.STATUS_FINISHED, task_id=UNRELATED_ID),
        },
    )

    assert cli.main(["delete", support.CHILD_ID, "--chain"]) == 0

    assert set(ledger.load_tasks()) == {UNRELATED_ID}


def test_delete_chain_keeps_ledger_when_any_task_is_running(ledger_path):
    tasks = {
        support.PARENT_ID: support.task_record(ledger.STATUS_FINISHED, task_id=support.PARENT_ID),
        support.CHILD_ID: support.task_record(
            ledger.STATUS_FINISHED, task_id=support.CHILD_ID, after=support.PARENT_ID
        ),
        support.SIBLING_ID: support.task_record(
            ledger.STATUS_RUNNING, task_id=support.SIBLING_ID, after=support.PARENT_ID
        ),
    }
    support.write_tasks(ledger_path, tasks)

    with pytest.raises(SystemExit, match=support.SIBLING_ID):
        cli.main(["delete", support.CHILD_ID, "--chain"])

    assert support.read_tasks(ledger_path) == tasks


def test_reused_parent_id_does_not_claim_waiting_dependent(ledger_path):
    parent = support.task_record(ledger.STATUS_FINISHED, task_id=support.PARENT_ID)
    tasks = {
        support.PARENT_ID: parent,
        support.CHILD_ID: support.task_record(
            ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
        ),
    }
    old_session_id = parent["session_id"]
    assert isinstance(old_session_id, str)
    tasks[support.PARENT_ID] = support.task_record(
        ledger.STATUS_FINISHED, task_id=support.PARENT_ID, session_id="replacement-session"
    )
    support.write_tasks(ledger_path, tasks)

    assert executor.claim_next_dependent(support.PARENT_ID, old_session_id) is None
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING


def test_delete_releases_worktree_only_after_its_last_task(owned_worktree, ledger_path):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, worktree=str(worktree)),
            "other": support.task_record(
                ledger.STATUS_FAILED, task_id="other", worktree=str(worktree)
            ),
        },
    )

    assert cli.main(["delete", support.TASK_ID]) == 0
    resource = ledger.load_worktrees()[str(worktree.resolve())]
    assert "released_at" not in resource

    assert cli.main(["delete", "other"]) == 0
    assert ledger.load_worktrees()[str(worktree.resolve())].get("released_at")


def test_delete_parent_and_waiting_child_releases_shared_worktree(owned_worktree, ledger_path):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID, worktree=str(worktree)
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
        },
    )

    assert cli.main(["delete", support.PARENT_ID, support.CHILD_ID]) == 0

    assert ledger.load_worktrees()[str(worktree.resolve())].get("released_at")


def test_delete_chain_removes_shared_worktree_with_waiting_dependents(owned_worktree, ledger_path):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID, worktree=str(worktree)
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.SIBLING_ID, after=support.PARENT_ID
            ),
        },
    )

    assert cli.main(["delete", support.CHILD_ID, "--chain", "--remove-worktree"]) == 0

    assert ledger.load_tasks() == {}
    assert not worktree.exists()


def test_delete_chain_removes_worktree_holding_golem_hooks(owned_worktree, ledger_path):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_BLOCKED,
                task_id=support.PARENT_ID,
                worktree=str(worktree),
                gate="false",
                gate_stop_reason=support.GATE_STOP_REASON,
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
        },
    )
    claude_hooks.sync_hooks(str(worktree))
    assert claude_hooks.local_settings_path(str(worktree)).exists()

    assert cli.main(["delete", support.CHILD_ID, "--chain", "--remove-worktree"]) == 0

    assert not worktree.exists()


def test_delete_checks_every_worktree_before_removing_any(git_repository, ledger_path):
    clean_worktree = support.git_worktree(git_repository)
    support.git_output(git_repository, "worktree", "add", "-qb", "other-branch", "other")
    changed_worktree = git_repository / "other"
    (changed_worktree / "changed").write_text("keep", encoding="utf-8")
    tasks = {
        support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, worktree=str(clean_worktree)),
        support.SIBLING_ID: support.task_record(
            ledger.STATUS_FINISHED, task_id=support.SIBLING_ID, worktree=str(changed_worktree)
        ),
    }
    support.write_tasks(ledger_path, tasks)
    support.write_worktrees(
        support.golem_worktree_resource(git_repository, clean_worktree)
        | support.golem_worktree_resource(git_repository, changed_worktree)
    )

    assert cli.main(["delete", support.TASK_ID, support.SIBLING_ID, "--remove-worktree"]) == 1

    assert clean_worktree.is_dir()
    assert support.read_tasks(ledger_path) == tasks


def test_delete_keeps_tasks_when_later_worktree_removal_fails(
    capsys, git_repository, ledger_path, monkeypatch
):
    first_worktree = support.git_worktree(git_repository)
    support.git_output(git_repository, "worktree", "add", "-qb", "other-branch", "other")
    second_worktree = git_repository / "other"
    tasks = {
        support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, worktree=str(first_worktree)),
        support.SIBLING_ID: support.task_record(
            ledger.STATUS_FINISHED, task_id=support.SIBLING_ID, worktree=str(second_worktree)
        ),
    }
    support.write_tasks(ledger_path, tasks)
    support.write_worktrees(
        support.golem_worktree_resource(git_repository, first_worktree)
        | support.golem_worktree_resource(git_repository, second_worktree)
    )
    run_git = git.run_git

    def fail_second_removal(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments and str(second_worktree.resolve()) in arguments:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="could not remove")
        return run_git(directory, *arguments)

    monkeypatch.setattr(git, "run_git", fail_second_removal)

    assert cli.main(["delete", support.TASK_ID, support.SIBLING_ID, "--remove-worktree"]) == 1
    assert f"removed worktree: {first_worktree.resolve()}" in capsys.readouterr().out
    assert not first_worktree.exists()
    assert support.read_tasks(ledger_path) == tasks

    monkeypatch.setattr(git, "run_git", run_git)

    assert cli.main(["delete", support.TASK_ID, support.SIBLING_ID, "--remove-worktree"]) == 0
    assert ledger.load_tasks() == {}
    assert not second_worktree.exists()


def test_delete_retains_worktree_shared_with_retryable_task(owned_worktree, ledger_path):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, worktree=str(worktree)),
            "other": support.task_record(
                ledger.STATUS_FAILED, task_id="other", worktree=str(worktree)
            ),
        },
    )

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1
    assert worktree.is_dir()
    assert support.TASK_ID in ledger.load_tasks()


def test_delete_prints_session_and_worktree(capsys, owned_worktree, ledger_path):
    worktree = owned_worktree()
    task = support.read_task(ledger_path)

    assert cli.main(["delete", support.TASK_ID]) == 0

    output = capsys.readouterr().out
    assert task["session_id"] in output
    assert str(worktree) in output


@pytest.mark.parametrize("owner", [None, task_directories.WORKTREE_OWNER_EXTERNAL])
def test_delete_does_not_remove_unknown_or_external_worktree(git_repository, ledger_path, owner):
    worktree = support.git_worktree(git_repository)
    support.write_task(ledger_path, ledger.STATUS_FINISHED, worktree=str(worktree))
    if owner is not None:
        support.write_worktrees(
            {
                str(worktree.resolve()): {
                    "owner": owner,
                    "repository": git.git_common_directory(str(git_repository)),
                    "session_id": "other",
                }
            }
        )

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1
    assert support.TASK_ID in ledger.load_tasks()
    assert worktree.is_dir()
    if owner is not None:
        resource = ledger.load_worktrees()[str(worktree.resolve())]
        assert "released_at" not in resource
        assert "removed_at" not in resource


def test_delete_with_no_worktree_keeps_task(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_FINISHED)

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["delete", support.TASK_ID, "--remove-worktree"])

    assert support.TASK_ID in ledger.load_tasks()


@pytest.mark.parametrize(
    "status",
    [ledger.STATUS_FINISHED, ledger.STATUS_FAILED],
)
def test_delete_removes_task_and_worktree_and_preserves_branch(owned_worktree, status):
    worktree = owned_worktree(status)

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 0
    assert support.TASK_ID not in ledger.load_tasks()
    assert not worktree.exists()
    assert ledger.load_worktrees()[str(worktree.resolve())].get("removed_at")
    assert support.git_output(worktree.parent, "show-ref", "--verify", "refs/heads/worktree")


@pytest.mark.parametrize("hidden_by", [None, "config", "assume-unchanged", "skip-worktree"])
def test_delete_retains_worktree_with_local_changes(owned_worktree, hidden_by):
    worktree = owned_worktree()
    changed_file = worktree / "changed"
    if hidden_by == "config":
        support.git_output(worktree, "config", "status.showUntrackedFiles", "no")
    elif hidden_by is not None:
        changed_file = worktree / "README"
        index_flag = f"--{hidden_by}"
        support.git_output(worktree, "update-index", index_flag, "README")
    changed_file.write_text("keep", encoding="utf-8")

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1

    assert changed_file.read_text(encoding="utf-8") == "keep"
    assert "removal_error" not in ledger.load_worktrees()[str(worktree.resolve())]
    assert "released_at" not in ledger.load_worktrees()[str(worktree.resolve())]
    assert support.TASK_ID in ledger.load_tasks()


@pytest.mark.parametrize(
    ("reference", "is_removed"),
    [
        pytest.param(None, False, id="unreferenced"),
        pytest.param("refs/worktree/local", False, id="private-ref"),
        pytest.param("refs/heads/retained", True, id="branch"),
    ],
)
def test_delete_preserves_detached_commit(owned_worktree, reference, is_removed):
    worktree = owned_worktree()
    support.git_output(worktree, "switch", "--detach")
    (worktree / "commit").write_text("keep", encoding="utf-8")
    support.git_output(worktree, "add", "commit")
    support.git_output(
        worktree,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "--no-gpg-sign",
        "-qm",
        "unpublished",
    )
    commit_id = support.git_output(worktree, "rev-parse", "HEAD")
    if reference is not None:
        support.git_output(worktree, "update-ref", reference, commit_id)

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed
    assert support.git_output(worktree.parent, "cat-file", "-t", commit_id) == "commit"


@pytest.mark.parametrize("directory_field", ["cwd", "worktree"])
def test_delete_retains_worktree_used_by_nested_task(owned_worktree, ledger_path, directory_field):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, worktree=str(worktree)),
            "scheduled": support.task_record(
                ledger.STATUS_SCHEDULED,
                task_id="scheduled",
                **{directory_field: str(worktree / "nested")},
            ),
        },
    )

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1
    assert worktree.is_dir()
    assert support.TASK_ID in ledger.load_tasks()


def test_delete_retains_nested_worktree(owned_worktree):
    worktree = owned_worktree()
    nested_worktree = worktree / ".claude" / "worktrees" / "nested"
    exclude_path = Path(support.git_output(worktree, "rev-parse", "--git-path", "info/exclude"))
    with exclude_path.open("a", encoding="utf-8") as exclude_file:
        exclude_file.write("\n.claude/\n")
    support.git_output(worktree, "worktree", "add", "-qb", "nested", str(nested_worktree))
    changed_file = nested_worktree / "README"
    changed_file.write_text("keep", encoding="utf-8")
    assert not support.git_output(worktree, "status", "--porcelain", "--untracked-files=all")

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1

    assert changed_file.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("is_nested_pickup", [False, True])
def test_delete_retains_worktree_in_use(
    owned_worktree, ledger_path, monkeypatch, tmp_path, is_nested_pickup
):
    worktree = owned_worktree()
    tasks = {support.TASK_ID: support.task_record(ledger.STATUS_FINISHED, worktree=str(worktree))}
    pickup_id = support.TASK_ID
    if is_nested_pickup:
        nested = worktree / "nested"
        nested.mkdir()
        tasks[support.CHILD_ID] = support.task_record(
            ledger.STATUS_FINISHED, task_id=support.CHILD_ID, cwd=str(nested)
        )
        pickup_id = support.CHILD_ID
    support.write_tasks(ledger_path, tasks)
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    pickup_process = multiprocessing.get_context("fork").Process(
        target=review.pickup, args=(pickup_id,)
    )
    pickup_process.start()
    try:
        support.wait_until(started_path.exists)
        assert cli.main(["worktree", "list"]) == 0
        assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1
        assert worktree.is_dir()
    finally:
        hold_path.unlink()
        pickup_process.join()
        assert pickup_process.exitcode == 0


@pytest.mark.parametrize(
    ("reason", "is_process_missing", "is_removed"),
    [
        pytest.param(
            f"claude session {support.TASK_ID}-session (pid 1 start 2026-10-16T00:00:00Z)",
            True,
            True,
            id="stale-claude-lock",
        ),
        pytest.param(
            f"claude session {support.TASK_ID}-session (pid 1 start 2026-10-16T00:00:00Z)",
            False,
            False,
            id="live-claude-lock",
        ),
        pytest.param("unverified lock", False, False, id="unverified-lock"),
        pytest.param(None, False, False, id="bare-lock"),
    ],
)
def test_delete_respects_worktree_locks(
    owned_worktree, monkeypatch, reason, is_process_missing, is_removed
):
    worktree = owned_worktree()
    support.git_output(
        worktree.parent,
        "worktree",
        "lock",
        *(["--reason", reason] if reason is not None else []),
        str(worktree),
    )

    def report_process(_: int, __: int) -> None:
        if is_process_missing:
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", report_process)

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed


def test_delete_records_git_removal_failure(capsys, owned_worktree, monkeypatch):
    worktree = owned_worktree()
    run_git = git.run_git

    def fail_removal(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="could not remove")
        return run_git(directory, *arguments)

    monkeypatch.setattr(git, "run_git", fail_removal)

    assert cli.main(["delete", support.TASK_ID, "--remove-worktree"]) == 1
    resource = ledger.load_worktrees()[str(worktree.resolve())]
    assert resource.get("removal_error") == "could not remove"
    assert "removed_at" not in resource
    assert worktree.is_dir()
    assert cli.main(["worktree", "list"]) == 0
    assert "could not remove" in capsys.readouterr().out
