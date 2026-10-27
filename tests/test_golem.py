import contextlib
import io
import json
import multiprocessing
import os
import plistlib
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Never, override

import pytest

import golem

TASK_ID = "fix-thing"
PROMPT = "fix the thing"
PARENT_ID = "parent"
CHILD_ID = "child"
GRANDCHILD_ID = "grandchild"
SIBLING_ID = "sibling"
PARENT_PROMPT = "run the parent"
CHILD_PROMPT = "run the child"
GRANDCHILD_PROMPT = "run the grandchild"
SIBLING_PROMPT = "run the sibling"
AGENT_ARGS = ["--model", "haiku"]
AT_TEXT = "9pm"
SKIP_PERMISSIONS = "--dangerously-skip-permissions"
WORKTREE_FLAG = "--worktree"
LAUNCH_AGENT_ERROR_PATTERN = re.escape(golem.LAUNCH_AGENT_ERROR)
SAVED_LANG = "en_CA.UTF-8"
CHILD_LANG = "fr_CA.UTF-8"
CURRENT_LANG = "en_US.UTF-8"
CLAUDE_RESULT = "completed the task"
BACKGROUND_STOP_LINE = (
    'Background tasks still running 10m after the last turn (workflow "Implement plan"); '
    "stopping them. Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely."
)
USAGE_LIMIT_RESULT = "You've hit your limit · resets 3pm"
RESET_TIME = datetime(2030, 1, 1, 10, tzinfo=UTC)
EARLIER_RESET_TIME = datetime(2030, 1, 1, 11, tzinfo=UTC)
LATER_RESET_TIME = datetime(2030, 1, 2, 11, tzinfo=UTC)
ANSI_COLOR = re.compile(r"\033\[38;5;\d+m|\033\[0m")
WORKTREE_BRANCH = "worktree"
DAY_SECONDS = 24 * 60 * 60
GATE_REASON = "no pull request yet"
USER_SETTINGS_MODE = 0o644
PARENT_SESSION = f"{PARENT_ID}-session"
USER_SETTINGS = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "echo user-stop"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "echo user-notify"}]}],
    },
}


@pytest.fixture(scope="session")
def fake_bin_path(tmp_path_factory):
    command_path = tmp_path_factory.mktemp("bin")
    for name, script in {
        "claude": """#!/bin/sh
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
        "gh": """#!/bin/sh
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
            "claude-args",
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
def ledger_path():
    return golem.ledger_path()


@pytest.fixture
def installed_launch_agent():
    assert golem.main(["agent", "install"]) == 0


def read_task(ledger_path, task_id=TASK_ID):
    return json.loads(ledger_path.read_text(encoding="utf-8"))[task_id]


def task_record(status, *, task_id=TASK_ID, **fields: object):
    record = {
        "agent_args": [],
        "cwd": str(Path.cwd()),
        "due_at": datetime.now(UTC).isoformat(),
        "environment": {"PATH": os.environ["PATH"]},
        "prompt": PROMPT,
        "session_id": f"{task_id}-session",
        "status": status,
    }
    record.update(fields)
    return record


def write_task(ledger_path, status, *, task_id=TASK_ID, **fields: object):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({task_id: task_record(status, task_id=task_id, **fields)}), encoding="utf-8"
    )


def write_tasks(ledger_path, tasks):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(tasks), encoding="utf-8")


def write_aliases(aliases):
    path = golem.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[aliases]",
        *(f"{name} = {json.dumps(template)}" for name, template in aliases.items()),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def list_output(capsys):
    assert golem.main(["list"]) == 0
    return capsys.readouterr().out


def list_row(output, task_id):
    return next(line for line in output.splitlines() if line.startswith(f"{task_id} "))


def test_list_groups_visible_tasks_by_status_and_id(ledger_path, capsys):
    tasks = {
        "z-failed": task_record(golem.STATUS_FAILED, task_id="z-failed"),
        "a-failed": task_record(golem.STATUS_FAILED, task_id="a-failed"),
        "scheduled": task_record(golem.STATUS_SCHEDULED, task_id="scheduled"),
        "running": task_record(golem.STATUS_RUNNING, task_id="running"),
        "review": task_record(golem.STATUS_NEEDS_REVIEW, task_id="review"),
        "parent": task_record(golem.STATUS_NEEDS_REVIEW, task_id="parent"),
        "waiting": task_record(golem.STATUS_WAITING, task_id="waiting", after="parent"),
        "done": task_record(golem.STATUS_DONE, task_id="done"),
        "cancelled": task_record(golem.STATUS_CANCELLED, task_id="cancelled"),
    }
    write_tasks(ledger_path, tasks)

    output = list_output(capsys)

    assert [line.split()[0] for line in output.splitlines()[1:]] == [
        "parent",
        "review",
        "a-failed",
        "z-failed",
        "running",
        "scheduled",
        "waiting",
        "done",
        "cancelled",
    ]
    assert list_row(output, "waiting").split()[-1] == "parent"


def test_list_abbreviates_only_home_and_its_descendants(ledger_path, capsys):
    home_directory = Path.home()
    prefix_directory = str(home_directory.with_name(f"{home_directory.name}-other"))
    tasks = {
        "home": task_record(golem.STATUS_DONE, task_id="home", cwd=str(home_directory)),
        "descendant": task_record(
            golem.STATUS_DONE, task_id="descendant", cwd=str(home_directory / "project")
        ),
        "prefix": task_record(golem.STATUS_DONE, task_id="prefix", cwd=prefix_directory),
    }
    write_tasks(ledger_path, tasks)

    output = list_output(capsys)

    assert "  ~" in list_row(output, "home")
    assert "  ~/project" in list_row(output, "descendant")
    assert prefix_directory in list_row(output, "prefix")


def test_list_formats_due_times_in_local_time(ledger_path, capsys, monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        @override
        def now(cls, tz=None) -> datetime:
            now = datetime(2030, 3, 10, 12, tzinfo=UTC)
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(golem, "datetime", FixedDateTime)
    tasks = {
        "today": task_record(
            golem.STATUS_SCHEDULED,
            task_id="today",
            due_at=datetime(2030, 3, 11, 3, 30, tzinfo=UTC).isoformat(),
        ),
        "tomorrow": task_record(
            golem.STATUS_SCHEDULED,
            task_id="tomorrow",
            due_at=datetime(2030, 3, 11, 13, tzinfo=UTC).isoformat(),
        ),
        "daylight": task_record(
            golem.STATUS_SCHEDULED,
            task_id="daylight",
            due_at=datetime(2030, 11, 3, 5, 30, tzinfo=UTC).isoformat(),
        ),
        "standard": task_record(
            golem.STATUS_SCHEDULED,
            task_id="standard",
            due_at=datetime(2030, 11, 3, 6, 30, tzinfo=UTC).isoformat(),
        ),
        "no-due": task_record(golem.STATUS_SCHEDULED, task_id="no-due"),
    }
    tasks["no-due"].pop("due_at")
    write_tasks(ledger_path, tasks)

    original_timezone = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    try:
        output = list_output(capsys)
    finally:
        if original_timezone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    assert "today 11:30pm EDT" in list_row(output, "today")
    assert "tomorrow 9:00am EDT" in list_row(output, "tomorrow")
    assert "Nov 3, 2030 1:30am EDT" in list_row(output, "daylight")
    assert "Nov 3, 2030 1:30am EST" in list_row(output, "standard")
    assert list_row(output, "no-due").endswith(str(Path.cwd()))


def test_list_uses_distinct_status_colors_and_keeps_columns_aligned(
    ledger_path, capsys, monkeypatch
):
    tasks = {
        status: task_record(status, task_id=status)
        for status in golem.LIST_STATUS_ORDER
        if status != golem.STATUS_WAITING
    }
    tasks[golem.STATUS_WAITING] = task_record(
        golem.STATUS_WAITING,
        task_id=golem.STATUS_WAITING,
        after=golem.STATUS_NEEDS_REVIEW,
    )
    write_tasks(ledger_path, tasks)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    colored_output = list_output(capsys)

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    plain_output = list_output(capsys)
    colors = re.findall(r"\033\[38;5;(\d+)m", colored_output)

    assert len(colors) == len(golem.LIST_STATUS_ORDER)
    assert len(set(colors)) == len(golem.LIST_STATUS_ORDER)
    assert ANSI_COLOR.sub("", colored_output) == plain_output


@pytest.mark.parametrize(
    ("is_tty", "term", "no_color"),
    [
        pytest.param(False, "xterm-256color", None, id="redirected"),
        pytest.param(True, "dumb", None, id="dumb-terminal"),
        pytest.param(True, "xterm-256color", "", id="no-color"),
    ],
)
def test_list_suppresses_status_colors(ledger_path, capsys, monkeypatch, is_tty, term, no_color):
    write_task(ledger_path, golem.STATUS_DONE)
    monkeypatch.setenv("TERM", term)
    if no_color is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: is_tty)

    output = list_output(capsys)

    assert ANSI_COLOR.search(output) is None


def write_worktrees(worktrees):
    golem.worktree_ledger_path().write_text(json.dumps(worktrees), encoding="utf-8")


def git_output(repository_path, *arguments: str):
    result = golem.subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=repository_path,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def git_worktree(repository_path):
    git_output(repository_path, "worktree", "add", "-qb", WORKTREE_BRANCH, "worktree")
    return repository_path / "worktree"


def golem_worktree_resource(repository_path, worktree_path, *, released_at=None):
    resource = {
        "owner": golem.WORKTREE_OWNER_GOLEM,
        "repository": golem.git_common_directory(str(repository_path)),
        "session_id": "fix-thing-session",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if released_at is not None:
        resource["released_at"] = released_at
    marker_path = golem.worktree_owner_marker_path(str(worktree_path))
    assert marker_path is not None
    marker_path.write_text("fix-thing-session", encoding="utf-8")
    return {str(worktree_path.resolve()): resource}


@pytest.fixture
def owned_worktree(git_repository, ledger_path):
    def create_worktree(status: str = golem.STATUS_DONE, *, released_at: str | None = None) -> Path:
        worktree = git_worktree(git_repository)
        write_task(ledger_path, status, worktree=str(worktree))
        write_worktrees(golem_worktree_resource(git_repository, worktree, released_at=released_at))
        return worktree

    return create_worktree


def read_tasks(ledger_path):
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def set_task_due_now(ledger_path, task_id=TASK_ID):
    tasks = read_tasks(ledger_path)
    tasks[task_id]["due_at"] = datetime.now(UTC).isoformat()
    write_tasks(ledger_path, tasks)


def logged_claude_calls(fake_commands):
    calls: list[list[str]] = []
    for call in fake_commands["claude-args"].read_text(encoding="utf-8").split("\x1e\n"):
        arguments = call.splitlines()
        if arguments:
            calls.append(arguments)
    return calls


def logged_claude_args(fake_commands):
    calls = logged_claude_calls(fake_commands)
    return calls[-1] if calls else []


def logged_codex_calls(fake_commands):
    calls: list[list[str]] = []
    for call in fake_commands["codex-args"].read_text(encoding="utf-8").split("\x1e\n"):
        arguments = call.splitlines()
        if arguments:
            calls.append(arguments)
    return calls


def logged_codex_args(fake_commands):
    calls = logged_codex_calls(fake_commands)
    return calls[-1] if calls else []


def write_codex_events(monkeypatch, tmp_path, events):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    monkeypatch.setenv("GOLEM_CODEX_EVENTS", str(events_path))
    return events_path


def write_codex_rate_limits(monkeypatch, tmp_path, primary, secondary):
    limits_path = tmp_path / "limits.json"
    limits_path.write_text(
        json.dumps({"rateLimits": {"primary": primary, "secondary": secondary}}), encoding="utf-8"
    )
    monkeypatch.setenv("GOLEM_CODEX_RATE_LIMITS", str(limits_path))
    return limits_path


def wait_until(predicate):
    # select instead of time.sleep, which some tests replace to drive the agent loop.
    while not predicate():
        select.select([], [], [], 0.05)


def logged_claude_cwds(fake_commands):
    return fake_commands["claude-cwd"].read_text(encoding="utf-8").splitlines()


def expected_claude_args(task, prompt=PROMPT, agent_args=AGENT_ARGS, task_id=TASK_ID):
    return [
        "--session-id",
        task["session_id"],
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        *agent_args,
        golem.VERBOSE_ARGUMENT,
        "--name",
        task_id,
        SKIP_PERMISSIONS,
    ]


def raise_usage_lookup_error() -> Never:
    raise SystemExit


def test_alias_expands_arguments_verbatim_like_run(
    installed_launch_agent, ledger_path, monkeypatch
):
    class FixedDateTime(datetime):
        @classmethod
        @override
        def now(cls, tz=None) -> datetime:
            now = datetime(2030, 1, 1, 10, tzinfo=UTC)
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(golem, "datetime", FixedDateTime)
    monkeypatch.setattr(golem, "new_session_id", lambda: "session")
    write_aliases({"impl": 'run {1} "/impl {2}" --then {1}-review /review-lite'})
    task_id = "t42"
    prompt = 'add "retry" {backoff}\nonce'

    assert golem.main(["impl", task_id, prompt, "--now"]) == 0
    alias_tasks = read_tasks(ledger_path)

    write_tasks(ledger_path, {})

    assert (
        golem.main(
            [
                "run",
                task_id,
                f"/impl {prompt}",
                "--then",
                f"{task_id}-review",
                "/review-lite",
                "--now",
            ]
        )
        == 0
    )

    assert read_tasks(ledger_path) == alias_tasks


def test_alias_appends_extra_arguments_before_template_agent_arguments(
    installed_launch_agent, ledger_path
):
    write_aliases({"queue": "run {1} {2} -- --model template-{1}"})

    assert (
        golem.main(
            [
                "queue",
                TASK_ID,
                PROMPT,
                "--now",
                "--",
                "--model",
                "command-line",
            ]
        )
        == 0
    )

    task = read_task(ledger_path)
    assert task["agent_args"] == ["--model", f"template-{TASK_ID}", "--model", "command-line"]
    assert datetime.fromisoformat(task["due_at"]) < RESET_TIME


def test_alias_requires_all_placeholder_arguments(ledger_path):
    write_aliases({"impl": "run {1} {2}"})

    with pytest.raises(SystemExit, match=r"alias impl needs 2 arguments, got 1"):
        golem.main(["impl", TASK_ID])

    assert not ledger_path.exists()


def test_alias_list_marks_shadowed_builtin(capsys):
    write_aliases({"ship": "run {1} {2}", "run": "not a command"})

    assert golem.main(["alias", "list"]) == 0

    ship_line, run_line = capsys.readouterr().out.splitlines()
    assert "shadowed" not in ship_line
    assert "shadowed" in run_line


def test_builtin_command_wins_over_alias(installed_launch_agent, ledger_path):
    write_aliases({"run": "not a command"})

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0

    assert read_task(ledger_path)["prompt"] == PROMPT


def test_alias_cannot_expand_to_another_alias(ledger_path):
    write_aliases({"impl": "review", "review": "run {1} {2}"})

    with pytest.raises(SystemExit, match=r"alias impl expands to alias review"):
        golem.main(["impl"])

    assert not ledger_path.exists()


def test_missing_and_invalid_alias_config_do_not_block_builtin_commands(capsys):
    assert golem.main(["alias", "list"]) == 0
    assert capsys.readouterr().out == "golem: no aliases\n"

    path = golem.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[aliases\n", encoding="utf-8")

    assert golem.main(["list"]) == 0
    assert capsys.readouterr().out == "golem: no tasks\n"
    with pytest.raises(SystemExit, match=re.escape(str(path))):
        golem.main(["alias", "list"])


@pytest.mark.parametrize(
    ("command", "status"),
    [
        pytest.param(["run", TASK_ID, PROMPT], None, id="run"),
        pytest.param(["retry", TASK_ID], golem.STATUS_FAILED, id="retry"),
    ],
)
def test_run_and_retry_require_an_installed_launch_agent(ledger_path, monkeypatch, command, status):
    if status is not None:
        write_task(ledger_path, status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        golem.main(command)

    assert golem.main(["agent", "install"]) == 0
    assert golem.main(["agent", "uninstall"]) == 0
    assert not golem.launch_agent_plist_path().exists()

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        golem.main(command)


def test_run_queues_and_wakes_launch_agent(installed_launch_agent, fake_commands, ledger_path):
    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""
    assert "kickstart" in fake_commands["launchctl-args"].read_text(encoding="utf-8")


def test_run_warns_when_waking_launch_agent_fails(
    installed_launch_agent, ledger_path, monkeypatch, capsys
):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_KICKSTART_EXIT", "1")

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0

    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert capsys.readouterr().err


def test_launch_agent_pass_starts_due_tasks_and_fails_abandoned_ones(ledger_path, monkeypatch):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_SCHEDULED),
            "dead": task_record(golem.STATUS_RUNNING, task_id="dead"),
            "future": task_record(
                golem.STATUS_SCHEDULED, task_id="future", due_at=RESET_TIME.isoformat()
            ),
        },
    )
    started_task_ids: list[str] = []
    monkeypatch.setattr(
        golem, "start_task_executor", lambda task_id, task, **_: started_task_ids.append(task_id)
    )

    golem.run_launch_agent_pass()

    assert started_task_ids == [TASK_ID]
    assert read_tasks(ledger_path)["dead"]["status"] == golem.STATUS_FAILED
    assert read_tasks(ledger_path)["future"]["status"] == golem.STATUS_SCHEDULED


def test_agent_log_copy_truncates_active_writer(monkeypatch):
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", 16)
    log_path = golem.agent_log_path()
    first_content = "first log content\n"
    second_content = "second log content\n"

    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(first_content)
        log_file.flush()

        golem.run_launch_agent_pass()

        assert log_path.with_name("agent.log.1").read_text(encoding="utf-8") == first_content
        assert log_path.read_text(encoding="utf-8") == ""

        log_file.write(second_content)
        log_file.flush()
        assert log_path.read_text(encoding="utf-8") == second_content

        golem.run_launch_agent_pass()

    assert log_path.with_name("agent.log.1").read_text(encoding="utf-8") == second_content
    assert log_path.read_text(encoding="utf-8") == ""


def test_agent_log_under_limit_is_untouched(monkeypatch):
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", 16)
    log_path = golem.agent_log_path()
    log_path.write_text("under limit", encoding="utf-8")

    golem.run_launch_agent_pass()

    assert log_path.read_text(encoding="utf-8") == "under limit"
    assert not log_path.with_name("agent.log.1").exists()


def test_agent_pass_removes_stale_state_but_keeps_held_lock(ledger_path):
    write_task(ledger_path, golem.STATUS_DONE)
    interrupted_write_path = ledger_path.parent / ".tasks.json.interrupted"
    interrupted_write_path.touch()
    orphan_lock_path = golem.run_lock_path("orphan")
    orphan_lock_path.touch()
    held_lock_file = golem.acquire_run_lock("held")
    assert held_lock_file is not None
    try:
        golem.run_launch_agent_pass()

        assert not interrupted_write_path.exists()
        assert not orphan_lock_path.exists()
        assert golem.acquire_run_lock("held") is None
        assert ledger_path.exists()
        assert ledger_path.with_suffix(".lock").exists()
    finally:
        golem.release_run_lock(held_lock_file, "held")


def test_agent_cleanup_waits_for_active_ledger_write(ledger_path, monkeypatch):
    context = multiprocessing.get_context("fork")
    write_started = context.Event()
    finish_write = context.Event()
    replace = Path.replace
    flock = golem.fcntl.flock

    def pause_write(temporary_path, task_ledger_path) -> Path:
        write_started.set()
        finish_write.wait()
        return replace(temporary_path, task_ledger_path)

    monkeypatch.setattr(Path, "replace", pause_write)
    writer = context.Process(
        target=lambda: golem.reserve_tasks({TASK_ID: task_record(golem.STATUS_DONE)})
    )
    writer.start()

    def resume_write(lock_file, operation) -> None:
        if operation == golem.fcntl.LOCK_EX:
            finish_write.set()
        flock(lock_file, operation)

    monkeypatch.setattr(golem.fcntl, "flock", resume_write)
    try:
        write_started.wait()

        golem.run_launch_agent_pass()
        writer.join()

        assert writer.exitcode == 0
        assert read_task(ledger_path)["status"] == golem.STATUS_DONE
    finally:
        finish_write.set()
        if writer.is_alive():
            writer.kill()
        writer.join()


