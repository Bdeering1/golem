import os
import shutil
import subprocess
import sys
import time as time_module
from datetime import datetime, time
from pathlib import Path

import pytest

from golem import accounts, cli, launch_agent, ledger, scheduling, spawn, usage_windows
from golem.backends import claude
from tests import support

WINDOW_ERROR = "rate limited"
WORK_WINDOW_CONFIG = f'[usage_windows]\n"{support.WORK_EMAIL}" = ["6am"]\n'
INVALID_CONFIGS = [
    pytest.param("usage_windows = 1\n", "usage_windows", id="not-a-table"),
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
    usage_windows.open_due_usage_windows(
        [usage_windows.UsageWindowEntry(ledger.BACKEND_CLAUDE, None, [window_time])], {}, now
    )


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
    window_time = support.current_minute_window_time()
    support.write_config_text(f'[usage_windows]\nclaude = ["{window_time}"]\n')


def test_agent_opens_due_window_with_saved_agent_environment(
    installed_launch_agent, monkeypatch, fake_commands
):
    write_current_minute_window_config()
    monkeypatch.setenv("PATH", support.LAUNCHD_PATH)

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


def open_windows_at(
    minute: int, entries: list[usage_windows.UsageWindowEntry] | None = None
) -> None:
    now = datetime(2030, 1, 1, 6, minute).astimezone()
    if entries is None:
        entries = usage_windows.usage_window_entries(dict(os.environ))
    usage_windows.open_due_usage_windows(entries, dict(os.environ), now)


def window_config_directories(fake_commands) -> list[str]:
    lines = fake_commands["claude-account"].read_text(encoding="utf-8").splitlines()
    return [line.split("|")[0] for line in lines]


@pytest.mark.parametrize(
    "config_text",
    [
        pytest.param(WORK_WINDOW_CONFIG, id="email"),
        pytest.param('[usage_windows]\n"1" = ["6am"]\n', id="number"),
        pytest.param(
            f'[usage_windows]\n"{support.WORK_EMAIL.upper()}" = ["6am"]\n', id="email-case"
        ),
    ],
)
def test_account_window_opens_once_a_day_on_account(
    add_account, fake_commands, capsys, config_text
):
    add_account()
    support.write_config_text(config_text)

    open_windows_at(2)
    open_windows_at(7)

    assert window_config_directories(fake_commands) == [str(accounts.account_directory(1))]
    assert f"opened Claude usage window for {support.WORK_EMAIL} (Work)" in capsys.readouterr().err


def test_failed_account_window_retries_then_notifies_with_account(add_account, fake_commands):
    add_account()
    support.write_config_text(WORK_WINDOW_CONFIG)
    entries = usage_windows.usage_window_entries(dict(os.environ))
    shutil.rmtree(accounts.account_directory(1))

    for minute in (1, 10, 30, 40):
        open_windows_at(minute, entries)

    assert window_config_directories(fake_commands) == []
    notifications = support.logged_osascript_calls(fake_commands)
    assert len(notifications) == 1
    assert support.WORK_EMAIL in notifications[0][-2]
    assert "removed" in notifications[0][-1]


def test_accounts_with_same_time_each_open_once(add_account, fake_commands):
    add_account()
    support.write_config_text(
        f'[usage_windows]\nclaude = ["6am"]\n"{support.WORK_EMAIL}" = ["6am"]\n'
    )

    open_windows_at(2)
    open_windows_at(7)

    assert window_config_directories(fake_commands) == ["", str(accounts.account_directory(1))]


@pytest.mark.parametrize(
    "key",
    [pytest.param(f'"{support.DEFAULT_EMAIL}"', id="email"), pytest.param('"0"', id="number")],
)
@pytest.mark.usefixtures("claude_config_directory")
def test_default_account_window_shares_claude_state(fake_commands, key):
    support.write_config_text('[usage_windows]\nclaude = ["6am"]\n')
    open_windows_at(2)
    support.write_config_text(f'[usage_windows]\n{key} = ["6am"]\n')

    open_windows_at(7)

    assert window_config_directories(fake_commands) == [""]


def test_window_open_uses_account(add_account, fake_commands):
    add_account()

    assert cli.main(["window", "open", "--account", "work"]) == 0

    assert window_config_directories(fake_commands) == [str(accounts.account_directory(1))]


@pytest.mark.parametrize(
    ("config_text", "backend_args"),
    [
        pytest.param("", ["--backend", "codex"], id="backend-flag"),
        pytest.param('default_backend = "codex"\n', [], id="default-backend"),
    ],
)
def test_window_open_refuses_account_for_codex(
    add_account, fake_commands, config_text, backend_args
):
    add_account()
    support.write_config_text(config_text)

    with pytest.raises(SystemExit, match="only Claude"):
        cli.main(["window", "open", "--account", support.WORK_EMAIL, *backend_args])

    assert support.logged_calls(fake_commands, "codex") == []


@pytest.mark.parametrize(
    "key",
    [
        pytest.param('"nobody@example.com"', id="unknown-email"),
        pytest.param("work", id="prefix"),
        pytest.param('"5"', id="unknown-number"),
    ],
)
def test_unmatched_account_window_is_skipped_with_warning(add_account, fake_commands, capsys, key):
    add_account()
    support.write_config_text(f'[usage_windows]\nclaude = ["6am"]\n{key} = ["6am"]\n')

    open_windows_at(2)

    assert window_config_directories(fake_commands) == [""]
    assert "matches no Claude account" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config_text", "error"),
    [
        pytest.param(
            WORK_WINDOW_CONFIG,
            f"usage_windows.{support.WORK_EMAIL} matches more than one account",
            id="ambiguous",
        ),
        pytest.param(
            f'[usage_windows]\nclaude = ["6am"]\n"{support.DEFAULT_EMAIL}" = ["8am"]\n',
            f"usage_windows.{support.DEFAULT_EMAIL} lists the same account as usage_windows.claude",
            id="default-account-twice",
        ),
    ],
)
def test_invalid_account_window_entry_fails(add_account, fake_commands, config_text, error):
    add_account()
    add_account(support.WORK_EMAIL, "org-other", "Other")
    support.write_config_text(config_text)

    with pytest.raises(SystemExit, match=error):
        cli.main(["window", "open"])

    assert window_config_directories(fake_commands) == []


