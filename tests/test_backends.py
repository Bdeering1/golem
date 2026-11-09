from datetime import datetime
from pathlib import Path

import pytest

from golem import cli, ledger
from golem.backends import common, registry
from tests import support

STUB_BACKEND_ID = "stub"
STUB_RESULT = "stub finished"


class StubBackend:
    NAME: str = "Stub"
    IS_GATE_SUPPORTED: bool = False

    def __init__(self) -> None:
        self.outcomes: list[common.RunOutcome] = []
        self.run_task_ids: list[str] = []
        self.validated_agent_args: list[list[str]] = []

    def run(
        self, task_id: str, task: ledger.Task, lock_file_descriptors: tuple[int, ...]
    ) -> common.RunOutcome:
        self.run_task_ids.append(task_id)
        return self.outcomes.pop(0)

    def next_reset_time(self) -> datetime | None:
        return support.RESET_TIME

    def validate_run_arguments(self, agent_args: list[str]) -> None:
        self.validated_agent_args.append(agent_args)

    def conversation_id(self, task: ledger.Task) -> str | None:
        return f"stub-{task['session_id']}"

    def transcript_paths(self, task: ledger.Task) -> list[Path]:
        return []


@pytest.fixture
def stub_backend(monkeypatch):
    backend = StubBackend()
    registered_backend: common.Backend = backend
    monkeypatch.setitem(registry.BACKENDS, STUB_BACKEND_ID, registered_backend)
    return backend


def execute(ledger_path, task_id):
    support.set_task_due_now(ledger_path, task_id)
    return cli.main(["_execute", task_id, support.read_task(ledger_path, task_id)["session_id"]])


def test_registered_backend_runs_through_task_lifecycle(
    installed_launch_agent, ledger_path, stub_backend, capsys
):
    stub_backend.outcomes = [
        common.RunOutcome(1, support.USAGE_LIMIT_RESULT, is_usage_limit=True),
        common.RunOutcome(0, STUB_RESULT, is_usage_limit=False),
        common.RunOutcome(1, None, is_usage_limit=False),
    ]
    assert (
        cli.main(
            [
                *["run", support.PARENT_ID, support.PARENT_PROMPT, "--backend", STUB_BACKEND_ID],
                *["--then", support.CHILD_ID, support.CHILD_PROMPT, "--", *support.AGENT_ARGS],
            ]
        )
        == 0
    )
    assert stub_backend.validated_agent_args == [support.AGENT_ARGS]
    assert support.read_task(ledger_path, support.CHILD_ID)["backend"] == STUB_BACKEND_ID

    assert execute(ledger_path, support.PARENT_ID) == 0
    parent = support.read_task(ledger_path, support.PARENT_ID)
    assert parent["status"] == ledger.STATUS_SCHEDULED
    assert datetime.fromisoformat(parent["due_at"]) == support.RESET_TIME

    assert execute(ledger_path, support.PARENT_ID) == 0
    assert stub_backend.run_task_ids == [support.PARENT_ID, support.PARENT_ID, support.CHILD_ID]
    assert support.read_task(ledger_path, support.CHILD_ID)["status"] == ledger.STATUS_FAILED
    capsys.readouterr()
    assert cli.main(["show", support.PARENT_ID]) == 0
    assert STUB_RESULT in capsys.readouterr().out

    assert cli.main(["retry", support.CHILD_ID]) == 0
    child = support.read_task(ledger_path, support.CHILD_ID)
    assert datetime.fromisoformat(child["due_at"]) == support.RESET_TIME

    capsys.readouterr()
    assert cli.main(["delete", support.CHILD_ID]) == 0
    assert f"golem: session: stub-{child['session_id']}" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        (["--auto-pickup"], "--auto-pickup requires a default app client for Stub tasks"),
        (["--gate", "true"], "--gate does not support Stub tasks"),
    ],
)
def test_registered_backend_refuses_unsupported_run_options(stub_backend, flag, message):
    with pytest.raises(SystemExit, match=message):
        cli.main(["run", support.TASK_ID, support.PROMPT, "--backend", STUB_BACKEND_ID, *flag])
