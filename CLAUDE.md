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

## Rules

Each rule names what enforces it. When a rule has no check, it is a judgment call: apply it yourself.

| Rule | Enforced by |
| --- | --- |
| No import cycles, and `golem.backends` never imports `golem.clients`. Put a rule two modules share in the lower one (`ledger` for task-record predicates, `chains` for chain predicates). | `tests/test_architecture.py` |
| Read external JSON (T3 Code, Codex and Claude output) through a TypedDict checked where it enters golem, not `cast()`. | `test_cast_count_only_falls` in `tests/test_architecture.py` |
| Tests never depend on the developer's git config. | `isolated_git_config` in `tests/conftest.py` |
| Don't add backward compatibility for old ledger or config records. | Judgment |
