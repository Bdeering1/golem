import json
import multiprocessing
import os
import plistlib
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never

import pytest

import golem

TASK_ID = "fix-thing"
PROMPT = "fix the thing"
PARENT_ID = "parent"
CHILD_ID = "child"
GRANDCHILD_ID = "grandchild"
SIBLING_ID = "sibling"
PARENT_PROMPT = "run the parent"
CHILD_PROMPT = "run the child"
GRANDCHILD_PROMPT = "run the grandchild"
SIBLING_PROMPT = "run the sibling"
AGENT_ARGS = ["--model", "haiku"]
AT_TEXT = "9pm"
SKIP_PERMISSIONS = "--dangerously-skip-permissions"
WORKTREE_FLAG = "--worktree"
LAUNCH_AGENT_ERROR_PATTERN = re.escape(golem.LAUNCH_AGENT_ERROR)
SAVED_LANG = "en_CA.UTF-8"
CHILD_LANG = "fr_CA.UTF-8"
CURRENT_LANG = "en_US.UTF-8"
CLAUDE_RESULT = "completed the task"
USAGE_LIMIT_RESULT = "You've hit your limit · resets 3pm"
RESET_TIME = datetime(2030, 1, 1, 10, tzinfo=UTC)
EARLIER_RESET_TIME = datetime(2030, 1, 1, 11, tzinfo=UTC)
LATER_RESET_TIME = datetime(2030, 1, 2, 11, tzinfo=UTC)
ANSI_COLOR = re.compile(r"\033\[38;5;\d+m|\033\[0m")


@pytest.fixture(scope="session")
def fake_bin_path(tmp_path_factory):
    command_path = tmp_path_factory.mktemp("bin")
    for name, script in {
        "claude": """#!/bin/sh
printf '\036\\n' >> "$GOLEM_CLAUDE_ARGS"
printf '%s\\n' "$@" >> "$GOLEM_CLAUDE_ARGS"
printf '%s\\n' "$PWD" >> "$GOLEM_CLAUDE_CWD"
printf '%s\\n' "${LANG-}" >> "$GOLEM_CLAUDE_LANG"
printf '%s\\n' "${LC_TIME-}" >> "$GOLEM_CLAUDE_LC_TIME"
if [ -n "${UNRELATED+x}" ]; then
    printf '%s\\n' "$UNRELATED" >> "$GOLEM_CLAUDE_UNRELATED"
fi
worktree=''
next=0
for argument in "$@"; do
    if [ "$next" = 1 ]; then
        worktree="$argument"
        next=0
        continue
    fi
    if [ "$argument" = '--worktree' ]; then
        next=1
        continue
    fi
    case "$argument" in --worktree=*) worktree="${argument#--worktree=}" ;; esac
done
if [ -n "$worktree" ]; then
    common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
    if [ -n "$common" ]; then
        mkdir -p "$(dirname "$common")/.claude/worktrees/$worktree"
    fi
fi
if [ -n "$GOLEM_CLAUDE_STARTED" ]; then
    : > "$GOLEM_CLAUDE_STARTED"
fi
if [ -n "$GOLEM_CLAUDE_STRAY" ]; then
    (while [ -e "$GOLEM_CLAUDE_STRAY" ]; do sleep 0.05; done) < /dev/null > /dev/null 2>&1 &
fi
while [ -n "$GOLEM_CLAUDE_HOLD" ] && [ -e "$GOLEM_CLAUDE_HOLD" ]; do
    sleep 0.05
done
if [ -f "$GOLEM_CLAUDE_STDOUT" ]; then
    cat "$GOLEM_CLAUDE_STDOUT"
else
    printf '%s' '{"result": "done"}'
fi
exit "${GOLEM_CLAUDE_EXIT:-0}"
""",
        "launchctl": """#!/bin/sh
printf '%s\\n' "$*" >> "$GOLEM_LAUNCHCTL_ARGS"
case "$1" in
    bootstrap) exit "${GOLEM_LAUNCHCTL_BOOTSTRAP_EXIT:-0}" ;;
    kickstart) exit "${GOLEM_LAUNCHCTL_KICKSTART_EXIT:-0}" ;;
esac
""",
        "open": """#!/bin/sh
printf '%s\\n' "$*" >> "$GOLEM_OPEN_ARGS"
""",
    }.items():
        path = command_path / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    return command_path


@pytest.fixture(autouse=True)
def fake_commands(monkeypatch, tmp_path, fake_bin_path):
    paths = {
        name: tmp_path / name
        for name in (
            "claude-args",
            "claude-cwd",
            "claude-lang",
            "claude-lc-time",
            "claude-stdout",
            "claude-unrelated",
            "launchctl-args",
            "open-args",
        )
    }
    for path in paths.values():
        path.touch()
    home_path = tmp_path / "home"
    state_path = tmp_path / "state"
    work_path = tmp_path / "work"
    work_path.mkdir()
    monkeypatch.setenv("PATH", f"{fake_bin_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_path))
    for name, path in paths.items():
        monkeypatch.setenv(f"GOLEM_{name.upper().replace('-', '_')}", str(path))
    monkeypatch.chdir(work_path)
    return paths


@pytest.fixture
def ledger_path():
    return golem.ledger_path()


@pytest.fixture
def installed_launch_agent():
    assert golem.main(["agent", "install"]) == 0


def read_task(ledger_path, task_id=TASK_ID):
    return json.loads(ledger_path.read_text(encoding="utf-8"))[task_id]


def task_record(status, *, task_id=TASK_ID, **fields: object):
    record = {
        "agent_args": [],
        "cwd": str(Path.cwd()),
        "due_at": datetime.now(UTC).isoformat(),
        "environment": {"PATH": os.environ["PATH"]},
        "prompt": PROMPT,
        "session_id": f"{task_id}-session",
        "status": status,
    }
    record.update(fields)
    return record


def write_task(ledger_path, status, *, task_id=TASK_ID, **fields: object):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({task_id: task_record(status, task_id=task_id, **fields)}), encoding="utf-8"
    )


def write_tasks(ledger_path, tasks):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(tasks), encoding="utf-8")


def list_output(capsys):
    assert golem.main(["list"]) == 0
    return capsys.readouterr().out


def list_row(output, task_id):
    return next(line for line in output.splitlines() if line.startswith(f"{task_id} "))


def test_list_groups_visible_tasks_by_status_and_id(ledger_path, capsys):
    tasks = {
        "z-failed": task_record(golem.STATUS_FAILED, task_id="z-failed"),
        "a-failed": task_record(golem.STATUS_FAILED, task_id="a-failed"),
        "scheduled": task_record(golem.STATUS_SCHEDULED, task_id="scheduled"),
        "running": task_record(golem.STATUS_RUNNING, task_id="running"),
        "review": task_record(golem.STATUS_NEEDS_REVIEW, task_id="review"),
        "parent": task_record(golem.STATUS_NEEDS_REVIEW, task_id="parent"),
        "waiting": task_record(golem.STATUS_WAITING, task_id="waiting", after="parent"),
        "done": task_record(golem.STATUS_DONE, task_id="done"),
        "cancelled": task_record(golem.STATUS_CANCELLED, task_id="cancelled"),
    }
    write_tasks(ledger_path, tasks)

    output = list_output(capsys)

    assert [line.split()[0] for line in output.splitlines()[1:]] == [
        "parent",
        "review",
        "a-failed",
        "z-failed",
        "running",
        "scheduled",
        "waiting",
        "done",
        "cancelled",
    ]
    assert list_row(output, "waiting").split()[-1] == "parent"


def test_list_abbreviates_only_home_and_its_descendants(ledger_path, capsys):
    home_directory = Path.home()
    prefix_directory = str(home_directory.with_name(f"{home_directory.name}-other"))
    tasks = {
        "home": task_record(golem.STATUS_DONE, task_id="home", cwd=str(home_directory)),
        "descendant": task_record(
            golem.STATUS_DONE, task_id="descendant", cwd=str(home_directory / "project")
        ),
        "prefix": task_record(golem.STATUS_DONE, task_id="prefix", cwd=prefix_directory),
    }
    write_tasks(ledger_path, tasks)

    output = list_output(capsys)

    assert "  ~" in list_row(output, "home")
    assert "  ~/project" in list_row(output, "descendant")
    assert prefix_directory in list_row(output, "prefix")


