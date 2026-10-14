import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import io
    from collections.abc import Callable, Generator

STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
SUCCEEDED_STATUSES = {STATUS_NEEDS_REVIEW, STATUS_DONE}
USAGE_ERROR = "golem: usage lookup unavailable"
USAGE_TIMEOUT_SECONDS = 30
USAGE_LIMIT_HTTP_STATUS = 429
FULL_UTILIZATION = 100
MAX_USAGE_LIMIT_RESUMES = 2
USAGE_LIMIT_RESUME_PROMPT = "The usage limit has reset. Continue the task."
SCHEDULE_DESCRIPTION = "Run at the next usage reset, or immediately if no usage window is active."
LAUNCH_AGENT_ERROR = "golem: the agent is not installed or is out of date; run golem agent install"
LAUNCH_AGENT_LABEL = "com.github.bdeering1.golem"
LAUNCH_AGENT_INTERVAL_SECONDS = 300
LAUNCH_AGENT_POLL_SECONDS = 10
WORKTREE_ARGUMENTS = {"-w", "--worktree"}
NAME_ARGUMENTS = {"-n", "--name"}
PERMISSION_ARGUMENTS = {
    "--allow-dangerously-skip-permissions",
    "--dangerously-skip-permissions",
    "--permission-mode",
}
VERBOSE_ARGUMENT = "--verbose"


def ledger_path() -> Path:
    state_home = Path(os.environ.get("XDG_STATE_HOME", ""))
    state_directory = (
        state_home / "golem"
        if state_home.is_absolute()
        else Path.home() / ".local" / "state" / "golem"
    )
    state_directory.mkdir(parents=True, exist_ok=True)
    return state_directory / "tasks.json"


def load_tasks() -> dict[str, Any]:
    task_ledger_path = ledger_path()
    if not task_ledger_path.exists():
        return {}
    return json.loads(task_ledger_path.read_text(encoding="utf-8"))


def save_tasks(tasks: dict[str, Any]) -> None:
    task_ledger_path = ledger_path()
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=task_ledger_path.parent,
        prefix=f".{task_ledger_path.name}.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(json.dumps(tasks, indent=2) + "\n")
        temporary_path.chmod(0o600)
        temporary_path.replace(task_ledger_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def locked_tasks() -> Generator[dict[str, Any]]:
    lock_path = ledger_path().with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        tasks = load_tasks()
        original_ledger_text = json.dumps(tasks)
        yield tasks
        if json.dumps(tasks) != original_ledger_text:
            save_tasks(tasks)


def run_lock_path(session_id: str) -> Path:
    lock_directory = ledger_path().parent / "running"
    lock_directory.mkdir(exist_ok=True)
    return lock_directory / f"{session_id}.lock"


def acquire_lock(lock_path: Path) -> io.TextIOWrapper | None:
    lock_file = lock_path.open("a", encoding="utf-8")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def release_lock(lock_file: io.TextIOWrapper) -> None:
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()


def acquire_run_lock(session_id: str) -> io.TextIOWrapper | None:
    return acquire_lock(run_lock_path(session_id))


def release_run_lock(run_lock_file: io.TextIOWrapper, session_id: str) -> None:
    run_lock_path(session_id).unlink(missing_ok=True)
    release_lock(run_lock_file)


def directory_lock_path(directory: str) -> Path:
    lock_directory = ledger_path().parent / "directories"
    lock_directory.mkdir(exist_ok=True)
    resolved_directory = str(Path(directory).resolve())
    lock_name = hashlib.sha256(resolved_directory.encode()).hexdigest()
    return lock_directory / f"{lock_name}.lock"


def reserve_task(task_id: str, record: str | dict[str, Any]) -> None:
    with locked_tasks() as tasks:
        if task_id in tasks:
            raise SystemExit(f"golem: task already exists: {task_id}")
        tasks[task_id] = record


def waiting_dependent_ids(tasks: dict[str, Any], parent_id: str) -> list[str]:
    return [
        dependent_id
        for dependent_id, dependent in tasks.items()
        if dependent["status"] == STATUS_WAITING and dependent.get("after") == parent_id
    ]


def reserve_dependent_task(
    task_id: str,
    record: dict[str, Any],
    parent_id: str,
) -> bool:
    with locked_tasks() as tasks:
        if task_id in tasks:
            raise SystemExit(f"golem: task already exists: {task_id}")
        if parent_id not in tasks:
            raise SystemExit(f"golem: task not found: {parent_id}")
        parent_status = tasks[parent_id]["status"]
        if parent_status == STATUS_CANCELLED:
            raise SystemExit(f"golem: parent task is cancelled: {parent_id}")
        if parent_status in SUCCEEDED_STATUSES:
            return False
        tasks[task_id] = record
        return True


def launch_agent_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def golem_command() -> list[str]:
    return [str(Path(sys.executable).absolute()), str(Path(__file__).resolve())]


def launch_agent_plist_data() -> bytes:
    log_path = str(ledger_path().parent / "agent.log")
    plist: dict[str, object] = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", *golem_command(), "_agent"],
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


def new_session_id() -> str:
    return str(uuid.uuid4())


def is_captured_variable(name: str) -> bool:
    return name in {"PATH", "LANG"} or name.startswith("LC_")


def captured_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if is_captured_variable(name)}


