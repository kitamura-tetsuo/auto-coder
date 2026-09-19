# Two-Tier PR Review Cycle Persistence

`src/auto_coder/pr_review_cycle.py` (`PrReviewCycleRepository`) is the
durable, provider-independent state machine behind the two-tier PR review
workflow: ordinary adversarial review converges to PASS, an independent
strong audit either passes or raises findings, and — when it raises findings
— ordinary review verifies and closes them without necessarily requiring
another strong audit. This module owns persistence and the
transition/authorization boundary only. It never calls an LLM backend,
publishes a GitHub review, dispatches a repair, or merges a PR; those
responsibilities belong to a reviewer-execution adapter and to production PR
processing, which consume this model through its public transition API.

## Identity, not timestamps

A review round is identified by repository/PR plus:

- **H** — the exact audited head SHA.
- **B** — the reviewed base/provenance commit SHA.
- **M** (`ContractSnapshot`) — the resolved authoritative Issue identities and
  their complete Requirements text. Its `identity` is a stable hash of that
  content, so changing Requirement text invalidates prior authorization even
  at an unchanged head, while re-observing the same text never does.
- **P** (`StrongPolicyIdentity`, strong tier only) — the configured strong
  route, review-affecting model/options, and audit-protocol version.

Quota observations, timestamps, duplicate wakes, and provider conversation
identifiers never participate in these identities and never by themselves
create a new round. The actual reviewer backend/model used is recorded
separately as `reviewer_provenance` and never gates authorization.

## Lifecycle phases

`PrReviewCycleRepository.snapshot(pr_number)` returns a `PrReviewCycleSnapshot`
whose `phase` is one of `ORDINARY_REVIEW`, `STRONG_PENDING`,
`STRONG_RUNNING`, `ORDINARY_CLOSURE`, `COMPLETE`, or `CLOSED`, plus a
`waiting_reason`, `pending_effect`, and durable retry reason/deadline. An
accepted result awaiting publication is exposed as effect work, not as a new
model-execution request. A generic ordinary PASS, a claimed-but-unresolved
strong attempt, or a legacy single-tier review record (this module shares no
store with `adversarial_validation_attempts.py` or
`reviewer_session_registry.py`) never represents completed strong auditing.

## Two ways to reach COMPLETE

1. **Strong PASS**: `record_ordinary_pass` for H/B/M, then
   `claim_strong_audit`/`record_strong_result` with verdict `PASS` for the
   same H/B plus policy P, then `acknowledge_publication` for that round, then
   `accept_strong_pass_completion`. Completion is authorized only once
   publication is confirmed.
2. **Ordinary closure of strong findings**: after a `FINDINGS` verdict,
   `certify_closure` at a repair head H2 accepts closure evidence only when it
   references the still-current accepted round and finding-set revision,
   B/M/P are unchanged from that round, an applicable ordinary PASS exists for
   H2, every outstanding finding has an evidence-backed `FIXED`/`INVALID`
   disposition, and the cumulative diff from the original strong-audit head
   through H2 is certified `bounded=True` with reviewer-produced evidence. An
   omitted or wrong-head disposition is rejected without changing the finding
   set. The accepted closure remains non-authorizing until both the strong
   result and closure publication/bookkeeping are confirmed through
   `acknowledge_publication` and `acknowledge_closure_publication`. A
   `bounded=False` (EXPANDED) result requires a brand-new
   independent strong round on the current ordinary-passed H2 rather than
   another closure attempt.

Findings retain their complete Requirement identities and texts, producing
claim/round identity, counterexample, behavioral evidence, affected boundary,
and focused regression request. Findings discovered during closure
(`new_findings`) join the tracked obligations instead of being suppressed,
and a disposition never overwrites an already-dispositioned finding,
preserving the originating payload.

## Fencing overlapping controllers

Every transition is taken under a dedicated file lock
(`serialized_transition`) distinct from the short read/write lock, mirroring
`AdversarialValidationAttemptRepository`. `claim_strong_audit` is idempotent
for a retry by the owning controller, while another controller receives
`ClaimContendedError` and therefore never obtains the owner's execution
credential. A claim for a newer applicable identity supersedes an older
in-progress claim; `record_strong_result` rejects a result whose claim is no
longer the active one, so a stale result can never publish authority after a
newer attempt superseded it. Callers may also pass `expected_version` (from
`current_version`) to any mutating call for optimistic-concurrency fencing;
a mismatch raises `StaleTransitionError` before any write occurs.

## Restart and reopen

All state is written atomically (temp file + `os.replace`) to
`~/.auto-coder/<repo>/pr_review_cycle.json`. After a restart, `snapshot`
reconstructs the pending phase, complete contract and policy snapshots, the
accepted strong-audit bundle, accepted closure, outstanding findings,
retry-not-before/error metadata, and each result's `publication_status`
(`PENDING`/`ACKNOWLEDGED`) without requiring another model invocation; only
publication/acknowledgement work needs to resume. `set_finding_delivery_status`
tracks a finding's repair-message delivery (`NONE`/`PENDING`/`UNKNOWN`); when
a finding is later dispositioned through `certify_closure`, a pending or
unknown delivery is automatically retired rather than invented as delivered.

`mark_closed` blocks every transition and `is_completion_authorized` on a
closed PR; `mark_reopened` advances an internal open-epoch counter so a
completion accepted under a prior opening can never again authorize an
effect, while the historical record remains intact for audit purposes.
