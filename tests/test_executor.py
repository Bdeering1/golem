import json
import os
import signal
from datetime import datetime
from typing import Any, Never

import pytest

from golem import cli, executor, launch_agent, ledger, locks, scheduling, spawn
from golem.backends import claude, common
from tests import support

BACKGROUND_STOP_LINE = (
    'Background tasks still running 10m after the last turn (workflow "Implement plan"); '
    "stopping them. Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely."
)


def raise_usage_lookup_error(_environment: dict[str, str]) -> Never:
    raise SystemExit


@pytest.mark.parametrize("is_retried", [False, True])
def test_dependent_runs_in_parent_worktree_with_its_own_environment(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, is_retried
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setenv("LANG", support.SAVED_LANG)
    monkeypatch.setenv("LC_TIME", support.SAVED_LANG)
    cli.main(["run", support.PARENT_ID, support.PARENT_PROMPT, "--now"])
    monkeypatch.setenv("LANG", support.CHILD_LANG)
    monkeypatch.delenv("LC_TIME")
    cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID])
    monkeypatch.setenv("LANG", support.CURRENT_LANG)
    monkeypatch.setenv("LC_TIME", support.CURRENT_LANG)
    if is_retried:
        cli.main(["cancel", support.CHILD_ID])
        cli.main(["retry", support.CHILD_ID, "--now"])

    cli.main(
        [
            "_execute",
            support.PARENT_ID,
            support.read_task(ledger_path, support.PARENT_ID)["session_id"],
        ]
    )
    if is_retried:
        cli.main(
            [
                "_execute",
                support.CHILD_ID,
                support.read_task(ledger_path, support.CHILD_ID)["session_id"],
            ]
        )

    parent_worktree = support.read_task(ledger_path, support.PARENT_ID)["worktree"]
    child_arguments = support.logged_claude_args(fake_commands)
    assert support.logged_claude_cwds(fake_commands)[-1] == parent_worktree
    assert support.WORKTREE_FLAG not in child_arguments
    assert fake_commands["claude-lang"].read_text(encoding="utf-8").splitlines() == [
        support.SAVED_LANG,
        support.CHILD_LANG,
    ]
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8").splitlines() == [
        support.SAVED_LANG,
        "",
    ]


def test_finished_task_records_its_branch(
    installed_launch_agent, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    support.git_output(git_repository, "checkout", "-qb", "feature")
    support.add_github_remote(git_repository)
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--no-worktree"])

    cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]])

    assert support.read_task(ledger_path)["repository_branches"] == [
        {"repository": "owner/repository", "branch": "feature"}
    ]


@pytest.mark.parametrize(
    ("exit_code", "output", "expected_result", "expected_status"),
    [
        pytest.param(0, '{"result": "done"}', "done", ledger.STATUS_NEEDS_REVIEW, id="success"),
        pytest.param(1, '{"result": "done"}', "done", ledger.STATUS_FAILED, id="failure"),
        pytest.param(
            0,
            '[{"type": "system"}, {"type": "result", "result": "done"}]',
            "done",
            ledger.STATUS_NEEDS_REVIEW,
            id="verbose",
        ),
        pytest.param(0, "plain output", "plain output", ledger.STATUS_NEEDS_REVIEW, id="plain"),
    ],
)
def test_run_now_records_claude_result(
    installed_launch_agent,
    fake_commands,
    monkeypatch,
    ledger_path,
    capsys,
    exit_code,
    output,
    expected_result,
    expected_status,
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", str(exit_code))
    fake_commands["claude-stdout"].write_text(output, encoding="utf-8")
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--", *support.AGENT_ARGS])
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == exit_code
    task = support.read_task(ledger_path)
    assert task["status"] == expected_status
    assert task["returncode"] == exit_code
    assert task["result"] == expected_result
    assert support.logged_claude_args(fake_commands) == support.expected_claude_args(task)
    capsys.readouterr()
    assert cli.main(["show", support.TASK_ID]) == 0
    assert capsys.readouterr().out == f"{expected_result}\n"


@pytest.mark.parametrize(
    ("settings_ceiling", "parent_status", "parent_result", "dependent_status", "call_count"),
    [
        pytest.param(
            None,
            ledger.STATUS_DONE,
            support.CLAUDE_RESULT,
            ledger.STATUS_NEEDS_REVIEW,
            2,
            id="background-work-completes",
        ),
        pytest.param(
            "600000",
            ledger.STATUS_FAILED,
            f"{support.CLAUDE_RESULT}\n\n{BACKGROUND_STOP_LINE}",
            ledger.STATUS_WAITING,
            1,
            id="background-work-stopped",
        ),
    ],
)
def test_background_work_waits_or_fails_before_starting_dependents(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    capsys,
    settings_ceiling,
    parent_status,
    parent_result,
    dependent_status,
    call_count,
):
    monkeypatch.setenv("GOLEM_CLAUDE_BACKGROUND_STOP", BACKGROUND_STOP_LINE)
    if settings_ceiling is not None:
        monkeypatch.setenv("GOLEM_CLAUDE_SETTINGS_CEILING", settings_ceiling)
    cli.main(["run", support.PARENT_ID, support.PARENT_PROMPT, "--now"])
    cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID])
    parent_session_id = support.read_task(ledger_path, support.PARENT_ID)["session_id"]
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)

    assert cli.main(["_execute", support.PARENT_ID, parent_session_id]) == 0

    parent = support.read_task(ledger_path, support.PARENT_ID)
    assert parent["status"] == parent_status
    assert parent["result"] == parent_result
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == dependent_status
    assert len(support.logged_claude_calls(fake_commands)) == call_count
    assert (BACKGROUND_STOP_LINE in capsys.readouterr().err) is (settings_ceiling is not None)


