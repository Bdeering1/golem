import contextlib
import io
import json
import os
import select
import sys
import time
from pathlib import Path

import pytest

from golem import cli, gates, launch_agent, ledger, locks, spawn
from golem.backends import claude, claude_hooks
from tests import support

NATIVE_SESSION_ID = "native-session"


def gate_inputs(tmp_path):
    log_path = tmp_path / "gate-stdin"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def test_passing_gate_continues_chain_and_receives_task_as_json(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    parent = support.schedule_gated_chain(
        ledger_path,
        support.gate_command(tmp_path),
        then_arguments=("--then", support.GRANDCHILD_ID, support.GRANDCHILD_PROMPT),
    )
    support.write_transcript(parent["session_id"], 0)
    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0

    tasks = support.read_tasks(ledger_path)
    assert [
        tasks[task_id]["status"]
        for task_id in (support.PARENT_ID, support.CHILD_ID, support.GRANDCHILD_ID)
    ] == [
        ledger.STATUS_FINISHED,
        ledger.STATUS_FINISHED,
        ledger.STATUS_FINISHED,
    ]
    parent_input, child_input = gate_inputs(tmp_path)
    assert child_input["task_id"] == support.CHILD_ID
    assert parent_input == (
        {
            "task_id": support.PARENT_ID,
            "session_id": parent["session_id"],
            "directory": str(Path.cwd()),
            "started_at": tasks[support.PARENT_ID]["started_at"],
            "exit_code": 0,
            "result": support.CLAUDE_RESULT,
            "transcript_path": str(
                Path.home() / ".claude" / "projects" / "project" / f"{parent['session_id']}.jsonl"
            ),
        }
    )
    assert len(support.logged_claude_calls(fake_commands)) == 3


def test_gate_of_task_resumed_after_usage_limit_sees_first_run_start(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path, tmp_path
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_claude_output(
        fake_commands,
        support.USAGE_LIMIT_RESULT,
        api_error_status=claude.USAGE_LIMIT_HTTP_STATUS,
    )
    parent = support.schedule_gated_chain(ledger_path, support.gate_command(tmp_path))
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0
    first_started_at = support.read_task(ledger_path, support.PARENT_ID)["started_at"]
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    support.set_task_due_now(ledger_path, support.PARENT_ID)

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0

    [parent_input] = gate_inputs(tmp_path)
    assert parent_input["started_at"] == first_started_at
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_FINISHED


def test_failing_gate_stops_chain_with_its_stderr_as_reason(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    support.run_gated_chain(
        ledger_path,
        support.gate_command(tmp_path, exit_code=1, stderr=f"{support.GATE_STOP_REASON}\n"),
    )

    tasks = support.read_tasks(ledger_path)
    assert tasks[support.PARENT_ID]["status"] == ledger.STATUS_BLOCKED
    assert tasks[support.PARENT_ID]["gate_stop_reason"] == support.GATE_STOP_REASON
    assert tasks[support.CHILD_ID]["status"] == ledger.STATUS_WAITING
    assert len(support.logged_claude_calls(fake_commands)) == 1
    assert [call[-2:] for call in support.logged_osascript_calls(fake_commands)] == [
        [f"golem: {support.PARENT_ID} blocked", support.GATE_STOP_REASON]
    ]
    assert launch_agent.run_launch_agent_pass() == []
    assert not launch_agent.has_pending_tasks(tasks)


def test_executor_resuming_gated_task_runs_gate_before_follow_up(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    support.write_parent_with_waiting_child(ledger_path, gate=support.gate_command(tmp_path))

    assert cli.main(["_execute", support.PARENT_ID, f"{support.PARENT_ID}-session"]) == 0
    assert len(gate_inputs(tmp_path)) == 1
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_FINISHED


def test_gate_that_cannot_run_stops_chain_with_error_as_reason(
    installed_launch_agent, ledger_path, tmp_path
):
    missing_directory = str(tmp_path / "missing")
    support.write_parent_with_waiting_child(ledger_path, gate="true", cwd=missing_directory)

    assert cli.main(["_execute", support.PARENT_ID, f"{support.PARENT_ID}-session"]) == 0

    tasks = support.read_tasks(ledger_path)
    assert missing_directory in tasks[support.PARENT_ID]["gate_stop_reason"]
    assert tasks[support.CHILD_ID]["status"] == ledger.STATUS_WAITING


def test_passing_gate_does_not_wait_for_its_background_processes(ledger_path, monkeypatch):
    monkeypatch.setattr(gates, "GATE_TIMEOUT_SECONDS", 0.5)
    support.write_task(ledger_path, ledger.STATUS_FINISHED, gate="sleep 2 & exit 0")

    assert gates.gate_stop_reason(support.TASK_ID, support.read_task(ledger_path)) is None


def test_timed_out_gate_stops_chain_and_kills_processes_it_started(
    ledger_path, monkeypatch, tmp_path
):
    monkeypatch.setattr(gates, "GATE_TIMEOUT_SECONDS", 0.2)
    pid_path = tmp_path / "gate-child-pid"
    support.write_task(
        ledger_path, ledger.STATUS_FINISHED, gate=f"sleep 30 & echo $! > {pid_path}; wait"
    )

    assert (
        gates.gate_stop_reason(support.TASK_ID, support.read_task(ledger_path))
        == "gate timed out after 0.2s"
    )

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    with contextlib.suppress(ProcessLookupError):
        while time.monotonic() < deadline:
            os.kill(child_pid, 0)
            select.select([], [], [], 0.05)
        pytest.fail(f"gate child {child_pid} is still running")


def test_executor_resuming_gated_task_skips_gate_while_directory_is_busy(
    installed_launch_agent, ledger_path, tmp_path
):
    support.write_parent_with_waiting_child(ledger_path, gate=support.gate_command(tmp_path))
    directory_lock_file = locks.acquire_lock(locks.directory_lock_path(str(Path.cwd())))
    assert directory_lock_file is not None
    try:
        assert cli.main(["_execute", support.PARENT_ID, f"{support.PARENT_ID}-session"]) == 0
    finally:
        locks.release_lock(directory_lock_file)

    assert gate_inputs(tmp_path) == []
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING


@pytest.mark.parametrize(
    ("is_usage_limited", "expected_returncode", "expected_status"),
    [
        pytest.param(False, 1, ledger.STATUS_FAILED, id="failed"),
        pytest.param(True, 0, ledger.STATUS_SCHEDULED, id="usage-limited"),
    ],
)
def test_unsuccessful_task_never_runs_its_gate(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    tmp_path,
    monkeypatch,
    is_usage_limited,
    expected_returncode,
    expected_status,
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    if is_usage_limited:
        support.write_claude_output(
            fake_commands,
            support.USAGE_LIMIT_RESULT,
            api_error_status=claude.USAGE_LIMIT_HTTP_STATUS,
        )
    else:
        monkeypatch.setenv("GOLEM_CLAUDE_FAIL_TASK", support.PARENT_ID)
    parent = support.schedule_gated_chain(ledger_path, support.gate_command(tmp_path))

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == expected_returncode
    assert support.read_task(ledger_path, support.PARENT_ID)["status"] == expected_status
    assert gate_inputs(tmp_path) == []


def test_task_without_waiting_follow_up_never_runs_its_gate(
    installed_launch_agent, ledger_path, tmp_path
):
    gate = support.gate_command(tmp_path, exit_code=1)
    cli.main(["run", support.PARENT_ID, support.PARENT_PROMPT, "--now", "--gate", gate])
    parent = support.read_task(ledger_path, support.PARENT_ID)

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0
    assert support.read_task(ledger_path, support.PARENT_ID)["status"] == ledger.STATUS_FINISHED
    assert "gate_stop_reason" not in support.read_task(ledger_path, support.PARENT_ID)
    assert gate_inputs(tmp_path) == []


def test_auto_pickup_opens_stopped_task_and_keeps_chain_stopped(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    parent = support.run_gated_chain(
        ledger_path,
        support.gate_command(tmp_path, exit_code=1, stderr=support.GATE_STOP_REASON),
        "--auto-pickup",
    )

    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={parent['session_id']}\n"
    )
    assert (
        support.read_task(ledger_path, support.PARENT_ID)["gate_stop_reason"]
        == support.GATE_STOP_REASON
    )
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING
    assert launch_agent.run_launch_agent_pass() == []


@pytest.fixture
def started_executors(monkeypatch):
    executors: list[str] = []
    monkeypatch.setattr(
        spawn, "start_task_executor", lambda task_id, task, **_: executors.append(task_id)
    )
    return executors


@pytest.mark.parametrize(
    ("gate_exit_code", "continue_arguments", "expected_gate_run_count"),
    [
        pytest.param(0, [], 1, id="passing-gate"),
        pytest.param(1, ["--force"], 0, id="force"),
    ],
)
def test_continue_releases_stopped_task(
    ledger_path,
    tmp_path,
    started_executors,
    gate_exit_code,
    continue_arguments,
    expected_gate_run_count,
):
    support.write_parent_with_waiting_child(
        ledger_path,
        ledger.STATUS_BLOCKED,
        gate=support.gate_command(tmp_path, exit_code=gate_exit_code),
        gate_stop_reason=support.GATE_STOP_REASON,
    )

    assert cli.main(["continue", support.PARENT_ID, *continue_arguments]) == 0
    assert gates.run_pending_gate_under_directory_lock(support.PARENT_ID)

    parent = support.read_task(ledger_path, support.PARENT_ID)
    assert parent["status"] == ledger.STATUS_FINISHED
    assert "finished_at" in parent
    assert "gate_stop_reason" not in parent
    assert len(gate_inputs(tmp_path)) == expected_gate_run_count
    assert started_executors == [support.PARENT_ID]


def test_continue_with_failing_gate_keeps_task_stopped(
    ledger_path, tmp_path, capsys, started_executors
):
    support.write_parent_with_waiting_child(
        ledger_path,
        ledger.STATUS_BLOCKED,
        gate=support.gate_command(tmp_path, exit_code=1, stderr="still missing"),
        gate_stop_reason=support.GATE_STOP_REASON,
    )

    assert cli.main(["continue", support.PARENT_ID]) == 1

    parent = support.read_task(ledger_path, support.PARENT_ID)
    assert parent["status"] == ledger.STATUS_BLOCKED
    assert parent["gate_stop_reason"] == "still missing"
    assert "still missing" in capsys.readouterr().err
    assert started_executors == []


def test_continue_refuses_task_not_stopped_at_a_gate(ledger_path, started_executors):
    support.write_task(ledger_path, ledger.STATUS_FINISHED)

    with pytest.raises(SystemExit, match="not stopped at a gate"):
        cli.main(["continue", support.TASK_ID])

    assert started_executors == []


def run_hook(monkeypatch, capsys, event, session_id):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session_id})))
    assert cli.main(["_hook", event]) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("parent_status", "hook_session_id"),
    [
        pytest.param(ledger.STATUS_BLOCKED, support.PARENT_SESSION, id="session"),
        pytest.param(ledger.STATUS_BLOCKED, NATIVE_SESSION_ID, id="native-session"),
    ],
)
def test_stop_hook_continues_stopped_chain_when_gate_passes(
    ledger_path, tmp_path, started_executors, monkeypatch, capsys, parent_status, hook_session_id
):
    support.write_parent_with_waiting_child(
        ledger_path,
        parent_status,
        gate=support.gate_command(tmp_path),
        gate_stop_reason=support.GATE_STOP_REASON,
        native_session_id=NATIVE_SESSION_ID,
    )

    output = run_hook(monkeypatch, capsys, claude_hooks.HOOK_ARGUMENT_STOP, hook_session_id)

    assert json.loads(output)["systemMessage"].endswith(support.CHILD_ID)
    assert support.read_task(ledger_path, support.PARENT_ID)["status"] == ledger.STATUS_FINISHED
    assert started_executors == [support.PARENT_ID]


