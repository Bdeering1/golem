from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from golem import ledger, t3_code
from golem.backends import codex, registry

CLAUDE_DESKTOP_CLIENT = "claude-desktop"
BUILT_IN_DEFAULT_CLIENTS = {ledger.BACKEND_CLAUDE: CLAUDE_DESKTOP_CLIENT}


def keep_environment(environment: dict[str, str]) -> dict[str, str]:
    return environment


def accept_arguments(_agent_args: list[str]) -> None:
    pass


def skip_preparation(_task: ledger.Task, _conversation_id: str) -> None:
    pass


@dataclass(frozen=True)
class Client:
    name: str
    backend_ids: frozenset[str]
    is_terminal: bool
    command: Callable[[ledger.Task, str, list[str]], list[str]]
    environment: Callable[[dict[str, str]], dict[str, str]] = keep_environment
    validate_arguments: Callable[[list[str]], None] = accept_arguments
    prepare: Callable[[ledger.Task, str], None] = skip_preparation
    has_t3_code_thread: bool = False


CLIENTS: dict[str, Client] = {
    client.name: client
    for client in (
        Client(
            name="claude-code",
            backend_ids=frozenset({ledger.BACKEND_CLAUDE}),
            is_terminal=True,
            command=lambda _task, conversation_id, agent_args: [
                "claude",
                "--resume",
                conversation_id,
                *agent_args,
            ],
        ),
        Client(
            name="codex-cli",
            backend_ids=frozenset({ledger.BACKEND_CODEX}),
            is_terminal=True,
            command=lambda _task, conversation_id, agent_args: [
                "codex",
                "resume",
                conversation_id,
                *agent_args,
                *codex.CODEX_PROVIDER_ARGUMENTS,
            ],
            environment=codex.codex_environment,
            validate_arguments=codex.validate_resume_arguments,
        ),
        Client(
            name=CLAUDE_DESKTOP_CLIENT,
            backend_ids=frozenset({ledger.BACKEND_CLAUDE}),
            is_terminal=False,
            command=lambda _task, conversation_id, _agent_args: [
                "open",
                f"claude://resume?session={conversation_id}",
            ],
        ),
        Client(
            name="t3-code",
            backend_ids=frozenset({ledger.BACKEND_CLAUDE, ledger.BACKEND_CODEX}),
            is_terminal=False,
            command=lambda task, conversation_id, _agent_args: [
                "open",
                t3_code.thread_link(task, conversation_id),
            ],
            prepare=t3_code.prepare_thread,
            has_t3_code_thread=True,
        ),
    )
}


def task_client(task_id: str, task: ledger.Task, client_name: str | None) -> Client:
    backend = registry.task_backend(task)
    task_backend_id = task["backend"]
    if client_name is not None:
        client = CLIENTS[client_name]
        if task_backend_id not in client.backend_ids:
            raise SystemExit(
                f"golem: {client.name} does not support {backend.NAME} tasks: {task_id}"
            )
        return client
    return next(
        client
        for client in CLIENTS.values()
        if client.is_terminal and task_backend_id in client.backend_ids
    )


def default_clients(configured: dict[str, str], invalid_config_prefix: str) -> dict[str, str]:
    for backend_id, client_name in configured.items():
        entry_message_prefix = f"{invalid_config_prefix}: default_clients.{backend_id}"
        if backend_id not in registry.BACKENDS:
            raise SystemExit(f"{entry_message_prefix} is not a backend")
        client = CLIENTS.get(client_name)
        if client is None:
            raise SystemExit(f"{entry_message_prefix} names unknown client {client_name}")
        if client.is_terminal:
            raise SystemExit(f"{entry_message_prefix} must be an app client, not {client_name}")
        if backend_id not in client.backend_ids:
            backend_name = registry.BACKENDS[backend_id].NAME
            raise SystemExit(
                f"{entry_message_prefix} names {client_name}, which cannot open {backend_name} tasks"
            )
    return {**BUILT_IN_DEFAULT_CLIENTS, **configured}


def task_auto_pickup_client(task: ledger.Task) -> str | None:
    if "auto_pickup_client" in task:
        return task["auto_pickup_client"]
    return CLAUDE_DESKTOP_CLIENT if task.get("auto_pickup") else None
