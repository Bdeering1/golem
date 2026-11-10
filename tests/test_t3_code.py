from pathlib import Path

import pytest

from golem import cli, ledger, t3_code
from tests import support, t3_support


@pytest.fixture
def claude_task(ledger_path):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)


@pytest.mark.parametrize("task_location", ["worktree", "repository", "subdirectory"])
def test_pickup_attaches_claude_session_to_thread_and_opens_it(
    server, ledger_path, git_repository, fake_commands, task_location
):
    server.project_root = str(git_repository)
    t3_support.write_claude_model_cache(server.home)
    worktree = support.git_worktree(git_repository)
    subdirectory = git_repository / "pkg"
    subdirectory.mkdir()
    fields = {"worktree": str(worktree)} if task_location == "worktree" else {}
    cwd = subdirectory if task_location == "subdirectory" else git_repository
    expected_directory = {"worktree": worktree, "subdirectory": subdirectory}.get(task_location)
    expected_branch = {
        "worktree": support.WORKTREE_BRANCH,
        "subdirectory": support.git_output(git_repository, "branch", "--show-current").strip(),
    }.get(task_location)
    support.write_task(
        ledger_path,
        ledger.STATUS_NEEDS_REVIEW,
        cwd=str(cwd),
        prompt=f"{support.PROMPT}\nmore detail",
        **fields,
    )

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert t3_support.created_thread(server) == {
        "type": "thread.create",
        "createdBy": "user",
        "creationSource": "web",
        "threadId": t3_support.CLAUDE_THREAD_ID,
        "projectId": t3_support.PROJECT_ID,
        "title": support.PROMPT,
        "modelSelection": {"instanceId": "claudeAgent", "model": t3_support.CACHED_CLAUDE_MODEL},
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": expected_branch,
        "worktreePath": str(expected_directory) if expected_directory else None,
        "importedNativeThread": {
            "ref": {
                "driver": "claudeAgent",
                "nativeId": f"{support.TASK_ID}-session",
                "strength": "strong",
            }
        },
    }
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{t3_support.thread_link(t3_support.CLAUDE_THREAD_ID)}\n"
    )
    task = support.read_task(ledger_path)
    assert task["status"] == ledger.STATUS_DONE
    assert task["t3_code_thread_id"] == t3_support.CLAUDE_THREAD_ID


def test_pickup_all_attaches_codex_thread_with_default_model(server, ledger_path, fake_commands):
    support.write_task(
        ledger_path,
        ledger.STATUS_NEEDS_REVIEW,
        backend=ledger.BACKEND_CODEX,
        thread_id=t3_support.CODEX_THREAD,
    )

    assert cli.main(["pickup", "--all", "--client", "t3-code"]) == 0

    thread = t3_support.created_thread(server)
    assert thread["threadId"] == t3_support.CODEX_THREAD_ID
    assert thread["modelSelection"] == {
        "instanceId": "codex",
        "model": t3_code.DEFAULT_MODELS["codex"],
    }
    assert thread["importedNativeThread"]["ref"]["driver"] == "codex"
    assert thread["importedNativeThread"]["ref"]["nativeId"] == t3_support.CODEX_THREAD
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{t3_support.thread_link(t3_support.CODEX_THREAD_ID)}\n"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


