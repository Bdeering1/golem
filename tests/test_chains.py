import json
from typing import Any

import pytest

from golem import cli, ledger
from golem.backends import claude
from tests import support


def test_scheduled_task_runs_waiting_chain(installed_launch_agent, fake_commands, ledger_path):
    support.create_waiting_chain()
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    support.set_task_due_now(ledger_path, support.PARENT_ID)

    cli.main(["_execute", support.PARENT_ID, tasks[support.PARENT_ID]["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task_id: task["status"] for task_id, task in tasks.items()} == {
        support.PARENT_ID: ledger.STATUS_FINISHED,
        support.CHILD_ID: ledger.STATUS_FINISHED,
        support.GRANDCHILD_ID: ledger.STATUS_FINISHED,
        support.SIBLING_ID: ledger.STATUS_FINISHED,
    }
    assert support.logged_claude_calls(fake_commands) == [
        support.expected_claude_args(
            tasks[support.PARENT_ID], support.PARENT_PROMPT, task_id=support.PARENT_ID
        ),
        support.expected_claude_args(
            tasks[support.CHILD_ID], support.CHILD_PROMPT, task_id=support.CHILD_ID
        ),
        support.expected_claude_args(
            tasks[support.GRANDCHILD_ID], support.GRANDCHILD_PROMPT, task_id=support.GRANDCHILD_ID
        ),
        support.expected_claude_args(
            tasks[support.SIBLING_ID], support.SIBLING_PROMPT, task_id=support.SIBLING_ID
        ),
    ]


@pytest.mark.parametrize(
    ("parents", "schedule_arguments"),
    [
        pytest.param([], ["--now"], id="now"),
        pytest.param(
            [(support.PARENT_ID, support.PARENT_PROMPT)],
            ["--after", support.PARENT_ID],
            id="after-pending-parent",
        ),
    ],
)
def test_run_then_chain_runs_in_order_with_shared_agent_arguments(
    installed_launch_agent, fake_commands, ledger_path, parents, schedule_arguments
):
    for task_id, prompt in parents:
        cli.main(["run", task_id, prompt, "--at", support.AT_TEXT, "--", *support.AGENT_ARGS])
        support.set_task_due_now(ledger_path, task_id)
    follow_ups = [
        (support.GRANDCHILD_ID, support.GRANDCHILD_PROMPT),
        (support.SIBLING_ID, support.SIBLING_PROMPT),
    ]
    then_arguments = [argument for follow_up in follow_ups for argument in ("--then", *follow_up)]
    chain = [*parents, (support.CHILD_ID, support.CHILD_PROMPT), *follow_ups]
    first_id = chain[0][0]

    assert (
        cli.main(
            [
                "run",
                support.CHILD_ID,
                support.CHILD_PROMPT,
                *schedule_arguments,
                *then_arguments,
                "--",
                *support.AGENT_ARGS,
            ]
        )
        == 0
    )
    tasks = support.read_tasks(ledger_path)

    assert cli.main(["_execute", first_id, tasks[first_id]["session_id"]]) == 0
    assert [support.read_tasks(ledger_path)[task_id]["status"] for task_id, _ in chain] == [
        *[ledger.STATUS_FINISHED] * (len(chain) - 1),
        ledger.STATUS_FINISHED,
    ]
    assert support.logged_claude_calls(fake_commands) == [
        support.expected_claude_args(tasks[task_id], prompt, task_id=task_id)
        for task_id, prompt in chain
    ]


def test_run_then_auto_pickup_opens_only_last_task(
    installed_launch_agent, fake_commands, ledger_path
):
    assert (
        cli.main(
            [
                "run",
                support.TASK_ID,
                support.PROMPT,
                "--now",
                "--auto-pickup",
                "--then",
                support.CHILD_ID,
                support.CHILD_PROMPT,
                "--then",
                support.GRANDCHILD_ID,
                support.GRANDCHILD_PROMPT,
            ]
        )
        == 0
    )
    tasks = support.read_tasks(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, tasks[support.TASK_ID]["session_id"]]) == 0
    assert {task["status"] for task in support.read_tasks(ledger_path).values()} == {
        ledger.STATUS_FINISHED
    }
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={tasks[support.GRANDCHILD_ID]['session_id']}\n"
    )