@pytest.mark.parametrize(
    ("session_id", "gate", "is_directory_busy"),
    [
        ("unknown-session", "true", False),
        (support.PARENT_SESSION, "false", False),
        (support.PARENT_SESSION, "sleep 5", False),
        (support.PARENT_SESSION, "true", True),
    ],
    ids=["unknown-session", "failing-gate", "slow-gate", "busy-directory"],
)
def test_stop_hook_does_nothing(
    ledger_path, started_executors, monkeypatch, capsys, session_id, gate, is_directory_busy
):
    monkeypatch.setattr(gates, "HOOK_GATE_TIMEOUT_SECONDS", 0.2)
    support.write_parent_with_waiting_child(
        ledger_path, ledger.STATUS_BLOCKED, gate=gate, gate_stop_reason=support.GATE_STOP_REASON
    )
    directory_lock_file = (
        locks.acquire_lock(locks.directory_lock_path(str(Path.cwd())))
        if is_directory_busy
        else None
    )

    try:
        assert run_hook(monkeypatch, capsys, claude_hooks.HOOK_ARGUMENT_STOP, session_id) == ""
    finally:
        if directory_lock_file is not None:
            locks.release_lock(directory_lock_file)
    assert (
        support.read_task(ledger_path, support.PARENT_ID)["gate_stop_reason"]
        == support.GATE_STOP_REASON
    )
    assert started_executors == []


