import argparse
import json
import subprocess
import uuid
from pathlib import Path


LEDGER = Path(__file__).resolve().parent / "tasks.json"


def load_tasks() -> dict[str, str]:
    if not LEDGER.exists():
        return {}
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def save_tasks(tasks: dict[str, str]) -> None:
    LEDGER.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")


def session_id(task_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kairos:{task_id}"))


def run(task_id: str, prompt: str) -> int:
    tasks = load_tasks()
    if task_id in tasks:
        raise SystemExit(f"kairos: task already exists: {task_id}")

    cwd = str(Path.cwd())
    tasks[task_id] = cwd
    save_tasks(tasks)
    return subprocess.run(
        ["claude", "--session-id", session_id(task_id), "--print", prompt], cwd=cwd
    ).returncode


def pickup(task_id: str) -> int:
    tasks = load_tasks()
    if task_id not in tasks:
        raise SystemExit(f"kairos: task not found: {task_id}")
    return subprocess.run(
        ["claude", "--resume", session_id(task_id)], cwd=tasks[task_id]
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(prog="kairos")
    commands = parser.add_subparsers(dest="command", required=True)

    run_command = commands.add_parser("run")
    run_command.add_argument("id")
    prompt = run_command.add_mutually_exclusive_group(required=True)
    prompt.add_argument("prompt", nargs="?")
    prompt.add_argument("--file")

    pickup_command = commands.add_parser("pickup")
    pickup_command.add_argument("id")
    arguments = parser.parse_args()

    if arguments.command == "pickup":
        return pickup(arguments.id)
    prompt_text = (
        Path(arguments.file).read_text(encoding="utf-8")
        if arguments.file
        else arguments.prompt
    )
    return run(arguments.id, prompt_text)


if __name__ == "__main__":
    raise SystemExit(main())
