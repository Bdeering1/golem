import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    import io
    from collections.abc import Generator

STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
USAGE_ERROR = "golem: usage lookup unavailable"
USAGE_TIMEOUT_SECONDS = 30
USAGE_LIMIT_HTTP_STATUS = 429
FULL_UTILIZATION = 100
MAX_USAGE_LIMIT_RESUMES = 2
USAGE_LIMIT_RESUME_PROMPT = "The usage limit has reset. Continue the task."
SCHEDULE_DESCRIPTION = "Run at the next usage reset, or immediately if no usage window is active."


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
        yield tasks
        save_tasks(tasks)


def reserve_task(task_id: str, record: str | dict[str, Any]) -> None:
    with locked_tasks() as tasks:
        if task_id in tasks:
            raise SystemExit(f"golem: task already exists: {task_id}")
        tasks[task_id] = record


def new_session_id() -> str:
    return str(uuid.uuid4())


def run_claude(task: dict[str, Any]) -> tuple[int, str]:
    session_id = cast("str", task["session_id"])
    is_resuming = cast("int", task.get("usage_limit_resume_count", 0)) > 0
    command = (
        ["claude", "--resume", session_id, "--print", USAGE_LIMIT_RESUME_PROMPT]
        if is_resuming
        else ["claude", "--session-id", session_id, "--print", cast("str", task["prompt"])]
    )
    claude_process = subprocess.run(
        [
            *command,
            "--output-format",
            "json",
            *cast("list[str]", task["agent_args"]),
        ],
        cwd=cast("str", task["cwd"]),
        stdout=subprocess.PIPE,
        encoding="utf-8",
        check=False,
    )
    return claude_process.returncode, claude_process.stdout


def new_task_record(
    cwd: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "cwd": cwd,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "status": STATUS_SCHEDULED if when else STATUS_RUNNING,
        "session_id": new_session_id(),
    }
    if when:
        record["due_at"] = when.isoformat()
    return record


def run(task_id: str, prompt: str, *agent_args: str) -> int:
    record = new_task_record(str(Path.cwd()), prompt, agent_args, None)
    reserve_task(task_id, record)
    return execute_task(task_id, record)


def pickup(task_id: str, *agent_args: str) -> int:
    with locked_tasks() as tasks:
        if task_id not in tasks:
            raise SystemExit(f"golem: task not found: {task_id}")
        task = cast("dict[str, Any]", tasks[task_id])
        if task.get("status") in {STATUS_SCHEDULED, STATUS_CANCELLED}:
            raise SystemExit(f"golem: task has not started: {task_id}")
        if task.get("status") == STATUS_NEEDS_REVIEW:
            task["status"] = STATUS_DONE
        cwd = cast("str", task["cwd"])
        session_id = cast("str", task["session_id"])
    return subprocess.run(
        ["claude", "--resume", session_id, *agent_args],
        cwd=cwd,
        check=False,
    ).returncode


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
        if task.get("status") != STATUS_SCHEDULED:
            raise SystemExit(f"golem: task is not scheduled: {task_id}")
        at_job = task.get("at_job")
        task["status"] = STATUS_CANCELLED
    if not isinstance(at_job, str):
        return 0
    remove_at_job(at_job)
    return 0


