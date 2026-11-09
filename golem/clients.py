from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from golem import ledger
from golem.backends import codex, registry

AUTO_PICKUP_CLIENT = "claude-desktop"


def keep_environment(environment: dict[str, str]) -> dict[str, str]:
    return environment


def accept_arguments(_agent_args: list[str]) -> None:
    pass


@dataclass(frozen=True)
class Client:
    name: str
    backend_id: str
    is_terminal: bool
    command: Callable[[str, list[str]], list[str]]
    environment: Callable[[dict[str, str]], dict[str, str]] = keep_environment
    validate_arguments: Callable[[list[str]], None] = accept_arguments


CLIENTS: dict[str, Client] = {
    client.name: client
    for client in (
        Client(
            name="claude-code",
            backend_id=ledger.BACKEND_CLAUDE,
            is_terminal=True,
            command=lambda conversation_id, agent_args: [
                "claude",
                "--resume",
                conversation_id,
                *agent_args,
            ],
        ),
        Client(
            name="codex-cli",
            backend_id=ledger.BACKEND_CODEX,
            is_terminal=True,
            command=lambda conversation_id, agent_args: [
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
            name=AUTO_PICKUP_CLIENT,
            backend_id=ledger.BACKEND_CLAUDE,
            is_terminal=False,
            command=lambda conversation_id, _agent_args: [
                "open",
                f"claude://resume?session={conversation_id}",
            ],
        ),
    )
}


def task_client(task_id: str, task: ledger.Task, client_name: str | None) -> Client:
    backend = registry.task_backend(task)
    task_backend_id = task["backend"]
    if client_name is not None:
        client = CLIENTS[client_name]
        if client.backend_id != task_backend_id:
            raise SystemExit(
                f"golem: {client.name} does not support {backend.NAME} tasks: {task_id}"
            )
        return client
    return next(
        client
        for client in CLIENTS.values()
        if client.is_terminal and client.backend_id == task_backend_id
    )
