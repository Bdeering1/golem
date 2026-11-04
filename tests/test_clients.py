import pytest

from golem import cli, ledger
from tests import support


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
