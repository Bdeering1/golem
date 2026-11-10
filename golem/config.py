import tomllib
from pathlib import Path

from golem import ledger


def config_path() -> Path:
    return ledger.xdg_golem_directory("XDG_CONFIG_HOME", Path.home() / ".config") / "config.toml"


def invalid_config_message_prefix() -> str:
    return f"golem: invalid config file {config_path()}"


def load_config() -> dict[str, object]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise SystemExit(f"{invalid_config_message_prefix()}: {error}") from error
