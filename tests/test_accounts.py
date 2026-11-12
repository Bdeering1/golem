import json
from pathlib import Path

import pytest

from golem import accounts, cli, executor, ledger, t3_code
from golem.backends import claude
from tests import support, t3_support

NORMAL_EMAIL = "me@example.com"
WORK_EMAIL = "work@example.com"
SOLO_EMAIL = "solo@example.com"
CREDENTIALS = ".credentials.json"
T3_INSTANCE_ID = "golem-claude-1"


def status_text(email: str, org_id: str, org_name: str) -> str:
    return json.dumps({"loggedIn": True, "email": email, "orgId": org_id, "orgName": org_name})


@pytest.fixture(autouse=True)
def claude_config_directory():
    directory = Path.home() / ".claude"
    directory.mkdir(parents=True)
    (directory / CREDENTIALS).write_text(
        status_text(NORMAL_EMAIL, "org-personal", "Personal"), encoding="utf-8"
    )
    (directory / "settings.json").write_text("{}", encoding="utf-8")
    return directory


@pytest.fixture
def add_account(monkeypatch, tmp_path):
    def add(email: str = WORK_EMAIL, org_id: str = "org-work", org_name: str = "Work") -> int:
        login_path = tmp_path / "login.json"
        login_path.write_text(status_text(email, org_id, org_name), encoding="utf-8")
        monkeypatch.setenv("GOLEM_CLAUDE_LOGIN", str(login_path))
        return cli.main(["account", "add", "claude"])

    return add


def auth_calls(fake_commands):
    return fake_commands["claude-auth"].read_text(encoding="utf-8").splitlines()


def last_agent_environment(fake_commands):
    return fake_commands["claude-account"].read_text(encoding="utf-8").splitlines()[-1]


def t3_upsert_mutation():
    return {
        "operation": "upsert",
        "instanceId": T3_INSTANCE_ID,
        "instance": {
            "driver": "claudeAgent",
            "displayName": f"{WORK_EMAIL} (Work)",
            "config": {"homePath": str(accounts.account_directory(1))},
        },
    }


def run_and_execute(ledger_path, *arguments: str):
    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now", *arguments]) == 0
    task = support.read_task(ledger_path)
    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0


def test_add_records_account_and_links_shared_entries(add_account, claude_config_directory):
    assert add_account() == 0

    assert accounts.resolved_account_number(WORK_EMAIL) == 1
    shadow_directory = accounts.account_directory(1)
    assert (shadow_directory / "settings.json").resolve() == (
        claude_config_directory / "settings.json"
    ).resolve()
    assert not (shadow_directory / CREDENTIALS).is_symlink()


@pytest.mark.parametrize(
    ("existing_accounts", "login_exit", "new_account", "is_duplicate"),
    [
        pytest.param(0, "1", (WORK_EMAIL, "org-work", "Work"), False, id="failed-login"),
        pytest.param(
            0, "0", (NORMAL_EMAIL, "org-personal", "Personal"), True, id="normal-login-duplicate"
        ),
        pytest.param(1, "0", (WORK_EMAIL, "org-work", "Work"), True, id="account-duplicate"),
    ],
)
def test_refused_add_removes_directory(
    add_account,
    fake_commands,
    monkeypatch,
    existing_accounts,
    login_exit,
    new_account,
    is_duplicate,
):
    for _ in range(existing_accounts):
        add_account()
    monkeypatch.setenv("GOLEM_CLAUDE_LOGIN_EXIT", login_exit)

    with pytest.raises(SystemExit):
        add_account(*new_account)

    assert not accounts.account_directory(existing_accounts + 1).exists()
    with pytest.raises(SystemExit):
        accounts.resolved_account_number(str(existing_accounts + 1))
    assert ("logout" in auth_calls(fake_commands)) == is_duplicate


def test_list_shows_normal_login_and_accounts(add_account, capsys):
    add_account()
    add_account(WORK_EMAIL, "org-other", "Other")
    capsys.readouterr()

    assert cli.main(["account", "list"]) == 0

    assert [line.split() for line in capsys.readouterr().out.splitlines()] == [
        [NORMAL_EMAIL, "Personal", "0"],
        [WORK_EMAIL, "Work", "1"],
        [WORK_EMAIL, "Other", "2"],
    ]


def test_remove_logs_out_and_deletes_account(add_account, fake_commands):
    add_account()

    assert cli.main(["account", "remove", WORK_EMAIL]) == 0

    assert not accounts.account_directory(1).exists()
    with pytest.raises(SystemExit):
        accounts.resolved_account_number("1")
    assert "logout" in auth_calls(fake_commands)


def test_remove_refuses_while_unfinished_task_uses_account(add_account, ledger_path):
    add_account()
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, account=1)

    with pytest.raises(SystemExit, match=support.TASK_ID):
        cli.main(["account", "remove", WORK_EMAIL])

    assert accounts.account_directory(1).is_dir()


