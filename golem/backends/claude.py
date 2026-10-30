import json
import os
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    import io

from golem import ledger, progress_log, spawn, task_directories
from golem.backends import common

PROGRESS_TOOL_INPUT_KEYS = ("command", "file_path", "pattern", "url", "description")
USAGE_LIMIT_HTTP_STATUS = 429
BACKGROUND_WAIT_CEILING_VARIABLE = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
NAME_ARGUMENTS = {"-n", "--name"}
PERMISSION_ARGUMENTS = {
    "--allow-dangerously-skip-permissions",
    "--dangerously-skip-permissions",
    "--permission-mode",
}
VERBOSE_ARGUMENT = "--verbose"
NAME: str = "Claude"
IS_GATE_SUPPORTED: bool = True
IS_AUTO_PICKUP_SUPPORTED: bool = True


class ClaudeRateLimit(TypedDict):
    utilization: int | float
    resets_at: str | None


class ClaudeUsageResponse(TypedDict):
    rate_limits_available: bool
    rate_limits: dict[str, ClaudeRateLimit | None]


class ClaudeControlResponse(TypedDict):
    request_id: str
    subtype: str
    response: ClaudeUsageResponse


class ClaudeControlFrame(TypedDict):
    type: str
    response: ClaudeControlResponse


def claude_event_progress(event: object) -> list[str]:
    if not isinstance(event, dict):
        return []
    event_fields = cast("dict[str, object]", event)
    if (
        event_fields.get("type") != "assistant"
        or event_fields.get("parent_tool_use_id") is not None
    ):
        return []
    message = event_fields.get("message")
    content = (
        cast("dict[str, object]", message).get("content") if isinstance(message, dict) else None
    )
    if not isinstance(content, list):
        return []
    progress: list[str] = []
    for item in cast("list[object]", content):
        if not isinstance(item, dict):
            continue
        item_fields = cast("dict[str, object]", item)
        text = item_fields.get("text")
        name = item_fields.get("name")
        if item_fields.get("type") == "text" and isinstance(text, str):
            progress.append(text)
        elif item_fields.get("type") == "tool_use" and isinstance(name, str):
            tool_input = item_fields.get("input")
            input_fields = (
                cast("dict[str, object]", tool_input) if isinstance(tool_input, dict) else {}
            )
            argument = next(
                (
                    input_fields[key]
                    for key in PROGRESS_TOOL_INPUT_KEYS
                    if isinstance(input_fields.get(key), str)
                ),
                None,
            )
            progress.append(common.tool_call_progress(name, argument))
    return progress


def run_claude(
    task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str, str]:
    session_id = task["session_id"]
    is_resuming = common.is_usage_limit_resume(task)
    command = (
        ["claude", "--resume", session_id, "--print", common.USAGE_LIMIT_RESUME_PROMPT]
        if is_resuming
        else ["claude", "--session-id", session_id, "--print", task["prompt"]]
    )
    agent_args = task["agent_args"]
    default_args: list[str] = []
    if VERBOSE_ARGUMENT not in agent_args:
        default_args.append(VERBOSE_ARGUMENT)
    if not common.has_agent_argument(agent_args, NAME_ARGUMENTS):
        default_args += ["--name", task_id]
    worktree = task.get("worktree")
    is_creating_worktree = worktree is not None and not Path(worktree).is_dir()
    if worktree is not None and is_creating_worktree:
        default_args += ["--worktree", Path(worktree).name]
    if not common.has_agent_argument(agent_args, PERMISSION_ARGUMENTS):
        default_args.append("--dangerously-skip-permissions")
    stdout_lines: list[str] = []
    with (
        progress_log.progress_writer(session_id) as record_progress,
        tempfile.TemporaryFile("w+", encoding="utf-8") as stderr_file,
    ):
        claude_process = subprocess.Popen(
            [
                *command,
                "--output-format",
                "stream-json",
                *agent_args,
                *default_args,
            ],
            cwd=ledger.task_directory(task),
            env={**spawn.task_environment(task), BACKGROUND_WAIT_CEILING_VARIABLE: "0"},
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            encoding="utf-8",
            pass_fds=lock_file_descriptors,
        )
        with cast("io.TextIOWrapper", claude_process.stdout) as output:
            try:
                for line in output:
                    stdout_lines.append(line)
                    with suppress(json.JSONDecodeError):
                        for progress in claude_event_progress(cast("object", json.loads(line))):
                            record_progress(progress)
                returncode = claude_process.wait()
            except BaseException:
                claude_process.kill()
                claude_process.wait()
                raise
        stderr_file.seek(0)
        stderr = stderr_file.read()
    sys.stderr.write(stderr)
    if is_creating_worktree:
        task_directories.record_created_worktree_owner(task_id, task)
    return returncode, "".join(stdout_lines), stderr


def run(
    task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
) -> common.RunOutcome:
    returncode, output, stderr = run_claude(task_id, task, lock_file_descriptors)
    result, is_usage_limit = claude_result_and_usage_limit(output)
    return common.RunOutcome(
        returncode,
        result if output else None,
        is_usage_limit,
        stderr,
        None if task.get("no_worktree") else claude_working_directory(output),
    )


def validate_run_arguments(agent_args: list[str]) -> None:
    pass


def conversation_id(task: ledger.Task) -> str | None:
    return task["session_id"]


def transcript_paths(task: ledger.Task) -> list[Path]:
    return list((Path.home() / ".claude" / "projects").glob(f"*/{task['session_id']}.jsonl"))


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
            timeout=common.USAGE_TIMEOUT_SECONDS,
            check=False,
        )
        frames = [
            cast("ClaudeControlFrame", json.loads(line)) for line in result.stdout.splitlines()
        ]
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
            raise SystemExit(common.USAGE_ERROR)
        rate_limits = usage["response"]["rate_limits"]
        five_hour_limit = rate_limits["five_hour"]
        seven_day_limit = rate_limits.get("seven_day")
        exhausted_limits = [
            limit
            for limit in (five_hour_limit, seven_day_limit)
            if limit is not None and limit["utilization"] >= common.FULL_UTILIZATION
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
        raise SystemExit(common.USAGE_ERROR) from error


def reset_time_from_limit(limit: ClaudeRateLimit) -> datetime:
    resets_at = limit["resets_at"]
    if resets_at is None:
        raise SystemExit(common.USAGE_ERROR)
    return common.validated_reset_time(datetime.fromisoformat(resets_at))


def parsed_claude_output(output: str) -> object:
    with suppress(json.JSONDecodeError):
        return cast("object", json.loads(output))
    events: list[object] = []
    for line in output.splitlines():
        with suppress(json.JSONDecodeError):
            events.append(cast("object", json.loads(line)))
    return events


def claude_result_and_usage_limit(output: str) -> tuple[str, bool]:
    parsed_response = parsed_claude_output(output)
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
    parsed_response = parsed_claude_output(output)
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