def test_launch_agent_pass_leaves_running_executor_alone(
    installed_launch_agent, ledger_path, monkeypatch
):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    statuses_during_run: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        golem.run_launch_agent_pass()
        statuses_during_run.append(read_task(ledger_path)["status"])
        return 0, json.dumps({"result": CLAUDE_RESULT}), ""

    monkeypatch.setattr(golem, "run_claude", run_claude)

    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])

    assert statuses_during_run == [golem.STATUS_RUNNING]
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW


@pytest.mark.parametrize(
    ("tasks", "expected_sleep_count"),
    [
        pytest.param(
            {TASK_ID: task_record(golem.STATUS_SCHEDULED, due_at=RESET_TIME.isoformat())},
            1,
            id="scheduled",
        ),
        pytest.param(
            {
                PARENT_ID: task_record(golem.STATUS_FAILED, task_id=PARENT_ID),
                TASK_ID: task_record(golem.STATUS_WAITING, after=PARENT_ID),
            },
            0,
            id="waiting-on-failed-parent",
        ),
    ],
)
def test_launch_agent_runs_until_nothing_is_scheduled_or_running(
    ledger_path, monkeypatch, tasks, expected_sleep_count
):
    write_tasks(ledger_path, tasks)
    sleep_lengths: list[float] = []

    def cancel_while_sleeping(seconds: float) -> None:
        sleep_lengths.append(seconds)
        golem.cancel_task(TASK_ID)

    monkeypatch.setattr(golem.time, "sleep", cancel_while_sleeping)

    assert golem.run_launch_agent() == 0
    assert len(sleep_lengths) == expected_sleep_count


def test_changed_state_home_requires_reinstall(installed_launch_agent, monkeypatch, tmp_path):
    installed_plist = plistlib.loads(golem.launch_agent_plist_path().read_bytes())
    assert installed_plist["EnvironmentVariables"] == {
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"]
    }
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "other-state"))

    with pytest.raises(SystemExit, match=LAUNCH_AGENT_ERROR_PATTERN):
        golem.main(["run", TASK_ID, PROMPT, "--now"])


def test_launch_agent_executor_uses_only_saved_environment(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("LANG", SAVED_LANG)
    monkeypatch.setenv("LC_TIME", CHILD_LANG)
    monkeypatch.setenv("UNRELATED", "not-saved")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    monkeypatch.setenv("LANG", CURRENT_LANG)
    monkeypatch.setenv("LC_TIME", CURRENT_LANG)
    monkeypatch.delenv("UNRELATED")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    start_task_executor = golem.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    monkeypatch.setattr(golem, "start_task_executor", record_executor)

    golem.run_launch_agent_pass()

    assert len(processes) == 1
    processes[0].wait()
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert fake_commands["claude-lang"].read_text(encoding="utf-8") == f"{SAVED_LANG}\n"
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8") == f"{CHILD_LANG}\n"
    assert fake_commands["claude-unrelated"].read_text(encoding="utf-8") == ""


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
def empty_git_repository(tmp_path):
    repository_path = tmp_path / "empty-repository"
    repository_path.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository_path, check=True)
    return repository_path


@pytest.mark.parametrize(
    (
        "agent_args",
        "repository_fixture",
        "has_default_name",
        "has_default_worktree",
        "has_default_skip_permissions",
    ),
    [
        pytest.param([], "git_repository", True, True, True, id="defaults"),
        pytest.param([], None, True, False, True, id="non-git"),
        pytest.param([], "empty_git_repository", True, False, True, id="no-commits"),
        pytest.param(
            ["--permission-mode", "plan"], "git_repository", True, True, False, id="permission-mode"
        ),
        pytest.param(
            ["--permission-mode=plan"],
            "git_repository",
            True,
            True,
            False,
            id="permission-mode-value",
        ),
        pytest.param(
            ["--allow-dangerously-skip-permissions"],
            "git_repository",
            True,
            True,
            False,
            id="allow-permissions",
        ),
        pytest.param([WORKTREE_FLAG, "custom"], "git_repository", True, False, True, id="worktree"),
        pytest.param(["-w", "custom"], "git_repository", True, False, True, id="short-worktree"),
        pytest.param(["--name", "custom"], "git_repository", False, True, True, id="name"),
        pytest.param(["-n", "custom"], "git_repository", False, True, True, id="short-name"),
    ],
)
def test_headless_defaults_respect_agent_arguments(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    request,
    agent_args,
    repository_fixture,
    has_default_name,
    has_default_worktree,
    has_default_skip_permissions,
):
    if repository_fixture is not None:
        monkeypatch.chdir(request.getfixturevalue(repository_fixture))
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    arguments = logged_claude_args(fake_commands)
    assert arguments[arguments.index("--output-format") + 2 :] == [
        *agent_args,
        golem.VERBOSE_ARGUMENT,
        *(["--name", TASK_ID] if has_default_name else []),
        *([WORKTREE_FLAG, task["session_id"]] if has_default_worktree else []),
        *([SKIP_PERMISSIONS] if has_default_skip_permissions else []),
    ]


@pytest.mark.parametrize("is_retried", [False, True])
def test_dependent_runs_in_parent_worktree_with_its_own_environment(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, is_retried
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setenv("LANG", SAVED_LANG)
    monkeypatch.setenv("LC_TIME", SAVED_LANG)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    monkeypatch.setenv("LANG", CHILD_LANG)
    monkeypatch.delenv("LC_TIME")
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    monkeypatch.setenv("LANG", CURRENT_LANG)
    monkeypatch.setenv("LC_TIME", CURRENT_LANG)
    if is_retried:
        golem.main(["cancel", CHILD_ID])
        golem.main(["retry", CHILD_ID, "--now"])

    golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])
    if is_retried:
        golem.main(["_execute", CHILD_ID, read_task(ledger_path, CHILD_ID)["session_id"]])

    parent_worktree = read_task(ledger_path, PARENT_ID)["worktree"]
    child_arguments = logged_claude_args(fake_commands)
    assert logged_claude_cwds(fake_commands)[-1] == parent_worktree
    assert WORKTREE_FLAG not in child_arguments
    assert fake_commands["claude-lang"].read_text(encoding="utf-8").splitlines() == [
        SAVED_LANG,
        CHILD_LANG,
    ]
    assert fake_commands["claude-lc-time"].read_text(encoding="utf-8").splitlines() == [
        SAVED_LANG,
        "",
    ]


def test_detected_worktree_is_shared_with_dependents(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, tmp_path
):
    monkeypatch.chdir(git_repository)
    detected_worktree = tmp_path / "detected-worktree"
    detected_worktree.mkdir()
    write_claude_output(
        fake_commands, CLAUDE_RESULT, is_verbose=True, working_directory=str(detected_worktree)
    )
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now", "--", WORKTREE_FLAG, "custom"])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])

    golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])

    assert logged_claude_cwds(fake_commands)[-1] == str(detected_worktree)
    resource = golem.load_worktrees()[str(detected_worktree.resolve())]
    assert resource["owner"] == golem.WORKTREE_OWNER_EXTERNAL


def write_claude_events(fake_commands, events):
    fake_commands["claude-stdout"].write_text(
        "".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8"
    )


def write_claude_output(
    fake_commands, result, *, api_error_status=None, is_verbose=False, working_directory=None
):
    output = {
        "api_error_status": api_error_status,
        "is_error": api_error_status is not None,
        "result": result,
        "type": "result",
    }
    init_message = {"type": "system", "subtype": "init", "cwd": working_directory}
    events = [init_message, output] if is_verbose else [output]
    write_claude_events(fake_commands, events)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pickup"], id="pickup"),
        pytest.param(["pickup", "--app"], id="pickup-app"),
        pytest.param(["close"], id="close"),
    ],
)
def test_review_commands_complete_review_task(installed_launch_agent, ledger_path, command):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])

    assert golem.main([*command, TASK_ID]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_DONE


@pytest.mark.parametrize(
    "status", [golem.STATUS_SCHEDULED, golem.STATUS_WAITING, golem.STATUS_CANCELLED]
)
@pytest.mark.parametrize(
    "pickup_args", [pytest.param([], id="terminal"), pytest.param(["--app"], id="app")]
)
def test_pickup_refuses_tasks_that_never_started(ledger_path, status, pickup_args):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["pickup", TASK_ID, *pickup_args])


def test_pickup_app_opens_task_session_in_desktop_app(fake_commands, ledger_path):
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW)

    assert golem.main(["pickup", TASK_ID, "--app"]) == 0
    assert (
        fake_commands["open-args"].read_text(encoding="utf-8")
        == f"claude://resume?session={read_task(ledger_path)['session_id']}\n"
    )
    assert logged_claude_calls(fake_commands) == []


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pickup", TASK_ID, "--app"], id="one"),
        pytest.param(["pickup", "--all", "--app"], id="all"),
    ],
)
def test_failed_app_pickup_leaves_task_needing_review(
    fake_commands, ledger_path, monkeypatch, command
):
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW)
    monkeypatch.setenv("GOLEM_OPEN_EXIT", "1")

    assert golem.main(command) == 1
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={read_task(ledger_path)['session_id']}\n"
    )


def test_pickup_app_rejects_agent_arguments(fake_commands, ledger_path):
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW)

    with pytest.raises(SystemExit) as exit_info:
        golem.main(["pickup", TASK_ID, "--app", "--", *AGENT_ARGS])

    assert exit_info.value.code == 2
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["pickup", "--all"], id="all-needs-app"),
        pytest.param(["pickup", TASK_ID, "--all", "--app"], id="all-rejects-id"),
        pytest.param(["pickup", "--app"], id="needs-id-or-all"),
        pytest.param(
            ["reschedule", TASK_ID, "--now", "--", *AGENT_ARGS], id="reschedule-agent-args"
        ),
    ],
)
def test_pickup_all_and_reschedule_validate_arguments(command):
    with pytest.raises(SystemExit) as exit_info:
        golem.main(command)

    assert exit_info.value.code == 2


def test_pickup_marks_ancestors_that_need_review_done(fake_commands, ledger_path):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_NEEDS_REVIEW, task_id=CHILD_ID, after=PARENT_ID),
            GRANDCHILD_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=GRANDCHILD_ID, after=CHILD_ID
            ),
        },
    )

    assert golem.main(["pickup", GRANDCHILD_ID, "--app"]) == 0
    assert {task["status"] for task in read_tasks(ledger_path).values()} == {golem.STATUS_DONE}


@pytest.mark.parametrize(
    ("parent_fields", "child_status", "opened_ids"),
    [
        pytest.param({}, golem.STATUS_NEEDS_REVIEW, [CHILD_ID], id="child-needs-review"),
        pytest.param({}, golem.STATUS_RUNNING, [], id="child-running"),
        pytest.param({}, golem.STATUS_FAILED, [PARENT_ID], id="child-failed"),
        pytest.param(
            {"gate_reason": GATE_REASON}, golem.STATUS_WAITING, [PARENT_ID], id="parent-stopped"
        ),
    ],
)
def test_pickup_all_opens_only_latest_reviewable_task_in_chain(
    fake_commands, ledger_path, parent_fields, child_status, opened_ids
):
    tasks = {
        PARENT_ID: task_record(golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, **parent_fields),
        CHILD_ID: task_record(child_status, task_id=CHILD_ID, after=PARENT_ID),
    }
    write_tasks(ledger_path, tasks)

    assert golem.main(["pickup", "--all", "--app"]) == 0
    assert fake_commands["open-args"].read_text(encoding="utf-8").splitlines() == [
        f"claude://resume?session={tasks[task_id]['session_id']}" for task_id in opened_ids
    ]


def test_pickup_all_continues_after_busy_directory(fake_commands, ledger_path, capsys, tmp_path):
    busy_directory = tmp_path / "busy"
    other_directory = tmp_path / "other"
    busy_directory.mkdir()
    other_directory.mkdir()
    tasks = {
        "busy": task_record(golem.STATUS_NEEDS_REVIEW, task_id="busy", cwd=str(busy_directory)),
        "other": task_record(golem.STATUS_NEEDS_REVIEW, task_id="other", cwd=str(other_directory)),
    }
    write_tasks(ledger_path, tasks)
    directory_lock_file = golem.acquire_lock(golem.directory_lock_path(str(busy_directory)))
    assert directory_lock_file is not None

    try:
        assert golem.main(["pickup", "--all", "--app"]) == 1
    finally:
        golem.release_lock(directory_lock_file)

    assert f"golem: directory is busy: {busy_directory}" in capsys.readouterr().err
    assert read_task(ledger_path, "busy")["status"] == golem.STATUS_NEEDS_REVIEW
    assert read_task(ledger_path, "other")["status"] == golem.STATUS_DONE
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={tasks['other']['session_id']}\n"
    )


def test_after_pending_parent_skips_usage_lookup(installed_launch_agent, ledger_path, monkeypatch):
    write_task(ledger_path, golem.STATUS_SCHEDULED, task_id=PARENT_ID)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    assert golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID]) == 0
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING


@pytest.mark.parametrize(
    "status",
    [
        None,
        golem.STATUS_SCHEDULED,
        golem.STATUS_RUNNING,
        golem.STATUS_NEEDS_REVIEW,
        golem.STATUS_DONE,
    ],
)
def test_retry_refuses_unretryable_task_before_usage_lookup(ledger_path, monkeypatch, status):
    if status is not None:
        write_task(ledger_path, status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["retry", TASK_ID])


def test_launch_agent_install_removes_plist_when_bootstrap_fails(fake_commands, monkeypatch):
    monkeypatch.setenv("GOLEM_LAUNCHCTL_BOOTSTRAP_EXIT", "1")

    with pytest.raises(SystemExit, match="could not install agent"):
        golem.main(["agent", "install"])

    assert not golem.launch_agent_plist_path().exists()


@pytest.mark.parametrize("state_home", [None, "", "relative/state"])
def test_ledger_falls_back_to_home_when_xdg_state_home_is_not_absolute(
    fake_commands, monkeypatch, tmp_path, state_home
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if state_home is not None:
        monkeypatch.setenv("XDG_STATE_HOME", state_home)
    golem.main(["agent", "install"])

    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0
    assert (tmp_path / ".local" / "state" / "golem" / "tasks.json").exists()


@pytest.mark.parametrize(
    ("exit_code", "output", "expected_result", "expected_status"),
    [
        pytest.param(0, '{"result": "done"}', "done", golem.STATUS_NEEDS_REVIEW, id="success"),
        pytest.param(1, '{"result": "done"}', "done", golem.STATUS_FAILED, id="failure"),
        pytest.param(
            0,
            '[{"type": "system"}, {"type": "result", "result": "done"}]',
            "done",
            golem.STATUS_NEEDS_REVIEW,
            id="verbose",
        ),
        pytest.param(0, "plain output", "plain output", golem.STATUS_NEEDS_REVIEW, id="plain"),
    ],
)
def test_run_now_records_claude_result(
    installed_launch_agent,
    fake_commands,
    monkeypatch,
    ledger_path,
    capsys,
    exit_code,
    output,
    expected_result,
    expected_status,
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", str(exit_code))
    fake_commands["claude-stdout"].write_text(output, encoding="utf-8")
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == exit_code
    task = read_task(ledger_path)
    assert task["status"] == expected_status
    assert task["returncode"] == exit_code
    assert task["result"] == expected_result
    assert logged_claude_args(fake_commands) == expected_claude_args(task)
    capsys.readouterr()
    assert golem.main(["show", TASK_ID]) == 0
    assert capsys.readouterr().out == f"{expected_result}\n"


def test_auto_pickup_waits_for_usage_limit_resume(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    golem.main(["run", TASK_ID, PROMPT, "--now", "--auto-pickup"])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["open-args"].read_text(encoding="utf-8") == ""

    set_task_due_now(ledger_path)
    write_claude_output(fake_commands, PROMPT)
    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_DONE
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={task['session_id']}\n"
    )


@pytest.mark.parametrize(
    ("parent_status", "expected_open_args"),
    [
        pytest.param(
            golem.STATUS_NEEDS_REVIEW,
            f"claude://resume?session={PARENT_ID}-session\n",
            id="unreviewed-parent",
        ),
        pytest.param(golem.STATUS_DONE, "", id="reviewed-parent"),
    ],
)
def test_auto_pickup_opens_parent_when_resumed_follow_up_fails(
    fake_commands, ledger_path, monkeypatch, parent_status, expected_open_args
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(parent_status, task_id=PARENT_ID, auto_pickup=True),
            TASK_ID: task_record(golem.STATUS_SCHEDULED, after=PARENT_ID, auto_pickup=True),
        },
    )

    assert golem.main(["_execute", TASK_ID, f"{TASK_ID}-session"]) == 1
    tasks = read_tasks(ledger_path)
    assert tasks[TASK_ID]["status"] == golem.STATUS_FAILED
    assert tasks[PARENT_ID]["status"] == golem.STATUS_DONE
    assert fake_commands["open-args"].read_text(encoding="utf-8") == expected_open_args


@pytest.mark.parametrize(
    ("settings_ceiling", "parent_status", "parent_result", "dependent_status", "call_count"),
    [
        pytest.param(
            None,
            golem.STATUS_DONE,
            CLAUDE_RESULT,
            golem.STATUS_NEEDS_REVIEW,
            2,
            id="background-work-completes",
        ),
        pytest.param(
            "600000",
            golem.STATUS_FAILED,
            f"{CLAUDE_RESULT}\n\n{BACKGROUND_STOP_LINE}",
            golem.STATUS_WAITING,
            1,
            id="background-work-stopped",
        ),
    ],
)
def test_background_work_waits_or_fails_before_starting_dependents(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    capsys,
    settings_ceiling,
    parent_status,
    parent_result,
    dependent_status,
    call_count,
):
    monkeypatch.setenv("GOLEM_CLAUDE_BACKGROUND_STOP", BACKGROUND_STOP_LINE)
    if settings_ceiling is not None:
        monkeypatch.setenv("GOLEM_CLAUDE_SETTINGS_CEILING", settings_ceiling)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    parent_session_id = read_task(ledger_path, PARENT_ID)["session_id"]
    write_claude_output(fake_commands, CLAUDE_RESULT)

    assert golem.main(["_execute", PARENT_ID, parent_session_id]) == 0

    parent = read_task(ledger_path, PARENT_ID)
    assert parent["status"] == parent_status
    assert parent["result"] == parent_result
    assert read_task(ledger_path, CHILD_ID)["status"] == dependent_status
    assert len(logged_claude_calls(fake_commands)) == call_count
    assert (BACKGROUND_STOP_LINE in capsys.readouterr().err) is (settings_ceiling is not None)


@pytest.mark.parametrize("is_verbose", [False, True])
def test_usage_limited_task_resumes_at_reset_in_same_session(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path, is_verbose
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    agent_args = [*AGENT_ARGS, golem.VERBOSE_ARGUMENT] if is_verbose else AGENT_ARGS
    write_claude_output(
        fake_commands,
        USAGE_LIMIT_RESULT,
        api_error_status=golem.USAGE_LIMIT_HTTP_STATUS,
        is_verbose=is_verbose,
    )
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *agent_args])
    session_id = read_task(ledger_path)["session_id"]
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")

    assert golem.main(["_execute", TASK_ID, session_id]) == 0
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_SCHEDULED
    assert task["result"] == USAGE_LIMIT_RESULT
    assert datetime.fromisoformat(task["due_at"]) == RESET_TIME
    write_claude_output(fake_commands, CLAUDE_RESULT, is_verbose=is_verbose)
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    set_task_due_now(ledger_path)

    assert golem.main(["_execute", TASK_ID, session_id]) == 0

    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_NEEDS_REVIEW
    assert task["result"] == CLAUDE_RESULT
    assert task["session_id"] == session_id
    assert logged_claude_args(fake_commands) == [
        "--resume",
        session_id,
        "--print",
        golem.USAGE_LIMIT_RESUME_PROMPT,
        "--output-format",
        "stream-json",
        *agent_args,
        *([] if is_verbose else [golem.VERBOSE_ARGUMENT]),
        "--name",
        TASK_ID,
        SKIP_PERMISSIONS,
    ]


