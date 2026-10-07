import fcntl
import json
import os
import shutil
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
def installed_agent():
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


def expected_claude_args(task, prompt=PROMPT, agent_args=AGENT_ARGS):
    return [
        "--session-id",
        task["session_id"],
        "--print",
        prompt,
        "--output-format",
        "json",
        *agent_args,
        "--dangerously-skip-permissions",
    ]


def raise_usage_lookup_error() -> Never:
    raise SystemExit


@pytest.mark.parametrize(
    ("command", "status"),
    [
        pytest.param(["run", TASK_ID, PROMPT, "--now"], None, id="run"),
        pytest.param(["retry", TASK_ID, "--now"], golem.STATUS_FAILED, id="retry"),
    ],
)
def test_run_and_retry_require_an_installed_agent(ledger_path, command, status):
    if status is not None:
        write_task(ledger_path, status)

    with pytest.raises(SystemExit, match="agent is not installed"):
        golem.main(command)

    assert golem.main(["agent", "install"]) == 0
    assert golem.main(["agent", "uninstall"]) == 0
    assert not golem.agent_plist_path().exists()

    with pytest.raises(SystemExit, match="agent is not installed"):
        golem.main(command)


def test_run_queues_and_wakes_agent(installed_agent, fake_commands, ledger_path):
    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""
    assert "kickstart" in fake_commands["launchctl-args"].read_text(encoding="utf-8")


def test_run_warns_when_waking_agent_fails(installed_agent, ledger_path, monkeypatch, capsys):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_KICKSTART_EXIT", "1")

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0

    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert "could not wake agent" in capsys.readouterr().err


@pytest.mark.parametrize("agent_case", ["due-and-dead", "held-lock", "waiting"], ids=str)
def test_agent_pass_starts_due_tasks_and_handles_running_tasks(
    ledger_path, monkeypatch, agent_case
):
    if agent_case == "due-and-dead":
        write_tasks(
            ledger_path,
            {
                TASK_ID: task_record(golem.STATUS_SCHEDULED, due_at=datetime.now(UTC).isoformat()),
                "dead": task_record(golem.STATUS_RUNNING, task_id="dead"),
                "future": task_record(
                    golem.STATUS_SCHEDULED,
                    task_id="future",
                    due_at=RESET_TIME.isoformat(),
                ),
            },
        )
        started_task_ids: list[str] = []
        monkeypatch.setattr(
            golem, "start_task_executor", lambda task_id, task: started_task_ids.append(task_id)
        )

        assert golem.start_due_tasks()
        assert started_task_ids == [TASK_ID]
        assert read_tasks(ledger_path)["dead"]["status"] == golem.STATUS_FAILED
        return

    if agent_case == "held-lock":
        write_task(ledger_path, golem.STATUS_RUNNING, session_id="live")
        lock_file = golem.run_lock_path("live").open("a+", encoding="utf-8")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert golem.start_due_tasks()
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()

        assert read_task(ledger_path)["status"] == golem.STATUS_RUNNING
        return

    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_FAILED, task_id=PARENT_ID),
            TASK_ID: task_record(golem.STATUS_WAITING, after=PARENT_ID),
        },
    )

    assert not golem.start_due_tasks()


def test_changed_state_home_requires_reinstall(installed_agent, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "other-state"))

    with pytest.raises(SystemExit, match="agent is not installed"):
        golem.main(["run", TASK_ID, PROMPT, "--now"])


def test_agent_loop_exits_when_no_work_remains(ledger_path, monkeypatch):
    write_task(ledger_path, golem.STATUS_SCHEDULED, due_at=RESET_TIME.isoformat())
    monkeypatch.setattr(
        golem, "time", type("Clock", (), {"sleep": lambda *_: golem.cancel_task(TASK_ID)})()
    )
    assert golem.run_agent() == 0


