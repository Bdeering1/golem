from datetime import UTC, datetime
from pathlib import Path

from golem import display, ledger

PROGRESS_LINE_WIDTH = 120
USAGE_ERROR = "golem: usage lookup unavailable"
USAGE_TIMEOUT_SECONDS = 30
FULL_UTILIZATION = 100
USAGE_LIMIT_RESUME_PROMPT = "The usage limit has reset. Continue the task."
WORKTREE_ARGUMENTS = {"-w", "--worktree"}


def has_agent_argument(agent_args: list[str], names: set[str]) -> bool:
    return any(
        argument == name or argument.startswith(f"{name}=")
        for argument in agent_args
        for name in names
    )


def is_usage_limit_resume(task: ledger.Task) -> bool:
    return task.get("usage_limit_resume_count", 0) > 0


def tool_call_progress(name: str, argument: object) -> str:
    line = (
        f"[tool] {name} {display.first_line(argument)}"
        if isinstance(argument, str) and argument
        else f"[tool] {name}"
    )
    return line[:PROGRESS_LINE_WIDTH]


def validated_reset_time(reset_time: datetime) -> datetime:
    if reset_time.tzinfo is None or reset_time <= datetime.now(UTC):
        raise SystemExit(USAGE_ERROR)
    return reset_time


def transcript_paths(task: ledger.Task) -> list[Path]:
    if ledger.task_backend(task) == ledger.BACKEND_CLAUDE:
        return list((Path.home() / ".claude" / "projects").glob(f"*/{task['session_id']}.jsonl"))
    thread_id = task.get("thread_id")
    if not isinstance(thread_id, str):
        return []
    codex_home = Path(task.get("environment", {}).get("CODEX_HOME", Path.home() / ".codex"))
    return list((codex_home / "sessions").glob(f"**/rollout-*-{thread_id}.jsonl"))
