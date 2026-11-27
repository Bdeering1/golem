## Tooling

- Run every command through `uv run`.
- Verify changes with: `pytest`, `ruff check .`, `ruff format --check .`, `basedpyright`.
- Enable the pre-commit hook once per clone: `git config core.hooksPath .githooks`. It runs ruff
  check, ruff format --check and basedpyright on the working-tree copies of staged Python files;
  CI runs every check on each pull request.
- Never merge a PR with failing checks. GitHub Free cannot enforce required checks, so this rule is
  the gate.

## Issue tracker

Issues live in GitHub Issues (Bdeering1/golem).
