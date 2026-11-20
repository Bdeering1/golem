import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest

from golem import cli, ledger, progress_log, scheduling
from golem.backends import claude, common
from tests import support


def test_after_pending_parent_skips_usage_lookup(installed_launch_agent, ledger_path, monkeypatch):
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, task_id=support.PARENT_ID)
    monkeypatch.setattr(
        claude, "next_reset_time", lambda _environment: pytest.fail("usage lookup ran")
    )

    assert cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID]) == 0
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING


@pytest.mark.parametrize(
    "status",
    [
        None,
        ledger.STATUS_SCHEDULED,
        ledger.STATUS_RUNNING,
        ledger.STATUS_BLOCKED,
        ledger.STATUS_FINISHED,
    ],
)
def test_retry_refuses_unretryable_task_before_usage_lookup(ledger_path, monkeypatch, status):
    if status is not None:
        support.write_task(ledger_path, status)
    monkeypatch.setattr(
        claude, "next_reset_time", lambda _environment: pytest.fail("usage lookup ran")
    )

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["retry", support.TASK_ID])


@pytest.mark.parametrize(
    ("backend_id", "option_arguments", "message"),
    [
        ("codex", ["--account", "work@example.com"], "--account supports only Claude"),
        (
            "t3",
            ["--account", "work@example.com", "--", "--provider", "codex"],
            "--account supports only Claude tasks, not Codex tasks",
        ),
        ("t3", ["--env", "NAME=value"], "--env does not support T3 Code"),
        ("t3", ["--", "--yolo"], "argument is not supported: --yolo"),
        ("t3", ["--", "--provider", "gemini"], "--provider must be claude or codex"),
        ("t3", ["--", "--model"], "--model needs a value"),
        ("t3", ["--", "--model", "--provider", "codex"], "--model needs a value"),
    ],
)
def test_run_option_rejects_backend_without_changing_ledger(
    installed_launch_agent, ledger_path, backend_id, option_arguments, message
):
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED)
    tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=message):
        cli.main(
            [
                *["run", support.CHILD_PROMPT, "--backend", backend_id, "--now"],
                *option_arguments,
            ]
        )

    assert support.read_tasks(ledger_path) == tasks


@pytest.mark.parametrize(
    "fields",
    [
        {"auto_pickup": True},
        {"auto_pickup_client": "claude-desktop"},
        {"no_worktree": True},
        {"gate": "true"},
    ],
)
def test_retry_keeps_run_options(installed_launch_agent, ledger_path, fields):
    support.write_task(ledger_path, ledger.STATUS_FAILED, **fields)

    assert cli.main(["retry", support.TASK_ID, "--now"]) == 0
    assert support.read_task(ledger_path).items() >= fields.items()


def finished_task(task_id, status=ledger.STATUS_FINISHED, **fields: object):
    fields = {
        "pull_requests": [{"url": "merged", "state": ledger.PULL_REQUEST_STATE_MERGED}],
        **fields,
    }
    return support.task_record(
        status,
        task_id=task_id,
        **{name: value for name, value in fields.items() if value is not None},
    )


@pytest.mark.parametrize(
    ("schedule_arguments", "new_id"),
    [
        pytest.param(["--now"], "wor2", id="root"),
        pytest.param(["--after", support.PARENT_ID], support.SIBLING_ID, id="dependent"),
    ],
)
def test_run_keeps_existing_finished_chain(
    installed_launch_agent, ledger_path, monkeypatch, capsys, schedule_arguments, new_id
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: None)
    tasks = {
        support.PARENT_ID: finished_task(support.PARENT_ID),
        support.CHILD_ID: finished_task(support.CHILD_ID, after=support.PARENT_ID),
    }
    support.write_tasks(ledger_path, tasks)

    assert cli.main(["run", support.PROMPT, *schedule_arguments]) == 0

    assert new_id in capsys.readouterr().out
    assert support.read_tasks(ledger_path).items() >= tasks.items()
    assert support.read_task(ledger_path, new_id)["prompt"] == support.PROMPT


def test_run_after_refuses_when_parent_is_replaced_during_usage_lookup(
    installed_launch_agent, ledger_path, monkeypatch
):
    support.write_tasks(ledger_path, {support.PARENT_ID: finished_task(support.PARENT_ID)})

    def replace_parent(_environment: dict[str, str]) -> None:
        support.write_tasks(
            ledger_path,
            {
                support.PARENT_ID: support.task_record(
                    ledger.STATUS_SCHEDULED, task_id=support.PARENT_ID
                )
            },
        )

    monkeypatch.setattr(claude, "next_reset_time", replace_parent)

    with pytest.raises(SystemExit, match=f"parent task changed: {support.PARENT_ID}"):
        cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])

    assert support.CHILD_ID not in support.read_tasks(ledger_path)


