# golem

golem is an orchestrator for coding agents.

Queue tasks, chain them together and schedule them around your usage limits. golem runs the agents in the background, each in its own git worktree, and notifies you when something needs review. Setup scripts, gates and aliases let you build your own workflows on top.

## Install

Requires macOS and:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- git
- [Claude Code](https://code.claude.com/docs/en/setup) or [Codex](https://developers.openai.com/codex), logged in
- [gh](https://cli.github.com/), optional, for PR status

```bash
uv tool install git+https://github.com/Bdeering1/golem
golem agent install
```

## Quick start

Tasks run unattended, so golem turns off permission prompts unless you [set your own](#more-examples).

```bash
# Queue a task. --now skips the wait for your next usage reset
golem run fix-login "Fix the login redirect" --now

# Follow it on a live board. Press q to quit
golem board

# Open its session to review the result
golem pickup fix-login
```

## More examples

```bash
# Start at your next usage reset
golem run add-tests "Add tests for the login redirect"

# Start at 9pm in Codex
golem run fix-signup "Fix the signup redirect" --at 9pm --backend codex

# Review the work only if the tests pass
golem run add-auth "Add password reset" \
  --gate "make test" \
  --then review-auth "Review the password reset changes and fix what you find"

# Pass arguments to the agent after --
golem run fix-logout "Fix the logout redirect" -- --model opus

# Replace the default permissions. In Claude Code, this denies any tool not on the list
golem run fix-typos "Fix the typos in the docs" -- --permission-mode dontAsk --allowedTools "Read Glob Grep Edit"
```

## Commands

| Command | What it does |
| --- | --- |
| `golem run` | Queues a task |
| `golem board` | Shows all tasks on a live board |
| `golem list` | Prints all tasks |
| `golem watch` | Streams a running task's progress |
| `golem show` | Prints a task's final result |
| `golem pickup` | Opens a task's session and marks the task done |
| `golem prune` | Cleans up cancelled and finished tasks |

`golem --help` lists the rest.

## Config

golem reads `~/.config/golem/config.toml`, or the same path under `$XDG_CONFIG_HOME`.

```toml
[aliases]
# golem review 86
review = 'run review-{1} "Review PR {1} and fix what you find" --now'

[env]
# Set for every task. --env overrides it for one run
NODE_ENV = "development"
```

Worktrees stay on disk after you delete their task. To clean them up:

```bash
# Remove them after 14 days, unless they have local changes
golem worktree auto-remove --after 14
```
