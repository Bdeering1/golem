import json
import stat
import sys
from pathlib import Path

import pytest

from golem import cli, launch_agent, ledger, spawn
from golem.backends import claude_hooks
from tests import support

USER_SETTINGS_MODE = 0o644
USER_SETTINGS = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "echo user-stop"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "echo user-notify"}]}],
    },
}


def settings_file():
    return claude_hooks.local_settings_path(str(Path.cwd()))


def stop_gated_chain(ledger_path, fake_commands, tmp_path, original_settings):
    if original_settings is not None:
        settings_file().parent.mkdir()
        settings_file().write_text(json.dumps(original_settings), encoding="utf-8")
        settings_file().chmod(USER_SETTINGS_MODE)
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    return support.run_gated_chain(
        ledger_path, support.gate_command(tmp_path, exit_code=1, stderr=support.GATE_STOP_REASON)
    )


def finish_chain(ledger_path, parent):
    tasks = support.read_tasks(ledger_path)
    tasks[support.PARENT_ID]["status"] = ledger.STATUS_DONE
    del tasks[support.PARENT_ID]["gate_stop_reason"]
    support.write_tasks(ledger_path, tasks)
    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0


@pytest.mark.parametrize("original_settings", [None, USER_SETTINGS], ids=["no-file", "existing"])
@pytest.mark.parametrize("action", ["cancel", "delete", "finish"])
def test_hooks_are_seeded_when_chain_stops_and_removed_when_it_ends(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, original_settings, action
):
    parent = stop_gated_chain(ledger_path, fake_commands, tmp_path, original_settings)

    seeded = json.loads(settings_file().read_text(encoding="utf-8"))
    seeded_hooks = seeded["hooks"]
    assert {
        event: len(groups)
        for event, groups in seeded_hooks.items()
        if event in claude_hooks.HOOK_EVENT_ARGUMENTS
    } == {
        "Stop": 1 + (original_settings is not None),
        "PreToolUse": 1,
    }
    assert seeded_hooks["PreToolUse"][0]["matcher"] == claude_hooks.HOOK_TOOL_MATCHER
    if original_settings is not None:
        assert seeded["permissions"] == original_settings["permissions"]
        assert seeded_hooks["Stop"][0] == original_settings["hooks"]["Stop"][0]
        assert seeded_hooks["Notification"] == original_settings["hooks"]["Notification"]

    if action == "finish":
        finish_chain(ledger_path, parent)
    else:
        assert cli.main([action, support.CHILD_ID]) == 0

    if original_settings is None:
        assert not settings_file().parent.exists()
        return
    assert json.loads(settings_file().read_text(encoding="utf-8")) == original_settings
    assert stat.S_IMODE(settings_file().stat().st_mode) == USER_SETTINGS_MODE


@pytest.mark.parametrize("is_symlink", [False, True], ids=["not-json", "symlink"])
def test_unusable_hook_settings_are_left_alone(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, capsys, is_symlink
):
    settings_file().parent.mkdir()
    if is_symlink:
        shared_settings_path = tmp_path / "shared-settings.json"
        shared_settings_path.write_text("{}", encoding="utf-8")
        settings_file().symlink_to(shared_settings_path)
    else:
        settings_file().write_text("{not json", encoding="utf-8")
    original_text = settings_file().read_text(encoding="utf-8")
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    support.run_gated_chain(
        ledger_path, support.gate_command(tmp_path, exit_code=1, stderr=support.GATE_STOP_REASON)
    )

    assert settings_file().is_symlink() == is_symlink
    assert settings_file().read_text(encoding="utf-8") == original_text
    assert "hooks not installed" in capsys.readouterr().err


def write_settings(settings):
    settings_file().parent.mkdir()
    settings_file().write_text(json.dumps(settings), encoding="utf-8")


