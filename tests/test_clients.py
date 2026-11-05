import pytest

from golem import cli, clients, ledger, review
from tests import support

STUB_TERMINAL_CLIENT = "stub-terminal"
STUB_APP_CLIENT = "stub-app"


def stub_client(name, *, is_terminal, program):
    return clients.Client(
        name=name,
        backend_id=ledger.BACKEND_CLAUDE,
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
    ("task_fields", "client_name", "backend_name"),
    [
        pytest.param(
            {"backend": ledger.BACKEND_CODEX}, "claude-desktop", "Codex", id="codex-in-app"
        ),
        pytest.param(
            {"backend": ledger.BACKEND_CODEX}, "claude-code", "Codex", id="codex-in-claude-code"
        ),
        pytest.param({}, "codex-cli", "Claude", id="claude-in-codex-cli"),
    ],
)
def test_pickup_refuses_client_that_cannot_open_task_backend(
    fake_commands, ledger_path, task_fields, client_name, backend_name
):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, **task_fields)

    with pytest.raises(
        SystemExit, match=f"{client_name} does not support {backend_name} tasks: {support.TASK_ID}"
    ):
        cli.main(["pickup", support.TASK_ID, "--client", client_name])
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""
    assert support.logged_claude_calls(fake_commands) == []
    assert fake_commands["codex-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("command", "message"),
    [
        pytest.param(
            ["pickup", "--all", "--client", "claude-code"], "requires an app client", id="terminal"
        ),
        pytest.param(
            ["pickup", support.TASK_ID, "--app"], "unrecognized arguments: --app", id="removed-app"
        ),
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


def test_short_client_option_selects_client(fake_commands, ledger_path, stub_clients):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    assert cli.main(["pickup", support.TASK_ID, "-c", STUB_TERMINAL_CLIENT]) == 0
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_auto_pickup_opens_in_client_saved_when_queued(
    installed_launch_agent, fake_commands, ledger_path, stub_clients
):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--auto-pickup"]) == 0
    task = support.read_task(ledger_path)
    assert task["auto_pickup_client"] == STUB_APP_CLIENT

    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: "claude-desktop"})
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0

    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"stub:{task['session_id']}\n"
    )


def test_follow_up_after_auto_pickup_parent_uses_its_backends_default(
    installed_launch_agent, ledger_path, stub_clients
):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    cli.main(["run", support.PARENT_ID, support.PARENT_PROMPT, "--now", "--auto-pickup"])

    assert (
        cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID]) == 0
    )

    assert support.read_task(ledger_path, support.CHILD_ID)["auto_pickup_client"] == STUB_APP_CLIENT


def test_legacy_auto_pickup_task_opens_in_claude_desktop(fake_commands, ledger_path):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, auto_pickup=True)

    review.auto_pickup_task(support.TASK_ID)

    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={support.read_task(ledger_path)['session_id']}\n"
    )


def test_pickup_all_uses_backend_default_and_client_overrides_it(
    fake_commands, ledger_path, stub_clients
):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)
    session_id = support.read_task(ledger_path)["session_id"]

    assert cli.main(["pickup", "--all"]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8") == f"stub:{session_id}\n"

    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)
    assert cli.main(["pickup", "--all", "-c", "claude-desktop"]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines()[-1] == (
        f"claude://resume?session={session_id}"
    )


def test_pickup_all_rejects_agent_arguments(fake_commands, ledger_path, capsys):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    with pytest.raises(SystemExit):
        cli.main(["pickup", "--all", "--", "--model", "x"])

    assert "pickup --all does not accept agent arguments" in capsys.readouterr().err
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW


def test_pickup_all_fails_task_whose_backend_has_no_default(fake_commands, ledger_path, capsys):
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, backend=ledger.BACKEND_CODEX)

    assert cli.main(["pickup", "--all"]) == 1

    assert f"no default app client for Codex tasks: {support.TASK_ID}" in capsys.readouterr().err
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW


def test_pickup_by_id_ignores_default_clients(fake_commands, ledger_path, stub_clients):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW)

    assert cli.main(["pickup", support.TASK_ID]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


@pytest.mark.parametrize(
    ("backend_id", "client_name", "message"),
    [
        pytest.param("claude", "no-such-client", "names unknown client", id="unknown-client"),
        pytest.param("claude", STUB_TERMINAL_CLIENT, "must be an app client", id="terminal"),
        pytest.param("codex", STUB_APP_CLIENT, "cannot open codex tasks", id="wrong-backend"),
        pytest.param("no-such-backend", STUB_APP_CLIENT, "is not a backend", id="unknown-backend"),
    ],
)
def test_invalid_default_client_names_the_config_entry(
    installed_launch_agent, ledger_path, stub_clients, backend_id, client_name, message
):
    support.write_config_table("default_clients", {backend_id: client_name})

    with pytest.raises(SystemExit, match=rf"default_clients\.{backend_id}.*{message}"):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--auto-pickup"])

    assert not ledger_path.exists()
