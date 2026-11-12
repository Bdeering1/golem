import json
from datetime import datetime
from pathlib import Path

import pytest

from golem import cli, ledger, progress_log, spawn, t3_code
from golem.backends import claude, common, t3
from tests import support, t3_support

T3_REPLY = "fixed the thing"
NATIVE_SESSION_ID = "native-session"
COMMAND_ITEM = {
    "id": "command",
    "runId": "run",
    "status": "completed",
    "type": "command_execution",
    "input": "make test",
}
MESSAGE_ITEM = {
    "id": "message",
    "runId": "run",
    "status": "completed",
    "type": "assistant_message",
    "text": T3_REPLY,
}
USAGE_LIMIT_RESET_AT = "2030-01-01T10:00:00.000Z"
USAGE_LIMIT_ITEM = {
    "id": "usage-limit",
    "runId": "run",
    "status": "failed",
    "type": "error",
    "failure": {
        "class": "usage_limit",
        "message": "usage limit reached",
        "resetAt": USAGE_LIMIT_RESET_AT,
    },
}
CLAUDE_TRANSCRIPT = f".claude/projects/project/{NATIVE_SESSION_ID}.jsonl"
CODEX_TRANSCRIPT = (
    f".codex/sessions/2030/01/01/rollout-2030-01-01T10-00-00-{NATIVE_SESSION_ID}.jsonl"
)


def projection(*items: object):
    return {
        "thread": {"activeProviderThreadId": "provider-thread"},
        "providerThreads": [
            {
                "id": "provider-thread",
                "nativeThreadRef": {
                    "driver": "x",
                    "nativeId": NATIVE_SESSION_ID,
                    "strength": "strong",
                },
            }
        ],
        "turnItems": list(items),
    }


def write_send_result(server, exit_code, *, reply=None, error=None):
    output = {"threadId": "thread", "runId": "run", "status": "", "reply": reply, "error": error}
    (server.home / "send-output").write_text(json.dumps(output), encoding="utf-8")
    (server.home / "send-exit").write_text(str(exit_code), encoding="utf-8")


def execute(ledger_path, task_id=support.TASK_ID):
    task = support.read_task(ledger_path, task_id)
    cli.main(["_execute", task_id, task["session_id"]])
    return support.read_task(ledger_path, task_id)


def run_t3_task(ledger_path, *agent_args: str):
    command = ["run", support.TASK_ID, support.PROMPT, "--backend", "t3", "--now"]
    assert cli.main([*command, "--", *agent_args]) == 0
    return execute(ledger_path)


@pytest.fixture
def t3_server(server, installed_launch_agent):
    server.projections.append(projection(COMMAND_ITEM, MESSAGE_ITEM))
    write_send_result(server, 0, reply=T3_REPLY)
    return server


@pytest.mark.parametrize(
    ("agent_args", "instance_id", "model"),
    [
        pytest.param([], "claudeAgent", t3_support.CACHED_CLAUDE_MODEL, id="claude-default"),
        pytest.param(
            ["--provider", "codex"], "codex", t3_code.DEFAULT_MODELS["codex"], id="codex-default"
        ),
        pytest.param(["--provider=codex", "--model=gpt-x"], "codex", "gpt-x", id="codex-model"),
    ],
)
def test_run_creates_thread_in_worktree_and_records_reply(
    t3_server, ledger_path, git_repository, monkeypatch, agent_args, instance_id, model
):
    monkeypatch.chdir(git_repository)
    t3_server.project_root = str(git_repository)
    t3_support.write_claude_model_cache(t3_server.home)

    task = run_t3_task(ledger_path, *agent_args)

    worktree = task["worktree"]
    assert t3_support.created_thread(t3_server) == {
        "type": "thread.create",
        "createdBy": "user",
        "creationSource": "web",
        "threadId": t3.conversation_id(task),
        "projectId": t3_support.PROJECT_ID,
        "title": support.PROMPT,
        "modelSelection": {"instanceId": instance_id, "model": model},
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": f"worktree-{Path(worktree).name}",
        "worktreePath": worktree,
    }
    assert f"thread send {t3.conversation_id(task)} - --wait --json" in t3_server.t3_calls()
    assert (t3_server.home / "sent-message").read_text(encoding="utf-8") == support.PROMPT
    send_cwd = (t3_server.home / "send-cwd").read_text(encoding="utf-8").strip()
    assert Path(send_cwd).resolve() == Path(worktree).resolve()
    assert task["status"] == ledger.STATUS_NEEDS_REVIEW
    assert task["result"] == T3_REPLY
    assert task["t3_code_thread_id"] == t3.conversation_id(task)
    assert task["native_session_id"] == NATIVE_SESSION_ID
    progress = progress_log.progress_path(task["session_id"]).read_text(encoding="utf-8")
    assert common.tool_call_progress("command", "make test") in progress
    assert T3_REPLY in progress