@pytest.mark.parametrize(
    ("value", "expected_account"),
    [
        pytest.param(SOLO_EMAIL.upper(), 3, id="full-email"),
        pytest.param("solo", 3, id="prefix"),
        pytest.param("2", 2, id="number"),
        pytest.param(NORMAL_EMAIL, None, id="normal-login"),
    ],
)
def test_run_account_selects_account(
    installed_launch_agent, add_account, ledger_path, value, expected_account
):
    add_account()
    add_account(WORK_EMAIL, "org-other", "Other")
    add_account(SOLO_EMAIL, "org-solo", "Solo")

    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now", "--account", value]) == 0

    assert support.read_task(ledger_path).get("account") == expected_account


@pytest.mark.parametrize(
    ("value", "expected_numbers"),
    [
        pytest.param(WORK_EMAIL, ["1", "2"], id="ambiguous"),
        pytest.param("nobody", [], id="unknown"),
    ],
)
def test_run_account_refuses_before_ledger_changes(
    installed_launch_agent, add_account, ledger_path, value, expected_numbers
):
    add_account()
    add_account(WORK_EMAIL, "org-other", "Other")
    support.write_task(ledger_path, ledger.STATUS_DONE)
    tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit) as error:
        cli.main(["run", support.CHILD_ID, support.PROMPT, "--now", "--account", value])

    assert [line.split()[-1] for line in str(error.value).splitlines()[1:]] == expected_numbers
    assert support.read_tasks(ledger_path) == tasks


def test_start_links_new_entries_and_keeps_private_entries(
    installed_launch_agent, add_account, claude_config_directory, ledger_path
):
    add_account()
    (claude_config_directory / "skills").mkdir()
    (claude_config_directory / ".claude.json").write_text("{}", encoding="utf-8")

    run_and_execute(ledger_path, "--account", WORK_EMAIL)

    shadow_directory = accounts.account_directory(1)
    assert (shadow_directory / "skills").is_symlink()
    assert not (shadow_directory / ".claude.json").exists()
    assert WORK_EMAIL in (shadow_directory / CREDENTIALS).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("account_arguments", "expected_environment"),
    [
        pytest.param(["--account", WORK_EMAIL], "{shadow}||", id="account"),
        pytest.param([], "|token|token", id="normal-login-drops-leaked-config"),
    ],
)
def test_agent_environment_uses_task_account(
    installed_launch_agent,
    add_account,
    fake_commands,
    ledger_path,
    monkeypatch,
    account_arguments,
    expected_environment,
):
    add_account()
    shadow_directory = str(accounts.account_directory(1))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", shadow_directory)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")

    run_and_execute(ledger_path, *account_arguments)

    assert last_agent_environment(fake_commands) == expected_environment.format(
        shadow=shadow_directory
    )


def test_gate_runs_on_task_account(installed_launch_agent, add_account, ledger_path, tmp_path):
    add_account()
    output_path = tmp_path / "gate-config"

    support.run_gated_chain(
        ledger_path, f'printf %s "$CLAUDE_CONFIG_DIR" > {output_path}', "--account", WORK_EMAIL
    )

    assert output_path.read_text(encoding="utf-8") == str(accounts.account_directory(1))


def schedule_account_run(ledger_path) -> None:
    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--account", WORK_EMAIL]) == 0


def pause_usage_limited_account_task(ledger_path) -> None:
    support.write_task(ledger_path, ledger.STATUS_RUNNING, account=1)
    executor.execute_task(support.TASK_ID, support.read_task(ledger_path), ())


def retry_account_task(ledger_path) -> None:
    support.write_task(ledger_path, ledger.STATUS_FAILED, account=1)
    assert cli.main(["retry", support.TASK_ID]) == 0


def schedule_t3_account_run(ledger_path) -> None:
    command = ["run", support.TASK_ID, support.PROMPT, "--backend", "t3", "--account", WORK_EMAIL]
    assert cli.main(command) == 0


def retry_t3_account_task(ledger_path) -> None:
    support.write_task(ledger_path, ledger.STATUS_FAILED, backend=ledger.BACKEND_T3, account=1)
    assert cli.main(["retry", support.TASK_ID]) == 0


@pytest.mark.parametrize(
    "look_up_reset_time",
    [
        pytest.param(schedule_account_run, id="run"),
        pytest.param(pause_usage_limited_account_task, id="usage-limit-pause"),
        pytest.param(retry_account_task, id="retry"),
        pytest.param(schedule_t3_account_run, id="t3-run"),
        pytest.param(retry_t3_account_task, id="t3-retry"),
    ],
)
def test_usage_lookup_runs_on_task_account(
    installed_launch_agent, add_account, fake_commands, ledger_path, monkeypatch, look_up_reset_time
):
    add_account()
    environments: list[dict[str, str]] = []

    def record_environment(environment: dict[str, str]) -> None:
        environments.append(environment)

    monkeypatch.setattr(claude, "next_reset_time", record_environment)
    support.write_claude_output(
        fake_commands, support.USAGE_LIMIT_RESULT, api_error_status=claude.USAGE_LIMIT_HTTP_STATUS
    )

    look_up_reset_time(ledger_path)

    assert environments[0]["CLAUDE_CONFIG_DIR"] == str(accounts.account_directory(1))