def test_agent_executor_uses_only_saved_environment(
    installed_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("LANG", "en_CA.UTF-8")
    monkeypatch.setenv("LC_TIME", "fr_CA.UTF-8")
    monkeypatch.setenv("UNRELATED", "not-saved")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_TIME", "en_US.UTF-8")
    monkeypatch.delenv("UNRELATED")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    start_task_executor = golem.start_task_executor
    processes = []

    def record_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[Any]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    monkeypatch.setattr(golem, "start_task_executor", record_executor)

    assert golem.start_due_tasks()
    assert len(processes) == 1
    try:
        processes[0].wait(timeout=3)
    except subprocess.TimeoutExpired:
        processes[0].kill()
        processes[0].wait()
        pytest.fail("task executor did not exit")

    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert fake_commands["claude-lang"].read_text(encoding="utf-8") == "en_CA.UTF-8\n"
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8") == "fr_CA.UTF-8\n"
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


@pytest.mark.parametrize(
    ("agent_args", "worktree_name", "has_permission", "is_git_repository"),
    [
        pytest.param([], None, True, True, id="defaults"),
        pytest.param([], None, True, False, id="non-git"),
        pytest.param(["--permission-mode", "plan"], None, False, True, id="permission-mode"),
        pytest.param(["--worktree", "custom"], "custom", True, True, id="worktree"),
    ],
)
def test_headless_defaults_respect_agent_arguments(
    installed_agent,
    fake_commands,
    git_repository,
    ledger_path,
    monkeypatch,
    agent_args,
    worktree_name,
    has_permission,
    is_git_repository,
):
    if is_git_repository:
        monkeypatch.chdir(git_repository)
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    arguments = fake_commands["claude-args"].read_text(encoding="utf-8").splitlines()
    if is_git_repository:
        assert arguments.count("--worktree") == 1
        worktree_index = arguments.index("--worktree")
        assert arguments[worktree_index + 1] == (worktree_name or task["session_id"])
    else:
        assert "--worktree" not in arguments
    assert ("--dangerously-skip-permissions" in arguments) is has_permission


def test_dependent_runs_in_parent_worktree(
    installed_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    parent = read_task(ledger_path, PARENT_ID)
    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == 0
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert tasks[CHILD_ID]["worktree"] == tasks[PARENT_ID]["worktree"]
    assert (
        fake_commands["claude-cwd"].read_text(encoding="utf-8").splitlines()[-1]
        == tasks[PARENT_ID]["worktree"]
    )


def write_claude_output(fake_commands, result, *, api_error_status=None, is_verbose=False):
    output = {
        "api_error_status": api_error_status,
        "is_error": api_error_status is not None,
        "result": result,
        "type": "result",
    }
    response = [{"type": "system", "subtype": "init"}, output] if is_verbose else output
    fake_commands["claude-stdout"].write_text(json.dumps(response), encoding="utf-8")


@pytest.mark.parametrize("command", ["pickup", "close"])
def test_review_commands_complete_review_task(installed_agent, ledger_path, command):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])

    assert golem.main([command, TASK_ID]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_DONE


@pytest.mark.parametrize(
    "status", [golem.STATUS_SCHEDULED, golem.STATUS_WAITING, golem.STATUS_CANCELLED]
)
def test_pickup_refuses_tasks_that_never_started(ledger_path, status):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["pickup", TASK_ID])


def test_after_pending_parent_skips_usage_lookup(installed_agent, ledger_path, monkeypatch):
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


def test_agent_install_removes_plist_when_bootstrap_fails(fake_commands, monkeypatch):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_BOOTSTRAP_EXIT", "1")

    with pytest.raises(SystemExit, match="could not install agent"):
        golem.main(["agent", "install"])

    assert not golem.agent_plist_path().exists()


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
    ("exit_code", "output", "expected_status"),
    [
        pytest.param(0, '{"result": "done"}', golem.STATUS_NEEDS_REVIEW, id="success"),
        pytest.param(1, '{"result": "done"}', golem.STATUS_FAILED, id="failure"),
        pytest.param(
            0,
            '[{"type": "system"}, {"type": "result", "result": "done"}]',
            golem.STATUS_NEEDS_REVIEW,
            id="verbose",
        ),
        pytest.param(0, "plain output", golem.STATUS_NEEDS_REVIEW, id="plain"),
    ],
)
def test_run_now_records_claude_result(
    installed_agent,
    fake_commands,
    monkeypatch,
    ledger_path,
    capsys,
    exit_code,
    output,
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
    assert task["result"] == ("done" if output != "plain output" else output)
    assert logged_claude_args(fake_commands) == expected_claude_args(task)
    assert capsys.readouterr().out == f"golem: queued {TASK_ID}\n"
    assert golem.main(["show", TASK_ID]) == 0
    assert capsys.readouterr().out == f"{task['result']}\n"


@pytest.mark.parametrize("is_verbose", [False, True])
def test_usage_limited_task_resumes_at_reset_in_same_session(
    installed_agent, fake_commands, monkeypatch, ledger_path, is_verbose
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    agent_args = [*AGENT_ARGS, "--verbose"] if is_verbose else AGENT_ARGS
    write_claude_output(
        fake_commands,
        USAGE_LIMIT_RESULT,
        api_error_status=golem.USAGE_LIMIT_HTTP_STATUS,
        is_verbose=is_verbose,
    )
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args])
    session_id = read_task(ledger_path)["session_id"]

    golem.main(["_execute", TASK_ID, session_id])
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_SCHEDULED
    assert task["result"] == USAGE_LIMIT_RESULT
    assert datetime.fromisoformat(task["due_at"]) == RESET_TIME
    write_claude_output(fake_commands, CLAUDE_RESULT, is_verbose=is_verbose)
    golem.main(["_execute", TASK_ID, session_id])

    assert logged_claude_args(fake_commands) == [
        "--resume",
        session_id,
        "--print",
        golem.USAGE_LIMIT_RESUME_PROMPT,
        "--output-format",
        "json",
        *agent_args,
        "--dangerously-skip-permissions",
    ]


