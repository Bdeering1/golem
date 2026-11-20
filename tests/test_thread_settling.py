import pytest

from golem import cli, launch_agent, ledger, spawn, t3_code, thread_settling
from tests import support, t3_support

PARENT_FINISHED_AT = "2030-01-01T10:00:00+00:00"
RUN_BEFORE_FINISH = {"id": "run-1", "ordinal": 1, "startedAt": "2030-01-01T09:00:00.000Z"}
RUN_AFTER_FINISH = {"id": "run-2", "ordinal": 2, "startedAt": "2030-01-01T11:00:00.000Z"}
SETTLE_COMMAND = {
    "type": "thread.settle",
    "threadId": t3_support.CLAUDE_THREAD_ID,
    "expectedLatestRunId": RUN_BEFORE_FINISH["id"],
}
PENDING = {"t3_code_thread_settle": ledger.THREAD_SETTLE_PENDING}


def write_chain(
    ledger_path,
    *,
    parent_fields=None,
    child_status=ledger.STATUS_WAITING,
    waiting_task_parents=None,
    **child_fields: object,
):
    tasks = {
        support.PARENT_ID: support.task_record(
            ledger.STATUS_FINISHED,
            task_id=support.PARENT_ID,
            t3_code_thread_id=t3_support.CLAUDE_THREAD_ID,
            finished_at=PARENT_FINISHED_AT,
            **(parent_fields or {}),
        ),
        support.CHILD_ID: support.task_record(
            child_status, task_id=support.CHILD_ID, after=support.PARENT_ID, **child_fields
        ),
    }
    for task_id, parent_id in (waiting_task_parents or {}).items():
        tasks[task_id] = support.task_record(
            ledger.STATUS_WAITING, task_id=task_id, after=parent_id
        )
    support.write_tasks(ledger_path, tasks)


def run_parent_executor():
    assert cli.main(["_execute", support.PARENT_ID, support.PARENT_SESSION]) == 0


def parent_settle_state(ledger_path):
    return support.read_task(ledger_path, support.PARENT_ID).get("t3_code_thread_settle")


@pytest.fixture
def server(server):
    server.projections[0] = {"thread": {}, "runs": [RUN_BEFORE_FINISH]}
    return server


@pytest.mark.parametrize(
    ("claude_exit", "gate_exit"),
    [
        pytest.param("1", 0, id="failed"),
        pytest.param("0", 1, id="gate-blocked"),
    ],
)
def test_unsuccessful_dependent_leaves_parents_thread(
    server, ledger_path, monkeypatch, tmp_path, claude_exit, gate_exit
):
    write_chain(
        ledger_path,
        gate=support.gate_command(tmp_path, exit_code=gate_exit),
        waiting_task_parents={support.GRANDCHILD_ID: support.CHILD_ID},
    )
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", claude_exit)

    run_parent_executor()

    assert server.commands == []
    assert parent_settle_state(ledger_path) is None


def test_dependent_finished_by_continue_settles_parents_thread(server, ledger_path, monkeypatch):
    write_chain(
        ledger_path,
        child_status=ledger.STATUS_BLOCKED,
        gate="exit 1",
        gate_stop_reason=support.GATE_STOP_REASON,
    )
    monkeypatch.setattr(spawn, "start_task_executor", lambda *_, **__: None)

    assert cli.main(["continue", support.CHILD_ID, "--force"]) == 0
    assert cli.main(["_execute", support.CHILD_ID, f"{support.CHILD_ID}-session"]) == 0

    assert t3_support.sent_commands(server) == [SETTLE_COMMAND]


@pytest.mark.parametrize(
    "failing_task_id",
    [
        pytest.param("", id="both-finish"),
        pytest.param(support.CHILD_ID, id="one-fails"),
    ],
)
def test_finished_dependent_settles_parents_thread_once(
    server, ledger_path, monkeypatch, failing_task_id
):
    write_chain(ledger_path, waiting_task_parents={support.SIBLING_ID: support.PARENT_ID})
    monkeypatch.setenv("GOLEM_CLAUDE_FAIL_TASK", failing_task_id)

    run_parent_executor()

    assert t3_support.sent_commands(server) == [SETTLE_COMMAND]
    assert parent_settle_state(ledger_path) == ledger.THREAD_SETTLE_DONE