def task_environment(task: dict[str, Any]) -> dict[str, str]:
    inherited_environment = {
        name: value for name, value in os.environ.items() if not is_captured_variable(name)
    }
    return {**inherited_environment, **task.get("environment", {})}


def has_agent_argument(agent_args: list[str], names: set[str]) -> bool:
    return any(
        argument == name or argument.startswith(f"{name}=")
        for argument in agent_args
        for name in names
    )


def resolved_worktree(tasks: dict[str, Any], task: dict[str, Any]) -> tuple[str | None, bool]:
    if "worktree" in task:
        return task["worktree"], task.get("worktree_pending") is True
    parent = tasks.get(task.get("after", ""))
    if parent is not None and parent["cwd"] == task["cwd"] and "worktree" in parent:
        return parent["worktree"], parent.get("worktree_pending") is True
    if has_agent_argument(task["agent_args"], WORKTREE_ARGUMENTS):
        return None, False
    result = subprocess.run(
        [
            "git",
            "-C",
            task["cwd"],
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            "HEAD",
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        return None, False
    common_directory = Path(result.stdout.splitlines()[0])
    return str(common_directory.parent / ".claude" / "worktrees" / task["session_id"]), True


def task_directory(task: dict[str, Any]) -> str:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        return worktree
    return task["cwd"]


def locked_task_directory(task: dict[str, Any], worktree: str | None) -> str:
    return worktree if worktree is not None else task["cwd"]


def worktree_missing_message(worktree: str | None, *, is_worktree_pending: bool) -> str | None:
    if worktree is None or is_worktree_pending or Path(worktree).is_dir():
        return None
    return f"golem: worktree is missing: {worktree}"


def reject_missing_worktree(task: dict[str, Any]) -> None:
    missing_worktree = worktree_missing_message(
        task.get("worktree"), is_worktree_pending=task.get("worktree_pending") is True
    )
    if missing_worktree is not None:
        raise SystemExit(missing_worktree)


def clear_worktree_pending_if_created(task: dict[str, Any]) -> None:
    worktree = task.get("worktree")
    if worktree is not None and Path(worktree).is_dir():
        task.pop("worktree_pending", None)


def record_resolved_worktree(
    task: dict[str, Any], worktree: str | None, *, is_worktree_pending: bool
) -> None:
    if worktree is None:
        return
    task["worktree"] = worktree
    if is_worktree_pending:
        task["worktree_pending"] = True
    else:
        task.pop("worktree_pending", None)
    clear_worktree_pending_if_created(task)


def run_claude(
    task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str]:
    session_id = cast("str", task["session_id"])
    is_resuming = cast("int", task.get("usage_limit_resume_count", 0)) > 0
    command = (
        ["claude", "--resume", session_id, "--print", USAGE_LIMIT_RESUME_PROMPT]
        if is_resuming
        else ["claude", "--session-id", session_id, "--print", cast("str", task["prompt"])]
    )
    agent_args = cast("list[str]", task["agent_args"])
    default_args: list[str] = []
    if VERBOSE_ARGUMENT not in agent_args:
        default_args.append(VERBOSE_ARGUMENT)
    if not has_agent_argument(agent_args, NAME_ARGUMENTS):
        default_args += ["--name", task_id]
    worktree = task.get("worktree")
    if worktree is not None and not Path(worktree).is_dir():
        default_args += ["--worktree", Path(worktree).name]
    if not has_agent_argument(agent_args, PERMISSION_ARGUMENTS):
        default_args.append("--dangerously-skip-permissions")
    claude_process = subprocess.run(
        [
            *command,
            "--output-format",
            "json",
            *agent_args,
            *default_args,
        ],
        cwd=task_directory(task),
        env=task_environment(task),
        stdout=subprocess.PIPE,
        encoding="utf-8",
        pass_fds=lock_file_descriptors,
        check=False,
    )
    return claude_process.returncode, claude_process.stdout


def new_task_record(
    cwd: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "cwd": cwd,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "status": STATUS_WAITING,
        "session_id": new_session_id(),
        "environment": captured_environment(),
    }
    if after is not None:
        record["after"] = after
    if when is not None:
        record["status"] = STATUS_SCHEDULED
        record["due_at"] = when.isoformat()
    return record


def pickup(task_id: str, *agent_args: str, is_app: bool) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if task.get("status") in {STATUS_SCHEDULED, STATUS_WAITING, STATUS_CANCELLED}:
            raise SystemExit(f"golem: task has not started: {task_id}")
        reject_missing_worktree(task)
        directory = locked_task_directory(task, task.get("worktree"))
        directory_lock_file = acquire_lock(directory_lock_path(directory))
        if directory_lock_file is None:
            raise SystemExit(f"golem: directory is busy: {directory}")
        if task.get("status") == STATUS_NEEDS_REVIEW:
            task["status"] = STATUS_DONE
        cwd = task_directory(task)
        session_id = cast("str", task["session_id"])
    if is_app:
        release_lock(directory_lock_file)
        return subprocess.run(
            ["open", f"claude://resume?session={session_id}"], check=False
        ).returncode
    try:
        return subprocess.run(
            ["claude", "--resume", session_id, *agent_args],
            cwd=cwd,
            pass_fds=(directory_lock_file.fileno(),),
            check=False,
        ).returncode
    finally:
        release_lock(directory_lock_file)


def close(task_id: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if task.get("status") != STATUS_NEEDS_REVIEW:
            raise SystemExit(f"golem: task does not need review: {task_id}")
        task["status"] = STATUS_DONE
    return 0


def show_task_result(task_id: str) -> int:
    tasks = load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    task = cast("dict[str, Any]", tasks[task_id])
    result = task.get("result")
    if not isinstance(result, str):
        raise SystemExit(f"golem: task has no result: {task_id}")
    print_task_result(result)
    return 0


def cancel_task(task_id: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if task.get("status") not in {STATUS_SCHEDULED, STATUS_WAITING}:
            raise SystemExit(f"golem: task is not scheduled: {task_id}")
        task["status"] = STATUS_CANCELLED
        pending_task_ids = [task_id]
        while pending_task_ids:
            parent_id = pending_task_ids.pop()
            for waiting_task_id in waiting_dependent_ids(tasks, parent_id):
                waiting_task = cast("dict[str, Any]", tasks[waiting_task_id])
                waiting_task["status"] = STATUS_CANCELLED
                pending_task_ids.append(waiting_task_id)
    return 0


def list_tasks() -> int:
    tasks = load_tasks()
    if not tasks:
        print("golem: no tasks")
        return 0
    rows = [
        (
            task_id,
            task["status"],
            task["cwd"],
            task.get("due_at", ""),
            f"{task['after']} ({tasks[task['after']]['status']})"
            if task["status"] == STATUS_WAITING
            else "",
        )
        for task_id, task in tasks.items()
    ]
    rows.sort(key=lambda row: (row[1] != STATUS_NEEDS_REVIEW, row[0]))
    headers = ("ID", "STATUS", "DIRECTORY", "DUE AT", "AFTER")
    widths = [
        max(len(str(value)) for value in column) for column in zip(headers, *rows, strict=True)
    ]
    for row in (headers, *rows):
        print(
            "  ".join(
                str(value).ljust(width) for value, width in zip(row, widths, strict=True)
            ).rstrip()
        )
    return 0


def scheduled_time(value: str) -> datetime:
    match = re.fullmatch(r"(1[0-2]|[1-9])(?::([0-5][0-9]))?(am|pm)( tomorrow)?", value.lower())
    if match is None:
        raise SystemExit('golem: --at expects a time like 9pm or "8:30am tomorrow"')
    hour, minute, period, tomorrow = match.groups()
    now = datetime.now(UTC)
    local_time = now.astimezone().replace(tzinfo=None)
    when = local_time.replace(
        hour=int(hour) % 12 + (12 if period == "pm" else 0),
        minute=int(minute or 0),
        second=0,
        microsecond=0,
    )
    if tomorrow or when.astimezone() <= now:
        when += timedelta(days=1)
    return when.astimezone()


def next_reset_time() -> datetime | None:
    requests = (
        {
            "type": "control_request",
            "request_id": "init",
            "request": {"subtype": "initialize"},
        },
        {
            "type": "control_request",
            "request_id": "usage",
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        },
    )
    child_environment = os.environ.copy()
    child_environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                "haiku",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--no-session-persistence",
                VERBOSE_ARGUMENT,
            ],
            input="".join(json.dumps(request) + "\n" for request in requests),
            env=child_environment,
            capture_output=True,
            encoding="utf-8",
            timeout=USAGE_TIMEOUT_SECONDS,
            check=False,
        )
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        responses = {
            frame["response"]["request_id"]: frame["response"]
            for frame in frames
            if frame["type"] == "control_response"
        }
        initialized = responses["init"]["subtype"] == "success"
        usage = responses["usage"]
        if (
            result.returncode
            or not initialized
            or usage["subtype"] != "success"
            or usage["response"]["rate_limits_available"] is not True
        ):
            raise SystemExit(USAGE_ERROR)
        rate_limits = usage["response"]["rate_limits"]
        five_hour_limit = rate_limits["five_hour"]
        seven_day_limit = rate_limits.get("seven_day")
        exhausted_limits = [
            limit
            for limit in (five_hour_limit, seven_day_limit)
            if limit is not None and limit["utilization"] >= FULL_UTILIZATION
        ]
        if exhausted_limits:
            return max(reset_time_from_limit(limit) for limit in exhausted_limits)
        if five_hour_limit is None or five_hour_limit["resets_at"] is None:
            return None
        return reset_time_from_limit(five_hour_limit)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
    ) as error:
        raise SystemExit(USAGE_ERROR) from error


def reset_time_from_limit(limit: dict[str, Any]) -> datetime:
    reset_time = datetime.fromisoformat(limit["resets_at"])
    if reset_time.tzinfo is None or reset_time <= datetime.now(UTC):
        raise SystemExit(USAGE_ERROR)
    return reset_time


def schedule_new_task(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
    after: str | None,
) -> int:
    due_time = when or datetime.now(UTC).astimezone()
    record = new_task_record(str(Path.cwd()), prompt, agent_args, due_time, after)
    reserve_task(task_id, record)
    return wake_launch_agent_and_report(task_id, due_time)


def claude_result_and_usage_limit(output: str) -> tuple[str, bool]:
    try:
        parsed_response = cast("object", json.loads(output))
    except json.JSONDecodeError:
        return output, False
    if isinstance(parsed_response, list):
        parsed_response = next(
            (
                cast("dict[str, object]", message)
                for message in reversed(cast("list[object]", parsed_response))
                if isinstance(message, dict)
                and cast("dict[str, object]", message).get("type") == "result"
            ),
            None,
        )
    if not isinstance(parsed_response, dict):
        return output, False
    response = cast("dict[str, object]", parsed_response)
    result = response.get("result")
    if not isinstance(result, str):
        return output, False
    return result, response.get("api_error_status") == USAGE_LIMIT_HTTP_STATUS


def claude_working_directory(output: str) -> str | None:
    try:
        parsed_response = cast("object", json.loads(output))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed_response, list):
        return None
    for message in cast("list[object]", parsed_response):
        if not isinstance(message, dict):
            continue
        message_fields = cast("dict[str, object]", message)
        working_directory = message_fields.get("cwd")
        if message_fields.get("subtype") == "init" and isinstance(working_directory, str):
            return working_directory
    return None


