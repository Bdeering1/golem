import http.client
import json
import os
import subprocess
import sys
import time
import uuid
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
    return f"golem:{ledger.task_backend(task)}:{conversation_id}"


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


def dispatch_command_exit(origin: str, token: str, command: ledger.JsonObject) -> ledger.JsonObject:
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
                    "tag": "orchestration.dispatchCommand",
                    "headers": [],
                    "payload": command,
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


def dispatch_command(origin: str, token: str, command: ledger.JsonObject) -> None:
    command_exit = dispatch_command_exit(origin, token, command)
    if command_exit.get("_tag") != "Success":
        raise T3CodeError(error_message(command_exit) or json.dumps(command_exit))


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


def ensure_thread(task: ledger.Task, conversation_id: str, thread_id: str) -> None:
    if not server_runtime_path().exists():
        start_app()
    if is_thread_present(thread_id):
        return
    origin = server_origin()
    token = issue_token()
    instance_id = INSTANCE_IDS[ledger.task_backend(task)]
    root = project_root(task)
    worktree, branch = worktree_and_branch(task, root)
    dispatch_command(
        origin,
        token,
        {
            "type": "thread.create",
            "createdBy": "user",
            "creationSource": "web",
            "commandId": str(uuid.uuid4()),
            "threadId": thread_id,
            "projectId": project_id(origin, token, root),
            "title": (task["prompt"].strip().splitlines() or [thread_id])[0],
            "modelSelection": {"instanceId": instance_id, "model": default_model(instance_id)},
            "runtimeMode": "full-access",
            "interactionMode": "default",
            "branch": branch,
            "worktreePath": worktree,
            "importedNativeThread": {
                "ref": {"driver": instance_id, "nativeId": conversation_id, "strength": "strong"}
            },
        },
    )
    if not is_thread_present(thread_id):
        raise T3CodeError("thread is missing after thread.create")


def prepare_thread(task: ledger.Task, conversation_id: str) -> None:
    cli_path = t3_cli_path()
    if not cli_path.exists():
        raise SystemExit(f"golem: T3 Code is not installed: {cli_path} not found")
    thread_id = task_thread_id(task, conversation_id)
    try:
        ensure_thread(task, conversation_id, thread_id)
    except T3_CODE_ERRORS as error:
        version = run_t3("--version").stdout.strip()
        raise SystemExit(
            f"golem: T3 Code {version} could not open thread {thread_id}: {error}"
        ) from error


def print_delete_failure(thread_id: str, error: Exception) -> None:
    print(f"golem: could not delete T3 Code thread {thread_id}: {error}", file=sys.stderr)


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