def test_usage_limited_task_fails_once_resumes_run_out(
    installed_launch_agent, fake_commands, monkeypatch, ledger_path
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    for _ in range(golem.MAX_USAGE_LIMIT_RESUMES):
        task = read_task(ledger_path)
        assert task["status"] == golem.STATUS_SCHEDULED
        set_task_due_now(ledger_path)
        golem.main(["_execute", TASK_ID, task["session_id"]])

    assert read_task(ledger_path)["status"] == golem.STATUS_FAILED


@pytest.mark.parametrize(
    "find_reset_time",
    [
        pytest.param(lambda: None, id="no-reset"),
        pytest.param(raise_usage_lookup_error, id="lookup-error"),
    ],
)
def test_usage_limited_task_fails_when_it_cannot_be_rescheduled(
    fake_commands, ledger_path, monkeypatch, find_reset_time
):
    monkeypatch.setattr(golem, "next_reset_time", find_reset_time)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    write_task(ledger_path, golem.STATUS_RUNNING)

    golem.execute_task(TASK_ID, read_task(ledger_path), ())
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_FAILED
    assert task["result"] == USAGE_LIMIT_RESULT


def test_scheduled_task_runs_when_executed(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "scheduled_time", lambda _: RESET_TIME)
    golem.main(["run", TASK_ID, PROMPT, "--at", AT_TEXT])
    task = read_task(ledger_path)
    assert datetime.fromisoformat(task["due_at"]) == RESET_TIME
    set_task_due_now(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW


def create_waiting_chain():
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--at", AT_TEXT, "--", *AGENT_ARGS])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])
    golem.main(["run", GRANDCHILD_ID, GRANDCHILD_PROMPT, "--after", CHILD_ID, "--", *AGENT_ARGS])
    golem.main(["run", SIBLING_ID, SIBLING_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])


def test_scheduled_task_runs_waiting_chain(installed_launch_agent, fake_commands, ledger_path):
    create_waiting_chain()
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    set_task_due_now(ledger_path, PARENT_ID)

    golem.main(["_execute", PARENT_ID, tasks[PARENT_ID]["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task_id: task["status"] for task_id, task in tasks.items()} == {
        PARENT_ID: golem.STATUS_DONE,
        CHILD_ID: golem.STATUS_DONE,
        GRANDCHILD_ID: golem.STATUS_NEEDS_REVIEW,
        SIBLING_ID: golem.STATUS_NEEDS_REVIEW,
    }
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(tasks[PARENT_ID], PARENT_PROMPT, task_id=PARENT_ID),
        expected_claude_args(tasks[CHILD_ID], CHILD_PROMPT, task_id=CHILD_ID),
        expected_claude_args(tasks[GRANDCHILD_ID], GRANDCHILD_PROMPT, task_id=GRANDCHILD_ID),
        expected_claude_args(tasks[SIBLING_ID], SIBLING_PROMPT, task_id=SIBLING_ID),
    ]


@pytest.mark.parametrize(
    ("parents", "schedule_arguments"),
    [
        pytest.param([], ["--now"], id="now"),
        pytest.param(
            [(PARENT_ID, PARENT_PROMPT)], ["--after", PARENT_ID], id="after-pending-parent"
        ),
    ],
)
def test_run_then_chain_runs_in_order_with_shared_agent_arguments(
    installed_launch_agent, fake_commands, ledger_path, parents, schedule_arguments
):
    for task_id, prompt in parents:
        golem.main(["run", task_id, prompt, "--at", AT_TEXT, "--", *AGENT_ARGS])
        set_task_due_now(ledger_path, task_id)
    follow_ups = [(GRANDCHILD_ID, GRANDCHILD_PROMPT), (SIBLING_ID, SIBLING_PROMPT)]
    then_arguments = [argument for follow_up in follow_ups for argument in ("--then", *follow_up)]
    chain = [*parents, (CHILD_ID, CHILD_PROMPT), *follow_ups]
    first_id = chain[0][0]

    assert (
        golem.main(
            ["run", CHILD_ID, CHILD_PROMPT, *schedule_arguments, *then_arguments, "--", *AGENT_ARGS]
        )
        == 0
    )
    tasks = read_tasks(ledger_path)

    assert golem.main(["_execute", first_id, tasks[first_id]["session_id"]]) == 0
    assert [read_tasks(ledger_path)[task_id]["status"] for task_id, _ in chain] == [
        *[golem.STATUS_DONE] * (len(chain) - 1),
        golem.STATUS_NEEDS_REVIEW,
    ]
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(tasks[task_id], prompt, task_id=task_id) for task_id, prompt in chain
    ]


def test_run_then_auto_pickup_opens_only_last_task(
    installed_launch_agent, fake_commands, ledger_path
):
    assert (
        golem.main(
            [
                "run",
                TASK_ID,
                PROMPT,
                "--now",
                "--auto-pickup",
                "--then",
                CHILD_ID,
                CHILD_PROMPT,
                "--then",
                GRANDCHILD_ID,
                GRANDCHILD_PROMPT,
            ]
        )
        == 0
    )
    tasks = read_tasks(ledger_path)

    assert golem.main(["_execute", TASK_ID, tasks[TASK_ID]["session_id"]]) == 0
    assert {task["status"] for task in read_tasks(ledger_path).values()} == {golem.STATUS_DONE}
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={tasks[GRANDCHILD_ID]['session_id']}\n"
    )


def test_failed_follow_up_auto_pickup_opens_last_success(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("GOLEM_CLAUDE_FAIL_TASK", GRANDCHILD_ID)
    golem.main(
        [
            "run",
            TASK_ID,
            PROMPT,
            "--now",
            "--auto-pickup",
            "--then",
            CHILD_ID,
            CHILD_PROMPT,
            "--then",
            GRANDCHILD_ID,
            GRANDCHILD_PROMPT,
        ]
    )
    tasks = read_tasks(ledger_path)

    assert golem.main(["_execute", TASK_ID, tasks[TASK_ID]["session_id"]]) == 0
    assert {task_id: task["status"] for task_id, task in read_tasks(ledger_path).items()} == {
        TASK_ID: golem.STATUS_DONE,
        CHILD_ID: golem.STATUS_DONE,
        GRANDCHILD_ID: golem.STATUS_FAILED,
    }
    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={tasks[CHILD_ID]['session_id']}\n"
    )


def gate_command(tmp_path, *, exit_code=0, stderr=""):
    log_path = tmp_path / "gate-stdin"
    return (
        f"cat >> {log_path}; echo >> {log_path}; "
        f"printf %s {shlex.quote(stderr)} >&2; exit {exit_code}"
    )


def gate_inputs(tmp_path):
    log_path = tmp_path / "gate-stdin"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]


def schedule_gated_chain(ledger_path, gate, *extra_arguments: str, then_arguments=()):
    golem.main(
        [
            *["run", PARENT_ID, PARENT_PROMPT, "--now", "--gate", gate, *extra_arguments],
            *["--then", CHILD_ID, CHILD_PROMPT, *then_arguments],
        ]
    )
    return read_task(ledger_path, PARENT_ID)


def run_gated_chain(ledger_path, gate, *extra_arguments: str):
    parent = schedule_gated_chain(ledger_path, gate, *extra_arguments)
    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == 0
    return parent


def write_parent_with_waiting_child(
    ledger_path, parent_status: str = golem.STATUS_NEEDS_REVIEW, **parent_fields: object
):
    tasks = {
        PARENT_ID: task_record(parent_status, task_id=PARENT_ID, **parent_fields),
        CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
    }
    write_tasks(ledger_path, tasks)


def test_passing_gate_continues_chain_and_receives_task_as_json(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    write_claude_output(fake_commands, CLAUDE_RESULT)
    parent = schedule_gated_chain(
        ledger_path,
        gate_command(tmp_path),
        then_arguments=("--then", GRANDCHILD_ID, GRANDCHILD_PROMPT),
    )
    write_transcript(parent["session_id"], 0)
    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == 0

    tasks = read_tasks(ledger_path)
    assert [tasks[task_id]["status"] for task_id in (PARENT_ID, CHILD_ID, GRANDCHILD_ID)] == [
        golem.STATUS_DONE,
        golem.STATUS_DONE,
        golem.STATUS_NEEDS_REVIEW,
    ]
    parent_input, child_input = gate_inputs(tmp_path)
    assert child_input["task_id"] == CHILD_ID
    assert parent_input == (
        {
            "task_id": PARENT_ID,
            "session_id": parent["session_id"],
            "directory": str(Path.cwd()),
            "started_at": tasks[PARENT_ID]["started_at"],
            "exit_code": 0,
            "result": CLAUDE_RESULT,
            "transcript_path": str(
                Path.home() / ".claude" / "projects" / "project" / f"{parent['session_id']}.jsonl"
            ),
        }
    )
    assert len(logged_claude_calls(fake_commands)) == 3


def test_failing_gate_stops_chain_with_its_stderr_as_reason(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    write_claude_output(fake_commands, CLAUDE_RESULT)
    run_gated_chain(ledger_path, gate_command(tmp_path, exit_code=1, stderr=f"{GATE_REASON}\n"))

    tasks = read_tasks(ledger_path)
    assert tasks[PARENT_ID]["status"] == golem.STATUS_NEEDS_REVIEW
    assert tasks[PARENT_ID]["gate_reason"] == GATE_REASON
    assert tasks[CHILD_ID]["status"] == golem.STATUS_WAITING
    assert len(logged_claude_calls(fake_commands)) == 1
    assert golem.run_launch_agent_pass() == []
    assert not golem.has_pending_tasks(tasks)


def test_executor_resuming_gated_task_runs_gate_before_follow_up(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    write_parent_with_waiting_child(ledger_path, gate=gate_command(tmp_path))

    assert golem.main(["_execute", PARENT_ID, f"{PARENT_ID}-session"]) == 0
    assert len(gate_inputs(tmp_path)) == 1
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_NEEDS_REVIEW


def test_gate_that_cannot_run_stops_chain_with_error_as_reason(
    installed_launch_agent, ledger_path, tmp_path
):
    missing_directory = str(tmp_path / "missing")
    write_parent_with_waiting_child(ledger_path, gate="true", cwd=missing_directory)

    assert golem.main(["_execute", PARENT_ID, f"{PARENT_ID}-session"]) == 0

    tasks = read_tasks(ledger_path)
    assert missing_directory in tasks[PARENT_ID]["gate_reason"]
    assert tasks[CHILD_ID]["status"] == golem.STATUS_WAITING


def test_passing_gate_does_not_wait_for_its_background_processes(ledger_path, monkeypatch):
    monkeypatch.setattr(golem, "GATE_TIMEOUT_SECONDS", 0.5)
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW, gate="sleep 2 & exit 0")

    assert golem.gate_stop_reason(TASK_ID, read_task(ledger_path)) is None


def test_timed_out_gate_stops_chain_and_kills_processes_it_started(
    ledger_path, monkeypatch, tmp_path
):
    monkeypatch.setattr(golem, "GATE_TIMEOUT_SECONDS", 0.2)
    pid_path = tmp_path / "gate-child-pid"
    write_task(
        ledger_path, golem.STATUS_NEEDS_REVIEW, gate=f"sleep 30 & echo $! > {pid_path}; wait"
    )

    assert golem.gate_stop_reason(TASK_ID, read_task(ledger_path)) == "gate timed out after 0.2s"

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    with contextlib.suppress(ProcessLookupError):
        while time.monotonic() < deadline:
            os.kill(child_pid, 0)
            select.select([], [], [], 0.05)
        pytest.fail(f"gate child {child_pid} is still running")


def test_executor_resuming_gated_task_skips_gate_while_directory_is_busy(
    installed_launch_agent, ledger_path, tmp_path
):
    write_parent_with_waiting_child(ledger_path, gate=gate_command(tmp_path))
    directory_lock_file = golem.acquire_lock(golem.directory_lock_path(str(Path.cwd())))
    assert directory_lock_file is not None
    try:
        assert golem.main(["_execute", PARENT_ID, f"{PARENT_ID}-session"]) == 0
    finally:
        golem.release_lock(directory_lock_file)

    assert gate_inputs(tmp_path) == []
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING


def test_failing_gate_keeps_chain_stopped_when_task_is_closed_meanwhile(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    close_parent = shlex.join([sys.executable, golem.__file__, "close", PARENT_ID])
    write_parent_with_waiting_child(
        ledger_path, gate=f"{close_parent}; echo {GATE_REASON} >&2; exit 1"
    )

    assert golem.main(["_execute", PARENT_ID, f"{PARENT_ID}-session"]) == 0

    tasks = read_tasks(ledger_path)
    assert tasks[PARENT_ID]["status"] == golem.STATUS_DONE
    assert tasks[PARENT_ID]["gate_reason"] == GATE_REASON
    assert tasks[CHILD_ID]["status"] == golem.STATUS_WAITING


@pytest.mark.parametrize(
    ("is_usage_limited", "expected_returncode", "expected_status"),
    [
        pytest.param(False, 1, golem.STATUS_FAILED, id="failed"),
        pytest.param(True, 0, golem.STATUS_SCHEDULED, id="usage-limited"),
    ],
)
def test_unsuccessful_task_never_runs_its_gate(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    tmp_path,
    monkeypatch,
    is_usage_limited,
    expected_returncode,
    expected_status,
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    if is_usage_limited:
        write_claude_output(
            fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
        )
    else:
        monkeypatch.setenv("GOLEM_CLAUDE_FAIL_TASK", PARENT_ID)
    parent = schedule_gated_chain(ledger_path, gate_command(tmp_path))

    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == expected_returncode
    assert read_task(ledger_path, PARENT_ID)["status"] == expected_status
    assert gate_inputs(tmp_path) == []


def test_task_without_waiting_follow_up_never_runs_its_gate(
    installed_launch_agent, ledger_path, tmp_path
):
    gate = gate_command(tmp_path, exit_code=1)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now", "--gate", gate])
    parent = read_task(ledger_path, PARENT_ID)

    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == 0
    assert read_task(ledger_path, PARENT_ID)["status"] == golem.STATUS_NEEDS_REVIEW
    assert "gate_reason" not in read_task(ledger_path, PARENT_ID)
    assert gate_inputs(tmp_path) == []


def test_auto_pickup_opens_stopped_task_and_keeps_chain_stopped(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    parent = run_gated_chain(
        ledger_path, gate_command(tmp_path, exit_code=1, stderr=GATE_REASON), "--auto-pickup"
    )

    assert fake_commands["open-args"].read_text(encoding="utf-8") == (
        f"claude://resume?session={parent['session_id']}\n"
    )
    assert read_task(ledger_path, PARENT_ID)["gate_reason"] == GATE_REASON
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING
    assert golem.run_launch_agent_pass() == []


@pytest.fixture
def started_executors(monkeypatch):
    executors: list[str] = []
    monkeypatch.setattr(
        golem, "start_task_executor", lambda task_id, task, **_: executors.append(task_id)
    )
    return executors


@pytest.mark.parametrize(
    ("gate_exit_code", "continue_arguments", "expected_gate_run_count"),
    [
        pytest.param(0, [], 1, id="passing-gate"),
        pytest.param(1, ["--force"], 0, id="force"),
    ],
)
def test_continue_releases_stopped_task(
    ledger_path,
    tmp_path,
    started_executors,
    gate_exit_code,
    continue_arguments,
    expected_gate_run_count,
):
    write_parent_with_waiting_child(
        ledger_path,
        gate=gate_command(tmp_path, exit_code=gate_exit_code),
        gate_reason=GATE_REASON,
    )

    assert golem.main(["continue", PARENT_ID, *continue_arguments]) == 0

    parent = read_task(ledger_path, PARENT_ID)
    assert parent["status"] == golem.STATUS_DONE
    assert "gate_reason" not in parent
    assert len(gate_inputs(tmp_path)) == expected_gate_run_count
    assert started_executors == [PARENT_ID]


def test_continue_with_failing_gate_keeps_task_stopped(
    ledger_path, tmp_path, capsys, started_executors
):
    write_parent_with_waiting_child(
        ledger_path,
        gate=gate_command(tmp_path, exit_code=1, stderr="still missing"),
        gate_reason=GATE_REASON,
    )

    assert golem.main(["continue", PARENT_ID]) == 1

    parent = read_task(ledger_path, PARENT_ID)
    assert parent["status"] == golem.STATUS_NEEDS_REVIEW
    assert parent["gate_reason"] == "still missing"
    assert "still missing" in capsys.readouterr().err
    assert started_executors == []


def test_continue_refuses_task_not_stopped_at_a_gate(ledger_path, started_executors):
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW)

    with pytest.raises(SystemExit, match="not stopped at a gate"):
        golem.main(["continue", TASK_ID])

    assert started_executors == []


def settings_file():
    return golem.local_settings_path(str(Path.cwd()))


def run_hook(monkeypatch, capsys, event, session_id):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session_id})))
    assert golem.main(["_hook", event]) == 0
    return capsys.readouterr().out


def stop_gated_chain(ledger_path, fake_commands, tmp_path, original_settings):
    if original_settings is not None:
        settings_file().parent.mkdir()
        settings_file().write_text(json.dumps(original_settings), encoding="utf-8")
        settings_file().chmod(USER_SETTINGS_MODE)
    write_claude_output(fake_commands, CLAUDE_RESULT)
    return run_gated_chain(ledger_path, gate_command(tmp_path, exit_code=1, stderr=GATE_REASON))


def finish_chain(ledger_path, parent):
    tasks = read_tasks(ledger_path)
    tasks[PARENT_ID]["status"] = golem.STATUS_DONE
    del tasks[PARENT_ID]["gate_reason"]
    write_tasks(ledger_path, tasks)
    assert golem.main(["_execute", PARENT_ID, parent["session_id"]]) == 0


@pytest.mark.parametrize("original_settings", [None, USER_SETTINGS], ids=["no-file", "existing"])
@pytest.mark.parametrize("action", ["cancel", "delete", "finish"])
def test_hooks_are_seeded_when_chain_stops_and_removed_when_it_ends(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, original_settings, action
):
    parent = stop_gated_chain(ledger_path, fake_commands, tmp_path, original_settings)

    seeded = json.loads(settings_file().read_text(encoding="utf-8"))
    seeded_hooks = seeded["hooks"]
    assert {
        event: len(groups)
        for event, groups in seeded_hooks.items()
        if event in golem.HOOK_EVENT_ARGUMENTS
    } == {
        "Stop": 1 + (original_settings is not None),
        "PreToolUse": 1,
    }
    assert seeded_hooks["PreToolUse"][0]["matcher"] == golem.HOOK_TOOL_MATCHER
    if original_settings is not None:
        assert seeded["permissions"] == original_settings["permissions"]
        assert seeded_hooks["Stop"][0] == original_settings["hooks"]["Stop"][0]
        assert seeded_hooks["Notification"] == original_settings["hooks"]["Notification"]

    if action == "finish":
        finish_chain(ledger_path, parent)
    else:
        assert golem.main([action, CHILD_ID]) == 0

    if original_settings is None:
        assert not settings_file().parent.exists()
        return
    assert json.loads(settings_file().read_text(encoding="utf-8")) == original_settings
    assert stat.S_IMODE(settings_file().stat().st_mode) == USER_SETTINGS_MODE


@pytest.mark.parametrize("is_symlink", [False, True], ids=["not-json", "symlink"])
def test_unusable_hook_settings_are_left_alone(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, capsys, is_symlink
):
    settings_file().parent.mkdir()
    if is_symlink:
        shared_settings_path = tmp_path / "shared-settings.json"
        shared_settings_path.write_text("{}", encoding="utf-8")
        settings_file().symlink_to(shared_settings_path)
    else:
        settings_file().write_text("{not json", encoding="utf-8")
    original_text = settings_file().read_text(encoding="utf-8")
    write_claude_output(fake_commands, CLAUDE_RESULT)
    run_gated_chain(ledger_path, gate_command(tmp_path, exit_code=1, stderr=GATE_REASON))

    assert settings_file().is_symlink() == is_symlink
    assert settings_file().read_text(encoding="utf-8") == original_text
    assert "hooks not installed" in capsys.readouterr().err


def write_settings(settings):
    settings_file().parent.mkdir()
    settings_file().write_text(json.dumps(settings), encoding="utf-8")


