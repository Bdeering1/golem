<img width="2172" height="724" alt="exec-443e6493-249b-4510-a8f1-6cc0f689d857" src="https://github.com/user-attachments/assets/4bae1593-f078-4b47-8322-fbe6dc71f01d" />

A CLI-based orchestrator for coding agents.

Queue agent sessions, chain them together, run them on different accounts, and schedule them around your usage limits. Sessions run in the background in isolated worktrees. Setup scripts, gates, and aliases let you build your own workflows on top.

## Install

golem runs on macOS and needs:

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Claude Code](https://code.claude.com/docs/en/setup) or [Codex](https://developers.openai.com/codex)
- [gh](https://cli.github.com/) (optional, for PR status)

```bash
uv tool install git+https://github.com/Bdeering1/golem
golem agent install
```

## Quick start

golem calls each queued session a task. Tasks run unattended, so golem turns off permission prompts unless you pass your own permission flag.

```bash
# Start a task
golem run "Fix the login redirect" --label fix-login --now

# Track tasks on a live TUI board
golem board

# Open a task's session
golem pickup fix-login
```

## More examples

```bash
# Run in Codex at 9pm
golem run "Fix the signup redirect" --backend codex --at 9pm

# Run a Codex task as a T3 Code thread (--provider defaults to claude)
golem run "Fix the logout redirect"
  --backend t3
  -- --provider codex --model gpt-6-astra --effort high

# Run a task on another account (added using `golem account add claude`)
golem run "Fix the billing page" --account work@example.com

# The script must print {"directory": "<path>"} for the task to run in
# The review runs if the gate's exit code is zero
# Arguments after -- are passed through to `claude -p`
golem run "Add password reset" \
  --no-worktree \
  --setup ./create-worktree.sh \
  --gate "make test" \
  --then review "Review the password reset changes and fix what you find" \
  -- --permission-mode auto
```

## Commands

| Command | What it does |
| --- | --- |
| `golem run` | Queues a task |
| `golem board` | Shows all tasks on a live board |
| `golem list` | Prints all tasks |
| `golem watch` | Streams a running task's progress |
| `golem show` | Prints a task's final result |
| `golem pickup` | Opens a task's session |
| `golem prune` | Deletes cancelled tasks and done chains |
| `golem account` | Adds, lists, and removes Claude accounts |

`golem --help` lists the rest.

## Config

golem reads `~/.config/golem/config.toml`, or the same path under `$XDG_CONFIG_HOME`.

```toml
default_backend = "codex" # Claude is the default if unspecified

# Shell aliases with template args
[aliases]
review = 'run "Review PR {1} and fix what you find" --label review-{1} --now'

# Env variables to set for every task
[env]
NODE_ENV = "development"

[usage_windows]
claude = ["6am", "11am"] # Open a usage window at these times each day (default account)
"work@example.com" = ["8am", "1pm"] # Define usage windows for another account
```

Worktrees stay on disk after you delete their task. To clean them up:

```bash
# Remove after 14 days, unless there are local changes
golem worktree auto-remove --after 14
```