def test_list_formats_due_times_in_local_time(ledger_path, capsys, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            now = datetime(2030, 3, 10, 12, tzinfo=UTC)
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(golem, "datetime", FixedDateTime)
    tasks = {
        "today": task_record(
            golem.STATUS_SCHEDULED,
            task_id="today",
            due_at=datetime(2030, 3, 11, 3, 30, tzinfo=UTC).isoformat(),
        ),
        "tomorrow": task_record(
            golem.STATUS_SCHEDULED,
            task_id="tomorrow",
            due_at=datetime(2030, 3, 11, 13, tzinfo=UTC).isoformat(),
        ),
        "daylight": task_record(
            golem.STATUS_SCHEDULED,
            task_id="daylight",
            due_at=datetime(2030, 11, 3, 5, 30, tzinfo=UTC).isoformat(),
        ),
        "standard": task_record(
            golem.STATUS_SCHEDULED,
            task_id="standard",
            due_at=datetime(2030, 11, 3, 6, 30, tzinfo=UTC).isoformat(),
        ),
        "no-due": task_record(golem.STATUS_SCHEDULED, task_id="no-due"),
    }
    tasks["no-due"].pop("due_at")
    write_tasks(ledger_path, tasks)

    original_timezone = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    try:
        output = list_output(capsys)
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert "today 11:30pm EDT" in list_row(output, "today")
    assert "tomorrow 9:00am EDT" in list_row(output, "tomorrow")
    assert "Nov 3, 2030 1:30am EDT" in list_row(output, "daylight")
    assert "Nov 3, 2030 1:30am EST" in list_row(output, "standard")
    assert list_row(output, "no-due").endswith(str(Path.cwd()))


def test_list_uses_distinct_status_colors_and_keeps_columns_aligned(
    ledger_path, capsys, monkeypatch
):
    tasks = {
        status: task_record(status, task_id=status)
        for status in golem.LIST_STATUS_ORDER
        if status != golem.STATUS_WAITING
    }
    tasks[golem.STATUS_WAITING] = task_record(
        golem.STATUS_WAITING,
        task_id=golem.STATUS_WAITING,
        after=golem.STATUS_NEEDS_REVIEW,
    )
    write_tasks(ledger_path, tasks)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    colored_output = list_output(capsys)

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    plain_output = list_output(capsys)
    colors = re.findall(r"\033\[38;5;(\d+)m", colored_output)

    assert len(colors) == len(golem.LIST_STATUS_ORDER)
    assert len(set(colors)) == len(golem.LIST_STATUS_ORDER)
    assert ANSI_COLOR.sub("", colored_output) == plain_output


@pytest.mark.parametrize(
    ("is_tty", "term", "no_color"),
    [
        pytest.param(False, "xterm-256color", None, id="redirected"),
        pytest.param(True, "dumb", None, id="dumb-terminal"),
        pytest.param(True, "xterm-256color", "", id="no-color"),
    ],
)
def test_list_suppresses_status_colors(ledger_path, capsys, monkeypatch, is_tty, term, no_color):
    write_task(ledger_path, golem.STATUS_DONE)
    monkeypatch.setenv("TERM", term)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)

    output = list_output(capsys)

    assert ANSI_COLOR.search(output) is None


def write_worktrees(worktrees):
    golem.worktree_ledger_path().write_text(json.dumps(worktrees), encoding="utf-8")


def git_output(repository_path, *arguments: str):
    result = golem.subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=repository_path,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def git_worktree(repository_path):
    git_output(repository_path, "worktree", "add", "-qb", "worktree", "worktree")
    return repository_path / "worktree"


def golem_worktree_resource(repository_path, worktree_path, *, released_at=None):
    resource = {
        "owner": golem.WORKTREE_OWNER_GOLEM,
        "repository": golem.git_common_directory(str(repository_path)),
        "session_id": "fix-thing-session",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if released_at is not None:
        resource["released_at"] = released_at
    marker_path = golem.worktree_owner_marker_path(str(worktree_path))
    assert marker_path is not None
    marker_path.write_text("fix-thing-session", encoding="utf-8")
    return {str(worktree_path.resolve()): resource}


@pytest.fixture
def owned_worktree(git_repository, ledger_path):
    def create_worktree(status: str = golem.STATUS_DONE, *, released_at: str | None = None) -> Path:
        worktree = git_worktree(git_repository)
        write_task(ledger_path, status, worktree=str(worktree))
        write_worktrees(golem_worktree_resource(git_repository, worktree, released_at=released_at))
        return worktree

    return create_worktree


def read_tasks(ledger_path):
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def logged_claude_calls(fake_commands):
    calls: list[list[str]] = []
    for call in fake_commands["claude-args"].read_text(encoding="utf-8").split("\x1e\n"):
        arguments = call.splitlines()
        if arguments:
            calls.append(arguments)
    return calls


def logged_claude_args(fake_commands):
    calls = logged_claude_calls(fake_commands)
    return calls[-1] if calls else []


def wait_until(predicate):
    # select instead of time.sleep, which some tests replace to drive the agent loop.
    for _ in range(100):
        if predicate():
            return
        select.select([], [], [], 0.05)
    pytest.fail("condition did not become true")


def logged_claude_cwds(fake_commands):
    return fake_commands["claude-cwd"].read_text(encoding="utf-8").splitlines()


def expected_claude_args(task, prompt=PROMPT, agent_args=AGENT_ARGS, task_id=TASK_ID):
    return [
        "--session-id",
        task["session_id"],
        "--print",
        prompt,
        "--output-format",
        "json",
        *agent_args,
        golem.VERBOSE_ARGUMENT,
        "--name",
        task_id,
        SKIP_PERMISSIONS,
    ]


def raise_usage_lookup_error() -> Never:
    raise SystemExit


@pytest.mark.parametrize(
    ("command", "status"),
    [
        pytest.param(["run", TASK_ID, PROMPT], None, id="run"),
        pytest.param(["retry", TASK_ID], golem.STATUS_FAILED, id="retry"),
    ],
)
def test_run_and_retry_require_an_installed_launch_agent(ledger_path, monkeypatch, command, status):
    if status is not None:
        write_task(ledger_path, status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        golem.main(command)

    assert golem.main(["agent", "install"]) == 0
    assert golem.main(["agent", "uninstall"]) == 0
    assert not golem.launch_agent_plist_path().exists()

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        golem.main(command)


def test_run_queues_and_wakes_launch_agent(installed_launch_agent, fake_commands, ledger_path):
    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""
    assert "kickstart" in fake_commands["launchctl-args"].read_text(encoding="utf-8")


def test_run_warns_when_waking_launch_agent_fails(
    installed_launch_agent, ledger_path, monkeypatch, capsys
):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_KICKSTART_EXIT", "1")

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0

    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert capsys.readouterr().err


def test_launch_agent_pass_starts_due_tasks_and_fails_abandoned_ones(ledger_path, monkeypatch):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_SCHEDULED),
            "dead": task_record(golem.STATUS_RUNNING, task_id="dead"),
            "future": task_record(
                golem.STATUS_SCHEDULED, task_id="future", due_at=RESET_TIME.isoformat()
            ),
        },
    )
    started_task_ids: list[str] = []
    monkeypatch.setattr(
        golem, "start_task_executor", lambda task_id, task: started_task_ids.append(task_id)
    )

    golem.run_launch_agent_pass()

    assert started_task_ids == [TASK_ID]
    assert read_tasks(ledger_path)["dead"]["status"] == golem.STATUS_FAILED
    assert read_tasks(ledger_path)["future"]["status"] == golem.STATUS_SCHEDULED


def test_agent_log_copy_truncates_active_writer(monkeypatch):
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", 16)
    log_path = golem.agent_log_path()
    first_content = "first log content\n"
    second_content = "second log content\n"

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(first_content)
        log_file.flush()

        golem.run_launch_agent_pass()

        assert log_path.with_name("agent.log.1").read_text(encoding="utf-8") == first_content
        assert log_path.read_text(encoding="utf-8") == ""

        log_file.write(second_content)
        log_file.flush()
        assert log_path.read_text(encoding="utf-8") == second_content

        golem.run_launch_agent_pass()

    assert log_path.with_name("agent.log.1").read_text(encoding="utf-8") == second_content
    assert log_path.read_text(encoding="utf-8") == ""


def test_agent_log_under_limit_is_untouched(monkeypatch):
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", 16)
    log_path = golem.agent_log_path()
    log_path.write_text("under limit", encoding="utf-8")

    golem.run_launch_agent_pass()

    assert log_path.read_text(encoding="utf-8") == "under limit"
    assert not log_path.with_name("agent.log.1").exists()


