import pytest

from golem import cli, ledger, locks
from golem.backends import claude
from tests import support


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pickup"], id="pickup"),
        pytest.param(["pickup", "--client", "claude-code"], id="pickup-claude-code"),
        pytest.param(["pickup", "--client", "claude-desktop"], id="pickup-app"),
        pytest.param(["close"], id="close"),
    ],
)
def test_review_commands_complete_review_task(installed_launch_agent, ledger_path, command):
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    task = support.read_task(ledger_path)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])

    assert cli.main([*command, support.TASK_ID]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


@pytest.mark.parametrize(
    "status", [ledger.STATUS_SCHEDULED, ledger.STATUS_WAITING, ledger.STATUS_CANCELLED]
)
@pytest.mark.parametrize(
    "pickup_args",
    [pytest.param([], id="terminal"), pytest.param(["--client", "claude-desktop"], id="app")],
)
def test_pickup_refuses_tasks_that_never_started(ledger_path, status, pickup_args):
    support.write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["pickup", support.TASK_ID, *pickup_args])


def test_pickup_app_opens_task_session_in_desktop_app(fake_commands, ledger_path):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    assert cli.main(["pickup", support.TASK_ID, "--client", "claude-desktop"]) == 0
    assert (
        fake_commands["open-args"].read_text(encoding="utf-8")
        == f"claude://resume?session={support.read_task(ledger_path)['session_id']}\n"
    )
    assert support.logged_claude_calls(fake_commands) == []


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pickup", support.TASK_ID, "--client", "claude-desktop"], id="one"),
        pytest.param(["pickup", "--all", "--client", "claude-desktop"], id="all"),
    ],
)
def test_failed_app_pickup_leaves_task_needing_review(
    fake_commands, ledger_path, monkeypatch, command
):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)
    monkeypatch.setenv("GOLEM_OPEN_EXIT", "1")

    assert cli.main(command) == 1
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={support.read_task(ledger_path)['session_id']}\n"
    )


def test_pickup_app_rejects_agent_arguments(fake_commands, ledger_path):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            ["pickup", support.TASK_ID, "--client", "claude-desktop", "--", *support.AGENT_ARGS]
        )

    assert exit_info.value.code == 2
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            ["pickup", support.TASK_ID, "--all", "--client", "claude-desktop"], id="all-rejects-id"
        ),
        pytest.param(["pickup", "--client", "claude-desktop"], id="needs-id-or-all"),
        pytest.param(
            ["reschedule", support.TASK_ID, "--now", "--", *support.AGENT_ARGS],
            id="reschedule-agent-args",
        ),
    ],
)
def test_pickup_all_and_reschedule_validate_arguments(command):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(command)

    assert exit_info.value.code == 2


def test_pickup_marks_ancestors_that_need_review_done(fake_commands, ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_NEEDS_REVIEW, task_id=support.PARENT_ID
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_NEEDS_REVIEW, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.GRANDCHILD_ID: support.task_record(
                ledger.STATUS_NEEDS_REVIEW, task_id=support.GRANDCHILD_ID, after=support.CHILD_ID
            ),
        },
    )

    assert cli.main(["pickup", support.GRANDCHILD_ID, "--client", "claude-desktop"]) == 0
    assert {task["status"] for task in support.read_tasks(ledger_path).values()} == {
        ledger.STATUS_DONE
    }


