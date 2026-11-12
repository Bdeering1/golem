import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from golem import cli, ledger, t3_code
from tests import support, t3_support


@pytest.fixture(scope="session")
def fake_bin_path(tmp_path_factory):
    command_path = tmp_path_factory.mktemp("bin")
    for name, script in {
        "claude": """#!/bin/sh
if [ "$1" = auth ]; then
    printf '%s\\n' "$2" >> "$GOLEM_CLAUDE_AUTH"
    credentials="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
    case "$2" in
        status)
            if [ -f "$credentials" ]; then cat "$credentials"; exit 0; fi
            printf '{"loggedIn": false}'
            exit 1 ;;
        login)
            if [ -n "${GOLEM_CLAUDE_LOGIN-}" ]; then cp "$GOLEM_CLAUDE_LOGIN" "$credentials"; fi
            exit "${GOLEM_CLAUDE_LOGIN_EXIT:-0}" ;;
        logout)
            if [ "${GOLEM_CLAUDE_LOGOUT_EXIT:-0}" != 0 ]; then exit "$GOLEM_CLAUDE_LOGOUT_EXIT"; fi
            rm -f "$credentials"
            exit 0 ;;
    esac
fi
printf '%s|%s|%s\\n' "${CLAUDE_CONFIG_DIR-}" "${CLAUDE_CODE_OAUTH_TOKEN+token}" \\
    "${ANTHROPIC_API_KEY+token}" >> "$GOLEM_CLAUDE_ACCOUNT"
printf '\036\\n' >> "$GOLEM_CLAUDE_ARGS"
printf '%s\\n' "$@" >> "$GOLEM_CLAUDE_ARGS"
printf '%s\\n' "$PWD" >> "$GOLEM_CLAUDE_CWD"
printf '%s\\n' "${LANG-}" >> "$GOLEM_CLAUDE_LANG"
printf '%s\\n' "${LC_TIME-}" >> "$GOLEM_CLAUDE_LC_TIME"
if [ -n "${UNRELATED+x}" ]; then
    printf '%s\\n' "$UNRELATED" >> "$GOLEM_CLAUDE_UNRELATED"
fi
worktree=''
task_name=''
next=''
for argument in "$@"; do
    if [ -n "$next" ]; then
        if [ "$next" = worktree ]; then
            worktree="$argument"
        else
            task_name="$argument"
        fi
        next=''
        continue
    fi
    case "$argument" in
        --worktree) next=worktree ;;
        --worktree=*) worktree="${argument#--worktree=}" ;;
        --name) next=name ;;
        --name=*) task_name="${argument#--name=}" ;;
    esac
done
if [ -n "$worktree" ]; then
    common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
    if [ -n "$common" ]; then
        mkdir -p "$(dirname "$common")/.claude/worktrees/$worktree"
    fi
fi
if [ -n "$GOLEM_CLAUDE_STARTED" ]; then
    : > "$GOLEM_CLAUDE_STARTED"
fi
if [ -n "$GOLEM_CLAUDE_STRAY" ]; then
    (while [ -e "$GOLEM_CLAUDE_STRAY" ]; do sleep 0.05; done) < /dev/null > /dev/null 2>&1 &
fi
if [ -n "${GOLEM_CLAUDE_BACKGROUND_STOP-}" ]; then
    ceiling="${GOLEM_CLAUDE_SETTINGS_CEILING-${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS-600000}}"
    if [ "$ceiling" != 0 ]; then
        printf '%s\\n' "$GOLEM_CLAUDE_BACKGROUND_STOP" >&2
    fi
fi
if [ -f "$GOLEM_CLAUDE_STDOUT" ]; then
    cat "$GOLEM_CLAUDE_STDOUT"
else
    printf '%s' '{"result": "done"}'
fi
while [ -n "$GOLEM_CLAUDE_HOLD" ] && [ -e "$GOLEM_CLAUDE_HOLD" ]; do
    sleep 0.05
done
if [ -n "${GOLEM_CLAUDE_FAIL_TASK:-}" ] && [ "$task_name" = "$GOLEM_CLAUDE_FAIL_TASK" ]; then
    exit 1
fi
exit "${GOLEM_CLAUDE_EXIT:-0}"
""",
        "codex": f"""#!{sys.executable}
import json
import os
import sys
import time

prefix = os.environ["GOLEM_CODEX_"]

def record(name, value):
    with open(prefix + name, "a", encoding="utf-8") as output:
        output.write(value)

record("args", "\\x1e\\n" + "\\n".join(sys.argv[1:]) + "\\n")
record("cwd", os.getcwd() + "\\n")
record("api-keys", f"{{int('OPENAI_API_KEY' in os.environ)}} {{int('CODEX_API_KEY' in os.environ)}}\\n")
record("home", os.environ.get("CODEX_HOME", "") + "\\n")

if sys.argv[1:2] == ["app-server"]:
    account = {{"account": {{"type": "chatgpt"}}, "requiresOpenaiAuth": True}}
    rate_limits = {{"rateLimits": {{"primary": {{"usedPercent": 0, "resetsAt": None}}, "secondary": None}}}}
    account_path = os.environ.get("GOLEM_CODEX_ACCOUNT")
    limits_path = os.environ.get("GOLEM_CODEX_RATE_LIMITS")
    if account_path:
        account = json.load(open(account_path, encoding="utf-8"))
    if limits_path:
        rate_limits = json.load(open(limits_path, encoding="utf-8"))
    batch_responses = []
    for line in sys.stdin:
        record("stdin", line)
        request = json.loads(line)
        if "id" not in request:
            continue
        if os.environ.get("GOLEM_CODEX_ERROR"):
            response = {{"id": request["id"], "error": {{"message": "unavailable"}}}}
        elif request["method"] == "account/read":
            response = {{"id": request["id"], "result": account}}
        elif request["method"] == "account/rateLimits/read":
            response = {{"id": request["id"], "result": rate_limits}}
        else:
            response = {{"id": request["id"], "result": {{}}}}
        if os.environ.get("GOLEM_CODEX_BATCH_RESPONSES"):
            batch_responses.append(json.dumps(response))
            if request["id"] != 3:
                continue
            sys.stdout.write("\\n".join(batch_responses) + "\\n")
            sys.stdout.flush()
            continue
        print(json.dumps(response), flush=True)
    sys.exit(0)

if sys.argv[1:2] == ["resume"]:
    sys.exit(0)

standard_input = sys.stdin.read()
record("stdin", standard_input + "\\x1e\\n")
events_path = os.environ.get("GOLEM_CODEX_EVENTS")
if events_path:
    sys.stdout.write(open(events_path, encoding="utf-8").read())
else:
    print(json.dumps({{"type": "thread.started", "thread_id": "thread"}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "done"}}}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{}}}}))
sys.stdout.flush()
while os.environ.get("GOLEM_CODEX_HOLD") and os.path.exists(os.environ["GOLEM_CODEX_HOLD"]):
    time.sleep(0.05)
sys.exit(int(os.environ.get("GOLEM_CODEX_EXIT", "0")))
""",
        "launchctl": """#!/bin/sh
printf '%s\\n' "$*" >> "$GOLEM_LAUNCHCTL_ARGS"
case "$1" in
    bootstrap) exit "${GOLEM_LAUNCHCTL_BOOTSTRAP_EXIT:-0}" ;;
    kickstart) exit "${GOLEM_LAUNCHCTL_KICKSTART_EXIT:-0}" ;;
esac
""",
        "open": """#!/bin/sh
printf '%s\\n' "$*" >> "$GOLEM_OPEN_ARGS"
exit "${GOLEM_OPEN_EXIT:-0}"
""",
        "osascript": """#!/bin/sh
printf '\036\\n' >> "$GOLEM_OSASCRIPT_ARGS"
printf '%s\\n' "$@" >> "$GOLEM_OSASCRIPT_ARGS"
""",
        "gh": """#!/bin/sh
if [ "$1" = repo ]; then
    url=$(git remote get-url origin) || exit 1
    name=${url##*/}
    printf 'owner/%s\\nmain\\n' "${name%.git}"
    exit 0
fi
head=''
state=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        --head) head="$2"; shift ;;
        --state) state="$2"; shift ;;
    esac
    shift
done
pull_requests_file="$GOLEM_GH_PULL_REQUESTS/$head.json"
if [ "$state" = all ] && [ -f "$pull_requests_file" ]; then
    cat "$pull_requests_file"
else
    printf '[]'
fi
""",
    }.items():
        path = command_path / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    return command_path