def test_agent_pass_removes_stale_state_but_keeps_held_lock(ledger_path):
    write_task(ledger_path, golem.STATUS_DONE)
    interrupted_write_path = ledger_path.parent / ".tasks.json.interrupted"
    interrupted_write_path.touch()
    orphan_lock_path = golem.run_lock_path("orphan")
    orphan_lock_path.touch()
    held_lock_file = golem.acquire_run_lock("held")
    assert held_lock_file is not None
    try:
        golem.run_launch_agent_pass()

        assert not interrupted_write_path.exists()
        assert not orphan_lock_path.exists()
        assert golem.acquire_run_lock("held") is None
        assert ledger_path.exists()
        assert ledger_path.with_suffix(".lock").exists()
    finally:
        golem.release_run_lock(held_lock_file, "held")


def test_agent_cleanup_waits_for_active_ledger_write(ledger_path, monkeypatch):
    context = multiprocessing.get_context("fork")
    write_started = context.Event()
    finish_write = context.Event()
    replace = Path.replace
    flock = golem.fcntl.flock

    def pause_write(temporary_path, task_ledger_path) -> Path:
        write_started.set()
        assert finish_write.wait(timeout=5)
        return replace(temporary_path, task_ledger_path)

    monkeypatch.setattr(Path, "replace", pause_write)
    writer = context.Process(
        target=lambda: golem.reserve_task(TASK_ID, task_record(golem.STATUS_DONE))
    )
    writer.start()

    def resume_write(lock_file, operation) -> None:
        if operation == golem.fcntl.LOCK_EX:
            finish_write.set()
        flock(lock_file, operation)

    monkeypatch.setattr(golem.fcntl, "flock", resume_write)
    try:
        assert write_started.wait(timeout=3)

        golem.run_launch_agent_pass()
        writer.join(timeout=3)

        assert writer.exitcode == 0
        assert read_task(ledger_path)["status"] == golem.STATUS_DONE
    finally:
        finish_write.set()
        if writer.is_alive():
            writer.kill()
        writer.join()


def test_launch_agent_pass_leaves_running_executor_alone(
    installed_launch_agent, ledger_path, monkeypatch
):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    statuses_during_run: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str]:
        golem.run_launch_agent_pass()
        statuses_during_run.append(read_task(ledger_path)["status"])
        return 0, json.dumps({"result": CLAUDE_RESULT})

    monkeypatch.setattr(golem, "run_claude", run_claude)

    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])

    assert statuses_during_run == [golem.STATUS_RUNNING]
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW


@pytest.mark.parametrize(
    ("tasks", "expected_sleep_count"),
    [
        pytest.param(
            {TASK_ID: task_record(golem.STATUS_SCHEDULED, due_at=RESET_TIME.isoformat())},
            1,
            id="scheduled",
        ),
        pytest.param(
            {
                PARENT_ID: task_record(golem.STATUS_FAILED, task_id=PARENT_ID),
                TASK_ID: task_record(golem.STATUS_WAITING, after=PARENT_ID),
            },
            0,
            id="waiting-on-failed-parent",
        ),
    ],
)
def test_launch_agent_runs_until_nothing_is_scheduled_or_running(
    ledger_path, monkeypatch, tasks, expected_sleep_count
):
    write_tasks(ledger_path, tasks)
    sleep_lengths: list[float] = []

    def cancel_while_sleeping(seconds: float) -> None:
        sleep_lengths.append(seconds)
        golem.cancel_task(TASK_ID)

    monkeypatch.setattr(golem.time, "sleep", cancel_while_sleeping)

    assert golem.run_launch_agent() == 0
    assert len(sleep_lengths) == expected_sleep_count


def test_changed_state_home_requires_reinstall(installed_launch_agent, monkeypatch, tmp_path):
    installed_plist = plistlib.loads(golem.launch_agent_plist_path().read_bytes())
    assert installed_plist["EnvironmentVariables"] == {
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]
    }
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "other-state"))

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        golem.main(["run", TASK_ID, PROMPT, "--now"])


def test_launch_agent_executor_uses_only_saved_environment(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("LANG", SAVED_LANG)
    monkeypatch.setenv("LC_TIME", CHILD_LANG)
    monkeypatch.setenv("UNRELATED", "not-saved")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    monkeypatch.setenv("LANG", CURRENT_LANG)
    monkeypatch.setenv("LC_TIME", CURRENT_LANG)
    monkeypatch.delenv("UNRELATED")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    start_task_executor = golem.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    monkeypatch.setattr(golem, "start_task_executor", record_executor)

    golem.run_launch_agent_pass()

    assert len(processes) == 1
    try:
        processes[0].wait(timeout=3)
    except subprocess.TimeoutExpired:
        processes[0].kill()
        processes[0].wait()
        pytest.fail("task executor did not exit")
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert fake_commands["claude-lang"].read_text(encoding="utf-8") == f"{SAVED_LANG}\n"
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8") == f"{CHILD_LANG}\n"
    assert fake_commands["claude-unrelated"].read_text(encoding="utf-8") == ""


@pytest.fixture(scope="session")
def git_repository_template(tmp_path_factory):
    repository_path = tmp_path_factory.mktemp("repository")
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository_path, check=True)
    (repository_path / "README").write_text("test\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "add", "README"], cwd=repository_path, check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "test",
        ],
        cwd=repository_path,
        check=True,
    )
    return repository_path


@pytest.fixture
def git_repository(tmp_path, git_repository_template):
    repository_path = tmp_path / "repository"
    shutil.copytree(git_repository_template, repository_path)
    return repository_path


@pytest.fixture
def empty_git_repository(tmp_path):
    repository_path = tmp_path / "empty-repository"
    repository_path.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository_path, check=True)
    return repository_path


@pytest.mark.parametrize(
    (
        "agent_args",
        "repository_fixture",
        "has_default_name",
        "has_default_worktree",
        "has_default_skip_permissions",
    ),
    [
        pytest.param([], "git_repository", True, True, True, id="defaults"),
        pytest.param([], None, True, False, True, id="non-git"),
        pytest.param([], "empty_git_repository", True, False, True, id="no-commits"),
        pytest.param(
            ["--permission-mode", "plan"], "git_repository", True, True, False, id="permission-mode"
        ),
        pytest.param(
            ["--permission-mode=plan"],
            "git_repository",
            True,
            True,
            False,
            id="permission-mode-value",
        ),
        pytest.param(
            ["--allow-dangerously-skip-permissions"],
            "git_repository",
            True,
            True,
            False,
            id="allow-permissions",
        ),
        pytest.param([WORKTREE_FLAG, "custom"], "git_repository", True, False, True, id="worktree"),
        pytest.param(["-w", "custom"], "git_repository", True, False, True, id="short-worktree"),
        pytest.param(["--name", "custom"], "git_repository", False, True, True, id="name"),
        pytest.param(["-n", "custom"], "git_repository", False, True, True, id="short-name"),
    ],
)
def test_headless_defaults_respect_agent_arguments(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    request,
    agent_args,
    repository_fixture,
    has_default_name,
    has_default_worktree,
    has_default_skip_permissions,
):
    if repository_fixture is not None:
        monkeypatch.chdir(request.getfixturevalue(repository_fixture))
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    arguments = logged_claude_args(fake_commands)
    assert arguments[arguments.index("--output-format") + 2 :] == [
        *agent_args,
        golem.VERBOSE_ARGUMENT,
        *(["--name", TASK_ID] if has_default_name else []),
        *([WORKTREE_FLAG, task["session_id"]] if has_default_worktree else []),
        *([SKIP_PERMISSIONS] if has_default_skip_permissions else []),
    ]


@pytest.mark.parametrize("is_retried", [False, True])
def test_dependent_runs_in_parent_worktree_with_its_own_environment(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, is_retried
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setenv("LANG", SAVED_LANG)
    monkeypatch.setenv("LC_TIME", SAVED_LANG)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    monkeypatch.setenv("LANG", CHILD_LANG)
    monkeypatch.delenv("LC_TIME")
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    monkeypatch.setenv("LANG", CURRENT_LANG)
    monkeypatch.setenv("LC_TIME", CURRENT_LANG)
    if is_retried:
        golem.main(["cancel", CHILD_ID])
        golem.main(["retry", CHILD_ID, "--now"])

    golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])
    if is_retried:
        golem.main(["_execute", CHILD_ID, read_task(ledger_path, CHILD_ID)["session_id"]])

    parent_worktree = read_task(ledger_path, PARENT_ID)["worktree"]
    child_arguments = logged_claude_args(fake_commands)
    assert logged_claude_cwds(fake_commands)[-1] == parent_worktree
    assert WORKTREE_FLAG not in child_arguments
    assert fake_commands["claude-lang"].read_text(encoding="utf-8").splitlines() == [
        SAVED_LANG,
        CHILD_LANG,
    ]
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8").splitlines() == [
        SAVED_LANG,
        "",
    ]


