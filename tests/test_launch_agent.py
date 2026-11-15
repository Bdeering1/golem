import fcntl
import json
import multiprocessing
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Never

import pytest

from golem import (
    branch_pull_requests,
    cli,
    executor,
    launch_agent,
    ledger,
    locks,
    progress_log,
    scheduling,
    spawn,
    t3_code,
)
from golem.backends import claude
from tests import support

LAUNCH_AGENT_ERROR_PATTERN = re.escape(launch_agent.LAUNCH_AGENT_ERROR)
PULL_REQUEST_URL = "https://github.com/owner/repository/pull/1"
THREAD_PRESENT_MARKER = "thread-present"
THREAD_SHOW_ERROR_MARKER = "thread-show-error"
UNREACHABLE_THREAD = "unreachable"


@pytest.mark.parametrize(
    ("command", "status"),
    [
        pytest.param(["run", support.TASK_ID, support.PROMPT], None, id="run"),
        pytest.param(["retry", support.TASK_ID], ledger.STATUS_FAILED, id="retry"),
    ],
)
def test_run_and_retry_require_an_installed_launch_agent(ledger_path, monkeypatch, command, status):
    if status is not None:
        support.write_task(ledger_path, status)
    monkeypatch.setattr(
        claude, "next_reset_time", lambda _environment: pytest.fail("usage lookup ran")
    )

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        cli.main(command)

    assert cli.main(["agent", "install"]) == 0
    assert cli.main(["agent", "uninstall"]) == 0
    assert not launch_agent.launch_agent_plist_path().exists()

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        cli.main(command)


def test_run_queues_and_wakes_launch_agent(installed_launch_agent, fake_commands, ledger_path):
    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now"]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""
    assert "kickstart" in fake_commands["launchctl-args"].read_text(encoding="utf-8")


def test_run_warns_when_waking_launch_agent_fails(
    installed_launch_agent, ledger_path, monkeypatch, capsys
):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_KICKSTART_EXIT", "1")

    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now"]) == 0

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED
    assert capsys.readouterr().err


def test_launch_agent_pass_starts_due_tasks_and_fails_abandoned_ones(ledger_path, monkeypatch):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_SCHEDULED),
            "dead": support.task_record(ledger.STATUS_RUNNING, task_id="dead"),
            "future": support.task_record(
                ledger.STATUS_SCHEDULED, task_id="future", due_at=support.RESET_TIME.isoformat()
            ),
        },
    )
    started_task_ids: list[str] = []
    monkeypatch.setattr(
        spawn, "start_task_executor", lambda task_id, task, **_: started_task_ids.append(task_id)
    )

    launch_agent.run_launch_agent_pass()

    assert started_task_ids == [support.TASK_ID]
    assert support.read_tasks(ledger_path)["dead"]["status"] == ledger.STATUS_FAILED
    assert support.read_tasks(ledger_path)["future"]["status"] == ledger.STATUS_SCHEDULED


def test_agent_log_copy_truncates_active_writer(monkeypatch):
    monkeypatch.setattr(progress_log, "AGENT_LOG_MAX_BYTES", 16)
    log_path = launch_agent.agent_log_path()
    first_content = "first log content\n"
    second_content = "second log content\n"

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(first_content)
        log_file.flush()

        launch_agent.run_launch_agent_pass()

        assert log_path.with_name("agent.log.1").read_text(encoding="utf-8") == first_content
        assert log_path.read_text(encoding="utf-8") == ""

        log_file.write(second_content)
        log_file.flush()
        assert log_path.read_text(encoding="utf-8") == second_content

        launch_agent.run_launch_agent_pass()

    assert log_path.with_name("agent.log.1").read_text(encoding="utf-8") == second_content
    assert log_path.read_text(encoding="utf-8") == ""


def test_agent_log_under_limit_is_untouched(monkeypatch):
    monkeypatch.setattr(progress_log, "AGENT_LOG_MAX_BYTES", 16)
    log_path = launch_agent.agent_log_path()
    log_path.write_text("under limit", encoding="utf-8")

    launch_agent.run_launch_agent_pass()

    assert log_path.read_text(encoding="utf-8") == "under limit"
    assert not log_path.with_name("agent.log.1").exists()


