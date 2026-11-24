import pytest

from golem import cli, ledger
from golem.clients import client_registry
from tests import support

STUB_TERMINAL_CLIENT = "stub-terminal"
STUB_APP_CLIENT = "stub-app"


def stub_client(name, *, is_terminal, program):
    return client_registry.Client(
        name=name,
        backend_ids=frozenset({ledger.BACKEND_CLAUDE}),
        is_terminal=is_terminal,
        command=lambda _task, conversation_id, agent_args: [
            program,
            f"stub:{conversation_id}",
            *agent_args,
        ],
    )


@pytest.fixture
def stub_clients(monkeypatch):
    monkeypatch.setitem(
        client_registry.CLIENTS,
        STUB_TERMINAL_CLIENT,
        stub_client(STUB_TERMINAL_CLIENT, is_terminal=True, program="claude"),
    )
    monkeypatch.setitem(
        client_registry.CLIENTS,
        STUB_APP_CLIENT,
        stub_client(STUB_APP_CLIENT, is_terminal=False, program="open"),
    )


def test_registered_terminal_client_opens_task_and_marks_it_done(
    fake_commands, ledger_path, stub_clients
):
    support.write_task(ledger_path, ledger.STATUS_FINISHED)

    assert cli.main(["pickup", support.TASK_ID, "--client", STUB_TERMINAL_CLIENT, "--", "-x"]) == 0
    assert support.logged_claude_args(fake_commands) == [
        f"stub:{support.read_task(ledger_path)['session_id']}",
        "-x",
    ]
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


@pytest.mark.parametrize(
    ("task_fields", "client_name", "backend_name"),
    [
        pytest.param(
            {"backend": ledger.BACKEND_CODEX}, "claude-desktop", "Codex", id="codex-in-app"
        ),
        pytest.param({}, "codex-cli", "Claude", id="claude-in-codex-cli"),
    ],
)
def test_pickup_refuses_client_that_cannot_open_task_backend(
    fake_commands, ledger_path, task_fields, client_name, backend_name
):
    support.write_task(ledger_path, ledger.STATUS_FINISHED, **task_fields)

    with pytest.raises(
        SystemExit, match=f"{client_name} does not support {backend_name} tasks: {support.TASK_ID}"
    ):
        cli.main(["pickup", support.TASK_ID, "--client", client_name])
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED
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
            ["pickup", "--all", "--", "--model", "x"],
            "pickup --all does not accept agent arguments",
            id="all-agent-arguments",
        ),
    ],
)
def test_pickup_rejects_invalid_client_options(
    fake_commands, ledger_path, capsys, command, message
):
    support.write_task(ledger_path, ledger.STATUS_FINISHED)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(command)

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


def test_auto_pickup_opens_in_client_saved_when_queued(
    installed_launch_agent, fake_commands, ledger_path, stub_clients
):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    assert cli.main(["run", support.PROMPT, "--now", "--auto-pickup"]) == 0
    task = support.read_task(ledger_path)

    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: "claude-desktop"})
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0

    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"stub:{task['session_id']}\n"
    )


def test_follow_up_after_auto_pickup_parent_uses_its_backends_default(
    installed_launch_agent, ledger_path, stub_clients
):
    assert cli.main(["run", support.PARENT_PROMPT, "--now", "--auto-pickup"]) == 0
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})

    for backend_id in [ledger.BACKEND_CLAUDE, ledger.BACKEND_CODEX]:
        command = ["run", support.PROMPT, "--backend", backend_id]
        assert cli.main([*command, "--after", support.PARENT_ID]) == 0

    tasks = support.read_tasks(ledger_path)
    assert tasks[support.CHILD_ID]["auto_pickup_client"] == STUB_APP_CLIENT
    assert tasks[support.SIBLING_ID]["auto_pickup_client"] == client_registry.CODEX_APP_CLIENT


def test_then_follow_up_of_auto_pickup_run_uses_saved_default(
    installed_launch_agent, ledger_path, stub_clients
):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})

    assert (
        cli.main(
            [
                "run",
                support.PARENT_PROMPT,
                "--now",
                "--auto-pickup",
                "--then",
                support.CHILD_PROMPT,
            ]
        )
        == 0
    )

    assert support.read_task(ledger_path, support.CHILD_ID)["auto_pickup_client"] == STUB_APP_CLIENT


def test_pickup_all_client_skips_default_clients(fake_commands, ledger_path):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: "no-such-client"})
    support.write_task(ledger_path, ledger.STATUS_FINISHED)

    assert cli.main(["pickup", "--all", "-c", "claude-desktop"]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={support.read_task(ledger_path)['session_id']}\n"
    )


def test_pickup_all_opens_backend_defaults_and_fails_backend_without_one(
    fake_commands, ledger_path, stub_clients, capsys, monkeypatch
):
    monkeypatch.delitem(client_registry.BUILT_IN_DEFAULT_CLIENTS, ledger.BACKEND_CODEX)
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    status = ledger.STATUS_FINISHED
    codex_task = support.task_record(status, backend=ledger.BACKEND_CODEX)
    claude_task = support.task_record(status, task_id=support.CHILD_ID)
    support.write_tasks(ledger_path, {support.TASK_ID: codex_task, support.CHILD_ID: claude_task})

    assert cli.main(["pickup", "--all"]) == 1

    assert f"no default app client for Codex tasks: {support.TASK_ID}" in capsys.readouterr().err
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"stub:{claude_task['session_id']}\n"
    )


def test_pickup_by_id_ignores_default_clients(fake_commands, ledger_path, stub_clients):
    support.write_config_table("default_clients", {ledger.BACKEND_CLAUDE: STUB_APP_CLIENT})
    support.write_task(ledger_path, ledger.STATUS_FINISHED)

    assert cli.main(["pickup", support.TASK_ID]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED


@pytest.mark.parametrize(
    ("backend_id", "client_name"),
    [
        pytest.param("claude", "no-such-client", id="unknown-client"),
        pytest.param("claude", STUB_TERMINAL_CLIENT, id="terminal"),
        pytest.param("codex", STUB_APP_CLIENT, id="wrong-backend"),
        pytest.param("no-such-backend", STUB_APP_CLIENT, id="unknown-backend"),
    ],
)
def test_invalid_default_client_names_the_config_entry(
    installed_launch_agent, ledger_path, stub_clients, backend_id, client_name
):
    support.write_config_table("default_clients", {backend_id: client_name})

    with pytest.raises(SystemExit, match=rf"default_clients\.{backend_id} "):
        cli.main(["run", support.PROMPT, "--now", "--auto-pickup"])

    assert not ledger_path.exists()
