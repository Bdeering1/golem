import re
import string
from pathlib import Path
from typing import cast

from golem import chains, ledger
from golem.worktrees import git

BRANCH_LETTERS = string.ascii_lowercase[1:]
PREFIX_LENGTH = 3


def counters_path() -> Path:
    return ledger.state_directory() / "task_counters.json"


def repository_name(cwd: str) -> str:
    common_directory = git.git_common_directory(cwd)
    if common_directory is None:
        return Path(cwd).name
    return Path(common_directory).parent.name


def task_id_prefix(name: str) -> str:
    characters = [character for character in name if character.isascii() and character.isalnum()]
    return "".join(characters[:PREFIX_LENGTH]).lower()


def new_root_task_id(tasks: ledger.Tasks, prefix: str) -> str:
    path = counters_path()
    counters = cast("dict[str, int]", ledger.load_json(path))
    number = counters.get(prefix, 0) + 1
    while f"{prefix}{number}" in tasks:
        number += 1
    counters[prefix] = number
    ledger.save_json(path, counters)
    return f"{prefix}{number}"


def branch_letter(root_id: str, task_id: str) -> str:
    match = re.fullmatch(rf"{re.escape(root_id)}-\d+-([a-z])", task_id)
    return "" if match is None else match.group(1)


def new_dependent_task_id(tasks: ledger.Tasks, parent_id: str) -> str:
    root_id = ledger.root_task_id(tasks, parent_id)
    step_id = f"{root_id}-{chains.task_depth(tasks, parent_id) + 1}"
    letter = branch_letter(root_id, parent_id)
    task_id = f"{step_id}-{letter}" if letter else step_id
    if not chains.dependent_task_ids(tasks, parent_id) and task_id not in tasks:
        return task_id
    used_letters = {
        branch_letter(root_id, chain_id) for chain_id in chains.chain_task_ids(tasks, parent_id)
    }
    for letter in BRANCH_LETTERS:
        task_id = f"{step_id}-{letter}"
        if letter not in used_letters and task_id not in tasks:
            return task_id
    raise SystemExit(f"golem: chain {root_id} has no branch letters left")


def insert_dependents(
    tasks: ledger.Tasks, records: list[ledger.Task], parent_id: str
) -> ledger.Tasks:
    inserted: ledger.Tasks = {}
    for record in records:
        record["after"] = parent_id
        task_id = new_dependent_task_id(tasks, parent_id)
        tasks[task_id] = record
        inserted[task_id] = record
        parent_id = task_id
    return inserted
