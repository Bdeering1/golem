import plistlib
from pathlib import Path

import pytest

from golem import cli, clients, codex_app, ledger
from tests import support

APP_VERSION = "1.2.3"
THREAD_ID = "codex-thread"


@pytest.fixture
def codex_app_bundle(tmp_path):
    def create(bundle_id: str = codex_app.APP_BUNDLE_ID) -> Path:
        app = tmp_path / "Applications" / "ChatGPT.app"
        plist_path = app / "Contents" / "Info.plist"
        plist_path.parent.mkdir(parents=True)
        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleShortVersionString": APP_VERSION,
                }
            )
        )
        return app

    return create


def sent_links(fake_commands) -> list[str]:
    return [
        arguments
        for arguments in fake_commands["open-args"].read_text(encoding="utf-8").splitlines()
        if arguments.startswith("codex://")
    ]


def set_app_lock_holder(monkeypatch, fake_commands, app: Path, link_count=1):
    app_server = app / "Contents" / "Resources" / "codex-cli" / "codex"

    def lock_holder_paths(_lock_path: Path) -> list[str]:
        return [str(app_server)] if len(sent_links(fake_commands)) >= link_count else []

    monkeypatch.setattr(codex_app, "lock_holder_paths", lock_holder_paths)
    monkeypatch.setattr(codex_app, "LINK_INTERVAL_SECONDS", 0)


def write_codex_task(ledger_path):
    support.write_task(
        ledger_path,
        ledger.STATUS_NEEDS_REVIEW,
        backend=ledger.BACKEND_CODEX,
        thread_id=THREAD_ID,
    )


@pytest.mark.parametrize(
    "link_count",
    [pytest.param(1, id="running"), pytest.param(3, id="starting")],
)
def test_pickup_opens_codex_thread_in_app(
    fake_commands, ledger_path, monkeypatch, codex_app_bundle, link_count
):
    app = codex_app_bundle()
    set_app_lock_holder(monkeypatch, fake_commands, app, link_count)
    write_codex_task(ledger_path)

    assert cli.main(["pickup", support.TASK_ID, "--client", clients.CODEX_APP_CLIENT]) == 0

    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines() == [
        f"-b {codex_app.APP_BUNDLE_ID}",
        *[codex_app.thread_link(THREAD_ID)] * (link_count + 1),
    ]
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_pickup_all_uses_codex_app_default(
    fake_commands, ledger_path, monkeypatch, codex_app_bundle
):
    app = codex_app_bundle()
    set_app_lock_holder(monkeypatch, fake_commands, app)
    write_codex_task(ledger_path)

    assert cli.main(["pickup", "--all"]) == 0

    assert codex_app.thread_link(THREAD_ID) in sent_links(fake_commands)
    assert support.read_task(ledger_path)["status"] == ledger.STATUS_DONE


def test_auto_pickup_records_and_uses_codex_app(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, codex_app_bundle
):
    app = codex_app_bundle()
    set_app_lock_holder(monkeypatch, fake_commands, app)

    assert (
        cli.main(
            [
                "run",
                support.TASK_ID,
                support.PROMPT,
                "--backend",
                ledger.BACKEND_CODEX,
                "--now",
                "--auto-pickup",
            ]
        )
        == 0
    )
    task = support.read_task(ledger_path)
    assert task["auto_pickup_client"] == clients.CODEX_APP_CLIENT

    assert cli.main(["_execute", support.TASK_ID, task["session_id"]]) == 0
    completed_task = support.read_task(ledger_path)
    assert completed_task["status"] == ledger.STATUS_DONE
    assert codex_app.thread_link(completed_task["thread_id"]) in sent_links(fake_commands)


def remove_app(_monkeypatch, _fake_commands, app):
    (app / "Contents" / "Info.plist").unlink()


def add_wrong_app(_monkeypatch, _fake_commands, app):
    plist_path = app / "Contents" / "Info.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.openai.chat",
                "CFBundleShortVersionString": APP_VERSION,
            }
        )
    )


def add_other_lock_holder(monkeypatch, _fake_commands, _app):
    monkeypatch.setattr(codex_app, "lock_holder_paths", lambda _lock_path: ["/usr/local/bin/codex"])


def add_timeout(monkeypatch, _fake_commands, _app):
    monkeypatch.setattr(codex_app, "lock_holder_paths", lambda _lock_path: [])
    monkeypatch.setattr(codex_app, "OPEN_TIMEOUT_SECONDS", 0)


@pytest.mark.parametrize(
    ("break_codex_app", "message"),
    [
        pytest.param(remove_app, "Codex app is not installed", id="not-installed"),
        pytest.param(add_wrong_app, "Codex app is not installed", id="wrong-bundle"),
        pytest.param(
            add_other_lock_holder,
            f"Codex app {APP_VERSION} .*another Codex client",
            id="other-client",
        ),
        pytest.param(add_timeout, f"Codex app {APP_VERSION} .*did not open", id="timeout"),
    ],
)
def test_failed_pickup_leaves_codex_task_needing_review(
    fake_commands,
    ledger_path,
    monkeypatch,
    codex_app_bundle,
    break_codex_app,
    message,
):
    app = codex_app_bundle()
    break_codex_app(monkeypatch, fake_commands, app)
    write_codex_task(ledger_path)

    with pytest.raises(SystemExit, match=message):
        cli.main(["pickup", support.TASK_ID, "--client", clients.CODEX_APP_CLIENT])

    assert support.read_task(ledger_path)["status"] == ledger.STATUS_NEEDS_REVIEW
    assert sent_links(fake_commands) == []
