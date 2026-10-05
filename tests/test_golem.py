import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import golem


@pytest.fixture
def fake_commands(monkeypatch, tmp_path):
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    command_log_path = tmp_path / "commands"
    at_input_path = tmp_path / "at-input"
    atrm_args_path = tmp_path / "atrm-args"
    command_log_path.touch()
    scripts = {
        "claude": """#!/bin/sh
printf '%s\\n' "$@" > "$GOLEM_COMMAND_LOG"
exit "${GOLEM_CLAUDE_EXIT:-0}"
""",
        "at": """#!/bin/sh
cat > "$GOLEM_AT_INPUT"
if [ -n "${GOLEM_AT_EXIT:-}" ]; then
    printf '%s\\n' 'at failed' >&2
    exit "$GOLEM_AT_EXIT"
fi
if [ -n "${GOLEM_AT_NO_JOB:-}" ]; then
    exit 0
fi
printf '%s\\n' warning >&2
printf '%s\\n' 'job 42 at tomorrow' >&2
""",
        "atrm": """#!/bin/sh
printf '%s' "$*" > "$GOLEM_ATRM_ARGS"
exit "${GOLEM_ATRM_EXIT:-0}"
""",
    }
    for command_name, script in scripts.items():
        script_path = bin_path / command_name
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GOLEM_COMMAND_LOG", str(command_log_path))
    monkeypatch.setenv("GOLEM_AT_INPUT", str(at_input_path))
    monkeypatch.setenv("GOLEM_ATRM_ARGS", str(atrm_args_path))
    return {
        "at_input_path": at_input_path,
        "atrm_args_path": atrm_args_path,
        "command_log_path": command_log_path,
    }


@pytest.fixture
def state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "golem" / "tasks.json"


def read_task(state_path, task_id):
    return json.loads(state_path.read_text(encoding="utf-8"))[task_id]


def write_task(state_path, task_id, status):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                task_id: {
                    "agent_args": ["--model", "haiku"],
                    "at_job": "11",
                    "cwd": str(Path.cwd()),
                    "prompt": "fix the thing",
                    "returncode": 1,
                    "session_id": "old-session",
                    "status": status,
                }
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("xdg_state_home", [None, ""])
def test_ledger_uses_home_fallback_when_xdg_state_home_is_empty(
    fake_commands, monkeypatch, tmp_path, xdg_state_home
):
    monkeypatch.setenv("HOME", str(tmp_path))
    if xdg_state_home is None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_STATE_HOME", xdg_state_home)

    assert golem.main(["run", "fallback-task", "fix the thing", "--now"]) == 0

    assert (tmp_path / ".local" / "state" / "golem" / "tasks.json").exists()


@pytest.mark.parametrize(
    "case",
    [
        ("now", 0, "needs_review"),
        ("now", 1, "failed"),
        ("scheduled", 0, "needs_review"),
        ("scheduled", 1, "failed"),
    ],
)
def test_execution_records_task_result(
    fake_commands,
    monkeypatch,
    state_path,
    case,
):
    entry_point, claude_exit, expected_status = case
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", str(claude_exit))
    task_id = f"{entry_point}-task"
    if entry_point == "now":
        result = golem.main(["run", task_id, "fix the thing", "--now", "--", "--model", "haiku"])
    else:
        assert (
            golem.main(["run", task_id, "fix the thing", "--at", "9pm", "--", "--model", "haiku"])
            == 0
        )
        scheduled_task = read_task(state_path, task_id)
        assert scheduled_task["status"] == "scheduled"
        assert scheduled_task["at_job"] == "42"
        assert scheduled_task["session_id"] in fake_commands["at_input_path"].read_text(
            encoding="utf-8"
        )
        result = golem.main(["_execute", task_id, scheduled_task["session_id"]])

    assert result == claude_exit
    task = read_task(state_path, task_id)
    assert task["status"] == expected_status
    assert task["prompt"] == "fix the thing"
    assert task["agent_args"] == ["--model", "haiku"]
    assert task["returncode"] == claude_exit
    assert fake_commands["command_log_path"].read_text(encoding="utf-8").splitlines() == [
        "--session-id",
        task["session_id"],
        "--print",
        "fix the thing",
        "--model",
        "haiku",
    ]


@pytest.mark.parametrize("command", ["pickup", "close"])
def test_review_commands_complete_review_task(fake_commands, state_path, command):
    golem.main(["run", "review-task", "fix the thing", "--now"])

    assert golem.main([command, "review-task"]) == 0
    assert read_task(state_path, "review-task")["status"] == "done"


@pytest.mark.parametrize("status", ["scheduled", "cancelled"])
def test_pickup_refuses_tasks_that_never_started(fake_commands, state_path, status):
    write_task(state_path, "unstarted-task", status)

    with pytest.raises(SystemExit, match="golem: task has not started"):
        golem.main(["pickup", "unstarted-task"])


def test_cancel_marks_task_cancelled_and_removes_at_job(fake_commands, state_path):
    golem.main(["run", "scheduled-task", "fix the thing", "--at", "9pm"])

    assert golem.main(["cancel", "scheduled-task"]) == 0
    assert read_task(state_path, "scheduled-task")["status"] == "cancelled"
    assert fake_commands["atrm_args_path"].read_text(encoding="utf-8") == "42"


def test_cancel_without_at_job_skips_atrm(fake_commands, state_path):
    write_task(state_path, "scheduled-task", "scheduled")
    task = read_task(state_path, "scheduled-task")
    del task["at_job"]
    state_path.write_text(json.dumps({"scheduled-task": task}), encoding="utf-8")

    assert golem.main(["cancel", "scheduled-task"]) == 0
    assert read_task(state_path, "scheduled-task")["status"] == "cancelled"
    assert not fake_commands["atrm_args_path"].exists()


