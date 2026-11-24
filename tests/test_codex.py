import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from golem import cli, executor, launch_agent, ledger, spawn
from golem.backends import claude, codex, common
from golem.worktrees import git, task_directories
from tests import support


def logged_codex_calls(fake_commands):
    return support.logged_calls(fake_commands, "codex")


def logged_codex_args(fake_commands):
    calls = logged_codex_calls(fake_commands)
    return calls[-1] if calls else []


def write_codex_rate_limits(monkeypatch, tmp_path, primary, secondary):
    limits_path = tmp_path / "limits.json"
    limits_path.write_text(
        json.dumps({"rateLimits": {"primary": primary, "secondary": secondary}}), encoding="utf-8"
    )
    monkeypatch.setenv("GOLEM_CODEX_RATE_LIMITS", str(limits_path))
    return limits_path


@pytest.mark.parametrize(
    ("events", "exit_code", "result", "status"),
    [
        pytest.param(
            [
                {"type": "thread.started", "thread_id": "codex-thread"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Codex completed"},
                },
                {"type": "turn.completed", "usage": {}},
            ],
            0,
            "Codex completed",
            ledger.STATUS_FINISHED,
            id="success",
        ),
        pytest.param(
            [
                {"type": "thread.started", "thread_id": "codex-thread"},
                {"type": "turn.failed", "error": {"message": "ordinary failure"}},
            ],
            1,
            "ordinary failure",
            ledger.STATUS_FAILED,
            id="failure",
        ),
    ],
)
def test_codex_run_records_result_and_thread(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    tmp_path,
    capsys,
    events,
    exit_code,
    result,
    status,
):
    support.write_codex_events(monkeypatch, tmp_path, events)
    monkeypatch.setenv("GOLEM_CODEX_EXIT", str(exit_code))

    assert cli.main(["run", support.PROMPT, "--backend", "codex", "--now", "--", "-m", "x"]) == 0
    task = support.read_task(ledger_path)
    assert task["backend"] == ledger.BACKEND_CODEX
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == exit_code

    task = support.read_task(ledger_path)
    assert task["thread_id"] == "codex-thread"
    assert task["result"] == result
    assert task["status"] == status
    assert logged_codex_args(fake_commands) == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-m",
        "x",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        codex.CODEX_PROVIDER_OVERRIDE,
        "-",
    ]
    assert (
        fake_commands["codex-stdin"].read_text(encoding="utf-8").endswith(f"{support.PROMPT}\x1e\n")
    )
    if status == ledger.STATUS_FINISHED:
        assert cli.main(["show", support.TASK_ID]) == 0
        assert capsys.readouterr().out.endswith(f"{result}\n")


@pytest.mark.parametrize(
    ("primary", "secondary", "expected_offset"),
    [
        pytest.param({"usedPercent": 100, "resetsAt": 7200}, None, 7200, id="exhausted-primary"),
        pytest.param(
            {"usedPercent": 100, "resetsAt": 3600},
            {"usedPercent": 100, "resetsAt": 7200},
            7200,
            id="latest-exhausted",
        ),
        pytest.param({"usedPercent": 25, "resetsAt": 3600}, None, 3600, id="active"),
        pytest.param({"usedPercent": 0, "resetsAt": 3600}, None, None, id="unused"),
        pytest.param(None, None, None, id="null-primary"),
    ],
)
def test_next_codex_reset_time(
    fake_commands, monkeypatch, tmp_path, primary, secondary, expected_offset
):
    now = datetime.now(UTC)
    primary, secondary = (
        None
        if limit is None
        else {**limit, "resetsAt": int((now + timedelta(seconds=limit["resetsAt"])).timestamp())}
        for limit in (primary, secondary)
    )
    write_codex_rate_limits(monkeypatch, tmp_path, primary, secondary)

    reset_time = codex.next_reset_time(os.environ.copy())

    if expected_offset is None:
        assert reset_time is None
    else:
        assert reset_time is not None
        assert abs((reset_time - now).total_seconds() - expected_offset) < 2