def test_agent_pass_removes_stale_state_but_keeps_held_lock(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_FINISHED)
    interrupted_write_path = ledger_path.parent / ".tasks.json.interrupted"
    interrupted_write_path.touch()
    orphan_lock_path = locks.run_lock_path("orphan")
    orphan_lock_path.touch()
    held_lock_file = locks.acquire_run_lock("held")
    assert held_lock_file is not None
    try:
        launch_agent.run_launch_agent_pass()

        assert not interrupted_write_path.exists()
        assert not orphan_lock_path.exists()
        assert locks.acquire_run_lock("held") is None
        assert ledger_path.exists()
        assert ledger_path.with_suffix(".lock").exists()
    finally:
        locks.release_run_lock(held_lock_file, "held")


def test_agent_cleanup_waits_for_active_ledger_write(ledger_path, monkeypatch):
    context = multiprocessing.get_context("fork")
    write_started = context.Event()
    finish_write = context.Event()
    replace = Path.replace
    flock = fcntl.flock

    def pause_write(temporary_path, task_ledger_path) -> Path:
        write_started.set()
        finish_write.wait()
        return replace(temporary_path, task_ledger_path)

    monkeypatch.setattr(Path, "replace", pause_write)

    def write_done_task() -> None:
        with ledger.locked_tasks() as tasks:
            tasks[support.TASK_ID] = support.task_record(ledger.STATUS_FINISHED)

    writer = context.Process(target=write_done_task)
    writer.start()

    def resume_write(lock_file, operation) -> None:
        if operation == fcntl.LOCK_EX:
            finish_write.set()
        flock(lock_file, operation)

    monkeypatch.setattr(fcntl, "flock", resume_write)
    try:
        write_started.wait()

        launch_agent.run_launch_agent_pass()
        writer.join()

        assert writer.exitcode == 0
        assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED
    finally:
        finish_write.set()
        if writer.is_alive():
            writer.kill()
        writer.join()


def test_launch_agent_pass_leaves_running_executor_alone(
    installed_launch_agent, ledger_path, monkeypatch
):
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    statuses_during_run: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        launch_agent.run_launch_agent_pass()
        statuses_during_run.append(support.read_task(ledger_path)["status"])
        return 0, json.dumps({"result": support.CLAUDE_RESULT}), ""

    monkeypatch.setattr(claude, "run_claude", run_claude)

    cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]])

    assert statuses_during_run == [ledger.STATUS_RUNNING]
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


@pytest.mark.parametrize(
    ("tasks", "expected_sleep_count"),
    [
        pytest.param(
            {
                support.TASK_ID: support.task_record(
                    ledger.STATUS_SCHEDULED, due_at=support.RESET_TIME.isoformat()
                )
            },
            1,
            id="scheduled",
        ),
        pytest.param(
            {
                support.PARENT_ID: support.task_record(
                    ledger.STATUS_FAILED, task_id=support.PARENT_ID
                ),
                support.TASK_ID: support.task_record(
                    ledger.STATUS_WAITING, after=support.PARENT_ID
                ),
            },
            0,
            id="waiting-on-failed-parent",
        ),
    ],
)
def test_launch_agent_runs_until_nothing_is_scheduled_or_running(
    ledger_path, monkeypatch, tasks, expected_sleep_count
):
    support.write_tasks(ledger_path, tasks)
    sleep_lengths: list[float] = []

    def cancel_while_sleeping(seconds: float) -> None:
        sleep_lengths.append(seconds)
        scheduling.cancel_task(support.TASK_ID)

    monkeypatch.setattr(time, "sleep", cancel_while_sleeping)

    assert launch_agent.run_launch_agent() == 0
    assert len(sleep_lengths) == expected_sleep_count


def test_changed_state_home_requires_reinstall(installed_launch_agent, monkeypatch, tmp_path):
    installed_plist = plistlib.loads(launch_agent.launch_agent_plist_path().read_bytes())
    assert installed_plist["EnvironmentVariables"] == {
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]
    }
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "other-state"))

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])


