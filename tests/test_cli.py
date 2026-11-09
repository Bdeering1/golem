import re
from datetime import UTC, datetime
from typing import override

import pytest

from golem import cli, launch_agent, ledger, scheduling
from tests import support


def test_alias_expands_arguments_verbatim_like_run(
    installed_launch_agent, ledger_path, monkeypatch
):
    class FixedDateTime(datetime):
        @classmethod
        @override
        def now(cls, tz=None) -> datetime:
            now = datetime(2030, 1, 1, 10, tzinfo=UTC)
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(scheduling, "datetime", FixedDateTime)
    monkeypatch.setattr(launch_agent, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduling, "new_session_id", lambda: "session")
    support.write_aliases({"impl": 'run {1} "/impl {2}" --then {1}-review /review-lite'})
    task_id = "t42"
    prompt = 'add "retry" {backoff}\nonce'

    assert cli.main(["impl", task_id, prompt, "--now"]) == 0
    alias_tasks = support.read_tasks(ledger_path)

    support.write_tasks(ledger_path, {})

    assert (
        cli.main(
            [
                "run",
                task_id,
                f"/impl {prompt}",
                "--then",
                f"{task_id}-review",
                "/review-lite",
                "--now",
            ]
        )
        == 0
    )

    assert support.read_tasks(ledger_path) == alias_tasks


def test_alias_appends_extra_arguments_before_template_agent_arguments(
    installed_launch_agent, ledger_path
):
    support.write_aliases({"queue": "run {1} {2} -- --model template-{1}"})

    assert (
        cli.main(
            [
                "queue",
                support.TASK_ID,
                support.PROMPT,
                "--now",
                "--",
                "--model",
                "command-line",
            ]
        )
        == 0
    )

    task = support.read_task(ledger_path)
    assert task["agent_args"] == [
        "--model",
        f"template-{support.TASK_ID}",
        "--model",
        "command-line",
    ]
    assert datetime.fromisoformat(task["due_at"]) < support.RESET_TIME


def test_alias_requires_all_placeholder_arguments(ledger_path):
    support.write_aliases({"impl": "run {1} {2}"})

    with pytest.raises(SystemExit, match=r"alias impl needs 2 arguments, got 1"):
        cli.main(["impl", support.TASK_ID])

    assert not ledger_path.exists()


def test_alias_list_marks_shadowed_builtin(capsys):
    support.write_aliases({"ship": "run {1} {2}", "run": "not a command"})

    assert cli.main(["alias", "list"]) == 0

    ship_line, run_line = capsys.readouterr().out.splitlines()
    assert "shadowed" not in ship_line
    assert "shadowed" in run_line


def test_builtin_command_wins_over_alias(installed_launch_agent, ledger_path):
    support.write_aliases({"run": "not a command"})

    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now"]) == 0

    assert support.read_task(ledger_path)["prompt"] == support.PROMPT


def test_alias_cannot_expand_to_another_alias(ledger_path):
    support.write_aliases({"impl": "review", "review": "run {1} {2}"})

    with pytest.raises(SystemExit, match=r"alias impl expands to alias review"):
        cli.main(["impl"])

    assert not ledger_path.exists()


def test_missing_and_invalid_alias_config_do_not_block_builtin_commands(capsys):
    assert cli.main(["alias", "list"]) == 0
    assert capsys.readouterr().out == "golem: no aliases\n"

    path = cli.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[aliases\n", encoding="utf-8")

    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == "golem: no tasks\n"
    with pytest.raises(SystemExit, match=re.escape(str(path))):
        cli.main(["alias", "list"])


def test_run_environment_precedence_and_follow_ups(installed_launch_agent, ledger_path):
    support.write_config_table(
        "env", {"SHARED": "config", "FROM_CONFIG": "config", "PATH": "/config/bin"}
    )

    assert (
        cli.main(
            [
                *["run", support.TASK_ID, support.PROMPT, "--now"],
                *["--env", "SHARED=first", "--env", "SHARED=flag", "--env", "EMPTY="],
                *["--then", support.CHILD_ID, support.CHILD_PROMPT],
            ]
        )
        == 0
    )

    expected = {"SHARED": "flag", "FROM_CONFIG": "config", "PATH": "/config/bin", "EMPTY": ""}
    for task_id in (support.TASK_ID, support.CHILD_ID):
        environment = support.read_task(ledger_path, task_id)["environment"]
        assert expected.items() <= environment.items()


def test_run_after_parent_uses_its_own_environment(installed_launch_agent, ledger_path):
    support.write_task(
        ledger_path,
        ledger.STATUS_SCHEDULED,
        task_id=support.PARENT_ID,
        environment={"PARENT_ONLY": "1"},
    )
    support.write_config_table("env", {"OWN": "config"})

    assert (
        cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID]) == 0
    )

    environment = support.read_task(ledger_path, support.CHILD_ID)["environment"]
    assert environment["OWN"] == "config"
    assert "PARENT_ONLY" not in environment


