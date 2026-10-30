from golem import ledger
from golem.backends import claude, codex, common

BACKENDS: dict[str, common.Backend] = {ledger.BACKEND_CLAUDE: claude, ledger.BACKEND_CODEX: codex}


def task_backend(task: ledger.Task) -> common.Backend:
    return BACKENDS[ledger.task_backend(task)]
