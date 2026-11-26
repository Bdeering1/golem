import json
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import io
    from collections.abc import Callable
    from pathlib import Path

from golem import accounts, ledger, progress_log, t3_code, task_processes
from golem.backends import claude, codex, common
from golem.worktrees import task_directories

NAME: str = "T3 Code"
PROVIDER_ARGUMENT = "--provider"
MODEL_ARGUMENT = "--model"
EFFORT_ARGUMENT = "--effort"
PERMISSION_MODE_ARGUMENT = "--permission-mode"
SUPPORTED_ARGUMENTS = {PROVIDER_ARGUMENT, MODEL_ARGUMENT, EFFORT_ARGUMENT, PERMISSION_MODE_ARGUMENT}
PROJECTION_POLL_SECONDS = 2
TOKEN_REISSUE_SECONDS = 60 * 60
EXIT_WAITING_FOR_ANSWER = 2
WAITING_FOR_ANSWER_REASON = "waiting for an answer in T3 Code"
TERMINAL_ITEM_STATUSES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_RUN_STATUSES = {*TERMINAL_ITEM_STATUSES, "rolled_back"}
COMPLETED_STATUS = "completed"
SUBAGENT_ITEM_TYPE = "subagent"
SESSION_ENDED_STATUSES = {"stopped", "error"}
SESSION_STOPPED_REASON = "T3 Code stopped the session before its subagents finished"
USAGE_LIMIT_FAILURE_CLASS = "usage_limit"


@dataclass(frozen=True)
class ThreadArguments:
    provider: str
    model: str | None
    effort: str | None


def thread_arguments(agent_args: list[str]) -> ThreadArguments:
    values: dict[str, str] = {}
    remaining_args = iter(agent_args)
    for argument in remaining_args:
        name, separator, value = argument.partition("=")
        if name not in SUPPORTED_ARGUMENTS:
            raise SystemExit(f"golem: T3 Code argument is not supported: {argument}")
        if not separator:
            value = next(remaining_args, "")
        if not value or value.startswith("-"):
            raise SystemExit(f"golem: {name} needs a value")
        values[name] = value
    provider = values.get(PROVIDER_ARGUMENT, ledger.BACKEND_CLAUDE)
    if provider not in t3_code.INSTANCE_IDS:
        raise SystemExit(f"golem: {PROVIDER_ARGUMENT} must be claude or codex, got: {provider}")
    return ThreadArguments(provider, values.get(MODEL_ARGUMENT), values.get(EFFORT_ARGUMENT))


def provider_id(agent_args: list[str]) -> str:
    return thread_arguments(agent_args).provider


def validate_run_arguments(agent_args: list[str]) -> None:
    arguments = thread_arguments(agent_args)
    if arguments.effort is None:
        return
    driver = t3_code.INSTANCE_IDS[arguments.provider]
    model = arguments.model or t3_code.default_model(driver)
    levels = t3_code.effort_levels(driver, model)
    if levels is None or arguments.effort in levels:
        return
    if not levels:
        raise SystemExit(f"golem: {model} does not offer {EFFORT_ARGUMENT}")
    raise SystemExit(
        f"golem: {model} does not offer {EFFORT_ARGUMENT} {arguments.effort}; "
        f"choose from: {', '.join(levels)}"
    )


def conversation_id(task: ledger.Task) -> str | None:
    return f"golem:{ledger.BACKEND_T3}:{task['session_id']}"


def transcript_paths(task: ledger.Task) -> list[Path]:
    native_session_id = task.get("native_session_id")
    if native_session_id is None:
        return []
    if provider_id(task["agent_args"]) == ledger.BACKEND_CLAUDE:
        return claude.session_transcript_paths(native_session_id)
    return codex.session_transcript_paths(task, native_session_id)


def create_thread(
    task_id: str,
    task: ledger.Task,
    thread_id: str,
    origin: str,
    token: str,
    instance: t3_code.ClaudeInstance | None,
) -> None:
    arguments = thread_arguments(task["agent_args"])
    driver = t3_code.INSTANCE_IDS[arguments.provider]
    t3_code.dispatch_command(
        origin,
        token,
        t3_code.thread_create_command(
            origin,
            token,
            task,
            thread_id,
            t3_code.thread_instance_id(driver, instance),
            arguments.model or t3_code.default_model(driver),
            task_id=task_id,
            model_options=t3_code.effort_options(driver, arguments.effort),
        ),
    )
    common.record_session_field(task_id, task["session_id"], "t3_code_thread_id", thread_id)


