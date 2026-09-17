# PR adversarial-review durable audit

Auto-Coder's own PR adversarial review (the strong-model validator that
falsifies a PR against its linked Issue specification) records its own
scheduling, execution and result-consumption boundaries into the durable,
non-authorizing `ReviewAuditStore` (see `src/auto_coder/review_audit.py` and
`src/auto_coder/review_capture/`). This adapter never decides PR eligibility,
merge safety, or review/publication policy: it only observes what the real
production code already decided. Independent third-party GitHub review bots
and coding/repair conversations are outside scope; only Auto-Coder's own
`pr_adversarial` review is recorded.

## What is recorded

Every reachable adversarial-review boundary in `_handle_pr_merge`
(`src/auto_coder/pr_processor.py`) allocates or looks up one opaque
`review_id` for the current repository/PR/head and records a
`ReviewAuditRecord` with `review_kind="pr_adversarial"`:

- **BYPASSED**: adversarial validation is explicitly disabled
  (`pr_adversarial_validation`/`ENABLE_ADVERSARIAL_VALIDATION` is `false`).
  No native verdict and no reviewer-backend invocation.
- **REUSED**: an existing authoritative same-head result (a durable published
  comment/review) is consumed without a new reviewer-backend invocation. The
  producing review is looked up by exact head SHA and validation
  policy/cache identity (`compute_policy_identity`) among this repository's
  own recorded `EXECUTED`/`FINISHED` evaluations; when no such producer is
  found (including a result that predates this audit adapter), the
  `source_review_id` is explicitly `None` rather than guessed.
- **LOCAL_ONLY**: a local refusal/check (for example an invalid explicit
  Issue Requirements contract) blocks validation before any reviewer-backend
  invocation is attempted.
- **EXECUTED**: at least one reviewer-backend invocation was actually
  attempted for this review. Every actual backend call participating in one
  logical review (initial round, dynamic-check follow-up, session
  continuation, provider fallback) is bound to the same `review_id` via
  `review_capture.context.bind_review_context`; `BackendManager` then records
  each call as an ordered `ReviewInteractionRecord` automatically (see
  `backend_manager.py` and Issue #1983). Whether a review is EXECUTED or
  LOCAL_ONLY is derived from whether any interaction was actually recorded
  for that `review_id`, never from inspecting the validator's return value,
  so it stays correct regardless of which local pre-invocation refusal
  produced the result.

A new `review_id` is always allocated for a later independent review
attempt, even when the PR head or provider session is unchanged (for example
a forced re-review or a retried EXHAUSTED backend).

## Retained report and provenance

The final, owner-produced `AdversarialValidationResult` is persisted as a
redacted, normalized `native_report` (see
`review_capture.pr_adversarial_audit.normalize_report_for_audit`): the
native `result` (`PASS`, `NEEDS_FIX`, `NEEDS_TESTS`, `BLOCKED`,
`INCONCLUSIVE`, `ERROR`), summary, findings, requirement coverage,
specification/test-oracle gaps, thread dispositions, the native adversarial
attempt ID/sequence, and diagnostic category/reason when present. The full
raw backend response is never archived; only a bounded preview plus explicit
length/truncation metadata is retained (credential/token redaction from
`review_audit.redact_sensitive_data` still applies on top of this). A `PASS`
verdict accompanied by unresolved specification gaps or other blocking
conditions is retained as-is; the audit layer never synthesizes merge
eligibility from `result == "PASS"` alone.

The record also carries the reviewed head SHA (`reviewed_generation`), the
supplied validation policy/cache identity, and a best-effort linked
Issue-oracle reference (`related_issue_membership`, parsed from the PR body's
closing keywords); genuinely unavailable fields stay explicitly `None`
rather than guessed.

## External effects (publication/reconciliation)

Publication, reconciliation and supersession are recorded as separate
`ReviewEffectRecord`s (`confirmed`, `pending`, `failed`, `superseded`,
`unknown`), appended after the review's own report is already durable and
never overwriting it:

- A successful `publish_adversarial_review` call appends `confirmed`.
- An unsuccessful publish appends `pending` first (it may be a genuine
  failure or an accepted write whose response was lost), then `confirmed` if
  the existing reconciliation path (`_reconcile_failed_adversarial_publication`)
  confirms durability, or `failed`/`unknown` otherwise.
- A stale-head or superseded attempt (a newer attempt already applicable, the
  current head moved before durable acceptance, or the validation snapshot
  changed) appends `superseded` and leaves the original review's own result
  untouched as historical evidence for its own head.

## Non-interference guarantee

All audit recording is best-effort and wrapped so a failure (a full disk, a
locked database, or any other audit-only error) never changes the review
prompt, the normalized business result, native attempt/session/ownership
state, review-thread actions, publication/reconciliation requests,
retry/repair behavior, or merge decisions/effects. A missing or failed audit
record never implies a PR has never been reviewed, and it is never treated
as a merge gate or a reason to run another review.

## Reading the history

Recorded evaluations remain queryable through `ReviewAuditStore` after PR
closure/merge, reviewer-session cleanup, later heads, and controller restart,
using the same audit root (`AUTO_CODER_REVIEW_AUDIT_ROOT`, default
`~/.auto-coder/review_audit`). See `get_recent_history`,
`get_evaluation`, and `get_related_evaluations` in `review_audit.py`.