def test_detected_worktree_is_shared_with_dependents(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, tmp_path
):
    monkeypatch.chdir(git_repository)
    detected_worktree = tmp_path / "detected-worktree"
    detected_worktree.mkdir()
    write_claude_output(
        fake_commands, CLAUDE_RESULT, is_verbose=True, working_directory=str(detected_worktree)
    )
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now", "--", WORKTREE_FLAG, "custom"])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])

    golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])

    assert logged_claude_cwds(fake_commands)[-1] == str(detected_worktree)
    resource = golem.load_worktrees()[str(detected_worktree.resolve())]
    assert resource["owner"] == golem.WORKTREE_OWNER_EXTERNAL


def write_claude_output(
    fake_commands, result, *, api_error_status=None, is_verbose=False, working_directory=None
):
    output = {
        "api_error_status": api_error_status,
        "is_error": api_error_status is not None,
        "result": result,
        "type": "result",
    }
    init_message = {"type": "system", "subtype": "init", "cwd": working_directory}
    response = [init_message, output] if is_verbose else output
    fake_commands["claude-stdout"].write_text(json.dumps(response), encoding="utf-8")


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pickup"], id="pickup"),
        pytest.param(["pickup", "--app"], id="pickup-app"),
        pytest.param(["close"], id="close"),
    ],
)
def test_review_commands_complete_review_task(installed_launch_agent, ledger_path, command):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])

    assert golem.main([*command, TASK_ID]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_DONE


@pytest.mark.parametrize(
    "status", [golem.STATUS_SCHEDULED, golem.STATUS_WAITING, golem.STATUS_CANCELLED]
)
@pytest.mark.parametrize(
    "pickup_args", [pytest.param([], id="terminal"), pytest.param(["--app"], id="app")]
)
def test_pickup_refuses_tasks_that_never_started(ledger_path, status, pickup_args):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["pickup", TASK_ID, *pickup_args])


def test_pickup_app_opens_task_session_in_desktop_app(fake_commands, ledger_path):
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW)

    assert golem.main(["pickup", TASK_ID, "--app"]) == 0
    assert (
        fake_commands["open-args"].read_text(encoding="utf-8")
        == f"claude://resume?session={read_task(ledger_path)['session_id']}\n"
    )
    assert logged_claude_calls(fake_commands) == []


def test_pickup_app_rejects_agent_arguments(fake_commands, ledger_path):
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW)

    with pytest.raises(SystemExit) as exit_info:
        golem.main(["pickup", TASK_ID, "--app", "--", *AGENT_ARGS])

    assert exit_info.value.code == 2
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""


def test_after_pending_parent_skips_usage_lookup(installed_launch_agent, ledger_path, monkeypatch):
    write_task(ledger_path, golem.STATUS_SCHEDULED, task_id=PARENT_ID)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    assert golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID]) == 0
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING


@pytest.mark.parametrize(
    "status",
    [
        None,
        golem.STATUS_SCHEDULED,
        golem.STATUS_RUNNING,
        golem.STATUS_NEEDS_REVIEW,
        golem.STATUS_DONE,
    ],
)
def test_retry_refuses_unretryable_task_before_usage_lookup(ledger_path, monkeypatch, status):
    if status is not None:
        write_task(ledger_path, status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["retry", TASK_ID])


def test_launch_agent_install_removes_plist_when_bootstrap_fails(fake_commands, monkeypatch):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_BOOTSTRAP_EXIT", "1")

    with pytest.raises(SystemExit, match="could not install agent"):
        golem.main(["agent", "install"])

    assert not golem.launch_agent_plist_path().exists()


@pytest.mark.parametrize("state_home", [None, "", "relative/state"])
def test_ledger_falls_back_to_home_when_xdg_state_home_is_not_absolute(
    fake_commands, monkeypatch, tmp_path, state_home
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if state_home is not None:
        monkeypatch.setenv("XDG_STATE_HOME", state_home)
    golem.main(["agent", "install"])

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0
    assert (tmp_path / ".local" / "state" / "golem" / "tasks.json").exists()


@pytest.mark.parametrize(
    ("exit_code", "output", "expected_result", "expected_status"),
    [
        pytest.param(0, '{"result": "done"}', "done", golem.STATUS_NEEDS_REVIEW, id="success"),
        pytest.param(1, '{"result": "done"}', "done", golem.STATUS_FAILED, id="failure"),
        pytest.param(
            0,
            '[{"type": "system"}, {"type": "result", "result": "done"}]',
            "done",
            golem.STATUS_NEEDS_REVIEW,
            id="verbose",
        ),
        pytest.param(0, "plain output", "plain output", golem.STATUS_NEEDS_REVIEW, id="plain"),
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
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == exit_code
    task = read_task(ledger_path)
    assert task["status"] == expected_status
    assert task["returncode"] == exit_code
    assert task["result"] == expected_result
    assert logged_claude_args(fake_commands) == expected_claude_args(task)
    capsys.readouterr()
    assert golem.main(["show", TASK_ID]) == 0
    assert capsys.readouterr().out == f"{expected_result}\n"


@pytest.mark.parametrize("is_verbose", [False, True])
def test_usage_limited_task_resumes_at_reset_in_same_session(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path, is_verbose
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    agent_args = [*AGENT_ARGS, golem.VERBOSE_ARGUMENT] if is_verbose else AGENT_ARGS
    write_claude_output(
        fake_commands,
        USAGE_LIMIT_RESULT,
        api_error_status=golem.USAGE_LIMIT_HTTP_STATUS,
        is_verbose=is_verbose,
    )
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args])
    session_id = read_task(ledger_path)["session_id"]
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")

    assert golem.main(["_execute", TASK_ID, session_id]) == 0
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_SCHEDULED
    assert task["result"] == USAGE_LIMIT_RESULT
    assert datetime.fromisoformat(task["due_at"]) == RESET_TIME
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
        *([] if is_verbose else [golem.VERBOSE_ARGUMENT]),
        "--name",
        TASK_ID,
        SKIP_PERMISSIONS,
    ]


def test_usage_limited_task_fails_once_resumes_run_out(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])
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
    fake_commands, ledger_path, monkeypatch, find_reset_time
):
    monkeypatch.setattr(golem, "next_reset_time", find_reset_time)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    write_task(ledger_path, golem.STATUS_RUNNING)

    golem.execute_task(TASK_ID, read_task(ledger_path), ())
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_FAILED
    assert task["result"] == USAGE_LIMIT_RESULT


def test_scheduled_task_runs_when_executed(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "scheduled_time", lambda _: RESET_TIME)
    golem.main(["run", TASK_ID, PROMPT, "--at", AT_TEXT])
    task = read_task(ledger_path)
    assert datetime.fromisoformat(task["due_at"]) == RESET_TIME

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW


def create_waiting_chain():
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--at", AT_TEXT, "--", *AGENT_ARGS])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])
    golem.main(["run", GRANDCHILD_ID, GRANDCHILD_PROMPT, "--after", CHILD_ID, "--", *AGENT_ARGS])
    golem.main(["run", SIBLING_ID, SIBLING_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])


