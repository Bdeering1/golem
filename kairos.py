import argparse
import fcntl
import json
import os
import select
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, cast

LEDGER = Path(__file__).resolve().parent / "tasks.json"
LOCK = LEDGER.with_suffix(".lock")
SCHEDULES = LEDGER.parent / "schedules"
USAGE_TIMEOUT_SECONDS = 10


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


def update_tasks(change: Callable[[dict[str, Any]], None]) -> None:
    LOCK.touch(exist_ok=True)
    with LOCK.open("r+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            tasks = load_tasks()
            change(tasks)
            save_tasks(tasks)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def schedule_payload(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    cwd: str,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "prompt": prompt,
        "agent_args": list(agent_args),
        "cwd": cwd,
        "created_at": datetime.now(UTC).isoformat(),
    }


def reserve_task(task_id: str, record: Any) -> None:
    def add_task(tasks: dict[str, Any]) -> None:
        if task_id in tasks:
            raise SystemExit(f"kairos: task already exists: {task_id}")
        tasks[task_id] = record

    update_tasks(add_task)


def session_id(task_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kairos:{task_id}"))


def run(task_id: str, prompt: str, *agent_args: str) -> int:
    cwd = str(Path.cwd())
    reserve_task(task_id, cwd)
    return subprocess.run(
        ["claude", "--session-id", session_id(task_id), "--print", prompt, *agent_args],
        cwd=cwd,
        check=False,
    ).returncode


def pickup(task_id: str, *agent_args: str) -> int:
    tasks = load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"kairos: task not found: {task_id}")
    task = tasks[task_id]
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


def send_control_request(
    process: subprocess.Popen[str], request_id: str, request: dict[str, Any]
) -> None:
    if process.stdin is None:
        raise SystemExit("kairos: usage lookup unavailable: Claude input is closed")
    frame = {"type": "control_request", "request_id": request_id, "request": request}
    process.stdin.write(json.dumps(frame, separators=(",", ":")) + "\n")
    process.stdin.flush()


def control_response(process: subprocess.Popen[str], request_id: str) -> dict[str, object]:
    if process.stdout is None:
        raise SystemExit("kairos: usage lookup unavailable: Claude output is closed")
    deadline = monotonic() + USAGE_TIMEOUT_SECONDS
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SystemExit("kairos: usage lookup unavailable: Claude timed out")
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready:
            raise SystemExit("kairos: usage lookup unavailable: Claude timed out")
        line = process.stdout.readline()
        if not line:
            raise SystemExit("kairos: usage lookup unavailable: Claude closed the stream")
        try:
            frame_value: object = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit("kairos: usage lookup unavailable: invalid Claude response") from error
        if not isinstance(frame_value, dict):
            raise SystemExit("kairos: usage lookup unavailable: invalid Claude response")
        frame = cast(dict[str, object], frame_value)
        response_value = frame.get("response")
        if not isinstance(response_value, dict):
            continue
        response = cast(dict[str, object], response_value)
        if response.get("request_id") != request_id:
            continue
        if response.get("subtype") != "success":
            raise SystemExit("kairos: usage lookup unavailable: Claude rejected the request")
        payload_value = response.get("response")
        if not isinstance(payload_value, dict):
            raise SystemExit("kairos: usage lookup unavailable: missing response payload")
        return cast(dict[str, object], payload_value)


def close_usage_process(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=USAGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=USAGE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=USAGE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass


def next_reset_time() -> datetime:
    child_environment = os.environ.copy()
    child_environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    try:
        process = subprocess.Popen(
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
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as error:
        raise SystemExit(f"kairos: usage lookup unavailable: {error}") from error
    try:
        send_control_request(process, "init", {"subtype": "initialize"})
        control_response(process, "init")
        send_control_request(
            process,
            "usage",
            {"subtype": "get_usage", "skip_behaviors": True},
        )
        usage = control_response(process, "usage")
    except OSError as error:
        raise SystemExit("kairos: usage lookup unavailable: Claude closed the stream") from error
    finally:
        close_usage_process(process)

    if usage.get("rate_limits_available") is not True:
        raise SystemExit("kairos: usage lookup unavailable: rate limits are unavailable")
    rate_limits = usage.get("rate_limits")
    if not isinstance(rate_limits, dict):
        raise SystemExit("kairos: usage lookup unavailable: missing rate limits")
    rate_limit_values = cast(dict[str, object], rate_limits)
    five_hour = rate_limit_values.get("five_hour")
    if not isinstance(five_hour, dict):
        raise SystemExit("kairos: usage lookup unavailable: missing five-hour reset")
    five_hour_values = cast(dict[str, object], five_hour)
    reset_value = five_hour_values.get("resets_at")
    if not isinstance(reset_value, str):
        raise SystemExit("kairos: usage lookup unavailable: invalid five-hour reset")
    try:
        reset_time = datetime.fromisoformat(reset_value)
    except ValueError as error:
        raise SystemExit("kairos: usage lookup unavailable: invalid five-hour reset") from error
    if reset_time.tzinfo is None or reset_time.utcoffset() is None:
        raise SystemExit("kairos: usage lookup unavailable: invalid five-hour reset")
    if reset_time <= datetime.now(UTC):
        raise SystemExit("kairos: usage lookup unavailable: five-hour reset has passed")
    return reset_time


def write_payload(schedule_id: str, record: dict[str, Any]) -> Path:
    SCHEDULES.mkdir(mode=0o700, exist_ok=True)
    payload_path = SCHEDULES / f"{schedule_id}.json"
    payload_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    payload_path.chmod(0o600)
    return payload_path


def schedule_at(
    task_id: str,
    prompt: str,
    agent_args: tuple[str, ...],
    when: datetime,
) -> int:
    script_path = Path(__file__).resolve()
    interpreter_path = Path(sys.executable).resolve()
    if not script_path.is_file() or not interpreter_path.is_file():
        raise SystemExit("kairos: cannot locate the internal scheduler command")

    cwd = str(Path.cwd())
    schedule_id = str(uuid.uuid4())
    payload = schedule_payload(task_id, prompt, agent_args, cwd)
    payload_path = write_payload(schedule_id, payload)
    try:
        reserve_task(
            task_id,
            {"schedule_id": schedule_id, "cwd": cwd, "status": "scheduled"},
        )
    except BaseException:
        payload_path.unlink(missing_ok=True)
        raise

    command = " ".join(
        shlex.quote(part)
        for part in (str(interpreter_path), str(script_path), "_execute", schedule_id)
    )
    try:
        result = subprocess.run(
            ["at", "-t", at_time(when)],
            input=f"{command}\n",
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        message = str(error)
    else:
        if result.returncode == 0:
            return 0
        message = result.stderr.strip() or result.stdout.strip() or "at failed"

    def remove_task(tasks: dict[str, Any]) -> None:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return
        task_record = cast(dict[str, Any], task)
        if task_record.get("schedule_id") == schedule_id:
            del tasks[task_id]

    update_tasks(remove_task)
    payload_path.unlink(missing_ok=True)
    raise SystemExit(f"kairos: could not schedule task: {message}")


def execute_schedule(schedule_id: str) -> int:
    try:
        canonical_schedule_id = str(uuid.UUID(schedule_id))
    except ValueError as error:
        raise SystemExit(f"kairos: invalid schedule id: {schedule_id}") from error
    if schedule_id != canonical_schedule_id:
        raise SystemExit(f"kairos: invalid schedule id: {schedule_id}")

    payload_path = SCHEDULES / f"{schedule_id}.json"
    try:
        payload = cast(dict[str, object], json.loads(payload_path.read_text(encoding="utf-8")))
    except FileNotFoundError as error:
        raise SystemExit(f"kairos: schedule payload not found: {schedule_id}") from error

    task_id = payload.get("id")
    prompt = payload.get("prompt")
    agent_args_value = payload.get("agent_args")
    cwd = payload.get("cwd")
    if (
        not isinstance(task_id, str)
        or not isinstance(prompt, str)
        or not isinstance(agent_args_value, list)
        or not all(
            isinstance(agent_arg, str)
            for agent_arg in cast(list[object], agent_args_value)
        )
        or not isinstance(cwd, str)
    ):
        raise SystemExit(f"kairos: invalid schedule payload: {schedule_id}")
    agent_args = cast(list[str], agent_args_value)
    claimed = False

    def claim_task(tasks: dict[str, Any]) -> None:
        nonlocal claimed
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return
        task_record = cast(dict[str, Any], task)
        if (
            task_record.get("schedule_id") != schedule_id
            or task_record.get("status") != "scheduled"
        ):
            return
        task_record["status"] = "running"
        task_record["started_at"] = datetime.now(UTC).isoformat()
        claimed = True

    update_tasks(claim_task)
    if not claimed:
        return 0

    log_path = SCHEDULES / f"{schedule_id}.log"
    child_environment = os.environ.copy()
    child_environment.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            result = subprocess.run(
                [
                    "claude",
                    "--session-id",
                    session_id(task_id),
                    "--print",
                    prompt,
                    *agent_args,
                ],
                cwd=cwd,
                env=child_environment,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=False,
            )
            returncode = result.returncode
        except OSError as error:
            print(f"kairos: could not execute scheduled task: {error}", file=log_file)
            returncode = 127
    log_path.chmod(0o600)

    def record_outcome(tasks: dict[str, Any]) -> None:
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            return
        task_record = cast(dict[str, Any], task)
        if task_record.get("schedule_id") != schedule_id:
            return
        task_record["status"] = "completed" if returncode == 0 else "failed"
        task_record["completed_at"] = datetime.now(UTC).isoformat()
        task_record["returncode"] = returncode
        task_record["log_path"] = str(log_path)

    update_tasks(record_outcome)
    return returncode


def main() -> int:
    kairos_args = sys.argv[1:]
    agent_args: list[str] = []
    if "--" in kairos_args:
        separator_index = kairos_args.index("--")
        agent_args = kairos_args[separator_index + 1 :]
        kairos_args = kairos_args[:separator_index]

    parser = argparse.ArgumentParser(prog="kairos")
    commands = parser.add_subparsers(dest="command", required=True)

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
    execute_command = commands.add_parser("_execute", help=argparse.SUPPRESS)
    execute_command.add_argument("schedule_id")
    arguments = parser.parse_args(kairos_args)

    if arguments.command == "_execute":
        return execute_schedule(arguments.schedule_id)
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