def test_failed_follow_up_auto_pickup_opens_last_success(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("GOLEM_CLAUDE_FAIL_TASK", support.GRANDCHILD_ID)
    cli.main(
        [
            "run",
            support.TASK_ID,
            support.PROMPT,
            "--now",
            "--auto-pickup",
            "--then",
            support.CHILD_ID,
            support.CHILD_PROMPT,
            "--then",
            support.GRANDCHILD_ID,
            support.GRANDCHILD_PROMPT,
        ]
    )
    tasks = support.read_tasks(ledger_path)

    assert cli.main(["_execute", support.TASK_ID, tasks[support.TASK_ID]["session_id"]]) == 0
    assert {
        task_id: task["status"] for task_id, task in support.read_tasks(ledger_path).items()
    } == {
        support.TASK_ID: ledger.STATUS_FINISHED,
        support.CHILD_ID: ledger.STATUS_FINISHED,
        support.GRANDCHILD_ID: ledger.STATUS_FAILED,
    }
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={tasks[support.CHILD_ID]['session_id']}\n"
    )


def test_after_parent_with_pending_gate_waits_for_the_gate(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_parent_with_waiting_child(ledger_path, gate="true")

    assert (
        cli.main(["run", support.SIBLING_ID, support.SIBLING_PROMPT, "--after", support.PARENT_ID])
        == 0
    )
    assert support.read_task(ledger_path, support.SIBLING_ID)["status"] == ledger.STATUS_WAITING


def test_after_succeeded_auto_pickup_parent_does_not_pass_to_child(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(claude, "next_reset_time", lambda _environment: support.RESET_TIME)
    support.write_task(
        ledger_path,
        ledger.STATUS_FINISHED,
        task_id=support.PARENT_ID,
        auto_pickup=True,
    )

    assert (
        cli.main(["run", support.CHILD_ID, support.CHILD_PROMPT, "--after", support.PARENT_ID]) == 0
    )
    assert "auto_pickup_client" not in support.read_task(ledger_path, support.CHILD_ID)


def test_retrying_failed_parent_runs_waiting_dependent(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    support.create_waiting_chain()
    parent = support.read_task(ledger_path, support.PARENT_ID)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    support.set_task_due_now(ledger_path, support.PARENT_ID)
    cli.main(["_execute", support.PARENT_ID, parent["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")

    cli.main(["retry", support.PARENT_ID, "--now"])
    retry = support.read_task(ledger_path, support.PARENT_ID)
    cli.main(["_execute", support.PARENT_ID, retry["session_id"]])
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert tasks[support.PARENT_ID]["status"] == ledger.STATUS_FINISHED
    assert tasks[support.CHILD_ID]["status"] == ledger.STATUS_FINISHED


def test_failed_dependent_does_not_block_later_sibling(
    installed_launch_agent, ledger_path, monkeypatch
):
    support.create_waiting_chain()
    prompts: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        prompts.append(task["prompt"])
        return (
            int(task["prompt"] == support.CHILD_PROMPT),
            json.dumps({"result": support.CLAUDE_RESULT}),
            "",
        )

    monkeypatch.setattr(claude, "run_claude", run_claude)
    parent = support.read_task(ledger_path, support.PARENT_ID)
    support.set_task_due_now(ledger_path, support.PARENT_ID)
    cli.main(["_execute", support.PARENT_ID, parent["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert prompts == [support.PARENT_PROMPT, support.CHILD_PROMPT, support.SIBLING_PROMPT]
    assert tasks[support.CHILD_ID]["status"] == ledger.STATUS_FAILED
    assert tasks[support.SIBLING_ID]["status"] == ledger.STATUS_FINISHED
