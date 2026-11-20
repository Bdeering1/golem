import json
import os
import subprocess
from datetime import UTC, datetime

import pytest

from golem import cli
from golem.backends import claude
from tests import support

EARLIER_RESET_TIME = datetime(2030, 1, 1, 11, tzinfo=UTC)
LATER_RESET_TIME = datetime(2030, 1, 2, 11, tzinfo=UTC)


@pytest.fixture
def empty_git_repository(tmp_path):
    repository_path = tmp_path / f"{support.REPOSITORY_DIRECTORY}-empty"
    repository_path.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository_path, check=True)
    return repository_path


@pytest.mark.parametrize(
    (
        "agent_args",
        "repository_fixture",
        "has_default_name",
        "has_default_worktree",
        "has_default_skip_permissions",
    ),
    [
        pytest.param([], "git_repository", True, True, True, id="defaults"),
        pytest.param([], None, True, False, True, id="non-git"),
        pytest.param([], "empty_git_repository", True, False, True, id="no-commits"),
        pytest.param(
            ["--permission-mode", "plan"], "git_repository", True, True, False, id="permission-mode"
        ),
        pytest.param(
            ["--permission-mode=plan"],
            "git_repository",
            True,
            True,
            False,
            id="permission-mode-value",
        ),
        pytest.param(
            ["--allow-dangerously-skip-permissions"],
            "git_repository",
            True,
            True,
            False,
            id="allow-permissions",
        ),
        pytest.param(
            [support.WORKTREE_FLAG, "custom"], "git_repository", True, False, True, id="worktree"
        ),
        pytest.param(["-w", "custom"], "git_repository", True, False, True, id="short-worktree"),
        pytest.param(["--name", "custom"], "git_repository", False, True, True, id="name"),
        pytest.param(["-n", "custom"], "git_repository", False, True, True, id="short-name"),
    ],
)
def test_headless_defaults_respect_agent_arguments(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    request,
    agent_args,
    repository_fixture,
    has_default_name,
    has_default_worktree,
    has_default_skip_permissions,
):
    if repository_fixture is not None:
        monkeypatch.chdir(request.getfixturevalue(repository_fixture))
    cli.main(["run", support.PROMPT, "--now", "--", *agent_args])
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    arguments = support.logged_claude_args(fake_commands)
    assert arguments[arguments.index("--output-format") + 2 :] == [
        *agent_args,
        claude.VERBOSE_ARGUMENT,
        *(["--name", support.TASK_ID] if has_default_name else []),
        *([support.WORKTREE_FLAG, task["session_id"]] if has_default_worktree else []),
        *([support.SKIP_PERMISSIONS] if has_default_skip_permissions else []),
    ]


@pytest.mark.parametrize(
    ("five_hour", "seven_day", "expected"),
    [
        pytest.param(
            {"utilization": 74, "resets_at": EARLIER_RESET_TIME.isoformat()},
            {"utilization": 74, "resets_at": LATER_RESET_TIME.isoformat()},
            EARLIER_RESET_TIME,
            id="five-hour-available",
        ),
        pytest.param(
            {"utilization": 100, "resets_at": LATER_RESET_TIME.isoformat()},
            {"utilization": 100, "resets_at": EARLIER_RESET_TIME.isoformat()},
            LATER_RESET_TIME,
            id="latest-exhausted-window",
        ),
        pytest.param(
            {"utilization": 74, "resets_at": EARLIER_RESET_TIME.isoformat()},
            {"utilization": 100, "resets_at": LATER_RESET_TIME.isoformat()},
            LATER_RESET_TIME,
            id="weekly-exhausted",
        ),
    ],
)
def test_next_reset_time_picks_latest_exhausted_window(
    fake_commands, five_hour, seven_day, expected
):
    frames = [
        {"type": "control_response", "response": {"request_id": "init", "subtype": "success"}},
        {
            "type": "control_response",
            "response": {
                "request_id": "usage",
                "subtype": "success",
                "response": {
                    "rate_limits": {"five_hour": five_hour, "seven_day": seven_day},
                    "rate_limits_available": True,
                },
            },
        },
    ]
    fake_commands["claude-stdout"].write_text(
        "\n".join(json.dumps(frame) for frame in frames), encoding="utf-8"
    )

    assert claude.next_reset_time(os.environ.copy()) == expected


def test_labelled_task_session_name_includes_label(
    installed_launch_agent, fake_commands, ledger_path
):
    cli.main(["run", support.PROMPT, "--now", "--label", support.LABEL])
    task = support.read_task(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    arguments = support.logged_claude_args(fake_commands)
    assert arguments[arguments.index("--name") + 1] == f"{support.TASK_ID} {support.LABEL}"
