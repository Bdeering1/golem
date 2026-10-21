## Tooling

- Run every command through `uv run`.
- Verify changes with: `pytest`, `ruff check .`, `ruff format --check .`, `basedpyright`.
- Enable the pre-commit hook once per clone: `git config core.hooksPath .githooks`. It runs ruff
  check, ruff format and basedpyright on staged files; CI runs every check on each pull request.
- Never merge a PR with failing checks. GitHub Free cannot enforce required checks, so this rule is
  the gate.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (Bdeering1/golem) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