def test_codex_usage_limit_resumes_saved_conversation(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    support.write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}},
        ],
    )
    reset_time = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    monkeypatch.setattr(codex, "next_reset_time", lambda _environment: reset_time)

    cli.main(["run", support.PROMPT, "--backend", "codex", "--now"])
    task = support.read_task(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_SCHEDULED
    assert datetime.fromisoformat(task["due_at"]) == reset_time

    support.write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}},
        ],
    )
    support.set_task_due_now(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert logged_codex_args(fake_commands)[-3:] == ["resume", "codex-thread", "-"]
    assert (
        fake_commands["codex-stdin"]
        .read_text(encoding="utf-8")
        .endswith(f"{common.USAGE_LIMIT_RESUME_PROMPT}\x1e\n")
    )


def test_codex_creates_and_uses_golem_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    cli.main(["run", support.PROMPT, "--backend", "codex", "--now"])
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0

    worktree = Path(support.read_task(ledger_path)["worktree"])
    assert worktree.is_dir()
    assert (
        support.git_output(worktree, "branch", "--show-current") == f"worktree-{task['session_id']}"
    )
    assert logged_codex_args(fake_commands) == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        codex.CODEX_PROVIDER_OVERRIDE,
        "-",
    ]
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(worktree)
    assert task_directories.is_worktree_owner_verified(str(worktree), task["session_id"])
    resource = ledger.load_worktrees()[str(worktree.resolve())]
    assert resource["owner"] == task_directories.WORKTREE_OWNER_GOLEM
    assert resource["session_id"] == task["session_id"]
    common_directory = git.git_common_directory(str(git_repository))
    assert common_directory is not None
    exclude_path = Path(common_directory) / "info" / "exclude"
    assert ".claude/worktrees/" in exclude_path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    "agent_args",
    [
        pytest.param(["-s", "read-only"], id="short-sandbox"),
        pytest.param(["--sandbox=read-only"], id="sandbox"),
        pytest.param(["--yolo"], id="yolo"),
        pytest.param(["--approve-for-me"], id="approve"),
        pytest.param(["-c", 'sandbox_mode="read-only"'], id="sandbox-config"),
    ],
)
def test_codex_permission_arguments_suppress_unattended_default(
    installed_launch_agent, fake_commands, ledger_path, agent_args
):
    assert (
        cli.main(
            [
                "run",
                support.PROMPT,
                "--backend",
                "codex",
                "--now",
                "--",
                *agent_args,
            ]
        )
        == 0
    )
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert "--dangerously-bypass-approvals-and-sandbox" not in logged_codex_args(fake_commands)


def test_codex_rejects_api_key_accounts_before_execution(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    account_path = tmp_path / "account.json"
    account_path.write_text(
        json.dumps({"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}), encoding="utf-8"
    )
    monkeypatch.setenv("GOLEM_CODEX_ACCOUNT", str(account_path))

    with pytest.raises(SystemExit, match="ChatGPT subscription"):
        cli.main(["run", support.PROMPT, "--backend", "codex", "--now"])

    assert not any(call[0] == "exec" for call in logged_codex_calls(fake_commands))
    support.write_task(ledger_path, ledger.STATUS_SCHEDULED, backend=ledger.BACKEND_CODEX)
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 1
    assert support.read_task(ledger_path)["result"] == codex.CODEX_SUBSCRIPTION_ERROR
    assert not any(call[0] == "exec" for call in logged_codex_calls(fake_commands))


@pytest.mark.parametrize(
    ("agent_args", "argument"),
    [
        pytest.param(["--oss"], "--oss", id="oss"),
        pytest.param(["--local-provider=ollama"], "--local-provider=ollama", id="local-provider"),
        pytest.param(["-c", 'model_provider="x"'], 'model_provider="x"', id="provider-config"),
        pytest.param(
            ["--config=forced_login_method=api"],
            "forced_login_method=api",
            id="login-config",
        ),
        pytest.param(["--worktree"], "--worktree", id="worktree"),
    ],
)
def test_codex_run_rejects_billing_bypass_arguments(installed_launch_agent, agent_args, argument):
    with pytest.raises(SystemExit, match=re.escape(argument)):
        cli.main(
            [
                "run",
                support.PROMPT,
                "--backend",
                "codex",
                "--now",
                "--",
                *agent_args,
            ]
        )


def test_codex_processes_remove_api_keys_from_every_child(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden")
    monkeypatch.setenv("CODEX_API_KEY", "forbidden")
    assert cli.main(["run", support.PROMPT, "--backend", "codex", "--now"]) == 0
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert cli.main(["pickup", support.TASK_ID]) == 0
    assert {call[0] for call in logged_codex_calls(fake_commands)} == {
        "app-server",
        "exec",
        "resume",
    }
    assert set(fake_commands["codex-api-keys"].read_text(encoding="utf-8").splitlines()) == {"0 0"}


@pytest.mark.parametrize(("is_error", "reset_offset"), [(True, 3600), (False, -3600)])
def test_next_codex_reset_time_rejects_unavailable_or_past_limits(
    fake_commands, monkeypatch, tmp_path, is_error, reset_offset
):
    if is_error:
        monkeypatch.setenv("GOLEM_CODEX_ERROR", "1")
    else:
        write_codex_rate_limits(
            monkeypatch,
            tmp_path,
            {
                "usedPercent": 100,
                "resetsAt": int((datetime.now(UTC) + timedelta(seconds=reset_offset)).timestamp()),
            },
            None,
        )

    with pytest.raises(SystemExit, match=re.escape(common.USAGE_ERROR)):
        codex.next_reset_time(os.environ.copy())


def test_codex_app_server_reads_batched_responses(fake_commands, monkeypatch, tmp_path):
    reset_time = datetime.now(UTC) + timedelta(hours=1)
    write_codex_rate_limits(
        monkeypatch,
        tmp_path,
        {"usedPercent": 100, "resetsAt": int(reset_time.timestamp())},
        None,
    )
    monkeypatch.setenv("GOLEM_CODEX_BATCH_RESPONSES", "1")

    assert codex.next_reset_time(os.environ.copy()) == reset_time.replace(microsecond=0)


def test_codex_usage_limit_stores_result_and_stops_after_resume_limit(
    installed_launch_agent, ledger_path, monkeypatch, tmp_path
):
    usage_error = "You\N{RIGHT SINGLE QUOTATION MARK}ve hit your usage limit"
    support.write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "turn.failed", "error": {"message": usage_error}},
        ],
    )
    monkeypatch.setenv("GOLEM_CODEX_EXIT", "1")
    reset_time = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    monkeypatch.setattr(codex, "next_reset_time", lambda _environment: reset_time)
    assert cli.main(["run", support.PROMPT, "--backend", "codex", "--now"]) == 0
    session_id = support.read_task(ledger_path)["session_id"]

    for resume_count in range(executor.MAX_USAGE_LIMIT_RESUMES):
        support.set_task_due_now(ledger_path)
        assert cli.main(["_execute", support.TASK_ID, session_id]) == 0
        task = support.read_task(ledger_path)
        assert task["session_id"] == session_id
        assert task["result"] == usage_error
        assert task["usage_limit_resume_count"] == resume_count + 1
        assert datetime.fromisoformat(task["due_at"]) == reset_time

    support.set_task_due_now(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, session_id]) == 1
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FAILED


