import contextlib
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from golem import cli, display, ledger, locks, progress_log, spawn
from golem.backends import claude, codex
from tests import support

WATCHED_COMMAND = "ls -la"
BACKENDS = [ledger.BACKEND_CLAUDE, ledger.BACKEND_CODEX]


def write_agent_run_and_expected_progress(
    fake_commands, monkeypatch, tmp_path, backend, message, *, is_usage_limit=False
):
    command = f"{WATCHED_COMMAND}\nsecond line"
    if backend == ledger.BACKEND_CODEX:
        events = [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "item.completed", "item": {"type": "command_execution", "command": command}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        ]
        if is_usage_limit:
            events.append(
                {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}
            )
        support.write_codex_events(monkeypatch, tmp_path, events)
        return f"[tool] command {WATCHED_COMMAND}\n{message}\n"
    assistant = {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}},
                {"type": "text", "text": message},
            ]
        },
    }
    subagent_assistant = {
        "type": "assistant",
        "parent_tool_use_id": "toolu_agent",
        "message": {"content": [{"type": "text", "text": "subagent text"}]},
    }
    result = {
        "type": "result",
        "result": support.USAGE_LIMIT_RESULT if is_usage_limit else message,
        "api_error_status": claude.USAGE_LIMIT_HTTP_STATUS if is_usage_limit else None,
    }
    support.write_claude_events(fake_commands, [subagent_assistant, assistant, result])
    return f"[tool] Bash {WATCHED_COMMAND}\n{message}\n"


def run_now(ledger_path, backend):
    cli.main(["run", support.TASK_ID, support.PROMPT, "--backend", backend, "--now"])
    return support.read_task(ledger_path)


def watch_output(capsys):
    assert cli.main(["watch", support.TASK_ID]) == 0
    return capsys.readouterr().out


@contextlib.contextmanager
def watch_subprocess_output():
    with spawn.subprocess.Popen(
        [*spawn.golem_command(), "watch", support.TASK_ID], stdout=subprocess.PIPE, text=True
    ) as watch:
        assert watch.stdout is not None
        yield watch.stdout
    assert watch.returncode == 0


def write_progress(session_id, text=""):
    path = progress_log.progress_path(session_id)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("backend", BACKENDS)
def test_watch_follows_running_task_to_its_end(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, backend
):
    hold_path = tmp_path / "hold"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CODEX_HOLD", str(hold_path))
    progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, support.CLAUDE_RESULT
    )
    task = run_now(ledger_path, backend)
    executor = spawn.start_task_executor(support.TASK_ID, task)

    try:
        support.wait_until(progress_log.progress_path(task["session_id"]).exists)
        with watch_subprocess_output() as watch_output_file:
            first_line = watch_output_file.readline()
            hold_path.unlink()
            rest = watch_output_file.read()
    finally:
        hold_path.unlink(missing_ok=True)
        executor.wait()

    assert first_line + rest == f"{progress}golem: task needs_review: {support.TASK_ID}\n"
    assert support.read_task(ledger_path)["result"] == support.CLAUDE_RESULT


@pytest.mark.parametrize("backend", BACKENDS)
def test_progress_write_failure_keeps_task_result(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, backend
):
    write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, support.CLAUDE_RESULT
    )
    monkeypatch.setattr(progress_log, "AGENT_LOG_MAX_BYTES", 0)
    monkeypatch.setattr(
        progress_log, "rotated_progress_path", lambda _: tmp_path / "missing" / "log"
    )
    task = run_now(ledger_path, backend)

    cli.main(["_execute", support.TASK_ID, task["session_id"]])

    task = support.read_task(ledger_path)
    assert (task["status"], task["result"]) == (ledger.STATUS_NEEDS_REVIEW, support.CLAUDE_RESULT)


def test_watch_exits_130_on_interrupt(ledger_path, monkeypatch):
    support.write_task(ledger_path, ledger.STATUS_RUNNING)
    write_progress(support.read_task(ledger_path)["session_id"])

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)

    assert cli.main(["watch", support.TASK_ID]) == 130


def test_watch_takes_no_locks_and_leaves_ledger_unchanged(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_RUNNING)
    task = support.read_task(ledger_path)
    path = write_progress(task["session_id"], "first\n")
    run_lock_file = locks.acquire_run_lock(task["session_id"])
    directory_lock_file = locks.acquire_lock(locks.directory_lock_path(task["cwd"]))
    assert run_lock_file is not None
    assert directory_lock_file is not None

    try:
        with watch_subprocess_output() as watch_output_file:
            first_line = watch_output_file.readline()
            with path.open("a", encoding="utf-8") as progress_file:
                progress_file.write("second\n")
            ledger.save_tasks({support.TASK_ID: support.task_record(ledger.STATUS_FAILED)})
            ledger_text = ledger_path.read_text(encoding="utf-8")
            rest = watch_output_file.read()
    finally:
        locks.release_run_lock(run_lock_file, task["session_id"])
        locks.release_lock(directory_lock_file)

    assert first_line + rest == f"first\nsecond\ngolem: task failed: {support.TASK_ID}\n"
    assert ledger_path.read_text(encoding="utf-8") == ledger_text


