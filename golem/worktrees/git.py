import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path

GIT_FETCH_TIMEOUT_SECONDS = 5
GIT_FETCH_MAX_AGE_SECONDS = 24 * 60 * 60
ORIGIN_HEAD = "refs/remotes/origin/HEAD"


def resolved_path(path: str) -> str:
    return str(Path(path).resolve())


def run_git(
    directory: str, *arguments: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", directory, *arguments],
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def git_common_directory(directory: str) -> str | None:
    result = run_git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode:
        return None
    return result.stdout.strip()


def git_has_head(directory: str) -> bool:
    return run_git(directory, "rev-parse", "--verify", "HEAD").returncode == 0


def fetch_stale_default_branch(cwd: str) -> None:
    default_branch = run_git(cwd, "symbolic-ref", "--quiet", "--short", ORIGIN_HEAD)
    fetch_head = run_git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD")
    if default_branch.returncode or fetch_head.returncode:
        return
    fetch_head_path = Path(fetch_head.stdout.strip())
    if (
        fetch_head_path.exists()
        and time.time() - fetch_head_path.stat().st_mtime < GIT_FETCH_MAX_AGE_SECONDS
    ):
        return
    with suppress(subprocess.TimeoutExpired):
        subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "fetch",
                "--quiet",
                "--no-tags",
                "origin",
                default_branch.stdout.strip().removeprefix("origin/"),
            ],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GIT_FETCH_TIMEOUT_SECONDS,
            check=False,
        )


def git_worktree_entries(repository: str) -> list[list[str]] | None:
    result = run_git(repository, "worktree", "list", "--porcelain")
    if result.returncode:
        return None
    return [entry.splitlines() for entry in result.stdout.strip().split("\n\n") if entry]


def git_worktree_entry(entries: list[list[str]], worktree: str) -> list[str] | None:
    return next(
        (entry for entry in entries if entry[0] == f"worktree {resolved_path(worktree)}"), None
    )


def git_worktree_lock_reason(entry: list[str]) -> str | None:
    for line in entry[1:]:
        if line == "locked":
            return ""
        if line.startswith("locked "):
            return line.removeprefix("locked ")
    return None
