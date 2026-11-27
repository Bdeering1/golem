import re
from pathlib import Path

import pytest

from golem import cli, ledger
from golem.backends import claude
from golem.worktrees import git, task_directories
from tests import support


def test_detected_worktree_is_shared_with_dependents(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, tmp_path
):
    monkeypatch.chdir(git_repository)
    detected_worktree = tmp_path / "detected-worktree"
    detected_worktree.mkdir()
    support.write_claude_output(
        fake_commands,
        support.CLAUDE_RESULT,
        is_verbose=True,
        working_directory=str(detected_worktree),
    )
    cli.main(
        [
            "run",
            support.PARENT_PROMPT,
            "--now",
            "--",
            support.WORKTREE_FLAG,
            "custom",
        ]
    )
    cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])

    cli.main(
        [
            "_execute",
            support.PARENT_ID,
            support.read_task(ledger_path, support.PARENT_ID)["session_id"],
        ]
    )

    assert support.logged_claude_cwds(fake_commands)[-1] == str(detected_worktree)
    resource = ledger.load_worktrees()[str(detected_worktree.resolve())]
    assert resource["owner"] == task_directories.WORKTREE_OWNER_EXTERNAL


@pytest.mark.parametrize(
    ("aliases", "command"),
    [
        pytest.param({}, ["run", support.CHILD_PROMPT], id="run"),
        pytest.param(
            {"impl": f'run "{support.CHILD_PROMPT}" --label {{1}}'},
            ["impl", support.LABEL],
            id="alias",
        ),
    ],
)
def test_directory_option_creates_then_chain_in_directory(
    installed_launch_agent, ledger_path, monkeypatch, tmp_path, aliases, command
):
    support.write_aliases(aliases)
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path)

    assert (
        cli.main(
            [
                "-C",
                "project",
                *command,
                "--now",
                "--then",
                support.GRANDCHILD_PROMPT,
            ]
        )
        == 0
    )

    tasks = support.read_tasks(ledger_path)
    assert list(tasks) == ["pro1", "pro1-2"]
    assert {task["cwd"] for task in tasks.values()} == {str(tmp_path / "project")}


def test_directory_option_refuses_missing_directory_without_changing_ledger(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED)
    tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit, match="directory not found: missing"):
        cli.main(["-C", "missing", "run", support.CHILD_PROMPT, "--now"])

    assert support.read_tasks(ledger_path) == tasks


def test_no_worktree_chain_runs_in_directory_without_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    cli.main(
        [
            "run",
            support.PARENT_PROMPT,
            "--now",
            "--no-worktree",
            "--then",
            support.CHILD_PROMPT,
        ]
    )

    assert (
        cli.main(
            [
                "_execute",
                support.PARENT_ID,
                support.read_task(ledger_path, support.PARENT_ID)["session_id"],
            ]
        )
        == 0
    )

    tasks = support.read_tasks(ledger_path)
    assert [tasks[task_id]["status"] for task_id in (support.PARENT_ID, support.CHILD_ID)] == [
        ledger.STATUS_FINISHED,
        ledger.STATUS_FINISHED,
    ]
    assert support.logged_claude_cwds(fake_commands) == [str(git_repository)] * 2
    assert not (git_repository / ".claude").exists()
    assert ledger.load_worktrees() == {}


def test_after_in_place_parent_runs_in_parent_directory(
    installed_launch_agent, git_repository, ledger_path
):
    support.write_task(
        ledger_path,
        ledger.STATUS_SCHEDULED,
        task_id=support.PARENT_ID,
        cwd=str(git_repository),
        no_worktree=True,
    )

    cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])

    child = support.read_task(ledger_path, support.CHILD_ID)
    assert child["cwd"] == str(git_repository)
    assert child["no_worktree"] is True


