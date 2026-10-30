import subprocess
from contextlib import suppress

from golem import display, ledger

NOTIFY_TIMEOUT_SECONDS = 5
NOTIFICATION_MESSAGE_MAX_LENGTH = 200
OUTCOME_NEEDS_REVIEW = "needs review"
OUTCOME_FAILED = "failed"
OUTCOME_GATE_STOPPED = "stopped at gate"


def notify(title: str, message: str) -> None:
    with suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [
                "osascript",
                "-e",
                "on run argv",
                "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e",
                "end run",
                title,
                message,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )


def notify_task(task_id: str, task: ledger.Task, outcome: str) -> None:
    summary = (
        task.get("gate_stop_reason", "")
        if outcome == OUTCOME_GATE_STOPPED
        else task.get("result") or task.get("setup_failure_reason", "")
    )
    message = display.first_line(summary) or display.first_line(task["prompt"])
    notify(f"golem: {task_id} {outcome}", message[:NOTIFICATION_MESSAGE_MAX_LENGTH])


def notify_failed_tasks(failed_tasks: list[tuple[str, ledger.Task]]) -> None:
    for task_id, task in failed_tasks:
        notify_task(task_id, task, OUTCOME_FAILED)