def fail_usage_lookup(_environment: dict[str, str]) -> Never:
    raise SystemExit(common.USAGE_ERROR)


@pytest.mark.parametrize(
    ("parent", "child_fields", "next_reset_time", "error"),
    [
        pytest.param(
            finished_task(support.PARENT_ID, ledger.STATUS_CANCELLED),
            {},
            None,
            "parent task is cancelled",
            id="cancelled-parent",
        ),
        pytest.param(
            finished_task(support.PARENT_ID, closed_at="2030-03-10T12:00:00+00:00"),
            {},
            None,
            "parent chain is closed",
            id="closed-parent",
        ),
        pytest.param(
            finished_task(support.PARENT_ID),
            {},
            fail_usage_lookup,
            re.escape(common.USAGE_ERROR),
            id="usage-lookup-fails",
        ),
    ],
)
def test_refused_run_after_keeps_finished_task_and_progress(
    installed_launch_agent,
    ledger_path,
    monkeypatch,
    parent,
    child_fields,
    next_reset_time,
    error,
):
    if next_reset_time is not None:
        monkeypatch.setattr(claude, "next_reset_time", next_reset_time)
    tasks = {
        support.PARENT_ID: parent,
        support.CHILD_ID: finished_task(support.CHILD_ID, **child_fields),
    }
    support.write_tasks(ledger_path, tasks)
    progress_path = progress_log.progress_path(tasks[support.CHILD_ID]["session_id"])
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text("progress", encoding="utf-8")

    with pytest.raises(SystemExit, match=error):
        cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])

    assert support.read_tasks(ledger_path) == tasks
    assert progress_path.exists()


@pytest.mark.parametrize(
    ("cancel_id", "expected_statuses"),
    [
        pytest.param(
            support.PARENT_ID,
            {
                support.PARENT_ID: ledger.STATUS_CANCELLED,
                support.CHILD_ID: ledger.STATUS_CANCELLED,
                support.GRANDCHILD_ID: ledger.STATUS_CANCELLED,
                support.SIBLING_ID: ledger.STATUS_CANCELLED,
            },
            id="scheduled-parent",
        ),
        pytest.param(
            support.CHILD_ID,
            {
                support.PARENT_ID: ledger.STATUS_SCHEDULED,
                support.CHILD_ID: ledger.STATUS_CANCELLED,
                support.GRANDCHILD_ID: ledger.STATUS_CANCELLED,
                support.SIBLING_ID: ledger.STATUS_WAITING,
            },
            id="waiting-child",
        ),
    ],
)
def test_cancelling_task_cancels_waiting_dependents(
    installed_launch_agent, ledger_path, cancel_id, expected_statuses
):
    support.create_waiting_chain()

    assert cli.main(["cancel", cancel_id]) == 0
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task_id: task["status"] for task_id, task in tasks.items()} == expected_statuses


def test_after_successful_parent_schedules_task_normally(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    cli.main(["run", support.PARENT_PROMPT, "--now"])
    parent = support.read_task(ledger_path, support.PARENT_ID)
    cli.main(["_execute", support.PARENT_ID, parent["session_id"]])

    cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])
    child = support.read_task(ledger_path, support.CHILD_ID)
    assert child["status"] == ledger.STATUS_SCHEDULED
    assert datetime.fromisoformat(child["due_at"]) == support.RESET_TIME


@pytest.mark.parametrize("status", [None, ledger.STATUS_CANCELLED])
def test_after_refuses_missing_or_cancelled_parent(installed_launch_agent, ledger_path, status):
    if status is not None:
        support.write_task(ledger_path, status, task_id=support.PARENT_ID)

    with pytest.raises(SystemExit, match=support.PARENT_ID):
        cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])

    assert support.CHILD_ID not in ledger.load_tasks()


def test_cancel_marks_task_cancelled_and_does_not_run_executor(
    installed_launch_agent, fake_commands, ledger_path
):
    cli.main(["run", support.PROMPT, "--now"])
    task = support.read_task(ledger_path)
    cli.main(["cancel", support.TASK_ID])

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "status",
    [ledger.STATUS_RUNNING, ledger.STATUS_BLOCKED, ledger.STATUS_FAILED, ledger.STATUS_FINISHED],
)
def test_cancel_refuses_non_scheduled_tasks(ledger_path, status):
    support.write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["cancel", support.TASK_ID])