@pytest.mark.parametrize(
    ("backend_id", "fields", "thread_id"),
    [
        pytest.param(ledger.BACKEND_CLAUDE, {}, t3_support.CLAUDE_THREAD_ID, id="claude"),
        pytest.param(
            ledger.BACKEND_CODEX,
            {"thread_id": t3_support.CODEX_THREAD},
            t3_support.CODEX_THREAD_ID,
            id="codex",
        ),
    ],
)
def test_pickup_all_opens_task_in_t3_code_default_client(
    server, ledger_path, fake_commands, backend_id, fields, thread_id
):
    support.write_config_table("default_clients", {backend_id: "t3-code"})
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, backend=backend_id, **fields)

    assert cli.main(["pickup", "--all"]) == 0

    assert (
        fake_commands["open-args"].read_text(encoding="utf-8")
        == f"{t3_support.thread_link(thread_id)}\n"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_repeat_pickup_opens_existing_thread(server, claude_task, ledger_path, fake_commands):
    (server.home / "thread-present").touch()

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert server.commands == []
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"{t3_support.thread_link(t3_support.CLAUDE_THREAD_ID)}\n"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_pickup_adds_missing_project(server, claude_task):
    server.project_root = None

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert f"project add {Path.cwd()}" in server.t3_calls()
    assert t3_support.created_thread(server)["projectId"] == t3_support.ADDED_PROJECT_ID


def test_pickup_starts_t3_code_when_it_is_not_running(
    server, claude_task, ledger_path, fake_commands, monkeypatch
):
    server.runtime_path.unlink()
    monkeypatch.setattr(t3_code, "wait_for_server", server.mark_running)

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines() == [
        f"-b {t3_code.APP_BUNDLE_ID}",
        t3_support.thread_link(t3_support.CLAUDE_THREAD_ID),
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
    monkeypatch.setattr(
        t3_code, "fetch_projects", lambda _origin, _token: [{"id": t3_support.PROJECT_ID}]
    )


@pytest.mark.parametrize(
    ("break_t3_code", "message"),
    [
        pytest.param(remove_cli, "golem: T3 Code is not installed: ", id="not-installed"),
        pytest.param(stop_server, f"{t3_support.T3_VERSION} .*did not start", id="start-timeout"),
        pytest.param(
            reject_commands, f"{t3_support.T3_VERSION} .*project is archived", id="rejected"
        ),
        pytest.param(
            skip_thread_creation, f"{t3_support.T3_VERSION} .*missing", id="thread-missing"
        ),
        pytest.param(
            return_malformed_projects,
            f"{t3_support.T3_VERSION} could not open thread",
            id="malformed",
        ),
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


def write_task_with_thread(ledger_path, status=ledger.STATUS_DONE):
    support.write_task(
        ledger_path,
        status,
        pull_requests=t3_support.FINISHED_PULL_REQUESTS,
        t3_code_thread_id=t3_support.CLAUDE_THREAD_ID,
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["delete", support.TASK_ID], id="delete"),
        pytest.param(["prune", "--yes"], id="prune"),
        pytest.param(["run", support.TASK_ID, support.PROMPT, "--now"], id="replacement"),
    ],
)
def test_deleting_task_deletes_its_thread(server, installed_launch_agent, ledger_path, command):
    write_task_with_thread(ledger_path)

    assert cli.main(command) == 0

    assert t3_support.sent_commands(server) == [
        {"type": "thread.delete", "threadId": t3_support.CLAUDE_THREAD_ID}
    ]


@pytest.mark.parametrize(
    ("command", "status"),
    [
        pytest.param(["delete", support.TASK_ID], ledger.STATUS_DONE, id="delete"),
        pytest.param(
            ["run", support.TASK_ID, support.PROMPT, "--now"], ledger.STATUS_DONE, id="replacement"
        ),
        pytest.param(["retry", support.TASK_ID, "--now"], ledger.STATUS_FAILED, id="retry"),
    ],
)
def test_thread_deleting_command_refuses_while_t3_code_is_not_running(
    server, installed_launch_agent, ledger_path, command, status
):
    write_task_with_thread(ledger_path, status)
    server.runtime_path.unlink()
    tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit, match="T3 Code is not running"):
        cli.main(command)

    assert support.read_tasks(ledger_path) == tasks


def test_prune_keeps_task_with_thread_while_t3_code_is_not_running(server, ledger_path):
    write_task_with_thread(ledger_path)
    server.runtime_path.unlink()

    assert cli.main(["prune", "--yes"]) == 0

    assert support.TASK_ID in support.read_tasks(ledger_path)


def test_rejected_thread_delete_warns_without_failing(server, ledger_path, capsys, monkeypatch):
    support.write_task(
        ledger_path, ledger.STATUS_DONE, t3_code_thread_id=t3_support.CLAUDE_THREAD_ID
    )
    reject_commands(server, monkeypatch)

    assert cli.main(["delete", support.TASK_ID]) == 0

    assert (
        f"could not delete T3 Code thread {t3_support.CLAUDE_THREAD_ID}" in capsys.readouterr().err
    )
