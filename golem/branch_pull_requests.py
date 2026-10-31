import json
import subprocess
from pathlib import Path
from typing import cast

from golem import git, ledger, spawn, task_directories

PULL_REQUEST_LOOKUP_TIMEOUT_SECONDS = 30


def inspected_directory(tasks: ledger.Tasks, task: ledger.Task) -> str | None:
    if task.get("no_worktree"):
        directory = task["cwd"]
    else:
        recorded = task_directories.recorded_worktree(tasks, task)
        if recorded is None:
            return None
        directory = recorded[0]
    return directory if Path(directory).is_dir() else None


def repositories_within(repository: str) -> list[str] | None:
    untracked = git.run_git(repository, "ls-files", "-z", "--others", "--directory")
    if untracked.returncode:
        return None
    repositories = [repository]
    for entry in untracked.stdout.split("\0"):
        nested_repository = Path(repository) / entry
        if not entry or not (nested_repository / ".git").exists():
            continue
        nested_repositories = repositories_within(str(nested_repository))
        if nested_repositories is None:
            return None
        repositories.extend(nested_repositories)
    return repositories


def repository_pull_requests(
    repository: str, environment: dict[str, str]
) -> list[ledger.PullRequest] | None:
    branch = git.run_git(repository, "symbolic-ref", "--short", "HEAD")
    if branch.returncode:
        return []
    branch_name = branch.stdout.strip()
    default_branch = git.run_git(repository, "symbolic-ref", "--quiet", "--short", git.ORIGIN_HEAD)
    if (
        not default_branch.returncode
        and default_branch.stdout.strip().removeprefix("origin/") == branch_name
    ):
        return []
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", branch_name, "--state", "all", "--json", "url,state"],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=PULL_REQUEST_LOOKUP_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    try:
        return [
            {"url": pull_request["url"], "state": pull_request["state"].lower()}
            for pull_request in cast("list[ledger.PullRequest]", json.loads(result.stdout))
        ]
    except ValueError, KeyError, TypeError, AttributeError:
        return None


def directory_pull_requests(directory: str, task: ledger.Task) -> list[ledger.PullRequest] | None:
    top_level = git.run_git(directory, "rev-parse", "--show-toplevel")
    if top_level.returncode:
        return None
    repository = top_level.stdout.strip()
    # A removed worktree's leftover directory resolves to the main checkout around it.
    if not task.get("no_worktree") and git.resolved_path(repository) != git.resolved_path(
        directory
    ):
        return None
    repositories = repositories_within(repository)
    if repositories is None:
        return None
    pull_requests: list[ledger.PullRequest] = []
    for repository in repositories:
        found_pull_requests = repository_pull_requests(repository, spawn.task_environment(task))
        if found_pull_requests is None:
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
                or task["status"] != ledger.STATUS_DONE
                or task["session_id"] != checked_tasks[task_id]["session_id"]
            ):
                continue
            task["pull_requests"] = pull_requests


def refresh_pull_requests() -> None:
    tasks = ledger.load_tasks()
    task_ids_by_directory: dict[str, list[str]] = {}
    for task_id, task in tasks.items():
        if task["status"] != ledger.STATUS_DONE or ledger.has_finished_pull_requests(task):
            continue
        directory = inspected_directory(tasks, task)
        if directory is not None:
            task_ids_by_directory.setdefault(directory, []).append(task_id)
    for directory, task_ids in task_ids_by_directory.items():
        pull_requests = directory_pull_requests(directory, tasks[task_ids[0]])
        if pull_requests is not None:
            store_pull_requests(tasks, task_ids, pull_requests)