def remove_at_job(at_job: str) -> None:
    message: str | None = None
    try:
        result = subprocess.run(
            ["atrm", at_job],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        message = str(error)
    else:
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or "atrm failed"
    if message is not None:
        print(f"golem: could not cancel at job {at_job}: {message}", file=sys.stderr)


def list_tasks() -> int:
    tasks = load_tasks()
    if not tasks:
        print("golem: no tasks")
        return 0
    rows = [
        (task_id, task["status"], task["cwd"], task.get("due_at", ""))
        for task_id, task in tasks.items()
    ]
    rows.sort(key=lambda row: (row[1] != STATUS_NEEDS_REVIEW, row[0]))
    headers = ("ID", "STATUS", "DIRECTORY", "DUE AT")
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
                "--verbose",
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


def at_job_number(at_output: str) -> str | None:
    match = re.search(r"\bjob\s+(\d+)\s+at\b", at_output)
    if match is None:
        return None
    return match.group(1)


def submit_at_job(
    task_id: str,
    record: dict[str, Any],
    when: datetime,
    previous_record: dict[str, Any] | None = None,
) -> int:
    command = " ".join(
        shlex.quote(part)
        for part in (
            str(Path(sys.executable).absolute()),
            str(Path(__file__).resolve()),
            "_execute",
            task_id,
            cast("str", record["session_id"]),
        )
    )
    try:
        result = subprocess.run(
            ["at", "-t", when.strftime("%Y%m%d%H%M.%S")],
            input=f"{command}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            at_job = at_job_number(result.stderr)
            if at_job is None:
                message = "at queued a job but its number is unknown; remove it with atq and atrm"
            else:
                with locked_tasks() as tasks:
                    task = tasks.get(task_id)
                    is_cancelled = task == {**record, "status": STATUS_CANCELLED}
                    is_superseded = isinstance(task, dict) and (
                        cast("dict[str, Any]", task).get("session_id") != record["session_id"]
                    )
                    if task == record:
                        cast("dict[str, Any]", task)["at_job"] = at_job
                if is_cancelled or is_superseded:
                    remove_at_job(at_job)
                    raise SystemExit(f"golem: task was cancelled while scheduling: {task_id}")
                print(f"golem: scheduled {task_id} for {when:%Y-%m-%d %H:%M %Z %z}")
                return 0
        else:
            message = result.stderr.strip() or result.stdout.strip() or "at failed"
    except OSError as error:
        message = str(error)

    with locked_tasks() as tasks:
        if tasks.get(task_id) == record:
            if previous_record is None:
                del tasks[task_id]
            else:
                tasks[task_id] = previous_record
    raise SystemExit(f"golem: could not schedule task: {message}")


def schedule_new_task(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime,
) -> int:
    record = new_task_record(str(Path.cwd()), prompt, agent_args, when)
    reserve_task(task_id, record)
    return submit_at_job(task_id, record, when)


def claude_result_and_usage_limit(output: str) -> tuple[str, bool]:
    try:
        parsed_response = cast("object", json.loads(output))
    except json.JSONDecodeError:
        return output, False
    if not isinstance(parsed_response, dict):
        return output, False
    response = cast("dict[str, object]", parsed_response)
    result = response.get("result")
    if not isinstance(result, str):
        return output, False
    return result, response.get("api_error_status") == USAGE_LIMIT_HTTP_STATUS


def mark_task_waiting_for_reset(
    task_id: str,
    result: str,
) -> tuple[dict[str, Any], datetime] | None:
    try:
        when = planned_execution_time(is_now=False, at_text=None)
    except SystemExit:
        return None
    if when is None:
        return None
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return None
        task_record = cast("dict[str, Any]", task)
        if task_record.get("status") != STATUS_RUNNING:
            return None
        task_record["result"] = result
        task_record.pop("at_job", None)
        task_record["status"] = STATUS_SCHEDULED
        task_record["due_at"] = when.isoformat()
        task_record["usage_limit_resume_count"] = task_record.get("usage_limit_resume_count", 0) + 1
        return task_record.copy(), when


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


def print_task_result(result: str) -> None:
    print(result, end="" if result.endswith("\n") else "\n")


def execute_task(task_id: str, task: dict[str, Any]) -> int:
    try:
        returncode, output = run_claude(task)
    except OSError as error:
        print(f"golem: could not execute task: {error}", file=sys.stderr)
        returncode = 127
        output = ""
    result, is_usage_limit = claude_result_and_usage_limit(output)
    usage_limit_resume_count = cast("int", task.get("usage_limit_resume_count", 0))
    if is_usage_limit and usage_limit_resume_count < MAX_USAGE_LIMIT_RESUMES:
        rescheduled_task = mark_task_waiting_for_reset(task_id, result)
        if rescheduled_task is not None:
            rescheduled_record, when = rescheduled_task
            previous_record = {
                **task,
                "result": result,
                "status": STATUS_FAILED,
                "returncode": returncode,
            }
            submit_at_job(task_id, rescheduled_record, when, previous_record)
            print_task_result(result)
            return 0
    finish_task(
        task_id,
        result if output else None,
        returncode,
        is_usage_limit=is_usage_limit,
    )
    if output:
        print_task_result(result)
    return returncode


def execute_scheduled_task(task_id: str, session_id: str) -> int:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return 0
        task_record = cast("dict[str, Any]", task)
        if (
            task_record.get("status") != STATUS_SCHEDULED
            or task_record.get("session_id") != session_id
        ):
            return 0
        task_record["status"] = STATUS_RUNNING

    return execute_task(task_id, task_record)


def retryable_task(tasks: dict[str, Any], task_id: str) -> dict[str, Any]:
    if task_id not in tasks:
        raise SystemExit(f"golem: task not found: {task_id}")
    task = cast("dict[str, Any]", tasks[task_id])
    if task.get("status") not in {STATUS_FAILED, STATUS_CANCELLED}:
        raise SystemExit(f"golem: task cannot be retried: {task_id}")
    return task


def retry_task(task_id: str, *, is_now: bool, at_text: str | None) -> int:
    retryable_task(load_tasks(), task_id)
    when = planned_execution_time(is_now=is_now, at_text=at_text)

    with locked_tasks() as tasks:
        task = retryable_task(tasks, task_id)
        previous_record = task.copy()
        record = new_task_record(
            cast("str", task["cwd"]),
            cast("str", task["prompt"]),
            tuple(cast("list[str]", task["agent_args"])),
            when,
        )
        tasks[task_id] = record
    if when is None:
        return execute_task(task_id, record)
    return submit_at_job(task_id, record, when, previous_record)


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


def add_scheduling_arguments(command_parser: argparse.ArgumentParser) -> None:
    schedule_group = command_parser.add_mutually_exclusive_group()
    schedule_group.add_argument("--now", action="store_true", help="run immediately")
    schedule_group.add_argument(
        "--at",
        metavar="TIME",
        help='local time, e.g. 9pm or "8:30am tomorrow"; rolls forward if passed',
    )


def main(argv: list[str] | None = None) -> int:
    cast("io.TextIOWrapper", sys.stdout).reconfigure(encoding="utf-8")
    golem_args = sys.argv[1:] if argv is None else argv
    agent_args: list[str] = []
    if "--" in golem_args:
        separator_index = golem_args.index("--")
        agent_args = golem_args[separator_index + 1 :]
        golem_args = golem_args[:separator_index]

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
    add_scheduling_arguments(run_command)

    pickup_command = commands.add_parser("pickup")
    pickup_command.add_argument("id")
    close_command = commands.add_parser("close")
    close_command.add_argument("id")
    show_command = commands.add_parser("show")
    show_command.add_argument("id")
    cancel_command = commands.add_parser("cancel")
    cancel_command.add_argument("id")
    retry_command = commands.add_parser(
        "retry",
        description=SCHEDULE_DESCRIPTION,
    )
    retry_command.add_argument("id")
    add_scheduling_arguments(retry_command)
    commands.add_parser("list")
    execute_command = commands.add_parser("_execute")
    execute_command.add_argument("id")
    execute_command.add_argument("session_id")
    arguments = parser.parse_args(golem_args)

    if arguments.command == "_execute":
        return execute_scheduled_task(arguments.id, arguments.session_id)
    if arguments.command == "pickup":
        return pickup(arguments.id, *agent_args)
    if arguments.command == "close":
        return close(arguments.id)
    if arguments.command == "show":
        return show_task_result(arguments.id)
    if arguments.command == "cancel":
        return cancel_task(arguments.id)
    if arguments.command == "list":
        return list_tasks()
    if arguments.command == "retry":
        if agent_args:
            parser.error("retry does not accept agent arguments")
        return retry_task(arguments.id, is_now=arguments.now, at_text=arguments.at)
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8") if arguments.file else arguments.prompt
    )
    when = planned_execution_time(is_now=arguments.now, at_text=arguments.at)
    if when is None:
        return run(arguments.id, prompt_text, *agent_args)
    return schedule_new_task(arguments.id, prompt_text, tuple(agent_args), when)


if __name__ == "__main__":
    raise SystemExit(main())
