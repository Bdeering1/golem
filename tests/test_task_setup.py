import json
import os
import shlex
from pathlib import Path

import pytest

from golem import cli, gates, ledger, locks, spawn, task_setup
from golem.backends import claude, claude_hooks
from tests import support

SETUP_SUFFIX = "blocked by another ticket"


def setup_command(tmp_path, output="{}", *, exit_code=0, stderr=""):
    log_path = tmp_path / "setup-stdin"
    return (
        f"cat >> {log_path}; echo >> {log_path}; printf %s {shlex.quote(output)}; "
        f"printf %s {shlex.quote(stderr)} >&2; exit {exit_code}"
    )


def setup_inputs(tmp_path):
    log_path = tmp_path / "setup-stdin"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def create_directory(tmp_path, name="platform"):
    directory = tmp_path / name
    directory.mkdir()
    return directory


def directory_setup_output(directory, **fields: str):
    return json.dumps({"directory": str(directory), **fields})


def is_prompt_passed(claude_args, prompt):
    return f"--print\n{prompt}\n--" in "\n".join(claude_args)


def schedule_setup_chain(ledger_path, setup, *extra_arguments: str):
    cli.main(
        [
            *[
                "run",
                support.PARENT_PROMPT,
                "--now",
                "--no-worktree",
                "--setup",
                setup,
            ],
            *["--then", support.CHILD_PROMPT, *extra_arguments],
        ]
    )
    return support.read_task(ledger_path, support.PARENT_ID)


@pytest.mark.parametrize("is_returning_fields", [True, False], ids=["fields", "neither"])
def test_setup_prepares_directory_and_prompt_for_chain(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, capsys, is_returning_fields
):
    directory = create_directory(tmp_path)
    output = (
        directory_setup_output(directory, prompt_suffix=SETUP_SUFFIX)
        if is_returning_fields
        else "{}"
    )
    expected_directory = str(directory) if is_returning_fields else str(Path.cwd())
    expected_prompt = (
        f"{support.PARENT_PROMPT}\n\n{SETUP_SUFFIX}"
        if is_returning_fields
        else support.PARENT_PROMPT
    )
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    parent = schedule_setup_chain(ledger_path, setup_command(tmp_path, output))

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0

    tasks = support.read_tasks(ledger_path)
    assert setup_inputs(tmp_path) == [
        {
            "task_id": support.PARENT_ID,
            "label": None,
            "session_id": parent["session_id"],
            "directory": str(Path.cwd()),
            "prompt": support.PARENT_PROMPT,
        }
    ]
    assert support.logged_claude_cwds(fake_commands) == [expected_directory] * 2
    assert is_prompt_passed(support.logged_claude_calls(fake_commands)[0], expected_prompt)
    assert [tasks[task_id]["cwd"] for task_id in (support.PARENT_ID, support.CHILD_ID)] == [
        expected_directory
    ] * 2
    assert tasks[support.PARENT_ID]["prompt"] == expected_prompt
    capsys.readouterr()
    assert cli.main(["show", support.PARENT_ID]) == 0
    shown = capsys.readouterr().err
    assert expected_directory in shown
    assert expected_prompt in shown


def test_setup_seeds_gate_hooks_in_returned_directory(
    installed_launch_agent, ledger_path, tmp_path
):
    directory = create_directory(tmp_path)
    parent = schedule_setup_chain(
        ledger_path,
        setup_command(tmp_path, directory_setup_output(directory)),
        "--gate",
        "exit 1",
    )

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0

    assert claude_hooks.local_settings_path(str(directory)).exists()
    assert not claude_hooks.local_settings_path(str(Path.cwd())).exists()


