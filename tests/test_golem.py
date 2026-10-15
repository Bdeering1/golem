import json
import multiprocessing
import os
import plistlib
import re
import select
import shutil
import signal
import subprocess
from datetime import UTC, datetime
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
            PARENT_ID: task_record(golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID),
            CHILD_ID: task_record(
                golem.STATUS_WAITING,
                task_id=CHILD_ID,
                after=PARENT_ID,
                worktree=str(first_worktree),
            ),
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


def test_agent_waits_to_start_tasks_until_their_recorded_worktree_is_free(
    ledger_path, started_task_ids, tmp_path
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, worktree=str(worktree)
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_SCHEDULED, task_id=SIBLING_ID, after=PARENT_ID),
        },
    )
    directory_lock_file = golem.acquire_lock(golem.directory_lock_path(str(worktree)))
    assert directory_lock_file is not None
    try:
        golem.run_launch_agent_pass()
    finally:
        golem.release_lock(directory_lock_file)
    assert started_task_ids == []

    golem.run_launch_agent_pass()

    assert started_task_ids == [PARENT_ID, SIBLING_ID]


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


def test_dependent_fails_when_inherited_worktree_is_missing(ledger_path, tmp_path):
    missing_worktree = tmp_path / "missing"
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, worktree=str(missing_worktree)
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
        },
    )

    golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])

    child = read_task(ledger_path, CHILD_ID)
    assert child["status"] == golem.STATUS_FAILED
    assert child["result"] == f"golem: worktree is missing: {missing_worktree}"


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
