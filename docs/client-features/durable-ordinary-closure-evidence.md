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
(verdict, requirement coverage, blockers) with the assessment (repository, PR,
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
retained source. Certification requires semantic PASS with fully verified
Requirements and no ordinary blocker, exact FIXED/INVALID evidence for every
outstanding finding, and no new finding. `BOUNDED` scope certifies bounded
closure through `certify_closure`; `EXPANDED`/`UNKNOWN` scope records
convergence that requires a renewed strong audit. Incomplete or blocked evidence
is `NON_AUTHORIZING` and stays retained.

## Recovery and idempotence

`certify_closure` records the `source_identity` and returns the existing
snapshot when that source was already committed, so replay after any
interruption (after retention, after ordinary convergence, after certification,
or when a local acknowledgement is lost) only finishes the outstanding local
transition and never creates a second closure. `reconcile()` resumes all
retained sources for a PR. A committed closure is not reopened by a later
attempt, but `dependent_effects_allowed()` refuses subsequent effects once a
newer attempt or changed target exists.

## Boundaries

No GitHub mutation is performed and merge authority is never granted here: an
accepted closure exposes `pending_publication` and its producing provenance,
and merge stays blocked until the cycle's publication acknowledgement and other
gates complete. Wiring the observer and retention call into PR processing
belongs to the integration stage.
