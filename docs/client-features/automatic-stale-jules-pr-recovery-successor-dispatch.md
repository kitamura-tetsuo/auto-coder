# Automatic stale-Jules-PR recovery through retirement and successor dispatch

Issue #2286 closes the gap left after closing a stale Jules PR: previously,
`_close_stale_jules_pr` (`pr_processor.py`) closed the PR and posted a bare
`Auto-Coder Attempt: N` comment via `increment_attempt`, with no durable link
between that decision and the ownership/dispatch machinery. The Issue's
owned-start tombstone (`implementation_owned_starts`, unchanged since the
original Jules dispatch) and, once the predecessor's Jules PR/session were
fully retired, `evaluate_implementation_start` returning `ALREADY_OWNED`
(`implementation_ownership.py`) meant ordinary engine admission silently
skipped the announced retry forever. Parser and legacy-attribution fixes
(#2284, #2285) could pass while the retry itself never started.

## Recovery identity (R)

This closes the gap by reusing the same durable request/attempt machinery
already built for the operator-issued `--only --force --retry` path
(`issue_stage_routing.py`'s `implementation_retry_requests` table and
`implementation_ownership.acquire_explicit_retry`), instead of a parallel
controller or a global force flag.

`src/auto_coder/stale_jules_recovery.py` derives one deterministic recovery
identity `R` (an `ImplementationRetryRequest.request_id`) from
`(repository, issue_number, pr_number)` alone -- never from a timestamp,
comment, or PR head SHA. Reprocessing the same stale-PR closure decision (a
restart, a duplicate wake, or the same PR re-observed) always resolves to the
same durable record instead of minting another one.

`_close_stale_jules_pr` durably records `R` -- via
`IssueStageRoutingStore.accept_retry_request`, capturing the Issue's current
Implementation generation (`G`) -- strictly *before* closing the PR or
publishing any attempt effect. When `G` cannot be established for a linked
issue (no reconciled Implementation lane item and no already-started
production owner), or when a durable `R` already exists for this exact
(Issue, PR) pair bound to a *different* generation (the specification
changed underneath an unconsumed grant -- permanently disqualifying), the
issue falls back to the historical unlinked `increment_attempt` behavior
instead of receiving an automatic grant.

Once closed, the attempt-comment publication itself
(`publish_recovery_attempt_comment`) is a read-then-post projection of `R`:
it scans the full comment thread for the exact `stale-jules-recovery:<R>`
trigger marker before posting, so a restart or duplicate wake never posts a
second comment for the same recovery, and an unreadable comment thread
defers (never treated as "no prior attempts").

Engine context (`IssueStageRoutingStore`/`ImplementationSlotRepository`)
reaches `_close_stale_jules_pr` through a context variable
(`stale_jules_recovery_context`) set once by `process_pull_request`, rather
than threaded through every intermediate PR-processing helper; omitting it
(most existing tests, and any caller with no engine context) preserves
exactly the historical unlinked-increment behavior.

## Waiting for predecessor retirement, then crossing the tombstone

A pending `R` is durable authority to retry, not admitted execution. Ordinary
engine admission (`AutomationEngine._process_single_candidate_unified_impl`)
looks up the Issue's pending automatic recovery
(`AutomationEngine._pending_stale_jules_recovery`) on every ordinary
(non-explicit, non-manual-retry) admission attempt for that Issue:

* If its captured generation no longer matches the Issue's current
  Implementation generation, the grant is permanently invalidated
  (`IssueStageRoutingStore.invalidate_retry_request`) and processing falls
  through to ordinary admission with no special retry semantics at all.
* If the owner still retains qualifying implementation evidence
  (`ImplementationSlotRepository.has_qualifying_implementation_activity` --
  a retained provider session or implementation-PR membership, independent
  of whether a *local* execution is live), admission defers without
  attempting either the automatic-retry or the ordinary path. This is a
  deliberate, feature-specific wait: the general ownership adapter's own
  `CONTINUE` decision would otherwise let an ordinary same-generation start
  proceed alongside retained (but not yet retired) predecessor evidence, and
  `acquire_explicit_retry` itself only refuses on a still-*live* local
  execution, not retained PR/session membership. The predecessor is freed by
  the existing, independently scheduled reclamation pipeline
  (`implementation_reclamation_scheduler.py` /
  `implementation_retirement.py` / `implementation_retirement_observer.py`,
  Issues #2146/#2147/#2284); this module never evaluates or commits
  retirement itself, so a waiting `R` cannot itself block the retirement it
  needs.
* Once the owner is free, ordinary admission calls
  `implementation_ownership.acquire_explicit_retry` with `R` exactly as the
  manual `--retry` path does -- acquiring a fresh protected owner incarnation
  for the same generation despite its already-durable owned-start tombstone,
  and dispatching through the existing `manual_retry`/`retry_authority`
  machinery (`_dispatch_issue_candidates`, `issue_processor.py`) with no
  new dispatch code path.

## Non-goals

This does not change the CLI `--only --force --retry` contract (Issue
#2186), does not make every closed PR an automatic retry source, does not
introduce a new persisted schema (the recovery identity reuses the existing
`implementation_retry_requests` table; its deterministic id embeds the PR
number instead of adding a column), and does not itself evaluate or commit
predecessor retirement (that remains #2146/#2147's responsibility).

## Tests

* `tests/test_stale_jules_recovery.py` -- the module's own identity,
  capture/find, and comment-publication behavior against real
  `IssueStageRoutingStore`/`ImplementationSlotRepository` instances, plus the
  `acquire_explicit_retry` tombstone-crossing boundary this module hands `R`
  to.
* `tests/test_pr_processor_jules_timeout_close.py::TestCloseStaleJulesPrAutomaticRecovery`
  -- `_close_stale_jules_pr` capturing `R` before closing, idempotent
  reprocessing, the legacy fallback when `G` is unresolvable, and the
  generation-conflict fallback.
* `tests/test_implementation_ownership.py::test_automatic_stale_jules_recovery_defers_until_predecessor_retires`,
  `::test_automatic_stale_jules_recovery_acquires_once_predecessor_is_free`,
  and `::test_automatic_stale_jules_recovery_invalidated_on_generation_change`
  -- the real `AutomationEngine._process_single_candidate_unified` admission
  boundary for the wait/acquire/invalidate decisions above.
