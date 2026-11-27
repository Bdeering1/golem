---------------------------- MODULE T3ThreadWait ----------------------------
(***************************************************************************)
(* How golem decides that a T3 Code thread has gone quiet, and whether it  *)
(* settles the thread later. Runs, subagents and the cancel-before-start   *)
(* behaviour come from T3 Code; Poll and Settle are golem's code:          *)
(*   Poll   = one pass of backends/t3.py wait_for_quiet_thread             *)
(*   Settle = t3_code.py settle_idle_thread / is_thread_settleable          *)
(* Each golem step reads one projection, so it is one atomic step here.    *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS
    MaxRuns,      \* runs a thread can have, including golem's sent run 1
    WaitLatest,   \* which run the wait loop treats as the thread's latest
    SettleLatest  \* which run the settle check treats as the thread's latest

HighestOrdinal == "highest_ordinal"                         \* t3_code.latest_run
SkipCancelledBeforeStart == "skip_cancelled_before_start"   \* t3_code.latest_run_not_cancelled_before_start

ASSUME WaitLatest \in {HighestOrdinal, SkipCancelledBeforeStart}
ASSUME SettleLatest \in {HighestOrdinal, SkipCancelledBeforeStart}

Runs == 1..MaxRuns
NoRun == 0
Active == {"queued", "running"}  \* backends/t3.py has_active_run: not in TERMINAL_RUN_STATUSES

VARIABLES
    status,          \* run -> "absent" | "queued" | "running" | "completed" | "cancelled"
    started,         \* run -> TRUE once the run has a startedAt
    startedLate,     \* run -> TRUE if it started after golem finished the task
    subagent,        \* "running" | "ended": a subagent from golem's sent run
    golem,           \* "waiting" | "finished"
    decidedRun,      \* the run whose result golem recorded
    quietAtDecision, \* no run was queued or running when golem finished
    settle,          \* "none" | "settled" | "skipped"
    skipJustified    \* a later run had started or was pending when golem skipped settling

vars == <<status, started, startedLate, subagent, golem, decidedRun,
          quietAtDecision, settle, skipJustified>>

Present == {r \in Runs : status[r] # "absent"}

CancelledBeforeStart(r) == status[r] = "cancelled" /\ ~started[r]

Candidates(rule) ==
    IF rule = HighestOrdinal THEN Present
    ELSE {r \in Present : ~CancelledBeforeStart(r)}

Latest(rule) ==
    IF Candidates(rule) = {} THEN NoRun
    ELSE CHOOSE r \in Candidates(rule) : \A s \in Candidates(rule) : s <= r

NextRun == IF Present = Runs THEN NoRun ELSE CHOOSE r \in Runs \ Present : \A s \in Runs \ Present : r <= s

\* golem's wait loop starts once `thread send --wait` returns, after run 1 ends.
Init ==
    /\ status = [r \in Runs |-> IF r = 1 THEN "completed" ELSE "absent"]
    /\ started = [r \in Runs |-> r = 1]
    /\ startedLate = [r \in Runs |-> FALSE]
    /\ subagent \in {"running", "ended"}
    /\ golem = "waiting"
    /\ decidedRun = NoRun
    /\ quietAtDecision = FALSE
    /\ settle = "none"
    /\ skipJustified = FALSE

\* T3 Code wakes the thread in a new run when a subagent finishes.
SubagentEnds ==
    /\ subagent = "running"
    /\ NextRun # NoRun
    /\ subagent' = "ended"
    /\ status' = [status EXCEPT ![NextRun] = "queued"]
    /\ UNCHANGED <<started, startedLate, golem, decidedRun, quietAtDecision, settle, skipJustified>>

\* Any other follow-up T3 Code queues, such as a wake-up after a background task.
Queue ==
    /\ NextRun # NoRun
    /\ status' = [status EXCEPT ![NextRun] = "queued"]
    /\ UNCHANGED <<started, startedLate, subagent, golem, decidedRun, quietAtDecision, settle, skipJustified>>

\* T3 Code runs one run at a time, in the order it queued them.
Start(r) ==
    /\ status[r] = "queued"
    /\ \A s \in 1..(r - 1) : status[s] # "queued"
    /\ \A s \in Runs : status[s] # "running"
    /\ status' = [status EXCEPT ![r] = "running"]
    /\ started' = [started EXCEPT ![r] = TRUE]
    /\ startedLate' = [startedLate EXCEPT ![r] = (golem = "finished")]
    /\ UNCHANGED <<subagent, golem, decidedRun, quietAtDecision, settle, skipJustified>>

Complete(r) ==
    /\ status[r] = "running"
    /\ status' = [status EXCEPT ![r] = "completed"]
    /\ UNCHANGED <<started, startedLate, subagent, golem, decidedRun, quietAtDecision, settle, skipJustified>>

\* T3 Code can cancel a queued run before it starts, for example while an earlier run is active.
CancelQueued(r) ==
    /\ status[r] = "queued"
    /\ status' = [status EXCEPT ![r] = "cancelled"]
    /\ UNCHANGED <<started, startedLate, subagent, golem, decidedRun, quietAtDecision, settle, skipJustified>>

\* wait_for_quiet_thread: finish once the latest run is inactive and no subagent is unfinished.
Poll ==
    LET run == Latest(WaitLatest) IN
    /\ golem = "waiting"
    /\ run # NoRun
    /\ status[run] \notin Active
    /\ subagent # "running"
    /\ golem' = "finished"
    /\ decidedRun' = run
    /\ quietAtDecision' = \A r \in Runs : status[r] \notin Active
    /\ UNCHANGED <<status, started, startedLate, subagent, settle, skipJustified>>

\* settle_idle_thread runs once, after a dependent task succeeds; a skip is never retried.
Settle ==
    LET run == Latest(SettleLatest) IN
    /\ golem = "finished"
    /\ settle = "none"
    /\ settle' = IF run = NoRun \/ (started[run] /\ ~startedLate[run]) THEN "settled" ELSE "skipped"
    /\ skipJustified' = \E r \in Runs : startedLate[r] \/ status[r] \in Active
    /\ UNCHANGED <<status, started, startedLate, subagent, golem, decidedRun, quietAtDecision>>

Next ==
    \/ SubagentEnds
    \/ Queue
    \/ \E r \in Runs : Start(r) \/ Complete(r) \/ CancelQueued(r)
    \/ Poll
    \/ Settle

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
\* golem records a task's result only when the thread has no queued or running run.
FinishesOnlyWhenQuiet == golem = "finished" => quietAtDecision

\* The recorded result comes from a run that actually ran.
ResultComesFromARunThatRan == golem = "finished" => started[decidedRun]

\* golem skips settling only when the thread really has a later run.
SkipsSettleOnlyForALaterRun == settle = "skipped" => skipJustified

TypeOK ==
    /\ status \in [Runs -> {"absent", "queued", "running", "completed", "cancelled"}]
    /\ started \in [Runs -> BOOLEAN]
    /\ startedLate \in [Runs -> BOOLEAN]
    /\ subagent \in {"running", "ended"}
    /\ golem \in {"waiting", "finished"}
    /\ decidedRun \in Runs \cup {NoRun}
    /\ settle \in {"none", "settled", "skipped"}
=============================================================================