def record_worktree(task_id: str, worktree: str) -> None:
    with locked_tasks() as tasks:
        tasks[task_id]["worktree"] = worktree
        tasks[task_id].pop("worktree_pending", None)


def clear_recorded_worktree_pending(task_id: str) -> None:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if isinstance(task, dict):
            clear_worktree_pending_if_created(cast("dict[str, Any]", task))


def mark_task_waiting_for_reset(task_id: str, result: str) -> bool:
    try:
        when = planned_execution_time(is_now=False, at_text=None)
    except SystemExit:
        return False
    if when is None:
        return False
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return False
        task_record = cast("dict[str, Any]", task)
        if task_record.get("status") != STATUS_RUNNING:
            return False
        task_record["result"] = result
        task_record["status"] = STATUS_SCHEDULED
        task_record["due_at"] = when.isoformat()
        task_record["usage_limit_resume_count"] = task_record.get("usage_limit_resume_count", 0) + 1
        return True


def finish_task(
    task_id: str,
    result: str | None,
    returncode: int,
    *,
    is_usage_limit: bool,
) -> None:
    with locked_tasks() as tasks:
        stored_task = tasks.get(task_id)
        if not isinstance(stored_task, dict):
            return
        task_record = cast("dict[str, Any]", stored_task)
        if task_record.get("status") != STATUS_RUNNING:
            return
        if result is not None:
            task_record["result"] = result
        task_record["status"] = (
            STATUS_NEEDS_REVIEW if returncode == 0 and not is_usage_limit else STATUS_FAILED
        )
        task_record["returncode"] = returncode


