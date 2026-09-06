## Context

Outliner already has a project-level Schedule list at `/:project/-/schedules`, but it is currently a card-style list rather than a management/status surface comparable to Object Manager.

The current list shows a client-computed `Next run` using the RRULE and current wall clock, and shows `lastRunAt` / `lastRunStatus` from the Schedule rule. The scheduler itself maintains an authoritative recurrence cursor in `schedule_index.next_run_at`.

There is also an important lifecycle mismatch in the current implementation: `lastRunAt` is written after execution completes, so it is currently a completion-time observation rather than an execution-start timestamp. It must not simply be relabeled as `Last run` with new start-time semantics.

Add a compact Schedules Manager that makes operational state visible at a glance, including enable/disable, the most recent execution attempt, its result, the most recent successful completion, and the scheduler's actual next occurrence.

This should be visually similar in purpose to Object Manager, but Schedule-specific execution semantics remain independent from Object Manager semantics.

## Requirements

REQ-001: `/:project/-/schedules` must present Schedules as a compact management list/table that exposes, for every Schedule, at least: `Enabled`, `Name`, `Target`, `Cadence`, `Last run`, `Result`, `Last successful run`, `Next run`, and row actions. Existing Schedule detail navigation must remain available.

REQ-002: `Enabled` must be an interactive on/off control for users with the existing Schedule write permission. Changing it must update the persisted/shared Schedule `enabled` state used by the production scheduler. Read-only viewers may observe the value but must not be able to mutate it. A failed mutation must not leave the UI presenting the requested state as successfully committed.

REQ-003: `Last run` must mean the wall-clock time at which the most recent actual execution attempt began. When execution actually begins, `Last run` and `Result = Running` must become observable as one lifecycle transition. Queue time, scheduled occurrence time, RRULE time, and execution completion time must not be substituted for the execution-start timestamp. Both scheduled executions and `Run now` executions count as execution attempts.

REQ-004: `Result` must describe the execution identified by `Last run`. At minimum the manager must distinguish `Never run`, `Running`, `Success`, and a non-success terminal result. On successful completion, the corresponding execution becomes `Success`; on execution failure it becomes a non-success terminal result and the existing failure diagnostic must remain discoverable. An older execution result must never overwrite the result associated with a newer `Last run`.

REQ-005: `Last successful run` must mean the wall-clock completion time of the most recent execution that completed successfully. It must be updated only when an execution reaches successful completion. A later failed, interrupted, or still-running execution must not overwrite it. If no trustworthy successful-completion timestamp exists, the manager must show an explicit empty/never value rather than infer success from recurrence timing. Existing historical completion-style `lastRunAt` may only be used to seed this value where the stored result proves that execution succeeded; it must not be reinterpreted as a historical execution-start timestamp.

REQ-006: `Next run` must represent the production scheduler's authoritative next recurrence cursor/state, not a client-side `rrule.after(now)` approximation. When the scheduler has an active indexed occurrence, show that exact next occurrence. When the Schedule is disabled, exhausted/completed, invalid, or otherwise has no eligible next occurrence, do not show a fabricated future timestamp. If authoritative scheduler state is temporarily unavailable, show an unavailable/loading state rather than silently falling back to a locally computed value.

REQ-007: A manual `Run now` execution must update `Last run`, `Result`, and, when successful, `Last successful run`, but it must not advance or replace the recurring scheduler cursor merely because the manual execution occurred. `Next run` after `Run now` must continue to describe the next scheduled recurrence unless some separate Schedule edit changes the recurrence.

REQ-008: While the manager remains open, execution/status changes produced by the scheduler and enable/disable changes must become observable without requiring a navigation or full page reload. The displayed `Last run`, `Result`, `Last successful run`, and `Next run` values must not be assembled from mutually stale snapshots that describe different lifecycle generations.

REQ-009: A persisted `Running` state from an execution that cannot complete because the scheduler process was interrupted/restarted must not remain indefinitely presented as currently running. Recovery/reconciliation must transition such an execution to a terminal non-success state (for example `Interrupted`), without updating `Last successful run`.

REQ-010: Preserve the existing Schedule management capabilities from this route, including `Run now`, `Edit`, and `Delete`, subject to the existing authorization rules. Validation errors and last-run errors must remain visible or directly discoverable from the manager; the compact table must not hide operational failure information that is currently available.

## Acceptance Scenarios

### AS-001 — Never-run active Schedule
Covers REQ-001, REQ-003, REQ-004, REQ-005, REQ-006.

Given an enabled Schedule that has never executed and whose scheduler index has a next occurrence,
when the Schedules Manager opens,
then `Last run` shows `Never`/`—`,
and `Result` shows `Never run`,
and `Last successful run` shows `Never`/`—`,
and `Next run` shows the exact authoritative indexed occurrence.

### AS-002 — Successful scheduled execution
Covers REQ-003, REQ-004, REQ-005, REQ-006, REQ-008.

