# golem

golem queues Claude Code and Codex tasks to run at your next usage reset, then notifies you when each one is ready to review.

Each task runs in the background in its own git worktree, so you can queue several for one repository and keep working in your checkout. A task that hits the usage limit resumes after the reset. When a task finishes, `golem pickup` opens its session in Claude Code or Codex, in that worktree.

| Command | What it does |
| --- | --- |
| `golem run <id> "<prompt>"` | Queues a task. `--file prompt.md` takes the prompt from a file in place of `"<prompt>"` |
| `golem board` | Shows your tasks on a live full-screen board. Press `q` to quit |
| `golem list` | Prints your tasks. `--all` adds done tasks whose PRs are merged or closed |
| `golem watch <id>` | Streams a task's progress until it stops |
| `golem show <id>` | Prints a task's final result |
| `golem pickup <id>` | Opens the task's session and marks the task done |
| `golem prune` | Deletes cancelled tasks without asking. Asks before it deletes a done task if all its PRs are merged or closed, its directory is missing, or its session has been idle for 7 days |

`golem --help` lists the other commands. `golem <command> --help` lists a command's flags.

## Install

golem's background agent runs under launchd, so golem needs macOS.

Dependencies:

- [uv](https://docs.astral.sh/uv/getting-started/installation/), to install golem. uv fetches the Python version golem needs.
- git.
- [Claude Code](https://code.claude.com/docs/en/setup), logged in.
- Or [Codex](https://developers.openai.com/codex), logged in to a ChatGPT account. golem rejects API key accounts.
- [gh](https://cli.github.com/), optional. It adds PR state to `golem list` and `golem prune`.

```bash
uv tool install git+https://github.com/Bdeering1/golem
golem agent install
```

Run `golem agent install` once before your first task. It sets up the background agent, which then starts at every login. `golem run` refuses to queue a task until you do this, and asks you to run it again when the agent is out of date.

## Quick start

Run these commands inside a git repository. Tasks run without permission prompts. [Permissions](#permissions) shows how to change that.

Outside a repository, or in one with no commits, a task runs in the current directory. Add `--no-worktree` to `golem run` to do the same inside a repository.

### 1. Queue a task

```bash
golem run fix-login "Fix the login redirect" --now
```

`--now` starts the task right away. Without it, the task starts at the next usage reset, or right away if no usage window is active. `--at 9pm` or `--at "8:30am tomorrow"` picks a time. `--backend codex` runs the task in Codex.

### 2. Follow it

```bash
golem watch fix-login
```

If this prints `task has not started`, wait a few seconds and run it again. `golem board` shows all your tasks at once. A macOS notification tells you when a task needs review or fails.

### 3. Pick it up for review

```bash
golem pickup fix-login
```

This opens the session in the worktree and marks the task done. `--client claude-desktop` opens a Claude task in the Claude desktop app. Pass `--auto-pickup` to `golem run` to open it in the Claude desktop app as soon as the task succeeds.

## Permissions

Tasks run without permission prompts. golem passes `--dangerously-skip-permissions` to Claude and `--dangerously-bypass-approvals-and-sandbox` to Codex. Arguments after `--` go to Claude or Codex. A permission flag there replaces golem's default.

```bash
golem run add-tests "Add tests for the login redirect" -- --permission-mode acceptEdits --model opus
```

## Config

golem reads `~/.config/golem/config.toml`, or `$XDG_CONFIG_HOME/golem/config.toml` when set. golem doesn't create the file, so create it when you need it.

```toml
[aliases]
tonight = "run {1} {2} --at 9pm -- --model opus"

[env]
NODE_ENV = "development"
```

`[aliases]` maps a name to a golem command line. golem replaces `{1}`, `{2}` and so on with the alias's arguments. With the alias above, this queues `fix-signup` for 9pm:

```bash
golem tonight fix-signup "Fix the signup redirect"
```

An alias can't call another alias. If an alias has the same name as a built-in command, golem runs the built-in command. `golem alias list` prints your aliases.

`[env]` sets environment variables for every task. `--env NAME=VALUE` overrides them for one run.

Worktrees stay on disk after you delete their task. To remove them 14 days after the deletion, run the command below. golem keeps any worktree with local changes.

```bash
golem worktree auto-remove --after 14
```

`golem worktree auto-remove --off` stops this.