@pytest.mark.parametrize(
    ("config_text", "is_warned"),
    [
        pytest.param(
            f'[usage_windows]\n"{support.WORK_EMAIL.upper()}" = ["6am"]\n', True, id="email"
        ),
        pytest.param('[usage_windows]\n"01" = ["6am"]\n', True, id="number"),
        pytest.param('[usage_windows]\nclaude = ["6am"]\n', False, id="other-key"),
        pytest.param("[usage_windows\n", False, id="unparseable"),
    ],
)
def test_remove_warns_when_usage_windows_lists_account(add_account, capsys, config_text, is_warned):
    add_account()
    support.write_config_text(config_text)

    assert cli.main(["account", "remove", support.WORK_EMAIL]) == 0

    assert ("removed account" in capsys.readouterr().err) == is_warned
    assert not accounts.account_directory(1).exists()


@pytest.mark.parametrize(
    ("keys", "lookup_count"),
    [
        pytest.param(["claude"], 0, id="backend-keys"),
        pytest.param([f'"{support.DEFAULT_EMAIL}"', f'"{support.WORK_EMAIL}"'], 1, id="email-keys"),
    ],
)
def test_window_keys_look_up_login_at_most_once(add_account, fake_commands, keys, lookup_count):
    add_account()
    support.write_config_text("[usage_windows]\n" + "".join(f'{key} = ["6am"]\n' for key in keys))
    calls_before = len(support.auth_calls(fake_commands))

    usage_windows.usage_window_entries(dict(os.environ))

    assert len(support.auth_calls(fake_commands)) - calls_before == lookup_count


@pytest.mark.usefixtures("claude_config_directory")
def test_agent_opens_default_account_email_window_with_launchd_path(
    installed_launch_agent, monkeypatch, fake_commands
):
    window_time = support.current_minute_window_time()
    support.write_config_text(f'[usage_windows]\n"{support.DEFAULT_EMAIL}" = ["{window_time}"]\n')
    monkeypatch.setenv("PATH", support.LAUNCHD_PATH)

    assert launch_agent.run_launch_agent() == 0

    assert window_config_directories(fake_commands) == [""]