def test_install_hooks_replaces_stale_golem_command():
    stale_command = f"/old/python /old/golem.py _hook stop{golem.HOOK_FAILURE_SUFFIX}"
    stale_entry = {"type": "command", "command": stale_command}
    write_settings({"hooks": {"Stop": [{"hooks": [stale_entry]}]}})

    golem.install_hooks(str(Path.cwd()))

    stop_groups = json.loads(settings_file().read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert stop_groups == [golem.hook_group(golem.HOOK_EVENT_STOP)]


def test_remove_hooks_keeps_user_entry_sharing_a_group():
    user_entry = {"type": "command", "command": "echo user-stop"}
    golem_entry = golem.hook_group(golem.HOOK_EVENT_STOP)["hooks"][0]
    write_settings({"hooks": {"Stop": [{"hooks": [user_entry, golem_entry]}]}})

    golem.remove_hooks(str(Path.cwd()))

    assert json.loads(settings_file().read_text(encoding="utf-8")) == {
        "hooks": {"Stop": [{"hooks": [user_entry]}]}
    }


@pytest.mark.parametrize(
    ("parent_status", "child_status", "is_needed"),
    [
        (golem.STATUS_DONE, golem.STATUS_RUNNING, True),
        (golem.STATUS_DONE, golem.STATUS_SCHEDULED, True),
        (golem.STATUS_DONE, golem.STATUS_NEEDS_REVIEW, False),
        (golem.STATUS_FAILED, golem.STATUS_WAITING, False),
    ],
    ids=["follow-up-running", "follow-up-paused", "follow-up-finished", "parent-failed"],
)
def test_hooks_stay_until_follow_up_finishes(ledger_path, parent_status, child_status, is_needed):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(parent_status, task_id=PARENT_ID, gate="true"),
            CHILD_ID: task_record(child_status, task_id=CHILD_ID, after=PARENT_ID),
        },
    )

    golem.sync_hooks(str(Path.cwd()))

    assert settings_file().exists() == is_needed


@pytest.mark.parametrize(
    "requeue_arguments",
    [["run", GRANDCHILD_ID, CHILD_PROMPT, "--after", PARENT_ID], ["retry", CHILD_ID, "--now"]],
    ids=["run-after", "retry"],
)
def test_follow_up_requeued_after_stopped_task_seeds_hooks(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, requeue_arguments
):
    stop_gated_chain(ledger_path, fake_commands, tmp_path, None)
    assert golem.main(["cancel", CHILD_ID]) == 0
    assert not settings_file().exists()

    assert golem.main(requeue_arguments) == 0

    assert settings_file().exists()


def test_launch_agent_pass_removes_hooks_of_abandoned_follow_up(ledger_path):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, gate="true"),
            CHILD_ID: task_record(golem.STATUS_RUNNING, task_id=CHILD_ID, after=PARENT_ID),
        },
    )
    golem.sync_hooks(str(Path.cwd()))
    assert settings_file().exists()

    golem.run_launch_agent_pass()

    assert not settings_file().exists()


def test_ungated_chain_gets_no_hooks(installed_launch_agent, fake_commands, ledger_path):
    write_claude_output(fake_commands, CLAUDE_RESULT)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now", "--then", CHILD_ID, CHILD_PROMPT])
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    assert not settings_file().parent.exists()


@pytest.mark.parametrize(
    "parent_status",
    [golem.STATUS_NEEDS_REVIEW, golem.STATUS_DONE],
    ids=["needs-review", "opened-in-app"],
)
def test_stop_hook_continues_stopped_chain_when_gate_passes(
    ledger_path, tmp_path, started_executors, monkeypatch, capsys, parent_status
):
    write_parent_with_waiting_child(
        ledger_path, parent_status, gate=gate_command(tmp_path), gate_reason=GATE_REASON
    )

    output = run_hook(monkeypatch, capsys, golem.HOOK_ARGUMENT_STOP, PARENT_SESSION)

    assert json.loads(output)["systemMessage"].endswith(CHILD_ID)
    assert read_task(ledger_path, PARENT_ID)["status"] == golem.STATUS_DONE
    assert started_executors == [PARENT_ID]


@pytest.mark.parametrize(
    ("session_id", "gate", "is_directory_busy"),
    [
        ("unknown-session", "true", False),
        (PARENT_SESSION, "false", False),
        (PARENT_SESSION, "sleep 5", False),
        (PARENT_SESSION, "true", True),
    ],
    ids=["unknown-session", "failing-gate", "slow-gate", "busy-directory"],
)
def test_stop_hook_does_nothing(
    ledger_path, started_executors, monkeypatch, capsys, session_id, gate, is_directory_busy
):
    monkeypatch.setattr(golem, "HOOK_GATE_TIMEOUT_SECONDS", 0.2)
    write_parent_with_waiting_child(ledger_path, gate=gate, gate_reason=GATE_REASON)
    directory_lock_file = (
        golem.acquire_lock(golem.directory_lock_path(str(Path.cwd())))
        if is_directory_busy
        else None
    )

    try:
        assert run_hook(monkeypatch, capsys, golem.HOOK_ARGUMENT_STOP, session_id) == ""
    finally:
        if directory_lock_file is not None:
            golem.release_lock(directory_lock_file)
    assert read_task(ledger_path, PARENT_ID)["gate_reason"] == GATE_REASON
    assert started_executors == []


@pytest.mark.parametrize(
    ("child_status", "session_id", "is_denied"),
    [
        (golem.STATUS_RUNNING, PARENT_SESSION, True),
        (golem.STATUS_NEEDS_REVIEW, PARENT_SESSION, False),
        (golem.STATUS_RUNNING, "unknown-session", False),
    ],
    ids=["follow-up-running", "follow-up-finished", "unknown-session"],
)
def test_pre_tool_use_hook_denies_while_follow_up_runs(
    ledger_path, monkeypatch, capsys, child_status, session_id, is_denied
):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, gate="true"),
            CHILD_ID: task_record(child_status, task_id=CHILD_ID, after=PARENT_ID),
        },
    )

    output = run_hook(monkeypatch, capsys, golem.HOOK_ARGUMENT_PRE_TOOL_USE, session_id)

    if not is_denied:
        assert output == ""
        return
    decision = json.loads(output)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert CHILD_ID in decision["permissionDecisionReason"]


def test_pre_tool_use_hook_denies_while_grandchild_runs(ledger_path, monkeypatch, capsys):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, gate="true"),
            CHILD_ID: task_record(golem.STATUS_DONE, task_id=CHILD_ID, after=PARENT_ID),
            GRANDCHILD_ID: task_record(golem.STATUS_RUNNING, task_id=GRANDCHILD_ID, after=CHILD_ID),
        },
    )

    output = run_hook(monkeypatch, capsys, golem.HOOK_ARGUMENT_PRE_TOOL_USE, PARENT_SESSION)

    assert GRANDCHILD_ID in json.loads(output)["hookSpecificOutput"]["permissionDecisionReason"]


def test_stop_hook_returns_while_follow_up_runs(ledger_path, tmp_path, monkeypatch):
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    write_parent_with_waiting_child(
        ledger_path, gate=gate_command(tmp_path), gate_reason=GATE_REASON
    )

    result = golem.subprocess.run(
        ["/bin/sh", "-c", golem.hook_command_line(golem.HOOK_ARGUMENT_STOP)],
        input=json.dumps({"session_id": PARENT_SESSION}),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )

    try:
        wait_until(started_path.exists)
        assert CHILD_ID in json.loads(result.stdout)["systemMessage"]
    finally:
        hold_path.unlink()
        wait_until(lambda: read_task(ledger_path, CHILD_ID)["status"] != golem.STATUS_RUNNING)


def test_hook_commands_run_without_path_or_state_environment(ledger_path):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, gate="true"),
            CHILD_ID: task_record(golem.STATUS_RUNNING, task_id=CHILD_ID, after=PARENT_ID),
        },
    )
    golem.sync_hooks(str(Path.cwd()))
    hooks = json.loads(settings_file().read_text(encoding="utf-8"))["hooks"]
    command = hooks["PreToolUse"][0]["hooks"][0]["command"]

    result = golem.subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({"session_id": PARENT_SESSION}),
        env={},
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_command_does_not_block_claude_when_golem_is_missing(monkeypatch):
    monkeypatch.setattr(golem, "golem_command", lambda: [sys.executable, "/missing/golem.py"])

    result = golem.subprocess.run(
        ["/bin/sh", "-c", golem.hook_command_line(golem.HOOK_ARGUMENT_PRE_TOOL_USE)],
        input="",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 2


def test_after_parent_with_pending_gate_waits_for_the_gate(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_parent_with_waiting_child(ledger_path, gate="true")

    assert golem.main(["run", SIBLING_ID, SIBLING_PROMPT, "--after", PARENT_ID]) == 0
    assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_WAITING


def test_after_pending_auto_pickup_parent_passes_to_claude_only(
    installed_launch_agent, ledger_path
):
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now", "--auto-pickup"])

    assert golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID]) == 0
    assert (
        golem.main(
            ["run", GRANDCHILD_ID, GRANDCHILD_PROMPT, "--backend", "codex", "--after", PARENT_ID]
        )
        == 0
    )

    tasks = read_tasks(ledger_path)
    assert tasks[CHILD_ID]["auto_pickup"] is True
    assert "auto_pickup" not in tasks[GRANDCHILD_ID]


def test_after_succeeded_auto_pickup_parent_does_not_pass_to_child(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_task(
        ledger_path,
        golem.STATUS_DONE,
        task_id=PARENT_ID,
        auto_pickup=True,
    )

    assert golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID]) == 0
    assert "auto_pickup" not in read_task(ledger_path, CHILD_ID)


@pytest.mark.parametrize("option_arguments", [["--auto-pickup"], ["--gate", "true"]])
def test_claude_only_option_rejects_codex_without_changing_ledger(ledger_path, option_arguments):
    write_task(ledger_path, golem.STATUS_SCHEDULED)
    tasks = read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=f"{option_arguments[0]} does not support Codex"):
        golem.main(
            ["run", CHILD_ID, CHILD_PROMPT, "--backend", "codex", *option_arguments, "--now"]
        )

    assert read_tasks(ledger_path) == tasks


@pytest.mark.parametrize("fields", [{"auto_pickup": True}, {"no_worktree": True}, {"gate": "true"}])
def test_retry_keeps_run_options(installed_launch_agent, ledger_path, fields):
    write_task(ledger_path, golem.STATUS_FAILED, **fields)

    assert golem.main(["retry", TASK_ID, "--now"]) == 0
    assert read_task(ledger_path).items() >= fields.items()


@pytest.mark.parametrize(
    ("aliases", "command"),
    [
        pytest.param({}, ["run", CHILD_ID, CHILD_PROMPT], id="run"),
        pytest.param({"impl": f'run {{1}} "{CHILD_PROMPT}"'}, ["impl", CHILD_ID], id="alias"),
    ],
)
def test_directory_option_creates_then_chain_in_directory(
    installed_launch_agent, ledger_path, monkeypatch, tmp_path, aliases, command
):
    write_aliases(aliases)
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path)

    assert (
        golem.main(["-C", "project", *command, "--now", "--then", GRANDCHILD_ID, GRANDCHILD_PROMPT])
        == 0
    )

    tasks = read_tasks(ledger_path)
    assert {tasks[task_id]["cwd"] for task_id in (CHILD_ID, GRANDCHILD_ID)} == {
        str(tmp_path / "project")
    }


def test_directory_option_refuses_missing_directory_without_changing_ledger(ledger_path):
    write_task(ledger_path, golem.STATUS_SCHEDULED)
    tasks = read_tasks(ledger_path)

    with pytest.raises(SystemExit, match="directory not found: missing"):
        golem.main(["-C", "missing", "run", CHILD_ID, CHILD_PROMPT, "--now"])

    assert read_tasks(ledger_path) == tasks


def test_no_worktree_chain_runs_in_directory_without_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    golem.main(
        [
            "run",
            PARENT_ID,
            PARENT_PROMPT,
            "--now",
            "--no-worktree",
            "--then",
            CHILD_ID,
            CHILD_PROMPT,
        ]
    )

    assert golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]]) == 0

    tasks = read_tasks(ledger_path)
    assert [tasks[task_id]["status"] for task_id in (PARENT_ID, CHILD_ID)] == [
        golem.STATUS_DONE,
        golem.STATUS_NEEDS_REVIEW,
    ]
    assert logged_claude_cwds(fake_commands) == [str(git_repository)] * 2
    assert not (git_repository / ".claude").exists()
    assert golem.load_worktrees() == {}


def test_after_in_place_parent_runs_in_parent_directory(
    installed_launch_agent, git_repository, ledger_path
):
    write_task(
        ledger_path,
        golem.STATUS_SCHEDULED,
        task_id=PARENT_ID,
        cwd=str(git_repository),
        no_worktree=True,
    )

    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])

    child = read_task(ledger_path, CHILD_ID)
    assert child["cwd"] == str(git_repository)
    assert child["no_worktree"] is True


@pytest.mark.parametrize(
    ("parent_fields", "run_arguments", "message"),
    [
        pytest.param({}, ["--now", "--no-worktree"], "--no-worktree does not accept", id="flag"),
        pytest.param(
            {"no_worktree": True},
            ["--after", PARENT_ID],
            f"{PARENT_ID} runs in place",
            id="inherited",
        ),
    ],
)
def test_in_place_task_refuses_worktree_argument_without_changing_ledger(
    installed_launch_agent, ledger_path, parent_fields, run_arguments, message
):
    write_task(ledger_path, golem.STATUS_SCHEDULED, task_id=PARENT_ID, **parent_fields)
    tasks = read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=message):
        golem.main(["run", CHILD_ID, CHILD_PROMPT, *run_arguments, "--", WORKTREE_FLAG, "custom"])

    assert read_tasks(ledger_path) == tasks


@pytest.mark.parametrize(
    ("tasks", "schedule_arguments", "follow_up_ids"),
    [
        pytest.param(
            {GRANDCHILD_ID: task_record(golem.STATUS_SCHEDULED, task_id=GRANDCHILD_ID)},
            ["--now"],
            [GRANDCHILD_ID],
            id="existing-follow-up-now",
        ),
        pytest.param(
            {
                PARENT_ID: task_record(golem.STATUS_SCHEDULED, task_id=PARENT_ID),
                GRANDCHILD_ID: task_record(golem.STATUS_SCHEDULED, task_id=GRANDCHILD_ID),
            },
            ["--after", PARENT_ID],
            [GRANDCHILD_ID],
            id="existing-follow-up-after-pending-parent",
        ),
        pytest.param({}, ["--now"], [GRANDCHILD_ID, GRANDCHILD_ID], id="repeated-follow-up"),
        pytest.param({}, ["--now"], [CHILD_ID], id="follow-up-matches-run-task"),
    ],
)
def test_run_then_refuses_ids_without_changing_the_ledger(
    installed_launch_agent, ledger_path, tasks, schedule_arguments, follow_up_ids
):
    write_tasks(ledger_path, tasks)
    then_arguments = [
        argument for task_id in follow_up_ids for argument in ("--then", task_id, GRANDCHILD_PROMPT)
    ]

    with pytest.raises(SystemExit, match=f": {re.escape(follow_up_ids[-1])}$"):
        golem.main(["run", CHILD_ID, CHILD_PROMPT, *schedule_arguments, *then_arguments])

    assert read_tasks(ledger_path) == tasks


def test_retrying_failed_parent_runs_waiting_dependent(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    create_waiting_chain()
    parent = read_task(ledger_path, PARENT_ID)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    set_task_due_now(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")

    golem.main(["retry", PARENT_ID, "--now"])
    retry = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, retry["session_id"]])
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert tasks[PARENT_ID]["status"] == golem.STATUS_DONE
    assert tasks[CHILD_ID]["status"] == golem.STATUS_DONE


@pytest.mark.parametrize(
    ("cancel_id", "expected_statuses"),
    [
        pytest.param(
            PARENT_ID,
            {
                PARENT_ID: golem.STATUS_CANCELLED,
                CHILD_ID: golem.STATUS_CANCELLED,
                GRANDCHILD_ID: golem.STATUS_CANCELLED,
                SIBLING_ID: golem.STATUS_CANCELLED,
            },
            id="scheduled-parent",
        ),
        pytest.param(
            CHILD_ID,
            {
                PARENT_ID: golem.STATUS_SCHEDULED,
                CHILD_ID: golem.STATUS_CANCELLED,
                GRANDCHILD_ID: golem.STATUS_CANCELLED,
                SIBLING_ID: golem.STATUS_WAITING,
            },
            id="waiting-child",
        ),
    ],
)
def test_cancelling_task_cancels_waiting_dependents(
    installed_launch_agent, ledger_path, cancel_id, expected_statuses
):
    create_waiting_chain()

    assert golem.main(["cancel", cancel_id]) == 0
    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert {task_id: task["status"] for task_id, task in tasks.items()} == expected_statuses


def test_after_successful_parent_schedules_task_normally(
    installed_launch_agent, ledger_path, monkeypatch
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    parent = read_task(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    child = read_task(ledger_path, CHILD_ID)
    assert child["status"] == golem.STATUS_SCHEDULED
    assert datetime.fromisoformat(child["due_at"]) == RESET_TIME


@pytest.mark.parametrize("status", [None, golem.STATUS_CANCELLED])
def test_after_refuses_missing_or_cancelled_parent(installed_launch_agent, ledger_path, status):
    if status is not None:
        write_task(ledger_path, status, task_id=PARENT_ID)

    with pytest.raises(SystemExit, match=PARENT_ID):
        golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])

    assert CHILD_ID not in golem.load_tasks()


def test_cancel_marks_task_cancelled_and_does_not_run_executor(
    installed_launch_agent, fake_commands, ledger_path
):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["cancel", TASK_ID])

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "status", [golem.STATUS_SCHEDULED, golem.STATUS_WAITING, golem.STATUS_CANCELLED]
)
def test_delete_removes_non_running_task(ledger_path, status):
    write_task(ledger_path, status)

    assert golem.main(["delete", TASK_ID]) == 0
    assert TASK_ID not in golem.load_tasks()


def test_delete_refuses_running_task(ledger_path):
    write_task(ledger_path, golem.STATUS_RUNNING)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["delete", TASK_ID])

    assert TASK_ID in golem.load_tasks()


def test_delete_refuses_unknown_task():
    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["delete", TASK_ID])


def test_delete_refuses_and_lists_dependents(ledger_path):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_DONE),
            "first": task_record(golem.STATUS_CANCELLED, task_id="first", after=TASK_ID),
            "second": task_record(golem.STATUS_DONE, task_id="second", after=TASK_ID),
        },
    )

    with pytest.raises(SystemExit, match=r"first.*second"):
        golem.main(["delete", TASK_ID])

    assert TASK_ID in golem.load_tasks()


def test_delete_removes_several_tasks_including_parent_and_child(ledger_path):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_DONE, task_id=SIBLING_ID),
        },
    )

    assert golem.main(["delete", PARENT_ID, CHILD_ID]) == 0

    assert set(golem.load_tasks()) == {SIBLING_ID}


def test_delete_keeps_ledger_when_one_task_is_unknown(ledger_path):
    tasks = {
        TASK_ID: task_record(golem.STATUS_DONE),
        SIBLING_ID: task_record(golem.STATUS_DONE, task_id=SIBLING_ID),
    }
    write_tasks(ledger_path, tasks)

    with pytest.raises(SystemExit, match="unknown"):
        golem.main(["delete", TASK_ID, "unknown"])

    assert read_tasks(ledger_path) == tasks


def test_delete_chain_removes_ancestors_dependents_and_branches(ledger_path):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_DONE, task_id=CHILD_ID, after=PARENT_ID),
            GRANDCHILD_ID: task_record(golem.STATUS_DONE, task_id=GRANDCHILD_ID, after=CHILD_ID),
            SIBLING_ID: task_record(golem.STATUS_DONE, task_id=SIBLING_ID, after=PARENT_ID),
            TASK_ID: task_record(golem.STATUS_DONE),
        },
    )

    assert golem.main(["delete", CHILD_ID, "--chain"]) == 0

    assert set(golem.load_tasks()) == {TASK_ID}


def test_delete_chain_keeps_ledger_when_any_task_is_running(ledger_path):
    tasks = {
        PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
        CHILD_ID: task_record(golem.STATUS_DONE, task_id=CHILD_ID, after=PARENT_ID),
        SIBLING_ID: task_record(golem.STATUS_RUNNING, task_id=SIBLING_ID, after=PARENT_ID),
    }
    write_tasks(ledger_path, tasks)

    with pytest.raises(SystemExit, match=SIBLING_ID):
        golem.main(["delete", CHILD_ID, "--chain"])

    assert read_tasks(ledger_path) == tasks


