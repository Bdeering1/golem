import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import io

from golem import chains, gates, git, ledger, locks, notifications, spawn

SETUP_OUTPUT_ERROR = (
    "setup output must be a JSON object with optional string directory and prompt_suffix"
)
MAX_SETUP_BUSY_RETRIES = 12
SETUP_BUSY_RETRY_SECONDS = 300


def setup_input_json(task_id: str, task: ledger.Task) -> str:
    return json.dumps(
        {
            "task_id": task_id,
            "session_id": task["session_id"]
            if ledger.task_backend(task) == ledger.BACKEND_CLAUDE
            else None,
            "directory": ledger.task_directory(task),
            "prompt": task["prompt"],
        }
    )


class SetupError(Exception):
    pass


def setup_directory_and_prompt(task_id: str, task: ledger.Task, setup: str) -> tuple[str, str]:
    failure_reason, stdout = spawn.shell_command_outcome(
        setup,
        "setup",
        task,
        setup_input_json(task_id, task),
        gates.GATE_TIMEOUT_SECONDS,
        is_capturing_stdout=True,
    )
    if failure_reason is not None:
        raise SetupError(failure_reason)
    try:
        output = cast("object", json.loads(stdout))
    except json.JSONDecodeError as error:
        raise SetupError(f"setup output is not valid JSON: {error}") from error
    if not isinstance(output, dict):
        raise SetupError(SETUP_OUTPUT_ERROR)
    setup_output = cast("ledger.JsonObject", output)
    directory = setup_output.get("directory")
    prompt_suffix = setup_output.get("prompt_suffix")
    if not isinstance(directory, str | None) or not isinstance(prompt_suffix, str | None):
        raise SetupError(SETUP_OUTPUT_ERROR)
    if directory is not None:
        try:
            directory = git.resolved_path(str(Path(task["cwd"], Path(directory).expanduser())))
        except (OSError, RuntimeError, ValueError) as error:
            raise SetupError(f"setup directory is not usable: {error}") from error
        if not Path(directory).is_dir():
            raise SetupError(f"setup directory does not exist: {directory}")
    prompt = task["prompt"] if prompt_suffix is None else f"{task['prompt']}\n\n{prompt_suffix}"
    return directory or task["cwd"], prompt


def fail_setup(task_id: str, reason: str) -> None:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None or task.get("status") != ledger.STATUS_RUNNING:
            return
        task["status"] = ledger.STATUS_FAILED
        task["setup_failure_reason"] = f"setup failed: {reason}"
        failed_task = task.copy()
    notifications.notify_task(task_id, failed_task, notifications.OUTCOME_FAILED)


def record_setup(task_id: str, directory: str, prompt: str) -> ledger.Task | None:
    with ledger.locked_tasks() as tasks:
        stored_task = tasks.get(task_id)
        if stored_task is None or stored_task.get("status") != ledger.STATUS_RUNNING:
            return None
        stored_task["cwd"] = directory
        stored_task["prompt"] = prompt
        stored_task["setup_complete"] = True
        stored_task.pop("setup_directory", None)
        stored_task.pop("setup_busy_retry_count", None)
        for descendant_id in chains.descendant_ids_with_status(tasks, task_id, ledger.ALL_STATUSES):
            tasks[descendant_id]["cwd"] = directory
        return stored_task.copy()


def requeue_busy_setup(task_id: str, directory: str, prompt: str) -> None:
    with ledger.locked_tasks() as tasks:
        task = tasks.get(task_id)
        if task is None or task.get("status") != ledger.STATUS_RUNNING:
            return
        task["setup_directory"] = directory
        task["prompt"] = prompt
        retry_count = task.get("setup_busy_retry_count", 0)
        if retry_count < MAX_SETUP_BUSY_RETRIES:
            task["setup_busy_retry_count"] = retry_count + 1
            task["status"] = ledger.STATUS_SCHEDULED
            retry_at = datetime.now(UTC) + timedelta(seconds=SETUP_BUSY_RETRY_SECONDS)
            task["due_at"] = retry_at.isoformat()
            return
        task["status"] = ledger.STATUS_FAILED
        task["setup_failure_reason"] = f"setup directory stayed busy: {directory}"
        failed_task = task.copy()
    notifications.notify_task(task_id, failed_task, notifications.OUTCOME_FAILED)


def run_setup(
    task_id: str, task: ledger.Task, directory_lock_file: io.TextIOWrapper
) -> tuple[ledger.Task, io.TextIOWrapper] | None:
    setup = task.get("setup")
    if setup is None or task.get("setup_complete"):
        return task, directory_lock_file
    directory = task.get("setup_directory")
    prompt = task["prompt"]
    if directory is None:
        try:
            directory, prompt = setup_directory_and_prompt(task_id, task, setup)
        except SetupError as error:
            fail_setup(task_id, str(error))
            return None
    if locks.directory_lock_path(directory) == locks.directory_lock_path(task["cwd"]):
        prepared = record_setup(task_id, directory, prompt)
        return None if prepared is None else (prepared, directory_lock_file)
    moved_lock_file = locks.acquire_lock(locks.directory_lock_path(directory))
    if moved_lock_file is None:
        requeue_busy_setup(task_id, directory, prompt)
        return None
    prepared = record_setup(task_id, directory, prompt)
    if prepared is None:
        locks.release_lock(moved_lock_file)
        return None
    locks.release_lock(directory_lock_file)
    return prepared, moved_lock_file