@pytest.mark.parametrize(
    ("backend_id", "agent_args"),
    [
        pytest.param(ledger.BACKEND_CODEX, [], id="codex"),
        pytest.param(ledger.BACKEND_T3, ["--provider", "codex"], id="t3-codex"),
    ],
)
@pytest.mark.parametrize(
    ("status", "command"),
    [
        pytest.param(ledger.STATUS_FAILED, "retry", id="retry"),
        pytest.param(ledger.STATUS_SCHEDULED, "reschedule", id="reschedule"),
    ],
)
def test_codex_retry_and_reschedule_use_codex_reset_time(
    installed_launch_agent, ledger_path, monkeypatch, status, command, backend_id, agent_args
):
    reset_time = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    support.write_task(ledger_path, status, backend=backend_id, agent_args=agent_args)
    monkeypatch.setattr(
        claude, "next_reset_time", lambda _environment: pytest.fail("Claude lookup ran")
    )
    monkeypatch.setattr(codex, "next_reset_time", lambda _environment: reset_time)

    assert cli.main([command, support.TASK_ID]) == 0

    assert datetime.fromisoformat(support.read_task(ledger_path)["due_at"]) == reset_time


@pytest.mark.parametrize(
    "client_args",
    [pytest.param([], id="default"), pytest.param(["--client", "codex-cli"], id="codex-cli")],
)
def test_codex_pickup_resumes_thread_in_task_worktree(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, client_args
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    support.write_task(
        ledger_path,
        ledger.STATUS_FINISHED,
        backend=ledger.BACKEND_CODEX,
        thread_id="codex-thread",
        worktree=str(worktree),
    )

    assert cli.main(["pickup", support.TASK_ID, *client_args, "--", "-m", "x"]) == 0
    assert logged_codex_args(fake_commands) == [
        "resume",
        "codex-thread",
        "-m",
        "x",
        "-c",
        codex.CODEX_PROVIDER_OVERRIDE,
    ]
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(worktree)
    support.write_task(ledger_path, ledger.STATUS_FINISHED, backend=ledger.BACKEND_CODEX)

    with pytest.raises(SystemExit, match="--oss"):
        cli.main(["pickup", support.TASK_ID, "--", "--oss"])


def test_codex_non_git_run_and_retry_keep_worktree_and_arguments(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, tmp_path
):
    assert cli.main(["run", support.PROMPT, "--backend", "codex", "--now"]) == 0
    task = support.read_task(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(
        Path.cwd()
    )

    monkeypatch.chdir(git_repository)
    support.write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "failed-thread"},
            {"type": "turn.failed", "error": {"message": "ordinary failure"}},
        ],
    )
    monkeypatch.setenv("GOLEM_CODEX_EXIT", "1")
    assert cli.main(["run", support.PROMPT, "--backend", "codex", "--now", "--", "-m", "x"]) == 0
    retry_id = "wor2"
    failed = support.read_task(ledger_path, retry_id)
    assert cli.main(["_execute", retry_id, failed["session_id"]]) == 1
    worktree = support.read_task(ledger_path, retry_id)["worktree"]
    monkeypatch.setenv("GOLEM_CODEX_EXIT", "0")
    support.write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "fresh-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}},
        ],
    )

    assert cli.main(["retry", retry_id, "--now"]) == 0
    retried = support.read_task(ledger_path, retry_id)
    assert retried["backend"] == ledger.BACKEND_CODEX
    assert retried["worktree"] == worktree
    assert retried["agent_args"] == ["-m", "x"]
    assert cli.main(["_execute", retry_id, retried["session_id"]]) == 0
    exec_calls = [call for call in logged_codex_calls(fake_commands) if call[0] == "exec"]
    assert "resume" not in exec_calls[-1]


