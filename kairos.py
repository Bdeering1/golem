import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast


LEDGER = Path(__file__).resolve().parent / "tasks.json"
LOCK_PATH = LEDGER.with_suffix(".lock")
STATUS_SCHEDULED = "scheduled"
USAGE_ERROR = "kairos: usage lookup unavailable"
USAGE_TIMEOUT_SECONDS = 30


def load_tasks() -> dict[str, Any]:
    if not LEDGER.exists():
        return {}
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def save_tasks(tasks: dict[str, Any]) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=LEDGER.parent,
        prefix=f".{LEDGER.name}.",
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(json.dumps(tasks, indent=2) + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, LEDGER)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@contextmanager
def locked_tasks() -> Generator[dict[str, Any]]:
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        tasks = load_tasks()
        yield tasks
        save_tasks(tasks)


def reserve_task(task_id: str, record: Any) -> None:
    with locked_tasks() as tasks:
        if task_id in tasks:
            raise SystemExit(f"kairos: task already exists: {task_id}")
        tasks[task_id] = record


def session_id(task_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kairos:{task_id}"))


def run_claude(task_id: str, cwd: str, prompt: str, *agent_args: str) -> int:
    return subprocess.run(
        ["claude", "--session-id", session_id(task_id), "--print", prompt, *agent_args],
        cwd=cwd,
        check=False,
    ).returncode


def run(task_id: str, prompt: str, *agent_args: str) -> int:
    cwd = str(Path.cwd())
    reserve_task(task_id, cwd)
    return run_claude(task_id, cwd, prompt, *agent_args)


def pickup(task_id: str, *agent_args: str) -> int:
    tasks = load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"kairos: task not found: {task_id}")
    task = tasks[task_id]
    if not isinstance(task, str) and task["status"] == STATUS_SCHEDULED:
        raise SystemExit(f"kairos: task has not started: {task_id}")
    cwd = task if isinstance(task, str) else task["cwd"]
    return subprocess.run(
        ["claude", "--resume", session_id(task_id), *agent_args],
        cwd=cwd,
        check=False,
    ).returncode


def scheduled_time(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value)
    except ValueError as error:
        raise SystemExit(f"kairos: invalid --at time: {value}") from error
    if when.tzinfo is None or when.utcoffset() is None:
        raise SystemExit("kairos: --at requires an explicit UTC offset")
    if when <= datetime.now(UTC):
        raise SystemExit("kairos: --at time must be in the future")
    return when


def at_time(when: datetime) -> str:
    local_time = when.astimezone()
    if local_time.second or local_time.microsecond:
        local_time += timedelta(minutes=1)
    return local_time.replace(second=0, microsecond=0).strftime("%Y%m%d%H%M.%S")


def next_reset_time() -> datetime:
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
            text=True,
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
            raise ValueError
        five_hour_limit = usage["response"]["rate_limits"]["five_hour"]
        if five_hour_limit is None or five_hour_limit["resets_at"] is None:
            raise SystemExit("kairos: no active usage window")
        reset_time = datetime.fromisoformat(five_hour_limit["resets_at"])
        if reset_time.tzinfo is None or reset_time <= datetime.now(UTC):
            raise ValueError
        return reset_time
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
    ) as error:
        raise SystemExit(USAGE_ERROR) from error


def schedule_at(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime,
) -> int:
    record = {
        "cwd": str(Path.cwd()),
        "prompt": prompt,
        "agent_args": list(agent_args),
        "due_at": when.isoformat(),
        "status": STATUS_SCHEDULED,
    }
    reserve_task(task_id, record)

    command = " ".join(
        shlex.quote(part)
        for part in (
            str(Path(sys.executable).absolute()),
            str(Path(__file__).resolve()),
            "_execute",
            task_id,
        )
    )
    try:
        result = subprocess.run(
            ["at", "-t", at_time(when)],
            input=f"{command}\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return 0
        message = result.stderr.strip() or result.stdout.strip() or "at failed"
    except OSError as error:
        message = str(error)

    with locked_tasks() as tasks:
        if tasks.get(task_id) == record:
            del tasks[task_id]
    raise SystemExit(f"kairos: could not schedule task: {message}")


def execute_scheduled_task(task_id: str) -> int:
    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return 0
        task_record = cast(dict[str, Any], task)
        if task_record.get("status") != STATUS_SCHEDULED:
            return 0
        task_record["status"] = "running"
        cwd = cast(str, task_record["cwd"])
        prompt = cast(str, task_record["prompt"])
        agent_args = cast(list[str], task_record["agent_args"])

    try:
        returncode = run_claude(task_id, cwd, prompt, *agent_args)
    except OSError as error:
        print(f"kairos: could not execute scheduled task: {error}", file=sys.stderr)
        returncode = 127

    with locked_tasks() as tasks:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return returncode
        task_record = cast(dict[str, Any], task)
        if task_record.get("status") == "running":
            task_record["status"] = "completed" if returncode == 0 else "failed"
            task_record["returncode"] = returncode
    return returncode


def main() -> int:
    kairos_args = sys.argv[1:]
    agent_args: list[str] = []
    if "--" in kairos_args:
        separator_index = kairos_args.index("--")
        agent_args = kairos_args[separator_index + 1 :]
        kairos_args = kairos_args[:separator_index]

    parser = argparse.ArgumentParser(prog="kairos")
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{run,pickup}"
    )

    run_command = commands.add_parser("run")
    run_command.add_argument("id")
    prompt = run_command.add_mutually_exclusive_group(required=True)
    prompt.add_argument("prompt", nargs="?")
    prompt.add_argument("--file")
    schedule = run_command.add_mutually_exclusive_group()
    schedule.add_argument("--next-reset", action="store_true")
    schedule.add_argument("--at")

    pickup_command = commands.add_parser("pickup")
    pickup_command.add_argument("id")
    execute_command = commands.add_parser("_execute")
    execute_command.add_argument("id")
    arguments = parser.parse_args(kairos_args)

    if arguments.command == "_execute":
        return execute_scheduled_task(arguments.id)
    if arguments.command == "pickup":
        return pickup(arguments.id, *agent_args)
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8")
        if arguments.file
        else arguments.prompt
    )
    if arguments.at is not None:
        return schedule_at(
            arguments.id,
            prompt_text,
            tuple(agent_args),
            scheduled_time(arguments.at),
        )
    if arguments.next_reset:
        return schedule_at(
            arguments.id,
            prompt_text,
            tuple(agent_args),
            next_reset_time(),
        )
    return run(arguments.id, prompt_text, *agent_args)


if __name__ == "__main__":
    raise SystemExit(main())
