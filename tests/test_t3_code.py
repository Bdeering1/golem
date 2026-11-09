import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from golem import cli, ledger, t3_code
from tests import support

T3_VERSION = "t3 v0.1.7"
ENVIRONMENT_ID = "environment"
PROJECT_ID = "project"
ADDED_PROJECT_ID = "added-project"
CODEX_THREAD = "codex-thread"
CACHED_CLAUDE_MODEL = "claude-cached"
SUCCESS_EXIT = {"_tag": "Success", "value": {"sequence": 3}}
FINISHED_PULL_REQUESTS = [{"url": "merged", "state": ledger.PULL_REQUEST_STATE_MERGED}]
CLAUDE_THREAD_ID = f"golem:claude:{support.TASK_ID}-session"
FAKE_T3 = """#!/bin/sh
printf '%s\\n' "$*" >> "$T3CODE_HOME/t3-args"
case "$1 $2" in
    "--version "*) echo 't3 v0.1.7' ;;
    "thread show") [ -e "$T3CODE_HOME/thread-present" ] ;;
    "auth session") echo token ;;
    "project add") printf '%s' "$3" > "$T3CODE_HOME/added-project" ;;
esac
"""


@dataclass
class FakeServer:
    home: Path
    project_root: str | None = None
    command_exit: dict = field(default_factory=lambda: SUCCESS_EXIT)
    is_creating_threads: bool = True
    commands: list = field(default_factory=list)

    def fetch_projects(self, _origin, _token):
        added_path = self.home / "added-project"
        projects = []
        if self.project_root is not None:
            projects.append({"id": PROJECT_ID, "workspaceRoot": self.project_root})
        if added_path.exists():
            projects.append(
                {"id": ADDED_PROJECT_ID, "workspaceRoot": added_path.read_text(encoding="utf-8")}
            )
        return projects

    def dispatch_command_exit(self, _origin, _token, command):
        self.commands.append(command)
        if self.is_creating_threads and self.command_exit == SUCCESS_EXIT:
            (self.home / "thread-present").touch()
        return self.command_exit

    def mark_running(self):
        self.runtime_path.write_text(
            json.dumps({"origin": "http://127.0.0.1:3773"}), encoding="utf-8"
        )

    @property
    def runtime_path(self):
        return self.home / "userdata" / "server-runtime.json"

    def t3_calls(self):
        log_path = self.home / "t3-args"
        return log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []


@pytest.fixture
def server(monkeypatch, tmp_path):
    home = tmp_path / "t3"
    (home / "bin").mkdir(parents=True)
    (home / "userdata").mkdir()
    (home / "caches").mkdir()
    cli_path = home / "bin" / "t3"
    cli_path.write_text(FAKE_T3, encoding="utf-8")
    cli_path.chmod(0o755)
    (home / "userdata" / "environment-id").write_text(f"{ENVIRONMENT_ID}\n", encoding="utf-8")
    monkeypatch.setenv("T3CODE_HOME", str(home))
    fake_server = FakeServer(home, project_root=str(Path.cwd()))
    fake_server.mark_running()
    monkeypatch.setattr(t3_code, "fetch_projects", fake_server.fetch_projects)
    monkeypatch.setattr(t3_code, "dispatch_command_exit", fake_server.dispatch_command_exit)
    return fake_server


@pytest.fixture
def claude_task(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)


def thread_link(thread_id):
    return f"t3code://threads/{ENVIRONMENT_ID}/{thread_id.replace(':', '%3A')}"


def write_claude_model_cache(home):
    (home / "caches" / "claudeAgent.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "claude-other", "isDefault": False},
                    {"slug": CACHED_CLAUDE_MODEL, "isDefault": True},
                ]
            }
        ),
        encoding="utf-8",
    )


def created_thread(server):
    return {name: value for name, value in server.commands[-1].items() if name != "commandId"}


@pytest.mark.parametrize("is_worktree_task", [True, False], ids=["worktree", "repository"])
def test_pickup_attaches_claude_session_to_thread_and_opens_it(
    server, ledger_path, git_repository, fake_commands, is_worktree_task
):
    server.project_root = str(git_repository)
    write_claude_model_cache(server.home)
    worktree = support.git_worktree(git_repository)
    fields = {"worktree": str(worktree)} if is_worktree_task else {}
    support.write_task(
        ledger_path,
        ledger.STATUS_NEEDS_REVIEW,
        cwd=str(git_repository),
        prompt=f"{support.PROMPT}\nmore detail",
        **fields,
    )

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert created_thread(server) == {
        "type": "thread.create",
        "createdBy": "user",
        "creationSource": "web",
        "threadId": CLAUDE_THREAD_ID,
        "projectId": PROJECT_ID,
        "title": support.PROMPT,
        "modelSelection": {"instanceId": "claudeAgent", "model": CACHED_CLAUDE_MODEL},
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": support.WORKTREE_BRANCH if is_worktree_task else None,
        "worktreePath": str(worktree) if is_worktree_task else None,
        "importedNativeThread": {
            "ref": {
                "driver": "claudeAgent",
                "nativeId": f"{support.TASK_ID}-session",
                "strength": "strong",
            }
        },
    }
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{thread_link(CLAUDE_THREAD_ID)}\n"
    )
    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_DONE
    assert task["t3_code_thread_id"] == CLAUDE_THREAD_ID


