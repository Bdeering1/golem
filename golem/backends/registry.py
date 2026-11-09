from golem import ledger
from golem.backends import claude, codex, common, t3

BACKENDS: dict[str, common.Backend] = {
    ledger.BACKEND_CLAUDE: claude,
    ledger.BACKEND_CODEX: codex,
    ledger.BACKEND_T3: t3,
}
USAGE_BACKENDS: dict[str, common.UsageBackend] = {
    ledger.BACKEND_CLAUDE: claude,
    ledger.BACKEND_CODEX: codex,
}


def task_backend(task: ledger.Task) -> common.Backend:
    return BACKENDS[task["backend"]]


def agent_backend_id(backend_id: str, agent_args: list[str]) -> str:
    return t3.provider_id(agent_args) if backend_id == ledger.BACKEND_T3 else backend_id


def usage_backend(backend_id: str, agent_args: list[str]) -> common.UsageBackend:
    return USAGE_BACKENDS[agent_backend_id(backend_id, agent_args)]


def task_usage_backend(task: ledger.Task) -> common.UsageBackend:
    return usage_backend(task["backend"], task["agent_args"])
