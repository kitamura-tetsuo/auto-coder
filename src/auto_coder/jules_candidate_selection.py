"""Fail-closed acceptance and selection for speculative Jules artifacts.

This boundary intentionally consumes already-produced CI and adversarial-review
results.  It does not run either evaluator, compare candidates, or perform a
merge.  Selection is delegated to :class:`JulesCompetitionLedger`, whose
serialized transition decides which concurrent PASS record wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .ci_observation import CIConclusion, CIObservationSnapshot, ObservationAvailability
from .jules_competition_ledger import AcceptanceRecord, JulesCompetitionLedger, SelectionResult


class CandidateEvaluationDisposition(str, Enum):
    PASS = "PASS"
    PENDING = "PENDING"
    RETIRE = "RETIRE"


@dataclass(frozen=True)
class CandidateTarget:
    repository: str = ""
    issue_number: int = 0
    generation_id: str = ""
    candidate_id: str = ""
    pr_repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    base_ref: str = ""
    captured_base_sha: str = ""
    captured_base_ref: str = ""
    issue_oracle_fingerprint: str = ""
    binding_revision: int = 0
    generation_active: bool = False
    candidate_retired: bool = False
    provenance_verified: bool = False
    provenance_conflicting: bool = False
    pr_open: bool = False
    pr_draft: bool = False
    effective_diff_files: int = 0


@dataclass(frozen=True)
class IndependentValidationVerdict:
    result: str = "ERROR"
    head_sha: str = ""
    issue_oracle_fingerprint: str = ""
    validation_revision: int = 0
    complete_requirement_coverage: bool = False
    merge_blocking_findings: int = 0
    specification_gaps: int = 0
    validator_enabled: bool = True


@dataclass(frozen=True)
class CandidateAcceptanceEvidence:
    target: CandidateTarget = field(default_factory=CandidateTarget)
    ci: Optional[CIObservationSnapshot] = None
    ci_success: bool = False
    ci_in_progress: bool = False
    additional_merge_guards_pass: bool = False
    validation: IndependentValidationVerdict = field(default_factory=IndependentValidationVerdict)
    invalidation_revision: int = 0


@dataclass(frozen=True)
class CandidateEvaluation:
    disposition: CandidateEvaluationDisposition
    reason: str
    acceptance: Optional[AcceptanceRecord] = None


def evaluate_candidate(evidence: CandidateAcceptanceEvidence) -> CandidateEvaluation:
    """Normalize evidence without interpreting unavailable evidence as failure.

    ``RETIRE`` is deliberately restricted to the three definitive artifact
    failures owned by this stage.  Every missing, partial, pending, stale, or
    contradictory input remains ``PENDING``.
    """
    target = evidence.target
    validation = evidence.validation
    if not target.generation_active or target.candidate_retired:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "candidate authority is not active")
    if not target.provenance_verified or target.provenance_conflicting:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "PR provenance is unavailable or conflicting")
    if target.pr_repository.lower() != target.repository.lower():
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "PR repository does not match the captured repository")
    if target.base_ref != target.captured_base_ref or target.base_sha != target.captured_base_sha:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "PR base does not match the captured base")
    if not target.pr_open or target.pr_draft:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "PR is not an open, non-draft target")
    if target.effective_diff_files == 0:
        return CandidateEvaluation(CandidateEvaluationDisposition.RETIRE, "authoritative effective diff is empty")
    if target.effective_diff_files < 0:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "effective diff is unavailable")

    ci = evidence.ci
    if ci is None or ci.availability is not ObservationAvailability.KNOWN:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "complete CI evidence is unavailable")
    if ci.subject.repository.lower() != target.pr_repository.lower() or ci.subject.pr_number != target.pr_number or ci.subject.head_sha != target.head_sha:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "CI evidence targets another PR revision")
    if evidence.ci_in_progress:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "CI remains in progress")
    conclusions = tuple(fact.conclusion for fact in ci.facts)
    terminal_failures = {
        CIConclusion.FAILURE,
        CIConclusion.CANCELLED,
        CIConclusion.TIMED_OUT,
        CIConclusion.ACTION_REQUIRED,
    }
    if any(value in terminal_failures for value in conclusions) and not evidence.ci_in_progress:
        return CandidateEvaluation(CandidateEvaluationDisposition.RETIRE, "authoritative CI failure")
    if not evidence.ci_success or CIConclusion.SUCCESS not in conclusions:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "CI has no positive successful evidence")
    if not evidence.additional_merge_guards_pass:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "additional merge guards are not satisfied")

    normalized = validation.result.strip().upper()
    validation_is_current = validation.validator_enabled and validation.head_sha == target.head_sha and validation.issue_oracle_fingerprint == target.issue_oracle_fingerprint and validation.complete_requirement_coverage and evidence.invalidation_revision <= validation.validation_revision
    if not validation_is_current:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "independent verdict is incomplete or stale")
    if normalized == "NEEDS_FIX" and validation.merge_blocking_findings > 0:
        return CandidateEvaluation(CandidateEvaluationDisposition.RETIRE, "independent validation found a required fix")
    if normalized != "PASS":
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "independent validation did not produce PASS")
    if validation.merge_blocking_findings or validation.specification_gaps:
        return CandidateEvaluation(CandidateEvaluationDisposition.PENDING, "independent PASS is incomplete or stale")

    acceptance = AcceptanceRecord(
        repository=target.repository,
        generation_id=target.generation_id,
        candidate_id=target.candidate_id,
        pr_repository=target.pr_repository,
        pr_number=target.pr_number,
        head_sha=target.head_sha,
        base_sha=target.base_sha,
        issue_oracle_fingerprint=target.issue_oracle_fingerprint,
        expected_binding_revision=target.binding_revision,
        validation_revision=validation.validation_revision,
        invalidation_revision=evidence.invalidation_revision,
    )
    return CandidateEvaluation(CandidateEvaluationDisposition.PASS, "current independent acceptance established", acceptance)


def select_accepted_candidate(
    ledger: JulesCompetitionLedger,
    evidence: CandidateAcceptanceEvidence,
    *,
    operation_id: str,
    expected_epoch: int,
) -> tuple[CandidateEvaluation, Optional[SelectionResult]]:
    """Commit a PASS through the ledger's serialized first-writer transition."""
    evaluation = evaluate_candidate(evidence)
    if evaluation.acceptance is None:
        return evaluation, None
    target = evidence.target
    selected = ledger.select_winner(
        target.repository,
        target.issue_number,
        target.generation_id,
        target.candidate_id,
        operation_id,
        expected_epoch,
        evaluation.acceptance,
    )
    return evaluation, selected


def selected_pair_authorized(
    ledger: JulesCompetitionLedger,
    target: CandidateTarget,
    *,
    current_invalidation_revision: int,
) -> bool:
    """Reestablish immutable selected-pair authority at an outbound boundary."""
    namespace = ledger.get_namespace_snapshot(target.repository, target.issue_number)
    generation = namespace.get_generation(target.generation_id)
    return bool(
        generation
        and generation.is_active()
        and generation.winner_candidate_id == target.candidate_id
        and generation.winner_pr_repository == target.pr_repository
        and generation.winner_pr_number == target.pr_number
        and generation.winner_head_sha == target.head_sha
        and generation.winner_base_sha == target.base_sha
        and generation.winner_validation_revision is not None
        and generation.winner_invalidation_revision is not None
        and current_invalidation_revision <= generation.winner_validation_revision
        and current_invalidation_revision >= generation.winner_invalidation_revision
        and generation.issue_oracle_fingerprint == target.issue_oracle_fingerprint
        and not namespace.has_pending_reconciliation()
    )
