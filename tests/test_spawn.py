import subprocess
import sys

from golem import cli, launch_agent, ledger, spawn
from tests import support


def test_launch_agent_executor_uses_only_saved_environment(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("LANG", support.SAVED_LANG)
    monkeypatch.setenv("LC_TIME", support.CHILD_LANG)
    monkeypatch.setenv("UNRELATED", "not-saved")
    cli.main(["run", support.PROMPT, "--now"])
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
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED
    assert fake_commands["claude-lang"].read_text(encoding="utf-8") == f"{support.SAVED_LANG}\n"
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8") == f"{support.CHILD_LANG}\n"
    assert fake_commands["claude-unrelated"].read_text(encoding="utf-8") == ""


def test_golem_command_ignores_golem_package_in_working_directory(tmp_path):
    shadow_package = tmp_path / "golem"
    shadow_package.mkdir()
    (shadow_package / "__init__.py").write_text("", encoding="utf-8")
    (shadow_package / "__main__.py").write_text("print('shadow')\n", encoding="utf-8")

    completed = spawn.subprocess.run(
        [*spawn.golem_command(), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert completed.stdout.startswith("usage: golem")


def test_starting_an_executor_does_not_hold_the_callers_output(
    installed_launch_agent, ledger_path, monkeypatch, tmp_path
):
    hold_path = tmp_path / "hold"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    cli.main(["run", support.PROMPT, "--now"])
    start_executor = (
        "from golem import ledger, spawn; "
        f"spawn.start_task_executor({support.TASK_ID!r}, ledger.load_tasks()[{support.TASK_ID!r}])"
    )

    try:
        spawn.subprocess.run(
            [sys.executable, "-c", start_executor], capture_output=True, check=True, timeout=10
        )
    finally:
        hold_path.unlink()

    support.wait_until(lambda: support.read_task(ledger_path)["status"] == ledger.STATUS_FINISHED)
