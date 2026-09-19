"""Durable audit adapter for Auto-Coder's own Issue specification/decomposition
review (Issue #1984, Parent #1980).

Mirrors ``review_capture/pr_adversarial_audit.py`` (Issue #1985) for the two
review kinds owned by ``SpecificationValidationLifecycle.decide()`` and
``DecompositionValidationLifecycle.decide()``. This module never decides
Issue readiness, remediation, or publication: it only observes what the real
production lifecycles already decided, and every write is best-effort — a
failure here must never change Issue processing behavior (REQ-007).

Unlike the PR adversarial adapter, ``decide()`` is a single opaque call: it
may take a LOCAL_ONLY, REUSED, or EXECUTED branch internally with no signal
visible to the caller in advance (see ``specification_validation_lifecycle``
and ``decomposition_validation_lifecycle`` module docstrings for the exact
three-branch shape). ``run_traced_review`` is the single shared entry point
that lets ``AutomationEngine._traced_validation_job`` wrap either review kind
without knowing which branch will run, classifying EXECUTED vs. REUSED vs.
LOCAL_ONLY strictly *after* ``fn()`` returns, from evidence (REQ-003):

* EXECUTED: at least one ``ReviewInteractionRecord`` was captured for this
  review_id while it was bound as the active review context (a real backend
  call occurred; see ``backend_manager.py``).
* LOCAL_ONLY: no interaction was captured, and the returned decision's
  ``evaluation_source == "local-only"`` (a local Objective-integrity/
  structural-error refusal that never consulted the durable decision cache).
* REUSED: no interaction was captured, and ``evaluation_source`` is anything
  else (a pure durable-cache hit: ``decide()``'s ``self.store.get(identity)``
  branch).

One ``review_id`` identifies one logical review-job evaluation (REQ-001):
the ``ValidationScheduler`` coalesces concurrent submissions for the same
identity onto one shared future, and only the one thread that actually runs
``fn()`` reaches ``run_traced_review`` (see
``AutomationEngine._traced_validation_job``'s callers); other callers merely
observe the same shared result and never create a second review record.
"""

from __future__ import annotations

import dataclasses
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Sequence, TypeVar

from loguru import logger

from ..review_audit import (
    EvaluationLifecycle,
    ExecutionMode,
    ReviewAuditRecord,
    ReviewEffectRecord,
)
from .context import bind_review_context
from .recorder import get_review_audit_store

REVIEW_KIND_ISSUE_SPECIFICATION = "issue_specification"
REVIEW_KIND_ISSUE_DECOMPOSITION = "issue_decomposition"

# Maps the two ``_traced_validation_job`` stage identifiers (automation_engine.py,
# issue_review_service.py) to their review kind. An unrecognized stage_id runs
# unaudited rather than guess a kind (REQ-007: never gate/alter behavior).
_STAGE_REVIEW_KIND = {
    "issue.individual-validation-job": REVIEW_KIND_ISSUE_SPECIFICATION,
    "issue.decomposition-validation-job": REVIEW_KIND_ISSUE_DECOMPOSITION,
}

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _monotonic_sequence() -> int:
    """A best-effort ordering key; ties are harmless (review_id stays unique)."""
    return time.time_ns()


def _asdict_list(items: Sequence[Any]) -> list:
    result: list = []
    for item in items:
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            result.append(dataclasses.asdict(item))
        else:
            result.append(item)
    return result


def normalize_decision_for_audit(decision: Any) -> Dict[str, Any]:
    """Build a redacted-safe normalized report from a decision object.

    Accepts either a ``ValidationDecision`` (individual specification) or a
    ``DecompositionDecision``. Retains the native verdict, remediation,
    findings, evaluation source, execution provenance and the full producing
    identity (REQ-002, REQ-003, REQ-004): for an individual decision this is
    ``repository``/``issue_number``/``specification_digest``/``policy_identity``/
    ``relationship_digest``; for a decomposition decision this is the full
    ``parent``/``children`` ``SetMemberIdentity`` tuple plus ``policy_identity``.
    Neither ``SpecificationAnalysisResult`` nor ``DecompositionAnalysisResult``
    carries a raw-backend-response field (unlike the PR adversarial report),
    so there is nothing to bound/truncate here: only already-bounded,
    structured dataclass fields are retained (REQ-008 is naturally satisfied).
    """
    identity = getattr(decision, "identity", None)
    identity_dict = dataclasses.asdict(identity) if dataclasses.is_dataclass(identity) and not isinstance(identity, type) else None
    return {
        "verdict": getattr(decision, "verdict", None),
        "findings": _asdict_list(getattr(decision, "findings", ()) or ()),
        "remediation": getattr(decision, "remediation", None),
        "remediation_reason": getattr(decision, "remediation_reason", None),
        "evaluation_source": getattr(decision, "evaluation_source", None),
        "execution_provenance": getattr(decision, "execution_provenance", None),
        "legacy_candidates_detected": getattr(decision, "legacy_candidates_detected", None),
        "identity": identity_dict,
    }


