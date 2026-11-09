from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
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


@dataclass(frozen=True)
class RunOutcome:
    returncode: int
    result: str | None
    is_usage_limit: bool
    stderr: str = ""
    working_directory: str | None = None


class Backend(Protocol):
    NAME: str
    IS_GATE_SUPPORTED: bool

    def run(
        self, task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
    ) -> RunOutcome: ...

    def next_reset_time(self) -> datetime | None: ...

    def validate_run_arguments(self, agent_args: list[str]) -> None: ...

    def conversation_id(self, task: ledger.Task) -> str | None: ...

    def transcript_paths(self, task: ledger.Task) -> list[Path]: ...