@pytest.mark.parametrize("is_verbose", [False, True])
def test_usage_limited_task_resumes_at_reset_in_same_session(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path, is_verbose
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    agent_args = (
        [*support.AGENT_ARGS, claude.VERBOSE_ARGUMENT] if is_verbose else support.AGENT_ARGS
    )
    support.write_claude_output(
        fake_commands,
        support.USAGE_LIMIT_RESULT,
        api_error_status=claude.USAGE_LIMIT_HTTP_STATUS,
        is_verbose=is_verbose,
    )
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--", *agent_args])
    session_id = support.read_task(ledger_path)["session_id"]
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")

    assert cli.main(["_execute", support.TASK_ID, session_id]) == 0
    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_SCHEDULED
    assert task["result"] == support.USAGE_LIMIT_RESULT
    assert datetime.fromisoformat(task["due_at"]) == support.RESET_TIME
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT, is_verbose=is_verbose)
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    support.set_task_due_now(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, session_id]) == 0

    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_NEEDS_REVIEW
    assert task["result"] == support.CLAUDE_RESULT
    assert task["session_id"] == session_id
    assert support.logged_claude_args(fake_commands) == [
        "--resume",
        session_id,
        "--print",
        common.USAGE_LIMIT_RESUME_PROMPT,
        "--output-format",
        "stream-json",
        *agent_args,
        *([] if is_verbose else [claude.VERBOSE_ARGUMENT]),
        "--name",
        support.TASK_ID,
        support.SKIP_PERMISSIONS,
    ]


def usage_limit_assistant_event(parent_tool_use_id):
    return {
        "type": "assistant",
        "error": claude.USAGE_LIMIT_ASSISTANT_ERROR,
        "parent_tool_use_id": parent_tool_use_id,
        "message": {"content": [{"type": "text", "text": support.USAGE_LIMIT_RESULT}]},
    }


def test_subagent_usage_limit_event_does_not_pause_task(fake_commands, ledger_path):
    support.write_claude_events(
        fake_commands,
        [
            usage_limit_assistant_event("toolu_subagent"),
            {"type": "result", "api_error_status": None, "result": support.CLAUDE_RESULT},
        ],
    )
    support.write_task(ledger_path, ledger.STATUS_RUNNING)

    executor.execute_task(support.TASK_ID, support.read_task(ledger_path), ())

    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_NEEDS_REVIEW
    assert task["result"] == support.CLAUDE_RESULT


@pytest.mark.parametrize(
    "limit_event",
    [
        {
            "type": "result",
            "is_error": True,
            "api_error_status": claude.USAGE_LIMIT_HTTP_STATUS,
            "result": support.USAGE_LIMIT_RESULT,
        },
        usage_limit_assistant_event(None),
    ],
    ids=["result-event", "assistant-event"],
)
def test_usage_limit_pauses_task_when_background_agent_turns_follow(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path, limit_event
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_claude_output(fake_commands, support.CLAUDE_RESULT)
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--", *support.AGENT_ARGS])
    session_id = support.read_task(ledger_path)["session_id"]
    background_turn = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "background agent finished"}]},
    }
    empty_result = {"type": "result", "is_error": False, "api_error_status": None, "result": ""}
    support.write_claude_events(fake_commands, [limit_event, background_turn, empty_result])
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")

    assert cli.main(["_execute", support.TASK_ID, session_id]) == 0

    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_SCHEDULED
    assert task["result"] == support.USAGE_LIMIT_RESULT
    assert datetime.fromisoformat(task["due_at"]) == support.RESET_TIME
    assert task["session_id"] == session_id


def test_usage_limited_task_fails_once_resumes_run_out(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_claude_output(
        fake_commands, support.USAGE_LIMIT_RESULT, api_error_status=claude.USAGE_LIMIT_HTTP_STATUS
    )
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    task = support.read_task(ledger_path)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])
    for _ in range(executor.MAX_USAGE_LIMIT_RESUMES):
        task = support.read_task(ledger_path)
        assert task["status"] == ledger.STATUS_SCHEDULED
        support.set_task_due_now(ledger_path)
        cli.main(["_execute", support.TASK_ID, task["session_id"]])

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FAILED


