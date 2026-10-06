# Durable ordinary closure evidence

`ordinary_closure_evidence.py` retains the closure assessment produced by an
ordinary validation attempt and applies it to the owning review cycle without
another model call. The producer's output is a proposal; authority comes only
from the ordinary attempt registry, the `pr_review_cycle` owner, and an
authoritative current-target observation (GitHub head/base, linked Issue
Requirements, controller strong policy) supplied as an observer callback.
Audit history, logs, resolved-thread state, and in-memory objects are never
authority.

## Retention

`OrdinaryClosureEvidence.retain()` journals the complete ordinary outcome
(the full validator result as recoverable JSON, plus derived blockers) with the assessment (repository, PR,
open epoch, attempt ID/sequence, H2/B/M/P, accepted strong round and audited H0,
finding-set revision, dispositions, scope and evidence, reviewer provenance) in
`ordinary_closure_evidence.json`, keyed by an immutable source identity. The
assessment must name a registered attempt for the same PR and head and the same
attempt as the ordinary result. A result with no assessment is `MISSING`
(including legacy bare ordinary PASS records); a failed write or corrupt store
is `UNAVAILABLE`; neither authorizes anything. A retained source is never
overwritten.

## Application

`apply()` holds the attempt transition fence and rejects (`REJECTED`, journaled)
a source when a newer ordinary attempt (even pending or failed), a newer strong
claim or round, a changed finding set, an open-epoch change, or a changed
H/B/M/P exists. An unavailable observation or store defers without erasing the
retained source. Certification requires semantic PASS whose coverage verifies exactly the
observed Requirements snapshot (a subset, unknown, or ambiguous identity is
non-authorizing) and no ordinary blocker, exact FIXED/INVALID evidence for every
outstanding finding, and no new finding. `BOUNDED` scope certifies bounded
closure through `certify_closure`; `EXPANDED`/`UNKNOWN` scope records
convergence that requires a renewed strong audit. Incomplete or blocked evidence
is `NON_AUTHORIZING` and stays retained.

A retained `INCONCLUSIVE` assessment with `EXPANDED`/`UNKNOWN` scope may
record nonbounded convergence when the ordinary result is a complete semantic
PASS, every outstanding finding has evidence-backed FIXED/INVALID disposition,
and there is no diagnostic or new finding. The original assessment verdict is
preserved. This closes the finding set and requires a renewed Strong audit;
it grants neither bounded closure nor merge or dependent publication authority.
BOUNDED INCONCLUSIVE and any incomplete, blocked, stale or superseded evidence
remain non-authorizing. Existing retained evidence resumes through this same
application path without rewriting its verdict or invoking another model.
Reentry also reuses the producing ordinary PASS from nonbounded convergence
while its attempt remains newest and head/base/Requirements/policy still match.
It does not require an accepted bounded closure to find that source; the renewed
Strong audit remains mandatory and cannot be replaced by another ordinary review.

## Recovery and idempotence

`certify_closure` records the `source_identity` and returns the existing
snapshot when that source was already committed, so replay after any
interruption (after retention, after ordinary convergence, after certification,
or when a local acknowledgement is lost) only finishes the outstanding local
transition and never creates a second closure. `reconcile()` resumes all
retained sources for a PR. A committed closure is not reopened by a later
attempt, but `dependent_effects_allowed()` refuses subsequent effects once a
newer ordinary attempt is still running or ended without a clean PASS (a newer
clean PASS leaves the closed findings closed), or the target changed, a newer
strong claim/round exists, or the finding set changed. Production consults it
before sending or reconciling the closure publication and before reusing the
closure completion as merge authority.

## Boundaries

No GitHub mutation is performed and merge authority is never granted here: an
accepted closure exposes `pending_publication` and its producing provenance,
and merge stays blocked until the cycle's publication acknowledgement and other
gates complete.

## Production integration

`pr_processor` retains and applies the evidence returned by the single ordinary
validation of a repaired head, supplies the observer (strict head read plus
freshly resolved base, Requirements and policy), reconciles retained sources
before any same-head shortcut or reviewer admission, and publishes the closure
evidence with its real ordinary attempt and reviewer. See
`independent-strong-audit-and-ordinary-closure.md`.

The production-flow regression fixture publishes its initial strong finding
through the authenticated reviewer adapter and durable effect journal. Its
GitHub transport retains exact review roots and reports resolved threads as
resolved rather than removing them. This lets the single-review and restart
scenarios verify closure acknowledgement against actual publication receipts
and observed thread state, while still checking that independent merge gates
remain blocking.
