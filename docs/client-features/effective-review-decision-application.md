# Effective review decision application

`src/auto_coder/effective_decision_application.py` and the shared
`_handle_pr_merge` boundary apply the derived effective ordinary-review decision
(see `effective-ordinary-review-decisions.md`) through publication, repair,
reconciliation and closure. Automatic processing, explicit single-PR processing
and `--force` all reach it, for local-origin and cloud-origin repairs alike.

## Decision first, headline second

* After an ordinary validation attempt is parsed, the accepted-finding
  projection (`AcceptedFindingBridge`) and the parsed result yield one effective
  decision. The result that is retained, published and used for repair is the
  decision, not the model verdict: `apply_effective_decision` returns a new
  result whose status is the decision status, with every accepted obligation
  folded in under its existing identity (the TOG alias for a regression gap, the
  Strong source alias for an implementation finding), so publication references
  the existing canonical blocker/root instead of creating a duplicate. The raw
  model verdict is kept only as a labeled historical diagnostic.
* `settle_accepted_gaps` separates the ordinary session's own verdict from
  obligations the lifecycle owns: a raw NEEDS_TESTS that exists only because the
  validator kept an accepted gap open is judged through the authoritative
  projection (open, closed, awaiting closure), never inherited.
* The decision is durably retained (`EffectiveDecisionStore`,
  `~/.auto-coder/<repo>/effective_review_decisions.json`) with its binding,
  blocker identities, evidence revision, publication state, corrective-handoff
  state and spent reconciliation/closure attempts **before** any dependent
  effect. A failed write returns an operational failure and nothing is
  published or dispatched. Interruptions after retention, after an accepted but
  unrecorded review, or after a confirmed review before handoff are resumed by
  ordinary processing: a lost-response publication is reconciled against the
  durable review rather than replayed, and an undispatched correction is sent
  once the review is confirmed.

## Saved headlines are not clearance

Before a saved same-head PASS is consumed the accepted state is re-acquired. A
PASS is clearance only while the state is readable and every accepted finding
is closed for the current target; otherwise the effective decision is re-derived
(no dummy commit, store reset or `--force` required). A saved NEEDS_FIX/NEEDS_TESTS
review resumes its outstanding correction through the originating route using
the exact associated root bodies and canonical blocker identities, and is
re-derived when the lifecycle has since closed every correction behind it.

## Pre-send approval authority

Immediately before a native APPROVE can be transmitted `GitHubAppReviewer.publish`
consults an `ApprovalAuthority`. It re-reads the accepted state and refuses when
a newer validation attempt is registered, the state is unreadable or incomplete,
the accepted finding set changed, or the current decision is not
approval-eligible. A refusal (`ReviewPublicationResult.policy_refusal`) means
nothing was transmitted: it is not an HTTP failure and never enters publication
failure recovery. The caller re-reads the authoritative threads and accepted
state and re-derives once while its attempt is still the applicable one, then
publishes the replacement decision (for example REQUEST_CHANGES with the corrective
handoff); a participant superseded by a later attempt stops, and the retained
work stays with the current attempt's consumer. APPROVE is never sent and then
retracted.

## Corrective handoff and waits

A published NEEDS_TESTS/NEEDS_FIX decision advances in the same invocation to the
originating route (existing resolver for local and cloud origins) with a bounded
bundle built from the original canonical blockers, qualified requirements and
correction scope. Test-only blockers request focused regression protection;
mixed blockers keep their categories. Existing request/admission identity and
blocker-owned allowance are reused, so repeated processing or changed aliases do
not start a second provider request. The outcome is retained as delivered or as a
distinct wait reason (`ROUTE_UNAVAILABLE`, `QUOTA_DEFERRED`,
`INDETERMINATE_DELIVERY`, `ALLOWANCE_EXHAUSTED`); a wait is never reported as
delivered or complete, resets no allowance and borrows no other route.

## Reconciliation

A reconciliation-required decision keeps the original known obligations, issues
no speculative repair or REQUEST_CHANGES and publishes at most a non-approving
COMMENT. Each distinct evidence revision (authority, association, source/finding
revisions; head-dependent adjudication is excluded) gets one focused attempt.
Unchanged evidence waits explicitly without model or repair work; new exact
association/authority evidence, a changed head or an explicit `--force` makes
processing eligible again.

## Closure without an effective PASS

Closure eligibility, requirement coverage and final approval are consumed
separately. When the ordinary session cleared coverage (`raw_ordinary_clear`) and
a corrective generation exists (a new head, a supported local no-change
completion, or changed provider activity), the applicable ordinary PASS is
recorded and the bounded ordinary-closure producer runs even though the effective
result is not PASS. Acceptance is committed by the owning review-cycle lifecycle
(fenced against newer attempts), its projection is re-read, and only then is the
decision re-derived and a PASS published. An independent closure review that
retains the finding is evidence the finding is still upheld and returns it to the
admitted-repair path for the same blocker. Thread resolution for an accepted
finding is gated on the lifecycle having accepted the closure. Strong
completion, CI and the final merge boundary are unchanged.

## Reporting

Repair-delegation, validation and merge stages carry a `review_disposition` fact:
`CORRECTIVE_HANDOFF`, `CORRECTION_WAITING`, `RECONCILIATION_WAIT`,
`OPERATIONAL_FAILURE`, `REVIEW_VERIFIED` or `MERGE_COMPLETED`. An enqueued repair,
a refused approval, a COMMENT or a raw PASS is none of the last two.