@pytest.mark.parametrize(
    "find_reset_time",
    [
        pytest.param(lambda _environment: None, id="no-reset"),
        pytest.param(raise_usage_lookup_error, id="lookup-error"),
    ],
)
def test_usage_limited_task_fails_when_it_cannot_be_rescheduled(
    fake_commands, ledger_path, monkeypatch, find_reset_time
):
    monkeypatch.setattr(claude, "next_reset_time", find_reset_time)
    support.write_claude_output(
        fake_commands, support.USAGE_LIMIT_RESULT, api_error_status=claude.USAGE_LIMIT_HTTP_STATUS
    )
    support.write_task(ledger_path, ledger.STATUS_RUNNING)

    executor.execute_task(support.TASK_ID, support.read_task(ledger_path), ())
    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_FAILED
    assert task["result"] == support.USAGE_LIMIT_RESULT


def test_scheduled_task_runs_when_executed(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setattr(scheduling, "scheduled_time", lambda _: support.RESET_TIME)
    cli.main(["run", support.TASK_ID, support.PROMPT, "--at", support.AT_TEXT])
    task = support.read_task(ledger_path)
    assert datetime.fromisoformat(task["due_at"]) == support.RESET_TIME
    support.set_task_due_now(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW


def test_leftover_executor_does_not_run_retried_task(
    installed_launch_agent, fake_commands, ledger_path
):
    support.write_task(ledger_path, ledger.STATUS_CANCELLED, session_id="old")
    cli.main(["retry", support.TASK_ID, "--at", support.AT_TEXT])

    assert cli.main(["_execute", support.TASK_ID, "old"]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


def test_executor_death_keeps_task_running_until_claude_exits(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    task = support.read_task(ledger_path)
    executor_process = spawn.start_task_executor(support.TASK_ID, task)

    try:
        support.wait_until(started_path.exists)
        os.kill(executor_process.pid, signal.SIGKILL)
        executor_process.wait()

        launch_agent.run_launch_agent_pass()
        assert support.read_task(ledger_path)["status"] == ledger.STATUS_RUNNING
        assert locks.acquire_lock(locks.directory_lock_path(task["cwd"])) is None
        with pytest.raises(SystemExit, match="cannot be retried"):
            cli.main(["retry", support.TASK_ID, "--now"])

        hold_path.unlink()

        support.wait_until(lambda: locks.is_run_lock_free(task["session_id"]))
        launch_agent.run_launch_agent_pass()
        assert support.read_task(ledger_path)["status"] == ledger.STATUS_FAILED
        assert [call[-2:] for call in support.logged_osascript_calls(fake_commands)] == [
            [f"golem: {support.TASK_ID} failed", support.PROMPT]
        ]
    finally:
        hold_path.unlink(missing_ok=True)
        if executor_process.poll() is None:
            executor_process.kill()
            executor_process.wait()


def test_executor_frees_worktree_held_by_stray_claude_process(ledger_path, monkeypatch, tmp_path):
    stray_path = tmp_path / "stray"
    stray_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_STRAY", str(stray_path))
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED)
    task = support.read_task(ledger_path)

    try:
        cli.main(["_execute", support.TASK_ID, task["session_id"]])
        directory_lock_file = locks.acquire_lock(locks.directory_lock_path(task["cwd"]))
    finally:
        stray_path.unlink()

    assert directory_lock_file is not None
    locks.release_lock(directory_lock_file)


@pytest.mark.parametrize("first_dependent", ["busy", "missing"])
def test_dependent_drain_stops_for_busy_worktrees_and_skips_missing_worktrees(
    fake_commands, ledger_path, monkeypatch, tmp_path, first_dependent
):
    first_worktree = tmp_path / "first"
    second_worktree = tmp_path / "second"
    second_worktree.mkdir()
    if first_dependent == "busy":
        first_worktree.mkdir()
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_NEEDS_REVIEW, task_id=support.PARENT_ID, worktree=str(first_worktree)
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_WAITING,
                task_id=support.SIBLING_ID,
                after=support.PARENT_ID,
                worktree=str(second_worktree),
            ),
        },
    )
    run_task_ids: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        run_task_ids.append(task_id)
        return 0, json.dumps({"result": support.CLAUDE_RESULT}), ""

    monkeypatch.setattr(claude, "run_claude", run_claude)
    directory_lock_file = None
    if first_dependent == "busy":
        directory_lock_file = locks.acquire_lock(locks.directory_lock_path(str(first_worktree)))
    try:
        executor.drain_dependents(
            support.PARENT_ID, support.read_task(ledger_path, support.PARENT_ID)["session_id"]
        )
    finally:
        if directory_lock_file is not None:
            locks.release_lock(directory_lock_file)

    if first_dependent == "busy":
        assert run_task_ids == []
        assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING
        assert support.read_task(ledger_path, support.SIBLING_ID)["status"] == ledger.STATUS_WAITING
        return
    assert run_task_ids == [support.SIBLING_ID]
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_FAILED
    assert support.read_task(ledger_path, support.CHILD_ID)["result"] == (
        f"golem: worktree is missing: {first_worktree}"
    )
    assert (
        support.read_task(ledger_path, support.SIBLING_ID)["status"] == ledger.STATUS_NEEDS_REVIEW
    )
    assert [notification[-2] for notification in support.logged_osascript_calls(fake_commands)] == [
        f"golem: {support.CHILD_ID} failed",
        f"golem: {support.SIBLING_ID} needs review",
    ]
