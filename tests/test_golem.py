import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import golem

TASK_ID = "fix-thing"
PROMPT = "fix the thing"
AGENT_ARGS = ["--model", "haiku"]
AT_TEXT = "9pm"
OLD_SESSION_ID = "old-session"
OLD_DUE_AT = "2030-01-01T09:00:00+00:00"


@pytest.fixture(scope="session")
def fake_bin_path(tmp_path_factory):
    bin_path = tmp_path_factory.mktemp("bin")
    scripts = {
        "claude": """#!/bin/sh
printf '%s\\n' "$@" > "$GOLEM_CLAUDE_ARGS"
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
printf '%s\\n' "job ${GOLEM_AT_JOB:-42} at tomorrow" >&2
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
    return bin_path


@pytest.fixture
def fake_commands(monkeypatch, tmp_path, fake_bin_path):
    claude_args_path = tmp_path / "claude-args"
    at_input_path = tmp_path / "at-input"
    atrm_args_path = tmp_path / "atrm-args"
    claude_args_path.touch()
    atrm_args_path.touch()
    monkeypatch.setenv("PATH", f"{fake_bin_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GOLEM_CLAUDE_ARGS", str(claude_args_path))
    monkeypatch.setenv("GOLEM_AT_INPUT", str(at_input_path))
    monkeypatch.setenv("GOLEM_ATRM_ARGS", str(atrm_args_path))
    return {
        "at_input_path": at_input_path,
        "atrm_args_path": atrm_args_path,
        "claude_args_path": claude_args_path,
    }


@pytest.fixture
def ledger_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "golem" / "tasks.json"


def read_task(ledger_path):
    return json.loads(ledger_path.read_text(encoding="utf-8"))[TASK_ID]


def write_task(ledger_path, status):
    task = {
        "agent_args": AGENT_ARGS,
        "at_job": "11",
        "cwd": str(Path.cwd()),
        "due_at": OLD_DUE_AT,
        "prompt": PROMPT,
        "returncode": 1,
        "session_id": OLD_SESSION_ID,
        "status": status,
    }
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({TASK_ID: task}), encoding="utf-8")


def logged_claude_args(fake_commands):
    return fake_commands["claude_args_path"].read_text(encoding="utf-8").splitlines()


def expected_claude_args(task):
    return ["--session-id", task["session_id"], "--print", PROMPT, *AGENT_ARGS]


@pytest.mark.parametrize(
    "xdg_state_home",
    [
        pytest.param(None, id="unset"),
        pytest.param("", id="empty"),
        pytest.param("relative/state", id="relative"),
    ],
)
def test_ledger_falls_back_to_home_when_xdg_state_home_is_not_absolute(
    fake_commands, monkeypatch, tmp_path, xdg_state_home
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if xdg_state_home is not None:
        monkeypatch.setenv("XDG_STATE_HOME", xdg_state_home)

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0

    assert (tmp_path / ".local" / "state" / "golem" / "tasks.json").exists()


@pytest.mark.parametrize(
    ("claude_exit", "expected_status"),
    [
        pytest.param(0, golem.STATUS_NEEDS_REVIEW, id="success"),
        pytest.param(1, golem.STATUS_FAILED, id="failure"),
    ],
)
def test_run_now_records_claude_result(
    fake_commands, monkeypatch, ledger_path, claude_exit, expected_status
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", str(claude_exit))

    assert golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS]) == claude_exit

    task = read_task(ledger_path)
    assert task["status"] == expected_status
    assert task["returncode"] == claude_exit
    assert logged_claude_args(fake_commands) == expected_claude_args(task)


def test_scheduled_task_runs_when_its_at_job_fires(fake_commands, ledger_path):
    golem.main(["run", TASK_ID, PROMPT, "--at", AT_TEXT, "--", *AGENT_ARGS])
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_SCHEDULED
    assert task["at_job"] == "42"
    assert task["session_id"] in fake_commands["at_input_path"].read_text(encoding="utf-8")

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0

    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert logged_claude_args(fake_commands) == expected_claude_args(task)


@pytest.mark.parametrize("command_name", ["pickup", "close"])
def test_review_commands_complete_review_task(fake_commands, ledger_path, command_name):
    golem.main(["run", TASK_ID, PROMPT, "--now"])

    assert golem.main([command_name, TASK_ID]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_DONE


@pytest.mark.parametrize("status", [golem.STATUS_SCHEDULED, golem.STATUS_CANCELLED])
def test_pickup_refuses_tasks_that_never_started(fake_commands, ledger_path, status):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["pickup", TASK_ID])


@pytest.mark.parametrize(
    "atrm_exit",
    [
        pytest.param("0", id="removed"),
        pytest.param("1", id="atrm-fails"),
    ],
)
def test_cancel_marks_task_cancelled_and_removes_at_job(
    fake_commands, monkeypatch, ledger_path, atrm_exit
):
    write_task(ledger_path, golem.STATUS_SCHEDULED)
    monkeypatch.setenv("GOLEM_ATRM_EXIT", atrm_exit)

    assert golem.main(["cancel", TASK_ID]) == 0
    assert golem.main(["_execute", TASK_ID, OLD_SESSION_ID]) == 0

    assert read_task(ledger_path)["status"] == golem.STATUS_CANCELLED
    assert fake_commands["atrm_args_path"].read_text(encoding="utf-8") == "11"
    assert logged_claude_args(fake_commands) == []


@pytest.mark.parametrize(
    "status",
    [golem.STATUS_RUNNING, golem.STATUS_NEEDS_REVIEW, golem.STATUS_FAILED, golem.STATUS_DONE],
)
def test_cancel_refuses_non_scheduled_tasks(fake_commands, ledger_path, status):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["cancel", TASK_ID])


@pytest.mark.parametrize(
    ("retry_args", "reset_time", "expected_status"),
    [
        pytest.param(["--now"], None, golem.STATUS_NEEDS_REVIEW, id="now"),
        pytest.param(["--at", AT_TEXT], None, golem.STATUS_SCHEDULED, id="at"),
        pytest.param([], None, golem.STATUS_NEEDS_REVIEW, id="no-usage-window"),
        pytest.param(
            [], datetime(2030, 1, 1, tzinfo=UTC), golem.STATUS_SCHEDULED, id="usage-window"
        ),
    ],
)
def test_retry_runs_failed_task_in_new_session(
    fake_commands, monkeypatch, ledger_path, retry_args, reset_time, expected_status
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS])
    failed_session_id = read_task(ledger_path)["session_id"]
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setattr(golem, "next_reset_time", lambda: reset_time)

    assert golem.main(["retry", TASK_ID, *retry_args]) == 0

    task = read_task(ledger_path)
    assert task["status"] == expected_status
    assert task["session_id"] != failed_session_id

    golem.main(["_execute", TASK_ID, task["session_id"]])

    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert logged_claude_args(fake_commands) == expected_claude_args(task)


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(None, id="missing"),
        golem.STATUS_SCHEDULED,
        golem.STATUS_RUNNING,
        golem.STATUS_NEEDS_REVIEW,
        golem.STATUS_DONE,
    ],
)
def test_retry_refuses_unretryable_task_before_usage_lookup(
    fake_commands, monkeypatch, ledger_path, status
):
    if status is not None:
        write_task(ledger_path, status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["retry", TASK_ID])


def test_leftover_at_job_does_not_run_retried_task(fake_commands, ledger_path):
    write_task(ledger_path, golem.STATUS_CANCELLED)
    golem.main(["retry", TASK_ID, "--at", AT_TEXT])

    assert golem.main(["_execute", TASK_ID, OLD_SESSION_ID]) == 0

    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert logged_claude_args(fake_commands) == []


@pytest.mark.parametrize(
    ("retry_args", "expected_status"),
    [
        pytest.param(None, golem.STATUS_CANCELLED, id="cancel-only"),
        pytest.param(["--now"], golem.STATUS_NEEDS_REVIEW, id="retry-now"),
        pytest.param(["--at", AT_TEXT], golem.STATUS_SCHEDULED, id="retry-at"),
    ],
)
def test_cancel_during_scheduling_removes_original_at_job(
    fake_commands, monkeypatch, ledger_path, retry_args, expected_status
):
    parse_at_job_number = golem.at_job_number

    def cancel_then_parse_at_job_number(at_output) -> str | None:
        golem.main(["cancel", TASK_ID])
        if retry_args is not None:
            monkeypatch.setattr(golem, "at_job_number", parse_at_job_number)
            monkeypatch.setenv("GOLEM_AT_JOB", "43")
            golem.main(["retry", TASK_ID, *retry_args])
        return parse_at_job_number(at_output)

    monkeypatch.setattr(golem, "at_job_number", cancel_then_parse_at_job_number)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["run", TASK_ID, PROMPT, "--at", AT_TEXT])

    task = read_task(ledger_path)
    assert task["status"] == expected_status
    assert fake_commands["atrm_args_path"].read_text(encoding="utf-8") == "42"
    if expected_status == golem.STATUS_SCHEDULED:
        assert task["at_job"] == "43"


def test_retry_restores_previous_record_when_scheduling_fails(
    fake_commands, monkeypatch, ledger_path
):
    write_task(ledger_path, golem.STATUS_FAILED)
    previous_task = read_task(ledger_path)
    monkeypatch.setenv("GOLEM_AT_EXIT", "1")

    with pytest.raises(SystemExit):
        golem.main(["retry", TASK_ID, "--at", AT_TEXT])

    assert read_task(ledger_path) == previous_task


@pytest.mark.parametrize("environment_variable_name", ["GOLEM_AT_EXIT", "GOLEM_AT_NO_JOB"])
def test_scheduling_failure_removes_new_task(
    fake_commands, monkeypatch, ledger_path, environment_variable_name
):
    monkeypatch.setenv(environment_variable_name, "1")

    with pytest.raises(SystemExit):
        golem.main(["run", TASK_ID, PROMPT, "--at", AT_TEXT])

    assert TASK_ID not in json.loads(ledger_path.read_text(encoding="utf-8"))
