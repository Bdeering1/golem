import subprocess
import sys
import threading
from datetime import UTC, datetime

import pytest

from golem import cli, ledger, locks, progress_log, t3_code, task_processes
from tests import support

HELD_COMMAND = "sleep 30"
STARTED_AT = datetime(2030, 1, 1, tzinfo=UTC).isoformat()
JOIN_SECONDS = 15


@pytest.fixture
def hold_path(monkeypatch, tmp_path):
    path = tmp_path / "hold"
    path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(path))
    monkeypatch.setenv("GOLEM_CODEX_HOLD", str(path))
    yield path
    path.unlink(missing_ok=True)


def start_executor(ledger_path, task_id=support.TASK_ID):
    session_id = support.read_task(ledger_path, task_id)["session_id"]
    executor_thread = threading.Thread(target=cli.main, args=(["_execute", task_id, session_id],))
    executor_thread.start()
    return executor_thread


def wait_for_field(ledger_path, field, task_id=support.TASK_ID):
    support.wait_until(lambda: field in support.read_task(ledger_path, task_id))


def cancel_and_join(executor_thread, task_id=support.TASK_ID):
    assert cli.main(["cancel", task_id]) == 0
    executor_thread.join(JOIN_SECONDS)
    assert not executor_thread.is_alive()


@pytest.mark.parametrize(
    "backend", [ledger.BACKEND_CLAUDE, ledger.BACKEND_CODEX, ledger.BACKEND_T3]
)
def test_cancel_stops_running_agent_and_task_stays_cancelled(
    installed_launch_agent, fake_commands, ledger_path, hold_path, request, backend
):
    server = request.getfixturevalue("server") if backend == ledger.BACKEND_T3 else None
    if server is not None:
        (server.home / "send-hold").touch()
    cli.main(["run", support.PROMPT, "--backend", backend, "--now"])
    executor_thread = start_executor(ledger_path)
    wait_for_field(ledger_path, task_processes.FIELD_AGENT_PID)

    cancel_and_join(executor_thread)

    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_CANCELLED
    assert task_processes.FIELD_AGENT_PID not in task
    assert support.logged_osascript_calls(fake_commands) == []
    if server is not None:
        assert {"type": "thread.stop", "threadId": task["t3_code_thread_id"]} in [
            {name: value for name, value in command.items() if name != "commandId"}
            for command in server.commands
        ]


def test_cancel_kills_agent_that_ignores_terminate(
    installed_launch_agent, ledger_path, hold_path, monkeypatch, tmp_path
):
    started_path = tmp_path / "started"
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    monkeypatch.setenv("GOLEM_CLAUDE_IGNORE_TERM", "1")
    monkeypatch.setattr(task_processes, "AGENT_STOP_SECONDS", 0.2)
    cli.main(["run", support.PROMPT, "--now"])
    executor_thread = start_executor(ledger_path)
    support.wait_until(started_path.exists)
    wait_for_field(ledger_path, task_processes.FIELD_AGENT_PID)

    cancel_and_join(executor_thread)

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_CANCELLED


@pytest.mark.parametrize(
    ("phase_args", "expected_claude_calls"),
    [
        pytest.param(["--no-worktree", "--setup", HELD_COMMAND], 0, id="setup"),
        pytest.param(["--gate", HELD_COMMAND], 1, id="gate"),
    ],
)
def test_cancel_stops_command_and_cancels_waiting_dependents(
    installed_launch_agent, fake_commands, ledger_path, phase_args, expected_claude_calls
):
    cli.main(["run", support.PARENT_PROMPT, "--now", *phase_args])
    cli.main(["run", support.CHILD_PROMPT, "--after", support.PARENT_ID])
    executor_thread = start_executor(ledger_path)
    wait_for_field(ledger_path, task_processes.FIELD_COMMAND_PROCESS_GROUP)

    cancel_and_join(executor_thread)

    tasks = support.read_tasks(ledger_path)
    assert {task["status"] for task in tasks.values()} == {ledger.STATUS_CANCELLED}
    assert "setup_failure_reason" not in tasks[support.PARENT_ID]
    assert len(support.logged_claude_calls(fake_commands)) == expected_claude_calls
    assert support.logged_osascript_calls(fake_commands) == []


@pytest.mark.parametrize(
    "field", [task_processes.FIELD_AGENT_PID, task_processes.FIELD_COMMAND_PROCESS_GROUP]
)
def test_process_started_after_cancel_is_stopped(ledger_path, field):
    support.write_task(ledger_path, ledger.STATUS_CANCELLED)
    session_id = support.read_task(ledger_path)["session_id"]

    with (
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True, text=True
        ) as process,
        task_processes.recorded_process(support.TASK_ID, session_id, field, process),
    ):
        assert process.wait(timeout=JOIN_SECONDS) < 0