def test_run_requires_reinstall_without_saved_agent_environment(installed_launch_agent):
    settings = ledger.load_settings()
    del settings["agent_environment"]
    ledger.save_settings(settings)

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])


def test_launch_agent_install_removes_plist_when_bootstrap_fails(fake_commands, monkeypatch):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_BOOTSTRAP_EXIT", "1")

    with pytest.raises(SystemExit, match="could not install agent"):
        cli.main(["agent", "install"])

    assert not launch_agent.launch_agent_plist_path().exists()


def test_agent_recovers_interrupted_dependent_draining_in_ledger_order(
    fake_commands, ledger_path, monkeypatch
):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_SCHEDULED,
                task_id=support.PARENT_ID,
                prompt=support.PARENT_PROMPT,
                agent_args=support.AGENT_ARGS,
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING,
                task_id=support.CHILD_ID,
                after=support.PARENT_ID,
                prompt=support.CHILD_PROMPT,
                agent_args=support.AGENT_ARGS,
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_WAITING,
                task_id=support.SIBLING_ID,
                after=support.PARENT_ID,
                prompt=support.SIBLING_PROMPT,
                agent_args=support.AGENT_ARGS,
            ),
            "failed": support.task_record(ledger.STATUS_FAILED, task_id="failed"),
            support.GRANDCHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.GRANDCHILD_ID, after="failed"
            ),
        },
    )
    drain_dependents = executor.drain_dependents

    def crash_before_draining(task_id: str, session_id: str) -> Never:
        raise RuntimeError("executor crashed")

    monkeypatch.setattr(executor, "drain_dependents", crash_before_draining)
    with pytest.raises(RuntimeError):
        cli.main(
            [
                "_execute",
                support.PARENT_ID,
                support.read_task(ledger_path, support.PARENT_ID)["session_id"],
            ]
        )
    monkeypatch.setattr(executor, "drain_dependents", drain_dependents)
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING
    started_task_ids: list[str] = []

    def start_in_process(task_id: str, task: dict[str, Any]) -> None:
        started_task_ids.append(task_id)
        cli.main(["_execute", task_id, task["session_id"]])

    monkeypatch.setattr(spawn, "start_task_executor", start_in_process)
    launch_agent.run_launch_agent_pass()
    launch_agent.run_launch_agent_pass()

    assert started_task_ids == [support.PARENT_ID]
    assert support.logged_claude_calls(fake_commands) == [
        support.expected_claude_args(
            support.read_task(ledger_path, support.PARENT_ID),
            support.PARENT_PROMPT,
            task_id=support.PARENT_ID,
        ),
        support.expected_claude_args(
            support.read_task(ledger_path, support.CHILD_ID),
            support.CHILD_PROMPT,
            task_id=support.CHILD_ID,
        ),
        support.expected_claude_args(
            support.read_task(ledger_path, support.SIBLING_ID),
            support.SIBLING_PROMPT,
            task_id=support.SIBLING_ID,
        ),
    ]
    assert support.read_task(ledger_path, support.GRANDCHILD_ID)["status"] == ledger.STATUS_WAITING


@pytest.fixture
def started_task_ids(monkeypatch):
    task_ids: list[str] = []
    monkeypatch.setattr(
        spawn, "start_task_executor", lambda task_id, task, **_: task_ids.append(task_id)
    )
    return task_ids


def test_agent_does_not_recover_draining_while_a_dependent_is_running(
    ledger_path, started_task_ids
):
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_RUNNING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.SIBLING_ID, after=support.PARENT_ID
            ),
        },
    )
    child_session_id = support.read_task(ledger_path, support.CHILD_ID)["session_id"]
    child_run_lock_file = locks.acquire_run_lock(child_session_id)
    assert child_run_lock_file is not None
    try:
        launch_agent.run_launch_agent_pass()
    finally:
        locks.release_run_lock(child_run_lock_file, child_session_id)

    assert started_task_ids == []