def claim_task(
    tasks: dict[str, Any],
    task_id: str,
    session_id: str,
    expected_status: str,
) -> tuple[dict[str, Any], io.TextIOWrapper, io.TextIOWrapper] | None:
    task = tasks.get(task_id)
    if task is None or task["status"] != expected_status or task["session_id"] != session_id:
        return None
    run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return None
    worktree, is_worktree_pending = resolved_worktree(tasks, task)
    if missing_worktree := worktree_missing_message(
        worktree, is_worktree_pending=is_worktree_pending
    ):
        record_resolved_worktree(task, worktree, is_worktree_pending=is_worktree_pending)
        task["status"] = STATUS_FAILED
        task["result"] = missing_worktree
        release_run_lock(run_lock_file, session_id)
        return None
    directory_lock_file = acquire_lock(directory_lock_path(locked_task_directory(task, worktree)))
    if directory_lock_file is None:
        release_run_lock(run_lock_file, session_id)
        return None
    record_resolved_worktree(task, worktree, is_worktree_pending=is_worktree_pending)
    task["status"] = STATUS_RUNNING
    return task.copy(), run_lock_file, directory_lock_file


def claim_next_dependent(
    task_id: str,
) -> tuple[str, dict[str, Any], io.TextIOWrapper, io.TextIOWrapper] | None:
    with locked_tasks() as tasks:
        parent_record = cast("dict[str, Any]", tasks[task_id])
        if parent_record.get("status") not in SUCCEEDED_STATUSES:
            return None
        for dependent_id in waiting_dependent_ids(tasks, task_id):
            session_id = tasks[dependent_id]["session_id"]
            claim = claim_task(tasks, dependent_id, session_id, STATUS_WAITING)
            if claim is not None:
                return dependent_id, *claim
            if tasks[dependent_id]["status"] == STATUS_WAITING:
                return None
    return None


