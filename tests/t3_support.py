import json
from dataclasses import dataclass, field
from pathlib import Path

from golem import ledger
from tests import support

T3_VERSION = "t3 v0.1.7"
ENVIRONMENT_ID = "environment"
PROJECT_ID = "project"
ADDED_PROJECT_ID = "added-project"
CODEX_THREAD = "codex-thread"
CACHED_CLAUDE_MODEL = "claude-cached"
CLAUDE_EFFORT_LEVELS = ["low", "medium", "high"]
SUCCESS_EXIT = {"_tag": "Success", "value": {"sequence": 3}}
FINISHED_PULL_REQUESTS = [{"url": "merged", "state": ledger.PULL_REQUEST_STATE_MERGED}]
CLAUDE_THREAD_ID = f"golem:claude:{support.TASK_ID}-session"
CODEX_THREAD_ID = f"golem:codex:{CODEX_THREAD}"
FAKE_T3 = """#!/bin/sh
printf '%s\\n' "$*" >> "$T3CODE_HOME/t3-args"
case "$1 $2" in
    "--version "*) echo 't3 v0.1.7' ;;
    "thread show")
        [ -e "$T3CODE_HOME/thread-present" ] && exit 0
        [ -e "$T3CODE_HOME/thread-show-error" ] && exit 1
        echo "Thread $3 is no longer available." >&2
        exit 1 ;;
    "auth session") echo token ;;
    "project add") printf '%s' "$3" > "$T3CODE_HOME/added-project" ;;
    "thread send")
        pwd > "$T3CODE_HOME/send-cwd"
        cat > "$T3CODE_HOME/sent-message"
        while [ -e "$T3CODE_HOME/send-hold" ]; do sleep 0.05; done
        [ -f "$T3CODE_HOME/send-output" ] && cat "$T3CODE_HOME/send-output"
        exit "$(cat "$T3CODE_HOME/send-exit" 2>/dev/null || echo 0)" ;;
esac
"""


@dataclass
class FakeServer:
    home: Path
    project_root: str | None = None
    command_exit: dict = field(default_factory=lambda: SUCCESS_EXIT)
    settings_exit: dict = field(default_factory=lambda: SUCCESS_EXIT)
    is_creating_threads: bool = True
    commands: list = field(default_factory=list)
    settings_mutations: list = field(default_factory=list)
    projections: list = field(default_factory=lambda: [{"thread": {}, "turnItems": []}])
    # After a send, each read takes the next one; the last repeats. An exception entry is raised.
    follow_up_projections: list = field(default_factory=list)

    def fetch_projects(self, _origin, _token):
        added_path = self.home / "added-project"
        projects = []
        if self.project_root is not None:
            projects.append({"id": PROJECT_ID, "workspaceRoot": self.project_root})
        if added_path.exists():
            projects.append(
                {"id": ADDED_PROJECT_ID, "workspaceRoot": added_path.read_text(encoding="utf-8")}
            )
        return projects

    def dispatch_command_exit(self, _origin, _token, command):
        self.commands.append(command)
        if self.is_creating_threads and self.command_exit == SUCCESS_EXIT:
            (self.home / "thread-present").touch()
        return self.command_exit

    def update_settings_exit(self, _origin, _token, mutation):
        self.settings_mutations.append(mutation)
        return self.settings_exit

    def thread_projection(self, _origin, _token, _thread_id):
        send_count = sum(call.startswith("thread send") for call in self.t3_calls())
        if send_count and self.follow_up_projections:
            queue = self.follow_up_projections
            follow_up = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(follow_up, Exception):
                raise follow_up
            return follow_up
        return self.projections[min(send_count, len(self.projections) - 1)]

    def mark_running(self):
        self.runtime_path.write_text(
            json.dumps({"origin": "http://127.0.0.1:3773"}), encoding="utf-8"
        )

    @property
    def runtime_path(self):
        return self.home / "userdata" / "server-runtime.json"

    def t3_calls(self):
        log_path = self.home / "t3-args"
        return log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []


def thread_link(thread_id):
    return f"t3code://threads/{ENVIRONMENT_ID}/{thread_id.replace(':', '%3A')}"


def write_claude_model_cache(home):
    (home / "caches" / "claudeAgent.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "claude-other", "isDefault": False},
                    {
                        "slug": CACHED_CLAUDE_MODEL,
                        "aliases": ["cached"],
                        "isDefault": True,
                        "capabilities": {
                            "optionDescriptors": [
                                {
                                    "id": "effort",
                                    "type": "select",
                                    "options": [{"id": level} for level in CLAUDE_EFFORT_LEVELS],
                                }
                            ]
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


def sent_commands(server):
    return [
        {name: value for name, value in command.items() if name != "commandId"}
        for command in server.commands
    ]


def created_thread(server):
    return sent_commands(server)[-1]