def test_agent_waits_to_start_tasks_until_their_known_directory_is_free(
    ledger_path, started_task_ids, tmp_path
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    own_worktree_task = support.task_record(
        ledger.STATUS_SCHEDULED, task_id="own-worktree", agent_args=[support.WORKTREE_FLAG, "name"]
    )
    support.write_tasks(
        ledger_path,
        {
            support.PARENT_ID: support.task_record(
                ledger.STATUS_FINISHED, task_id=support.PARENT_ID, worktree=str(worktree)
            ),
            support.CHILD_ID: support.task_record(
                ledger.STATUS_WAITING, task_id=support.CHILD_ID, after=support.PARENT_ID
            ),
            support.SIBLING_ID: support.task_record(
                ledger.STATUS_SCHEDULED, task_id=support.SIBLING_ID, after=support.PARENT_ID
            ),
            "own-worktree": own_worktree_task,
        },
    )
    directory_lock_files = [
        locks.acquire_lock(locks.directory_lock_path(directory))
        for directory in (str(worktree), str(own_worktree_task["cwd"]))
    ]
    try:
        launch_agent.run_launch_agent_pass()
    finally:
        for directory_lock_file in directory_lock_files:
            assert directory_lock_file is not None
            locks.release_lock(directory_lock_file)
    assert started_task_ids == []

    launch_agent.run_launch_agent_pass()

    assert started_task_ids == [support.PARENT_ID, support.SIBLING_ID, "own-worktree"]


def test_launch_agent_reaps_finished_executor_while_future_task_is_pending(
    fake_commands, ledger_path, monkeypatch
):
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_SCHEDULED),
            "future": support.task_record(
                ledger.STATUS_SCHEDULED, task_id="future", due_at=support.RESET_TIME.isoformat()
            ),
        },
    )
    start_task_executor = spawn.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: ledger.Task) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    sleep_count = 0

    def sleep_while_agent_polls(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            support.wait_until(
                lambda: fake_commands["claude-args"].read_text(encoding="utf-8") != ""
            )
            support.wait_until(
                lambda: (
                    os.waitid(os.P_PID, processes[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    is not None
                )
            )
            return
        assert processes[0].returncode is not None
        scheduling.cancel_task("future")

    monkeypatch.setattr(spawn, "start_task_executor", record_executor)
    monkeypatch.setattr(time, "sleep", sleep_while_agent_polls)

    assert launch_agent.run_launch_agent() == 0
    assert processes[0].returncode is not None


def test_launch_agent_starts_due_tasks_while_pull_requests_refresh(ledger_path, monkeypatch):
    support.write_tasks(ledger_path, {})
    task_started = threading.Event()
    was_started_during_refresh: list[bool] = []
    processes: list[subprocess.Popen[bytes]] = []

    def refresh_while_task_is_queued() -> None:
        with ledger.locked_tasks() as tasks:
            tasks[support.TASK_ID] = support.task_record(ledger.STATUS_SCHEDULED)
        was_started_during_refresh.append(task_started.wait(5))

    def start_executor_that_finishes(task_id: str, _: ledger.Task) -> subprocess.Popen[bytes]:
        with ledger.locked_tasks() as tasks:
            tasks[task_id]["status"] = ledger.STATUS_FINISHED
        task_started.set()
        processes.append(subprocess.Popen([sys.executable, "-c", ""]))
        return processes[0]

    monkeypatch.setattr(branch_pull_requests, "refresh_pull_requests", refresh_while_task_is_queued)
    monkeypatch.setattr(spawn, "start_task_executor", start_executor_that_finishes)
    monkeypatch.setattr(launch_agent, "LAUNCH_AGENT_POLL_SECONDS", 0.01)

    assert launch_agent.run_launch_agent() == 0
    processes[0].wait()
    assert was_started_during_refresh == [True]


def test_waiting_dependent_of_succeeded_parent_keeps_launch_agent_running():
    tasks: ledger.Tasks = {
        support.PARENT_ID: support.task_record(ledger.STATUS_FINISHED, task_id=support.PARENT_ID),
        support.TASK_ID: support.task_record(ledger.STATUS_WAITING, after=support.PARENT_ID),
    }

    assert launch_agent.has_pending_tasks(tasks)


def test_launch_agent_stores_pull_requests_of_finished_tasks(write_pull_requests, ledger_path):
    support.write_task(
        ledger_path,
        ledger.STATUS_FINISHED,
        repository_branches=[{"repository": "owner/repository", "branch": support.WORKTREE_BRANCH}],
    )
    write_pull_requests(support.WORKTREE_BRANCH, [{"url": PULL_REQUEST_URL, "state": "MERGED"}])

    assert launch_agent.run_launch_agent() == 0

    assert support.read_task(ledger_path)["pull_requests"] == [
        {"url": PULL_REQUEST_URL, "state": ledger.PULL_REQUEST_STATE_MERGED}
    ]


def unreachable_projection(*_arguments: object) -> Never:
    raise t3_code.T3CodeError("no projection")


@pytest.mark.parametrize(
    ("thread", "thread_show_marker", "expected_state"),
    [
        pytest.param({}, THREAD_PRESENT_MARKER, ledger.THREAD_STATE_UNSETTLED, id="unsettled"),
        pytest.param(
            {"settledAt": "2030-03-01"},
            THREAD_PRESENT_MARKER,
            ledger.THREAD_STATE_SETTLED,
            id="settled",
        ),
        pytest.param(
            {"settledOverride": "settled"},
            THREAD_PRESENT_MARKER,
            ledger.THREAD_STATE_SETTLED,
            id="override",
        ),
        pytest.param(
            {"archivedAt": "2030-03-01"},
            THREAD_PRESENT_MARKER,
            ledger.THREAD_STATE_ARCHIVED,
            id="archived",
        ),
        pytest.param(
            {"deletedAt": "2030-03-01"},
            THREAD_PRESENT_MARKER,
            ledger.THREAD_STATE_DELETED,
            id="deleted",
        ),
        pytest.param(UNREACHABLE_THREAD, None, ledger.THREAD_STATE_DELETED, id="missing"),
        pytest.param(None, None, ledger.THREAD_STATE_DELETED, id="null-thread"),
        pytest.param(
            UNREACHABLE_THREAD, THREAD_PRESENT_MARKER, ledger.THREAD_STATE_SETTLED, id="unreadable"
        ),
        pytest.param(
            UNREACHABLE_THREAD,
            THREAD_SHOW_ERROR_MARKER,
            ledger.THREAD_STATE_SETTLED,
            id="show-error",
        ),
    ],
)
def test_launch_agent_records_thread_state_of_finished_chain(
    server, ledger_path, monkeypatch, thread, thread_show_marker, expected_state
):
    support.write_task(
        ledger_path,
        ledger.STATUS_FINISHED,
        t3_code_thread_id="thread",
        t3_code_thread_state=ledger.THREAD_STATE_SETTLED,
    )
    if thread == UNREACHABLE_THREAD:
        monkeypatch.setattr(t3_code, "thread_projection", unreachable_projection)
    server.projections[0] = {"thread": thread}
    if thread_show_marker is not None:
        (server.home / thread_show_marker).touch()

    assert launch_agent.run_launch_agent() == 0

    assert support.read_task(ledger_path)["t3_code_thread_state"] == expected_state


@pytest.mark.parametrize(
    ("fields", "is_running"),
    [
        pytest.param({}, False, id="t3-code-closed"),
        pytest.param(
            {"pull_requests": [{"url": PULL_REQUEST_URL, "state": "open"}]}, True, id="pr"
        ),
    ],
)
def test_launch_agent_keeps_last_thread_state_without_reading(
    server, ledger_path, monkeypatch, fields, is_running
):
    support.write_task(
        ledger_path,
        ledger.STATUS_FINISHED,
        t3_code_thread_id="thread",
        t3_code_thread_state=ledger.THREAD_STATE_SETTLED,
        **fields,
    )
    if not is_running:
        server.runtime_path.unlink()
    monkeypatch.setattr(t3_code, "thread_projection", lambda *_: pytest.fail("thread read"))

    assert launch_agent.run_launch_agent() == 0

    assert support.read_task(ledger_path)["t3_code_thread_state"] == ledger.THREAD_STATE_SETTLED
