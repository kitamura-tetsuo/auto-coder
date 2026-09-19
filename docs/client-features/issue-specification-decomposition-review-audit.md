# Issue specification/decomposition review durable audit

Auto-Coder's own Issue specification review (`SpecificationValidationLifecycle.decide()`)
and parent/child decomposition review (`DecompositionValidationLifecycle.decide()`)
record their own scheduling, execution and result-consumption boundaries into
the same durable, non-authorizing `ReviewAuditStore` the PR adversarial
adapter uses (see `src/auto_coder/review_audit.py`,
`docs/client-features/pr-adversarial-review-audit.md`, and
`src/auto_coder/review_capture/issue_review_audit.py`). This adapter never
decides Issue readiness, remediation, or publication: it only observes what
the real production lifecycles already decided.

## Single shared choke point

Unlike the PR adversarial adapter, `decide()` is a single opaque call that
may internally take a local-refusal, cache-hit, or fresh-analyzer branch with
no signal visible to its caller in advance. Rather than instrument the
lifecycles themselves, audit recording wraps
`AutomationEngine._traced_validation_job` — the one function every
standalone, forced/explicit, retained-owner-reevaluation, parent/child
scheduling, and pending-work-resumption review path already funnels through
(directly, or via `IssueReviewService`'s injected `trace_job`) — and
classifies the outcome strictly *after* `fn()` returns:

- **EXECUTED**: at least one `ReviewInteractionRecord` was captured for this
  review_id while it was the bound review context (a real backend call
  occurred; see `backend_manager.py` and Issue #1983).
- **LOCAL_ONLY**: no interaction was captured, and the returned decision's
  `evaluation_source == "local-only"` (a local Objective-integrity or
  structural-error refusal that never consulted the durable decision cache).
- **REUSED**: no interaction was captured, and `evaluation_source` is
  anything else (a pure durable-cache hit: `decide()`'s
  `self.store.get(identity)` branch).
- **BYPASSED**: recorded separately, at the genuine "would have reviewed but
  the flag says no" scheduling decision points
  (`AutomationEngine._schedule_parent_validations`'s decomposition- and
  specification-disabled branches) — no LLM job or authorization decision is
  ever created for a BYPASSED generation.

Because the `ValidationScheduler` coalesces concurrent submissions for the
same identity onto one shared future, only the one thread that actually runs
`fn()` reaches this wrapper; other callers observe the same shared result and
never create a second review record for one logical job.

## Retained report and provenance

The returned `ValidationDecision`/`DecompositionDecision` is persisted as a
redacted, normalized `native_report` (see
`review_capture.issue_review_audit.normalize_decision_for_audit`): the native
verdict (`READY`, `BLOCKED`, `ERROR`), findings, remediation/remediation
reason, `evaluation_source`, execution provenance, and the full producing
identity — for an individual review this is
`repository`/`issue_number`/`specification_digest`/`policy_identity`/`relationship_digest`;
for a decomposition review this is the full `parent`/`children`
`SetMemberIdentity` tuple (a closed child is included exactly when the
analyzer was actually given it) plus `policy_identity`. Neither analysis
result carries a raw-backend-response field, so there is nothing to
bound/truncate beyond the structured, already-bounded dataclass fields
(credential/token redaction from `review_audit.redact_sensitive_data` still
applies). A `READY` verdict is retained exactly like `BLOCKED`/`ERROR`, never
recorded only on failure.

## Reuse provenance

A REUSED observation looks up the most recent same-generation `EXECUTED`/
`FINISHED` record (`review_capture.issue_review_audit.find_reusable_source_review_id`,
matched on the producing `ValidationIdentity`/`DecompositionIdentity`'s
stable `.key` digest, which already cryptographically includes
`policy_identity`) and records it both in the dedicated `source_review_id`
column and as `native_report["reuse_source_review_id"]`.
When no matching producer is found — including a decision computed or cached
before this audit adapter existed — the association is left absent rather
than fabricated. Exact producer lookup is a newest-first direct query and
is not bounded by the general 500-row history display window.

## External effects (publication)

A best-effort `ReviewEffectRecord` (`unknown`/`failed`) is appended after a
BLOCKED decision's publication attempt in `IssueReviewService._apply_individual_blocked`/
`_apply_decomposition_blocked`, attached to the review that produced the
decision (looked up the same way as reuse provenance). This never changes
what `apply_blocked`/`apply_inherited_blocked` return or how their callers
behave. Because the legacy publication API cannot distinguish a successful
no-error return from a stale-generation refusal, it is never promoted to a
false `confirmed` observation. A missing/unknown producer gets an explicit
source-unavailable reuse observation rather than being silently dropped.

Queued, started, and terminal UTC observations are retained in each native
report. Producing identity and decomposition membership are bound on the
initial row before backend invocation. If authoritative decision persistence
raises after parsing, the native verdict/report remains audit evidence and
the failed authorization disposition is recorded separately; the original
business exception is still re-raised unchanged.

## Non-interference guarantee

All audit recording is best-effort and wrapped so a failure (a full disk, a
locked database, or any other audit-only error) never changes the returned
decision, prevents `fn()` from running, or alters Issue review/scheduling
behavior. A missing or failed audit record never implies an Issue has never
been reviewed and is never treated as a review gate.

## Reading the history

Recorded evaluations remain queryable through `ReviewAuditStore` after
controller restart, using the same audit root
(`AUTO_CODER_REVIEW_AUDIT_ROOT`, default `~/.auto-coder/review_audit`). See
`get_recent_history`, `get_evaluation`, and `get_related_evaluations` in
`review_audit.py`; issue reviews use `target_type="issue"` and
`review_kind` `"issue_specification"` or `"issue_decomposition"`.