@pytest.fixture(autouse=True)
def fake_commands(monkeypatch, tmp_path, fake_bin_path):
    paths = {
        name: tmp_path / name
        for name in (
            "claude-account",
            "claude-args",
            "claude-auth",
            "claude-cwd",
            "claude-lang",
            "claude-lc-time",
            "claude-stdout",
            "claude-unrelated",
            "codex-args",
            "codex-cwd",
            "codex-stdin",
            "codex-api-keys",
            "codex-home",
            "launchctl-args",
            "open-args",
            "osascript-args",
        )
    }
    for path in paths.values():
        path.touch()
    paths["gh-pull-requests"] = tmp_path / "gh-pull-requests"
    paths["gh-pull-requests"].mkdir()
    home_path = tmp_path / "home"
    state_path = tmp_path / "state"
    config_path = tmp_path / "config"
    work_path = tmp_path / "work"
    work_path.mkdir()
    monkeypatch.setenv("PATH", f"{fake_bin_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_path))
    for name, path in paths.items():
        monkeypatch.setenv(f"GOLEM_{name.upper().replace('-', '_')}", str(path))
    monkeypatch.setenv("GOLEM_CODEX_", str(tmp_path / "codex-"))
    monkeypatch.chdir(work_path)
    return paths


@pytest.fixture
def write_pull_requests(fake_commands):
    def write(branch: str, pull_requests: list[dict[str, object]]) -> None:
        (fake_commands["gh-pull-requests"] / f"{branch}.json").write_text(
            json.dumps(pull_requests), encoding="utf-8"
        )

    return write


@pytest.fixture
def ledger_path():
    return ledger.ledger_path()


@pytest.fixture
def installed_launch_agent():
    assert cli.main(["agent", "install"]) == 0


@pytest.fixture
def owned_worktree(git_repository, ledger_path):
    def create_worktree(
        status: str = ledger.STATUS_DONE, *, released_at: str | None = None
    ) -> Path:
        worktree = support.git_worktree(git_repository)
        support.write_task(ledger_path, status, worktree=str(worktree))
        support.write_worktrees(
            support.golem_worktree_resource(git_repository, worktree, released_at=released_at)
        )
        return worktree

    return create_worktree


@pytest.fixture(scope="session")
def git_repository_template(tmp_path_factory):
    repository_path = tmp_path_factory.mktemp("repository")
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository_path, check=True)
    (repository_path / "README").write_text("test\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "add", "README"], cwd=repository_path, check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "commit",
            "--no-gpg-sign",
            "-qm",
            "test",
        ],
        cwd=repository_path,
        check=True,
    )
    return repository_path


