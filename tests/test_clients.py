import pytest

from golem import cli, clients, ledger
from tests import support

STUB_TERMINAL_CLIENT = "stub-terminal"
STUB_APP_CLIENT = "stub-app"


def stub_client(name, *, is_terminal, program):
    return clients.Client(
        name=name,
        backend=ledger.BACKEND_CLAUDE,
        is_terminal=is_terminal,
        command=lambda conversation_id, agent_args: [
            program,
            f"stub:{conversation_id}",
            *agent_args,
        ],
    )


@pytest.fixture
def stub_clients(monkeypatch):
    monkeypatch.setitem(
        clients.CLIENTS,
        STUB_TERMINAL_CLIENT,
        stub_client(STUB_TERMINAL_CLIENT, is_terminal=True, program="claude"),
    )
    monkeypatch.setitem(
        clients.CLIENTS,
        STUB_APP_CLIENT,
        stub_client(STUB_APP_CLIENT, is_terminal=False, program="open"),
    )


def test_registered_terminal_client_opens_task_and_marks_it_done(
    fake_commands, ledger_path, stub_clients
):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    assert cli.main(["pickup", support.TASK_ID, "--client", STUB_TERMINAL_CLIENT, "--", "-x"]) == 0
    assert support.logged_claude_args(fake_commands) == [
        f"stub:{support.read_task(ledger_path)['session_id']}",
        "-x",
    ]
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_registered_app_client_opens_task_through_pickup_all(
    fake_commands, ledger_path, stub_clients
):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    assert cli.main(["pickup", "--all", "--client", STUB_APP_CLIENT]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"stub:{support.read_task(ledger_path)['session_id']}\n"
    )
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


@pytest.mark.parametrize(
    ("task_fields", "client_name"),
    [
        pytest.param({"backend": ledger.BACKEND_CODEX}, "claude-desktop", id="codex-in-app"),
        pytest.param({"backend": ledger.BACKEND_CODEX}, "claude-code", id="codex-in-claude-code"),
        pytest.param({}, "codex-cli", id="claude-in-codex-cli"),
    ],
)
def test_pickup_refuses_client_that_cannot_open_task_backend(
    fake_commands, ledger_path, task_fields, client_name
):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, **task_fields)

    with pytest.raises(SystemExit, match=f"{client_name} does not support"):
        cli.main(["pickup", support.TASK_ID, "--client", client_name])
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""
    assert support.logged_claude_calls(fake_commands) == []


@pytest.mark.parametrize(
    ("command", "message"),
    [
        pytest.param(
            ["pickup", "--all", "--client", "claude-code"], "requires an app client", id="terminal"
        ),
        pytest.param(["pickup", "--all"], "requires an app client", id="no-client"),
        pytest.param(["pickup", support.TASK_ID, "--app"], "use --client", id="removed-app-flag"),
    ],
)
def test_pickup_rejects_invalid_client_options(
    fake_commands, ledger_path, capsys, command, message
):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(command)

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
