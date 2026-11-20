import pytest

from golem import cli, ledger, task_ids
from tests import support


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("golem", "gol"),
        ("gh-dash", "ghd"),
        ("3d-viewer", "3dv"),
        ("Go", "go"),
        ("---", ""),
    ],
)
def test_task_id_prefix_keeps_first_three_letters_or_digits(name, prefix):
    assert task_ids.task_id_prefix(name) == prefix


def test_repository_name_comes_from_main_checkout_of_a_worktree(git_repository):
    worktree = support.git_worktree(git_repository)

    assert task_ids.repository_name(str(worktree)) == support.REPOSITORY_DIRECTORY


def test_repository_name_outside_git_is_the_directory_name(tmp_path):
    directory = tmp_path / "gh-dash"
    directory.mkdir()

    assert task_ids.repository_name(str(directory)) == "gh-dash"


def test_each_prefix_counts_separately_and_skips_ids_in_the_ledger():
    tasks: ledger.Tasks = {"gol2": support.task_record(ledger.STATUS_FINISHED, task_id="gol2")}

    assert [
        task_ids.new_root_task_id(tasks, prefix) for prefix in ("gol", "gol", "ghd", "gol")
    ] == ["gol1", "gol3", "ghd1", "gol4"]


def test_pruned_task_number_is_not_reused(installed_launch_agent, ledger_path):
    assert cli.main(["run", support.PROMPT, "--now"]) == 0
    assert cli.main(["cancel", support.TASK_ID]) == 0
    assert cli.main(["prune", "--yes"]) == 0

    assert cli.main(["run", support.PROMPT, "--now"]) == 0

    assert list(support.read_tasks(ledger_path)) == ["wor2"]


def insert_dependent(tasks: ledger.Tasks, parent_id: str) -> str:
    record = support.task_record(ledger.STATUS_WAITING)
    return next(iter(task_ids.insert_with_new_ids(tasks, [record], parent_id)))


@pytest.mark.parametrize("root_id", ["gol14", "fix-login"])
def test_dependents_get_step_numbers_and_later_siblings_start_branches(root_id):
    tasks: ledger.Tasks = {root_id: support.task_record(ledger.STATUS_FINISHED, task_id=root_id)}

    first_child = insert_dependent(tasks, root_id)
    branch_child = insert_dependent(tasks, root_id)
    branch_grandchild = insert_dependent(tasks, branch_child)
    first_grandchild = insert_dependent(tasks, first_child)
    second_branch_grandchild = insert_dependent(tasks, first_child)

    assert [
        first_child,
        branch_child,
        branch_grandchild,
        first_grandchild,
        second_branch_grandchild,
    ] == [
        f"{root_id}-2",
        f"{root_id}-2-b",
        f"{root_id}-3-b",
        f"{root_id}-3",
        f"{root_id}-3-c",
    ]
