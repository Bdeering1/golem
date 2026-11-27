import shutil
import sys
from pathlib import Path
from typing import cast

from golem import ledger
from golem.backends import common
from golem.worktrees import git

WORKTREE_OWNER_GOLEM = "golem"
WORKTREE_OWNER_EXTERNAL = "external"
WORKTREE_OWNER_MARKER = "golem-owner"
WORKTREE_INCLUDE_FILE = ".worktreeinclude"


def recorded_worktree(tasks: ledger.Tasks, task: ledger.Task) -> tuple[str, bool] | None:
    if task.get("no_worktree"):
        return None
    ancestor = task
    while True:
        if "worktree" in ancestor:
            return ancestor["worktree"], ancestor.get("worktree_pending") is True
        parent = tasks.get(ancestor.get("after", ""))
        if parent is None:
            return None
        ancestor = parent
        if ancestor.get("cwd") != task.get("cwd"):
            return None


def worktree_resource(
    worktrees: ledger.Worktrees, worktree: str | None
) -> ledger.WorktreeResource | None:
    if worktree is None:
        return None
    return worktrees.get(git.resolved_path(worktree))


def clear_worktree_release(
    tasks: ledger.Tasks, worktrees: ledger.Worktrees, task: ledger.Task
) -> None:
    for worktree, resource in worktrees.items():
        if task_uses_worktree(tasks, task, worktree):
            resource.pop("released_at", None)


def record_worktree_resource(
    worktrees: ledger.Worktrees,
    worktree: str,
    session_id: str,
    *,
    owner: str = WORKTREE_OWNER_EXTERNAL,
    repository: str | None = None,
) -> None:
    if owner == WORKTREE_OWNER_EXTERNAL:
        repository = git.git_common_directory(worktree)
    resource: ledger.WorktreeResource = {
        "owner": owner,
        "session_id": session_id,
    }
    if repository is not None:
        resource["repository"] = repository
    worktrees.setdefault(git.resolved_path(worktree), resource)


def worktree_owner_marker_path(worktree: str) -> Path | None:
    result = git.run_git(worktree, "rev-parse", "--absolute-git-dir")
    if result.returncode:
        return None
    return Path(result.stdout.strip()) / WORKTREE_OWNER_MARKER


def is_worktree_owner_verified(worktree: str, session_id: str) -> bool:
    marker_path = worktree_owner_marker_path(worktree)
    if marker_path is None:
        return False
    try:
        return marker_path.read_text(encoding="utf-8") == session_id
    except OSError:
        return False


def record_created_worktree_owner(task_id: str, task: ledger.Task) -> None:
    worktree = task.get("worktree")
    if worktree is None:
        return
    with ledger.locked_ledger() as (tasks, worktrees):
        current_task = tasks.get(task_id)
        resource = worktree_resource(worktrees, worktree)
        if (
            current_task is None
            or current_task.get("session_id") != task["session_id"]
            or current_task.get("worktree") != worktree
            or resource is None
            or resource.get("owner") != WORKTREE_OWNER_GOLEM
            or git.git_common_directory(worktree) != resource.get("repository")
        ):
            return
        marker_path = worktree_owner_marker_path(worktree)
        if marker_path is None:
            return
        try:
            with marker_path.open("x", encoding="utf-8") as marker_file:
                marker_file.write(resource["session_id"])
        except FileExistsError:
            return
        except OSError as error:
            print(f"golem: could not record worktree ownership: {error}", file=sys.stderr)


def is_worktree_left_to_agent(task: ledger.Task) -> bool:
    return bool(task.get("no_worktree")) or common.has_agent_argument(
        task["agent_args"], common.WORKTREE_ARGUMENTS
    )


def resolved_worktree(tasks: ledger.Tasks, task: ledger.Task) -> tuple[str | None, bool]:
    recorded = recorded_worktree(tasks, task)
    if recorded is not None:
        return recorded
    if is_worktree_left_to_agent(task):
        return None, False
    common_directory = git.git_common_directory(task["cwd"])
    if common_directory is None or not git.git_has_head(task["cwd"]):
        return None, False
    return str(Path(common_directory).parent / ".claude" / "worktrees" / task["session_id"]), True