def test_retry_runs_task_cancelled_while_running(
    installed_launch_agent, fake_commands, ledger_path, hold_path
):
    cli.main(["run", support.PROMPT, "--now"])
    executor_thread = start_executor(ledger_path)
    wait_for_field(ledger_path, task_processes.FIELD_AGENT_PID)
    cancel_and_join(executor_thread)
    hold_path.unlink()

    assert cli.main(["retry", support.TASK_ID, "--now"]) == 0
    start_executor(ledger_path).join(JOIN_SECONDS)

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


@pytest.mark.parametrize(
    ("status", "expected_thread_stops"),
    [
        (ledger.STATUS_FAILED, []),
        (ledger.STATUS_BLOCKED, []),
        (ledger.STATUS_RUNNING, ["thread"]),
        (ledger.STATUS_FINISHED, []),
    ],
)
def test_cancel_without_live_executor_leaves_recorded_processes_alone(
    ledger_path, monkeypatch, status, expected_thread_stops
):
    support.write_parent_with_waiting_child(
        ledger_path,
        status,
        backend=ledger.BACKEND_T3,
        agent_pid=1,
        command_process_group=1,
        t3_code_thread_id="thread",
    )
    process_stops = []
    thread_stops = []
    monkeypatch.setattr(task_processes, "stop_agent", process_stops.append)
    monkeypatch.setattr(task_processes, "stop_command_process_group", process_stops.append)
    monkeypatch.setattr(t3_code, "stop_thread", thread_stops.append)

    assert cli.main(["cancel", support.PARENT_ID]) == 0

    statuses = {task["status"] for task in support.read_tasks(ledger_path).values()}
    assert statuses == {ledger.STATUS_CANCELLED}
    assert process_stops == []
    assert thread_stops == expected_thread_stops


@pytest.mark.parametrize("status", [ledger.STATUS_FINISHED, ledger.STATUS_CANCELLED])
def test_cancel_refuses_done_tasks(ledger_path, status):
    support.write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=f"cannot be cancelled: {support.TASK_ID}"):
        cli.main(["cancel", support.TASK_ID])

    assert support.read_task(ledger_path)["status"] == status


def test_cancel_reports_task_still_stopping(ledger_path, monkeypatch):
    support.write_task(ledger_path, ledger.STATUS_RUNNING)
    monkeypatch.setattr(task_processes, "RUN_LOCK_WAIT_SECONDS", 0.1)
    run_lock_file = locks.acquire_run_lock(support.read_task(ledger_path)["session_id"])
    assert run_lock_file is not None

    try:
        with pytest.raises(SystemExit, match=f"still stopping: {support.TASK_ID}"):
            cli.main(["cancel", support.TASK_ID])
    finally:
        locks.release_lock(run_lock_file)

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_CANCELLED


def test_pickup_and_watch_open_started_cancelled_task(ledger_path, capsys):
    support.write_task(ledger_path, ledger.STATUS_CANCELLED, started_at=STARTED_AT)
    progress_path = progress_log.progress_path(support.read_task(ledger_path)["session_id"])
    progress_path.parent.mkdir(parents=True)
    progress_path.write_text("progress\n", encoding="utf-8")

    assert cli.main(["pickup", support.TASK_ID]) == 0
    assert cli.main(["watch", support.TASK_ID]) == 0
    assert capsys.readouterr().out.endswith(f"progress\ngolem: task cancelled: {support.TASK_ID}\n")


def test_cancelling_running_dependent_still_runs_its_sibling(
    installed_launch_agent, fake_commands, ledger_path
):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(ledger.STATUS_FINISHED, no_worktree=True),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING,
                task_id=support.CHILD_ID,
                after=support.PARENT_ID,
                no_worktree=True,
                setup=HELD_COMMAND,
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_WAITING,
                task_id=support.SIBLING_ID,
                after=support.PARENT_ID,
                no_worktree=True,
            ),
        },
    )
    executor_thread = start_executor(ledger_path, support.PARENT_ID)
    wait_for_field(ledger_path, task_processes.FIELD_COMMAND_PROCESS_GROUP, support.CHILD_ID)

    cancel_and_join(executor_thread, support.CHILD_ID)

    tasks = support.read_tasks(ledger_path)
    assert tasks[support.CHILD_ID]["status"] == ledger.STATUS_CANCELLED
    assert tasks[support.SIBLING_ID]["status"] == ledger.STATUS_FINISHED
