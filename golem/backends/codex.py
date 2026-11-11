import json
import os
import queue
import subprocess
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import io
    from collections.abc import Callable

from golem import ledger, progress_log, spawn, task_directories
from golem.backends import common

CODEX_ACCOUNT_ERROR = "golem: Codex account lookup unavailable"
CODEX_SUBSCRIPTION_ERROR = (
    "golem: Codex must be signed in with a ChatGPT subscription; run codex login"
)
CODEX_PERMISSION_ARGUMENTS = {
    "-s",
    "--sandbox",
    "--approve-for-me",
    "--not-so-yolo",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
    "-a",
    "--ask-for-approval",
}
CODEX_CONFIG_ARGUMENTS = {"-c", "--config"}
CODEX_UNSUPPORTED_ARGUMENTS = {
    "--oss",
    "--local-provider",
    "--remote",
    "--remote-auth-token-env",
}
CODEX_PROVIDER_OVERRIDE = 'model_provider="openai"'
CODEX_PROVIDER_ARGUMENTS = ("-c", CODEX_PROVIDER_OVERRIDE)
NAME: str = "Codex"
type AppServerRequests = list[ledger.JsonObject]
type AppServerResponses = dict[int, ledger.JsonObject]


def codex_environment(environment: dict[str, str]) -> dict[str, str]:
    environment = environment.copy()
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODEX_API_KEY", None)
    return environment


def codex_config_key(argument: str) -> str:
    return argument.split("=", 1)[0].strip()


def codex_config_values(agent_args: list[str]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(agent_args):
        argument = agent_args[index]
        if argument in CODEX_CONFIG_ARGUMENTS and index + 1 < len(agent_args):
            values.append(agent_args[index + 1])
            index += 2
            continue
        if argument.startswith("--config="):
            values.append(argument.removeprefix("--config="))
        index += 1
    return values


def has_codex_permission_argument(agent_args: list[str]) -> bool:
    return common.has_agent_argument(agent_args, CODEX_PERMISSION_ARGUMENTS) or any(
        codex_config_key(value) in {"sandbox_mode", "approval_policy"}
        for value in codex_config_values(agent_args)
    )


def reject_unsupported_codex_arguments(agent_args: list[str], *, is_run: bool) -> None:
    unsupported: set[str] = CODEX_UNSUPPORTED_ARGUMENTS | (
        common.WORKTREE_ARGUMENTS if is_run else set()
    )
    for argument in agent_args:
        if argument in unsupported or any(argument.startswith(f"{name}=") for name in unsupported):
            raise SystemExit(f"golem: Codex argument is not supported: {argument}")
    for value in codex_config_values(agent_args):
        if codex_config_key(value) in {"model_provider", "forced_login_method"}:
            raise SystemExit(f"golem: Codex argument is not supported: {value}")


def codex_event_message(event: dict[str, object]) -> str | None:
    message = event.get("message")
    return message if isinstance(message, str) else None


def codex_command(task: ledger.Task) -> tuple[list[str], str]:
    agent_args = task["agent_args"]
    default_args = (
        []
        if has_codex_permission_argument(agent_args)
        else ["--dangerously-bypass-approvals-and-sandbox"]
    )
    command = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        *agent_args,
        *default_args,
        *CODEX_PROVIDER_ARGUMENTS,
    ]
    prompt = task["prompt"]
    if common.is_usage_limit_resume(task):
        thread_id = task.get("thread_id")
        if thread_id is None:
            raise KeyError("thread_id")
        command += ["resume", thread_id, "-"]
        return command, common.USAGE_LIMIT_RESUME_PROMPT
    command.append("-")
    return command, prompt


@dataclass
class CodexEventState:
    last_agent_message: str | None = None
    failed_message: str | None = None
    last_error_message: str | None = None


