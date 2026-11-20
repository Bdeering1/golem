import contextlib
import curses
import json
import os
import select
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from golem import board, cli, config, ledger, spawn
from golem.backends import claude
from golem.worktrees import git, task_directories

if TYPE_CHECKING:
    from collections.abc import Generator

DEFAULT_EMAIL = "me@example.com"
WORK_EMAIL = "work@example.com"
CREDENTIALS = ".credentials.json"
TASK_ID = "fix-thing"
PROMPT = "fix the thing"
LABEL = "GH-115"
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
SAVED_LANG = "en_CA.UTF-8"
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
CHILD_LANG = "fr_CA.UTF-8"
CURRENT_LANG = "en_US.UTF-8"
CLAUDE_RESULT = "completed the task"
USAGE_LIMIT_RESULT = "You've hit your limit · resets 3pm"
RESET_TIME = datetime(2030, 1, 1, 10, tzinfo=UTC)
WORKTREE_BRANCH = "worktree"
DAY_SECONDS = 24 * 60 * 60
GATE_STOP_REASON = "no pull request yet"
PARENT_SESSION = f"{PARENT_ID}-session"


ESCAPE_KEYS = [27, -1]
ENTER_KEY = ord("\n")


def interrupt():
    raise KeyboardInterrupt


class FakeScreen:
    def __init__(self, keys, size):
        self.keys = list(keys)
        self.size = size
        self.lines = {}
        self.frames = []

    def getch(self):
        key = self.keys.pop(0)
        return key() if callable(key) else key

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.lines = {}

    def clear(self):
        self.lines = {}

    def addstr(self, y, x, text, _attribute):
        line = self.lines.get(y, "").ljust(x)
        self.lines[y] = line[:x] + text + line[x + len(text) :]

    def refresh(self):
        self.frames.append([self.lines.get(y, "") for y in range(self.size[0])])

    def timeout(self, _milliseconds):
        pass


def use_fake_terminal(monkeypatch, keys, size=(24, 80), glyphs=board.ASCII_GLYPHS):
    screen = FakeScreen(keys, size)

    @contextlib.contextmanager
    def fake_terminal_screen() -> Generator[tuple[FakeScreen, bool, board.Glyphs]]:
        yield screen, False, glyphs

    monkeypatch.setattr(board, "is_terminal", lambda: True)
    monkeypatch.setattr(board, "terminal_screen", fake_terminal_screen)
    return screen


def fail_screen_start():
    raise curses.error


def use_terminal_failing_to_start(monkeypatch):
    monkeypatch.setattr(board, "is_terminal", lambda: True)
    monkeypatch.setattr(board, "start_screen", fail_screen_start)


def read_task(ledger_path, task_id=TASK_ID):
    return json.loads(ledger_path.read_text(encoding="utf-8"))[task_id]


def task_record(status: str, *, task_id=TASK_ID, **fields: object) -> ledger.Task:
    record: ledger.Task = {
        "agent_args": [],
        "cwd": str(Path.cwd()),
        "due_at": datetime.now(UTC).isoformat(),
        "backend": ledger.BACKEND_CLAUDE,
        "environment": {"PATH": os.environ["PATH"]},
        "prompt": PROMPT,
        "session_id": f"{task_id}-session",
        "status": status,
    }
    record.update(cast(ledger.Task, fields))
    return record


def write_task(ledger_path, status, *, task_id=TASK_ID, **fields: object):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({task_id: task_record(status, task_id=task_id, **fields)}), encoding="utf-8"
    )


def write_tasks(ledger_path, tasks):
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(tasks), encoding="utf-8")


def write_config_text(text):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_config_table(table_name, values):
    lines = [
        f"[{table_name}]",
        *(f"{name} = {json.dumps(value)}" for name, value in values.items()),
    ]
    write_config_text("\n".join(lines) + "\n")


def write_aliases(aliases):
    write_config_table("aliases", aliases)


def list_output(capsys):
    assert cli.main(["list"]) == 0
    return capsys.readouterr().out


def list_row(output, task_id):
    return next(line for line in output.splitlines() if line.startswith(f"{task_id} "))


def write_worktrees(worktrees):
    ledger.worktree_ledger_path().write_text(json.dumps(worktrees), encoding="utf-8")