def test_failed_run_fails_task_with_t3_code_error(t3_server, ledger_path):
    write_send_result(t3_server, 1, error="Claude Agent CLI was not found on PATH")

    task = run_t3_task(ledger_path)

    assert task["status"] == ledger.STATUS_FAILED
    assert task["result"] == "Claude Agent CLI was not found on PATH"


def test_run_waiting_for_answer_stops_task_until_continue(
    t3_server, ledger_path, fake_commands, monkeypatch
):
    executors = []
    monkeypatch.setattr(
        spawn, "start_task_executor", lambda task_id, _task, **_: executors.append(task_id)
    )
    write_send_result(t3_server, t3.EXIT_WAITING_FOR_ANSWER, reply="Which branch?")
    cli.main(
        [
            *["run", support.PARENT_ID, support.PARENT_PROMPT, "--backend", "t3", "--now"],
            *["--then", support.CHILD_ID, support.CHILD_PROMPT],
        ]
    )

    parent = execute(ledger_path, support.PARENT_ID)

    assert parent["status"] == ledger.STATUS_NEEDS_REVIEW
    assert parent["gate_stop_reason"] == t3.WAITING_FOR_ANSWER_REASON
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING
    assert support.logged_osascript_calls(fake_commands)[-1][-2:] == [
        f"golem: {support.PARENT_ID} stopped at gate",
        t3.WAITING_FOR_ANSWER_REASON,
    ]
    assert cli.main(["continue", support.PARENT_ID]) == 0
    assert executors == [support.PARENT_ID]


def test_usage_limit_resumes_same_thread_at_reported_reset(t3_server, ledger_path, monkeypatch):
    monkeypatch.setattr(
        claude, "next_reset_time", lambda _environment: pytest.fail("usage lookup ran")
    )
    t3_server.projections[1:] = [
        projection(COMMAND_ITEM, USAGE_LIMIT_ITEM),
        projection(COMMAND_ITEM, USAGE_LIMIT_ITEM, MESSAGE_ITEM),
    ]
    write_send_result(t3_server, 1, error="usage limit reached")

    task = run_t3_task(ledger_path)

    assert task["status"] == ledger.STATUS_SCHEDULED
    assert datetime.fromisoformat(task["due_at"]) == support.RESET_TIME
    assert t3_support.sent_commands(t3_server)[-1] == {
        "type": "thread.metadata.update",
        "threadId": t3.conversation_id(task),
        "limitRecovery": {
            "runId": USAGE_LIMIT_ITEM["runId"],
            "resetAt": USAGE_LIMIT_RESET_AT,
            "autoResume": False,
        },
    }
    write_send_result(t3_server, 0, reply=T3_REPLY)
    support.set_task_due_now(ledger_path)
    task = execute(ledger_path)
    assert task["status"] == ledger.STATUS_NEEDS_REVIEW
    assert (t3_server.home / "sent-message").read_text(
        encoding="utf-8"
    ) == common.USAGE_LIMIT_RESUME_PROMPT
    assert [command["type"] for command in t3_server.commands] == [
        "thread.create",
        "thread.metadata.update",
    ]
    progress = progress_log.progress_path(task["session_id"]).read_text(encoding="utf-8")
    assert progress.count(common.tool_call_progress("command", "make test")) == 1