@pytest.mark.parametrize(
    "backends",
    [
        pytest.param(
            (ledger.BACKEND_CLAUDE, ledger.BACKEND_CODEX, ledger.BACKEND_CLAUDE), id="claude-parent"
        ),
        pytest.param(
            (ledger.BACKEND_CODEX, ledger.BACKEND_CLAUDE, ledger.BACKEND_CODEX), id="codex-parent"
        ),
    ],
)
def test_mixed_backend_chain_shares_worktree_and_uses_fresh_conversations(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, backends
):
    monkeypatch.chdir(git_repository)
    parent_backend, child_backend, grandchild_backend = backends
    assert cli.main(["run", support.PROMPT, "--backend", parent_backend, "--now"]) == 0
    assert (
        cli.main(["run", support.PROMPT, "--backend", child_backend, "--after", support.PARENT_ID])
        == 0
    )
    assert (
        cli.main(
            [
                "run",
                support.PROMPT,
                "--backend",
                grandchild_backend,
                "--after",
                support.CHILD_ID,
            ]
        )
        == 0
    )
    parent = support.read_task(ledger_path, support.PARENT_ID)

    assert cli.main(["_execute", support.PARENT_ID, parent["session_id"]]) == 0
    tasks = support.read_tasks(ledger_path)
    worktree = tasks[support.PARENT_ID]["worktree"]
    assert {task["worktree"] for task in tasks.values()} == {worktree}
    assert len({task["session_id"] for task in tasks.values()}) == 3
    assert [
        tasks[task_id]["status"]
        for task_id in (support.PARENT_ID, support.CHILD_ID, support.GRANDCHILD_ID)
    ] == [
        ledger.STATUS_FINISHED,
        ledger.STATUS_FINISHED,
        ledger.STATUS_FINISHED,
    ]
    codex_cwds = fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()
    codex_executions = [
        (call, cwd)
        for call, cwd in zip(logged_codex_calls(fake_commands), codex_cwds, strict=True)
        if call[0] == "exec"
    ]
    assert len(codex_executions) == backends.count(ledger.BACKEND_CODEX)
    assert all("resume" not in call and cwd == worktree for call, cwd in codex_executions)
    assert support.logged_claude_cwds(fake_commands)[-1] == worktree


def test_codex_home_reaches_launch_agent_executor(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert cli.main(["run", support.PROMPT, "--backend", "codex", "--now"]) == 0
    monkeypatch.delenv("CODEX_HOME")
    fake_commands["codex-args"].write_text("", encoding="utf-8")
    fake_commands["codex-home"].write_text("", encoding="utf-8")
    start_task_executor = spawn.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: ledger.Task) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    monkeypatch.setattr(spawn, "start_task_executor", record_executor)
    launch_agent.run_launch_agent_pass()

    assert len(processes) == 1
    processes[0].wait()
    homes = fake_commands["codex-home"].read_text(encoding="utf-8").splitlines()
    assert next(
        home
        for call, home in zip(logged_codex_calls(fake_commands), homes, strict=True)
        if call[0] == "exec"
    ) == str(codex_home)
