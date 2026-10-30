import shutil

import pytest

from golem import branch_pull_requests, ledger
from tests import support

FEATURE_BRANCH = "feature"
NESTED_BRANCH = "nested-feature"
DEFAULT_BRANCH = "main"
OPEN_URL = "https://github.com/owner/repository/pull/1"
MERGED_URL = "https://github.com/owner/nested/pull/2"


def nested_repository(parent_path, name, git_repository_template, branch):
    repository_path = parent_path / name
    shutil.copytree(git_repository_template, repository_path)
    support.git_output(repository_path, "checkout", "-qB", branch)
    return repository_path


def stored_pull_requests(ledger_path):
    return support.read_task(ledger_path).get("pull_requests")


def test_refresh_stores_pull_requests_of_repository_and_nested_repositories(
    git_repository, git_repository_template, ledger_path, write_pull_requests
):
    support.git_output(git_repository, "checkout", "-qb", FEATURE_BRANCH)
    (git_repository / ".gitignore").write_text("nested/\n", encoding="utf-8")
    nested_path = nested_repository(
        git_repository, "nested", git_repository_template, NESTED_BRANCH
    )
    default_branch_path = nested_repository(
        nested_path, "default", git_repository_template, DEFAULT_BRANCH
    )
    support.git_output(
        default_branch_path, "update-ref", f"refs/remotes/origin/{DEFAULT_BRANCH}", "HEAD"
    )
    support.git_output(
        default_branch_path,
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        f"refs/remotes/origin/{DEFAULT_BRANCH}",
    )
    write_pull_requests(
        FEATURE_BRANCH,
        [{"url": OPEN_URL, "state": "OPEN"}, {"url": f"{OPEN_URL}0", "state": "CLOSED"}],
    )
    write_pull_requests(NESTED_BRANCH, [{"url": MERGED_URL, "state": "MERGED"}])
    write_pull_requests(DEFAULT_BRANCH, [{"url": "unexpected", "state": "OPEN"}])
    support.write_task(ledger_path, ledger.STATUS_DONE, cwd=str(git_repository), no_worktree=True)

    branch_pull_requests.refresh_pull_requests()

    assert stored_pull_requests(ledger_path) == [
        {"url": OPEN_URL, "state": ledger.PULL_REQUEST_STATE_OPEN},
        {"url": f"{OPEN_URL}0", "state": ledger.PULL_REQUEST_STATE_CLOSED},
        {"url": MERGED_URL, "state": ledger.PULL_REQUEST_STATE_MERGED},
    ]


def test_refresh_stores_worktree_task_pull_requests_and_tells_none_found_apart(
    owned_worktree, ledger_path, write_pull_requests
):
    worktree = owned_worktree()
    support.write_tasks(
        ledger_path,
        {
            support.TASK_ID: support.task_record(ledger.STATUS_DONE, worktree=str(worktree)),
            "unchecked": support.task_record(ledger.STATUS_DONE, task_id="unchecked"),
        },
    )
    write_pull_requests(support.WORKTREE_BRANCH, [])

    branch_pull_requests.refresh_pull_requests()

    tasks = support.read_tasks(ledger_path)
    assert tasks[support.TASK_ID]["pull_requests"] == []
    assert "pull_requests" not in tasks["unchecked"]


@pytest.mark.parametrize(
    ("stored", "is_gh_available"),
    [
        pytest.param(
            [{"url": OPEN_URL, "state": ledger.PULL_REQUEST_STATE_OPEN}], False, id="failed-lookup"
        ),
        pytest.param(
            [{"url": MERGED_URL, "state": ledger.PULL_REQUEST_STATE_MERGED}],
            True,
            id="finished",
        ),
    ],
)
def test_refresh_leaves_stored_pull_requests(
    owned_worktree,
    ledger_path,
    write_pull_requests,
    monkeypatch,
    tmp_path,
    stored,
    is_gh_available,
):
    worktree = owned_worktree()
    support.write_task(
        ledger_path, ledger.STATUS_DONE, worktree=str(worktree), pull_requests=stored
    )
    write_pull_requests(support.WORKTREE_BRANCH, [{"url": "new", "state": "OPEN"}])
    if not is_gh_available:
        git_only_path = tmp_path / "git-only-bin"
        git_only_path.mkdir()
        (git_only_path / "git").symlink_to(str(shutil.which("git")))
        monkeypatch.setenv("PATH", str(git_only_path))

    branch_pull_requests.refresh_pull_requests()

    assert stored_pull_requests(ledger_path) == stored


@pytest.mark.parametrize(
    "current_fields",
    [
        pytest.param({"status": ledger.STATUS_SCHEDULED}, id="no-longer-done"),
        pytest.param({"session_id": "rerun-session"}, id="rerun"),
    ],
)
def test_store_skips_task_changed_since_lookup(ledger_path, current_fields):
    checked_task = support.task_record(ledger.STATUS_DONE)
    support.write_tasks(ledger_path, {support.TASK_ID: {**checked_task, **current_fields}})

    branch_pull_requests.store_pull_requests(
        {support.TASK_ID: checked_task},
        [support.TASK_ID],
        [{"url": OPEN_URL, "state": ledger.PULL_REQUEST_STATE_OPEN}],
    )

    assert stored_pull_requests(ledger_path) is None
