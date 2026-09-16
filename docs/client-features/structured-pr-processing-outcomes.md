# Structured PR processing outcomes

The dependency-bot processing policy (`IGNORE_DEPENDABOT_PRS`,
`AUTO_MERGE_DEPENDABOT_PRS`) is enforced by one shared decision
(`evaluate_dependency_bot_admission` in `src/auto_coder/pr_processor.py`) at
the common PR-processing boundary in
`AutomationEngine._process_single_candidate_unified_impl`, immediately after
the PR author-allowlist check and before any implementation reservation,
execution, or admission-related PR membership is created. This applies to
every PR-processing origin -- startup-discovered and webhook-invalidated
PRs, synchronous `run()` candidates, explicit single-target processing, and
pending-work/merge-operation resumption -- not only the optional prefilter
retained in `AutomationEngine._get_candidates` for priority scheduling. When
`AUTO_MERGE_DEPENDABOT_PRS` is enabled, the common gate re-fetches
cache-bypassing PR metadata and same-HEAD CI evidence for the current
evaluation and reconfirms the PR's open/mergeable/HEAD facts again after
that CI read, rather than reusing whatever the collector or an earlier
evaluation already decided; a policy refusal is `SKIPPED` with a specific
reason, while unavailable/incomplete evidence is retryable `DEFERRED`, and
neither outcome disturbs an already-retained implementation owner.

Codex Cloud pull requests whose authoritative remote head is the shared
``work`` branch are rejected at the common merge/review boundary before CI,
adversarial validation, repair delivery, checkout, or merge work. PR metadata
is refreshed from GitHub without cache before every safety decision; a
failed or incomplete refresh fails the processing pass closed. The existing
task is asked to republish from a task-specific branch, retaining linked-Issue
ownership only when that request succeeds.

PR processing propagates a machine-readable `success`, `deferred`, or `failed`
outcome through the automation engine. Internal exceptions remain visible in the
human-readable action diagnostics and are also added to the top-level errors, so
single-item completion output cannot report a false success. Expected states such
as red CI, merge gates, and cloud-task waits remain deferred rather than failures.

Codex CLI provider transport failures are classified from structured diagnostic
events (with a conservative text fallback). Exhausted reconnects defer the
current target to normal scheduling without consuming an implementation attempt,
while preserving the provider status and endpoint diagnostic for operators.