def git_output(repository_path, *arguments: str):
    result = spawn.subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=repository_path,
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def add_github_remote(repository_path, name="repository"):
    git_output(repository_path, "remote", "add", "origin", f"https://github.com/owner/{name}.git")


def git_worktree(repository_path):
    git_output(repository_path, "worktree", "add", "-qb", WORKTREE_BRANCH, "worktree")
    return repository_path / "worktree"


def golem_worktree_resource(repository_path, worktree_path, *, released_at=None):
    resource = {
        "owner": task_directories.WORKTREE_OWNER_GOLEM,
        "repository": git.git_common_directory(str(repository_path)),
        "session_id": "fix-thing-session",
        "created_at": datetime.now(UTC).isoformat(),
    }
    if released_at is not None:
        resource["released_at"] = released_at
    marker_path = task_directories.worktree_owner_marker_path(str(worktree_path))
    assert marker_path is not None
    marker_path.write_text("fix-thing-session", encoding="utf-8")
    return {str(worktree_path.resolve()): resource}


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


def logged_calls(fake_commands, command):
    calls: list[list[str]] = []
    for call in fake_commands[f"{command}-args"].read_text(encoding="utf-8").split("\x1e\n"):
        arguments = call.splitlines()
        if arguments:
            calls.append(arguments)
    return calls


def logged_osascript_calls(fake_commands):
    return logged_calls(fake_commands, "osascript")


def write_codex_events(monkeypatch, tmp_path, events):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    monkeypatch.setenv("GOLEM_CODEX_EVENTS", str(events_path))
    return events_path


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
        claude.VERBOSE_ARGUMENT,
        "--name",
        task_id,
        SKIP_PERMISSIONS,
    ]


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


def create_waiting_chain():
    cli.main(["run", PARENT_ID, PARENT_PROMPT, "--at", AT_TEXT, "--", *AGENT_ARGS])
    cli.main(["run", CHILD_ID, CHILD_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])
    cli.main(["run", GRANDCHILD_ID, GRANDCHILD_PROMPT, "--after", CHILD_ID, "--", *AGENT_ARGS])
    cli.main(["run", SIBLING_ID, SIBLING_PROMPT, "--after", PARENT_ID, "--", *AGENT_ARGS])


def gate_command(tmp_path, *, exit_code=0, stderr=""):
    log_path = tmp_path / "gate-stdin"
    return (
        f"cat >> {log_path}; echo >> {log_path}; "
        f"printf %s {shlex.quote(stderr)} >&2; exit {exit_code}"
    )


def schedule_gated_chain(ledger_path, gate, *extra_arguments: str, then_arguments=()):
    cli.main(
        [
            *["run", PARENT_ID, PARENT_PROMPT, "--now", "--gate", gate, *extra_arguments],
            *["--then", CHILD_ID, CHILD_PROMPT, *then_arguments],
        ]
    )
    return read_task(ledger_path, PARENT_ID)


def run_gated_chain(ledger_path, gate, *extra_arguments: str):
    parent = schedule_gated_chain(ledger_path, gate, *extra_arguments)
    assert cli.main(["_execute", PARENT_ID, parent["session_id"]]) == 0
    return parent


def write_parent_with_waiting_child(
    ledger_path, parent_status: str = ledger.STATUS_FINISHED, **parent_fields: object
):
    tasks = {
        PARENT_ID: task_record(parent_status, task_id=PARENT_ID, **parent_fields),
        CHILD_ID: task_record(ledger.STATUS_WAITING, task_id=CHILD_ID, after=PARENT_ID),
    }
    write_tasks(ledger_path, tasks)


def write_idle_file(path, idle_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    modified_time = time.time() - idle_days * DAY_SECONDS
    os.utime(path, (modified_time, modified_time))


def write_transcript(session_id, idle_days):
    write_idle_file(
        Path.home() / ".claude" / "projects" / "project" / f"{session_id}.jsonl", idle_days
    )


def current_minute_window_time() -> str:
    return f"{datetime.now().astimezone():%I:%M%p}".lstrip("0").lower()


def status_text(email: str, org_id: str, org_name: str) -> str:
    return json.dumps({"loggedIn": True, "email": email, "orgId": org_id, "orgName": org_name})


def auth_calls(fake_commands):
    return fake_commands["claude-auth"].read_text(encoding="utf-8").splitlines()