def test_pickup_all_attaches_codex_thread_with_default_model(server, ledger_path, fake_commands):
    support.write_task(
        ledger_path,
        ledger.STATUS_NEEDS_REVIEW,
        backend=ledger.BACKEND_CODEX,
        thread_id=CODEX_THREAD,
    )

    assert cli.main(["pickup", "--all", "--client", "t3-code"]) == 0

    thread = created_thread(server)
    assert thread["threadId"] == f"golem:codex:{CODEX_THREAD}"
    assert thread["modelSelection"] == {"instanceId": "codex", "model": "gpt-6-astra"}
    assert thread["importedNativeThread"]["ref"]["driver"] == "codex"
    assert thread["importedNativeThread"]["ref"]["nativeId"] == CODEX_THREAD
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{thread_link(f'golem:codex:{CODEX_THREAD}')}\n"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_repeat_pickup_opens_existing_thread(server, claude_task, ledger_path, fake_commands):
    (server.home / "thread-present").touch()

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert server.commands == []
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{thread_link(CLAUDE_THREAD_ID)}\n"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_pickup_adds_missing_project(server, claude_task):
    server.project_root = None

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert f"project add {Path.cwd()}" in server.t3_calls()
    assert created_thread(server)["projectId"] == ADDED_PROJECT_ID


def test_pickup_starts_t3_code_when_it_is_not_running(
    server, claude_task, ledger_path, fake_commands, monkeypatch
):
    server.runtime_path.unlink()
    monkeypatch.setattr(t3_code, "wait_for_server", server.mark_running)

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines() == [
        f"-b {t3_code.APP_BUNDLE_ID}",
        thread_link(CLAUDE_THREAD_ID),
    ]
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def remove_cli(server, _monkeypatch):
    (server.home / "bin" / "t3").unlink()


def stop_server(server, monkeypatch):
    server.runtime_path.unlink()
    monkeypatch.setattr(t3_code, "APP_START_TIMEOUT_SECONDS", 0)


def reject_commands(server, _monkeypatch):
    server.command_exit = {
        "_tag": "Failure",
        "cause": {"_tag": "Fail", "error": {"message": "project is archived"}},
    }


def skip_thread_creation(server, _monkeypatch):
    server.is_creating_threads = False


def return_malformed_projects(_server, monkeypatch):
    monkeypatch.setattr(t3_code, "fetch_projects", lambda _origin, _token: [{"id": PROJECT_ID}])


@pytest.mark.parametrize(
    ("break_t3_code", "message"),
    [
        pytest.param(remove_cli, "golem: T3 Code is not installed: ", id="not-installed"),
        pytest.param(stop_server, f"{T3_VERSION} .*did not start", id="start-timeout"),
        pytest.param(reject_commands, f"{T3_VERSION} .*project is archived", id="rejected"),
        pytest.param(skip_thread_creation, f"{T3_VERSION} .*missing", id="thread-missing"),
        pytest.param(return_malformed_projects, f"{T3_VERSION} .*workspaceRoot", id="malformed"),
    ],
)
def test_failed_pickup_leaves_task_needing_review(
    server, claude_task, ledger_path, fake_commands, monkeypatch, break_t3_code, message
):
    break_t3_code(server, monkeypatch)

    with pytest.raises(SystemExit, match=message):
        cli.main(["pickup", support.TASK_ID, "--client", "t3-code"])

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert "t3code://" not in fake_commands["open-args"].read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["delete", support.TASK_ID], id="delete"),
        pytest.param(["prune", "--yes"], id="prune"),
        pytest.param(["run", support.TASK_ID, support.PROMPT, "--now"], id="replacement"),
    ],
)
@pytest.mark.parametrize("is_running", [True, False], ids=["running", "not-running"])
def test_deleting_task_deletes_its_thread_only_while_t3_code_runs(
    server, installed_launch_agent, ledger_path, fake_commands, command, is_running
):
    support.write_task(
        ledger_path,
        ledger.STATUS_DONE,
        pull_requests=FINISHED_PULL_REQUESTS,
        t3_code_thread_id=CLAUDE_THREAD_ID,
    )
    if not is_running:
        server.runtime_path.unlink()

    assert cli.main(command) == 0

    expected_commands = [{"type": "thread.delete", "threadId": CLAUDE_THREAD_ID}]
    assert [
        {name: value for name, value in sent.items() if name != "commandId"}
        for sent in server.commands
    ] == (expected_commands if is_running else [])
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""