def test_usage_limited_task_fails_once_resumes_run_out(
    installed_agent, fake_commands, monkeypatch, ledger_path
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

    golem.execute_task(TASK_ID, read_task(ledger_path))
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_FAILED
    assert task["result"] == USAGE_LIMIT_RESULT


def test_scheduled_task_runs_when_executed(
    installed_agent, fake_commands, ledger_path, monkeypatch
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


def test_scheduled_task_runs_waiting_chain(installed_agent, fake_commands, ledger_path):
    create_waiting_chain()
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))

    golem.main(["_execute", PARENT_ID, tasks[PARENT_ID]["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task["status"] for task in tasks.values()} == {golem.STATUS_NEEDS_REVIEW}
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(tasks[PARENT_ID], PARENT_PROMPT),
        expected_claude_args(tasks[CHILD_ID], CHILD_PROMPT),
        expected_claude_args(tasks[GRANDCHILD_ID], GRANDCHILD_PROMPT),
        expected_claude_args(tasks[SIBLING_ID], SIBLING_PROMPT),
    ]


def test_retrying_failed_parent_runs_waiting_dependent(
    installed_agent, fake_commands, ledger_path, monkeypatch
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
    installed_agent, ledger_path, cancel_id, expected_statuses
):
    create_waiting_chain()

    assert golem.main(["cancel", cancel_id]) == 0
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task_id: task["status"] for task_id, task in tasks.items()} == expected_statuses


def test_after_successful_parent_schedules_task_normally(installed_agent, ledger_path, monkeypatch):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    child = read_task(ledger_path, CHILD_ID)
    assert child["status"] == golem.STATUS_SCHEDULED
    assert datetime.fromisoformat(child["due_at"]) == RESET_TIME


@pytest.mark.parametrize("status", [None, golem.STATUS_CANCELLED])
def test_after_refuses_missing_or_cancelled_parent(installed_agent, ledger_path, status):
    if status is not None:
        write_task(ledger_path, status, task_id=PARENT_ID)

    with pytest.raises(SystemExit, match=PARENT_ID):
        golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])


def test_cancel_marks_task_cancelled_and_does_not_run_executor(
    installed_agent, fake_commands, ledger_path
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


def test_leftover_executor_does_not_run_retried_task(installed_agent, fake_commands, ledger_path):
    write_task(ledger_path, golem.STATUS_CANCELLED, session_id="old")
    golem.main(["retry", TASK_ID, "--at", "9pm"])

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
    installed_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    retry_args,
    reset_time,
    expected_due_at,
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
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


def test_retry_keeps_worktree_environment_and_parent(
    installed_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setattr(golem, "next_reset_time", lambda: None)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    child = read_task(ledger_path, CHILD_ID)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    assert golem.main(["_execute", CHILD_ID, child["session_id"]]) == 1
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    child = read_task(ledger_path, CHILD_ID)

    assert golem.main(["retry", CHILD_ID, "--now"]) == 0
    retry = read_task(ledger_path, CHILD_ID)
    assert retry["after"] == PARENT_ID
    assert retry["environment"] == child["environment"]
    assert retry["worktree"] == child["worktree"]
    assert golem.main(["_execute", CHILD_ID, retry["session_id"]]) == 0

    assert (
        fake_commands["claude-cwd"].read_text(encoding="utf-8").splitlines()[-1]
        == retry["worktree"]
    )
    assert "--worktree" not in logged_claude_args(fake_commands)


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


def test_dependent_scheduling_failure_does_not_block_later_sibling(
    installed_agent, ledger_path, monkeypatch
):
    create_waiting_chain()
    prompts: list[str] = []

    def run_claude(task: dict[str, Any]) -> tuple[int, str]:
        prompts.append(task["prompt"])
        if task["prompt"] == CHILD_PROMPT:
            return 1, json.dumps(
                {
                    "api_error_status": golem.USAGE_LIMIT_HTTP_STATUS,
                    "result": USAGE_LIMIT_RESULT,
                }
            )
        return 0, json.dumps({"result": CLAUDE_RESULT})

    monkeypatch.setattr(golem, "next_reset_time", lambda: None)
    monkeypatch.setattr(golem, "run_claude", run_claude)
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert prompts == [PARENT_PROMPT, CHILD_PROMPT, SIBLING_PROMPT]
    assert tasks[CHILD_ID]["status"] == golem.STATUS_FAILED
    assert tasks[SIBLING_ID]["status"] == golem.STATUS_NEEDS_REVIEW


def test_usage_limit_resume_and_pickup_use_task_worktree(
    installed_agent, fake_commands, git_repository, ledger_path, monkeypatch
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

    assert fake_commands["claude-cwd"].read_text(encoding="utf-8").splitlines()[-2:] == [
        worktree,
        worktree,
    ]