@pytest.fixture
def git_repository(tmp_path, git_repository_template):
    repository_path = tmp_path / "repository"
    shutil.copytree(git_repository_template, repository_path)
    return repository_path


@pytest.fixture
def server(monkeypatch, tmp_path):
    home = tmp_path / "t3"
    (home / "bin").mkdir(parents=True)
    (home / "userdata").mkdir()
    (home / "caches").mkdir()
    cli_path = home / "bin" / "t3"
    cli_path.write_text(t3_support.FAKE_T3, encoding="utf-8")
    cli_path.chmod(0o755)
    (home / "userdata" / "environment-id").write_text(
        f"{t3_support.ENVIRONMENT_ID}\n", encoding="utf-8"
    )
    monkeypatch.setenv("T3CODE_HOME", str(home))
    fake_server = t3_support.FakeServer(home, project_root=str(Path.cwd()))
    fake_server.mark_running()
    monkeypatch.setattr(t3_code, "fetch_projects", fake_server.fetch_projects)
    monkeypatch.setattr(t3_code, "dispatch_command_exit", fake_server.dispatch_command_exit)
    monkeypatch.setattr(t3_code, "update_settings_exit", fake_server.update_settings_exit)
    monkeypatch.setattr(t3_code, "thread_projection", fake_server.thread_projection)
    return fake_server
