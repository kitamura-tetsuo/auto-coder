"""Durable audit adapter for Auto-Coder's own PR adversarial review (Issue #1985).

This module attaches non-authorizing audit recording to the production PR
adversarial-review scheduling/execution/result-consumption boundaries. It never
decides PR eligibility, merge safety, or review/publication policy: every
function here only observes and records what the real production code already
decided, and every write is best-effort (a failure here must never change PR
processing behavior).

One ``review_id`` identifies one logical adversarial-review evaluation for an
exact repository/PR/head. Actual backend invocations that occur while a review
context is bound are recorded automatically as ``ReviewInteractionRecord``s by
``BackendManager`` (see ``backend_manager.py``); this module only allocates the
review_id, binds/unbinds the shared review-invocation context, and records the
evaluation lifecycle, native report and external-effect observations.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional, Sequence

from loguru import logger

from ..review_audit import (
    EvaluationLifecycle,
    ExecutionMode,
    ReviewAuditRecord,
    ReviewEffectRecord,
)
from .context import bind_review_context
from .recorder import get_review_audit_store

REVIEW_KIND_PR_ADVERSARIAL = "pr_adversarial"

# Bound how much of the reviewer's raw response text is retained (REQ-008: no
# archived full prompts/arbitrary CLI streams; keep only a bounded preview plus
# explicit truncation/count metadata per REQ-003).
_RAW_RESPONSE_PREVIEW_LIMIT = 4000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _monotonic_sequence() -> int:
    """A best-effort ordering key; ties are harmless (review_id stays unique)."""
    return time.time_ns()


@dataclass(frozen=True)
class PrAdversarialReviewTarget:
    """The exact repository/PR/head identity a review evaluation is attributed to."""

    repository: str
    pr_number: int
    head_sha: str


def compute_policy_identity(*, max_adversarial_reviews: Optional[int], thread_gate_enabled: bool) -> str:
    """Return a stable identity for the validation policy/cache configuration in effect.

    Used only to correlate a REUSED consumption with its producing EXECUTED
    review (REQ-002, REQ-011); it never changes reuse eligibility itself.
    """
    return json.dumps(
        {"max_adversarial_reviews": max_adversarial_reviews, "thread_gate_enabled": bool(thread_gate_enabled)},
        sort_keys=True,
    )


def linked_issue_membership(issue_numbers: Sequence[int]) -> Optional[str]:
    """Return a stable, comma-separated Issue-oracle reference list, or None if unknown."""
    if not issue_numbers:
        return None
    return ",".join(str(number) for number in issue_numbers)


def _asdict_list(items: Sequence[Any]) -> list:
    result: list = []
    for item in items:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            result.append(dataclasses.asdict(item))
        else:
            result.append(item)
    return result


def normalize_report_for_audit(result: Any) -> Dict[str, Any]:
    """Build a redacted-safe normalized report from an ``AdversarialValidationResult``.

    Retains the owner's native verdict, summary, findings, requirement
    coverage, specification/test-oracle gaps and thread dispositions (REQ-003).
    Deliberately excludes ``reviewer_session_checkpoint``/``reviewer_session_registry``
    (native attempt/session state, not review-report content) and the full raw
    backend response (REQ-008); a bounded preview plus explicit length/truncation
    metadata is retained instead.
    """
    raw_response = str(getattr(result, "raw_response", "") or "")
    truncated = len(raw_response) > _RAW_RESPONSE_PREVIEW_LIMIT
    return {
        "result": getattr(result, "result", None),
        "summary": getattr(result, "summary", None),
        "findings": _asdict_list(getattr(result, "findings", [])),
        "requirement_coverage": _asdict_list(getattr(result, "requirement_coverage", [])),
        "specification_gaps": _asdict_list(getattr(result, "specification_gaps", [])),
        "test_oracle_gaps": _asdict_list(getattr(result, "test_oracle_gaps", [])),
        "thread_dispositions": _asdict_list(getattr(result, "thread_dispositions", [])),
        "evidence_recovery": _asdict_list(getattr(result, "evidence_recovery", [])),
        "decision_critical_evidence_gaps": _asdict_list(getattr(result, "decision_critical_evidence_gaps", [])),
        "unexplained_changes": _asdict_list(getattr(result, "unexplained_changes", [])),
        "dynamic_check_requested": getattr(result, "dynamic_check_requested", None),
        "diagnostic_category": getattr(result, "diagnostic_category", None),
        "diagnostic_reason": getattr(result, "diagnostic_reason", None),
        "attempt_id": getattr(result, "attempt_id", None),
        "attempt_sequence": getattr(result, "attempt_sequence", None),
        "raw_response_preview": raw_response[:_RAW_RESPONSE_PREVIEW_LIMIT],
        "raw_response_length": len(raw_response),
        "raw_response_truncated": truncated,
    }


def _record_evaluation_best_effort(record: ReviewAuditRecord) -> None:
    try:
        get_review_audit_store().record_evaluation(record)
    except Exception:
        logger.opt(exception=True).warning(f"PR adversarial review audit: failed to record evaluation {record.review_id}")


def _update_evaluation_best_effort(
    *,
    review_id: str,
    repository: str,
    lifecycle: EvaluationLifecycle,
    execution_mode: ExecutionMode,
    native_verdict: Optional[str] = None,
    native_report: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        get_review_audit_store().update_evaluation(
            review_id=review_id,
            repository=repository,
            lifecycle=lifecycle,
            execution_mode=execution_mode,
            native_verdict=native_verdict,
            native_report=native_report,
        )
    except Exception:
        logger.opt(exception=True).warning(f"PR adversarial review audit: failed to update evaluation {review_id}")


def record_bypassed(target: PrAdversarialReviewTarget, *, policy_identity: str, related_issue_membership: Optional[str] = None) -> str:
    """Record a reached BYPASSED observation: validation is explicitly disabled.

    No native verdict is produced and no reviewer-backend invocation occurs.
    """
    review_id = _new_id()
    record = ReviewAuditRecord(
        review_id=review_id,
        repository=target.repository,
        target_type="pr",
        target_number=str(target.pr_number),
        review_kind=REVIEW_KIND_PR_ADVERSARIAL,
        origin="pr_processor._handle_pr_merge:disabled",
        process_identity=str(os.getpid()),
        creation_time=_now_iso(),
        creation_sequence=_monotonic_sequence(),
        reviewed_generation=target.head_sha,
        policy_identity=policy_identity,
        related_issue_membership=related_issue_membership,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.BYPASSED,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    _record_evaluation_best_effort(record)
    return review_id


def find_reusable_source_review_id(target: PrAdversarialReviewTarget, *, policy_identity: str) -> Optional[str]:
    """Locate the producing review_id for an authoritative same-head result, if known.

    Returns None when no matching instrumented producer is found (including the
    legacy case: an authoritative result created before this audit adapter
    existed). This never triggers a new backend invocation or alters the
    reused result itself (REQ-005).
    """
    try:
        result = get_review_audit_store().get_related_evaluations(
            target.repository,
            "pr",
            str(target.pr_number),
            review_kind=REVIEW_KIND_PR_ADVERSARIAL,
        )
    except Exception:
        logger.opt(exception=True).warning("PR adversarial review audit: failed to look up reusable source review")
        return None

    candidates = [record for record in result.records if record.reviewed_generation == target.head_sha and record.execution_mode == ExecutionMode.EXECUTED and record.lifecycle == EvaluationLifecycle.FINISHED and record.policy_identity == policy_identity]
    if not candidates:
        return None
    latest = max(candidates, key=lambda record: record.creation_sequence)
    return latest.review_id


def record_reused(
    target: PrAdversarialReviewTarget,
    *,
    policy_identity: str,
    source_review_id: Optional[str],
    native_verdict: Optional[str] = None,
    related_issue_membership: Optional[str] = None,
) -> str:
    """Record a REUSED observation: an existing authoritative result was consumed
    without a new reviewer-backend invocation. ``source_review_id`` is None when
    provenance is genuinely unavailable (legacy pre-instrumentation result)."""
    review_id = _new_id()
    record = ReviewAuditRecord(
        review_id=review_id,
        repository=target.repository,
        target_type="pr",
        target_number=str(target.pr_number),
        review_kind=REVIEW_KIND_PR_ADVERSARIAL,
        origin="pr_processor._handle_pr_merge:reused",
        process_identity=str(os.getpid()),
        creation_time=_now_iso(),
        creation_sequence=_monotonic_sequence(),
        reviewed_generation=target.head_sha,
        policy_identity=policy_identity,
        related_issue_membership=related_issue_membership,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=ExecutionMode.REUSED,
        native_verdict=native_verdict,
        native_report=None,
        source_review_id=source_review_id,
    )
    _record_evaluation_best_effort(record)
    return review_id


@contextmanager
def begin_executed_review(
    target: PrAdversarialReviewTarget,
    *,
    policy_identity: str,
    related_issue_membership: Optional[str] = None,
) -> Iterator[str]:
    """Allocate a review_id, record QUEUED then RUNNING, and bind the shared
    review-invocation context so every actual backend call made inside the
    ``with`` block is captured as this review's interaction (REQ-001, REQ-004).

    Callers must call :func:`finish_executed_review` after the block with the
    final (or absent, on an unrecovered exception) ``AdversarialValidationResult``.
    """
    review_id = _new_id()
    record = ReviewAuditRecord(
        review_id=review_id,
        repository=target.repository,
        target_type="pr",
        target_number=str(target.pr_number),
        review_kind=REVIEW_KIND_PR_ADVERSARIAL,
        origin="pr_processor._handle_pr_merge",
        process_identity=str(os.getpid()),
        creation_time=_now_iso(),
        creation_sequence=_monotonic_sequence(),
        reviewed_generation=target.head_sha,
        policy_identity=policy_identity,
        related_issue_membership=related_issue_membership,
        diagnostic_execution_references=None,
        lifecycle=EvaluationLifecycle.QUEUED,
        execution_mode=ExecutionMode.UNKNOWN,
        native_verdict=None,
        native_report=None,
        source_review_id=None,
    )
    _record_evaluation_best_effort(record)
    _update_evaluation_best_effort(
        review_id=review_id,
        repository=target.repository,
        lifecycle=EvaluationLifecycle.RUNNING,
        execution_mode=ExecutionMode.UNKNOWN,
    )
    with bind_review_context(
        review_id=review_id,
        repository=target.repository,
        target_type="pr",
        target_number=str(target.pr_number),
        review_kind=REVIEW_KIND_PR_ADVERSARIAL,
        generation_identity=target.head_sha,
    ):
        yield review_id


def finish_executed_review(
    review_id: str,
    target: PrAdversarialReviewTarget,
    result: Optional[Any],
) -> ExecutionMode:
    """Finalize a review begun with :func:`begin_executed_review`.

    Classifies EXECUTED vs. LOCAL_ONLY from whether any reviewer-backend
    interaction was actually recorded for this review_id (REQ-005) rather than
    from inspecting ``result`` content, so this stays correct regardless of
    which local pre-invocation refusal produced the ``result``.
    """
    invoked = False
    try:
        read = get_review_audit_store().get_evaluation(target.repository, review_id)
        invoked = bool(read.record and read.record.interactions)
    except Exception:
        logger.opt(exception=True).warning(f"PR adversarial review audit: failed to inspect interactions for {review_id}")

    execution_mode = ExecutionMode.EXECUTED if invoked else ExecutionMode.LOCAL_ONLY
    if result is not None:
        native_verdict = str(getattr(result, "result", None) or "ERROR")
        native_report = normalize_report_for_audit(result)
    else:
        native_verdict = "ERROR"
        native_report = {"diagnostic_category": "unrecovered_exception", "diagnostic_reason": "adversarial validation raised before producing a result"}

    _update_evaluation_best_effort(
        review_id=review_id,
        repository=target.repository,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=execution_mode,
        native_verdict=native_verdict,
        native_report=native_report,
    )
    return execution_mode


def record_effect(target: PrAdversarialReviewTarget, review_id: Optional[str], disposition: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Append one external-effect observation for ``review_id``.

    Effects (publication, reconciliation, supersession) are recorded
    separately from the retained review report and never overwrite it
    (REQ-006). A missing/unknown ``review_id`` is a no-op: there is nothing to
    attach the effect to, and this must never fail PR processing.
    """
    if not review_id:
        return
    effect = ReviewEffectRecord(
        review_id=review_id,
        effect_id=_new_id(),
        observation_time=_now_iso(),
        disposition=disposition,
        details=details,
    )
    try:
        get_review_audit_store().record_effect(target.repository, effect)
    except Exception:
        logger.opt(exception=True).warning(f"PR adversarial review audit: failed to record effect for {review_id}")