@pytest.mark.parametrize(
    ("parent_fields", "run_arguments", "worktree_args", "message"),
    [
        pytest.param(
            {},
            ["--now", "--no-worktree"],
            [support.WORKTREE_FLAG, "custom"],
            "--no-worktree does not accept",
            id="flag",
        ),
        pytest.param(
            {},
            ["--now", "--no-worktree"],
            ["-wcustom"],
            "--no-worktree does not accept",
            id="flag-attached-name",
        ),
        pytest.param(
            {"no_worktree": True},
            ["--after", support.PARENT_ID],
            [support.WORKTREE_FLAG, "custom"],
            f"{support.PARENT_ID} runs in place",
            id="inherited",
        ),
    ],
)
def test_in_place_task_refuses_worktree_argument_without_changing_ledger(
    installed_launch_agent, ledger_path, parent_fields, run_arguments, worktree_args, message
):
    support.write_task(
        ledger_path, ledger.STATUS_SCHEDULED, task_id=support.PARENT_ID, **parent_fields
    )
    tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=message):
        cli.main(
            [
                "run",
                support.CHILD_PROMPT,
                *run_arguments,
                "--",
                *worktree_args,
            ]
        )

    assert support.read_tasks(ledger_path) == tasks


def test_usage_limit_resume_and_pickup_use_task_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_claude_output(
        fake_commands, support.USAGE_LIMIT_RESULT, api_error_status=claude.USAGE_LIMIT_HTTP_STATUS
    )
    cli.main(["run", support.PROMPT, "--now"])
    task = support.read_task(ledger_path)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])
    worktree = support.read_task(ledger_path)["worktree"]
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    support.set_task_due_now(ledger_path)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])
    cli.main(["pickup", support.TASK_ID])

    assert support.logged_claude_cwds(fake_commands)[-2:] == [worktree, worktree]


@pytest.mark.parametrize(
    ("status", "command_args"),
    [
        pytest.param(ledger.STATUS_FINISHED, ["pickup", support.TASK_ID], id="pickup"),
        pytest.param(ledger.STATUS_FAILED, ["retry", support.TASK_ID, "--now"], id="retry"),
    ],
)
def test_missing_worktree_is_reported_without_changing_task(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, status, command_args
):
    missing_worktree = tmp_path / "missing"
    support.write_task(ledger_path, status, worktree=str(missing_worktree))
    original_tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=re.escape(f"worktree is missing: {missing_worktree}")):
        cli.main(command_args)

    assert support.read_tasks(ledger_path) == original_tasks
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


def test_retry_creates_pending_worktree(
    installed_launch_agent, git_repository, ledger_path, monkeypatch
):
    pending_worktree = git_repository / ".claude" / "worktrees" / "pending"
    monkeypatch.chdir(git_repository)
    support.write_task(
        ledger_path, ledger.STATUS_FAILED, worktree=str(pending_worktree), worktree_pending=True
    )

    cli.main(["retry", support.TASK_ID, "--now"])
    cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]])

    assert pending_worktree.is_dir()
    assert "worktree_pending" not in support.read_task(ledger_path)


def test_claim_records_golem_worktree_resource_and_dependent_reuses_it(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    cli.main(["run", support.PARENT_PROMPT, "--now"])
    cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])
    parent = support.read_task(ledger_path, support.PARENT_ID)
    common_directory = git.git_common_directory(str(git_repository))
    assert common_directory is not None
    parent_worktree = Path(common_directory).parent / ".claude" / "worktrees" / parent["session_id"]
    support.write_claude_output(
        fake_commands,
        support.CLAUDE_RESULT,
        is_verbose=True,
        working_directory=str(parent_worktree),
    )

    cli.main(["_execute", support.PARENT_ID, parent["session_id"]])

    parent_worktree = support.read_task(ledger_path, support.PARENT_ID)["worktree"]
    worktrees = ledger.load_worktrees()
    resource = worktrees[str(Path(parent_worktree).resolve())]
    assert resource["owner"] == task_directories.WORKTREE_OWNER_GOLEM
    assert resource.get("repository") == git.git_common_directory(str(git_repository))
    assert support.read_task(ledger_path, support.CHILD_ID)["worktree"] == parent_worktree
    tasks = support.read_tasks(ledger_path)
    tasks[support.CHILD_ID]["status"] = ledger.STATUS_FAILED
    support.write_tasks(ledger_path, tasks)

    assert cli.main(["retry", support.CHILD_ID, "--now"]) == 0
    assert support.read_task(ledger_path, support.CHILD_ID)["worktree"] == parent_worktree
    assert ledger.load_worktrees()[str(Path(parent_worktree).resolve())] == resource
