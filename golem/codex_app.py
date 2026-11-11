import plistlib
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

from golem.backends import codex

if TYPE_CHECKING:
    from golem import ledger

APP_BUNDLE_ID = "com.openai.codex"
APP_NAMES = ("ChatGPT.app", "Codex.app")
LINK_INTERVAL_SECONDS = 2
OPEN_TIMEOUT_SECONDS = 90


class CodexAppError(Exception):
    pass


def application_directories() -> tuple[Path, ...]:
    return (Path("/Applications"), Path.home() / "Applications")


def info_plist(app: Path) -> dict[str, object]:
    with (app / "Contents" / "Info.plist").open("rb") as file:
        return cast("dict[str, object]", plistlib.load(file))


def app_path() -> Path | None:
    for directory in application_directories():
        for name in APP_NAMES:
            app = directory / name
            plist_path = app / "Contents" / "Info.plist"
            if plist_path.exists() and info_plist(app).get("CFBundleIdentifier") == APP_BUNDLE_ID:
                return app
    return None


def version(app: Path) -> str:
    return str(info_plist(app)["CFBundleShortVersionString"])


def thread_link(conversation_id: str) -> str:
    return f"codex://threads/{conversation_id}"


def lock_holder_paths(lock_path: Path) -> list[str]:
    if not lock_path.exists():
        return []
    holders = subprocess.run(
        ["lsof", "-t", str(lock_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    process_ids = holders.stdout.split()
    if not process_ids:
        return []
    commands = subprocess.run(
        ["ps", "-o", "comm=", "-p", ",".join(process_ids)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    return commands.stdout.splitlines()


def open_returncode(*arguments: str) -> int:
    return subprocess.run(
        ["open", *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        encoding="utf-8",
    ).returncode


def start_app() -> None:
    if open_returncode("-b", APP_BUNDLE_ID):
        raise CodexAppError(f"could not start {APP_BUNDLE_ID}")


def wait_for_thread(app: Path, conversation_id: str, lock_path: Path) -> None:
    deadline = time.monotonic() + OPEN_TIMEOUT_SECONDS
    app_prefix = f"{app.resolve()}/"
    while True:
        holders = lock_holder_paths(lock_path)
        if any(holder.startswith(app_prefix) for holder in holders):
            return
        if holders:
            raise CodexAppError(f"it is open in another Codex client: {holders[0]}")
        if time.monotonic() >= deadline:
            raise CodexAppError(f"it did not open within {OPEN_TIMEOUT_SECONDS}s")
        if open_returncode(thread_link(conversation_id)):
            raise CodexAppError(f"could not open {thread_link(conversation_id)}")
        time.sleep(LINK_INTERVAL_SECONDS)


def prepare_thread(task: ledger.Task, conversation_id: str) -> None:
    app = app_path()
    if app is None:
        raise SystemExit(
            "golem: Codex app is not installed: no com.openai.codex app in /Applications or ~/Applications"
        )
    try:
        start_app()
        lock_path = codex.codex_home(task) / "thread-writer-locks" / f"{conversation_id}.lock"
        wait_for_thread(app, conversation_id, lock_path)
    except (CodexAppError, OSError) as error:
        raise SystemExit(
            f"golem: Codex app {version(app)} could not open thread {conversation_id}: {error}"
        ) from error