def test_scheduled_task_runs_waiting_chain(installed_launch_agent, fake_commands, ledger_path):
    create_waiting_chain()
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))

    golem.main(["_execute", PARENT_ID, tasks[PARENT_ID]["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task["status"] for task in tasks.values()} == {golem.STATUS_NEEDS_REVIEW}
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(tasks[PARENT_ID], PARENT_PROMPT, task_id=PARENT_ID),
        expected_claude_args(tasks[CHILD_ID], CHILD_PROMPT, task_id=CHILD_ID),
        expected_claude_args(tasks[GRANDCHILD_ID], GRANDCHILD_PROMPT, task_id=GRANDCHILD_ID),
        expected_claude_args(tasks[SIBLING_ID], SIBLING_PROMPT, task_id=SIBLING_ID),
    ]


def test_retrying_failed_parent_runs_waiting_dependent(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    create_waiting_chain()
    parent = read_task(ledger_path, PARENT_ID)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    golem.main(["_execute", PARENT_ID, parent["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")

    golem.main(["retry", PARENT_ID, "--now"])
    retry = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, retry["session_id"]])
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert tasks[PARENT_ID]["status"] == golem.STATUS_NEEDS_REVIEW
    assert tasks[CHILD_ID]["status"] == golem.STATUS_NEEDS_REVIEW


@pytest.mark.parametrize(
    ("cancel_id", "expected_statuses"),
    [
        pytest.param(
            PARENT_ID,
            {
                PARENT_ID: golem.STATUS_CANCELLED,
                CHILD_ID: golem.STATUS_CANCELLED,
                GRANDCHILD_ID: golem.STATUS_CANCELLED,
                SIBLING_ID: golem.STATUS_CANCELLED,
            },
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
            id="waiting-child",
        ),
    ],
)
def test_cancelling_task_cancels_waiting_dependents(
    installed_launch_agent, ledger_path, cancel_id, expected_statuses
):
    create_waiting_chain()

    assert golem.main(["cancel", cancel_id]) == 0
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task_id: task["status"] for task_id, task in tasks.items()} == expected_statuses


def test_after_successful_parent_schedules_task_normally(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    child = read_task(ledger_path, CHILD_ID)
    assert child["status"] == golem.STATUS_SCHEDULED
    assert datetime.fromisoformat(child["due_at"]) == RESET_TIME


@pytest.mark.parametrize("status", [None, golem.STATUS_CANCELLED])
def test_after_refuses_missing_or_cancelled_parent(installed_launch_agent, ledger_path, status):
    if status is not None:
        write_task(ledger_path, status, task_id=PARENT_ID)

    with pytest.raises(SystemExit, match=PARENT_ID):
        golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])

    assert CHILD_ID not in golem.load_tasks()


def test_cancel_marks_task_cancelled_and_does_not_run_executor(
    installed_launch_agent, fake_commands, ledger_path
):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["cancel", TASK_ID])

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "status",
    [golem.STATUS_RUNNING, golem.STATUS_NEEDS_REVIEW, golem.STATUS_FAILED, golem.STATUS_DONE],
)
def test_cancel_refuses_non_scheduled_tasks(ledger_path, status):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["cancel", TASK_ID])


def test_leftover_executor_does_not_run_retried_task(
    installed_launch_agent, fake_commands, ledger_path
):
    write_task(ledger_path, golem.STATUS_CANCELLED, session_id="old")
    golem.main(["retry", TASK_ID, "--at", AT_TEXT])

    assert golem.main(["_execute", TASK_ID, "old"]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("retry_args", "reset_time", "expected_due_at"),
    [
        pytest.param(["--now"], None, None, id="now"),
        pytest.param(["--at", AT_TEXT], None, RESET_TIME, id="at"),
        pytest.param([], None, None, id="no-usage-window"),
        pytest.param([], RESET_TIME, RESET_TIME, id="usage-window"),
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
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS])
    failed = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, failed["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setattr(golem, "next_reset_time", lambda: reset_time)
    monkeypatch.setattr(golem, "scheduled_time", lambda _: RESET_TIME)

    assert golem.main(["retry", TASK_ID, *retry_args]) == 0
    retry = read_task(ledger_path)
    assert retry["status"] == golem.STATUS_SCHEDULED
    assert retry["session_id"] != failed["session_id"]
    due_at = datetime.fromisoformat(retry["due_at"])
    if expected_due_at is None:
        assert due_at <= datetime.now(UTC)
    else:
        assert due_at == expected_due_at
    assert golem.main(["_execute", TASK_ID, retry["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert logged_claude_args(fake_commands) == expected_claude_args(retry)


def test_retry_reuses_task_worktree_and_saved_environment(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setenv("LANG", SAVED_LANG)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    first_session_id = read_task(ledger_path)["session_id"]
    golem.main(["_execute", TASK_ID, first_session_id])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setenv("LANG", CURRENT_LANG)

    golem.main(["retry", TASK_ID, "--now"])
    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])

    assert Path(logged_claude_cwds(fake_commands)[-1]).name == first_session_id
    assert WORKTREE_FLAG not in logged_claude_args(fake_commands)
    assert fake_commands["claude-lang"].read_text(encoding="utf-8").splitlines() == [
        SAVED_LANG,
        SAVED_LANG,
    ]


@pytest.mark.parametrize(
    ("five_hour", "seven_day", "expected"),
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
    fake_commands, five_hour, seven_day, expected
):
    frames = [
        {"type": "control_response", "response": {"request_id": "init", "subtype": "success"}},
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
    ]
    fake_commands["claude-stdout"].write_text(
        "\n".join(json.dumps(frame) for frame in frames), encoding="utf-8"
    )

    assert golem.next_reset_time() == expected


def test_failed_dependent_does_not_block_later_sibling(
    installed_launch_agent, ledger_path, monkeypatch
):
    create_waiting_chain()
    prompts: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str]:
        prompts.append(task["prompt"])
        return int(task["prompt"] == CHILD_PROMPT), json.dumps({"result": CLAUDE_RESULT})

    monkeypatch.setattr(golem, "run_claude", run_claude)
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert prompts == [PARENT_PROMPT, CHILD_PROMPT, SIBLING_PROMPT]
    assert tasks[CHILD_ID]["status"] == golem.STATUS_FAILED
    assert tasks[SIBLING_ID]["status"] == golem.STATUS_NEEDS_REVIEW


def test_usage_limit_resume_and_pickup_use_task_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    worktree = read_task(ledger_path)["worktree"]
    write_claude_output(fake_commands, CLAUDE_RESULT)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    golem.main(["pickup", TASK_ID])

    assert logged_claude_cwds(fake_commands)[-2:] == [worktree, worktree]


def test_executor_death_keeps_task_running_until_claude_exits(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    executor = golem.start_task_executor(TASK_ID, task)

    try:
        wait_until(started_path.exists)
        os.kill(executor.pid, signal.SIGKILL)
        executor.wait(timeout=3)

        golem.run_launch_agent_pass()
        assert read_task(ledger_path)["status"] == golem.STATUS_RUNNING
        assert golem.acquire_lock(golem.directory_lock_path(task["cwd"])) is None
        with pytest.raises(SystemExit, match="cannot be retried"):
            golem.main(["retry", TASK_ID, "--now"])

        hold_path.unlink()

        wait_until(lambda: golem.is_run_lock_free(task["session_id"]))
        golem.run_launch_agent_pass()
        assert read_task(ledger_path)["status"] == golem.STATUS_FAILED
    finally:
        hold_path.unlink(missing_ok=True)
        if executor.poll() is None:
            executor.kill()
            executor.wait()


def test_executor_frees_worktree_held_by_stray_claude_process(ledger_path, monkeypatch, tmp_path):
    stray_path = tmp_path / "stray"
    stray_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_STRAY", str(stray_path))
    write_task(ledger_path, golem.STATUS_SCHEDULED)
    task = read_task(ledger_path)

    try:
        golem.main(["_execute", TASK_ID, task["session_id"]])
        directory_lock_file = golem.acquire_lock(golem.directory_lock_path(task["cwd"]))
    finally:
        stray_path.unlink()

    assert directory_lock_file is not None
    golem.release_lock(directory_lock_file)


@pytest.mark.parametrize("contender", ["scheduled", "pickup"])
def test_shared_worktree_blocks_contenders_and_allows_other_worktrees(
    fake_commands, ledger_path, monkeypatch, tmp_path, contender
):
    shared_worktree = tmp_path / "shared"
    other_worktree = tmp_path / "other"
    shared_worktree.mkdir()
    other_worktree.mkdir()
    contender_status = (
        golem.STATUS_SCHEDULED if contender == "scheduled" else golem.STATUS_NEEDS_REVIEW
    )
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_SCHEDULED, worktree=str(shared_worktree)),
            CHILD_ID: task_record(
                contender_status, task_id=CHILD_ID, worktree=str(shared_worktree)
            ),
            SIBLING_ID: task_record(
                golem.STATUS_SCHEDULED, task_id=SIBLING_ID, worktree=str(other_worktree)
            ),
        },
    )
    run_task_ids: list[str] = []

    def execute_contender() -> None:
        if contender == "pickup":
            with pytest.raises(SystemExit, match="directory is busy"):
                golem.main(["pickup", CHILD_ID])
            return
        child = read_task(ledger_path, CHILD_ID)
        assert golem.main(["_execute", CHILD_ID, child["session_id"]]) == 0

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str]:
        run_task_ids.append(task_id)
        if task_id == TASK_ID:
            sibling = read_task(ledger_path, SIBLING_ID)
            execute_contender()
            assert golem.main(["_execute", SIBLING_ID, sibling["session_id"]]) == 0
        return 0, json.dumps({"result": CLAUDE_RESULT})

    monkeypatch.setattr(golem, "run_claude", run_claude)
    first = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, first["session_id"]])

    assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_NEEDS_REVIEW
    if contender == "pickup":
        assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_NEEDS_REVIEW
        assert golem.main(["pickup", CHILD_ID]) == 0
        assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_DONE
        assert run_task_ids == [TASK_ID, SIBLING_ID]
        return
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_SCHEDULED
    child = read_task(ledger_path, CHILD_ID)
    assert golem.main(["_execute", CHILD_ID, child["session_id"]]) == 0
    assert run_task_ids == [TASK_ID, SIBLING_ID, CHILD_ID]