def worktree_missing_message(
    worktree: str | None,
    worktrees: ledger.Worktrees,
    *,
    is_worktree_pending: bool,
) -> str | None:
    if worktree is None or is_worktree_pending or Path(worktree).is_dir():
        return None
    resource = worktree_resource(worktrees, worktree)
    if resource is not None and resource.get("removed_at") is not None:
        return f"golem: worktree was removed: {worktree}"
    return f"golem: worktree is missing: {worktree}"


def reject_missing_worktree(task: ledger.Task, worktrees: ledger.Worktrees) -> None:
    missing_worktree = worktree_missing_message(
        task.get("worktree"), worktrees, is_worktree_pending=task.get("worktree_pending") is True
    )
    if missing_worktree is not None:
        raise SystemExit(missing_worktree)


def clear_worktree_pending_if_created(task: ledger.Task) -> None:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        task.pop("worktree_pending", None)


def record_resolved_worktree(
    task: ledger.Task, worktree: str | None, *, is_worktree_pending: bool
) -> None:
    if worktree is None:
        return
    task["worktree"] = worktree
    if is_worktree_pending:
        task["worktree_pending"] = True
    else:
        task.pop("worktree_pending", None)
    clear_worktree_pending_if_created(task)


def task_uses_worktree(tasks: ledger.Tasks, task: ledger.Task, worktree: str) -> bool:
    recorded = recorded_worktree(tasks, task)
    directories = [task["cwd"]]
    if recorded is not None:
        directories.append(recorded[0])
    worktree_path = Path(worktree).resolve()
    return any(Path(directory).resolve().is_relative_to(worktree_path) for directory in directories)


def worktree_task_ids(tasks: ledger.Tasks, worktree: str) -> list[str]:
    return [task_id for task_id, task in tasks.items() if task_uses_worktree(tasks, task, worktree)]


def copy_worktree_include_files(cwd: str, worktree: str) -> None:
    toplevel = git.run_git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
    if not Path(toplevel, WORKTREE_INCLUDE_FILE).is_file():
        return
    ignored = git.excluded_untracked_files(toplevel, "--exclude-standard")
    included = git.excluded_untracked_files(toplevel, f"--exclude-from={WORKTREE_INCLUDE_FILE}")
    for relative_path in sorted(ignored & included):
        source = Path(toplevel, relative_path)
        if source.is_symlink():
            continue
        destination = Path(worktree, relative_path)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        except OSError as error:
            print(f"golem: could not copy {relative_path} to worktree: {error}", file=sys.stderr)


def create_worktree(task_id: str, task: ledger.Task) -> str | None:
    worktree = cast("str", task.get("worktree"))
    cwd = task["cwd"]
    git.fetch_stale_default_branch(cwd)
    base = git.ORIGIN_HEAD
    if git.run_git(cwd, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode:
        base = "HEAD"
    result = git.run_git(
        cwd,
        "worktree",
        "add",
        "--no-track",
        "-b",
        f"worktree-{Path(worktree).name}",
        worktree,
        base,
    )
    if result.returncode:
        error = result.stderr.strip() or result.stdout.strip()
        return f"golem: could not create worktree: {error}"
    common_directory = cast("str", git.git_common_directory(cwd))
    exclude_path = Path(common_directory) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    exclude_text = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    if ".claude/worktrees/" not in exclude_text.splitlines():
        separator = "\n" if exclude_text and not exclude_text.endswith("\n") else ""
        with exclude_path.open("a", encoding="utf-8") as exclude_file:
            exclude_file.write(f"{separator}.claude/worktrees/\n")
    record_created_worktree_owner(task_id, task)
    copy_worktree_include_files(cwd, worktree)
    return None


def prepare_worktree(task_id: str, task: ledger.Task) -> str | None:
    worktree = task.get("worktree")
    if worktree is None or Path(worktree).is_dir():
        return None
    if task.get("worktree_pending") is not True:
        return f"golem: worktree is missing: {worktree}"
    return create_worktree(task_id, task)