def test_deleted_task_id_reuse_ignores_earlier_executor(
    installed_launch_agent, fake_commands, ledger_path
):
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    old_task = read_task(ledger_path)

    assert golem.main(["delete", TASK_ID]) == 0
    assert golem.main(["run", TASK_ID, PROMPT, "--now"]) == 0
    new_task = read_task(ledger_path)
    assert new_task["session_id"] != old_task["session_id"]

    assert golem.main(["_execute", TASK_ID, old_task["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


def test_reused_parent_id_does_not_claim_waiting_dependent(ledger_path):
    parent = task_record(golem.STATUS_DONE, task_id=PARENT_ID)
    tasks = {
        PARENT_ID: parent,
        CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
    }
    old_session_id = parent["session_id"]
    assert isinstance(old_session_id, str)
    tasks[PARENT_ID] = task_record(
        golem.STATUS_DONE, task_id=PARENT_ID, session_id="replacement-session"
    )
    write_tasks(ledger_path, tasks)

    assert golem.claim_next_dependent(PARENT_ID, old_session_id) is None
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING


@pytest.mark.parametrize(
    "status",
    [golem.STATUS_RUNNING, golem.STATUS_NEEDS_REVIEW, golem.STATUS_FAILED, golem.STATUS_DONE],
)
def test_cancel_refuses_non_scheduled_tasks(ledger_path, status):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["cancel", TASK_ID])


def test_leftover_executor_does_not_run_retried_task(
    installed_launch_agent, fake_commands, ledger_path
):
    write_task(ledger_path, golem.STATUS_CANCELLED, session_id="old")
    golem.main(["retry", TASK_ID, "--at", AT_TEXT])

    assert golem.main(["_execute", TASK_ID, "old"]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("retry_args", "reset_time", "expected_due_at"),
    [
        pytest.param(["--now"], None, None, id="now"),
        pytest.param(["--at", AT_TEXT], None, RESET_TIME, id="at"),
        pytest.param([], None, None, id="no-usage-window"),
        pytest.param([], RESET_TIME, RESET_TIME, id="usage-window"),
    ],
)
def test_retry_runs_failed_task_in_new_session(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    retry_args,
    reset_time,
    expected_due_at,
):
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    golem.main(["run", TASK_ID, PROMPT, "--now", "--", *AGENT_ARGS])
    failed = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, failed["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setattr(golem, "next_reset_time", lambda: reset_time)
    monkeypatch.setattr(golem, "scheduled_time", lambda _: RESET_TIME)

    assert golem.main(["retry", TASK_ID, *retry_args]) == 0
    retry = read_task(ledger_path)
    assert retry["status"] == golem.STATUS_SCHEDULED
    assert retry["session_id"] != failed["session_id"]
    due_at = datetime.fromisoformat(retry["due_at"])
    if expected_due_at is None:
        assert due_at <= datetime.now(UTC)
    else:
        assert due_at == expected_due_at
    set_task_due_now(ledger_path)
    assert golem.main(["_execute", TASK_ID, retry["session_id"]]) == 0
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    assert logged_claude_args(fake_commands) == expected_claude_args(retry)


@pytest.mark.parametrize("status", [None, golem.STATUS_RUNNING])
def test_reschedule_refuses_unknown_and_non_scheduled_tasks(ledger_path, monkeypatch, status):
    if status is not None:
        write_task(ledger_path, status)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("usage lookup ran"))

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["reschedule", TASK_ID])


def test_reschedule_changes_only_due_time(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    write_task(
        ledger_path,
        golem.STATUS_SCHEDULED,
        result=USAGE_LIMIT_RESULT,
        usage_limit_resume_count=1,
    )
    original_task = read_task(ledger_path)
    later = (datetime.now(UTC) + timedelta(days=1)).replace(second=0, microsecond=0)
    monkeypatch.setattr(golem, "scheduled_time", lambda _: later)

    assert golem.main(["reschedule", TASK_ID, "--at", AT_TEXT]) == 0
    assert golem.main(["_execute", TASK_ID, original_task["session_id"]]) == 0
    rescheduled_task = read_task(ledger_path)
    assert datetime.fromisoformat(rescheduled_task["due_at"]) == later
    assert {name: value for name, value in rescheduled_task.items() if name != "due_at"} == {
        name: value for name, value in original_task.items() if name != "due_at"
    }

    assert golem.main(["reschedule", TASK_ID, "--now"]) == 0
    assert golem.main(["_execute", TASK_ID, original_task["session_id"]]) == 0
    assert logged_claude_args(fake_commands) == [
        "--resume",
        original_task["session_id"],
        "--print",
        golem.USAGE_LIMIT_RESUME_PROMPT,
        "--output-format",
        "stream-json",
        golem.VERBOSE_ARGUMENT,
        "--name",
        TASK_ID,
        SKIP_PERMISSIONS,
    ]


def test_retry_reuses_task_worktree_and_saved_environment(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setenv("LANG", SAVED_LANG)
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    first_session_id = read_task(ledger_path)["session_id"]
    golem.main(["_execute", TASK_ID, first_session_id])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    monkeypatch.setenv("LANG", CURRENT_LANG)

    golem.main(["retry", TASK_ID, "--now"])
    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])

    assert Path(logged_claude_cwds(fake_commands)[-1]).name == first_session_id
    assert WORKTREE_FLAG not in logged_claude_args(fake_commands)
    assert fake_commands["claude-lang"].read_text(encoding="utf-8").splitlines() == [
        SAVED_LANG,
        SAVED_LANG,
    ]


@pytest.mark.parametrize(
    ("five_hour", "seven_day", "expected"),
    [
        pytest.param(
            {"utilization": 74, "resets_at": EARLIER_RESET_TIME.isoformat()},
            {"utilization": 74, "resets_at": LATER_RESET_TIME.isoformat()},
            EARLIER_RESET_TIME,
            id="five-hour-available",
        ),
        pytest.param(
            {"utilization": 100, "resets_at": LATER_RESET_TIME.isoformat()},
            {"utilization": 100, "resets_at": EARLIER_RESET_TIME.isoformat()},
            LATER_RESET_TIME,
            id="latest-exhausted-window",
        ),
        pytest.param(
            {"utilization": 74, "resets_at": EARLIER_RESET_TIME.isoformat()},
            {"utilization": 100, "resets_at": LATER_RESET_TIME.isoformat()},
            LATER_RESET_TIME,
            id="weekly-exhausted",
        ),
    ],
)
def test_next_reset_time_picks_latest_exhausted_window(
    fake_commands, five_hour, seven_day, expected
):
    frames = [
        {"type": "control_response", "response": {"request_id": "init", "subtype": "success"}},
        {
            "type": "control_response",
            "response": {
                "request_id": "usage",
                "subtype": "success",
                "response": {
                    "rate_limits": {"five_hour": five_hour, "seven_day": seven_day},
                    "rate_limits_available": True,
                },
            },
        },
    ]
    fake_commands["claude-stdout"].write_text(
        "\n".join(json.dumps(frame) for frame in frames), encoding="utf-8"
    )

    assert golem.next_reset_time() == expected


def test_failed_dependent_does_not_block_later_sibling(
    installed_launch_agent, ledger_path, monkeypatch
):
    create_waiting_chain()
    prompts: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        prompts.append(task["prompt"])
        return int(task["prompt"] == CHILD_PROMPT), json.dumps({"result": CLAUDE_RESULT}), ""

    monkeypatch.setattr(golem, "run_claude", run_claude)
    parent = read_task(ledger_path, PARENT_ID)
    set_task_due_now(ledger_path, PARENT_ID)
    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    tasks = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert prompts == [PARENT_PROMPT, CHILD_PROMPT, SIBLING_PROMPT]
    assert tasks[CHILD_ID]["status"] == golem.STATUS_FAILED
    assert tasks[SIBLING_ID]["status"] == golem.STATUS_NEEDS_REVIEW


def test_usage_limit_resume_and_pickup_use_task_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    write_claude_output(
        fake_commands, USAGE_LIMIT_RESULT, api_error_status=golem.USAGE_LIMIT_HTTP_STATUS
    )
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    worktree = read_task(ledger_path)["worktree"]
    write_claude_output(fake_commands, CLAUDE_RESULT)
    set_task_due_now(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    golem.main(["pickup", TASK_ID])

    assert logged_claude_cwds(fake_commands)[-2:] == [worktree, worktree]


def test_executor_death_keeps_task_running_until_claude_exits(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    executor = golem.start_task_executor(TASK_ID, task)

    try:
        wait_until(started_path.exists)
        os.kill(executor.pid, signal.SIGKILL)
        executor.wait()

        golem.run_launch_agent_pass()
        assert read_task(ledger_path)["status"] == golem.STATUS_RUNNING
        assert golem.acquire_lock(golem.directory_lock_path(task["cwd"])) is None
        with pytest.raises(SystemExit, match="cannot be retried"):
            golem.main(["retry", TASK_ID, "--now"])

        hold_path.unlink()

        wait_until(lambda: golem.is_run_lock_free(task["session_id"]))
        golem.run_launch_agent_pass()
        assert read_task(ledger_path)["status"] == golem.STATUS_FAILED
    finally:
        hold_path.unlink(missing_ok=True)
        if executor.poll() is None:
            executor.kill()
            executor.wait()


def test_executor_frees_worktree_held_by_stray_claude_process(ledger_path, monkeypatch, tmp_path):
    stray_path = tmp_path / "stray"
    stray_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_STRAY", str(stray_path))
    write_task(ledger_path, golem.STATUS_SCHEDULED)
    task = read_task(ledger_path)

    try:
        golem.main(["_execute", TASK_ID, task["session_id"]])
        directory_lock_file = golem.acquire_lock(golem.directory_lock_path(task["cwd"]))
    finally:
        stray_path.unlink()

    assert directory_lock_file is not None
    golem.release_lock(directory_lock_file)


@pytest.mark.parametrize("contender", ["scheduled", "pickup"])
def test_shared_worktree_blocks_contenders_and_allows_other_worktrees(
    fake_commands, ledger_path, monkeypatch, tmp_path, contender
):
    shared_worktree = tmp_path / "shared"
    other_worktree = tmp_path / "other"
    shared_worktree.mkdir()
    other_worktree.mkdir()
    contender_status = (
        golem.STATUS_SCHEDULED if contender == "scheduled" else golem.STATUS_NEEDS_REVIEW
    )
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_SCHEDULED, worktree=str(shared_worktree)),
            CHILD_ID: task_record(
                contender_status, task_id=CHILD_ID, worktree=str(shared_worktree)
            ),
            SIBLING_ID: task_record(
                golem.STATUS_SCHEDULED, task_id=SIBLING_ID, worktree=str(other_worktree)
            ),
        },
    )
    run_task_ids: list[str] = []

    def execute_contender() -> None:
        if contender == "pickup":
            with pytest.raises(SystemExit, match="directory is busy"):
                golem.main(["pickup", CHILD_ID])
            return
        child = read_task(ledger_path, CHILD_ID)
        assert golem.main(["_execute", CHILD_ID, child["session_id"]]) == 0

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        run_task_ids.append(task_id)
        if task_id == TASK_ID:
            sibling = read_task(ledger_path, SIBLING_ID)
            execute_contender()
            assert golem.main(["_execute", SIBLING_ID, sibling["session_id"]]) == 0
        return 0, json.dumps({"result": CLAUDE_RESULT}), ""

    monkeypatch.setattr(golem, "run_claude", run_claude)
    first = read_task(ledger_path)
    golem.main(["_execute", TASK_ID, first["session_id"]])

    assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_NEEDS_REVIEW
    if contender == "pickup":
        assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_NEEDS_REVIEW
        assert golem.main(["pickup", CHILD_ID]) == 0
        assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_DONE
        assert run_task_ids == [TASK_ID, SIBLING_ID]
        return
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_SCHEDULED
    child = read_task(ledger_path, CHILD_ID)
    assert golem.main(["_execute", CHILD_ID, child["session_id"]]) == 0
    assert run_task_ids == [TASK_ID, SIBLING_ID, CHILD_ID]


def test_pickup_holds_worktree_until_its_claude_process_exits(
    fake_commands, ledger_path, monkeypatch, tmp_path
):
    worktree = tmp_path / "shared"
    worktree.mkdir()
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    headless_task = task_record(
        golem.STATUS_SCHEDULED,
        worktree=str(worktree),
        environment={"PATH": os.environ["PATH"], "GOLEM_CLAUDE_HOLD": ""},
    )
    write_tasks(
        ledger_path,
        {
            "pickup": task_record(
                golem.STATUS_NEEDS_REVIEW, task_id="pickup", worktree=str(worktree)
            ),
            TASK_ID: headless_task,
        },
    )
    pickup_process = multiprocessing.get_context("fork").Process(
        target=lambda: golem.main(["pickup", "pickup"])
    )
    pickup_process.start()
    headless_session_id = str(headless_task["session_id"])
    try:
        wait_until(started_path.exists)
        assert golem.main(["_execute", TASK_ID, headless_session_id]) == 0
        assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
        pickup_process.kill()
        pickup_process.join()
        assert golem.main(["_execute", TASK_ID, headless_session_id]) == 0
        assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED

        hold_path.unlink()

        wait_until(
            lambda: (
                golem.main(["_execute", TASK_ID, headless_session_id]) == 0
                and read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
            )
        )
    finally:
        hold_path.unlink(missing_ok=True)
        pickup_process.kill()
        pickup_process.join()


@pytest.mark.parametrize("first_dependent", ["busy", "missing"])
def test_dependent_drain_stops_for_busy_worktrees_and_skips_missing_worktrees(
    ledger_path, monkeypatch, tmp_path, first_dependent
):
    first_worktree = tmp_path / "first"
    second_worktree = tmp_path / "second"
    second_worktree.mkdir()
    if first_dependent == "busy":
        first_worktree.mkdir()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, worktree=str(first_worktree)
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(
                golem.STATUS_WAITING,
                task_id=SIBLING_ID,
                after=PARENT_ID,
                worktree=str(second_worktree),
            ),
        },
    )
    run_task_ids: list[str] = []

    def run_claude(
        task_id: str, task: dict[str, Any], lock_file_descriptors: tuple[int, ...]
    ) -> tuple[int, str, str]:
        run_task_ids.append(task_id)
        return 0, json.dumps({"result": CLAUDE_RESULT}), ""

    monkeypatch.setattr(golem, "run_claude", run_claude)
    directory_lock_file = None
    if first_dependent == "busy":
        directory_lock_file = golem.acquire_lock(golem.directory_lock_path(str(first_worktree)))
    try:
        golem.drain_dependents(PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"])
    finally:
        if directory_lock_file is not None:
            golem.release_lock(directory_lock_file)

    if first_dependent == "busy":
        assert run_task_ids == []
        assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING
        assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_WAITING
        return
    assert run_task_ids == [SIBLING_ID]
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_FAILED
    assert read_task(ledger_path, CHILD_ID)["result"] == (
        f"golem: worktree is missing: {first_worktree}"
    )
    assert read_task(ledger_path, SIBLING_ID)["status"] == golem.STATUS_NEEDS_REVIEW


def test_agent_recovers_interrupted_dependent_draining_in_ledger_order(
    fake_commands, ledger_path, monkeypatch
):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_SCHEDULED,
                task_id=PARENT_ID,
                prompt=PARENT_PROMPT,
                agent_args=AGENT_ARGS,
            ),
            CHILD_ID: task_record(
                golem.STATUS_WAITING,
                task_id=CHILD_ID,
                after=PARENT_ID,
                prompt=CHILD_PROMPT,
                agent_args=AGENT_ARGS,
            ),
            SIBLING_ID: task_record(
                golem.STATUS_WAITING,
                task_id=SIBLING_ID,
                after=PARENT_ID,
                prompt=SIBLING_PROMPT,
                agent_args=AGENT_ARGS,
            ),
            "failed": task_record(golem.STATUS_FAILED, task_id="failed"),
            GRANDCHILD_ID: task_record(golem.STATUS_WAITING, task_id=GRANDCHILD_ID, after="failed"),
        },
    )
    drain_dependents = golem.drain_dependents

    def crash_before_draining(task_id: str, session_id: str) -> Never:
        raise RuntimeError("executor crashed")

    monkeypatch.setattr(golem, "drain_dependents", crash_before_draining)
    with pytest.raises(RuntimeError):
        golem.main(["_execute", PARENT_ID, read_task(ledger_path, PARENT_ID)["session_id"]])
    monkeypatch.setattr(golem, "drain_dependents", drain_dependents)
    assert read_task(ledger_path, CHILD_ID)["status"] == golem.STATUS_WAITING
    started_task_ids: list[str] = []

    def start_in_process(task_id: str, task: dict[str, Any]) -> None:
        started_task_ids.append(task_id)
        golem.main(["_execute", task_id, task["session_id"]])

    monkeypatch.setattr(golem, "start_task_executor", start_in_process)
    golem.run_launch_agent_pass()
    golem.run_launch_agent_pass()

    assert started_task_ids == [PARENT_ID]
    assert logged_claude_calls(fake_commands) == [
        expected_claude_args(read_task(ledger_path, PARENT_ID), PARENT_PROMPT, task_id=PARENT_ID),
        expected_claude_args(read_task(ledger_path, CHILD_ID), CHILD_PROMPT, task_id=CHILD_ID),
        expected_claude_args(
            read_task(ledger_path, SIBLING_ID), SIBLING_PROMPT, task_id=SIBLING_ID
        ),
    ]
    assert read_task(ledger_path, GRANDCHILD_ID)["status"] == golem.STATUS_WAITING


@pytest.fixture
def started_task_ids(monkeypatch):
    task_ids: list[str] = []
    monkeypatch.setattr(
        golem, "start_task_executor", lambda task_id, task, **_: task_ids.append(task_id)
    )
    return task_ids


def test_agent_does_not_recover_draining_while_a_dependent_is_running(
    ledger_path, started_task_ids
):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_RUNNING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_WAITING, task_id=SIBLING_ID, after=PARENT_ID),
        },
    )
    child_session_id = read_task(ledger_path, CHILD_ID)["session_id"]
    child_run_lock_file = golem.acquire_run_lock(child_session_id)
    assert child_run_lock_file is not None
    try:
        golem.run_launch_agent_pass()
    finally:
        golem.release_run_lock(child_run_lock_file, child_session_id)

    assert started_task_ids == []


def test_agent_waits_to_start_tasks_until_their_known_directory_is_free(
    ledger_path, started_task_ids, tmp_path
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    own_worktree_task = task_record(
        golem.STATUS_SCHEDULED, task_id="own-worktree", agent_args=[WORKTREE_FLAG, "name"]
    )
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_NEEDS_REVIEW, task_id=PARENT_ID, worktree=str(worktree)
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_SCHEDULED, task_id=SIBLING_ID, after=PARENT_ID),
            "own-worktree": own_worktree_task,
        },
    )
    directory_lock_files = [
        golem.acquire_lock(golem.directory_lock_path(directory))
        for directory in (str(worktree), str(own_worktree_task["cwd"]))
    ]
    try:
        golem.run_launch_agent_pass()
    finally:
        for directory_lock_file in directory_lock_files:
            assert directory_lock_file is not None
            golem.release_lock(directory_lock_file)
    assert started_task_ids == []

    golem.run_launch_agent_pass()

    assert started_task_ids == [PARENT_ID, SIBLING_ID, "own-worktree"]


@pytest.mark.parametrize(
    ("status", "command_args"),
    [
        pytest.param(golem.STATUS_NEEDS_REVIEW, ["pickup", TASK_ID], id="pickup"),
        pytest.param(golem.STATUS_FAILED, ["retry", TASK_ID, "--now"], id="retry"),
    ],
)
def test_missing_worktree_is_reported_without_changing_task(
    installed_launch_agent, fake_commands, ledger_path, tmp_path, status, command_args
):
    missing_worktree = tmp_path / "missing"
    write_task(ledger_path, status, worktree=str(missing_worktree))
    original_tasks = read_tasks(ledger_path)

    with pytest.raises(SystemExit, match=re.escape(f"worktree is missing: {missing_worktree}")):
        golem.main(command_args)

    assert read_tasks(ledger_path) == original_tasks
    assert fake_commands["claude-args"].read_text(encoding="utf-8") == ""