def test_cancel_keeps_task_cancelled_when_atrm_fails(
    fake_commands,
    monkeypatch,
    state_path,
    capsys,
):
    golem.main(["run", "scheduled-task", "fix the thing", "--at", "9pm"])
    monkeypatch.setenv("GOLEM_ATRM_EXIT", "1")

    assert golem.main(["cancel", "scheduled-task"]) == 0

    assert read_task(state_path, "scheduled-task")["status"] == "cancelled"
    assert "could not cancel at job 42" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["running", "needs_review", "failed", "done"])
def test_cancel_refuses_non_scheduled_tasks(fake_commands, state_path, status):
    write_task(state_path, "other-task", status)

    with pytest.raises(SystemExit, match="golem: task is not scheduled"):
        golem.main(["cancel", "other-task"])


@pytest.mark.parametrize(
    "case",
    [
        ("failed", ["--now"], "needs_review", False, True, None, True),
        ("cancelled", ["--at", "9pm"], "scheduled", True, False, "42", False),
    ],
)
def test_retry_preserves_task_details_and_starts_new_session(
    fake_commands,
    state_path,
    case,
):
    (
        initial_status,
        retry_args,
        expected_status,
        has_due_at,
        has_returncode,
        expected_at_job,
        calls_claude,
    ) = case
    write_task(state_path, "retry-task", initial_status)

    assert golem.main(["retry", "retry-task", *retry_args]) == 0

    task = read_task(state_path, "retry-task")
    command_args = fake_commands["command_log_path"].read_text(encoding="utf-8").splitlines()
    assert task["status"] == expected_status
    assert task["session_id"] != "old-session"
    assert task["prompt"] == "fix the thing"
    assert task["agent_args"] == ["--model", "haiku"]
    assert ("due_at" in task) is has_due_at
    assert ("returncode" in task) is has_returncode
    assert task.get("at_job") == expected_at_job
    assert (
        command_args
        == [
            "--session-id",
            task["session_id"],
            "--print",
            "fix the thing",
            "--model",
            "haiku",
        ]
    ) is calls_claude


@pytest.mark.parametrize(
    "case",
    [
        (None, "needs_review", False),
        (datetime(2030, 1, 1, tzinfo=UTC), "scheduled", True),
    ],
)
def test_retry_uses_run_default_schedule(fake_commands, monkeypatch, state_path, case):
    reset_time, expected_status, has_due_at = case
    write_task(state_path, "retry-task", "failed")
    monkeypatch.setattr(golem, "next_reset_time", lambda: reset_time)

    assert golem.main(["retry", "retry-task"]) == 0

    task = read_task(state_path, "retry-task")
    assert task["status"] == expected_status
    assert ("due_at" in task) is has_due_at


@pytest.mark.parametrize("status", ["scheduled", "running", "needs_review", "done"])
def test_retry_refuses_other_task_statuses_before_usage_lookup(
    fake_commands, monkeypatch, state_path, status
):
    write_task(state_path, "other-task", status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match="golem: task cannot be retried"):
        golem.main(["retry", "other-task"])


def test_retry_refuses_missing_task_before_usage_lookup(fake_commands, monkeypatch, state_path):
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match="golem: task not found"):
        golem.main(["retry", "missing-task"])


def test_retry_rejects_agent_arguments(fake_commands, state_path):
    write_task(state_path, "retry-task", "failed")

    with pytest.raises(SystemExit, match="2"):
        golem.main(["retry", "retry-task", "--", "--model", "opus"])


def test_cancelled_job_from_an_old_session_cannot_execute_a_retry(
    fake_commands, monkeypatch, state_path
):
    golem.main(["run", "retry-task", "fix the thing", "--at", "9pm"])
    old_session = read_task(state_path, "retry-task")["session_id"]
    monkeypatch.setenv("GOLEM_ATRM_EXIT", "1")
    golem.main(["cancel", "retry-task"])
    monkeypatch.delenv("GOLEM_ATRM_EXIT")

    assert golem.main(["retry", "retry-task", "--at", "9pm"]) == 0

    task = read_task(state_path, "retry-task")
    assert task["session_id"] != old_session
    assert golem.main(["_execute", "retry-task", old_session]) == 0
    assert read_task(state_path, "retry-task")["status"] == "scheduled"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_retry_restores_previous_record_when_scheduling_fails(
    fake_commands,
    monkeypatch,
    state_path,
    status,
):
    write_task(state_path, "retry-task", status)
    previous_task = read_task(state_path, "retry-task")
    monkeypatch.setenv("GOLEM_AT_EXIT", "1")

    with pytest.raises(SystemExit, match="golem: could not schedule task"):
        golem.main(["retry", "retry-task", "--at", "9pm"])

    assert read_task(state_path, "retry-task") == previous_task


@pytest.mark.parametrize("environment_name", ["GOLEM_AT_EXIT", "GOLEM_AT_NO_JOB"])
def test_scheduling_failure_removes_new_task(
    fake_commands, monkeypatch, state_path, environment_name
):
    monkeypatch.setenv(environment_name, "1")

    with pytest.raises(SystemExit, match="golem: could not schedule task"):
        golem.main(["run", "new-task", "fix the thing", "--at", "9pm"])

    assert "new-task" not in json.loads(state_path.read_text(encoding="utf-8"))