def print_task_result(result: str) -> None:
    print(result, end="" if result.endswith("\n") else "\n")


def drain_dependents(task_id: str) -> None:
    while next_dependent := claim_next_dependent(task_id):
        execute_claimed_task(*next_dependent)


def execute_task(task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]) -> int:
    try:
        returncode, output = run_claude(task_id, task, lock_file_descriptors)
    except OSError as error:
        print(f"golem: could not execute task: {error}", file=sys.stderr)
        returncode = 127
        output = ""
    result, is_usage_limit = claude_result_and_usage_limit(output)
    working_directory = claude_working_directory(output)
    if working_directory is not None and working_directory != task["cwd"]:
        record_worktree(task_id, working_directory)
    clear_recorded_worktree_pending(task_id)
    usage_limit_resume_count = cast("int", task.get("usage_limit_resume_count", 0))
    if (
        is_usage_limit
        and usage_limit_resume_count < MAX_USAGE_LIMIT_RESUMES
        and mark_task_waiting_for_reset(task_id, result)
    ):
        return 0
    finish_task(
        task_id,
        result if output else None,
        returncode,
        is_usage_limit=is_usage_limit,
    )
    return returncode


def execute_claimed_task(
    task_id: str,
    task: dict[str, Any],
    run_lock_file: io.TextIOWrapper,
    directory_lock_file: io.TextIOWrapper,
) -> int:
    try:
        try:
            returncode = execute_task(
                task_id,
                task,
                (run_lock_file.fileno(), directory_lock_file.fileno()),
            )
        finally:
            release_lock(directory_lock_file)
        drain_dependents(task_id)
        return returncode
    finally:
        release_run_lock(run_lock_file, task["session_id"])


