import json
import subprocess
import sys
from pathlib import Path
from typing import cast

from golem import ledger, spawn
from golem.worktrees import git, task_directories

COMMAND_TIMEOUT_SECONDS = 30


def run_git_with_timeout(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return git.run_git(directory, *arguments, timeout=COMMAND_TIMEOUT_SECONDS)


def run_gh(
    arguments: list[str], *, cwd: str | None = None, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return None if result.returncode else result


def repositories_within(repository: str) -> list[str] | None:
    untracked = run_git_with_timeout(repository, "ls-files", "-z", "--others", "--directory")
    if untracked.returncode:
        return None
    repositories = [repository]
    for entry in untracked.stdout.split("\0"):
        nested_repository = Path(repository) / entry
        if not entry or nested_repository.is_symlink() or not (nested_repository / ".git").exists():
            continue
        nested_repositories = repositories_within(str(nested_repository))
        if nested_repositories is None:
            return None
        repositories.extend(nested_repositories)
    return repositories


def branches_to_record(repository: str) -> list[ledger.RepositoryBranch] | None:
    branch_result = run_git_with_timeout(repository, "symbolic-ref", "--short", "HEAD")
    if branch_result.returncode:
        return []
    remotes = run_git_with_timeout(repository, "remote")
    if remotes.returncode:
        return None
    if not remotes.stdout.strip():
        return []
    repository_view = run_gh(
        [
            "repo",
            "view",
            "--json",
            "nameWithOwner,defaultBranchRef",
            "--jq",
            ".nameWithOwner, .defaultBranchRef.name",
        ],
        cwd=repository,
    )
    if repository_view is None:
        print(
            f"golem: could not record branches: {repository}: gh repo view failed", file=sys.stderr
        )
        return None
    repository_name, _, default_branch = repository_view.stdout.partition("\n")
    branch = branch_result.stdout.strip()
    if branch == default_branch.strip():
        return []
    return [{"repository": repository_name, "branch": branch}]


def directory_branches(directory: str) -> list[ledger.RepositoryBranch] | None:
    try:
        top_level = run_git_with_timeout(directory, "rev-parse", "--show-toplevel")
        if top_level.returncode:
            return None
        repositories = repositories_within(top_level.stdout.strip())
        if repositories is None:
            return None
        branches: list[ledger.RepositoryBranch] = []
        for repository in repositories:
            found_branches = branches_to_record(repository)
            if found_branches is None:
                return None
            branches.extend(found_branches)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"golem: could not record branches: {directory}: {error}", file=sys.stderr)
        return None
    return branches


def task_branches(task_id: str) -> list[ledger.RepositoryBranch] | None:
    tasks = ledger.load_tasks()
    task = tasks.get(task_id)
    if task is None:
        return None
    if task.get("no_worktree"):
        return directory_branches(task["cwd"])
    recorded = task_directories.recorded_worktree(tasks, task)
    return None if recorded is None else directory_branches(recorded[0])


def pull_requests_on_branch(
    branch: ledger.RepositoryBranch, environment: dict[str, str]
) -> list[ledger.PullRequest] | None:
    result = run_gh(
        [
            "pr",
            "list",
            "--repo",
            branch["repository"],
            "--head",
            branch["branch"],
            "--state",
            "all",
            "--json",
            "url,state",
        ],
        environment=environment,
    )
    if result is None:
        return None
    try:
        pull_requests = [
            {"url": pull_request["url"], "state": pull_request["state"].lower()}
            for pull_request in cast("list[dict[str, str]]", json.loads(result.stdout))
        ]
    except ValueError, KeyError, TypeError, AttributeError:
        return None
    if any(
        pull_request["state"] not in ledger.PULL_REQUEST_STATES for pull_request in pull_requests
    ):
        return None
    return cast("list[ledger.PullRequest]", pull_requests)


def task_pull_requests(task: ledger.Task) -> list[ledger.PullRequest] | None:
    environment = spawn.task_environment(task)
    pull_requests: list[ledger.PullRequest] = []
    for branch in task.get("repository_branches", []):
        found_pull_requests = pull_requests_on_branch(branch, environment)
        if found_pull_requests is None:
            print(
                f"golem: could not look up pull requests: {branch['repository']}: "
                f"{branch['branch']}",
                file=sys.stderr,
            )
            return None
        pull_requests.extend(found_pull_requests)
    return pull_requests


def store_pull_requests(
    checked_tasks: ledger.Tasks, task_ids: list[str], pull_requests: list[ledger.PullRequest]
) -> None:
    with ledger.locked_tasks() as tasks:
        for task_id in task_ids:
            task = tasks.get(task_id)
            if (
                task is None
                or task["status"] != ledger.STATUS_FINISHED
                or task["session_id"] != checked_tasks[task_id]["session_id"]
            ):
                continue
            task["pull_requests"] = pull_requests


def refresh_pull_requests() -> None:
    tasks = ledger.load_tasks()
    task_ids_by_branches: dict[str, list[str]] = {}
    for task_id, task in tasks.items():
        if (
            task["status"] != ledger.STATUS_FINISHED
            or "repository_branches" not in task
            or ledger.has_finished_pull_requests(task)
        ):
            continue
        branches_key = json.dumps(task["repository_branches"], sort_keys=True)
        task_ids_by_branches.setdefault(branches_key, []).append(task_id)
    for task_ids in task_ids_by_branches.values():
        pull_requests = task_pull_requests(tasks[task_ids[0]])
        if pull_requests is not None:
            store_pull_requests(tasks, task_ids, pull_requests)
