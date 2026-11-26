import http.client
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib import parse

from websockets.exceptions import WebSocketException
from websockets.sync import client as websocket_client

from golem import ledger
from golem.worktrees import git

if TYPE_CHECKING:
    from collections.abc import Iterable

APP_BUNDLE_ID = "com.t3tools.t3code"
APP_START_TIMEOUT_SECONDS = 60
APP_START_POLL_SECONDS = 0.5
NETWORK_TIMEOUT_SECONDS = 30
INSTANCE_IDS = {ledger.BACKEND_CLAUDE: "claudeAgent", ledger.BACKEND_CODEX: "codex"}
DEFAULT_MODELS = {"claudeAgent": "claude-opus-5-5", "codex": "gpt-6-astra"}
THREAD_MISSING_MESSAGE = "is no longer available"
EFFORT_OPTION_IDS = {"claudeAgent": "effort", "codex": "reasoningEffort"}
SETTLE_RACE_MESSAGE = "The thread received another run before settlement."
CANCELLED_RUN_STATUS = "cancelled"
SETTLE_BLOCKING_FIELDS = (
    "pinnedAt",
    "snoozedUntil",
    "snoozedAt",
    "archivedAt",
    "deletedAt",
    "settledAt",
    "settledOverride",
)


class T3CodeError(Exception):
    pass


@dataclass(frozen=True)
class ClaudeInstance:
    instance_id: str
    display_name: str
    home_path: str


CONNECTION_ERRORS = (OSError, http.client.HTTPException, WebSocketException)
T3_CODE_ERRORS = (
    T3CodeError,
    *CONNECTION_ERRORS,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
)


def t3_home() -> Path:
    home = os.environ.get("T3CODE_HOME")
    return Path(home) if home else Path.home() / ".t3"


def t3_cli_path() -> Path:
    return t3_home() / "bin" / "t3"


def server_runtime_path() -> Path:
    return t3_home() / "userdata" / "server-runtime.json"


def task_thread_id(task: ledger.Task, conversation_id: str) -> str:
    if task["backend"] == ledger.BACKEND_T3:
        return conversation_id
    return f"golem:{task['backend']}:{conversation_id}"


def recorded_thread_ids(tasks: Iterable[ledger.Task]) -> list[str]:
    return [task["t3_code_thread_id"] for task in tasks if "t3_code_thread_id" in task]


def run_t3(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(t3_cli_path()), *arguments],
        capture_output=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=False,
    )