def test_retry_creates_pending_worktree(
    installed_launch_agent, git_repository, ledger_path, monkeypatch
):
    pending_worktree = git_repository / ".claude" / "worktrees" / "pending"
    monkeypatch.chdir(git_repository)
    write_task(
        ledger_path, golem.STATUS_FAILED, worktree=str(pending_worktree), worktree_pending=True
    )

    golem.main(["retry", TASK_ID, "--now"])
    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])

    assert pending_worktree.is_dir()
    assert "worktree_pending" not in read_task(ledger_path)


def test_launch_agent_reaps_finished_executor_while_future_task_is_pending(
    fake_commands, ledger_path, monkeypatch
):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_SCHEDULED),
            "future": task_record(
                golem.STATUS_SCHEDULED, task_id="future", due_at=RESET_TIME.isoformat()
            ),
        },
    )
    start_task_executor = golem.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    sleep_count = 0

    def sleep_while_agent_polls(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 1:
            wait_until(lambda: fake_commands["claude-args"].read_text(encoding="utf-8") != "")
            wait_until(
                lambda: (
                    os.waitid(os.P_PID, processes[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                    is not None
                )
            )
            return
        assert processes[0].returncode is not None
        golem.cancel_task("future")

    monkeypatch.setattr(golem, "start_task_executor", record_executor)
    monkeypatch.setattr(golem.time, "sleep", sleep_while_agent_polls)

    assert golem.run_launch_agent() == 0
    assert processes[0].returncode is not None


def test_waiting_dependent_of_succeeded_parent_keeps_launch_agent_running():
    tasks = {
        PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
        TASK_ID: task_record(golem.STATUS_WAITING, after=PARENT_ID),
    }

    assert golem.has_pending_tasks(tasks)


def test_claim_records_golem_worktree_resource_and_dependent_reuses_it(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    golem.main(["run", PARENT_ID, PARENT_PROMPT, "--now"])
    golem.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID])
    parent = read_task(ledger_path, PARENT_ID)
    common_directory = golem.git_common_directory(str(git_repository))
    assert common_directory is not None
    parent_worktree = Path(common_directory).parent / ".claude" / "worktrees" / parent["session_id"]
    write_claude_output(
        fake_commands,
        CLAUDE_RESULT,
        is_verbose=True,
        working_directory=str(parent_worktree),
    )

    golem.main(["_execute", PARENT_ID, parent["session_id"]])

    parent_worktree = read_task(ledger_path, PARENT_ID)["worktree"]
    worktrees = golem.load_worktrees()
    resource = worktrees[str(Path(parent_worktree).resolve())]
    assert resource["owner"] == golem.WORKTREE_OWNER_GOLEM
    assert resource["repository"] == golem.git_common_directory(str(git_repository))
    assert read_task(ledger_path, CHILD_ID)["worktree"] == parent_worktree
    tasks = read_tasks(ledger_path)
    tasks[CHILD_ID]["status"] = golem.STATUS_FAILED
    write_tasks(ledger_path, tasks)

    assert golem.main(["retry", CHILD_ID, "--now"]) == 0
    assert read_task(ledger_path, CHILD_ID)["worktree"] == parent_worktree
    assert golem.load_worktrees()[str(Path(parent_worktree).resolve())] == resource


@pytest.mark.parametrize(
    ("owner", "expected_reason"),
    [
        pytest.param(None, "ownership unknown", id="legacy"),
        pytest.param(golem.WORKTREE_OWNER_EXTERNAL, "not created by Golem", id="external"),
    ],
)
def test_worktree_list_retains_non_golem_worktrees(
    capsys, git_repository, ledger_path, owner, expected_reason
):
    worktree = git_worktree(git_repository)
    write_task(ledger_path, golem.STATUS_DONE, worktree=str(worktree))
    if owner is not None:
        write_worktrees(
            {
                str(worktree.resolve()): {
                    "owner": owner,
                    "repository": golem.git_common_directory(str(git_repository)),
                    "session_id": "other",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            }
        )

    assert golem.main(["worktree", "list"]) == 0

    assert expected_reason in capsys.readouterr().out
    assert golem.main(["delete", TASK_ID]) == 0
    if owner is not None:
        resource = golem.load_worktrees()[str(worktree.resolve())]
        assert "released_at" not in resource
        assert "removed_at" not in resource


def test_delete_releases_worktree_only_after_its_last_task(owned_worktree, ledger_path):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_DONE, worktree=str(worktree)),
            "other": task_record(golem.STATUS_FAILED, task_id="other", worktree=str(worktree)),
        },
    )

    assert golem.main(["delete", TASK_ID]) == 0
    resource = golem.load_worktrees()[str(worktree.resolve())]
    assert "released_at" not in resource

    assert golem.main(["delete", "other"]) == 0
    assert golem.load_worktrees()[str(worktree.resolve())]["released_at"]


def test_delete_parent_and_waiting_child_releases_shared_worktree(owned_worktree, ledger_path):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, worktree=str(worktree)),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
        },
    )

    assert golem.main(["delete", PARENT_ID, CHILD_ID]) == 0

    assert golem.load_worktrees()[str(worktree.resolve())]["released_at"]


def test_delete_chain_removes_shared_worktree_with_waiting_dependents(owned_worktree, ledger_path):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID, worktree=str(worktree)),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_WAITING, task_id=SIBLING_ID, after=PARENT_ID),
        },
    )

    assert golem.main(["delete", CHILD_ID, "--chain", "--remove-worktree"]) == 0

    assert golem.load_tasks() == {}
    assert not worktree.exists()


def test_delete_chain_removes_worktree_holding_golem_hooks(owned_worktree, ledger_path):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(
                golem.STATUS_DONE,
                task_id=PARENT_ID,
                worktree=str(worktree),
                gate="false",
                gate_reason=GATE_REASON,
            ),
            CHILD_ID: task_record(golem.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
        },
    )
    golem.sync_hooks(str(worktree))
    assert golem.local_settings_path(str(worktree)).exists()

    assert golem.main(["delete", CHILD_ID, "--chain", "--remove-worktree"]) == 0

    assert not worktree.exists()


def test_delete_checks_every_worktree_before_removing_any(git_repository, ledger_path):
    clean_worktree = git_worktree(git_repository)
    git_output(git_repository, "worktree", "add", "-qb", "other-branch", "other")
    changed_worktree = git_repository / "other"
    (changed_worktree / "changed").write_text("keep", encoding="utf-8")
    tasks = {
        TASK_ID: task_record(golem.STATUS_DONE, worktree=str(clean_worktree)),
        SIBLING_ID: task_record(
            golem.STATUS_DONE, task_id=SIBLING_ID, worktree=str(changed_worktree)
        ),
    }
    write_tasks(ledger_path, tasks)
    write_worktrees(
        golem_worktree_resource(git_repository, clean_worktree)
        | golem_worktree_resource(git_repository, changed_worktree)
    )

    assert golem.main(["delete", TASK_ID, SIBLING_ID, "--remove-worktree"]) == 1

    assert clean_worktree.is_dir()
    assert read_tasks(ledger_path) == tasks


def test_delete_keeps_tasks_when_later_worktree_removal_fails(
    capsys, git_repository, ledger_path, monkeypatch
):
    first_worktree = git_worktree(git_repository)
    git_output(git_repository, "worktree", "add", "-qb", "other-branch", "other")
    second_worktree = git_repository / "other"
    tasks = {
        TASK_ID: task_record(golem.STATUS_DONE, worktree=str(first_worktree)),
        SIBLING_ID: task_record(
            golem.STATUS_DONE, task_id=SIBLING_ID, worktree=str(second_worktree)
        ),
    }
    write_tasks(ledger_path, tasks)
    write_worktrees(
        golem_worktree_resource(git_repository, first_worktree)
        | golem_worktree_resource(git_repository, second_worktree)
    )
    run_git = golem.run_git

    def fail_second_removal(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments and str(second_worktree.resolve()) in arguments:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="could not remove")
        return run_git(directory, *arguments)

    monkeypatch.setattr(golem, "run_git", fail_second_removal)

    assert golem.main(["delete", TASK_ID, SIBLING_ID, "--remove-worktree"]) == 1
    assert f"removed worktree: {first_worktree.resolve()}" in capsys.readouterr().out
    assert not first_worktree.exists()
    assert read_tasks(ledger_path) == tasks

    monkeypatch.setattr(golem, "run_git", run_git)

    assert golem.main(["delete", TASK_ID, SIBLING_ID, "--remove-worktree"]) == 0
    assert golem.load_tasks() == {}
    assert not second_worktree.exists()


def test_delete_retains_worktree_shared_with_retryable_task(owned_worktree, ledger_path):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_DONE, worktree=str(worktree)),
            "other": task_record(golem.STATUS_FAILED, task_id="other", worktree=str(worktree)),
        },
    )

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1
    assert worktree.is_dir()
    assert TASK_ID in golem.load_tasks()


def test_delete_prints_session_and_worktree(capsys, owned_worktree, ledger_path):
    worktree = owned_worktree()
    task = read_task(ledger_path)

    assert golem.main(["delete", TASK_ID]) == 0

    output = capsys.readouterr().out
    assert task["session_id"] in output
    assert str(worktree) in output


@pytest.mark.parametrize("owner", [None, golem.WORKTREE_OWNER_EXTERNAL])
def test_delete_does_not_remove_unknown_or_external_worktree(git_repository, ledger_path, owner):
    worktree = git_worktree(git_repository)
    write_task(ledger_path, golem.STATUS_DONE, worktree=str(worktree))
    if owner is not None:
        write_worktrees(
            {
                str(worktree.resolve()): {
                    "owner": owner,
                    "repository": golem.git_common_directory(str(git_repository)),
                    "session_id": "other",
                    "created_at": datetime.now(UTC).isoformat(),
                }
            }
        )

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1
    assert TASK_ID in golem.load_tasks()
    assert worktree.is_dir()
    if owner is not None:
        resource = golem.load_worktrees()[str(worktree.resolve())]
        assert "released_at" not in resource
        assert "removed_at" not in resource


def test_delete_with_no_worktree_keeps_task(ledger_path):
    write_task(ledger_path, golem.STATUS_DONE)

    with pytest.raises(SystemExit, match=TASK_ID):
        golem.main(["delete", TASK_ID, "--remove-worktree"])

    assert TASK_ID in golem.load_tasks()


@pytest.mark.parametrize(
    "status",
    [golem.STATUS_NEEDS_REVIEW, golem.STATUS_DONE, golem.STATUS_FAILED],
)
def test_delete_removes_task_and_worktree_and_preserves_branch(owned_worktree, status):
    worktree = owned_worktree(status)

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 0
    assert TASK_ID not in golem.load_tasks()
    assert not worktree.exists()
    assert golem.load_worktrees()[str(worktree.resolve())]["removed_at"]
    assert git_output(worktree.parent, "show-ref", "--verify", "refs/heads/worktree")


@pytest.mark.parametrize("hidden_by", [None, "config", "assume-unchanged", "skip-worktree"])
def test_delete_retains_worktree_with_local_changes(owned_worktree, hidden_by):
    worktree = owned_worktree()
    changed_file = worktree / "changed"
    if hidden_by == "config":
        git_output(worktree, "config", "status.showUntrackedFiles", "no")
    elif hidden_by is not None:
        changed_file = worktree / "README"
        index_flag = f"--{hidden_by}"
        git_output(worktree, "update-index", index_flag, "README")
    changed_file.write_text("keep", encoding="utf-8")

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1

    assert changed_file.read_text(encoding="utf-8") == "keep"
    assert "removal_error" not in golem.load_worktrees()[str(worktree.resolve())]
    assert "released_at" not in golem.load_worktrees()[str(worktree.resolve())]
    assert TASK_ID in golem.load_tasks()


@pytest.mark.parametrize(
    ("reference", "is_removed"),
    [
        pytest.param(None, False, id="unreferenced"),
        pytest.param("refs/worktree/local", False, id="private-ref"),
        pytest.param("refs/heads/retained", True, id="branch"),
        pytest.param("refs/tags/retained", True, id="tag"),
    ],
)
def test_delete_preserves_detached_commit(owned_worktree, reference, is_removed):
    worktree = owned_worktree()
    git_output(worktree, "switch", "--detach")
    (worktree / "commit").write_text("keep", encoding="utf-8")
    git_output(worktree, "add", "commit")
    git_output(
        worktree,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "--no-gpg-sign",
        "-qm",
        "unpublished",
    )
    commit_id = git_output(worktree, "rev-parse", "HEAD")
    if reference is not None:
        git_output(worktree, "update-ref", reference, commit_id)

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed
    assert git_output(worktree.parent, "cat-file", "-t", commit_id) == "commit"


@pytest.mark.parametrize("directory_field", ["cwd", "worktree"])
def test_delete_retains_worktree_used_by_nested_task(owned_worktree, ledger_path, directory_field):
    worktree = owned_worktree()
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_DONE, worktree=str(worktree)),
            "scheduled": task_record(
                golem.STATUS_SCHEDULED,
                task_id="scheduled",
                **{directory_field: str(worktree / "nested")},
            ),
        },
    )

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1
    assert worktree.is_dir()
    assert TASK_ID in golem.load_tasks()


def test_delete_retains_nested_worktree(owned_worktree):
    worktree = owned_worktree()
    nested_worktree = worktree / ".claude" / "worktrees" / "nested"
    exclude_path = Path(git_output(worktree, "rev-parse", "--git-path", "info/exclude"))
    with exclude_path.open("a", encoding="utf-8") as exclude_file:
        exclude_file.write("\n.claude/\n")
    git_output(worktree, "worktree", "add", "-qb", "nested", str(nested_worktree))
    changed_file = nested_worktree / "README"
    changed_file.write_text("keep", encoding="utf-8")
    assert not git_output(worktree, "status", "--porcelain", "--untracked-files=all")

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1

    assert changed_file.read_text(encoding="utf-8") == "keep"


def test_removed_worktree_is_reported_by_pickup_and_retry(
    installed_launch_agent, owned_worktree, ledger_path
):
    worktree = owned_worktree()
    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 0
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW, worktree=str(worktree))

    with pytest.raises(SystemExit, match="worktree was removed"):
        golem.main(["pickup", TASK_ID])
    write_task(ledger_path, golem.STATUS_FAILED, worktree=str(worktree))
    with pytest.raises(SystemExit, match="worktree was removed"):
        golem.main(["retry", TASK_ID, "--now"])


@pytest.mark.parametrize(
    ("retention_days", "released_ago", "expected_exists"),
    [
        pytest.param(None, timedelta(days=2), True, id="disabled"),
        pytest.param(1, None, True, id="unreleased"),
        pytest.param(1, timedelta(hours=23), True, id="before-retention"),
        pytest.param(1, timedelta(days=1), False, id="at-retention"),
    ],
)
def test_auto_remove_requires_release_and_retention(
    owned_worktree, retention_days, released_ago, expected_exists
):
    released_at = (
        (datetime.now(UTC) - released_ago).isoformat() if released_ago is not None else None
    )
    worktree = owned_worktree(released_at=released_at)
    if retention_days is not None:
        golem.set_worktree_auto_remove(retention_days)

    golem.auto_remove_worktrees()
    assert worktree.is_dir() is expected_exists


def test_launch_agent_removes_worktree_released_by_delete(owned_worktree):
    worktree = owned_worktree(golem.STATUS_FAILED)
    assert golem.main(["worktree", "auto-remove", "--after", "0"]) == 0
    assert golem.main(["delete", TASK_ID]) == 0

    assert golem.run_launch_agent() == 0

    assert not worktree.exists()


def test_worktree_list_retains_unreleased_retryable_task(capsys, owned_worktree):
    owned_worktree(golem.STATUS_FAILED)

    assert golem.main(["worktree", "list"]) == 0

    assert "retained" in capsys.readouterr().out


@pytest.mark.parametrize("is_nested_pickup", [False, True])
def test_delete_retains_worktree_in_use(
    owned_worktree, ledger_path, monkeypatch, tmp_path, is_nested_pickup
):
    worktree = owned_worktree()
    tasks = {TASK_ID: task_record(golem.STATUS_DONE, worktree=str(worktree))}
    pickup_id = TASK_ID
    if is_nested_pickup:
        nested = worktree / "nested"
        nested.mkdir()
        tasks[CHILD_ID] = task_record(golem.STATUS_DONE, task_id=CHILD_ID, cwd=str(nested))
        pickup_id = CHILD_ID
    write_tasks(ledger_path, tasks)
    hold_path = tmp_path / "hold"
    started_path = tmp_path / "started"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CLAUDE_STARTED", str(started_path))
    pickup_process = multiprocessing.get_context("fork").Process(
        target=golem.pickup, args=(pickup_id,), kwargs={"is_app": False}
    )
    pickup_process.start()
    try:
        wait_until(started_path.exists)
        assert golem.main(["worktree", "list"]) == 0
        assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1
        assert worktree.is_dir()
    finally:
        hold_path.unlink()
        pickup_process.join()
        assert pickup_process.exitcode == 0


@pytest.mark.parametrize(
    ("reason", "is_process_missing", "is_removed"),
    [
        pytest.param(
            "claude session fix-thing-session (pid 1 start 2026-10-16T00:00:00Z)",
            True,
            True,
            id="stale-claude-lock",
        ),
        pytest.param(
            "claude session fix-thing-session (pid 1 start 2026-10-16T00:00:00Z)",
            False,
            False,
            id="live-claude-lock",
        ),
        pytest.param("unverified lock", False, False, id="unverified-lock"),
        pytest.param(None, False, False, id="bare-lock"),
    ],
)
def test_delete_respects_worktree_locks(
    owned_worktree, monkeypatch, reason, is_process_missing, is_removed
):
    worktree = owned_worktree()
    git_output(
        worktree.parent,
        "worktree",
        "lock",
        *(["--reason", reason] if reason is not None else []),
        str(worktree),
    )

    def report_process(_: int, __: int) -> None:
        if is_process_missing:
            raise ProcessLookupError

    monkeypatch.setattr(golem.os, "kill", report_process)

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed


def test_delete_records_git_removal_failure(capsys, owned_worktree, monkeypatch):
    worktree = owned_worktree()
    run_git = golem.run_git

    def fail_removal(directory: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments:
            return subprocess.CompletedProcess(arguments, 1, stdout="", stderr="could not remove")
        return run_git(directory, *arguments)

    monkeypatch.setattr(golem, "run_git", fail_removal)

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == 1
    resource = golem.load_worktrees()[str(worktree.resolve())]
    assert resource["removal_error"] == "could not remove"
    assert "removed_at" not in resource
    assert worktree.is_dir()
    assert golem.main(["worktree", "list"]) == 0
    assert "could not remove" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "command", "is_nested"),
    [
        pytest.param(golem.STATUS_NEEDS_REVIEW, ["pickup", TASK_ID], False, id="pickup"),
        pytest.param(golem.STATUS_FAILED, ["retry", TASK_ID, "--now"], False, id="retry"),
        pytest.param(golem.STATUS_DONE, ["pickup", TASK_ID], True, id="nested-pickup"),
        pytest.param(golem.STATUS_FAILED, ["retry", TASK_ID, "--now"], True, id="nested-retry"),
    ],
)
def test_task_reuse_revokes_worktree_release(
    installed_launch_agent, owned_worktree, ledger_path, status, command, is_nested
):
    worktree = owned_worktree(status, released_at=RESET_TIME.isoformat())
    if is_nested:
        nested = worktree / "nested"
        nested.mkdir()
        write_task(ledger_path, status, cwd=str(nested))

    assert golem.main(command) == 0

    assert "released_at" not in golem.load_worktrees()[str(worktree.resolve())]