@pytest.mark.parametrize(
    ("child_status", "session_id", "is_denied"),
    [
        (ledger.STATUS_RUNNING, support.PARENT_SESSION, True),
        (ledger.STATUS_FINISHED, support.PARENT_SESSION, False),
        (ledger.STATUS_RUNNING, "unknown-session", False),
    ],
    ids=["follow-up-running", "follow-up-finished", "unknown-session"],
)
def test_pre_tool_use_hook_denies_while_follow_up_runs(
    ledger_path, monkeypatch, capsys, child_status, session_id, is_denied
):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID, gate="true"
            ),
            support.CHILD_ID: support.task_record(
                child_status, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
        },
    )

    output = run_hook(monkeypatch, capsys, claude_hooks.HOOK_ARGUMENT_PRE_TOOL_USE, session_id)

    if not is_denied:
        assert output == ""
        return
    decision = json.loads(output)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert support.CHILD_ID in decision["permissionDecisionReason"]


def test_pre_tool_use_hook_denies_while_grandchild_runs(ledger_path, monkeypatch, capsys):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID, gate="true"
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.GRANDCHILD_ID: support.task_record(
                ledger.STATUS_RUNNING, task_id=support.GRANDCHILD_ID, after=support.CHILD_ID
            ),
        },
    )

    output = run_hook(
        monkeypatch, capsys, claude_hooks.HOOK_ARGUMENT_PRE_TOOL_USE, support.PARENT_SESSION
    )

    assert (
        support.GRANDCHILD_ID
        in json.loads(output)["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_stop_hook_returns_while_follow_up_runs(ledger_path, tmp_path, monkeypatch):
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    support.write_parent_with_waiting_child(
        ledger_path,
        ledger.STATUS_BLOCKED,
        gate=support.gate_command(tmp_path),
        gate_stop_reason=support.GATE_STOP_REASON,
    )

    result = spawn.subprocess.run(
        ["/bin/sh", "-c", claude_hooks.hook_command_line(claude_hooks.HOOK_ARGUMENT_STOP)],
        input=json.dumps({"session_id": support.PARENT_SESSION}),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    try:
        support.wait_until(started_path.exists)
        assert support.CHILD_ID in json.loads(result.stdout)["systemMessage"]
    finally:
        hold_path.unlink()
        support.wait_until(
            lambda: (
                support.read_task(ledger_path, support.CHILD_ID)["status"] != ledger.STATUS_RUNNING
            )
        )