def test_install_hooks_replaces_stale_golem_command():
    stale_command = f"/old/python /old/golem.py _hook stop{claude_hooks.HOOK_FAILURE_SUFFIX}"
    stale_entry = {"type": "command", "command": stale_command}
    write_settings({"hooks": {"Stop": [{"hooks": [stale_entry]}]}})

    claude_hooks.install_hooks(str(Path.cwd()))

    stop_groups = json.loads(settings_file().read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert stop_groups == [claude_hooks.hook_group(claude_hooks.HOOK_EVENT_STOP)]


def test_remove_hooks_keeps_user_entry_sharing_a_group():
    user_entry = {"type": "command", "command": "echo user-stop"}
    golem_entry = claude_hooks.group_entries(claude_hooks.hook_group(claude_hooks.HOOK_EVENT_STOP))[
        0
    ]
    write_settings({"hooks": {"Stop": [{"hooks": [user_entry, golem_entry]}]}})

    claude_hooks.remove_hooks(str(Path.cwd()))

    assert json.loads(settings_file().read_text(encoding="utf-8")) == {
        "hooks": {"Stop": [{"hooks": [user_entry]}]}
    }


@pytest.mark.parametrize(
    ("parent_status", "child_status", "is_needed"),
    [
        (ledger.STATUS_DONE, ledger.STATUS_RUNNING, True),
        (ledger.STATUS_DONE, ledger.STATUS_SCHEDULED, True),
        (ledger.STATUS_DONE, ledger.STATUS_NEEDS_REVIEW, False),
        (ledger.STATUS_FAILED, ledger.STATUS_WAITING, False),
    ],
    ids=["follow-up-running", "follow-up-paused", "follow-up-finished", "parent-failed"],
)
def test_hooks_stay_until_follow_up_finishes(ledger_path, parent_status, child_status, is_needed):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                parent_status, task_id=support.PARENT_ID, gate="true"
            ),
            support.CHILD_ID: support.task_record(
                child_status, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
        },
    )

    claude_hooks.sync_hooks(str(Path.cwd()))

    assert settings_file().exists() == is_needed


@pytest.mark.parametrize(
    "requeue_arguments",
    [
        ["run", support.GRANDCHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID],
        ["retry", support.CHILD_ID, "--now"],
    ],
    ids=["run-after", "retry"],
)
def test_follow_up_requeued_after_stopped_task_seeds_hooks(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, requeue_arguments
):
    stop_gated_chain(ledger_path, fake_commands, tmp_path, None)
    assert cli.main(["cancel", support.CHILD_ID]) == 0
    assert not settings_file().exists()

    assert cli.main(requeue_arguments) == 0

    assert settings_file().exists()


def test_launch_agent_pass_removes_hooks_of_abandoned_follow_up(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_DONE, task_id=support.PARENT_ID, gate="true"
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_RUNNING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
        },
    )
    claude_hooks.sync_hooks(str(Path.cwd()))
    assert settings_file().exists()

    launch_agent.run_launch_agent_pass()

    assert not settings_file().exists()


def test_ungated_chain_gets_no_hooks(installed_launch_agent, fake_commands, ledger_path):
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    cli.main(
        [
            "run",
            support.PARENT_ID,
            support.PARENT_PROMPT,
            "--now",
            "--then",
            support.CHILD_ID,
            support.CHILD_PROMPT,
        ]
    )
    parent = support.read_task(ledger_path, support.PARENT_ID)
    cli.main(["_execute", support.PARENT_ID, parent["session_id"]])

    assert not settings_file().parent.exists()


def test_hook_commands_run_without_path_or_state_environment(ledger_path):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_DONE, task_id=support.PARENT_ID, gate="true"
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_RUNNING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
        },
    )
    claude_hooks.sync_hooks(str(Path.cwd()))
    hooks = json.loads(settings_file().read_text(encoding="utf-8"))["hooks"]
    command = hooks["PreToolUse"][0]["hooks"][0]["command"]

    result = spawn.subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({"session_id": support.PARENT_SESSION}),
        env={},
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_command_does_not_block_claude_when_golem_is_missing(monkeypatch):
    monkeypatch.setattr(spawn, "golem_command", lambda: [sys.executable, "/missing/golem.py"])

    result = spawn.subprocess.run(
        ["/bin/sh", "-c", claude_hooks.hook_command_line(claude_hooks.HOOK_ARGUMENT_PRE_TOOL_USE)],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 2