def codex_item_progress(item: dict[str, object]) -> str | None:
    item_type = item.get("type")
    if item_type == "agent_message":
        text = item.get("text")
        return text if isinstance(text, str) else None
    if item_type == "command_execution":
        return common.tool_call_progress("command", item.get("command"))
    if item_type == "file_change":
        changes = item.get("changes")
        paths = [
            cast("dict[str, object]", change).get("path")
            for change in (cast("list[object]", changes) if isinstance(changes, list) else [])
            if isinstance(change, dict)
        ]
        return common.tool_call_progress(
            "file_change", " ".join(path for path in paths if isinstance(path, str))
        )
    if item_type == "mcp_tool_call":
        return common.tool_call_progress("mcp", item.get("tool"))
    if item_type == "web_search":
        return common.tool_call_progress("web_search", item.get("query"))
    return None


def update_codex_event_state(
    event: dict[str, object],
    task_id: str,
    session_id: str,
    state: CodexEventState,
    record_progress: Callable[[str], None],
) -> None:
    event_type = event.get("type")
    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        if isinstance(thread_id, str):
            common.record_session_field(task_id, session_id, "thread_id", thread_id)
        return
    if event_type == "item.completed":
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_fields = cast("dict[str, object]", item)
        if progress := codex_item_progress(item_fields):
            record_progress(progress)
        text = item_fields.get("text") if item_fields.get("type") == "agent_message" else None
        if isinstance(text, str):
            state.last_agent_message = text
        return
    if event_type == "turn.failed":
        error = event.get("error")
        if isinstance(error, dict):
            state.failed_message = codex_event_message(cast("dict[str, object]", error))
        return
    if event_type == "error":
        state.last_error_message = codex_event_message(event)


def read_codex_events(
    process: subprocess.Popen[str],
    task_id: str,
    session_id: str,
    record_progress: Callable[[str], None],
) -> CodexEventState:
    state = CodexEventState()
    output = cast("io.TextIOWrapper", process.stdout)
    try:
        for line in output:
            try:
                event = cast("object", json.loads(line))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                update_codex_event_state(
                    cast("dict[str, object]", event), task_id, session_id, state, record_progress
                )
    finally:
        output.close()
    return state


def run_codex(
    task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
) -> tuple[int, str | None, bool]:
    session_id = task["session_id"]
    with progress_log.progress_writer(session_id) as record_progress:
        try:
            require_codex_subscription(spawn.task_environment(task))
        except SystemExit as error:
            return 1, str(error), False
        if error := task_directories.prepare_worktree(task_id, task):
            return 1, error, False
        if common.is_usage_limit_resume(task) and not isinstance(task.get("thread_id"), str):
            return 1, "golem: task has no Codex conversation", False
        command, prompt = codex_command(task)
        process = subprocess.Popen(
            command,
            cwd=ledger.task_directory(task),
            env=codex_environment(spawn.task_environment(task)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            encoding="utf-8",
            pass_fds=lock_file_descriptors,
        )
        standard_input = cast("io.TextIOWrapper", process.stdin)
        try:
            with suppress(BrokenPipeError):
                standard_input.write(prompt)
        finally:
            with suppress(BrokenPipeError):
                standard_input.close()
        try:
            event_state = read_codex_events(process, task_id, session_id, record_progress)
            returncode = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
    result = (
        event_state.failed_message
        or event_state.last_agent_message
        or event_state.last_error_message
    )
    is_usage_limit = bool(
        event_state.failed_message
        and event_state.failed_message.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").startswith(
            "You've hit your usage limit"
        )
    )
    return returncode, result, is_usage_limit


def run(
    task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
) -> common.RunOutcome:
    returncode, result, is_usage_limit = run_codex(task_id, task, lock_file_descriptors)
    return common.RunOutcome(returncode, result or None, is_usage_limit)


def validate_run_arguments(agent_args: list[str]) -> None:
    reject_unsupported_codex_arguments(agent_args, is_run=True)
    require_codex_subscription(os.environ.copy())