def test_watch_follows_progress_across_rotation(ledger_path, monkeypatch):
    support.write_task(ledger_path, ledger.STATUS_RUNNING)
    task = support.read_task(ledger_path)
    write_progress(task["session_id"], "first\n")
    monkeypatch.setattr(progress_log, "AGENT_LOG_MAX_BYTES", len("first\n"))

    with watch_subprocess_output() as watch_output_file:
        first_line = watch_output_file.readline()
        with progress_log.progress_writer(task["session_id"]) as record_progress:
            record_progress("second")
            record_progress("third")
        ledger.save_tasks({support.TASK_ID: support.task_record(ledger.STATUS_FAILED)})
        rest = watch_output_file.read()

    assert progress_log.rotated_progress_path(task["session_id"]).exists()
    assert first_line + rest == f"first\nsecond\nthird\ngolem: task failed: {support.TASK_ID}\n"


@pytest.mark.parametrize("backend", BACKENDS)
def test_watch_prints_usage_limit_pause_and_its_resume(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, capsys, backend
):
    monkeypatch.setattr(claude, "next_reset_time", lambda: support.RESET_TIME)
    monkeypatch.setattr(codex, "next_codex_reset_time", lambda: support.RESET_TIME)
    paused_progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, "paused", is_usage_limit=True
    )
    task = run_now(ledger_path, backend)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_SCHEDULED
    capsys.readouterr()

    resume_time = display.display_due_at(
        support.RESET_TIME.isoformat(), datetime.now().astimezone().date()
    )
    assert watch_output(capsys) == (
        f"{paused_progress}golem: task paused at the usage limit, resumes {resume_time}: {support.TASK_ID}\n"
    )

    resumed_progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, "resumed"
    )
    support.set_task_due_now(ledger_path)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])
    capsys.readouterr()

    assert watch_output(capsys) == (
        f"{paused_progress}{resumed_progress}golem: task needs_review: {support.TASK_ID}\n"
    )


def test_watch_shows_only_the_run_after_a_retry(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, capsys
):
    write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, ledger.BACKEND_CLAUDE, "first"
    )
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    first_task = run_now(ledger_path, ledger.BACKEND_CLAUDE)
    cli.main(["_execute", support.TASK_ID, first_task["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    cli.main(["retry", support.TASK_ID, "--now"])
    retried_task = support.read_task(ledger_path)
    progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, ledger.BACKEND_CLAUDE, "second"
    )
    cli.main(["_execute", support.TASK_ID, retried_task["session_id"]])
    capsys.readouterr()

    assert watch_output(capsys) == f"{progress}golem: task needs_review: {support.TASK_ID}\n"
    assert not progress_log.progress_path(first_task["session_id"]).exists()


def test_watch_prints_rotated_progress_first(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(progress_log, "AGENT_LOG_MAX_BYTES", 1)
    progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, ledger.BACKEND_CLAUDE, "done"
    )
    task = run_now(ledger_path, ledger.BACKEND_CLAUDE)
    cli.main(["_execute", support.TASK_ID, task["session_id"]])
    capsys.readouterr()

    assert watch_output(capsys) == f"{progress}golem: task needs_review: {support.TASK_ID}\n"
    assert progress_log.rotated_progress_path(task["session_id"]).exists()


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (ledger.STATUS_SCHEDULED, "task has not started"),
        (ledger.STATUS_DONE, "task has no recorded progress"),
    ],
)
def test_watch_refuses_task_without_progress(ledger_path, status, message):
    support.write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=f"golem: {message}: {support.TASK_ID}"):
        cli.main(["watch", support.TASK_ID])


def test_pickup_suggests_watch_for_running_task(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_RUNNING)
    directory_lock_file = locks.acquire_lock(locks.directory_lock_path(str(Path.cwd())))
    assert directory_lock_file is not None

    try:
        with pytest.raises(SystemExit, match=f"golem watch {support.TASK_ID}"):
            cli.main(["pickup", support.TASK_ID])
    finally:
        locks.release_lock(directory_lock_file)


def test_delete_removes_recorded_progress(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_DONE)
    session_id = support.read_task(ledger_path)["session_id"]
    paths = [write_progress(session_id), progress_log.rotated_progress_path(session_id)]
    paths[1].touch()

    assert cli.main(["delete", support.TASK_ID]) == 0
    assert not any(path.exists() for path in paths)