@pytest.mark.parametrize(
    ("config_text", "arguments", "message"),
    [
        pytest.param(None, ["--env", "NO_EQUALS"], "NAME=VALUE", id="flag-without-equals"),
        pytest.param(None, ["--env", "1BAD=x"], "name is invalid", id="flag-bad-name"),
        pytest.param("env = 1\n", [], "env must be a table", id="config-not-a-table"),
        pytest.param("[env]\nA = 1\n", [], "env A must be a string", id="config-non-string"),
        pytest.param('[env]\n"a-b" = "x"\n', [], "name a-b is invalid", id="config-bad-name"),
        pytest.param('[env]\nA = "a\\u0000b"\n', [], "contains a null byte", id="config-null-byte"),
    ],
)
def test_run_rejects_invalid_environment_without_changing_the_ledger(
    installed_launch_agent, ledger_path, config_text, arguments, message
):
    if config_text is not None:
        support.write_config_text(config_text)

    with pytest.raises(SystemExit, match=message):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--now", *arguments])

    assert not ledger_path.exists()


@pytest.mark.parametrize(
    ("config_text", "arguments", "expected_backend"),
    [
        pytest.param(None, [], ledger.BACKEND_CLAUDE, id="no-default"),
        pytest.param('default_backend = "codex"\n', [], ledger.BACKEND_CODEX, id="saved-default"),
        pytest.param(
            'default_backend = "codex"\n',
            ["--backend", ledger.BACKEND_CLAUDE],
            ledger.BACKEND_CLAUDE,
            id="flag-overrides-default",
        ),
    ],
)
def test_run_backend_comes_from_flag_then_saved_default(
    installed_launch_agent, ledger_path, config_text, arguments, expected_backend
):
    if config_text is not None:
        support.write_config_text(config_text)

    assert (
        cli.main(
            [
                *["run", support.TASK_ID, support.PROMPT, "--now", *arguments],
                *["--then", support.CHILD_ID, support.CHILD_PROMPT],
            ]
        )
        == 0
    )

    for task_id in (support.TASK_ID, support.CHILD_ID):
        assert support.read_task(ledger_path, task_id)["backend"] == expected_backend


def test_run_after_parent_uses_saved_default_not_parent_backend(
    installed_launch_agent, ledger_path
):
    support.write_task(
        ledger_path,
        ledger.STATUS_SCHEDULED,
        task_id=support.PARENT_ID,
        backend=ledger.BACKEND_CLAUDE,
    )
    support.write_config_text('default_backend = "codex"\n')

    assert (
        cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID]) == 0
    )

    assert support.read_task(ledger_path, support.CHILD_ID)["backend"] == ledger.BACKEND_CODEX


@pytest.mark.parametrize(
    ("config_text", "message"),
    [
        pytest.param("default_backend = 1\n", "default_backend must be a string", id="non-string"),
        pytest.param(
            'default_backend = "gemini"\n',
            "default_backend names an unknown backend: gemini",
            id="unknown",
        ),
    ],
)
def test_invalid_default_backend_fails_run_only(
    installed_launch_agent, ledger_path, config_text, message
):
    support.write_config_text(config_text)

    with pytest.raises(SystemExit, match=message):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])

    assert not ledger_path.exists()
    assert cli.main(["alias", "list"]) == 0