def execute_scheduled_task(task_id: str, session_id: str) -> int:
    with locked_tasks() as tasks:
        claim = claim_task(tasks, task_id, session_id, STATUS_SCHEDULED)
    if claim is None:
        return 0
    return execute_claimed_task(task_id, *claim)


def drain_succeeded_task(task_id: str, session_id: str) -> None:
    with locked_tasks():
        run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return
    try:
        drain_dependents(task_id)
    finally:
        release_run_lock(run_lock_file, session_id)


def execute_or_drain_task(task_id: str, session_id: str) -> int:
    task = load_tasks().get(task_id)
    if task is None or task["session_id"] != session_id or task["status"] not in SUCCEEDED_STATUSES:
        return execute_scheduled_task(task_id, session_id)
    drain_succeeded_task(task_id, session_id)
    return 0


def start_task_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*golem_command(), "_execute", task_id, task["session_id"]],
        env=task_environment(task),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )


def is_run_lock_free(session_id: str) -> bool:
    run_lock_file = acquire_run_lock(session_id)
    if run_lock_file is None:
        return False
    release_run_lock(run_lock_file, session_id)
    return True


def run_launch_agent_pass() -> list[subprocess.Popen[bytes]]:
    now = datetime.now(UTC)
    due_tasks: list[tuple[str, dict[str, Any]]] = []
    with locked_tasks() as tasks:
        for task_id, task in tasks.items():
            if task["status"] == STATUS_RUNNING:
                if is_run_lock_free(task["session_id"]):
                    task["status"] = STATUS_FAILED
            elif (
                task["status"] == STATUS_SCHEDULED and datetime.fromisoformat(task["due_at"]) <= now
            ) or (
                task["status"] in SUCCEEDED_STATUSES
                and waiting_dependent_ids(tasks, task_id)
                and is_run_lock_free(task["session_id"])
            ):
                due_tasks.append((task_id, task.copy()))
    processes: list[subprocess.Popen[bytes]] = []
    for task_id, task in due_tasks:
        processes.append(start_task_executor(task_id, task))
    return processes


