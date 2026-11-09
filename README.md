# golem

golem is an orchestrator for coding agents.

Queue agent sessions, chain them together and schedule them around your usage limits. golem runs them in the background, each in its own git worktree, and notifies you when one needs review. Setup scripts, gates and aliases let you build your own workflows on top.

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
# Queue a task. --now skips the wait for your next usage reset
golem run fix-login "Fix the login redirect" --now

# Follow tasks on a live TUI board. Press q to quit
golem board

# Open its session to review the result
golem pickup fix-login
```

## More examples

```bash
# Run in Codex at 9pm
golem run fix-signup "Fix the signup redirect" --backend codex --at 9pm

# The script must print {"directory": "<path>"} for the task to run in
# The review runs if the tests pass (exit code of zero). Arguments after -- go to the agent
golem run add-auth "Add password reset" \
  --no-worktree \
  --setup ./create-worktree.sh \
  --gate "make test" \
  --then review-auth "Review the password reset changes and fix what you find" \
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
| `golem pickup` | Opens a task's session and marks the task done |
| `golem prune` | Cleans up cancelled and finished tasks |

`golem --help` lists the rest.

## Config

golem reads `~/.config/golem/config.toml`, or the same path under `$XDG_CONFIG_HOME`.

```toml
# Backend for golem run when --backend is missing. Defaults to claude
default_backend = "codex"

[aliases]
# eg. golem review 86
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
