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
  result whose status is the decision status, with current actionable accepted
  obligations folded in under their existing identities (the TOG alias for a regression gap, the
  Strong source alias for an implementation finding), so publication references
  the existing canonical blocker/root instead of creating a duplicate. The raw
  model verdict is kept as a labeled historical diagnostic. Independently
  substantiated ordinary findings and gaps remain actionable even when a
  historical accepted obligation still needs adjudication or closure.
* `settle_accepted_gaps` separates the ordinary session's own verdict from
  obligations the lifecycle owns: a raw NEEDS_TESTS that exists only because the
  validator kept an accepted gap open is judged through the authoritative
  projection (open, closed, awaiting closure), never inherited.
* The decision is durably retained (`EffectiveDecisionStore`,
  `~/.auto-coder/<repo>/effective_review_decisions.json`) with its binding,
  blocker identities, evidence revision, normalized ordinary evidence, publication state, corrective-handoff
  state and spent reconciliation/closure attempts **before** any dependent
  effect. A failed write returns an operational failure and nothing is
  published or dispatched. Interruptions after retention, after an accepted but
  unrecorded review, or after a confirmed review before handoff are resumed by
  ordinary processing: a lost-response publication is reconciled against the
  durable review rather than replayed, and an undispatched correction is sent
  once the review is confirmed.
* The typed `ordinary_correction` payload is retained even when the optional
  closure assessment is missing or malformed. Its versioned binding includes
  repository/PR, head/base, contract/policy, open epoch, producing attempt and
  sequence, accepted Strong round/audited head, finding-set revision and accepted
  source identities. Controller-issued canonical reference context is retained;
  process-local projections and session repositories are reacquired, not serialized
  as authority. Valid per-finding closure proposals may be retained with the
  ordinary result without becoming accepted lifecycle closure.
  When the optional Strong tier is configured, its current contract and policy
  must remain observable. A result captured without that tier instead uses its
  accepted-source binding and a cache-bypassing check of the ordinary invocation's
  Requirements and change-input digests; enabling the tier cannot make a
  mismatched result applicable. The invocation binding survives an empty
  canonical-reference context. Readable changed inputs or missing legacy
  bindings get one persisted recovery admission; unavailable inputs wait.
  These replay-specific input checks apply to unfinished ordinary evidence. A
  completed PASS with no pending action continues through saved-verdict clearance
  checks and remains eligible for an explicitly forced fresh review.

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

The retained decision records `pending_closure_source_identities` and
`pending_reconciliation_source_identities` separately from sources requiring
correction. A current repair may proceed while other accepted findings await
lifecycle closure or historical adjudication. Fresh publication and saved-report
replay exclude those pending roots and gaps from the corrective bundle;
the durable split allows same-head replay without requesting another review or
resending previously delivered feedback. This never accepts their closure.
Local and cloud feedback selection accept the exact finding body with the reviewer-added
trailing canonical `Blocker identity` annotation; that annotation does not make
newly published findings disappear from the corrective handoff. Other body
changes use the retained observation-to-owner mapping and authenticated native
root aliases. An explicit canonical subset cannot expand through shared file
names, prose similarity or unrelated report dispositions. The ordinary canonical
IDs are retained before dispatch, and identical producing evidence preserves
confirmed publication and handoff state across reconstruction.
Every selected correction must have an observable actionable root before either
route accepts the bundle. A missing or resolved native root cannot turn delivery
of the other corrections into a confirmed handoff for the whole retained set.

## Reconciliation

A saved BLOCKED, NEEDS_FIX or NEEDS_TESTS decision may have reusable ordinary
corrective evidence, including when the previous invocation stopped before
publication. Before reviewer admission, including `--force`, recover that result
from the typed effective-decision payload or a matching authoritative legacy
closure record. Head, base, contract, policy, open epoch, accepted Strong round,
finding revision and accepted sources must still match. A newer registered
attempt, including a pending or failed one, supersedes automatic replay. An
explicit force request may admit a fresh review under changed authority; it does
not make stale evidence applicable. A readable change to the target or accepted
authority can also admit one automatic recovery under its new semantic key.

Re-read accepted findings and native roots, rederive the actionable subset, and
hold the ordinary-attempt and accepted-lifecycle transition fences through the
dependent publication and handoff. Reconcile existing publication receipts
before sending any missing review; unresolved transmission uncertainty waits
without a blind resend. Confirmed publication and provider delivery are reused
without another model call. Typed evidence that fails applicability cannot fall
through to legacy headline replay. Recovery grants neither closure nor approval;
freshly observed corrective completion can still admit its independent closure
review without an old STILL_VALID observation masking the completion.

Cloud replay can use an explicitly identified STILL_VALID native thread in
the retained report when its wording differs from the newly rendered finding.
It cannot select other addressed threads merely because they remain unresolved.

A reconciliation-required decision keeps the original known obligations, issues
no speculative repair or REQUEST_CHANGES and publishes at most a non-approving
COMMENT. Each distinct semantic evidence revision gets one focused attempt.
The fingerprint includes target/contract/policy, open epoch, finding set,
availability and relevant accepted-source identity, state and root associations.
Global ledger/source CAS counters, repeated observations, harmless prose and
unrelated publication bookkeeping do not renew it. Those counters remain
available for atomic writes and applicability checks.
Repairing an ambiguous association permits a fresh accepted-finding assessment;
an old reconciliation result without independent ordinary corrections does not
substitute for that assessment. Completed corrective generations remain accounted
for when their sources move into pending adjudication or closure, so the Strong
handoff path cannot overwrite a retained closure assessment during recovery.
Unchanged evidence waits explicitly without model or repair work; new exact
association/authority evidence, a changed head or an explicit `--force` makes
processing eligible again.

A legacy BLOCKED record with no reusable ordinary payload receives one
`ordinary-correction-v1` recovery admission for the observed target and semantic
evidence. Its spent key is durably recorded before reviewer invocation and
survives interruption, reconstruction and replacement of the saved envelope.
A changed base or contract produces a new key; a stale envelope or base-less
placeholder cannot prevent that one new admission. Recovery uses the existing
ordinary attempt mechanism and never starts a Strong audit solely to recover
the missing payload.

A closure-acceptance wait also spends its focused attempt for the observed
evidence revision. A missing or malformed closure assessment cannot repeatedly
admit ordinary validation at the same head and unchanged authority/association
state. New evidence, a changed head or explicit `--force` can admit recovery;
retained valid closure evidence continues to resume without a model call.

When independent local verification has already marked a canonical blocker
`VERIFIED_CORRECTION` while its accepted Strong finding remains OPEN, the
projection stays incomplete and cannot authorize approval. If every source and
root association is readable and unambiguous and this is the only disagreement,
the ordinary closure review and stale-authority audit renewal remain reachable.
The lifecycle owner must independently accept closure before approval. Other
disagreements, unavailable reads and ambiguous associations still wait. A
versioned recovery revision gives decisions retained by older consumers one
focused reconciliation attempt, preserving subsequent unchanged-evidence waits.
If a renewed exact-head Strong audit accepts new findings while historical
corrections await lifecycle closure, its repair handoff contains only that
round's findings and canonical sources. Readable ledger-ahead diagnostics for
older sources do not suppress this independently accepted current correction;
historical obligations remain open for later closure and never authorize merge.

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

For a root shared with a historical non-Strong owner, accepted-finding adjudication uses the Strong source aliases and retains the other owner unchanged. The existing reconciliation and verification trace events and `review_disposition` fields continue to report the derived decision; no event schema, processing origin or provider route changes. The existing decision record gains the typed payload and pending-adjudication identities described above.