def has_pending_tasks(tasks: dict[str, Any]) -> bool:
    return any(
        task["status"] in {STATUS_SCHEDULED, STATUS_RUNNING}
        or (
            task["status"] == STATUS_WAITING
            and tasks.get(task.get("after", ""), {}).get("status") in SUCCEEDED_STATUSES
        )
        for task in tasks.values()
    )


def reap_finished_executors(
    processes: list[subprocess.Popen[bytes]],
) -> list[subprocess.Popen[bytes]]:
    return [process for process in processes if process.poll() is None]


def run_launch_agent() -> int:
    processes = run_launch_agent_pass()
    while has_pending_tasks(load_tasks()):
        time.sleep(LAUNCH_AGENT_POLL_SECONDS)
        processes = reap_finished_executors(processes)
        processes.extend(run_launch_agent_pass())
    return 0


def retryable_task(tasks: dict[str, Any], task_id: str) -> dict[str, Any]:
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    task = cast("dict[str, Any]", tasks[task_id])
    if task.get("status") not in {STATUS_FAILED, STATUS_CANCELLED}:
        raise SystemExit(f"golem: task cannot be retried: {task_id}")
    reject_missing_worktree(task)
    return task


def retry_task(task_id: str, *, is_now: bool, at_text: str | None) -> int:
    retryable_task(load_tasks(), task_id)
    require_launch_agent()
    when = planned_execution_time(is_now=is_now, at_text=at_text) or datetime.now(UTC).astimezone()

    with locked_tasks() as tasks:
        task = retryable_task(tasks, task_id)
        record = new_task_record(
            cast("str", task["cwd"]),
            cast("str", task["prompt"]),
            tuple(cast("list[str]", task["agent_args"])),
            when,
            task.get("after"),
        )
        for name in ("environment", "worktree", "worktree_pending"):
            if name in task:
                record[name] = task[name]
        tasks[task_id] = record
    return wake_launch_agent_and_report(task_id, when)


def planned_execution_time(*, is_now: bool, at_text: str | None) -> datetime | None:
    if is_now:
        return None
    when = next_reset_time() if at_text is None else scheduled_time(at_text)
    if when is None:
        return None
    when = when.astimezone(UTC)
    if when.second or when.microsecond:
        when += timedelta(minutes=1)
    return when.replace(second=0, microsecond=0).astimezone()


def add_scheduling_arguments(add_argument: Callable[..., argparse.Action]) -> None:
    add_argument("--now", action="store_true", help="run immediately")
    add_argument(
        "--at",
        metavar="TIME",
        help='local time, e.g. 9pm or "8:30am tomorrow"; rolls forward if passed',
    )


def set_command_handler(
    command_parser: argparse.ArgumentParser, handler: Callable[[argparse.Namespace], int]
) -> None:
    command_parser.set_defaults(handler=handler)


