import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path

from golem import accounts, ledger, task_processes


def golem_command() -> list[str]:
    return [str(Path(sys.executable).absolute()), "-P", "-m", "golem"]


def is_captured_variable(name: str) -> bool:
    return name in {"PATH", "LANG", "CODEX_HOME"} or name.startswith("LC_")


def captured_environment() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if is_captured_variable(name)}


def task_environment(task: ledger.Task) -> dict[str, str]:
    inherited_environment = {
        name: value for name, value in os.environ.items() if not is_captured_variable(name)
    }
    return {**inherited_environment, **task.get("environment", {})}


def shell_command_outcome(
    command: str,
    task_id: str,
    label: str,
    task: ledger.Task,
    input_json: str,
    timeout_seconds: float,
    *,
    is_capturing_stdout: bool = False,
) -> tuple[str | None, str]:
    # Files, not pipes: a background process the command starts can hold them open after it exits.
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")
        if is_capturing_stdout
        else Path(os.devnull).open("w+", encoding="utf-8") as stdout_file,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                ["/bin/sh", "-c", command],
                cwd=ledger.task_directory(task),
                env=accounts.account_environment(task_environment(task), task.get("account")),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                encoding="utf-8",
                errors="replace",
                process_group=0,
            )
        except OSError as error:
            return str(error), ""
        with (
            process,
            task_processes.recorded_process(
                task_id, task["session_id"], task_processes.FIELD_COMMAND_PROCESS_GROUP, process
            ),
        ):
            try:
                process.communicate(input_json, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                return f"{label} timed out after {timeout_seconds}s", ""
            finally:
                # Unreaped, so the group ID is still ours; macOS fails with EPERM once only zombies remain.
                if process.returncode is None:
                    with suppress(OSError):
                        os.killpg(process.pid, signal.SIGKILL)
        if process.returncode == 0:
            stdout_file.seek(0)
            return None, stdout_file.read()
        stderr_file.seek(0)
        reason = stderr_file.read().strip() or f"{label} exited with status {process.returncode}"
        return reason, ""


def start_task_executor(
    task_id: str, task: ledger.Task, *, stderr: int | None = None
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [*golem_command(), "_execute", task_id, task["session_id"]],
        env=task_environment(task),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
    )


def reap_finished_executors(
    processes: list[subprocess.Popen[bytes]],
) -> list[subprocess.Popen[bytes]]:
    return [process for process in processes if process.poll() is None]