@pytest.mark.parametrize(
    ("retry_args", "reset_time", "expected_due_at"),
    [
        pytest.param(["--now"], None, None, id="now"),
        pytest.param(["--at", support.AT_TEXT], None, support.RESET_TIME, id="at"),
        pytest.param([], None, None, id="no-usage-window"),
        pytest.param([], support.RESET_TIME, support.RESET_TIME, id="usage-window"),
    ],
)
def test_retry_runs_failed_task_in_new_session(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    retry_args,
    reset_time,
    expected_due_at,
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    cli.main(["run", support.PROMPT, "--now", "--", *support.AGENT_ARGS])
    failed = support.read_task(ledger_path)
    cli.main(["_execute", support.TASK_ID, failed["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: reset_time)
    monkeypatch.setattr(scheduling, "scheduled_time", lambda _: support.RESET_TIME)

    assert cli.main(["retry", support.TASK_ID, *retry_args]) == 0
    retry = support.read_task(ledger_path)
    assert retry["status"] == ledger.STATUS_SCHEDULED
    assert retry["session_id"] != failed["session_id"]
    due_at = datetime.fromisoformat(retry["due_at"])
    if expected_due_at is None:
        assert due_at <= datetime.now(UTC)
    else:
        assert due_at == expected_due_at
    support.set_task_due_now(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, retry["session_id"]]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED
    assert support.logged_claude_args(fake_commands) == support.expected_claude_args(retry)


@pytest.mark.parametrize("status", [None, ledger.STATUS_RUNNING])
def test_reschedule_refuses_unknown_and_non_scheduled_tasks(ledger_path, monkeypatch, status):
    if status is not None:
        support.write_task(ledger_path, status)
    monkeypatch.setattr(
        claude, "next_reset_time", lambda _environment: pytest.fail("usage lookup ran")
    )

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["reschedule", support.TASK_ID])


def test_reschedule_changes_only_due_time(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    support.write_task(
        ledger_path,
        ledger.STATUS_SCHEDULED,
        result=support.USAGE_LIMIT_RESULT,
        usage_limit_resume_count=1,
    )
    original_task = support.read_task(ledger_path)
    later = (datetime.now(UTC) + timedelta(days=1)).replace(second=0, microsecond=0)
    monkeypatch.setattr(scheduling, "scheduled_time", lambda _: later)

    assert cli.main(["reschedule", support.TASK_ID, "--at", support.AT_TEXT]) == 0
    assert cli.main(["_execute", support.TASK_ID, original_task["session_id"]]) == 0
    rescheduled_task = support.read_task(ledger_path)
    assert datetime.fromisoformat(rescheduled_task["due_at"]) == later
    assert {name: value for name, value in rescheduled_task.items() if name != "due_at"} == {
        name: value for name, value in original_task.items() if name != "due_at"
    }

    assert cli.main(["reschedule", support.TASK_ID, "--now"]) == 0
    assert cli.main(["_execute", support.TASK_ID, original_task["session_id"]]) == 0
    assert support.logged_claude_args(fake_commands) == [
        "--resume",
        original_task["session_id"],
        "--print",
        common.USAGE_LIMIT_RESUME_PROMPT,
        "--output-format",
        "stream-json",
        claude.VERBOSE_ARGUMENT,
        "--name",
        support.TASK_ID,
        support.SKIP_PERMISSIONS,
    ]


def test_retry_reuses_task_worktree_and_saved_environment(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setenv("LANG", support.SAVED_LANG)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    cli.main(["run", support.PROMPT, "--now"])
    first_session_id = support.read_task(ledger_path)["session_id"]
    cli.main(["_execute", support.TASK_ID, first_session_id])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setenv("LANG", support.CURRENT_LANG)

    cli.main(["retry", support.TASK_ID, "--now"])
    cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]])

    assert Path(support.logged_claude_cwds(fake_commands)[-1]).name == first_session_id
    assert support.WORKTREE_FLAG not in support.logged_claude_args(fake_commands)
    assert fake_commands["claude-lang"].read_text(encoding="utf-8").splitlines() == [
        support.SAVED_LANG,
        support.SAVED_LANG,
    ]