def test_pickup_holds_worktree_until_its_claude_process_exits(
    fake_commands, ledger_path, monkeypatch, tmp_path
):
    worktree = tmp_path / "shared"
    worktree.mkdir()
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    headless_task = task_record(
        golem.STATUS_SCHEDULED,
        worktree=str(worktree),
        environment={"PATH": os.environ["PATH"], "GOLEM_CLAUDE_HOLD": ""},
    )
    write_tasks(
        ledger_path,
        {
            "pickup": task_record(
                golem.STATUS_NEEDS_REVIEW, task_id="pickup", worktree=str(worktree)
            ),
            TASK_ID: headless_task,
        },
    )
    pickup_process = multiprocessing.get_context("fork").Process(
        target=lambda: golem.main(["pickup", "pickup"])
    )
    pickup_process.start()
    headless_session_id = str(headless_task["session_id"])
    try:
        wait_until(started_path.exists)
        assert golem.main(["_execute", TASK_ID, headless_session_id]) == 0
        assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
        pickup_process.kill()
        pickup_process.join(timeout=3)
        assert golem.main(["_execute", TASK_ID, headless_session_id]) == 0
        assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED

        hold_path.unlink()

        wait_until(
            lambda: (
                golem.main(["_execute", TASK_ID, headless_session_id]) == 0
                and read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
            )
        )
    finally:
        hold_path.unlink(missing_ok=True)
        pickup_process.kill()
        pickup_process.join()


@pytest.mark.parametrize("first_dependent", ["busy", "missing"])
def test_dependent_drain_stops_for_busy_worktrees_and_skips_missing_worktrees(
    ledger_path, monkeypatch, tmp_path, first_dependent
):
    first_worktree = tmp_path / "first"
    second_worktree = tmp_path / "second"
    second_worktree.mkdir()
    if first_dependent == "busy":
        first_worktree.mkdir()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, worktree=str(first_worktree)
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(
                golem.STATUS_WAITING,
                task_id=SIBLING_ID,
                after=PARENT_ID,
                worktree=str(second_worktree),
            ),
        },
    )
    run_task_ids: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str]:
        run_task_ids.append(task_id)
        return 0, json.dumps({"result": CLAUDE_RESULT})

    monkeypatch.setattr(golem, "run_claude", run_claude)
    directory_lock_file = None
    if first_dependent == "busy":
        directory_lock_file = golem.acquire_lock(golem.directory_lock_path(str(first_worktree)))
    try:
        golem.drain_dependents(PARENT_ID)
    finally:
        if directory_lock_file is not None:
            golem.release_lock(directory_lock_file)

    if first_dependent == "busy":
        assert run_task_ids == []
        assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING
        assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_WAITING
        return
    assert run_task_ids == [SIBLING_ID]
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_FAILED
    assert read_task(ledger_path, CHILD_ID)["result"] == (
        f"golem: worktree is missing: {first_worktree}"
    )
    assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_NEEDS_REVIEW


def test_agent_recovers_interrupted_dependent_draining_in_ledger_order(
    fake_commands, ledger_path, monkeypatch
):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_SCHEDULED,
                task_id=PARENT_ID,
                prompt=PARENT_PROMPT,
                agent_args=AGENT_ARGS,
            ),
            CHILD_ID: task_record(
                golem.STATUS_WAITING,
                task_id=CHILD_ID,
                after=PARENT_ID,
                prompt=CHILD_PROMPT,
                agent_args=AGENT_ARGS,
            ),
            SIBLING_ID: task_record(
                golem.STATUS_WAITING,
                task_id=SIBLING_ID,
                after=PARENT_ID,
                prompt=SIBLING_PROMPT,
                agent_args=AGENT_ARGS,
            ),
            "failed": task_record(golem.STATUS_FAILED, task_id="failed"),
            GRANDCHILD_ID: task_record(golem.STATUS_WAITING, task_id=GRANDCHILD_ID, after="failed"),
        },
    )
    drain_dependents = golem.drain_dependents

    def crash_before_draining(task_id: str) -> Never:
        raise RuntimeError("executor crashed")

    monkeypatch.setattr(golem, "drain_dependents", crash_before_draining)
    with pytest.raises(RuntimeError):
        golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])
    monkeypatch.setattr(golem, "drain_dependents", drain_dependents)
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING
    started_task_ids: list[str] = []

    def start_in_process(task_id: str, task: dict[str, Any]) -> None:
        started_task_ids.append(task_id)
        golem.main(["_execute", task_id, task["session_id"]])

    monkeypatch.setattr(golem, "start_task_executor", start_in_process)
    golem.run_launch_agent_pass()
    golem.run_launch_agent_pass()

    assert started_task_ids == [PARENT_ID]
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(read_task(ledger_path, PARENT_ID), PARENT_PROMPT, task_id=PARENT_ID),
        expected_claude_args(read_task(ledger_path, CHILD_ID), CHILD_PROMPT, task_id=CHILD_ID),
        expected_claude_args(
            read_task(ledger_path, SIBLING_ID), SIBLING_PROMPT, task_id=SIBLING_ID
        ),
    ]
    assert read_task(ledger_path, GRANDCHILD_ID)["status"] == golem.STATUS_WAITING


@pytest.fixture
def started_task_ids(monkeypatch):
    task_ids: list[str] = []
    monkeypatch.setattr(
        golem, "start_task_executor", lambda task_id, task: task_ids.append(task_id)
    )
    return task_ids


def test_agent_does_not_recover_draining_while_a_dependent_is_running(
    ledger_path, started_task_ids
):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_RUNNING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_WAITING, task_id=SIBLING_ID, after=PARENT_ID),
        },
    )
    child_session_id = read_task(ledger_path, CHILD_ID)["session_id"]
    child_run_lock_file = golem.acquire_run_lock(child_session_id)
    assert child_run_lock_file is not None
    try:
        golem.run_launch_agent_pass()
    finally:
        golem.release_run_lock(child_run_lock_file, child_session_id)

    assert started_task_ids == []


def test_agent_waits_to_start_tasks_until_their_known_directory_is_free(
    ledger_path, started_task_ids, tmp_path
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    own_worktree_task = task_record(
        golem.STATUS_SCHEDULED, task_id="own-worktree", agent_args=[WORKTREE_FLAG, "name"]
    )
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, worktree=str(worktree)
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_SCHEDULED, task_id=SIBLING_ID, after=PARENT_ID),
            "own-worktree": own_worktree_task,
        },
    )
    directory_lock_files = [
        golem.acquire_lock(golem.directory_lock_path(directory))
        for directory in (str(worktree), str(own_worktree_task["cwd"]))
    ]
    try:
        golem.run_launch_agent_pass()
    finally:
        for directory_lock_file in directory_lock_files:
            assert directory_lock_file is not None
            golem.release_lock(directory_lock_file)
    assert started_task_ids == []

    golem.run_launch_agent_pass()

    assert started_task_ids == [PARENT_ID, SIBLING_ID, "own-worktree"]


@pytest.mark.parametrize(
    ("status", "command_args"),
    [
        pytest.param(golem.STATUS_NEEDS_REVIEW, ["pickup", TASK_ID], id="pickup"),
        pytest.param(golem.STATUS_FAILED, ["retry", TASK_ID, "--now"], id="retry"),
    ],
)
def test_missing_worktree_is_reported_without_changing_task(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, status, command_args
):
    missing_worktree = tmp_path / "missing"
    write_task(ledger_path, status, worktree=str(missing_worktree))
    original_tasks = read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=re.escape(f"worktree is missing: {missing_worktree}")):
        golem.main(command_args)

    assert read_tasks(ledger_path) == original_tasks
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


def test_retry_creates_pending_worktree(
    installed_launch_agent, git_repository, ledger_path, monkeypatch
):
    pending_worktree = git_repository / ".claude" / "worktrees" / "pending"
    monkeypatch.chdir(git_repository)
    write_task(
        ledger_path, golem.STATUS_FAILED, worktree=str(pending_worktree), worktree_pending=True
    )

    golem.main(["retry", TASK_ID, "--now"])
    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])

    assert pending_worktree.is_dir()
    assert "worktree_pending" not in read_task(ledger_path)


