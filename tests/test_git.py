import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from golem import cli
from golem.worktrees import git
from tests import support


@pytest.mark.parametrize(
    ("fetched_age", "is_reachable", "is_fetched"),
    [
        pytest.param(timedelta(days=2), True, True, id="stale"),
        pytest.param(timedelta(hours=1), True, False, id="recent"),
        pytest.param(timedelta(days=2), False, False, id="unreachable"),
    ],
)
def test_codex_worktree_base_fetches_stale_default_branch(
    installed_launch_agent,
    git_repository,
    git_repository_template,
    ledger_path,
    monkeypatch,
    tmp_path,
    fetched_age,
    is_reachable,
    is_fetched,
):
    remote_path = tmp_path / "remote"
    shutil.copytree(git_repository_template, remote_path)
    support.git_output(git_repository, "remote", "add", "origin", str(remote_path))
    support.git_output(git_repository, "fetch", "-q", "origin")
    support.git_output(git_repository, "remote", "set-head", "origin", "--auto")
    cached_commit = support.git_output(git_repository, "rev-parse", "origin/HEAD")
    (remote_path / "README").write_text("updated\n", encoding="utf-8")
    support.git_output(
        remote_path,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "--no-gpg-sign",
        "-qam",
        "u",
    )
    remote_commit = support.git_output(remote_path, "rev-parse", "HEAD")
    if not is_reachable:
        support.git_output(git_repository, "remote", "set-url", "origin", str(tmp_path / "missing"))
    fetch_head_path = Path(
        support.git_output(
            git_repository, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"
        )
    )
    fetched_at = (datetime.now(UTC) - fetched_age).timestamp()
    os.utime(fetch_head_path, (fetched_at, fetched_at))
    monkeypatch.chdir(git_repository)
    cli.main(["run", support.PROMPT, "--backend", "codex", "--now"])

    assert (
        cli.main(["_execute", support.TASK_ID, support.read_task(ledger_path)["session_id"]]) == 0
    )

    worktree = Path(support.read_task(ledger_path)["worktree"])
    assert support.git_output(worktree, "rev-parse", "HEAD") == (
        remote_commit if is_fetched else cached_commit
    )
    assert git.run_git(str(worktree), "rev-parse", "--abbrev-ref", "@{upstream}").returncode