Given an enabled Schedule with an upcoming indexed occurrence,
when the scheduler begins that execution,
then `Last run` becomes the actual execution-start timestamp and `Result` becomes `Running` before completion.
When that execution succeeds,
then `Result` becomes `Success`,
and `Last successful run` becomes that execution's completion timestamp,
and `Next run` advances to the scheduler's next authoritative occurrence.

### AS-003 — Failure after an earlier success
Covers REQ-003, REQ-004, REQ-005.

Given a Schedule whose previous successful execution completed at `T1`,
when a later execution begins at `T2` and fails,
then `Last run = T2`,
and `Result` is a non-success terminal result,
and `Last successful run = T1`,
and the previous success timestamp is not overwritten by the failure.

### AS-004 — Manual run does not consume recurrence
Covers REQ-003, REQ-005, REQ-006, REQ-007.

Given an enabled recurring Schedule whose authoritative next occurrence is `Tnext`,
when the user invokes `Run now` before `Tnext`,
then the manual execution updates `Last run` and `Result`,
and a successful manual execution updates `Last successful run`,
but `Next run` remains `Tnext` unless the recurrence itself is separately changed.

### AS-005 — Disable and re-enable
Covers REQ-002, REQ-006, REQ-008.

Given an enabled Schedule with an indexed next occurrence,
when an authorized user turns it off,
then the shared `enabled` state becomes false and the manager no longer presents that occurrence as an eligible `Next run`.
When the Schedule is turned on again,
then the manager shows the scheduler's newly authoritative next occurrence after scheduler reconciliation rather than a locally guessed timestamp.

### AS-006 — Scheduler cursor differs from naive client calculation
Covers REQ-006.

Given a Schedule for which the scheduler cursor is overdue, catch-up-adjusted, DST-adjusted, or otherwise differs from `rrule.after(now)`,
when the manager renders `Next run`,
then it displays the scheduler cursor/state and not the naive client calculation.

### AS-007 — Legacy telemetry must not become a fake start time
Covers REQ-003, REQ-005.

Given a pre-feature Schedule that has the existing completion-style `lastRunAt` but no recorded execution-start timestamp,
when the manager is first opened after upgrade,
then that old completion timestamp is not shown as `Last run` start time.
If the stored result proves the old execution succeeded, it may be represented/migrated as `Last successful run`; otherwise no successful-completion time is invented.

### AS-008 — Restart during execution
Covers REQ-004, REQ-005, REQ-009.

Given an execution that has persisted `Running` state and its start timestamp,
when the scheduler process terminates before writing a terminal result and later restarts,
then the execution is reconciled to a terminal non-success state rather than remaining `Running` forever,
and `Last successful run` remains unchanged.

### AS-009 — Read-only manager
Covers REQ-002, REQ-010.

Given a viewer without Schedule write permission,
when they open the Schedules Manager,
then all status columns are readable,
but enable/disable, `Run now`, and destructive actions are unavailable according to the existing authorization model,
and detail navigation remains available.

### AS-010 — Newer execution cannot be overwritten by older telemetry
Covers REQ-004, REQ-008.

Given telemetry for execution A followed by execution B becoming the most recent execution,
when any delayed observation/update associated with A arrives after B has started,
then the manager must not regress `Last run` / `Result` back to execution A or combine A's result with B's start timestamp.

## Non-goals

- Full per-execution history/log browsing. This issue only requires summary telemetry for the most recent attempt and most recent successful completion.
- Retry policy redesign, catch-up policy redesign, or changing recurrence semantics.
- Making Schedule execution state part of Object Manager's generic object model.
- Redesigning the Schedule detail editor beyond navigation/integration needed by the manager.
- Adding sorting/filtering/reorderable columns unless they are already available cheaply through an existing shared management-table component.

## Implementation Notes

- The current `ScheduleRuleList.svelte` computes `Next run` locally with `rrulestr(...).after(now)`. That is not a valid source for REQ-006; use a production-scheduler status surface backed by the scheduler index/cursor or an equivalent authoritative representation.
- The current scheduler writes `lastRunAt` after `dispatchJob(...)` returns. Do not satisfy REQ-003 by merely relabeling that field. Record a real start-time observation. Introducing an explicit field such as `lastRunStartedAt` is preferable to silently changing the meaning of historical data.
- A separate durable field such as `lastSuccessfulRunAt` is likely appropriate for REQ-005. `completedAt` already represents recurrence exhaustion/completion and must not be reused as "last successful execution".
- If execution identity/generation metadata is needed to guarantee REQ-004 / REQ-008 against stale completion writes, keep it internal unless exposing it materially improves diagnostics.
- Reuse the existing management route and route helpers; this is a replacement/improvement of the current Schedules list, not a second competing Schedule-management page.

## Difficulty

This issue spans the scheduler execution lifecycle, authoritative scheduler-index state, persisted Yjs Schedule telemetry, permissions, and the project management UI. A superficially correct UI-only implementation could still show false `Next run` values or relabel completion timestamps as start timestamps, so mark this issue `difficult`.