def test_launch_agent_reaps_finished_executor_while_future_task_is_pending(
    fake_commands, ledger_path, monkeypatch
):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_SCHEDULED),
            "future": task_record(
                golem.STATUS_SCHEDULED, task_id="future", due_at=RESET_TIME.isoformat()
            ),
        },
    )
    start_task_executor = golem.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    sleep_count = 0

    def sleep_while_agent_polls(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            wait_until(lambda: fake_commands["claude-args"].read_text(encoding="utf-8") != "")
            wait_until(
                lambda: (
                    os.waitid(os.P_PID, processes[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    is not None
                )
            )
            return
        assert processes[0].returncode is not None
        golem.cancel_task("future")

    monkeypatch.setattr(golem, "start_task_executor", record_executor)
    monkeypatch.setattr(golem.time, "sleep", sleep_while_agent_polls)

    assert golem.run_launch_agent() == 0
    assert processes[0].returncode is not None


def test_waiting_dependent_of_succeeded_parent_keeps_launch_agent_running():
    tasks = {
        PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
        TASK_ID: task_record(golem.STATUS_WAITING, after=PARENT_ID),
    }

    assert golem.has_pending_tasks(tasks)


def test_claim_records_golem_worktree_resource_and_dependent_reuses_it(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    parent = read_task(ledger_path, PARENT_ID)
    common_directory = golem.git_common_directory(str(git_repository))
    assert common_directory is not None
    parent_worktree = Path(common_directory).parent / ".claude" / "worktrees" / parent["session_id"]
    write_claude_output(
        fake_commands,
        CLAUDE_RESULT,
        is_verbose=True,
        working_directory=str(parent_worktree),
    )

    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    parent_worktree = read_task(ledger_path, PARENT_ID)["worktree"]
    worktrees = golem.load_worktrees()
    resource = worktrees[str(Path(parent_worktree).resolve())]
    assert resource["owner"] == golem.WORKTREE_OWNER_GOLEM
    assert resource["repository"] == golem.git_common_directory(str(git_repository))
    assert read_task(ledger_path, CHILD_ID)["worktree"] == parent_worktree
    tasks = read_tasks(ledger_path)
    tasks[CHILD_ID]["status"] = golem.STATUS_FAILED
    write_tasks(ledger_path, tasks)

    assert golem.main(["retry", CHILD_ID, "--now"]) == 0
    assert read_task(ledger_path, CHILD_ID)["worktree"] == parent_worktree
    assert golem.load_worktrees()[str(Path(parent_worktree).resolve())] == resource


@pytest.mark.parametrize(
    ("owner", "expected_reason"),
    [
        pytest.param(None, "ownership unknown", id="legacy"),
        pytest.param(golem.WORKTREE_OWNER_EXTERNAL, "not created by Golem", id="external"),
    ],
)
def test_worktree_list_retains_non_golem_worktrees(
    capsys, git_repository, ledger_path, owner, expected_reason
):
    worktree = git_worktree(git_repository)
    write_task(ledger_path, golem.STATUS_DONE, worktree=str(worktree))
    if owner is not None:
        write_worktrees(
            {
                str(worktree.resolve()): {
                    "owner": owner,
                    "repository": golem.git_common_directory(str(git_repository)),
                    "session_id": "other",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            }
        )

    assert golem.main(["worktree", "list"]) == 0

    assert expected_reason in capsys.readouterr().out
    with pytest.raises(SystemExit):
        golem.main(["worktree", "release", TASK_ID])


def test_worktree_remove_records_removal_and_preserves_branch(capsys, owned_worktree):
    worktree = owned_worktree()

    assert golem.main(["worktree", "remove", TASK_ID]) == 0
    assert not worktree.exists()
    assert golem.load_worktrees()[str(worktree.resolve())]["removed_at"]
    assert golem.main(["worktree", "remove", TASK_ID]) == 0

    assert "already removed" in capsys.readouterr().out
    assert git_output(worktree.parent, "show-ref", "--verify", "refs/heads/worktree")


@pytest.mark.parametrize("hidden_by", [None, "config", "assume-unchanged", "skip-worktree"])
def test_worktree_remove_retains_local_changes(owned_worktree, hidden_by):
    worktree = owned_worktree()
    changed_file = worktree / "changed"
    if hidden_by == "config":
        git_output(worktree, "config", "status.showUntrackedFiles", "no")
    elif hidden_by is not None:
        changed_file = worktree / "README"
        index_flag = f"--{hidden_by}"
        git_output(worktree, "update-index", index_flag, "README")
    changed_file.write_text("keep", encoding="utf-8")

    assert golem.main(["worktree", "remove", TASK_ID]) == 1

    assert changed_file.read_text(encoding="utf-8") == "keep"
    assert "removal_error" not in golem.load_worktrees()[str(worktree.resolve())]


@pytest.mark.parametrize(
    ("reference", "is_removed"),
    [
        pytest.param(None, False, id="unreferenced"),
        pytest.param("refs/worktree/local", False, id="private-ref"),
        pytest.param("refs/heads/retained", True, id="branch"),
        pytest.param("refs/tags/retained", True, id="tag"),
    ],
)
def test_worktree_remove_preserves_detached_commit(owned_worktree, reference, is_removed):
    worktree = owned_worktree()
    git_output(worktree, "switch", "--detach")
    (worktree / "commit").write_text("keep", encoding="utf-8")
    git_output(worktree, "add", "commit")
    git_output(
        worktree,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "--no-gpg-sign",
        "-qm",
        "unpublished",
    )
    commit_id = git_output(worktree, "rev-parse", "HEAD")
    if reference is not None:
        git_output(worktree, "update-ref", reference, commit_id)

    assert golem.main(["worktree", "remove", TASK_ID]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed
    assert git_output(worktree.parent, "cat-file", "-t", commit_id) == "commit"


@pytest.mark.parametrize("directory_field", ["cwd", "worktree"])
def test_worktree_remove_retains_task_using_nested_directory(
    owned_worktree, ledger_path, directory_field
):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_DONE, worktree=str(worktree)),
            "scheduled": task_record(
                golem.STATUS_SCHEDULED,
                task_id="scheduled",
                **{directory_field: str(worktree / "nested")},
            ),
        },
    )

    assert golem.main(["worktree", "remove", TASK_ID]) == 1
    assert worktree.is_dir()


def test_worktree_remove_retains_nested_worktree(owned_worktree):
    worktree = owned_worktree()
    nested_worktree = worktree / ".claude" / "worktrees" / "nested"
    exclude_path = Path(git_output(worktree, "rev-parse", "--git-path", "info/exclude"))
    with exclude_path.open("a", encoding="utf-8") as exclude_file:
        exclude_file.write("\n.claude/\n")
    git_output(worktree, "worktree", "add", "-qb", "nested", str(nested_worktree))
    changed_file = nested_worktree / "README"
    changed_file.write_text("keep", encoding="utf-8")
    assert not git_output(worktree, "status", "--porcelain", "--untracked-files=all")

    assert golem.main(["worktree", "remove", TASK_ID]) == 1

    assert changed_file.read_text(encoding="utf-8") == "keep"


def test_worktree_remove_does_not_release_a_refused_resource(owned_worktree):
    worktree = owned_worktree(golem.STATUS_NEEDS_REVIEW)

    assert golem.main(["worktree", "remove", TASK_ID]) == 1
    assert "released_at" not in golem.load_worktrees()[str(worktree.resolve())]


@pytest.mark.parametrize(
    "status",
    [golem.STATUS_SCHEDULED, golem.STATUS_WAITING, golem.STATUS_NEEDS_REVIEW],
)
def test_worktree_removal_retains_tasks_that_still_require_it(owned_worktree, status):
    worktree = owned_worktree(status, released_at=RESET_TIME.isoformat())

    assert golem.main(["worktree", "remove", TASK_ID]) == 1
    assert worktree.is_dir()


def test_removed_worktree_is_reported_by_pickup_and_retry(
    installed_launch_agent, owned_worktree, ledger_path
):
    worktree = owned_worktree()
    assert golem.main(["worktree", "remove", TASK_ID]) == 0
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW, worktree=str(worktree))

    with pytest.raises(SystemExit, match="worktree was removed"):
        golem.main(["pickup", TASK_ID])
    write_task(ledger_path, golem.STATUS_FAILED, worktree=str(worktree))
    with pytest.raises(SystemExit, match="worktree was removed"):
        golem.main(["retry", TASK_ID, "--now"])


@pytest.mark.parametrize(
    ("retention_days", "released_ago", "expected_exists"),
    [
        pytest.param(None, timedelta(days=2), True, id="disabled"),
        pytest.param(1, None, True, id="unreleased"),
        pytest.param(1, timedelta(hours=23), True, id="before-retention"),
        pytest.param(1, timedelta(days=1), False, id="at-retention"),
    ],
)
def test_auto_remove_requires_release_and_retention(
    owned_worktree, retention_days, released_ago, expected_exists
):
    released_at = (
        (datetime.now(UTC) - released_ago).isoformat() if released_ago is not None else None
    )
    worktree = owned_worktree(released_at=released_at)
    if retention_days is not None:
        golem.set_worktree_auto_remove(retention_days)

    golem.auto_remove_worktrees()
    assert worktree.is_dir() is expected_exists


@pytest.mark.parametrize("status", [golem.STATUS_FAILED, golem.STATUS_CANCELLED])
def test_launch_agent_removes_released_retryable_worktree(owned_worktree, status):
    worktree = owned_worktree(status)
    assert golem.main(["worktree", "auto-remove", "--after", "0"]) == 0
    assert golem.main(["worktree", "release", TASK_ID]) == 0

    assert golem.run_launch_agent() == 0

    assert not worktree.exists()


@pytest.mark.parametrize("status", [golem.STATUS_FAILED, golem.STATUS_CANCELLED])
def test_worktree_remove_overrides_unreleased_retryable_task(owned_worktree, status):
    worktree = owned_worktree(status)

    assert golem.main(["worktree", "remove", TASK_ID]) == 0
    assert not worktree.exists()


@pytest.mark.parametrize("status", [golem.STATUS_SCHEDULED, golem.STATUS_WAITING])
def test_worktree_removal_retains_inheriting_dependent(owned_worktree, ledger_path, status):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, worktree=str(worktree)),
            CHILD_ID: task_record(status, task_id=CHILD_ID, after=PARENT_ID),
        },
    )

    assert golem.main(["worktree", "remove", PARENT_ID]) == 1
    assert worktree.is_dir()