def t3_output(*arguments: str) -> str:
    result = run_t3(*arguments)
    if result.returncode:
        raise T3CodeError(f"t3 {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def server_origin() -> str:
    return cast("str", ledger.load_json(server_runtime_path())["origin"])


def issue_token() -> str:
    return t3_output("auth", "session", "issue", "--token-only", "--ttl", "2h")


def fetch_projects(origin: str, token: str) -> list[ledger.JsonObject]:
    connection = http.client.HTTPConnection(
        parse.urlsplit(origin).netloc, timeout=NETWORK_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", "/api/projects", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        if response.status != http.HTTPStatus.OK:
            raise T3CodeError(f"GET /api/projects returned HTTP {response.status}")
        snapshot = cast("ledger.JsonObject", json.load(response))
    finally:
        connection.close()
    return cast("list[ledger.JsonObject]", snapshot["projects"])


def rpc_exit(origin: str, token: str, tag: str, payload: ledger.JsonObject) -> ledger.JsonObject:
    host = parse.urlsplit(origin).netloc
    with websocket_client.connect(
        f"ws://{host}/ws?orchestrationProtocol=2",
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=NETWORK_TIMEOUT_SECONDS,
    ) as connection:
        connection.send(
            json.dumps(
                {
                    "_tag": "Request",
                    "id": "1",
                    "tag": tag,
                    "headers": [],
                    "payload": payload,
                }
            )
        )
        while True:
            message = cast(
                "ledger.JsonObject", json.loads(connection.recv(timeout=NETWORK_TIMEOUT_SECONDS))
            )
            if message.get("_tag") == "Exit":
                return cast("ledger.JsonObject", message["exit"])


def error_message(value: object) -> str | None:
    if isinstance(value, list):
        children = cast("list[object]", value)
    elif isinstance(value, dict):
        fields = cast("ledger.JsonObject", value)
        message = fields.get("message")
        if isinstance(message, str):
            return message
        children = list(fields.values())
    else:
        return None
    return next((message for child in children if (message := error_message(child))), None)


def dispatch_command_exit(origin: str, token: str, command: ledger.JsonObject) -> ledger.JsonObject:
    return rpc_exit(origin, token, "orchestration.dispatchCommand", command)


def successful_value(rpc_exit_value: ledger.JsonObject) -> object:
    if rpc_exit_value.get("_tag") != "Success":
        raise T3CodeError(error_message(rpc_exit_value) or json.dumps(rpc_exit_value))
    return rpc_exit_value.get("value")


def dispatch_command(origin: str, token: str, command: ledger.JsonObject) -> None:
    successful_value(dispatch_command_exit(origin, token, command))


def update_settings_exit(origin: str, token: str, mutation: ledger.JsonObject) -> ledger.JsonObject:
    return rpc_exit(
        origin,
        token,
        "server.updateSettings",
        {"patch": {}, "providerInstanceMutation": mutation},
    )


def upsert_claude_instance(origin: str, token: str, instance: ClaudeInstance) -> None:
    mutation: ledger.JsonObject = {
        "operation": "upsert",
        "instanceId": instance.instance_id,
        "instance": {
            "driver": INSTANCE_IDS[ledger.BACKEND_CLAUDE],
            "displayName": instance.display_name,
            "config": {"homePath": instance.home_path},
        },
    }
    successful_value(update_settings_exit(origin, token, mutation))


def thread_instance_id(driver: str, instance: ClaudeInstance | None) -> str:
    return driver if instance is None else instance.instance_id


def thread_projection(origin: str, token: str, thread_id: str) -> ledger.JsonObject:
    projection_exit = rpc_exit(
        origin, token, "orchestration.getThreadProjection", {"threadId": thread_id}
    )
    return cast("ledger.JsonObject", successful_value(projection_exit))


def thread_state(origin: str, token: str, thread_id: str) -> ledger.ThreadState | None:
    try:
        projected_thread = thread_projection(origin, token, thread_id).get("thread")
    except T3_CODE_ERRORS:
        projected_thread = None
    if not isinstance(projected_thread, dict):
        return ledger.THREAD_STATE_DELETED if is_thread_missing(thread_id) else None
    thread = cast("ledger.JsonObject", projected_thread)
    if thread.get("deletedAt") is not None:
        return ledger.THREAD_STATE_DELETED
    if thread.get("archivedAt") is not None:
        return ledger.THREAD_STATE_ARCHIVED
    if thread.get("settledOverride") == "settled" or thread.get("settledAt") is not None:
        return ledger.THREAD_STATE_SETTLED
    return ledger.THREAD_STATE_UNSETTLED


def latest_run(projection: ledger.JsonObject) -> ledger.JsonObject | None:
    runs = cast("list[ledger.JsonObject]", projection.get("runs", []))
    return max(runs, key=lambda run: cast("int", run["ordinal"]), default=None)


def latest_run_not_cancelled_before_start(
    projection: ledger.JsonObject, kept_run_id: str | None = None
) -> ledger.JsonObject | None:
    # T3 Code can cancel a queued run before it starts while an earlier run is still active.
    # The kept run counts even then, so a send cancelled before it starts still fails its task.
    runs = [
        run
        for run in cast("list[ledger.JsonObject]", projection.get("runs", []))
        if run.get("id") == kept_run_id
        or run.get("status") != CANCELLED_RUN_STATUS
        or run.get("startedAt") is not None
    ]
    return max(runs, key=lambda run: cast("int", run["ordinal"]), default=None)


def is_thread_settleable(
    thread: ledger.JsonObject, run: ledger.JsonObject | None, finished_at: str
) -> bool:
    if any(thread.get(field) is not None for field in SETTLE_BLOCKING_FIELDS):
        return False
    if run is None:
        return True
    started_at = run.get("startedAt")
    return isinstance(started_at, str) and datetime.fromisoformat(
        started_at
    ) <= datetime.fromisoformat(finished_at)


def settle_idle_thread(
    origin: str, token: str, thread_id: str, projection: ledger.JsonObject, finished_at: str
) -> None:
    thread = projection.get("thread")
    if not isinstance(thread, dict):
        if is_thread_missing(thread_id):
            return
        raise T3CodeError("the thread projection has no thread")
    run = latest_run_not_cancelled_before_start(projection)
    if not is_thread_settleable(cast("ledger.JsonObject", thread), run, finished_at):
        return
    command: ledger.JsonObject = {
        "type": "thread.settle",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
    }
    # T3 Code checks this against its highest-ordinal run, including cancelled ones.
    if (latest := latest_run(projection)) is not None:
        command["expectedLatestRunId"] = latest["id"]
    try:
        dispatch_command(origin, token, command)
    except T3CodeError as error:
        if SETTLE_RACE_MESSAGE not in str(error):
            raise


def wait_for_server() -> None:
    deadline = time.monotonic() + APP_START_TIMEOUT_SECONDS
    while not server_runtime_path().exists():
        if time.monotonic() >= deadline:
            raise T3CodeError(f"T3 Code did not start within {APP_START_TIMEOUT_SECONDS}s")
        time.sleep(APP_START_POLL_SECONDS)


def ensure_app_running() -> None:
    if is_running():
        return
    if subprocess.run(["open", "-b", APP_BUNDLE_ID], check=False).returncode:
        raise T3CodeError(f"could not start {APP_BUNDLE_ID}")
    wait_for_server()


def thread_show_result(thread_id: str) -> subprocess.CompletedProcess[str]:
    return run_t3("thread", "show", thread_id, "--json")


def is_thread_present(thread_id: str) -> bool:
    return thread_show_result(thread_id).returncode == 0


def is_thread_missing(thread_id: str) -> bool:
    result = thread_show_result(thread_id)
    return result.returncode != 0 and THREAD_MISSING_MESSAGE in result.stderr


def project_root(task: ledger.Task) -> str:
    common_directory = git.git_common_directory(task["cwd"])
    return str(Path(common_directory).parent) if common_directory is not None else task["cwd"]


def matching_project_id(projects: list[ledger.JsonObject], root: str) -> str | None:
    return next(
        (
            cast("str", project["id"])
            for project in projects
            if project.get("deletedAt") is None
            and git.resolved_path(cast("str", project["workspaceRoot"])) == git.resolved_path(root)
        ),
        None,
    )


def project_id(origin: str, token: str, root: str) -> str:
    existing_id = matching_project_id(fetch_projects(origin, token), root)
    if existing_id is not None:
        return existing_id
    t3_output("project", "add", root)
    added_id = matching_project_id(fetch_projects(origin, token), root)
    if added_id is None:
        raise T3CodeError(f"no project for {root} after t3 project add")
    return added_id


def cached_models(driver: str) -> list[ledger.JsonObject]:
    cache = ledger.load_json(t3_home() / "caches" / f"{driver}.json")
    return cast("list[ledger.JsonObject]", cache.get("models", []))


def default_model(driver: str) -> str:
    slug = next(
        (model.get("slug") for model in cached_models(driver) if model.get("isDefault")), None
    )
    return slug if isinstance(slug, str) else DEFAULT_MODELS[driver]


def effort_levels(driver: str, model: str) -> list[str] | None:
    entry = next(
        (
            entry
            for entry in cached_models(driver)
            if model == entry.get("slug") or model in cast("list[object]", entry.get("aliases", []))
        ),
        None,
    )
    if entry is None:
        return None
    capabilities = cast("ledger.JsonObject", entry.get("capabilities", {}))
    descriptors = cast("list[ledger.JsonObject]", capabilities.get("optionDescriptors", []))
    for descriptor in descriptors:
        if descriptor.get("id") == EFFORT_OPTION_IDS[driver]:
            options = cast("list[ledger.JsonObject]", descriptor.get("options", []))
            return [cast("str", option["id"]) for option in options]
    return []


def effort_options(driver: str, effort: str | None) -> list[ledger.JsonObject]:
    return [{"id": EFFORT_OPTION_IDS[driver], "value": effort}] if effort else []


def worktree_and_branch(task: ledger.Task, root: str) -> tuple[str | None, str | None]:
    directory = ledger.task_directory(task)
    if git.resolved_path(directory) == git.resolved_path(root):
        return None, None
    branch = git.run_git(directory, "branch", "--show-current").stdout.strip()
    return directory, branch or None


def thread_title(task_id: str, task: ledger.Task, thread_id: str) -> str:
    if task.get("label") is None:
        return (task["prompt"].strip().splitlines() or [thread_id])[0]
    return ledger.task_name(task_id, task)


def thread_create_command(
    origin: str,
    token: str,
    task: ledger.Task,
    thread_id: str,
    instance_id: str,
    model: str,
    *,
    task_id: str,
    model_options: list[ledger.JsonObject] | None = None,
) -> ledger.JsonObject:
    root = project_root(task)
    worktree, branch = worktree_and_branch(task, root)
    model_selection: ledger.JsonObject = {"instanceId": instance_id, "model": model}
    if model_options:
        model_selection["options"] = model_options
    return {
        "type": "thread.create",
        "createdBy": "user",
        "creationSource": "web",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "projectId": project_id(origin, token, root),
        "title": thread_title(task_id, task, thread_id),
        "modelSelection": model_selection,
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": branch,
        "worktreePath": worktree,
    }


def version() -> str:
    return run_t3("--version").stdout.strip()


def print_delete_failure(thread_id: str, error: Exception) -> None:
    print(f"golem: could not delete T3 Code thread {thread_id}: {error}", file=sys.stderr)


def is_running() -> bool:
    return server_runtime_path().exists()


def require_running_to_delete(thread_ids: list[str]) -> None:
    if thread_ids and not is_running():
        raise SystemExit(
            f"golem: T3 Code is not running; open it to delete thread: {', '.join(thread_ids)}"
        )


def open_to_delete(thread_ids: list[str]) -> None:
    if not thread_ids:
        return
    try:
        ensure_app_running()
    except T3_CODE_ERRORS as error:
        raise SystemExit(
            f"golem: could not open T3 Code to delete thread: {', '.join(thread_ids)}: {error}"
        ) from error


def delete_threads(thread_ids: list[str]) -> None:
    if not thread_ids or not server_runtime_path().exists():
        return
    try:
        origin = server_origin()
        token = issue_token()
    except T3_CODE_ERRORS as error:
        for thread_id in thread_ids:
            print_delete_failure(thread_id, error)
        return
    for thread_id in thread_ids:
        try:
            dispatch_command(
                origin,
                token,
                {"type": "thread.delete", "commandId": str(uuid.uuid4()), "threadId": thread_id},
            )
        except T3_CODE_ERRORS as error:
            print_delete_failure(thread_id, error)


def stop_thread(thread_id: str) -> None:
    if not is_running():
        return
    try:
        dispatch_command(
            server_origin(),
            issue_token(),
            {"type": "thread.stop", "commandId": str(uuid.uuid4()), "threadId": thread_id},
        )
    except T3_CODE_ERRORS as error:
        print(f"golem: could not stop T3 Code thread {thread_id}: {error}", file=sys.stderr)


def remove_claude_instance(instance_id: str) -> None:
    if not t3_cli_path().exists():
        return
    ensure_app_running()
    mutation: ledger.JsonObject = {"operation": "remove", "instanceId": instance_id}
    successful_value(update_settings_exit(server_origin(), issue_token(), mutation))