def validate_resume_arguments(agent_args: list[str]) -> None:
    reject_unsupported_codex_arguments(agent_args, is_run=False)
    require_codex_subscription(os.environ.copy())


def conversation_id(task: ledger.Task) -> str | None:
    thread_id = task.get("thread_id")
    return thread_id if isinstance(thread_id, str) else None


def session_transcript_paths(task: ledger.Task, thread_id: str) -> list[Path]:
    codex_home = Path(task.get("environment", {}).get("CODEX_HOME", Path.home() / ".codex"))
    return list((codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"))


def transcript_paths(task: ledger.Task) -> list[Path]:
    thread_id = conversation_id(task)
    return [] if thread_id is None else session_transcript_paths(task, thread_id)


def codex_app_server_requests(*, has_rate_limits: bool) -> AppServerRequests:
    requests: AppServerRequests = [
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "golem", "version": "0.1.0"}},
        },
        {"method": "initialized"},
        {"id": 2, "method": "account/read", "params": {}},
    ]
    if has_rate_limits:
        requests.append({"id": 3, "method": "account/rateLimits/read"})
    return requests


def read_codex_app_server_output(
    output: io.TextIOWrapper, response_lines: queue.Queue[str | None]
) -> None:
    for line in output:
        response_lines.put(line)
    response_lines.put(None)


def codex_app_server_response(
    response_lines: queue.Queue[str | None], deadline: float
) -> ledger.JsonObject:
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise subprocess.TimeoutExpired("codex app-server", common.USAGE_TIMEOUT_SECONDS)
    try:
        line = response_lines.get(timeout=timeout)
    except queue.Empty as error:
        raise subprocess.TimeoutExpired("codex app-server", common.USAGE_TIMEOUT_SECONDS) from error
    if not line:
        raise OSError("Codex app-server exited before responding")
    response = cast("object", json.loads(line))
    if not isinstance(response, dict):
        raise TypeError("invalid Codex app-server response")
    return cast("ledger.JsonObject", response)


def validate_codex_app_server(
    process: subprocess.Popen[str], responses: AppServerResponses
) -> None:
    if process.returncode:
        raise OSError("Codex app-server failed")
    if any("error" in response or "result" not in response for response in responses.values()):
        raise ValueError("Codex app-server returned an error")


def codex_app_server_responses(
    environment: dict[str, str], *, has_rate_limits: bool
) -> AppServerResponses:
    process: subprocess.Popen[str] | None = None
    response_reader: threading.Thread | None = None
    standard_output: io.TextIOWrapper | None = None
    try:
        process = subprocess.Popen(
            ["codex", "app-server", *CODEX_PROVIDER_ARGUMENTS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=codex_environment(environment),
            encoding="utf-8",
        )
        standard_input = cast("io.TextIOWrapper", process.stdin)
        standard_output = cast("io.TextIOWrapper", process.stdout)
        response_lines: queue.Queue[str | None] = queue.Queue()
        response_reader = threading.Thread(
            target=read_codex_app_server_output,
            args=(standard_output, response_lines),
            daemon=True,
        )
        response_reader.start()
        requests = codex_app_server_requests(has_rate_limits=has_rate_limits)
        standard_input.write("".join(json.dumps(request) + "\n" for request in requests))
        standard_input.flush()
        responses: AppServerResponses = {}
        required_ids = {request["id"] for request in requests if isinstance(request.get("id"), int)}
        deadline = time.monotonic() + common.USAGE_TIMEOUT_SECONDS
        while required_ids - responses.keys():
            response = codex_app_server_response(response_lines, deadline)
            response_id = response.get("id")
            if isinstance(response_id, int):
                responses[response_id] = response
        standard_input.close()
        process.wait(timeout=max(0, deadline - time.monotonic()))
        validate_codex_app_server(process, responses)
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
    ) as error:
        raise SystemExit(CODEX_ACCOUNT_ERROR) from error
    else:
        return responses
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            with suppress(BrokenPipeError):
                cast("io.TextIOWrapper", process.stdin).close()
        if response_reader is not None:
            response_reader.join()
        if standard_output is not None:
            standard_output.close()


