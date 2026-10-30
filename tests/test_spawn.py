import subprocess

from golem import cli, launch_agent, ledger, spawn
from tests import support


def test_launch_agent_executor_uses_only_saved_environment(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("LANG", support.SAVED_LANG)
    monkeypatch.setenv("LC_TIME", support.CHILD_LANG)
    monkeypatch.setenv("UNRELATED", "not-saved")
    cli.main(["run", support.TASK_ID, support.PROMPT, "--now"])
    monkeypatch.setenv("LANG", support.CURRENT_LANG)
    monkeypatch.setenv("LC_TIME", support.CURRENT_LANG)
    monkeypatch.delenv("UNRELATED")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    start_task_executor = spawn.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: ledger.Task) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    monkeypatch.setattr(spawn, "start_task_executor", record_executor)

    launch_agent.run_launch_agent_pass()

    assert len(processes) == 1
    processes[0].wait()
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert fake_commands["claude-lang"].read_text(encoding="utf-8") == f"{support.SAVED_LANG}\n"
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8") == f"{support.CHILD_LANG}\n"
    assert fake_commands["claude-unrelated"].read_text(encoding="utf-8") == ""
