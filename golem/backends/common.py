import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pathlib import Path

from golem import display, ledger

PROGRESS_LINE_WIDTH = 120
USAGE_ERROR = "golem: usage lookup unavailable"
USAGE_TIMEOUT_SECONDS = 30
USAGE_WINDOW_PROMPT = "Reply with the single character: k"
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


def record_session_field(
    task_id: str,
    session_id: str,
    name: Literal["thread_id", "t3_code_thread_id", "native_session_id"],
    value: str,
) -> None:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is not None and task.get("session_id") == session_id:
            task[name] = value


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


def usage_window_request_error(command: list[str], environment: dict[str, str]) -> str | None:
    with tempfile.TemporaryDirectory() as directory:
        try:
            result = subprocess.run(
                command,
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=USAGE_TIMEOUT_SECONDS,
                check=False,
            )
        except OSError as error:
            return str(error)
        except subprocess.TimeoutExpired:
            return f"{command[0]} timed out after {USAGE_TIMEOUT_SECONDS}s"
    if result.returncode == 0:
        return None
    output_lines = (result.stderr.strip() or result.stdout.strip()).splitlines()
    if not output_lines:
        return f"{command[0]} exited with status {result.returncode}"
    return output_lines[-1].strip()


@dataclass(frozen=True)
class RunOutcome:
    returncode: int
    result: str | None
    is_usage_limit: bool
    stderr: str = ""
    working_directory: str | None = None
    reset_time: datetime | None = None
    stop_reason: str | None = None


class UsageBackend(Protocol):
    NAME: str

    def next_reset_time(self, environment: dict[str, str]) -> datetime | None: ...

    def open_usage_window(self, environment: dict[str, str]) -> str | None: ...


class Backend(Protocol):
    NAME: str

    def run(
        self, task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
    ) -> RunOutcome: ...

    def validate_run_arguments(self, agent_args: list[str]) -> None: ...

    def conversation_id(self, task: ledger.Task) -> str | None: ...

    def transcript_paths(self, task: ledger.Task) -> list[Path]: ...