def validate_codex_subscription(responses: AppServerResponses) -> None:
    account_result = responses[2]["result"]
    if not isinstance(account_result, dict):
        raise SystemExit(CODEX_ACCOUNT_ERROR)
    account_result_fields = cast("dict[str, object]", account_result)
    account = account_result_fields.get("account")
    account_fields = cast("dict[str, object]", account) if isinstance(account, dict) else None
    if (
        account_fields is None
        or account_fields.get("type") != "chatgpt"
        or account_result_fields.get("requiresOpenaiAuth") is not True
    ):
        raise SystemExit(CODEX_SUBSCRIPTION_ERROR)


def require_codex_subscription(environment: dict[str, str]) -> None:
    validate_codex_subscription(codex_app_server_responses(environment, has_rate_limits=False))


def codex_rate_limits() -> ledger.JsonObject:
    responses = codex_app_server_responses(os.environ.copy(), has_rate_limits=True)
    validate_codex_subscription(responses)
    rate_limits_result = responses[3]["result"]
    rate_limits_fields = (
        cast("dict[str, object]", rate_limits_result)
        if isinstance(rate_limits_result, dict)
        else None
    )
    if rate_limits_fields is None or not isinstance(rate_limits_fields.get("rateLimits"), dict):
        raise SystemExit(CODEX_ACCOUNT_ERROR)
    return cast("ledger.JsonObject", rate_limits_fields["rateLimits"])


def codex_reset_time_from_limit(limit: ledger.JsonObject) -> datetime:
    resets_at = limit.get("resetsAt")
    if not isinstance(resets_at, int | float):
        raise SystemExit(common.USAGE_ERROR)
    return common.validated_reset_time(datetime.fromtimestamp(resets_at, UTC))


def required_codex_primary_limit(rate_limits: ledger.JsonObject) -> ledger.JsonObject | None:
    if "primary" not in rate_limits:
        raise SystemExit(common.USAGE_ERROR)
    primary = rate_limits["primary"]
    return cast("ledger.JsonObject", primary) if isinstance(primary, dict) else None


def codex_active_reset_time(primary: ledger.JsonObject) -> datetime | None:
    used_percent = primary.get("usedPercent")
    if primary.get("resetsAt") is None or used_percent == 0:
        return None
    if not isinstance(used_percent, int | float):
        raise SystemExit(common.USAGE_ERROR)
    return codex_reset_time_from_limit(primary)


def next_codex_reset_time() -> datetime | None:
    try:
        rate_limits = codex_rate_limits()
        primary = required_codex_primary_limit(rate_limits)
        secondary = rate_limits.get("secondary")
        limits = [
            cast("ledger.JsonObject", limit)
            for limit in (primary, secondary)
            if isinstance(limit, dict)
        ]
        exhausted_limits = [
            limit
            for limit in limits
            if isinstance(used_percent := limit.get("usedPercent"), int | float)
            and used_percent >= common.FULL_UTILIZATION
        ]
        if exhausted_limits:
            return max(codex_reset_time_from_limit(limit) for limit in exhausted_limits)
        if primary is None:
            return None
        return codex_active_reset_time(primary)
    except SystemExit as error:
        if str(error) == CODEX_SUBSCRIPTION_ERROR:
            raise
        raise SystemExit(common.USAGE_ERROR) from error


def next_reset_time() -> datetime | None:
    return next_codex_reset_time()


def open_usage_window(environment: dict[str, str]) -> str | None:
    return common.usage_window_request_error(
        [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--disable",
            "plugins",
            "--disable",
            "shell_tool",
            "-m",
            "gpt-6-luna",
            "-c",
            "model_reasoning_effort=low",
            *CODEX_PROVIDER_ARGUMENTS,
            common.USAGE_WINDOW_PROMPT,
        ],
        codex_environment(environment),
    )