def json_objects(value: object) -> list[ledger.JsonObject]:
    if not isinstance(value, list):
        return []
    return [
        cast("ledger.JsonObject", item)
        for item in cast("list[object]", value)
        if isinstance(item, dict)
    ]


def turn_item_progress(item: ledger.JsonObject) -> str | None:
    item_type = item.get("type")
    if item_type == "assistant_message":
        text = item.get("text")
        return text if isinstance(text, str) and item.get("streaming") is not True else None
    if item_type == "command_execution":
        return common.tool_call_progress("command", item.get("input"))
    if item_type == "file_change":
        paths = [change.get("path") for change in json_objects(item.get("changes"))]
        changed_paths = " ".join(path for path in paths if isinstance(path, str))
        return common.tool_call_progress("file_change", changed_paths or item.get("fileName"))
    if item_type == "web_search":
        patterns = item.get("patterns")
        first_pattern = (
            cast("list[object]", patterns)[0] if isinstance(patterns, list) and patterns else None
        )
        return common.tool_call_progress("web_search", first_pattern)
    if item_type == "file_search":
        return common.tool_call_progress("file_search", item.get("pattern"))
    tool_name = item.get("toolName")
    if item_type == "dynamic_tool" and isinstance(tool_name, str):
        return common.tool_call_progress(tool_name, item.get("input"))
    return None


def projection_native_session_id(projection: ledger.JsonObject) -> str | None:
    thread = cast("ledger.JsonObject", projection["thread"])
    provider_threads = [
        provider_thread
        for provider_thread in json_objects(projection.get("providerThreads"))
        if isinstance(provider_thread.get("nativeThreadRef"), dict)
    ]
    active_thread = next(
        (
            provider_thread
            for provider_thread in provider_threads
            if provider_thread.get("id") == thread.get("activeProviderThreadId")
        ),
        next(iter(provider_threads), None),
    )
    if active_thread is None:
        return None
    native_id = cast("ledger.JsonObject", active_thread["nativeThreadRef"]).get("nativeId")
    return native_id if isinstance(native_id, str) else None


@dataclass
class ThreadWatcher:
    task_id: str
    session_id: str
    thread_id: str
    origin: str
    record_progress: Callable[[str], None]
    token: str
    token_issued_at: float
    native_session_id: str | None = None
    previous_item_ids: set[str] = field(default_factory=set)
    run_items: dict[str, ledger.JsonObject] = field(default_factory=dict)
    recorded_item_ids: set[str] = field(default_factory=set)
    run_id: str | None = None

    def current_token(self) -> str:
        now = time.monotonic()
        if now - self.token_issued_at >= TOKEN_REISSUE_SECONDS:
            self.token = t3_code.issue_token()
            self.token_issued_at = now
        return self.token

    def projection(self) -> ledger.JsonObject:
        return t3_code.thread_projection(self.origin, self.current_token(), self.thread_id)

    def skip_previous_items(self) -> None:
        with suppress(*t3_code.T3_CODE_ERRORS):
            items = json_objects(self.projection().get("turnItems"))
            self.previous_item_ids = {cast("str", item["id"]) for item in items}

    def poll(self) -> ledger.JsonObject | None:
        with suppress(*t3_code.T3_CODE_ERRORS):
            projection = self.projection()
            for item in json_objects(projection.get("turnItems")):
                item_id = cast("str", item["id"])
                if item_id in self.previous_item_ids:
                    continue
                self.run_items[item_id] = item
                if item_id in self.recorded_item_ids or item.get("status") not in (
                    TERMINAL_ITEM_STATUSES
                ):
                    continue
                self.recorded_item_ids.add(item_id)
                if progress := turn_item_progress(item):
                    self.record_progress(progress)
            native_session_id = projection_native_session_id(projection)
            if native_session_id is not None and native_session_id != self.native_session_id:
                common.record_session_field(
                    self.task_id, self.session_id, "native_session_id", native_session_id
                )
                self.native_session_id = native_session_id
            return projection
        return None

    def is_task_running(self) -> bool:
        task = ledger.load_tasks().get(self.task_id)
        return (
            task is not None
            and task["session_id"] == self.session_id
            and task["status"] == ledger.STATUS_RUNNING
        )

    def last_failed_error(self) -> ledger.JsonObject:
        failed_errors = [
            item
            for item in self.run_items.values()
            if item.get("type") == "error"
            and item.get("status") == "failed"
            and self.run_id in (None, item.get("runId"))
        ]
        return failed_errors[-1] if failed_errors else {}

    def last_failure(self) -> ledger.JsonObject:
        failure = self.last_failed_error().get("failure")
        return cast("ledger.JsonObject", failure) if isinstance(failure, dict) else {}

    def disable_auto_resume(self) -> None:
        # golem resumes the thread itself, so T3 Code's auto-resume would send a second turn.
        limit_recovery = {
            "runId": self.last_failed_error().get("runId"),
            "resetAt": self.last_failure().get("resetAt"),
            "autoResume": False,
        }
        with suppress(*t3_code.T3_CODE_ERRORS):
            t3_code.dispatch_command(
                self.origin,
                self.current_token(),
                {
                    "type": "thread.metadata.update",
                    "commandId": str(uuid.uuid4()),
                    "threadId": self.thread_id,
                    "limitRecovery": limit_recovery,
                },
            )