def _record_evaluation_best_effort(record: ReviewAuditRecord) -> None:
    try:
        get_review_audit_store().record_evaluation(record)
    except Exception:
        logger.opt(exception=True).warning(f"Issue review audit: failed to record evaluation {record.review_id}")


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
        logger.opt(exception=True).warning(f"Issue review audit: failed to update evaluation {review_id}")


def record_bypassed(
    *,
    repository: str,
    target_number: int,
    review_kind: str,
    origin: str,
    policy_identity: str = "",
    related_issue_membership: Optional[str] = None,
) -> str:
    """Record a reached BYPASSED observation: review is explicitly disabled.

    No native verdict is produced and no reviewer-backend invocation occurs;
    no LLM job or authorization decision is created (REQ-001, REQ-003).
    """
    review_id = _new_id()
    record = ReviewAuditRecord(
        review_id=review_id,
        repository=repository,
        target_type="issue",
        target_number=str(target_number),
        review_kind=review_kind,
        origin=origin,
        process_identity=str(os.getpid()),
        creation_time=_now_iso(),
        creation_sequence=_monotonic_sequence(),
        reviewed_generation="",
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


def find_reusable_source_review_id(
    *,
    repository: str,
    target_number: int,
    review_kind: str,
    generation_key: Optional[str],
    policy_identity: Optional[str] = None,
) -> Optional[str]:
    """Locate the producing review_id for an EXECUTED same-identity decision.

    Generalizes ``pr_adversarial_audit.find_reusable_source_review_id`` for
    the identity-key-as-generation model (REQ-005): Issue specification and
    decomposition decisions have no head-SHA equivalent, so the producing
    ``ValidationIdentity``/``DecompositionIdentity``'s stable ``.key`` digest
    is used as ``reviewed_generation`` instead. Returns ``None`` when no
    matching instrumented producer is found (including the legacy case: a
    decision computed or cached before this audit adapter existed). Never
    triggers a new backend invocation or alters the reused result.
    """
    if not generation_key:
        return None
    try:
        result = get_review_audit_store().get_related_evaluations(repository, "issue", str(target_number), review_kind=review_kind)
    except Exception:
        logger.opt(exception=True).warning("Issue review audit: failed to look up reusable source review")
        return None
    # Unlike the PR adversarial adapter's head-SHA-based ``reviewed_generation``
    # (which needs a separate ``policy_identity`` filter to avoid conflating
    # two different policies reviewing the same head), ``generation_key`` here
    # is a ``ValidationIdentity``/``DecompositionIdentity`` digest that already
    # cryptographically includes ``policy_identity`` as one of its hashed
    # fields: two decisions with different policies never share a
    # ``generation_key``. ``policy_identity`` is accepted for documentation
    # symmetry with the PR adapter but is not needed as an extra filter.
    del policy_identity
    candidates = [record for record in result.records if record.reviewed_generation == generation_key and record.execution_mode == ExecutionMode.EXECUTED and record.lifecycle == EvaluationLifecycle.FINISHED]
    if not candidates:
        return None
    latest = max(candidates, key=lambda record: record.creation_sequence)
    return latest.review_id


def run_traced_review(
    *,
    repository: str,
    target_number: int,
    stage_id: str,
    facts: Optional[Dict[str, Any]],
    origin: str,
    fn: Callable[[], T],
) -> T:
    """Run one ``decide()`` call with best-effort, non-authorizing audit recording.

    This is the single shared boundary ``AutomationEngine._traced_validation_job``
    (and, through it, ``IssueReviewService``) uses for both individual and
    decomposition review jobs (REQ-001 through REQ-005): the caller never
    needs to know in advance which of the EXECUTED/REUSED/LOCAL_ONLY branches
    ``decide()`` will take internally; classification happens strictly after
    ``fn()`` returns (see module docstring).

    A pre-flight ``QUEUED``/``RUNNING`` evaluation row is recorded before
    ``fn()`` runs (mirrors ``pr_adversarial_audit.begin_executed_review``) so
    interactions captured during ``fn()`` are attributable back to this
    review_id (``ReviewAuditStore.get_evaluation`` only returns interactions
    for a review_id that already has an evaluation row). Because
    ``ReviewAuditStore.update_evaluation`` has no parameter to set the
    dedicated ``source_review_id`` column after that initial insert (only
    the first ``record_evaluation`` call can set it, and REUSED cannot be
    known before ``fn()`` runs), a REUSED classification's resolved
    provenance is instead recorded inside ``native_report["reuse_source_review_id"]``,
    which remains durable and queryable; the dedicated column stays ``None``
    for these rows rather than being fabricated (REQ-005). ``review_audit.py``
    is an existing, out-of-scope store and is not modified to close this gap.

    Every audit-recording step is wrapped in a narrow try/except that only
    logs: a raised exception from ``fn()`` is always re-raised unchanged, and
    nothing here can prevent ``fn()`` from running or change what it returns
    (REQ-007).
    """
    review_kind = _STAGE_REVIEW_KIND.get(stage_id)
    if review_kind is None:
        return fn()

    review_id = _new_id()
    generation_hint = str((facts or {}).get("validation_identity") or "")
    try:
        record = ReviewAuditRecord(
            review_id=review_id,
            repository=repository,
            target_type="issue",
            target_number=str(target_number),
            review_kind=review_kind,
            origin=origin,
            process_identity=str(os.getpid()),
            creation_time=_now_iso(),
            creation_sequence=_monotonic_sequence(),
            reviewed_generation=generation_hint,
            policy_identity="",
            related_issue_membership=None,
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
            repository=repository,
            lifecycle=EvaluationLifecycle.RUNNING,
            execution_mode=ExecutionMode.UNKNOWN,
        )
    except Exception:
        logger.opt(exception=True).warning(f"Issue review audit: failed to open evaluation {review_id}")

    try:
        with bind_review_context(
            review_id=review_id,
            repository=repository,
            target_type="issue",
            target_number=str(target_number),
            review_kind=review_kind,
            generation_identity=generation_hint or f"unknown:{stage_id}:{target_number}",
        ):
            decision = fn()
    except BaseException:
        _finish_traced_review_best_effort(
            review_id=review_id,
            repository=repository,
            target_number=target_number,
            review_kind=review_kind,
            decision=None,
        )
        raise
    _finish_traced_review_best_effort(
        review_id=review_id,
        repository=repository,
        target_number=target_number,
        review_kind=review_kind,
        decision=decision,
    )
    return decision


def _finish_traced_review_best_effort(*, review_id: str, repository: str, target_number: int, review_kind: str, decision: Any) -> None:
    invoked = False
    try:
        read = get_review_audit_store().get_evaluation(repository, review_id)
        invoked = bool(read.record and read.record.interactions)
    except Exception:
        logger.opt(exception=True).warning(f"Issue review audit: failed to inspect interactions for {review_id}")

    native_report: Optional[Dict[str, Any]]
    if decision is None:
        # fn() raised before producing a decision. EXECUTED is still
        # possible (a real backend call happened, then something else
        # raised, e.g. a durable persistence failure); otherwise this is
        # treated like a local refusal (mirrors
        # pr_adversarial_audit.finish_executed_review's ``result is None``
        # branch).
        execution_mode = ExecutionMode.EXECUTED if invoked else ExecutionMode.LOCAL_ONLY
        native_verdict: Optional[str] = "ERROR"
        native_report = {
            "diagnostic_category": "unrecovered_exception",
            "diagnostic_reason": f"{review_kind} validation raised before producing a decision",
        }
    else:
        evaluation_source = getattr(decision, "evaluation_source", None)
        if invoked:
            execution_mode = ExecutionMode.EXECUTED
        elif evaluation_source == "local-only":
            execution_mode = ExecutionMode.LOCAL_ONLY
        else:
            execution_mode = ExecutionMode.REUSED
        native_verdict = str(getattr(decision, "verdict", None) or "ERROR")
        try:
            native_report = normalize_decision_for_audit(decision)
        except Exception:
            logger.opt(exception=True).warning(f"Issue review audit: failed to normalize decision for {review_id}")
            native_report = None

    if execution_mode == ExecutionMode.REUSED and native_report is not None:
        try:
            identity = getattr(decision, "identity", None)
            generation_key = getattr(identity, "key", None)
            policy_identity = getattr(identity, "policy_identity", None)
            source_review_id = find_reusable_source_review_id(
                repository=repository,
                target_number=target_number,
                review_kind=review_kind,
                generation_key=generation_key,
                policy_identity=policy_identity,
            )
            native_report = {**native_report, "reuse_source_review_id": source_review_id}
        except Exception:
            logger.opt(exception=True).warning(f"Issue review audit: failed to resolve reuse source for {review_id}")

    _update_evaluation_best_effort(
        review_id=review_id,
        repository=repository,
        lifecycle=EvaluationLifecycle.FINISHED,
        execution_mode=execution_mode,
        native_verdict=native_verdict,
        native_report=native_report,
    )


def record_effect(
    *,
    repository: str,
    target_number: int,
    review_kind: str,
    generation_key: Optional[str],
    policy_identity: Optional[str],
    disposition: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one external-effect observation for the review that produced this decision.

    Mirrors ``pr_adversarial_audit.record_effect`` (REQ-006, REQ-007):
    observation-only, best-effort, and a no-op when no owning EXECUTED
    review_id can be found for ``generation_key`` (a legacy decision
    predating this audit adapter, or one whose executing review was never
    durably recorded). Never fails the caller and never changes publication
    behavior or scheduling.
    """
    try:
        review_id = find_reusable_source_review_id(
            repository=repository,
            target_number=target_number,
            review_kind=review_kind,
            generation_key=generation_key,
            policy_identity=policy_identity,
        )
        if not review_id:
            return
        effect = ReviewEffectRecord(
            review_id=review_id,
            effect_id=_new_id(),
            observation_time=_now_iso(),
            disposition=disposition,
            details=details,
        )
        get_review_audit_store().record_effect(repository, effect)
    except Exception:
        logger.opt(exception=True).warning("Issue review audit: failed to record effect")