@pytest.mark.parametrize(
    ("backend_id", "agent_args", "transcript_name"),
    [
        pytest.param("t3", [], CLAUDE_TRANSCRIPT, id="t3-claude"),
        pytest.param("t3", ["--provider", "codex"], CODEX_TRANSCRIPT, id="t3-codex"),
        pytest.param("codex", [], None, id="codex"),
    ],
)
def test_gate_stops_chain_and_hooks_only_claude_tasks(
    t3_server, ledger_path, tmp_path, backend_id, agent_args, transcript_name
):
    for name in (CLAUDE_TRANSCRIPT, CODEX_TRANSCRIPT):
        support.write_idle_file(Path.home() / name, 0)
    support.schedule_gated_chain(
        ledger_path,
        support.gate_command(tmp_path, exit_code=1, stderr=support.GATE_STOP_REASON),
        "--backend",
        backend_id,
        then_arguments=("--", *agent_args),
    )

    parent = execute(ledger_path, support.PARENT_ID)

    assert parent["gate_stop_reason"] == support.GATE_STOP_REASON
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_WAITING
    gate_input = json.loads((tmp_path / "gate-stdin").read_text(encoding="utf-8"))
    assert gate_input["transcript_path"] == (
        str(Path.home() / transcript_name) if transcript_name else None
    )
    is_claude = transcript_name == CLAUDE_TRANSCRIPT
    assert (Path.cwd() / ".claude" / "settings.local.json").exists() == is_claude


def test_pickup_opens_t3_task_in_t3_code_without_client(server, ledger_path, fake_commands):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, backend=ledger.BACKEND_T3)
    (server.home / "thread-present").touch()

    assert cli.main(["pickup", support.TASK_ID]) == 0

    task = support.read_task(ledger_path)
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{t3_support.thread_link(t3.conversation_id(task))}\n"
    )
    assert task["status"] == ledger.STATUS_DONE


def test_pickup_refuses_t3_task_without_thread(server, ledger_path, fake_commands):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, backend=ledger.BACKEND_T3)

    with pytest.raises(SystemExit, match="thread does not exist in T3 Code"):
        cli.main(["pickup", support.TASK_ID])


def test_run_does_not_store_configured_environment(installed_launch_agent, ledger_path):
    support.write_config_table("env", {"NAME": "value"})

    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--backend", "t3", "--now"]) == 0

    assert "NAME" not in support.read_task(ledger_path)["environment"]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["delete", support.TASK_ID], id="delete"),
        pytest.param(["retry", support.TASK_ID, "--now"], id="retry"),
    ],
)
def test_delete_and_retry_delete_previous_thread(
    server, installed_launch_agent, ledger_path, command
):
    previous_thread_id = f"golem:t3:{support.TASK_ID}-session"
    support.write_task(
        ledger_path,
        ledger.STATUS_FAILED,
        backend=ledger.BACKEND_T3,
        t3_code_thread_id=previous_thread_id,
        native_session_id=NATIVE_SESSION_ID,
    )

    assert cli.main(command) == 0

    assert t3_support.sent_commands(server) == [
        {"type": "thread.delete", "threadId": previous_thread_id}
    ]
    retried_task = support.read_tasks(ledger_path).get(support.TASK_ID, {})
    assert "t3_code_thread_id" not in retried_task
    assert "native_session_id" not in retried_task


def test_run_starts_t3_code_when_it_is_not_running(
    t3_server, ledger_path, fake_commands, monkeypatch
):
    t3_server.runtime_path.unlink()
    monkeypatch.setattr(t3_code, "wait_for_server", t3_server.mark_running)

    task = run_t3_task(ledger_path)

    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines() == [
        f"-b {t3_code.APP_BUNDLE_ID}"
    ]
    assert task["status"] == ledger.STATUS_NEEDS_REVIEW
