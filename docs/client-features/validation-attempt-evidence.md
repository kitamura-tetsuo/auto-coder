# Attempt-bound validation evidence

Ordinary PR adversarial validation (the path reached from
`pr_processor._handle_pr_merge`, including when it is admitted through the
adversarial-validation scheduler) records one bounded, redacted diagnostic
record per native attempt so a mismatch (for example twelve returned `VERIFIED`
entries rejected as `unknown_requirement_coverage_id`) can be diagnosed without
rerunning the review. The record extends the existing non-authorizing
`ReviewAuditStore`; it adds no archive, no retention policy and no public
projection, and it never gates, repairs, publishes or merges anything.

Code: `review_capture/validation_evidence.py` (capture), `review_audit.py`
(`validation_evidence` table, `record_validation_evidence`,
`get_validation_evidence`), hooks in `adversarial_validator.py`, allocation in
`review_capture/pr_adversarial_audit.py`.

## Identity

`begin_executed_review` receives the already allocated native `attempt_id` and
`attempt_sequence` and starts capture before any input is read. The record binds
repository, PR number, head/base SHA (base is `unavailable` when the PR payload
has none), scoped `review_id`, the controller `process_run_id`, and the
diagnostic `execution_id` only when an execution scope is bound. Missing values
are listed in `identity.unavailable`; nothing is guessed from timestamps, counts,
latest rows or provider sessions. A later independent attempt for the same
PR/head/session has its own attempt and review identity. The `build` section is
a copy of `observe_controller_artifact()` for the producing process (installed
version, embedded revision, origin, availability, reason).

## Phases and responses

* **input** (captured by `build_adversarial_validation_context` before
  reviewer submission; the cache-bypassing reconfirmation call does not
  overwrite it): SHA-256 and UTF-8 byte length of the PR body, of each resolved
  Issue body (ordered, with repository/number, `source_updated_at` and
  `retrieval_mode` only when the retrieval supplied them, otherwise `null`) and of
  the linked-Issue-context representation; `rendered_pr_body` only when the clipped
  prompt form differs. `captured_at` is not freshness. No full text is stored and
  nothing is refetched.
* **manifests**: `supplied` (what the prompt contained) and `checked` (what the
  deterministic check used), each with `mode`, exact ordered IDs, per-entry
  requirement-text SHA-256, `count`, and `identity_sha256` over the versioned
  (`requirement-manifest-v1`) JSON `{"version", "entries":[[id,text],...]}`.
  Spelling/qualification is preserved; no alias repair.
* **responses** (`r1`, `r2`, ...): one per semantic response consumed by the
  parser, with `stage` (`initial`, `target_correction`,
  `target_selection_correction`, `dynamic_check_followup`,
  `evidence_completion`), the submitted prompt fingerprint recorded *before*
  invocation, `manifest_transmitted` and either `supplied_manifest_id` or
  `continues_manifest_id` (or `continues_unavailable_reason`), the review-scoped
  interaction IDs/backend/provider/requested model/reported model verified from
  the existing interaction records (otherwise `unavailable_reason`), the exact
  response fingerprint and state (`pending|nonempty|empty|unavailable`), the
  `semantic_payload` fingerprint only when CLI-envelope normalization changed it,
  and `parse` (state, failure category, parsed verdict, returned
  `id/status` entries exactly as parsed, including duplicates and unknown IDs). A
  missing or failed parse has `returned_entries_observed: false`, never an empty
  successful coverage set.
* **coverage_checks**: each deterministic interpretation bound to one
  `response_id`, with expected/returned/missing/duplicate/unknown IDs and counts
  (returned evidence, never acceptance), `verdict_before` (parsed) and
  `verdict_after` (controller), and the diagnostic category/reason. A branch that
  does not check records `performed: false` with `not_performed_reason`.
* **final**: the result returned to the caller, its `kind`
  (`semantic_response|local_without_semantic_response|exception`) and the
  `source_response_id` it came from. A later local override never borrows an
  earlier response's coverage or model identity.

`completeness` is `partial` (with `unrecorded` listing absent sections) until the
final result is recorded, so an interrupted writer reads as explicitly partial,
not running, terminated or successful.

## Reuse, lookup and states

`get_validation_evidence(repository, pr_number, attempt_id=... | attempt_sequence=...)`
is an exact, indexed, read-only lookup. It returns the original `producer` row,
separate `reuse_observations` (a REUSED row only references the producer via
`reuse.source_review_id` when exactly known and never copies its build or input
evidence) and the `effects` attributed to each row's own `review_id`. Status:
`AVAILABLE`, `NOT_FOUND`, `PRE_FEATURE` (no extension table, or a legacy
evaluation retained the attempt without it), `UNSUPPORTED_SCHEMA`, `CORRUPT`,
`UNINITIALIZED`, `UNAVAILABLE`. Storage, repository isolation and restart
readability are those of the existing audit root
(`AUTO_CODER_REVIEW_AUDIT_ROOT`, default `~/.auto-coder/review_audit`).

## Limits and redaction

Each record is at most 256 KiB serialized UTF-8, 500 entries per collection and
2,000 characters per free-text value (short categorical labels keep 200
characters even at the smallest budget). Counts and digests are computed before
clipping; `limits.omissions` lists the per-section source/retained/omitted
counts, `limits.clipped_fields` the clipped text, and `limits.incomplete_reason`
a record that could not fit even in minimal form. Configured credential values
(environment variables whose names look like tokens/secrets) and the audit's
token families are redacted *before* clipping; absolute/home/drive paths and URLs
in free text become `[LOCATION]`. An identity changed by redaction, longer than
the text cap, or path/URL-like is never rewritten: it is replaced by
`{"omitted": true, "reason", "sha256", "byte_length"}`. Full bodies, prompts,
diffs, transcripts, environment and provider URLs are never stored; the existing
bounded `raw_response_preview` is unchanged and separate.

## Non-interference

Hooks are no-ops without a bound recorder, never raise, perform no GitHub, LLM
or provider request, and never change a prompt, backend call/route, attempt
allocation, verdict, retry, publication, merge or exception. The only value
written onto a business object is the diagnostic `source_response_id` on
`AdversarialValidationResult`. A write failure is logged and ignored.

## Observability

No processing origin, admission gate, outcome, provider routing, durable
resumption path or structured trace event changed, and no UI projection was
added, so `docs/dashboard-observability.md` needs no update: this is audit-store
diagnostic data read only through `ReviewAuditStore`.