@pytest.mark.parametrize(
    ("setup_arguments", "expected_reason"),
    [
        pytest.param({"exit_code": 3, "stderr": "no ticket\n"}, "no ticket", id="non-zero"),
        pytest.param({"exit_code": 3}, "setup exited with status 3", id="non-zero-silent"),
        pytest.param({"output": "not json"}, "setup output is not valid JSON", id="invalid-json"),
        pytest.param({"output": "[]"}, task_setup.SETUP_OUTPUT_ERROR, id="not-object"),
        pytest.param(
            {"output": json.dumps({"directory": "missing"})},
            "setup directory does not exist",
            id="missing-directory",
        ),
        pytest.param(
            {"output": json.dumps({"directory": "~nosuchuser/x"})},
            "setup directory is not usable",
            id="unusable-directory",
        ),
    ],
)
def test_failed_setup_fails_task_and_keeps_reason(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    tmp_path,
    capsys,
    setup_arguments,
    expected_reason,
):
    parent = schedule_setup_chain(ledger_path, setup_command(tmp_path, **setup_arguments))

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 1

    tasks = support.read_tasks(ledger_path)
    assert tasks[support.PARENT_ID]["status"] == ledger.STATUS_FAILED
    assert expected_reason in tasks[support.PARENT_ID]["setup_failure_reason"]
    assert tasks[support.CHILD_ID]["status"] == ledger.STATUS_WAITING
    assert support.logged_claude_calls(fake_commands) == []
    [notification] = support.logged_osascript_calls(fake_commands)
    assert notification[-2] == f"golem: {support.PARENT_ID} failed"
    assert expected_reason in notification[-1]
    assert expected_reason in support.list_row(support.list_output(capsys), support.PARENT_ID)
    assert cli.main(["show", support.PARENT_ID]) == 1
    assert expected_reason in capsys.readouterr().err


def test_setup_that_cannot_run_fails_task(installed_launch_agent, ledger_path, tmp_path):
    missing_directory = str(tmp_path / "missing")
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, setup="true", cwd=missing_directory)

    assert cli.main(["_execute", support.TASK_ID, f"{support.TASK_ID}-session"]) == 1

    assert missing_directory in support.read_task(ledger_path)["setup_failure_reason"]


def test_timed_out_setup_fails_task(installed_launch_agent, ledger_path, monkeypatch):
    monkeypatch.setattr(gates, "GATE_TIMEOUT_SECONDS", 0.2)
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, setup="sleep 30")

    assert cli.main(["_execute", support.TASK_ID, f"{support.TASK_ID}-session"]) == 1

    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_FAILED
    assert "timed out" in task["setup_failure_reason"]


def test_usage_limit_resume_skips_setup(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, monkeypatch
):
    directory = create_directory(tmp_path)
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_claude_output(
        fake_commands, support.USAGE_LIMIT_RESULT, api_error_status=claude.USAGE_LIMIT_HTTP_STATUS
    )
    support.write_task(
        ledger_path,
        ledger.STATUS_SCHEDULED,
        setup=setup_command(tmp_path, directory_setup_output(directory)),
        no_worktree=True,
    )
    assert cli.main(["_execute", support.TASK_ID, f"{support.TASK_ID}-session"]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED

    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    support.set_task_due_now(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, f"{support.TASK_ID}-session"]) == 0

    assert len(setup_inputs(tmp_path)) == 1
    assert support.logged_claude_cwds(fake_commands) == [str(directory)] * 2
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


def execute_with_busy_directory(directory, task_id, session_id):
    busy_lock_file = locks.acquire_lock(locks.directory_lock_path(str(directory)))
    assert busy_lock_file is not None
    try:
        return cli.main(["_execute", task_id, session_id])
    finally:
        locks.release_lock(busy_lock_file)


def test_setup_into_busy_directory_requeues_without_rerunning_setup(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, capsys
):
    directory = create_directory(tmp_path)
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    parent = schedule_setup_chain(
        ledger_path, setup_command(tmp_path, directory_setup_output(directory))
    )

    execute_with_busy_directory(directory, support.PARENT_ID, parent["session_id"])

    assert support.read_task(ledger_path, support.PARENT_ID)["status"] == ledger.STATUS_SCHEDULED
    assert f"waiting for directory: {directory}" in support.list_row(
        support.list_output(capsys), support.PARENT_ID
    )
    queued_lock_file = locks.acquire_lock(locks.directory_lock_path(str(Path.cwd())))
    assert queued_lock_file is not None
    locks.release_lock(queued_lock_file)
    support.set_task_due_now(ledger_path, support.PARENT_ID)
    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0

    assert len(setup_inputs(tmp_path)) == 1
    assert support.logged_claude_cwds(fake_commands) == [str(directory)] * 2