def test_picked_up_thread_without_runs_is_settled(server, ledger_path):
    write_chain(ledger_path, parent_fields=PENDING)
    server.projections[0] = {"thread": {}, "runs": []}

    thread_settling.settle_pending_threads()

    assert t3_support.sent_commands(server) == [
        {"type": "thread.settle", "threadId": t3_support.CLAUDE_THREAD_ID}
    ]


def raise_missing_thread(*_: object):
    raise t3_code.T3CodeError("Thread not found")


@pytest.mark.parametrize(
    ("thread", "runs"),
    [
        pytest.param({"pinnedAt": "2030-01-01"}, [], id="pinned"),
        pytest.param({"snoozedUntil": "2030-01-02"}, [], id="snoozed"),
        pytest.param({"archivedAt": "2030-01-01"}, [], id="archived"),
        pytest.param({"deletedAt": "2030-01-01"}, [], id="deleted"),
        pytest.param({"settledOverride": "unsettled"}, [], id="unsettled-by-user"),
        pytest.param(None, [], id="missing"),
        pytest.param({}, [RUN_BEFORE_FINISH, RUN_AFTER_FINISH], id="message-after-finish"),
        pytest.param({}, [{"id": "run-1", "ordinal": 1, "startedAt": None}], id="run-not-started"),
    ],
)
def test_unsettleable_thread_is_skipped_silently(
    server, ledger_path, monkeypatch, capsys, thread, runs
):
    write_chain(ledger_path, parent_fields=PENDING)
    if thread is None:
        monkeypatch.setattr(t3_code, "thread_projection", raise_missing_thread)
    else:
        server.projections[0] = {"thread": thread, "runs": runs}

    thread_settling.settle_pending_threads()

    assert server.commands == []
    assert parent_settle_state(ledger_path) == ledger.THREAD_SETTLE_DONE
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("message", "expected_error"),
    [
        pytest.param(t3_code.SETTLE_RACE_MESSAGE, "", id="run-before-settlement"),
        pytest.param(
            "rejected",
            f"golem: could not settle T3 Code thread {t3_support.CLAUDE_THREAD_ID}: rejected\n",
            id="other-failure",
        ),
    ],
)
def test_rejected_settle_is_not_retried(server, ledger_path, capsys, message, expected_error):
    write_chain(ledger_path, parent_fields=PENDING)
    server.command_exit = {"_tag": "Failure", "cause": {"error": {"message": message}}}

    thread_settling.settle_pending_threads()

    assert capsys.readouterr().err == expected_error
    assert parent_settle_state(ledger_path) == ledger.THREAD_SETTLE_DONE


def raise_connection_error(*_: object):
    raise ConnectionRefusedError


def raise_token_error(*_: object):
    raise t3_code.T3CodeError("t3 auth session issue failed")


def stop_server(server, _monkeypatch):
    server.runtime_path.unlink()


def fail_token_issue(_server, monkeypatch):
    monkeypatch.setattr(t3_code, "issue_token", raise_token_error)


def refuse_projection(_server, monkeypatch):
    monkeypatch.setattr(t3_code, "thread_projection", raise_connection_error)


@pytest.mark.parametrize(
    "make_unreachable",
    [
        pytest.param(stop_server, id="not-running"),
        pytest.param(fail_token_issue, id="token-error"),
        pytest.param(refuse_projection, id="connection-error"),
    ],
)
def test_unreachable_t3_code_is_retried_by_launch_agent(
    server, ledger_path, monkeypatch, capsys, make_unreachable
):
    write_chain(ledger_path, parent_fields=PENDING)
    with monkeypatch.context() as unreachable_patch:
        make_unreachable(server, unreachable_patch)
        thread_settling.settle_pending_threads()

    assert server.commands == []
    assert parent_settle_state(ledger_path) == ledger.THREAD_SETTLE_PENDING
    assert capsys.readouterr().err == ""
    server.mark_running()

    launch_agent.refresh_chain_states()

    assert t3_support.sent_commands(server) == [SETTLE_COMMAND]
    assert parent_settle_state(ledger_path) == ledger.THREAD_SETTLE_DONE