@pytest.mark.parametrize(
    ("state", "is_removed"),
    [("created", True), ("retried", True), ("replaced", False), ("preexisting", False)],
)
def test_cleanup_verifies_original_worktree_ownership(
    installed_launch_agent, git_repository, ledger_path, monkeypatch, tmp_path, state, is_removed
):
    monkeypatch.chdir(git_repository)
    claude_path = tmp_path / "claude"
    claude_path.write_text(
        """#!/bin/sh
if [ "${GOLEM_CLAUDE_EXIT:-0}" != 0 ]; then
    exit "$GOLEM_CLAUDE_EXIT"
fi
while [ "$#" -gt 0 ]; do
    if [ "$1" = '--worktree' ]; then
        shift
        /usr/bin/git worktree add -qb created ".claude/worktrees/$1" || exit 1
    fi
    shift
done
printf '%s' '{"result":"done"}'
""",
        encoding="utf-8",
    )
    claude_path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    golem.main(["run", TASK_ID, PROMPT, "--now"])
    task = read_task(ledger_path)
    worktree = git_repository / ".claude" / "worktrees" / task["session_id"]
    resource_before_retry = None

    if state == "preexisting":
        git_output(git_repository, "worktree", "add", "-qb", "external", str(worktree))
    if state == "retried":
        monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
        assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 1
        resource_before_retry = golem.load_worktrees()[str(worktree.resolve())]
        monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
        assert golem.main(["retry", TASK_ID, "--now"]) == 0
        retried_task = read_task(ledger_path)
        assert retried_task["session_id"] != task["session_id"]
        assert retried_task["worktree"] == str(worktree)
        task = retried_task

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    if state == "retried":
        assert golem.load_worktrees()[str(worktree.resolve())] == resource_before_retry
    assert golem.main(["close", TASK_ID]) == 0
    if state == "replaced":
        git_output(git_repository, "worktree", "remove", str(worktree))
        git_output(git_repository, "worktree", "add", "-qb", "external", str(worktree))

    assert golem.main(["delete", TASK_ID, "--remove-worktree"]) == int(not is_removed)
    assert worktree.is_dir() is not is_removed


def test_close_does_not_release_worktree(owned_worktree):
    worktree = owned_worktree(golem.STATUS_NEEDS_REVIEW)

    assert golem.main(["close", TASK_ID]) == 0

    assert "released_at" not in golem.load_worktrees()[str(worktree.resolve())]


@pytest.mark.parametrize(
    ("events", "exit_code", "result", "status"),
    [
        pytest.param(
            [
                {"type": "thread.started", "thread_id": "codex-thread"},
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Codex completed"},
                },
                {"type": "turn.completed", "usage": {}},
            ],
            0,
            "Codex completed",
            golem.STATUS_NEEDS_REVIEW,
            id="success",
        ),
        pytest.param(
            [
                {"type": "thread.started", "thread_id": "codex-thread"},
                {"type": "turn.failed", "error": {"message": "ordinary failure"}},
            ],
            1,
            "ordinary failure",
            golem.STATUS_FAILED,
            id="failure",
        ),
    ],
)
def test_codex_run_records_result_and_thread(
    installed_launch_agent,
    fake_commands,
    ledger_path,
    monkeypatch,
    tmp_path,
    capsys,
    events,
    exit_code,
    result,
    status,
):
    write_codex_events(monkeypatch, tmp_path, events)
    monkeypatch.setenv("GOLEM_CODEX_EXIT", str(exit_code))

    assert golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now", "--", "-m", "x"]) == 0
    task = read_task(ledger_path)
    assert task["backend"] == golem.BACKEND_CODEX
    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == exit_code

    task = read_task(ledger_path)
    assert task["thread_id"] == "codex-thread"
    assert task["result"] == result
    assert task["status"] == status
    assert logged_codex_args(fake_commands) == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-m",
        "x",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        golem.CODEX_PROVIDER_OVERRIDE,
        "-",
    ]
    assert fake_commands["codex-stdin"].read_text(encoding="utf-8").endswith(f"{PROMPT}\x1e\n")
    if status == golem.STATUS_NEEDS_REVIEW:
        assert golem.main(["show", TASK_ID]) == 0
        assert capsys.readouterr().out.endswith(f"{result}\n")


@pytest.mark.parametrize(
    ("primary", "secondary", "expected_offset"),
    [
        pytest.param({"usedPercent": 100, "resetsAt": 7200}, None, 7200, id="exhausted-primary"),
        pytest.param(
            {"usedPercent": 100, "resetsAt": 3600},
            {"usedPercent": 100, "resetsAt": 7200},
            7200,
            id="latest-exhausted",
        ),
        pytest.param({"usedPercent": 25, "resetsAt": 3600}, None, 3600, id="active"),
        pytest.param({"usedPercent": 0, "resetsAt": 3600}, None, None, id="unused"),
        pytest.param(None, None, None, id="null-primary"),
    ],
)
def test_next_codex_reset_time(
    fake_commands, monkeypatch, tmp_path, primary, secondary, expected_offset
):
    now = datetime.now(UTC)
    primary, secondary = (
        None
        if limit is None
        else {**limit, "resetsAt": int((now + timedelta(seconds=limit["resetsAt"])).timestamp())}
        for limit in (primary, secondary)
    )
    write_codex_rate_limits(monkeypatch, tmp_path, primary, secondary)

    reset_time = golem.next_codex_reset_time()

    if expected_offset is None:
        assert reset_time is None
    else:
        assert reset_time is not None
        assert abs((reset_time - now).total_seconds() - expected_offset) < 2


def test_codex_usage_limit_resumes_saved_conversation(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}},
        ],
    )
    reset_time = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    monkeypatch.setattr(golem, "next_codex_reset_time", lambda: reset_time)

    golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"])
    task = read_task(ledger_path)
    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    task = read_task(ledger_path)
    assert task["status"] == golem.STATUS_SCHEDULED
    assert datetime.fromisoformat(task["due_at"]) == reset_time

    write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}},
        ],
    )
    set_task_due_now(ledger_path)
    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert logged_codex_args(fake_commands)[-3:] == ["resume", "codex-thread", "-"]
    assert (
        fake_commands["codex-stdin"]
        .read_text(encoding="utf-8")
        .endswith(f"{golem.USAGE_LIMIT_RESUME_PROMPT}\x1e\n")
    )


def test_codex_creates_and_uses_golem_worktree(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch
):
    monkeypatch.chdir(git_repository)
    golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"])
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0

    worktree = Path(read_task(ledger_path)["worktree"])
    assert worktree.is_dir()
    assert git_output(worktree, "branch", "--show-current") == f"worktree-{task['session_id']}"
    assert logged_codex_args(fake_commands) == [
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        golem.CODEX_PROVIDER_OVERRIDE,
        "-",
    ]
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(worktree)
    assert golem.is_worktree_owner_verified(str(worktree), task["session_id"])
    resource = golem.load_worktrees()[str(worktree.resolve())]
    assert resource["owner"] == golem.WORKTREE_OWNER_GOLEM
    assert resource["session_id"] == task["session_id"]
    common_directory = golem.git_common_directory(str(git_repository))
    assert common_directory is not None
    exclude_path = Path(common_directory) / "info" / "exclude"
    assert ".claude/worktrees/" in exclude_path.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("fetched_age", "is_reachable", "is_fetched"),
    [
        pytest.param(timedelta(days=2), True, True, id="stale"),
        pytest.param(timedelta(hours=1), True, False, id="recent"),
        pytest.param(timedelta(days=2), False, False, id="unreachable"),
    ],
)
def test_codex_worktree_base_fetches_stale_default_branch(
    installed_launch_agent,
    git_repository,
    git_repository_template,
    ledger_path,
    monkeypatch,
    tmp_path,
    fetched_age,
    is_reachable,
    is_fetched,
):
    remote_path = tmp_path / "remote"
    shutil.copytree(git_repository_template, remote_path)
    git_output(git_repository, "remote", "add", "origin", str(remote_path))
    git_output(git_repository, "fetch", "-q", "origin")
    git_output(git_repository, "remote", "set-head", "origin", "--auto")
    cached_commit = git_output(git_repository, "rev-parse", "origin/HEAD")
    (remote_path / "README").write_text("updated\n", encoding="utf-8")
    git_output(
        remote_path,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-qam",
        "u",
    )
    remote_commit = git_output(remote_path, "rev-parse", "HEAD")
    if not is_reachable:
        git_output(git_repository, "remote", "set-url", "origin", str(tmp_path / "missing"))
    fetch_head_path = Path(
        git_output(
            git_repository, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"
        )
    )
    fetched_at = (datetime.now(UTC) - fetched_age).timestamp()
    os.utime(fetch_head_path, (fetched_at, fetched_at))
    monkeypatch.chdir(git_repository)
    golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"])

    assert golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]]) == 0

    worktree = Path(read_task(ledger_path)["worktree"])
    assert git_output(worktree, "rev-parse", "HEAD") == (
        remote_commit if is_fetched else cached_commit
    )
    assert golem.run_git(str(worktree), "rev-parse", "--abbrev-ref", "@{upstream}").returncode


@pytest.mark.parametrize(
    "agent_args",
    [
        pytest.param(["-s", "read-only"], id="short-sandbox"),
        pytest.param(["--sandbox=read-only"], id="sandbox"),
        pytest.param(["--yolo"], id="yolo"),
        pytest.param(["--approve-for-me"], id="approve"),
        pytest.param(["-c", 'sandbox_mode="read-only"'], id="sandbox-config"),
    ],
)
def test_codex_permission_arguments_suppress_unattended_default(
    installed_launch_agent, fake_commands, ledger_path, agent_args
):
    assert (
        golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now", "--", *agent_args]) == 0
    )
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert "--dangerously-bypass-approvals-and-sandbox" not in logged_codex_args(fake_commands)


def test_codex_rejects_api_key_accounts_before_execution(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    account_path = tmp_path / "account.json"
    account_path.write_text(
        json.dumps({"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}), encoding="utf-8"
    )
    monkeypatch.setenv("GOLEM_CODEX_ACCOUNT", str(account_path))

    with pytest.raises(SystemExit, match="ChatGPT subscription"):
        golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"])

    assert not any(call[0] == "exec" for call in logged_codex_calls(fake_commands))
    write_task(ledger_path, golem.STATUS_SCHEDULED, backend=golem.BACKEND_CODEX)
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 1
    assert read_task(ledger_path)["result"] == golem.CODEX_SUBSCRIPTION_ERROR
    assert not any(call[0] == "exec" for call in logged_codex_calls(fake_commands))


@pytest.mark.parametrize(
    ("agent_args", "argument"),
    [
        pytest.param(["--oss"], "--oss", id="oss"),
        pytest.param(["--local-provider=ollama"], "--local-provider=ollama", id="local-provider"),
        pytest.param(["-c", 'model_provider="x"'], 'model_provider="x"', id="provider-config"),
        pytest.param(
            ["--config=forced_login_method=api"],
            "forced_login_method=api",
            id="login-config",
        ),
        pytest.param(["--worktree"], "--worktree", id="worktree"),
    ],
)
def test_codex_run_rejects_billing_bypass_arguments(installed_launch_agent, agent_args, argument):
    with pytest.raises(SystemExit, match=re.escape(argument)):
        golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now", "--", *agent_args])


def test_codex_processes_remove_api_keys_from_every_child(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "forbidden")
    monkeypatch.setenv("CODEX_API_KEY", "forbidden")
    assert golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"]) == 0
    task = read_task(ledger_path)

    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert golem.main(["pickup", TASK_ID]) == 0
    assert {call[0] for call in logged_codex_calls(fake_commands)} == {
        "app-server",
        "exec",
        "resume",
    }
    assert set(fake_commands["codex-api-keys"].read_text(encoding="utf-8").splitlines()) == {"0 0"}


@pytest.mark.parametrize(("is_error", "reset_offset"), [(True, 3600), (False, -3600)])
def test_next_codex_reset_time_rejects_unavailable_or_past_limits(
    fake_commands, monkeypatch, tmp_path, is_error, reset_offset
):
    if is_error:
        monkeypatch.setenv("GOLEM_CODEX_ERROR", "1")
    else:
        write_codex_rate_limits(
            monkeypatch,
            tmp_path,
            {
                "usedPercent": 100,
                "resetsAt": int((datetime.now(UTC) + timedelta(seconds=reset_offset)).timestamp()),
            },
            None,
        )

    with pytest.raises(SystemExit, match=re.escape(golem.USAGE_ERROR)):
        golem.next_codex_reset_time()


def test_codex_app_server_reads_batched_responses(fake_commands, monkeypatch, tmp_path):
    reset_time = datetime.now(UTC) + timedelta(hours=1)
    write_codex_rate_limits(
        monkeypatch,
        tmp_path,
        {"usedPercent": 100, "resetsAt": int(reset_time.timestamp())},
        None,
    )
    monkeypatch.setenv("GOLEM_CODEX_BATCH_RESPONSES", "1")

    assert golem.next_codex_reset_time() == reset_time.replace(microsecond=0)


def test_codex_usage_limit_stores_result_and_stops_after_resume_limit(
    installed_launch_agent, ledger_path, monkeypatch, tmp_path
):
    usage_error = "You\N{RIGHT SINGLE QUOTATION MARK}ve hit your usage limit"
    write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "turn.failed", "error": {"message": usage_error}},
        ],
    )
    monkeypatch.setenv("GOLEM_CODEX_EXIT", "1")
    reset_time = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    monkeypatch.setattr(golem, "next_codex_reset_time", lambda: reset_time)
    assert golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"]) == 0
    session_id = read_task(ledger_path)["session_id"]

    for resume_count in range(golem.MAX_USAGE_LIMIT_RESUMES):
        set_task_due_now(ledger_path)
        assert golem.main(["_execute", TASK_ID, session_id]) == 0
        task = read_task(ledger_path)
        assert task["session_id"] == session_id
        assert task["result"] == usage_error
        assert task["usage_limit_resume_count"] == resume_count + 1
        assert datetime.fromisoformat(task["due_at"]) == reset_time

    set_task_due_now(ledger_path)
    assert golem.main(["_execute", TASK_ID, session_id]) == 1
    assert read_task(ledger_path)["status"] == golem.STATUS_FAILED


@pytest.mark.parametrize(
    ("status", "command"),
    [
        pytest.param(golem.STATUS_FAILED, "retry", id="retry"),
        pytest.param(golem.STATUS_SCHEDULED, "reschedule", id="reschedule"),
    ],
)
def test_codex_retry_and_reschedule_use_codex_reset_time(
    installed_launch_agent, ledger_path, monkeypatch, status, command
):
    reset_time = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=1)
    write_task(ledger_path, status, backend=golem.BACKEND_CODEX)
    monkeypatch.setattr(golem, "next_reset_time", lambda: pytest.fail("Claude lookup ran"))
    monkeypatch.setattr(golem, "next_codex_reset_time", lambda: reset_time)

    assert golem.main([command, TASK_ID]) == 0

    assert datetime.fromisoformat(read_task(ledger_path)["due_at"]) == reset_time


def test_codex_pickup_resumes_thread_in_task_worktree_and_rejects_app(
    installed_launch_agent, fake_commands, ledger_path, tmp_path
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    write_task(
        ledger_path,
        golem.STATUS_NEEDS_REVIEW,
        backend=golem.BACKEND_CODEX,
        thread_id="codex-thread",
        worktree=str(worktree),
    )

    assert golem.main(["pickup", TASK_ID, "--", "-m", "x"]) == 0
    assert logged_codex_args(fake_commands) == [
        "resume",
        "codex-thread",
        "-m",
        "x",
        "-c",
        golem.CODEX_PROVIDER_OVERRIDE,
    ]
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(worktree)
    write_task(ledger_path, golem.STATUS_NEEDS_REVIEW, backend=golem.BACKEND_CODEX)

    with pytest.raises(SystemExit, match="does not support Codex"):
        golem.main(["pickup", TASK_ID, "--app"])
    assert read_task(ledger_path)["status"] == golem.STATUS_NEEDS_REVIEW
    with pytest.raises(SystemExit, match="--oss"):
        golem.main(["pickup", TASK_ID, "--", "--oss"])


def test_codex_non_git_run_and_retry_keep_worktree_and_arguments(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, tmp_path
):
    assert golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"]) == 0
    task = read_task(ledger_path)
    assert golem.main(["_execute", TASK_ID, task["session_id"]]) == 0
    assert fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()[-1] == str(
        Path.cwd()
    )

    monkeypatch.chdir(git_repository)
    write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "failed-thread"},
            {"type": "turn.failed", "error": {"message": "ordinary failure"}},
        ],
    )
    monkeypatch.setenv("GOLEM_CODEX_EXIT", "1")
    assert golem.main(["run", "retry", PROMPT, "--backend", "codex", "--now", "--", "-m", "x"]) == 0
    failed = read_task(ledger_path, "retry")
    assert golem.main(["_execute", "retry", failed["session_id"]]) == 1
    worktree = read_task(ledger_path, "retry")["worktree"]
    monkeypatch.setenv("GOLEM_CODEX_EXIT", "0")
    write_codex_events(
        monkeypatch,
        tmp_path,
        [
            {"type": "thread.started", "thread_id": "fresh-thread"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}},
        ],
    )

    assert golem.main(["retry", "retry", "--now"]) == 0
    retried = read_task(ledger_path, "retry")
    assert retried["backend"] == golem.BACKEND_CODEX
    assert retried["worktree"] == worktree
    assert retried["agent_args"] == ["-m", "x"]
    assert golem.main(["_execute", "retry", retried["session_id"]]) == 0
    exec_calls = [call for call in logged_codex_calls(fake_commands) if call[0] == "exec"]
    assert "resume" not in exec_calls[-1]


@pytest.mark.parametrize(
    "backends",
    [
        pytest.param(
            (golem.BACKEND_CLAUDE, golem.BACKEND_CODEX, golem.BACKEND_CLAUDE), id="claude-parent"
        ),
        pytest.param(
            (golem.BACKEND_CODEX, golem.BACKEND_CLAUDE, golem.BACKEND_CODEX), id="codex-parent"
        ),
    ],
)
def test_mixed_backend_chain_shares_worktree_and_uses_fresh_conversations(
    installed_launch_agent, fake_commands, git_repository, ledger_path, monkeypatch, backends
):
    monkeypatch.chdir(git_repository)
    parent_backend, child_backend, grandchild_backend = backends
    assert golem.main(["run", "parent", PROMPT, "--backend", parent_backend, "--now"]) == 0
    assert (
        golem.main(["run", "child", PROMPT, "--backend", child_backend, "--after", "parent"]) == 0
    )
    assert (
        golem.main(
            ["run", "grandchild", PROMPT, "--backend", grandchild_backend, "--after", "child"]
        )
        == 0
    )
    parent = read_task(ledger_path, "parent")

    assert golem.main(["_execute", "parent", parent["session_id"]]) == 0
    tasks = read_tasks(ledger_path)
    worktree = tasks["parent"]["worktree"]
    assert {task["worktree"] for task in tasks.values()} == {worktree}
    assert len({task["session_id"] for task in tasks.values()}) == 3
    assert [tasks[task_id]["status"] for task_id in ("parent", "child", "grandchild")] == [
        golem.STATUS_DONE,
        golem.STATUS_DONE,
        golem.STATUS_NEEDS_REVIEW,
    ]
    codex_cwds = fake_commands["codex-cwd"].read_text(encoding="utf-8").splitlines()
    codex_executions = [
        (call, cwd)
        for call, cwd in zip(logged_codex_calls(fake_commands), codex_cwds, strict=True)
        if call[0] == "exec"
    ]
    assert len(codex_executions) == backends.count(golem.BACKEND_CODEX)
    assert all("resume" not in call and cwd == worktree for call, cwd in codex_executions)
    assert logged_claude_cwds(fake_commands)[-1] == worktree


def test_codex_home_reaches_launch_agent_executor(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path
):
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert golem.main(["run", TASK_ID, PROMPT, "--backend", "codex", "--now"]) == 0
    monkeypatch.delenv("CODEX_HOME")
    fake_commands["codex-args"].write_text("", encoding="utf-8")
    fake_commands["codex-home"].write_text("", encoding="utf-8")
    start_task_executor = golem.start_task_executor
    processes: list[subprocess.Popen[bytes]] = []

    def record_executor(task_id: str, task: dict[str, Any]) -> subprocess.Popen[bytes]:
        process = start_task_executor(task_id, task)
        processes.append(process)
        return process

    monkeypatch.setattr(golem, "start_task_executor", record_executor)
    golem.run_launch_agent_pass()

    assert len(processes) == 1
    processes[0].wait()
    homes = fake_commands["codex-home"].read_text(encoding="utf-8").splitlines()
    assert next(
        home
        for call, home in zip(logged_codex_calls(fake_commands), homes, strict=True)
        if call[0] == "exec"
    ) == str(codex_home)


