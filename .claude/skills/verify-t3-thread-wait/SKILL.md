---
name: verify-t3-thread-wait
description: Model-check how golem decides a T3 Code thread is quiet and whether it settles it. Use when changing golem/backends/t3.py's wait loop or golem/t3_code.py's run selection or settle check, or when test_formal_model_matches_code fails.
---

# Verify the T3 thread wait

`formal/T3ThreadWait.tla` is a TLA+ model of golem waiting on a T3 Code thread. TLC checks every order of T3 Code events (runs queued, started, completed or cancelled before starting; subagents ending) against golem's two decisions:

- **Poll**: one pass of `wait_for_quiet_thread` in `golem/backends/t3.py`. It finishes the task once the latest run is inactive and no subagent is unfinished.
- **Settle**: `settle_idle_thread` and `is_thread_settleable` in `golem/t3_code.py`. It runs once after a dependent succeeds and is never retried after a skip.

Properties, each with the harm it prevents:

- `FinishesOnlyWhenQuiet`: golem records a result while a run is still queued or running, so dependents start on unfinished work.
- `ResultComesFromARunThatRan`: the recorded result belongs to a run that never ran, such as a "cancelled" result for a working thread.
- `SkipsSettleOnlyForALaterRun`: golem skips settling a thread with no later run, and the thread is never settled.

## Run it

`formal/check.sh` needs Java 11 or newer and downloads a pinned `tla2tools.jar` on first run. It runs in about 5 seconds, and CI runs the same script.

- `current.cfg` models the code as it is and must pass.
- `wait-before-fix.cfg` and `settle-before-fix.cfg` model the code before Fix/Settle On Latest Active Run, when both decisions used `t3_code.latest_run`. Each must still fail. If one stops failing, the model can no longer catch that bug.

Read every TLC result with the `statechart` skill: read its `SKILL.md` and follow it. It tells a counterexample as its shortest path in plain words, and charts the states when the check passes.

## When you change covered code

`tests/test_formal_model.py` hashes the covered functions and fails when one changes. Then:

1. Decide whether the change alters what Poll or Settle decide, or which T3 Code behaviour golem relies on.
2. If it does, change the matching action in the model so it describes the new code, then run `formal/check.sh`.
3. If `current.cfg` now fails, treat the counterexample as a bug in the code first. Change the model to make it pass only if the model was wrong about the code or about T3 Code, and say which in the PR.
4. Update the hashes in `tests/test_formal_model.py` only after the model describes the new code, and move the sync commit below to the commit you checked.

## Scope

- Bound: 5 runs per thread, including golem's sent run. One subagent.
- Not modelled: request-pending and session-ended exits, `is_sent_run_cancelled_before_start` (golem's own send cancelled before it starts), run failures and usage limits, token reissue, and the timestamp comparison in `has_unfinished_subagent_work` (the model queues the wake run in the same step the subagent ends).
- Assumes T3 Code runs one run at a time and starts queued runs in order.

## Coverage

| Model part | Code |
| --- | --- |
| `Poll` | `backends/t3.py`: `wait_for_quiet_thread`, `has_active_run`, `has_unfinished_subagent_work` |
| `Settle` | `t3_code.py`: `settle_idle_thread`, `is_thread_settleable` |
| `Latest(rule)` | `t3_code.py`: `latest_run`, `latest_run_not_cancelled_before_start`, `is_cancelled_before_start` |

Sync commit: c79bf0a (Fix/Settle On Latest Active Run). The model was last confirmed against the covered code at this commit.
