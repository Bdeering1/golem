import json
import subprocess
import sys
import time as time_module
from datetime import datetime, time
from pathlib import Path

import pytest

from golem import cli, launch_agent, ledger, scheduling, spawn, usage_windows
from golem.backends import claude
from tests import support

WINDOW_ERROR = "rate limited"
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
INVALID_CONFIGS = [
    pytest.param("usage_windows = 1\n", "usage_windows", id="not-a-table"),
    pytest.param('[usage_windows]\nt3 = ["6am"]\n', "usage_windows.t3", id="unknown-key"),
    pytest.param('[usage_windows]\nclaude = "6am"\n', "usage_windows.claude", id="not-a-list"),
    pytest.param('[usage_windows]\nclaude = ["25am"]\n', "usage_windows.claude", id="bad-time"),
    pytest.param(
        '[usage_windows]\nclaude = ["6am tomorrow"]\n', "usage_windows.claude", id="tomorrow"
    ),
]


class FakeWindowOpener:
    def __init__(self) -> None:
        self.open_count = 0
        self.error: str | None = None

    def __call__(self, _environment: dict[str, str]) -> str | None:
        self.open_count += 1
        return self.error


@pytest.fixture
def claude_opener(monkeypatch):
    opener = FakeWindowOpener()
    monkeypatch.setattr(claude, "open_usage_window", opener)
    return opener


def local_now(hour: int, minute: int, *, day: int = 1) -> datetime:
    return datetime(2030, 1, day, hour, minute).astimezone()


def open_windows(now: datetime, window_time: time = time(6)) -> None:
    usage_windows.open_due_usage_windows({ledger.BACKEND_CLAUDE: [window_time]}, {}, now)


@pytest.mark.parametrize(
    ("window_time", "now"),
    [
        pytest.param(time(6), local_now(6, 2), id="same-day"),
        pytest.param(time(23, 50), local_now(0, 10, day=2), id="after-midnight"),
    ],
)
def test_due_time_opens_once_without_notifying(
    claude_opener, fake_commands, capsys, window_time, now
):
    open_windows(now, window_time)
    open_windows(now.replace(minute=now.minute + 5), window_time)

    assert claude_opener.open_count == 1
    assert "opened" in capsys.readouterr().err
    assert support.logged_osascript_calls(fake_commands) == []


def test_time_opens_again_the_next_day(claude_opener):
    open_windows(local_now(6, 2))
    open_windows(local_now(6, 2, day=2))

    assert claude_opener.open_count == 2


def test_time_missed_by_30_minutes_is_skipped_and_logged(claude_opener, fake_commands, capsys):
    open_windows(local_now(6, 30))
    open_windows(local_now(6, 45))

    assert claude_opener.open_count == 0
    assert capsys.readouterr().err.count("skipped") == 1
    assert support.logged_osascript_calls(fake_commands) == []


def test_failed_open_retries_until_cutoff_then_notifies_once(claude_opener, fake_commands):
    claude_opener.error = WINDOW_ERROR

    for minute in (1, 10, 29):
        open_windows(local_now(6, minute))
    assert claude_opener.open_count == 3
    assert support.logged_osascript_calls(fake_commands) == []

    open_windows(local_now(6, 30))
    open_windows(local_now(6, 40))

    assert claude_opener.open_count == 3
    notifications = support.logged_osascript_calls(fake_commands)
    assert len(notifications) == 1
    assert WINDOW_ERROR in notifications[0][-1]


@pytest.mark.parametrize(
    ("arguments", "config_text", "expected_backend"),
    [
        pytest.param(["--backend", "codex"], "", "codex", id="backend-flag"),
        pytest.param([], 'default_backend = "codex"\n', "codex", id="default-backend"),
        pytest.param([], 'default_backend = "t3"\n', "claude", id="t3-default"),
        pytest.param([], "", "claude", id="no-default"),
    ],
)
def test_window_open_requests_selected_backend_from_temporary_directory(
    fake_commands, capsys, arguments, config_text, expected_backend
):
    support.write_config_text(config_text)

    assert cli.main(["window", "open", *arguments]) == 0

    assert "opened" in capsys.readouterr().out
    other_backend = "claude" if expected_backend == "codex" else "codex"
    assert support.logged_calls(fake_commands, other_backend) == []
    assert support.logged_calls(fake_commands, expected_backend)
    request_directory = fake_commands[f"{expected_backend}-cwd"].read_text(encoding="utf-8")
    assert request_directory.strip() != str(Path.cwd())
    assert not Path(request_directory.strip()).exists()


def test_window_open_exits_non_zero_when_request_fails(monkeypatch):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")

    with pytest.raises(SystemExit, match="could not open"):
        cli.main(["window", "open"])


@pytest.mark.parametrize(("config_text", "entry"), INVALID_CONFIGS)
def test_invalid_usage_windows_config_fails_run_and_window_open(
    installed_launch_agent, ledger_path, fake_commands, config_text, entry
):
    support.write_config_text(config_text)

    with pytest.raises(SystemExit, match=f"{entry} "):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    assert not ledger_path.exists()
    with pytest.raises(SystemExit, match=f"{entry} "):
        cli.main(["window", "open"])
    assert support.logged_claude_calls(fake_commands) == []


def test_agent_logs_invalid_config_and_still_starts_due_tasks(
    ledger_path, monkeypatch, capsys, claude_opener
):
    support.write_config_text('[usage_windows]\nclaude = ["6am tomorrow"]\n')
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED)
    started_task_ids: list[str] = []
    processes: list[subprocess.Popen[bytes]] = []

    def start_executor_that_finishes(task_id: str, _: ledger.Task) -> subprocess.Popen[bytes]:
        with ledger.locked_tasks() as tasks:
            tasks[task_id]["status"] = ledger.STATUS_DONE
        started_task_ids.append(task_id)
        processes.append(subprocess.Popen([sys.executable, "-c", ""]))
        return processes[-1]

    monkeypatch.setattr(spawn, "start_task_executor", start_executor_that_finishes)

    assert launch_agent.run_launch_agent() == 0
    for process in processes:
        process.wait()

    assert started_task_ids == [support.TASK_ID]
    assert "usage_windows.claude" in capsys.readouterr().err
    assert claude_opener.open_count == 0


def write_current_minute_window_config() -> None:
    window_time = f"{datetime.now().astimezone():%I:%M%p}".lstrip("0").lower()
    support.write_config_text(f'[usage_windows]\nclaude = ["{window_time}"]\n')


def test_agent_opens_due_window_with_saved_agent_environment(
    installed_launch_agent, monkeypatch, fake_commands
):
    write_current_minute_window_config()
    monkeypatch.setenv("PATH", LAUNCHD_PATH)

    assert launch_agent.run_launch_agent() == 0

    assert len(support.logged_claude_calls(fake_commands)) == 1


def test_busy_agent_retries_failed_window_between_passes(ledger_path, monkeypatch, claude_opener):
    write_current_minute_window_config()
    claude_opener.error = WINDOW_ERROR
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, due_at=support.RESET_TIME.isoformat())
    sleep_count = 0

    def succeed_then_cancel_while_sleeping(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            claude_opener.error = None
        if sleep_count == 3:
            scheduling.cancel_task(support.TASK_ID)

    monkeypatch.setattr(time_module, "sleep", succeed_then_cancel_while_sleeping)

    assert launch_agent.run_launch_agent() == 0

    assert claude_opener.open_count == 3
    state_text = usage_windows.usage_window_state_path().read_text(encoding="utf-8")
    records = json.loads(state_text)
    assert [record["is_settled"] for record in records.values()] == [True]
