import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, Unpack

import pytest

import golem

TASK_ID = "fix-thing"
PARENT_ID = "parent"
CHILD_ID = "child"
GRANDCHILD_ID = "grandchild"
SIBLING_ID = "sibling"
PROMPT = "fix the thing"
PARENT_PROMPT = "run the parent"
CHILD_PROMPT = "run the child"
GRANDCHILD_PROMPT = "run the grandchild"
SIBLING_PROMPT = "run the sibling"
AGENT_ARGS = ["--model", "haiku"]
AT_TEXT = "9pm"
OLD_SESSION_ID = "old-session"
OLD_DUE_AT = "2030-01-01T09:00:00+00:00"
CLAUDE_RESULT = "completed the task"
USAGE_LIMIT_RESULT = "You've hit your limit · resets 3pm"
RESET_TIME = datetime(2030, 1, 1, 10, 0, tzinfo=UTC)
EARLIER_RESET_TIME = datetime(2030, 1, 1, 11, 0, tzinfo=UTC)
LATER_RESET_TIME = datetime(2030, 1, 2, 11, 0, tzinfo=UTC)


class AtRunKwargs(TypedDict):
    input: str
    capture_output: bool
    text: bool
    check: bool


@pytest.fixture(scope="session")
def fake_bin_path(tmp_path_factory):
    bin_path = tmp_path_factory.mktemp("bin")
    scripts = {
        "claude": """#!/bin/sh
printf '\036\\n' >> "$GOLEM_CLAUDE_ARGS"
printf '%s\\n' "$@" >> "$GOLEM_CLAUDE_ARGS"
if [ -f "$GOLEM_CLAUDE_STDOUT" ]; then
    cat "$GOLEM_CLAUDE_STDOUT"
fi
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
    claude_stdout_path = tmp_path / "claude-stdout"
    at_input_path = tmp_path / "at-input"
    atrm_args_path = tmp_path / "atrm-args"
    claude_args_path.touch()
    atrm_args_path.touch()
    monkeypatch.setenv("PATH", f"{fake_bin_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GOLEM_CLAUDE_ARGS", str(claude_args_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STDOUT", str(claude_stdout_path))
    monkeypatch.setenv("GOLEM_AT_INPUT", str(at_input_path))
    monkeypatch.setenv("GOLEM_ATRM_ARGS", str(atrm_args_path))
    return {
        "at_input_path": at_input_path,
        "atrm_args_path": atrm_args_path,
        "claude_args_path": claude_args_path,
        "claude_stdout_path": claude_stdout_path,
    }


@pytest.fixture
def ledger_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "golem" / "tasks.json"


def read_task(ledger_path):
    return read_tasks(ledger_path)[TASK_ID]


def read_tasks(ledger_path):
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def write_task(ledger_path, status, *, task_id=TASK_ID, **fields: object):
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
    task.update(fields)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({task_id: task}), encoding="utf-8")


def logged_claude_args(fake_commands):
    calls = logged_claude_calls(fake_commands)
    return calls[-1] if calls else []


def logged_claude_calls(fake_commands):
    calls: list[list[str]] = []
    for call in fake_commands["claude_args_path"].read_text(encoding="utf-8").split("\x1e\n"):
        arguments = call.splitlines()
        if arguments:
            calls.append(arguments)
    return calls


def expected_claude_args(task, prompt=PROMPT):
    return [
        "--session-id",
        task["session_id"],
        "--print",
        prompt,
        "--output-format",
        "json",
        *AGENT_ARGS,
    ]


def write_claude_output(fake_commands, result, *, api_error_status=None, is_verbose=False):
    output = {
        "api_error_status": api_error_status,
        "is_error": api_error_status is not None,
        "result": result,
        "type": "result",
    }
    response = [{"type": "system", "subtype": "init"}, output] if is_verbose else output
    fake_commands["claude_stdout_path"].write_text(json.dumps(response), encoding="utf-8")


@pytest.fixture
def usage_limited_claude(fake_commands, monkeypatch):
    write_claude_output(
        fake_commands,
        USAGE_LIMIT_RESULT,
        api_error_status=golem.USAGE_LIMIT_HTTP_STATUS,
    )
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)


def raise_usage_lookup_error():
    raise SystemExit


def usage_response(five_hour, seven_day):
    frames = (
        {
            "type": "control_response",
            "response": {"request_id": "init", "subtype": "success"},
        },
        {
            "type": "control_response",
            "response": {
                "request_id": "usage",
                "subtype": "success",
                "response": {
                    "rate_limits": {"five_hour": five_hour, "seven_day": seven_day},
                    "rate_limits_available": True,
                },
            },
        },
    )
    return "\n".join(json.dumps(frame) for frame in frames)


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
    ("claude_exit", "claude_output", "expected_result", "expected_status"),
    [
        pytest.param(
            0,
            json.dumps({"result": CLAUDE_RESULT}),
            CLAUDE_RESULT,
            golem.STATUS_NEEDS_REVIEW,
            id="json-success",
        ),
        pytest.param(
            1,
            json.dumps({"result": CLAUDE_RESULT}),
            CLAUDE_RESULT,
            golem.STATUS_FAILED,
            id="json-failure",
        ),
        pytest.param(
            0,
            json.dumps(
                [
                    {"type": "system", "subtype": "init"},
                    {"type": "result", "result": CLAUDE_RESULT},
                ]
            ),
            CLAUDE_RESULT,
            golem.STATUS_NEEDS_REVIEW,
            id="verbose-json-success",
        ),
        pytest.param(
            0,
            "plain output",
            "plain output",
            golem.STATUS_NEEDS_REVIEW,
            id="plain-output",
        ),
    ],
)
def test_run_now_records_claude_result(
    capsys,
    fake_commands,
    monkeypatch,
    ledger_path,
    claude_exit,
    claude_output,
    expected_result,
    expected_status,
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", str(claude_exit))
    fake_commands["claude_stdout_path"].write_text(claude_output, encoding="utf-8")

    assert golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS]) == claude_exit

    task = read_task(ledger_path)
    assert task["status"] == expected_status
    assert task["returncode"] == claude_exit
    assert logged_claude_args(fake_commands) == expected_claude_args(task)
    assert task["result"] == expected_result
    assert capsys.readouterr().out == f"{expected_result}\n"

    assert golem.main(["show", TASK_ID]) == 0

    assert capsys.readouterr().out == f"{expected_result}\n"


@pytest.mark.parametrize("is_verbose", [False, True])
def test_usage_limited_task_resumes_at_reset_in_same_session(
    usage_limited_claude, fake_commands, monkeypatch, ledger_path, is_verbose
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    agent_args = [*AGENT_ARGS, "--verbose"] if is_verbose else AGENT_ARGS
    write_claude_output(
        fake_commands,
        USAGE_LIMIT_RESULT,
        api_error_status=golem.USAGE_LIMIT_HTTP_STATUS,
        is_verbose=is_verbose,
    )

    assert golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args]) == 0

    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_SCHEDULED
    assert task["result"] == USAGE_LIMIT_RESULT
    assert datetime.fromisoformat(task["due_at"]) == RESET_TIME
    session_id = task["session_id"]

    write_claude_output(fake_commands, CLAUDE_RESULT, is_verbose=is_verbose)
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")

    assert golem.main(["_execute", TASK_ID, session_id]) == 0

    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_NEEDS_REVIEW
    assert task["result"] == CLAUDE_RESULT
    assert task["session_id"] == session_id
    assert logged_claude_args(fake_commands) == [
        "--resume",
        session_id,
        "--print",
        golem.USAGE_LIMIT_RESUME_PROMPT,
        "--output-format",
        "json",
        *agent_args,
    ]


def test_usage_limited_task_fails_once_resumes_run_out(usage_limited_claude, ledger_path):
    golem.main(["run", TASK_ID, PROMPT, "--now"])

    for _ in range(golem.MAX_USAGE_LIMIT_RESUMES):
        task = read_task(ledger_path)
        assert task["status"] == golem.STATUS_SCHEDULED
        golem.main(["_execute", TASK_ID, task["session_id"]])

    assert read_task(ledger_path)["status"] == golem.STATUS_FAILED


@pytest.mark.parametrize(
    "find_reset_time",
    [
        pytest.param(lambda: None, id="no-reset"),
        pytest.param(raise_usage_lookup_error, id="lookup-error"),
    ],
)
def test_usage_limited_task_fails_when_it_cannot_be_rescheduled(
    usage_limited_claude,
    monkeypatch,
    ledger_path,
    find_reset_time,
):
    monkeypatch.setattr(golem, "next_reset_time", find_reset_time)
    write_task(ledger_path, golem.STATUS_RUNNING)

    golem.execute_task(TASK_ID, read_task(ledger_path))

    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_FAILED
    assert task["result"] == USAGE_LIMIT_RESULT


def test_usage_limited_task_fails_when_at_cannot_reschedule(
    usage_limited_claude, monkeypatch, ledger_path
):
    monkeypatch.setenv("GOLEM_AT_EXIT", "1")
    write_task(ledger_path, golem.STATUS_RUNNING)

    with pytest.raises(SystemExit):
        golem.execute_task(TASK_ID, read_task(ledger_path))

    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_FAILED
    assert task["result"] == USAGE_LIMIT_RESULT


@pytest.mark.parametrize(
    ("five_hour", "seven_day", "expected_reset_time"),
    [
        pytest.param(
            {"utilization": 74, "resets_at": EARLIER_RESET_TIME.isoformat()},
            {"utilization": 74, "resets_at": LATER_RESET_TIME.isoformat()},
            EARLIER_RESET_TIME,
            id="five-hour-available",
        ),
        pytest.param(
            {"utilization": 100, "resets_at": LATER_RESET_TIME.isoformat()},
            {"utilization": 100, "resets_at": EARLIER_RESET_TIME.isoformat()},
            LATER_RESET_TIME,
            id="latest-exhausted-window",
        ),
        pytest.param(
            {"utilization": 74, "resets_at": EARLIER_RESET_TIME.isoformat()},
            {"utilization": 100, "resets_at": LATER_RESET_TIME.isoformat()},
            LATER_RESET_TIME,
            id="weekly-exhausted",
        ),
    ],
)
def test_next_reset_time_picks_latest_exhausted_window(
    fake_commands, five_hour, seven_day, expected_reset_time
):
    fake_commands["claude_stdout_path"].write_text(
        usage_response(five_hour, seven_day), encoding="utf-8"
    )

    assert golem.next_reset_time() == expected_reset_time


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


@pytest.mark.parametrize(
    "status", [golem.STATUS_SCHEDULED, golem.STATUS_WAITING, golem.STATUS_CANCELLED]
)
def test_pickup_refuses_tasks_that_never_started(fake_commands, ledger_path, status):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["pickup", TASK_ID])


def create_waiting_chain():
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--at", AT_TEXT, "--", *AGENT_ARGS])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])
    golem.main(["run", GRANDCHILD_ID, GRANDCHILD_PROMPT, "--after", CHILD_ID, "--", *AGENT_ARGS])
    golem.main(["run", SIBLING_ID, SIBLING_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])


def test_scheduled_task_runs_waiting_chain(fake_commands, ledger_path):
    create_waiting_chain()

    tasks = read_tasks(ledger_path)
    assert tasks[CHILD_ID]["status"] == golem.STATUS_WAITING
    assert tasks[GRANDCHILD_ID]["status"] == golem.STATUS_WAITING
    assert tasks[SIBLING_ID]["status"] == golem.STATUS_WAITING
    assert logged_claude_args(fake_commands) == []

    assert golem.main(["_execute", PARENT_ID, tasks[PARENT_ID]["session_id"]]) == 0

    tasks = read_tasks(ledger_path)
    assert {task["status"] for task in tasks.values()} == {golem.STATUS_NEEDS_REVIEW}
    assert len({task["session_id"] for task in tasks.values()}) == 4
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(tasks[PARENT_ID], PARENT_PROMPT),
        expected_claude_args(tasks[CHILD_ID], CHILD_PROMPT),
        expected_claude_args(tasks[GRANDCHILD_ID], GRANDCHILD_PROMPT),
        expected_claude_args(tasks[SIBLING_ID], SIBLING_PROMPT),
    ]


def test_retrying_failed_parent_runs_waiting_dependent(fake_commands, ledger_path, monkeypatch):
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--at", AT_TEXT])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    parent_session_id = read_tasks(ledger_path)[PARENT_ID]["session_id"]
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")

    assert golem.main(["_execute", PARENT_ID, parent_session_id]) == 1

    tasks = read_tasks(ledger_path)
    assert tasks[PARENT_ID]["status"] == golem.STATUS_FAILED
    assert tasks[CHILD_ID]["status"] == golem.STATUS_WAITING
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")

    assert golem.main(["retry", PARENT_ID, "--now"]) == 0

    tasks = read_tasks(ledger_path)
    assert tasks[PARENT_ID]["status"] == golem.STATUS_NEEDS_REVIEW
    assert tasks[CHILD_ID]["status"] == golem.STATUS_NEEDS_REVIEW


@pytest.mark.parametrize(
    ("cancel_id", "expected_statuses", "expected_atrm_args"),
    [
        pytest.param(
            PARENT_ID,
            {
                PARENT_ID: golem.STATUS_CANCELLED,
                CHILD_ID: golem.STATUS_CANCELLED,
                GRANDCHILD_ID: golem.STATUS_CANCELLED,
                SIBLING_ID: golem.STATUS_CANCELLED,
            },
            "42",
            id="scheduled-parent",
        ),
        pytest.param(
            CHILD_ID,
            {
                PARENT_ID: golem.STATUS_SCHEDULED,
                CHILD_ID: golem.STATUS_CANCELLED,
                GRANDCHILD_ID: golem.STATUS_CANCELLED,
                SIBLING_ID: golem.STATUS_WAITING,
            },
            "",
            id="waiting-child",
        ),
    ],
)
def test_cancelling_task_cancels_waiting_dependents(
    fake_commands, ledger_path, cancel_id, expected_statuses, expected_atrm_args
):
    create_waiting_chain()

    assert golem.main(["cancel", cancel_id]) == 0

    tasks = read_tasks(ledger_path)
    assert {task_id: task["status"] for task_id, task in tasks.items()} == expected_statuses
    assert fake_commands["atrm_args_path"].read_text(encoding="utf-8") == expected_atrm_args


def test_after_successful_parent_schedules_task_normally(fake_commands, ledger_path, monkeypatch):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)

    assert golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"]) == 0
    assert golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID]) == 0

    child = read_tasks(ledger_path)[CHILD_ID]
    assert child["status"] == golem.STATUS_SCHEDULED
    assert datetime.fromisoformat(child["due_at"]) == RESET_TIME


@pytest.mark.parametrize(
    ("parent_status", "message"),
    [
        pytest.param(None, "task not found", id="missing"),
        pytest.param(golem.STATUS_CANCELLED, "parent task is cancelled", id="cancelled"),
    ],
)
def test_after_refuses_missing_or_cancelled_parent(
    fake_commands, ledger_path, parent_status, message
):
    if parent_status is not None:
        write_task(ledger_path, parent_status, task_id=PARENT_ID)

    with pytest.raises(SystemExit, match=f"{message}: {PARENT_ID}"):
        golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])

    assert not ledger_path.exists() or CHILD_ID not in read_tasks(ledger_path)


def test_dependent_scheduling_failure_does_not_block_later_sibling(
    capsys, fake_commands, ledger_path, monkeypatch
):
    prompts: list[str] = []

    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--at", AT_TEXT])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    golem.main(["run", SIBLING_ID, SIBLING_PROMPT, "--after", PARENT_ID])

    def run_claude(task: dict[str, Any]) -> tuple[int, str]:
        prompt = task["prompt"]
        prompts.append(prompt)
        if prompt == CHILD_PROMPT:
            return 1, json.dumps(
                {
                    "api_error_status": golem.USAGE_LIMIT_HTTP_STATUS,
                    "result": USAGE_LIMIT_RESULT,
                }
            )
        return 0, json.dumps({"result": CLAUDE_RESULT})

    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    monkeypatch.setattr(golem, "run_claude", run_claude)
    monkeypatch.setenv("GOLEM_AT_EXIT", "1")
    parent = read_tasks(ledger_path)[PARENT_ID]

    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == 0

    tasks = read_tasks(ledger_path)
    assert prompts == [PARENT_PROMPT, CHILD_PROMPT, SIBLING_PROMPT]
    assert tasks[PARENT_ID]["status"] == golem.STATUS_NEEDS_REVIEW
    assert tasks[CHILD_ID]["status"] == golem.STATUS_FAILED
    assert tasks[SIBLING_ID]["status"] == golem.STATUS_NEEDS_REVIEW
    assert "could not schedule task" in capsys.readouterr().err


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

    assert TASK_ID not in read_tasks(ledger_path)


def test_scheduling_failure_keeps_parent_with_waiting_dependent(
    fake_commands, ledger_path, monkeypatch
):
    run_subprocess = subprocess.run

    def register_dependent(
        command: list[str],
        **run_kwargs: Unpack[AtRunKwargs],
    ) -> subprocess.CompletedProcess[str]:
        result = run_subprocess(command, **run_kwargs)
        if command[0] == "at":
            assert golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", TASK_ID]) == 0
        return result

    monkeypatch.setattr(golem.subprocess, "run", register_dependent)
    monkeypatch.setenv("GOLEM_AT_EXIT", "1")

    with pytest.raises(SystemExit, match="could not schedule task"):
        golem.main(["run", TASK_ID, PROMPT, "--at", AT_TEXT])

    tasks = read_tasks(ledger_path)
    assert tasks[TASK_ID]["status"] == golem.STATUS_FAILED
    assert tasks[CHILD_ID]["status"] == golem.STATUS_WAITING