def send_message(
    task: ledger.Task,
    thread_id: str,
    watcher: ThreadWatcher,
    lock_file_descriptors: tuple[int, ...],
) -> tuple[int, str, str]:
    message = (
        common.USAGE_LIMIT_RESUME_PROMPT if common.is_usage_limit_resume(task) else task["prompt"]
    )
    # Files, not pipes: a long reply would fill a pipe while golem polls the projection.
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stdout_file,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stderr_file,
    ):
        process = subprocess.Popen(
            [str(t3_code.t3_cli_path()), "thread", "send", thread_id, "-", "--wait", "--json"],
            cwd=ledger.task_directory(task),
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            encoding="utf-8",
            pass_fds=lock_file_descriptors,
        )
        with task_processes.recorded_process(
            watcher.task_id, task["session_id"], task_processes.FIELD_AGENT_PID, process
        ):
            try:
                standard_input = cast("io.TextIOWrapper", process.stdin)
                with suppress(BrokenPipeError):
                    standard_input.write(message)
                with suppress(BrokenPipeError):
                    standard_input.close()
                while True:
                    try:
                        returncode = process.wait(timeout=PROJECTION_POLL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        watcher.poll()
            except BaseException:
                process.kill()
                process.wait()
                raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        return returncode, stdout_file.read(), stderr_file.read()


def parsed_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def send_output(stdout: str) -> ledger.JsonObject:
    try:
        parsed_output = cast("object", json.loads(stdout))
    except json.JSONDecodeError:
        return {}
    return cast("ledger.JsonObject", parsed_output) if isinstance(parsed_output, dict) else {}


def waiting_outcome(reply_text: str | None) -> common.RunOutcome:
    return common.RunOutcome(
        EXIT_WAITING_FOR_ANSWER,
        reply_text,
        is_usage_limit=False,
        stop_reason=WAITING_FOR_ANSWER_REASON,
    )


def failed_run_outcome(
    returncode: int, failure: ledger.JsonObject, *messages: object
) -> common.RunOutcome:
    result = next((text for text in messages if isinstance(text, str) and text), None)
    is_usage_limit = failure.get("class") == USAGE_LIMIT_FAILURE_CLASS
    return common.RunOutcome(
        returncode,
        result,
        is_usage_limit,
        reset_time=parsed_timestamp(failure.get("resetAt")) if is_usage_limit else None,
    )


def send_outcome(
    returncode: int, output: ledger.JsonObject, stdout: str, stderr: str, watcher: ThreadWatcher
) -> common.RunOutcome:
    reply = output.get("reply")
    reply_text = reply if isinstance(reply, str) else None
    if returncode == 0:
        return common.RunOutcome(returncode, reply_text, is_usage_limit=False)
    if returncode == EXIT_WAITING_FOR_ANSWER:
        return waiting_outcome(reply_text)
    failure = watcher.last_failure()
    return failed_run_outcome(
        returncode,
        failure,
        output.get("error"),
        failure.get("message"),
        stderr.strip(),
        stdout.strip(),
    )


def has_active_run(run: ledger.JsonObject | None) -> bool:
    return run is not None and run.get("status") not in TERMINAL_RUN_STATUSES


def has_unfinished_subagent_work(
    projection: ledger.JsonObject, run: ledger.JsonObject | None
) -> bool:
    if run is None:
        return False
    subagents = [
        item
        for item in json_objects(projection.get("turnItems"))
        if item.get("type") == SUBAGENT_ITEM_TYPE
    ]
    if any(item.get("status") not in TERMINAL_ITEM_STATUSES for item in subagents):
        return True
    run_ended_at = parsed_timestamp(run.get("completedAt"))
    if run_ended_at is None:
        return False
    subagent_ended_times = [
        parsed_timestamp(item.get("completedAt") or item.get("updatedAt")) for item in subagents
    ]
    return any(
        ended_at is not None and ended_at > run_ended_at for ended_at in subagent_ended_times
    )


def is_session_ended(projection: ledger.JsonObject) -> bool:
    sessions = json_objects(projection.get("providerSessions"))
    return all(session.get("status") in SESSION_ENDED_STATUSES for session in sessions)


def is_request_pending(projection: ledger.JsonObject) -> bool:
    return any(
        request.get("status") == "pending"
        for request in json_objects(projection.get("runtimeRequests"))
    )


def last_reply(projection: ledger.JsonObject, run: ledger.JsonObject | None) -> str | None:
    if run is None:
        return None
    replies = [
        turn_item_progress(item)
        for item in json_objects(projection.get("turnItems"))
        if item.get("type") == "assistant_message" and item.get("runId") == run.get("id")
    ]
    return next((reply for reply in reversed(replies) if reply is not None), None)


def latest_run_outcome(
    outcome: common.RunOutcome,
    projection: ledger.JsonObject,
    run: ledger.JsonObject | None,
    watcher: ThreadWatcher,
) -> common.RunOutcome:
    if run is None or watcher.run_id is None or run.get("id") == watcher.run_id:
        return outcome
    watcher.run_id = cast("str | None", run.get("id"))
    if run.get("status") == COMPLETED_STATUS:
        return common.RunOutcome(
            0, last_reply(projection, run) or outcome.result, is_usage_limit=False
        )
    failure = watcher.last_failure()
    return failed_run_outcome(1, failure, failure.get("message"), run.get("status"))


def wait_for_quiet_thread(
    outcome: common.RunOutcome,
    watcher: ThreadWatcher,
    projection: ledger.JsonObject | None,
) -> common.RunOutcome:
    # T3 Code runs a subagent's follow-up in a new run, which `thread send --wait` doesn't follow.
    while True:
        if projection is not None:
            run = t3_code.latest_run_not_cancelled_before_start(projection)
            if is_request_pending(projection):
                return waiting_outcome(last_reply(projection, run))
            is_active = has_active_run(run)
            if not is_active and not has_unfinished_subagent_work(projection, run):
                return latest_run_outcome(outcome, projection, run, watcher)
            if not is_active and is_session_ended(projection):
                return failed_outcome(SESSION_STOPPED_REASON)
        if not t3_code.is_running():
            return failed_outcome(SESSION_STOPPED_REASON)
        if not watcher.is_task_running():
            return outcome
        time.sleep(PROJECTION_POLL_SECONDS)
        projection = watcher.poll()


def failed_outcome(message: str) -> common.RunOutcome:
    return common.RunOutcome(1, message, is_usage_limit=False)


def run(
    task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
) -> common.RunOutcome:
    session_id = task["session_id"]
    thread_id = cast("str", conversation_id(task))
    with progress_log.progress_writer(session_id) as record_progress:
        if error := task_directories.prepare_worktree(task_id, task):
            return failed_outcome(error)
        cli_path = t3_code.t3_cli_path()
        if not cli_path.exists():
            return failed_outcome(f"golem: T3 Code is not installed: {cli_path} not found")
        instance = accounts.t3_claude_instance(task.get("account"))
        try:
            t3_code.ensure_app_running()
            origin = t3_code.server_origin()
            token = t3_code.issue_token()
            if instance is not None:
                t3_code.upsert_claude_instance(origin, token, instance)
            if not common.is_usage_limit_resume(task) and not t3_code.is_thread_present(thread_id):
                create_thread(task_id, task, thread_id, origin, token, instance)
        except t3_code.T3_CODE_ERRORS as error:
            return failed_outcome(
                f"golem: T3 Code {t3_code.version()} could not run thread {thread_id}: {error}"
            )
        watcher = ThreadWatcher(
            task_id,
            session_id,
            thread_id,
            origin,
            record_progress,
            native_session_id=task.get("native_session_id"),
            token=token,
            token_issued_at=time.monotonic(),
        )
        watcher.skip_previous_items()
        returncode, stdout, stderr = send_message(task, thread_id, watcher, lock_file_descriptors)
        output = send_output(stdout)
        run_id = output.get("runId")
        watcher.run_id = run_id if isinstance(run_id, str) else None
        first_projection = watcher.poll()
        outcome = send_outcome(returncode, output, stdout, stderr, watcher)
        is_thread_readable = first_projection is not None or t3_code.is_running()
        if is_thread_readable and returncode != EXIT_WAITING_FOR_ANSWER:
            outcome = wait_for_quiet_thread(outcome, watcher, first_projection)
    if outcome.is_usage_limit:
        watcher.disable_auto_resume()
    return outcome