def test_worktree_list_retains_unreleased_retryable_task(capsys, owned_worktree):
    owned_worktree(golem.STATUS_FAILED)

    assert golem.main(["worktree", "list"]) == 0

    assert "retained" in capsys.readouterr().out


@pytest.mark.parametrize("is_nested_pickup", [False, True])
def test_worktree_remove_retains_directory_in_use(
    owned_worktree, ledger_path, monkeypatch, tmp_path, is_nested_pickup
):
    worktree = owned_worktree()
    tasks = {TASK_ID: task_record(golem.STATUS_DONE, worktree=str(worktree))}
    pickup_id = TASK_ID
    if is_nested_pickup:
        nested = worktree / "nested"
        nested.mkdir()
        tasks[CHILD_ID] = task_record(golem.STATUS_DONE, task_id=CHILD_ID, cwd=str(nested))
        pickup_id = CHILD_ID
    write_tasks(ledger_path, tasks)
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    pickup_process = multiprocessing.get_context("fork").Process(
        target=golem.pickup, args=(pickup_id,), kwargs={"is_app": False}
    )
    pickup_process.start()
    try:
        wait_until(started_path.exists)
        assert golem.main(["worktree", "list"]) == 0
        assert golem.main(["worktree", "remove", TASK_ID]) == 1
        assert worktree.is_dir()
    finally:
        hold_path.unlink()
        pickup_process.join(timeout=3)
        assert pickup_process.exitcode == 0


@pytest.mark.parametrize(
    ("reason", "is_process_missing", "is_removed"),
    [
        pytest.param(
            "claude session fix-thing-session (pid 1 start 2026-10-16T00:00:00Z)",
            True,
            True,
            id="stale-claude-lock",
        ),
        pytest.param(
            "claude session fix-thing-session (pid 1 start 2026-10-16T00:00:00Z)",
            False,
            False,
            id="live-claude-lock",
        ),
        pytest.param("unverified lock", False, False, id="unverified-lock"),
        pytest.param(None, False, False, id="bare-lock"),
    ],
)
def test_worktree_remove_respects_locks(
    owned_worktree, monkeypatch, reason, is_process_missing, is_removed
):
    worktree = owned_worktree()
    git_output(
        worktree.parent,
        "worktree",
        "lock",
        *(["--reason", reason] if reason is not None else []),
        str(worktree),
    )

    def report_process(_: int, __: int) -> None:
        if is_process_missing:
            raise ProcessLookupError

    monkeypatch.setattr(golem.os, "kill", report_process)

    assert golem.main(["worktree", "remove", TASK_ID]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed


def test_worktree_remove_records_git_removal_failure(capsys, owned_worktree, monkeypatch):
    worktree = owned_worktree()
    run_git = golem.run_git

    def fail_removal(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="could not remove")
        return run_git(directory, *arguments)

    monkeypatch.setattr(golem, "run_git", fail_removal)

    assert golem.main(["worktree", "remove", TASK_ID]) == 1
    resource = golem.load_worktrees()[str(worktree.resolve())]
    assert resource["removal_error"] == "could not remove"
    assert "removed_at" not in resource
    assert worktree.is_dir()
    assert golem.main(["worktree", "list"]) == 0
    assert "could not remove" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "command", "is_nested"),
    [
        pytest.param(golem.STATUS_NEEDS_REVIEW, ["pickup", TASK_ID], False, id="pickup"),
        pytest.param(golem.STATUS_FAILED, ["retry", TASK_ID, "--now"], False, id="retry"),
        pytest.param(golem.STATUS_DONE, ["pickup", TASK_ID], True, id="nested-pickup"),
        pytest.param(golem.STATUS_FAILED, ["retry", TASK_ID, "--now"], True, id="nested-retry"),
    ],
)
def test_task_reuse_revokes_worktree_release(
    installed_launch_agent, owned_worktree, ledger_path, status, command, is_nested
):
    worktree = owned_worktree(status, released_at=RESET_TIME.isoformat())
    if is_nested:
        nested = worktree / "nested"
        nested.mkdir()
        write_task(ledger_path, status, cwd=str(nested))

    assert golem.main(command) == 0

    assert "released_at" not in golem.load_worktrees()[str(worktree.resolve())]


@pytest.mark.parametrize(
    ("state", "is_removed"),
    [("created", True), ("retried", True), ("replaced", False), ("preexisting", False)],
)
def test_cleanup_verifies_original_worktree_ownership(
    installed_launch_agent, git_repository, ledger_path, monkeypatch, tmp_path, state, is_removed
):
    monkeypatch.chdir(git_repository)
    claude_path = tmp_path / "claude"
    claude_path.write_text(
        """#!/bin/sh
if [ "${GOLEM_CLAUDE_EXIT:-0}" != 0 ]; then
    exit "$GOLEM_CLAUDE_EXIT"
fi
while [ "$#" -gt 0 ]; do
    if [ "$1" = '--worktree' ]; then
        shift
        /usr/bin/git worktree add -qb created ".claude/worktrees/$1" || exit 1
    fi
    shift
done
printf '%s' '{"result":"done"}'
""",
        encoding="utf-8",
    )
    claude_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    worktree = git_repository / ".claude" / "worktrees" / task["session_id"]
    resource_before_retry = None

    if state == "preexisting":
        git_output(git_repository, "worktree", "add", "-qb", "external", str(worktree))
    if state == "retried":
        monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
        assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 1
        resource_before_retry = golem.load_worktrees()[str(worktree.resolve())]
        monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
        assert golem.main(["retry", TASK_ID, "--now"]) == 0
        retried_task = read_task(ledger_path)
        assert retried_task["session_id"] != task["session_id"]
        assert retried_task["worktree"] == str(worktree)
        task = retried_task

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    if state == "retried":
        assert golem.load_worktrees()[str(worktree.resolve())] == resource_before_retry
    assert golem.main(["close", TASK_ID]) == 0
    if state == "replaced":
        git_output(git_repository, "worktree", "remove", str(worktree))
        git_output(git_repository, "worktree", "add", "-qb", "external", str(worktree))

    assert golem.main(["worktree", "remove", TASK_ID]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed


def test_close_does_not_release_worktree(owned_worktree):
    worktree = owned_worktree(golem.STATUS_NEEDS_REVIEW)

    assert golem.main(["close", TASK_ID]) == 0

    assert "released_at" not in golem.load_worktrees()[str(worktree.resolve())]