def run_task_from_arguments(arguments: argparse.Namespace) -> int:
    require_launch_agent()
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8") if arguments.file else arguments.prompt
    )
    task_args = tuple(arguments.agent_args)
    if arguments.after is not None:
        dependent_record = new_task_record(
            str(Path.cwd()), prompt_text, task_args, None, arguments.after
        )
        if reserve_dependent_task(arguments.id, dependent_record, arguments.after):
            print(f"golem: {arguments.id} will run after {arguments.after}")
            return 0
    when = planned_execution_time(is_now=arguments.now, at_text=arguments.at)
    return schedule_new_task(arguments.id, prompt_text, task_args, when, arguments.after)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="golem")
    commands = parser.add_subparsers(dest="command", required=True)

    run_command = commands.add_parser(
        "run",
        description=SCHEDULE_DESCRIPTION,
    )
    run_command.add_argument("id")
    prompt_group = run_command.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("prompt", nargs="?")
    prompt_group.add_argument("--file")
    run_schedule_group = run_command.add_mutually_exclusive_group()
    add_scheduling_arguments(run_schedule_group.add_argument)
    run_schedule_group.add_argument("--after", metavar="PARENT", help="run after PARENT succeeds")
    set_command_handler(run_command, run_task_from_arguments)

    pickup_command = commands.add_parser("pickup")
    pickup_command.add_argument("id")
    pickup_command.add_argument(
        "--app",
        action="store_true",
        help="open the session in the Claude desktop app, in the app's default permission mode",
    )
    set_command_handler(
        pickup_command,
        lambda arguments: pickup(arguments.id, *arguments.agent_args, is_app=arguments.app),
    )
    close_command = commands.add_parser("close")
    close_command.add_argument("id")
    set_command_handler(close_command, lambda arguments: close(arguments.id))
    show_command = commands.add_parser("show")
    show_command.add_argument("id")
    set_command_handler(show_command, lambda arguments: show_task_result(arguments.id))
    cancel_command = commands.add_parser("cancel")
    cancel_command.add_argument("id")
    set_command_handler(cancel_command, lambda arguments: cancel_task(arguments.id))
    retry_command = commands.add_parser(
        "retry",
        description=SCHEDULE_DESCRIPTION,
    )
    retry_command.add_argument("id")
    retry_schedule_group = retry_command.add_mutually_exclusive_group()
    add_scheduling_arguments(retry_schedule_group.add_argument)
    set_command_handler(
        retry_command,
        lambda arguments: retry_task(arguments.id, is_now=arguments.now, at_text=arguments.at),
    )
    set_command_handler(commands.add_parser("list"), lambda _: list_tasks())
    launch_agent_command = commands.add_parser("agent")
    launch_agent_command.add_argument("action", choices=("install", "uninstall"))
    set_command_handler(
        launch_agent_command,
        lambda arguments: (
            install_launch_agent() if arguments.action == "install" else uninstall_launch_agent()
        ),
    )
    set_command_handler(commands.add_parser("_agent"), lambda _: run_launch_agent())
    execute_command = commands.add_parser("_execute")
    execute_command.add_argument("id")
    execute_command.add_argument("session_id")
    set_command_handler(
        execute_command,
        lambda arguments: execute_or_drain_task(arguments.id, arguments.session_id),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    cast("io.TextIOWrapper", sys.stdout).reconfigure(encoding="utf-8")
    golem_args = sys.argv[1:] if argv is None else argv
    agent_args: list[str] = []
    if "--" in golem_args:
        separator_index = golem_args.index("--")
        agent_args = golem_args[separator_index + 1 :]
        golem_args = golem_args[:separator_index]

    parser = build_parser()
    arguments = parser.parse_args(golem_args, argparse.Namespace(agent_args=agent_args))
    if agent_args and arguments.command == "retry":
        parser.error("retry does not accept agent arguments")
    if agent_args and arguments.command == "pickup" and arguments.app:
        parser.error("pickup --app does not accept agent arguments")
    return arguments.handler(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
