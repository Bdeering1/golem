import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from golem import (
    branch_pull_requests,
    chains,
    ledger,
    locks,
    notifications,
    progress_log,
    spawn,
    task_directories,
    worktree_cleanup,
)
from golem.backends import claude_hooks

LAUNCH_AGENT_ERROR = "golem: the agent is not installed or is out of date; run golem agent install"
LAUNCH_AGENT_LABEL = "com.github.bdeering1.golem"
LAUNCH_AGENT_INTERVAL_SECONDS = 300
LAUNCH_AGENT_POLL_SECONDS = 10


def launch_agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def agent_log_path() -> Path:
    return ledger.ledger_path().parent / "agent.log"


def rotate_agent_log() -> None:
    log_path = agent_log_path()
    if log_path.exists() and log_path.stat().st_size > progress_log.AGENT_LOG_MAX_BYTES:
        shutil.copyfile(log_path, log_path.with_name(f"{log_path.name}.1"))
        log_path.write_text("", encoding="utf-8")


def launch_agent_plist_data() -> bytes:
    log_path = str(agent_log_path())
    plist: dict[str, object] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", *spawn.golem_command(), "_agent"],
        "RunAtLoad": True,
        "StartInterval": LAUNCH_AGENT_INTERVAL_SECONDS,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    state_home = os.environ.get("XDG_STATE_HOME", "")
    if Path(state_home).is_absolute():
        plist["EnvironmentVariables"] = {"XDG_STATE_HOME": state_home}
    return plistlib.dumps(plist)


def launch_agent_target() -> str:
    return f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"


def run_launchctl(arguments: list[str]) -> str | None:
    result = subprocess.run(
        ["launchctl", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return result.stderr.strip() or result.stdout.strip() or f"launchctl {arguments[0]} failed"


def install_launch_agent() -> int:
    plist_path = launch_agent_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(launch_agent_plist_data())
    run_launchctl(["bootout", launch_agent_target()])
    error = run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(plist_path)])
    if error is not None:
        plist_path.unlink()
        raise SystemExit(f"golem: could not install agent: {error}; run golem agent install again")
    return 0


def uninstall_launch_agent() -> int:
    run_launchctl(["bootout", launch_agent_target()])
    launch_agent_plist_path().unlink(missing_ok=True)
    return 0


def require_launch_agent() -> None:
    plist_path = launch_agent_plist_path()
    if not plist_path.exists() or plist_path.read_bytes() != launch_agent_plist_data():
        raise SystemExit(LAUNCH_AGENT_ERROR)


def wake_launch_agent_and_report(task_id: str, when: datetime) -> int:
    error = run_launchctl(["kickstart", launch_agent_target()])
    if error is not None:
        print(f"golem: could not wake agent: {error}", file=sys.stderr)
    if when <= datetime.now(UTC):
        print(f"golem: queued {task_id}")
        return 0
    print(f"golem: scheduled {task_id} for {when:%Y-%m-%d %H:%M %Z %z}")
    return 0


def is_known_directory_busy(tasks: ledger.Tasks, task: ledger.Task) -> bool:
    recorded = task_directories.recorded_worktree(tasks, task)
    if recorded is not None:
        directory = recorded[0]
    elif task_directories.is_worktree_left_to_agent(task):
        directory = task["cwd"]
    else:
        return False
    return locks.is_directory_busy(directory)


def run_launch_agent_pass() -> list[subprocess.Popen[bytes]]:
    rotate_agent_log()
    now = datetime.now(UTC)
    due_tasks: list[tuple[str, ledger.Task]] = []
    failed_tasks: list[tuple[str, ledger.Task]] = []
    hook_directories: set[str] = set()
    with ledger.locked_tasks() as tasks:
        locks.remove_orphan_run_locks()
        ledger.remove_interrupted_ledger_writes()
        for task_id, task in tasks.items():
            if task["status"] == ledger.STATUS_RUNNING:
                if locks.is_run_lock_free(task["session_id"]):
                    task["status"] = ledger.STATUS_FAILED
                    failed_tasks.append((task_id, task.copy()))
                    hook_directories |= claude_hooks.hook_affected_directories(tasks, task)
            elif (
                task["status"] == ledger.STATUS_SCHEDULED
                and (due_at := task.get("due_at")) is not None
                and datetime.fromisoformat(due_at) <= now
                and not is_known_directory_busy(tasks, task)
            ) or (
                chains.can_start_dependents(task)
                and (dependent_ids := chains.waiting_dependent_ids(tasks, task_id))
                and not chains.has_running_dependent(tasks, task_id)
                and not is_known_directory_busy(tasks, tasks[dependent_ids[0]])
                and locks.is_run_lock_free(task["session_id"])
            ):
                due_tasks.append((task_id, task.copy()))
    for directory in hook_directories:
        claude_hooks.sync_hooks(directory)
    notifications.notify_failed_tasks(failed_tasks)
    processes: list[subprocess.Popen[bytes]] = []
    for task_id, task in due_tasks:
        processes.append(spawn.start_task_executor(task_id, task))
    return processes


def has_pending_tasks(tasks: ledger.Tasks) -> bool:
    return any(
        task["status"] in {ledger.STATUS_SCHEDULED, ledger.STATUS_RUNNING}
        or (
            task["status"] == ledger.STATUS_WAITING
            and (parent := tasks.get(task.get("after", ""))) is not None
            and chains.can_start_dependents(parent)
        )
        for task in tasks.values()
    )


def run_launch_agent() -> int:
    pull_request_refresh = threading.Thread(target=branch_pull_requests.refresh_pull_requests)
    pull_request_refresh.start()
    worktree_cleanup.auto_remove_worktrees()
    processes = run_launch_agent_pass()
    while True:
        if has_pending_tasks(ledger.load_tasks()):
            time.sleep(LAUNCH_AGENT_POLL_SECONDS)
        elif pull_request_refresh.is_alive():
            # A wake while the agent still runs is dropped, so keep polling for queued tasks.
            pull_request_refresh.join(LAUNCH_AGENT_POLL_SECONDS)
        else:
            return 0
        processes = spawn.reap_finished_executors(processes)
        processes.extend(run_launch_agent_pass())