@pytest.mark.parametrize("failure", ["setup", "agent", "busy-directory"])
def test_retry_runs_setup_again_only_after_failed_setup(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    tmp_path,
    monkeypatch,
    failure,
):
    directory = create_directory(tmp_path)
    first_run_path = tmp_path / "first-run"
    setup = setup_command(tmp_path, directory_setup_output(directory, prompt_suffix=SETUP_SUFFIX))
    if failure == "setup":
        failing_setup = setup_command(tmp_path, exit_code=1)
        setup = f"if [ -e {first_run_path} ]; then {setup}; else touch {first_run_path}; {failing_setup}; fi"
    elif failure == "agent":
        monkeypatch.setenv("GOLEM_CLAUDE_FAIL_TASK", support.TASK_ID)
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, setup=setup, no_worktree=True)
    if failure == "busy-directory":
        monkeypatch.setattr(task_setup, "MAX_SETUP_BUSY_RETRIES", 0)
        assert (
            execute_with_busy_directory(directory, support.TASK_ID, f"{support.TASK_ID}-session")
            == 1
        )
        assert (
            support.read_task(ledger_path)["setup_failure_reason"]
            == f"setup directory stayed busy: {directory}"
        )
    else:
        assert cli.main(["_execute", support.TASK_ID, f"{support.TASK_ID}-session"]) == 1
    monkeypatch.delenv("GOLEM_CLAUDE_FAIL_TASK", raising=False)

    assert cli.main(["retry", support.TASK_ID, "--now"]) == 0
    assert (
        cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]]) == 0
    )

    assert len(setup_inputs(tmp_path)) == (2 if failure == "setup" else 1)
    assert support.logged_claude_cwds(fake_commands)[-1] == str(directory)
    assert is_prompt_passed(
        support.logged_claude_args(fake_commands), f"{support.PROMPT}\n\n{SETUP_SUFFIX}"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


def test_setups_in_one_directory_run_one_at_a_time(
    installed_launch_agent, ledger_path, tmp_path, monkeypatch
):
    setup_started_path = tmp_path / "setup-started"
    setup_hold_path = tmp_path / "setup-hold"
    agent_hold_path = tmp_path / "agent-hold"
    agent_started_path = tmp_path / "agent-started"
    setup_hold_path.touch()
    agent_hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(agent_hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(agent_started_path))
    held_directory = create_directory(tmp_path, "held")
    sibling_directory = create_directory(tmp_path, "sibling")
    held_setup = (
        f"touch {setup_started_path}; while [ -e {setup_hold_path} ]; do sleep 0.05; done; "
        f"printf %s {shlex.quote(directory_setup_output(held_directory))}"
    )
    sibling_setup = setup_command(tmp_path, directory_setup_output(sibling_directory))
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_SCHEDULED,
                task_id=support.PARENT_ID,
                setup=held_setup,
                no_worktree=True,
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_SCHEDULED,
                task_id=support.SIBLING_ID,
                setup=sibling_setup,
                no_worktree=True,
                environment={"PATH": os.environ["PATH"], "GOLEM_CLAUDE_HOLD": ""},
            ),
        },
    )
    held_executor = spawn.subprocess.Popen(
        [*spawn.golem_command(), "_execute", support.PARENT_ID, support.PARENT_SESSION]
    )
    try:
        support.wait_until(setup_started_path.exists)
        assert cli.main(["_execute", support.SIBLING_ID, f"{support.SIBLING_ID}-session"]) == 0
        assert (
            support.read_task(ledger_path, support.SIBLING_ID)["status"] == ledger.STATUS_SCHEDULED
        )
        assert setup_inputs(tmp_path) == []

        setup_hold_path.unlink()
        support.wait_until(agent_started_path.exists)
        assert cli.main(["_execute", support.SIBLING_ID, f"{support.SIBLING_ID}-session"]) == 0
        assert support.read_task(ledger_path, support.SIBLING_ID)["cwd"] == str(sibling_directory)
    finally:
        setup_hold_path.unlink(missing_ok=True)
        agent_hold_path.unlink(missing_ok=True)
        held_executor.wait(timeout=15)
    assert support.read_task(ledger_path, support.PARENT_ID)["cwd"] == str(held_directory)


def test_codex_task_gets_setup_directory_and_suffix(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    directory = create_directory(tmp_path)
    setup = setup_command(tmp_path, directory_setup_output(directory, prompt_suffix=SETUP_SUFFIX))
    cli.main(
        [
            "run",
            support.PROMPT,
            "--backend",
            "codex",
            "--now",
            "--no-worktree",
            "--setup",
            setup,
        ]
    )

    assert (
        cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]]) == 0
    )

    assert setup_inputs(tmp_path)[0]["session_id"] is None
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(directory)
    assert (
        fake_commands["codex-stdin"]
        .read_text(encoding="utf-8")
        .endswith(f"{support.PROMPT}\n\n{SETUP_SUFFIX}\x1e\n")
    )


def test_setup_requires_no_worktree(installed_launch_agent, ledger_path):
    with pytest.raises(SystemExit, match="--setup requires --no-worktree"):
        cli.main(["run", support.PROMPT, "--now", "--setup", "true"])

    assert not ledger_path.exists()