@pytest.mark.parametrize(
    ("parent_fields", "child_status", "opened_ids"),
    [
        pytest.param({}, ledger.STATUS_NEEDS_REVIEW, [support.CHILD_ID], id="child-needs-review"),
        pytest.param({}, ledger.STATUS_RUNNING, [], id="child-running"),
        pytest.param({}, ledger.STATUS_FAILED, [support.PARENT_ID], id="child-failed"),
        pytest.param(
            {"gate_stop_reason": support.GATE_STOP_REASON},
            ledger.STATUS_WAITING,
            [support.PARENT_ID],
            id="parent-stopped",
        ),
        pytest.param(
            {"gate_reason": support.GATE_STOP_REASON},
            ledger.STATUS_WAITING,
            [support.PARENT_ID],
            id="parent-stopped-in-old-ledger",
        ),
    ],
)
def test_pickup_all_opens_only_latest_reviewable_task_in_chain(
    fake_commands, ledger_path, parent_fields, child_status, opened_ids
):
    tasks = {
        support.PARENT_ID: support.task_record(
            ledger.STATUS_NEEDS_REVIEW, task_id=support.PARENT_ID, **parent_fields
        ),
        support.CHILD_ID: support.task_record(
            child_status, task_id=support.CHILD_ID, after=support.PARENT_ID
        ),
    }
    support.write_tasks(ledger_path, tasks)

    assert cli.main(["pickup", "--all", "--client", "claude-desktop"]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines() == [
        f"claude://resume?session={tasks[task_id]['session_id']}" for task_id in opened_ids
    ]


def test_pickup_all_continues_after_busy_directory(fake_commands, ledger_path, capsys, tmp_path):
    busy_directory = tmp_path / "busy"
    other_directory = tmp_path / "other"
    busy_directory.mkdir()
    other_directory.mkdir()
    tasks = {
        "busy": support.task_record(
            ledger.STATUS_NEEDS_REVIEW, task_id="busy", cwd=str(busy_directory)
        ),
        "other": support.task_record(
            ledger.STATUS_NEEDS_REVIEW, task_id="other", cwd=str(other_directory)
        ),
    }
    support.write_tasks(ledger_path, tasks)
    directory_lock_file = locks.acquire_lock(locks.directory_lock_path(str(busy_directory)))
    assert directory_lock_file is not None

    try:
        assert cli.main(["pickup", "--all", "--client", "claude-desktop"]) == 1
    finally:
        locks.release_lock(directory_lock_file)

    assert f"golem: directory is busy: {busy_directory}" in capsys.readouterr().err
    assert support.read_task(ledger_path, "busy")["status"] == ledger.STATUS_NEEDS_REVIEW
    assert support.read_task(ledger_path, "other")["status"] == ledger.STATUS_DONE
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={tasks['other']['session_id']}\n"
    )


def test_auto_pickup_waits_for_usage_limit_resume(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_claude_output(
        fake_commands, support.USAGE_LIMIT_RESULT, api_error_status=claude.USAGE_LIMIT_HTTP_STATUS
    )
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--auto-pickup"])
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""
    assert support.logged_osascript_calls(fake_commands) == []

    support.set_task_due_now(ledger_path)
    support.write_claude_output(fake_commands, support.PROMPT)
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={task['session_id']}\n"
    )


@pytest.mark.parametrize(
    ("parent_status", "expected_open_args"),
    [
        pytest.param(
            ledger.STATUS_NEEDS_REVIEW,
            f"claude://resume?session={support.PARENT_ID}-session\n",
            id="unreviewed-parent",
        ),
        pytest.param(ledger.STATUS_DONE, "", id="reviewed-parent"),
    ],
)
def test_auto_pickup_opens_parent_when_resumed_follow_up_fails(
    fake_commands, ledger_path, monkeypatch, parent_status, expected_open_args
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                parent_status, task_id=support.PARENT_ID, auto_pickup=True
            ),
            support.TASK_ID: support.task_record(
                ledger.STATUS_SCHEDULED, after=support.PARENT_ID, auto_pickup=True
            ),
        },
    )

    assert cli.main(["_execute", support.TASK_ID, f"{support.TASK_ID}-session"]) == 1
    tasks = support.read_tasks(ledger_path)
    assert tasks[support.TASK_ID]["status"] == ledger.STATUS_FAILED
    assert tasks[support.PARENT_ID]["status"] == ledger.STATUS_DONE
    assert fake_commands["open-args"].read_text(encoding="utf-8") == expected_open_args
