import http.client
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib import parse

from websockets.exceptions import WebSocketException
from websockets.sync import client as websocket_client

from golem import git, ledger

if TYPE_CHECKING:
    from collections.abc import Iterable

APP_BUNDLE_ID = "com.t3tools.t3code"
APP_START_TIMEOUT_SECONDS = 60
APP_START_POLL_SECONDS = 0.5
NETWORK_TIMEOUT_SECONDS = 30
INSTANCE_IDS = {ledger.BACKEND_CLAUDE: "claudeAgent", ledger.BACKEND_CODEX: "codex"}
DEFAULT_MODELS = {"claudeAgent": "claude-opus-5-5", "codex": "gpt-6-astra"}


class T3CodeError(Exception):
    pass


@dataclass(frozen=True)
class ClaudeInstance:
    instance_id: str
    display_name: str
    home_path: str


T3_CODE_ERRORS = (
    T3CodeError,
    OSError,
    http.client.HTTPException,
    WebSocketException,
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


def thread_link(task: ledger.Task, conversation_id: str) -> str:
    environment_id = (t3_home() / "userdata" / "environment-id").read_text(encoding="utf-8")
    encoded_thread_id = parse.quote(task_thread_id(task, conversation_id), safe="")
    return f"t3code://threads/{environment_id.strip()}/{encoded_thread_id}"


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


def thread_projection(origin: str, token: str, thread_id: str) -> ledger.JsonObject:
    projection_exit = rpc_exit(
        origin, token, "orchestration.getThreadProjection", {"threadId": thread_id}
    )
    return cast("ledger.JsonObject", successful_value(projection_exit))


def wait_for_server() -> None:
    deadline = time.monotonic() + APP_START_TIMEOUT_SECONDS
    while not server_runtime_path().exists():
        if time.monotonic() >= deadline:
            raise T3CodeError(f"T3 Code did not start within {APP_START_TIMEOUT_SECONDS}s")
        time.sleep(APP_START_POLL_SECONDS)


def start_app() -> None:
    if subprocess.run(["open", "-b", APP_BUNDLE_ID], check=False).returncode:
        raise T3CodeError(f"could not start {APP_BUNDLE_ID}")
    wait_for_server()


def is_thread_present(thread_id: str) -> bool:
    return run_t3("thread", "show", thread_id, "--json").returncode == 0


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


def default_model(instance_id: str) -> str:
    cache = ledger.load_json(t3_home() / "caches" / f"{instance_id}.json")
    models = cast("list[ledger.JsonObject]", cache.get("models", []))
    slug = next((model.get("slug") for model in models if model.get("isDefault")), None)
    return slug if isinstance(slug, str) else DEFAULT_MODELS[instance_id]


def worktree_and_branch(task: ledger.Task, root: str) -> tuple[str | None, str | None]:
    directory = ledger.task_directory(task)
    if git.resolved_path(directory) == git.resolved_path(root):
        return None, None
    branch = git.run_git(directory, "branch", "--show-current").stdout.strip()
    return directory, branch or None


def thread_create_command(
    origin: str, token: str, task: ledger.Task, thread_id: str, instance_id: str, model: str
) -> ledger.JsonObject:
    root = project_root(task)
    worktree, branch = worktree_and_branch(task, root)
    return {
        "type": "thread.create",
        "createdBy": "user",
        "creationSource": "web",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "projectId": project_id(origin, token, root),
        "title": (task["prompt"].strip().splitlines() or [thread_id])[0],
        "modelSelection": {"instanceId": instance_id, "model": model},
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": branch,
        "worktreePath": worktree,
    }


def ensure_thread(
    task: ledger.Task, conversation_id: str, thread_id: str, instance: ClaudeInstance | None
) -> None:
    if not server_runtime_path().exists():
        start_app()
    origin = server_origin()
    token = None
    if instance is not None:
        token = issue_token()
        upsert_claude_instance(origin, token, instance)
    if is_thread_present(thread_id):
        return
    if task["backend"] == ledger.BACKEND_T3:
        raise T3CodeError("thread does not exist in T3 Code")
    token = token or issue_token()
    driver = INSTANCE_IDS[task["backend"]]
    instance_id = driver if instance is None else instance.instance_id
    command = thread_create_command(
        origin, token, task, thread_id, instance_id, default_model(driver)
    )
    command["importedNativeThread"] = {
        "ref": {"driver": driver, "nativeId": conversation_id, "strength": "strong"}
    }
    dispatch_command(origin, token, command)
    if not is_thread_present(thread_id):
        raise T3CodeError("thread is missing after thread.create")


def version() -> str:
    return run_t3("--version").stdout.strip()


def prepare_thread(
    task: ledger.Task, conversation_id: str, instance: ClaudeInstance | None
) -> None:
    cli_path = t3_cli_path()
    if not cli_path.exists():
        raise SystemExit(f"golem: T3 Code is not installed: {cli_path} not found")
    thread_id = task_thread_id(task, conversation_id)
    try:
        ensure_thread(task, conversation_id, thread_id, instance)
    except T3_CODE_ERRORS as error:
        raise SystemExit(
            f"golem: T3 Code {version()} could not open thread {thread_id}: {error}"
        ) from error


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
    if not thread_ids or is_running():
        return
    try:
        start_app()
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


def remove_claude_instance(instance_id: str) -> None:
    if not t3_cli_path().exists():
        return
    if not is_running():
        start_app()
    mutation: ledger.JsonObject = {"operation": "remove", "instanceId": instance_id}
    successful_value(update_settings_exit(server_origin(), issue_token(), mutation))