def test_then_follow_up_uses_run_account(installed_launch_agent, add_account, ledger_path):
    add_account()

    support.schedule_gated_chain(ledger_path, "true", "--account", WORK_EMAIL)

    assert support.read_task(ledger_path, support.CHILD_ID)["account"] == 1


def test_after_does_not_inherit_parent_account(installed_launch_agent, add_account, ledger_path):
    add_account()
    support.write_task(ledger_path, ledger.STATUS_RUNNING, task_id=support.PARENT_ID, account=1)

    assert (
        cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID]) == 0
    )

    assert "account" not in support.read_task(ledger_path, support.CHILD_ID)


def test_remove_keeps_account_when_logout_fails(add_account, monkeypatch):
    add_account()
    monkeypatch.setenv("GOLEM_CLAUDE_LOGOUT_EXIT", "1")

    with pytest.raises(SystemExit):
        cli.main(["account", "remove", WORK_EMAIL])

    assert accounts.account_directory(1).is_dir()
    assert accounts.resolved_account_number("1") == 1


def test_claude_code_pickup_resumes_on_task_account(
    add_account, fake_commands, ledger_path, monkeypatch
):
    add_account()
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, account=1)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")

    assert cli.main(["pickup", support.TASK_ID, "--client", "claude-code"]) == 0

    assert last_agent_environment(fake_commands) == f"{accounts.account_directory(1)}||"


def test_retry_keeps_task_account(installed_launch_agent, add_account, ledger_path):
    add_account()
    support.write_task(ledger_path, ledger.STATUS_FAILED, account=1)

    assert cli.main(["retry", support.TASK_ID, "--now"]) == 0

    assert support.read_task(ledger_path)["account"] == 1


def test_retry_refuses_removed_account_before_ledger_changes(installed_launch_agent, ledger_path):
    support.write_task(ledger_path, ledger.STATUS_FAILED, account=1)
    tasks = support.read_tasks(ledger_path)

    with pytest.raises(SystemExit, match="account 1"):
        cli.main(["retry", support.TASK_ID, "--now"])

    assert support.read_tasks(ledger_path) == tasks


def test_t3_code_pickup_creates_thread_on_account_instance(server, add_account, ledger_path):
    add_account()
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, account=1)

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert server.settings_mutations == [t3_upsert_mutation()]
    thread = t3_support.created_thread(server)
    assert thread["modelSelection"]["instanceId"] == T3_INSTANCE_ID
    assert thread["importedNativeThread"]["ref"]["driver"] == "claudeAgent"


def test_repeat_t3_code_pickup_updates_account_instance(server, add_account, ledger_path):
    add_account()
    support.write_task(ledger_path, ledger.STATUS_NEEDS_REVIEW, account=1)
    (server.home / "thread-present").touch()

    assert cli.main(["pickup", support.TASK_ID, "--client", "t3-code"]) == 0

    assert server.settings_mutations == [t3_upsert_mutation()]
    assert server.commands == []


def test_t3_run_creates_thread_on_account_instance(
    server, installed_launch_agent, add_account, ledger_path
):
    add_account()

    run_and_execute(ledger_path, "--backend", "t3", "--account", WORK_EMAIL)

    assert server.settings_mutations == [t3_upsert_mutation()]
    assert t3_support.created_thread(server)["modelSelection"]["instanceId"] == T3_INSTANCE_ID


@pytest.mark.parametrize("is_t3_running", [True, False], ids=["running", "not-running"])
def test_remove_removes_t3_instance_before_logout(
    server, add_account, fake_commands, monkeypatch, is_t3_running
):
    add_account()
    if not is_t3_running:
        server.runtime_path.unlink()
        monkeypatch.setattr(t3_code, "wait_for_server", server.mark_running)

    assert cli.main(["account", "remove", WORK_EMAIL]) == 0

    assert server.settings_mutations == [{"operation": "remove", "instanceId": T3_INSTANCE_ID}]
    assert "logout" in auth_calls(fake_commands)
    assert not accounts.account_directory(1).exists()


def stop_t3(server, monkeypatch):
    server.runtime_path.unlink()
    monkeypatch.setattr(t3_code, "APP_START_TIMEOUT_SECONDS", 0)


def reject_t3_settings(server, _monkeypatch):
    server.settings_exit = {"_tag": "Failure", "cause": {"error": {"message": "rejected"}}}


@pytest.mark.parametrize("break_t3", [stop_t3, reject_t3_settings], ids=["no-start", "rejected"])
def test_remove_keeps_account_when_t3_cannot_remove_instance(
    server, add_account, fake_commands, monkeypatch, break_t3
):
    add_account()
    break_t3(server, monkeypatch)

    with pytest.raises(SystemExit, match="T3 Code could not remove the account"):
        cli.main(["account", "remove", WORK_EMAIL])

    assert "logout" not in auth_calls(fake_commands)
    assert accounts.account_directory(1).is_dir()
    assert accounts.resolved_account_number("1") == 1