class TerminalInput(io.StringIO):
    def __init__(self, answers, on_first_read=lambda: None):
        super().__init__(answers)
        self.on_first_read = on_first_read

    @override
    def isatty(self):
        return True

    @override
    def readline(self, size: int = -1) -> str:
        self.on_first_read()
        self.on_first_read = lambda: None
        return super().readline(size)


def write_idle_file(path, idle_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    modified_time = time.time() - idle_days * DAY_SECONDS
    os.utime(path, (modified_time, modified_time))


def write_transcript(session_id, idle_days):
    write_idle_file(
        Path.home() / ".claude" / "projects" / "project" / f"{session_id}.jsonl", idle_days
    )


@pytest.fixture
def write_pull_requests(fake_commands):
    def write(branch: str, pull_requests: list[dict[str, object]]) -> None:
        (fake_commands["gh-pull-requests"] / f"{branch}.json").write_text(
            json.dumps(pull_requests), encoding="utf-8"
        )

    return write


@pytest.fixture
def done_worktree(git_repository, ledger_path):
    worktree = git_worktree(git_repository)
    write_task(ledger_path, golem.STATUS_DONE, worktree=str(worktree))
    return worktree


@pytest.mark.parametrize(
    ("prune_args", "is_done_removed"), [(["prune"], False), (["prune", "--yes"], True)]
)
def test_prune_without_terminal_deletes_done_tasks_only_when_confirmed(
    ledger_path, monkeypatch, prune_args, is_done_removed
):
    statuses = [
        golem.STATUS_SCHEDULED,
        golem.STATUS_WAITING,
        golem.STATUS_RUNNING,
        golem.STATUS_NEEDS_REVIEW,
        golem.STATUS_FAILED,
        golem.STATUS_CANCELLED,
        golem.STATUS_DONE,
    ]
    write_tasks(ledger_path, {status: task_record(status, task_id=status) for status in statuses})
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))

    assert golem.main(prune_args) == 0

    removed_ids = (
        {golem.STATUS_CANCELLED, golem.STATUS_DONE} if is_done_removed else {golem.STATUS_CANCELLED}
    )
    assert set(golem.load_tasks()) == set(statuses) - removed_ids


@pytest.mark.parametrize(
    ("pull_requests", "idle_days", "is_removed"),
    [
        pytest.param([{"number": 1, "state": "MERGED"}], 0, True, id="merged-pr"),
        pytest.param([{"number": 1, "state": "CLOSED"}], 0, True, id="closed-pr"),
        pytest.param([{"number": 1, "state": "OPEN"}], None, False, id="open-pr"),
        pytest.param(
            [{"number": 2, "state": "MERGED"}, {"number": 1, "state": "OPEN"}],
            None,
            False,
            id="open-and-merged-pr",
        ),
        pytest.param([], golem.PRUNE_IDLE_DAYS, True, id="idle-session"),
        pytest.param([], golem.PRUNE_IDLE_DAYS - 1, False, id="active-session"),
        pytest.param([], None, True, id="missing-transcript"),
    ],
)
def test_prune_decides_done_tasks_by_pull_request_then_idle_time(
    done_worktree, write_pull_requests, ledger_path, pull_requests, idle_days, is_removed
):
    write_pull_requests(WORKTREE_BRANCH, pull_requests)
    if idle_days is not None:
        write_transcript(read_task(ledger_path)["session_id"], idle_days)

    assert golem.main(["prune", "--yes"]) == 0

    assert (TASK_ID not in golem.load_tasks()) is is_removed


@pytest.mark.parametrize(
    ("idle_days", "is_removed"),
    [
        pytest.param(golem.PRUNE_IDLE_DAYS, True, id="idle-conversation"),
        pytest.param(golem.PRUNE_IDLE_DAYS - 1, False, id="active-conversation"),
        pytest.param(None, True, id="missing-rollout"),
    ],
)
def test_prune_uses_codex_conversation_idle_time(ledger_path, tmp_path, idle_days, is_removed):
    codex_home = tmp_path / "saved-codex-home"
    write_task(
        ledger_path,
        golem.STATUS_DONE,
        backend=golem.BACKEND_CODEX,
        thread_id="codex-thread",
        environment={"PATH": os.environ["PATH"], "CODEX_HOME": str(codex_home)},
    )
    if idle_days is not None:
        rollout_name = "rollout-2030-01-01T00-00-00-codex-thread.jsonl"
        write_idle_file(codex_home / "sessions" / "2030" / "01" / "01" / rollout_name, idle_days)

    assert golem.main(["prune", "--yes"]) == 0

    assert (TASK_ID not in golem.load_tasks()) is is_removed


def test_prune_keeps_done_task_with_attached_pickup(done_worktree, write_pull_requests):
    write_pull_requests(WORKTREE_BRANCH, [{"number": 1, "state": "MERGED"}])
    pickup_lock_file = golem.acquire_lock(golem.directory_lock_path(str(done_worktree)))
    assert pickup_lock_file is not None
    try:
        assert golem.main(["prune", "--yes"]) == 0
    finally:
        golem.release_lock(pickup_lock_file)

    assert TASK_ID in golem.load_tasks()


@pytest.mark.parametrize(("idle_days", "is_removed"), [(golem.PRUNE_IDLE_DAYS, True), (0, False)])
def test_prune_without_gh_applies_only_idle_rule(
    done_worktree, write_pull_requests, ledger_path, monkeypatch, tmp_path, idle_days, is_removed
):
    write_pull_requests(WORKTREE_BRANCH, [{"number": 1, "state": "MERGED"}])
    write_transcript(read_task(ledger_path)["session_id"], idle_days)
    git_only_path = tmp_path / "git-only-bin"
    git_only_path.mkdir()
    (git_only_path / "git").symlink_to(str(shutil.which("git")))
    monkeypatch.setenv("PATH", str(git_only_path))

    assert golem.main(["prune", "--yes"]) == 0

    assert (TASK_ID not in golem.load_tasks()) is is_removed


@pytest.mark.parametrize(
    ("answers", "removed_ids"),
    [
        pytest.param("y\nn\ny\n", {"first", "third"}, id="yes-and-no"),
        pytest.param("n\na\n", {"second", "third"}, id="all"),
        pytest.param("y\nq\ny\n", {"first"}, id="quit"),
        pytest.param("y\n", {"first"}, id="end-of-input"),
    ],
)
def test_prune_applies_each_answer_in_turn(ledger_path, monkeypatch, answers, removed_ids):
    task_ids = ["first", "second", "third"]
    write_tasks(
        ledger_path,
        {task_id: task_record(golem.STATUS_DONE, task_id=task_id) for task_id in task_ids},
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput(answers))

    assert golem.main(["prune"]) == 0

    assert set(golem.load_tasks()) == set(task_ids) - removed_ids


def interrupt():
    raise KeyboardInterrupt


def test_prune_interrupted_at_prompt_deletes_nothing(ledger_path, monkeypatch):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_CANCELLED),
            SIBLING_ID: task_record(golem.STATUS_DONE, task_id=SIBLING_ID),
        },
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput("y\n", interrupt))

    assert golem.main(["prune"]) == 130

    assert set(golem.load_tasks()) == {TASK_ID, SIBLING_ID}


def test_prune_offers_parent_only_after_its_dependents_are_removed(ledger_path, monkeypatch):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_DONE, task_id=CHILD_ID, after=PARENT_ID),
            SIBLING_ID: task_record(golem.STATUS_DONE, task_id=SIBLING_ID),
        },
    )
    monkeypatch.setattr(sys, "stdin", TerminalInput("n\ny\n"))

    assert golem.main(["prune"]) == 0

    assert set(golem.load_tasks()) == {PARENT_ID, CHILD_ID}


def test_prune_keeps_parent_of_remaining_task(ledger_path):
    write_tasks(
        ledger_path,
        {
            PARENT_ID: task_record(golem.STATUS_DONE, task_id=PARENT_ID),
            CHILD_ID: task_record(golem.STATUS_CANCELLED, task_id=CHILD_ID, after=PARENT_ID),
            GRANDCHILD_ID: task_record(golem.STATUS_DONE, task_id=GRANDCHILD_ID, after=CHILD_ID),
            SIBLING_ID: task_record(golem.STATUS_FAILED, task_id=SIBLING_ID, after=PARENT_ID),
        },
    )

    assert golem.main(["prune", "--yes"]) == 0

    assert set(golem.load_tasks()) == {PARENT_ID, SIBLING_ID}


def test_prune_releases_worktree_after_its_last_task(owned_worktree):
    worktree = owned_worktree()

    assert golem.main(["prune", "--yes"]) == 0

    assert TASK_ID not in golem.load_tasks()
    assert worktree.is_dir()
    assert golem.load_worktrees()[str(worktree.resolve())]["released_at"]


@pytest.mark.parametrize("command", [["delete", TASK_ID], ["prune", "--yes"]])
def test_delete_and_prune_leave_in_place_directory_alone(
    installed_launch_agent, git_repository, ledger_path, monkeypatch, command
):
    monkeypatch.chdir(git_repository)
    golem.main(["run", TASK_ID, PROMPT, "--now", "--no-worktree"])
    golem.main(["_execute", TASK_ID, read_task(ledger_path)["session_id"]])
    tasks = read_tasks(ledger_path)
    tasks[TASK_ID]["status"] = golem.STATUS_CANCELLED
    write_tasks(ledger_path, tasks)

    assert golem.main(command) == 0

    assert TASK_ID not in golem.load_tasks()
    assert golem.load_worktrees() == {}


def attach_pickup(ledger_path, exit_stack):
    pickup_lock_file = golem.acquire_lock(golem.directory_lock_path(str(Path.cwd())))
    assert pickup_lock_file is not None
    exit_stack.callback(golem.release_lock, pickup_lock_file)


def add_dependent_task(ledger_path, exit_stack):
    tasks = read_tasks(ledger_path)
    tasks[CHILD_ID] = task_record(golem.STATUS_SCHEDULED, task_id=CHILD_ID, after=TASK_ID)
    write_tasks(ledger_path, tasks)


def replace_task(ledger_path, exit_stack):
    tasks = read_tasks(ledger_path)
    tasks[TASK_ID] = task_record(golem.STATUS_DONE, session_id="replacement-session")
    write_tasks(ledger_path, tasks)


@pytest.mark.parametrize("change_task", [attach_pickup, add_dependent_task, replace_task])
def test_prune_keeps_only_task_that_changes_while_prompting(
    ledger_path, monkeypatch, tmp_path, change_task
):
    write_tasks(
        ledger_path,
        {
            TASK_ID: task_record(golem.STATUS_DONE),
            SIBLING_ID: task_record(golem.STATUS_DONE, task_id=SIBLING_ID, cwd=str(tmp_path)),
        },
    )

    with contextlib.ExitStack() as exit_stack:
        monkeypatch.setattr(
            sys, "stdin", TerminalInput("y\ny\n", lambda: change_task(ledger_path, exit_stack))
        )
        assert golem.main(["prune"]) == 0

    tasks = golem.load_tasks()
    assert TASK_ID in tasks
    assert SIBLING_ID not in tasks


WATCHED_COMMAND = "ls -la"
BACKENDS = [golem.BACKEND_CLAUDE, golem.BACKEND_CODEX]


def write_agent_run_and_expected_progress(
    fake_commands, monkeypatch, tmp_path, backend, message, *, is_usage_limit=False
):
    command = f"{WATCHED_COMMAND}\nsecond line"
    if backend == golem.BACKEND_CODEX:
        events = [
            {"type": "thread.started", "thread_id": "codex-thread"},
            {"type": "item.completed", "item": {"type": "command_execution", "command": command}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": message}},
        ]
        if is_usage_limit:
            events.append(
                {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}
            )
        write_codex_events(monkeypatch, tmp_path, events)
        return f"[tool] command {WATCHED_COMMAND}\n{message}\n"
    assistant = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": command}},
                {"type": "text", "text": message},
            ]
        },
    }
    result = {
        "type": "result",
        "result": USAGE_LIMIT_RESULT if is_usage_limit else message,
        "api_error_status": golem.USAGE_LIMIT_HTTP_STATUS if is_usage_limit else None,
    }
    write_claude_events(fake_commands, [assistant, result])
    return f"[tool] Bash {WATCHED_COMMAND}\n{message}\n"


def run_now(ledger_path, backend):
    golem.main(["run", TASK_ID, PROMPT, "--backend", backend, "--now"])
    return read_task(ledger_path)


def watch_output(capsys):
    assert golem.main(["watch", TASK_ID]) == 0
    return capsys.readouterr().out


@contextlib.contextmanager
def watch_subprocess_output():
    with golem.subprocess.Popen(
        [*golem.golem_command(), "watch", TASK_ID], stdout=subprocess.PIPE, text=True
    ) as watch:
        assert watch.stdout is not None
        yield watch.stdout
    assert watch.returncode == 0


def write_progress(session_id, text=""):
    path = golem.progress_path(session_id)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("backend", BACKENDS)
def test_watch_follows_running_task_to_its_end(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, backend
):
    hold_path = tmp_path / "hold"
    hold_path.touch()
    monkeypatch.setenv("GOLEM_CLAUDE_HOLD", str(hold_path))
    monkeypatch.setenv("GOLEM_CODEX_HOLD", str(hold_path))
    progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, CLAUDE_RESULT
    )
    task = run_now(ledger_path, backend)
    executor = golem.start_task_executor(TASK_ID, task)

    try:
        wait_until(golem.progress_path(task["session_id"]).exists)
        with watch_subprocess_output() as watch_output_file:
            first_line = watch_output_file.readline()
            hold_path.unlink()
            rest = watch_output_file.read()
    finally:
        hold_path.unlink(missing_ok=True)
        executor.wait()

    assert first_line + rest == f"{progress}golem: task needs_review: {TASK_ID}\n"
    assert read_task(ledger_path)["result"] == CLAUDE_RESULT


@pytest.mark.parametrize("backend", BACKENDS)
def test_progress_write_failure_keeps_task_result(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, backend
):
    write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, CLAUDE_RESULT
    )
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", 0)
    monkeypatch.setattr(golem, "rotated_progress_path", lambda _: tmp_path / "missing" / "log")
    task = run_now(ledger_path, backend)

    golem.main(["_execute", TASK_ID, task["session_id"]])

    task = read_task(ledger_path)
    assert (task["status"], task["result"]) == (golem.STATUS_NEEDS_REVIEW, CLAUDE_RESULT)


def test_watch_exits_130_on_interrupt(ledger_path, monkeypatch):
    write_task(ledger_path, golem.STATUS_RUNNING)
    write_progress(read_task(ledger_path)["session_id"])

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(golem.time, "sleep", interrupt)

    assert golem.main(["watch", TASK_ID]) == 130


def test_watch_takes_no_locks_and_leaves_ledger_unchanged(ledger_path):
    write_task(ledger_path, golem.STATUS_RUNNING)
    task = read_task(ledger_path)
    path = write_progress(task["session_id"], "first\n")
    run_lock_file = golem.acquire_run_lock(task["session_id"])
    directory_lock_file = golem.acquire_lock(golem.directory_lock_path(task["cwd"]))
    assert run_lock_file is not None
    assert directory_lock_file is not None

    try:
        with watch_subprocess_output() as watch_output_file:
            first_line = watch_output_file.readline()
            with path.open("a", encoding="utf-8") as progress_file:
                progress_file.write("second\n")
            golem.save_tasks({TASK_ID: task_record(golem.STATUS_FAILED)})
            ledger_text = ledger_path.read_text(encoding="utf-8")
            rest = watch_output_file.read()
    finally:
        golem.release_run_lock(run_lock_file, task["session_id"])
        golem.release_lock(directory_lock_file)

    assert first_line + rest == f"first\nsecond\ngolem: task failed: {TASK_ID}\n"
    assert ledger_path.read_text(encoding="utf-8") == ledger_text


def test_watch_follows_progress_across_rotation(ledger_path, monkeypatch):
    write_task(ledger_path, golem.STATUS_RUNNING)
    task = read_task(ledger_path)
    write_progress(task["session_id"], "first\n")
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", len("first\n"))

    with watch_subprocess_output() as watch_output_file:
        first_line = watch_output_file.readline()
        with golem.progress_writer(task["session_id"]) as record_progress:
            record_progress("second")
            record_progress("third")
        golem.save_tasks({TASK_ID: task_record(golem.STATUS_FAILED)})
        rest = watch_output_file.read()

    assert golem.rotated_progress_path(task["session_id"]).exists()
    assert first_line + rest == f"first\nsecond\nthird\ngolem: task failed: {TASK_ID}\n"


@pytest.mark.parametrize("backend", BACKENDS)
def test_watch_prints_usage_limit_pause_and_its_resume(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, capsys, backend
):
    monkeypatch.setattr(golem, "next_reset_time", lambda: RESET_TIME)
    monkeypatch.setattr(golem, "next_codex_reset_time", lambda: RESET_TIME)
    paused_progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, "paused", is_usage_limit=True
    )
    task = run_now(ledger_path, backend)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    assert read_task(ledger_path)["status"] == golem.STATUS_SCHEDULED
    capsys.readouterr()

    resume_time = golem.display_due_at(RESET_TIME.isoformat(), datetime.now().astimezone().date())
    assert watch_output(capsys) == (
        f"{paused_progress}golem: task paused at the usage limit, resumes {resume_time}: {TASK_ID}\n"
    )

    resumed_progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, backend, "resumed"
    )
    set_task_due_now(ledger_path)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    capsys.readouterr()

    assert watch_output(capsys) == (
        f"{paused_progress}{resumed_progress}golem: task needs_review: {TASK_ID}\n"
    )


def test_watch_shows_only_the_run_after_a_retry(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, capsys
):
    write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, golem.BACKEND_CLAUDE, "first"
    )
    monkeypatch.setenv("GOLEM_CLAUDE_EXIT", "1")
    first_task = run_now(ledger_path, golem.BACKEND_CLAUDE)
    golem.main(["_execute", TASK_ID, first_task["session_id"]])
    monkeypatch.delenv("GOLEM_CLAUDE_EXIT")
    golem.main(["retry", TASK_ID, "--now"])
    retried_task = read_task(ledger_path)
    progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, golem.BACKEND_CLAUDE, "second"
    )
    golem.main(["_execute", TASK_ID, retried_task["session_id"]])
    capsys.readouterr()

    assert watch_output(capsys) == f"{progress}golem: task needs_review: {TASK_ID}\n"
    assert not golem.progress_path(first_task["session_id"]).exists()


def test_watch_prints_rotated_progress_first(
    installed_launch_agent, fake_commands, ledger_path, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(golem, "AGENT_LOG_MAX_BYTES", 1)
    progress = write_agent_run_and_expected_progress(
        fake_commands, monkeypatch, tmp_path, golem.BACKEND_CLAUDE, "done"
    )
    task = run_now(ledger_path, golem.BACKEND_CLAUDE)
    golem.main(["_execute", TASK_ID, task["session_id"]])
    capsys.readouterr()

    assert watch_output(capsys) == f"{progress}golem: task needs_review: {TASK_ID}\n"
    assert golem.rotated_progress_path(task["session_id"]).exists()


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (golem.STATUS_SCHEDULED, "task has not started"),
        (golem.STATUS_DONE, "task has no recorded progress"),
    ],
)
def test_watch_refuses_task_without_progress(ledger_path, status, message):
    write_task(ledger_path, status)

    with pytest.raises(SystemExit, match=f"golem: {message}: {TASK_ID}"):
        golem.main(["watch", TASK_ID])


def test_pickup_suggests_watch_for_running_task(ledger_path):
    write_task(ledger_path, golem.STATUS_RUNNING)
    directory_lock_file = golem.acquire_lock(golem.directory_lock_path(str(Path.cwd())))
    assert directory_lock_file is not None

    try:
        with pytest.raises(SystemExit, match=f"golem watch {TASK_ID}"):
            golem.main(["pickup", TASK_ID])
    finally:
        golem.release_lock(directory_lock_file)


def test_delete_removes_recorded_progress(ledger_path):
    write_task(ledger_path, golem.STATUS_DONE)
    session_id = read_task(ledger_path)["session_id"]
    paths = [write_progress(session_id), golem.rotated_progress_path(session_id)]
    paths[1].touch()

    assert golem.main(["delete", TASK_ID]) == 0
    assert not any(path.exists() for path in paths)
