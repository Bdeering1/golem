import pytest

from golem import cli
from tests import support


@pytest.mark.parametrize("state_home", [None, "", "relative/state"])
def test_ledger_falls_back_to_home_when_xdg_state_home_is_not_absolute(
    fake_commands, monkeypatch, tmp_path, state_home
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if state_home is not None:
        monkeypatch.setenv("XDG_STATE_HOME", state_home)
    cli.main(["agent", "install"])

    assert cli.main(["run", support.TASK_ID, support.PROMPT, "--now"]) == 0
    assert (tmp_path / ".local" / "state" / "golem" / "tasks.json").exists()
