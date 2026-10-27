"""Exact, complete, current-evidence PR blocker closure validation.

Stage S4 of convergent PR review tracking (#2134, GitHub Issue #2138):
Binds review blocker closure strictly to the exact original finding and
complete current-head evidence, persisting accepted closures to the canonical
blocker ledger before executing GitHub resolve mutations.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional, Sequence, Set, Tuple

from .adversarial_validator import ReviewThreadDisposition
from .canonical_pr_blocker_ledger import (
    BlockerAlias,
    BlockerDisposition,
    BlockerLedgerSnapshot,
    BlockerSnapshot,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    QualifiedRequirement,
    StaleLedgerRevisionError,
    UnknownBlockerReferenceError,
)
from .logger_config import get_logger
from .review_thread_closure_receipt import ClosureReceipt, closure_receipt_lock, observe_closure_receipt, put_receipt
from .review_thread_validation import (
    UNRESOLVE_ROLLBACK_MAX_ATTEMPTS,
    ClaimedReviewThread,
    StaleReviewThreadRegistry,
    StaleReviewThreadResolutionError,
    _find_claimed_thread,
)
from .runtime_locks import LockAcquisitionTimeout
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)

_BLOCKER_ID_RE = re.compile(r"(?:Blocker identity:\s*`?([^`\s\n]+)`?|blocker_id[=:]\s*`?([^`\s\n]+)`?)", re.IGNORECASE)
_GAP_ID_RE = re.compile(r"(?:Gap identity:\s*`?([^`\s\n]+)`?|gap_id[=:]\s*`?([^`\s\n]+)`?|TEST_ORACLE_GAP\s+([a-zA-Z0-9_-]+))", re.IGNORECASE)
_REQ_ID_RE = re.compile(r"\b(REQ-[0-9A-Za-z_-]+)\b")


# ---------------------------------------------------------------------------
# Domain Dataclasses (REQ-001, REQ-007)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureCandidate:
    """A claimed review thread matched to its canonical blocker and concrete scope."""

    api_origin: str = ""
    repository: str = ""
    pr_number: int = 0
    thread_id: str = ""
    root_comment_database_id: Optional[int] = None
    blocker_id: Optional[str] = None
    category: str = "IMPLEMENTATION"  # IMPLEMENTATION, TEST_ORACLE, SPECIFICATION, REGRESSION
    authoritative_boundary: str = ""
    requirement_ids: tuple[str, ...] = ()
    original_finding: str = ""
    accepted_scope_description: str = ""
    owned_concern_ids: tuple[str, ...] = ()
    reviewed_head_sha: str = ""
    reviewed_base_sha: str = ""
    requirement_manifest_revision: str = ""
    review_attempt_id: str = ""
    ledger_revision: int = 0
    known_blocker_ids_for_thread: tuple[str, ...] = ()
    known_absent_apis: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClosureEvaluationResult:
    """Outcome of evaluating a validator disposition against a closure candidate."""

    candidate: ClosureCandidate
    is_accepted: bool = False
    effective_status: str = "INCONCLUSIVE"  # "ADDRESSED", "STILL_VALID", "INCONCLUSIVE"
    rationale: str = ""
    evidence: str = ""
    remaining_concern_ids: tuple[str, ...] = ()
    rejection_reason: Optional[str] = None
    evaluated_blocker_id: Optional[str] = None
    thread_id: str = ""


@dataclass(frozen=True)
class ThreadClosureOutcome:
    """Machine-readable settlement state for one selected review thread."""

    repository: str = ""
    pr_number: int = 0
    thread_id: str = ""
    evaluated_head_sha: str = ""
    review_attempt_id: str = ""
    root_comment_database_id: Optional[int] = None
    blocker_ids: tuple[str, ...] = ()
    decision: str = "MISSING"
    acceptance_state: str = "NOT_ACCEPTED"
    effect_state: str = "NOT_ATTEMPTED"
    phase: str = "disposition"
    reason: str = "No disposition was returned for the selected thread"
    cleanup_warning: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self.decision == "ADDRESSED" and self.acceptance_state == "CONFIRMED" and self.effect_state == "CONFIRMED" and self.cleanup_warning is None


@dataclass(frozen=True, eq=False)
class ClosureExecutionResult:
    """Outcome of persisting closures to the ledger and resolving GitHub threads."""

    accepted_closures: tuple[ClosureEvaluationResult, ...] = ()
    resolved_thread_ids: tuple[str, ...] = ()
    unresolved_thread_ids: tuple[str, ...] = ()
    persisted_blocker_transitions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    thread_outcomes: tuple[ThreadClosureOutcome, ...] = ()

    @property
    def unfinished_outcomes(self) -> tuple[ThreadClosureOutcome, ...]:
        return tuple(outcome for outcome in self.thread_outcomes if not outcome.completed)

    # Keep the former sequence surface while callers migrate to the report.
    def __iter__(self):
        return iter(self.resolved_thread_ids)

    def __len__(self) -> int:
        return len(self.resolved_thread_ids)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(other) == self.resolved_thread_ids
        if not isinstance(other, ClosureExecutionResult):
            return False
        return self.__dict__ == other.__dict__


# ---------------------------------------------------------------------------
# Candidate Extraction (REQ-001, REQ-006)
# ---------------------------------------------------------------------------


def extract_closure_candidates(
    claimed_threads: Sequence[ClaimedReviewThread],
    snapshot: Optional[BlockerLedgerSnapshot] = None,
    *,
    api_origin: str = "https://api.github.com",
    repository: str = "",
    pr_number: int = 0,
    reviewed_head_sha: str = "",
    reviewed_base_sha: str = "",
    requirement_manifest_revision: str = "",
    review_attempt_id: str = "",
    known_absent_apis: tuple[str, ...] = (),
) -> tuple[ClosureCandidate, ...]:
    """Extract closure candidates for each claimed review thread from the ledger snapshot."""
    norm_origin = normalize_api_origin(api_origin)
    candidates: list[ClosureCandidate] = []

    for claimed_thread in claimed_threads:
        matching_blockers: list[BlockerSnapshot] = []
        root_comment_id_str = str(claimed_thread.root_comment_database_id) if claimed_thread.root_comment_database_id is not None else None

        if snapshot is not None:
            # Match by root comment database ID alias
            if root_comment_id_str:
                matching_blockers.extend(snapshot.get_blockers_for_alias("github_root_comment", root_comment_id_str))
                matching_blockers.extend(snapshot.get_blockers_for_alias("root_comment_id", root_comment_id_str))
                matching_blockers.extend(snapshot.get_blockers_for_alias("historical_root_comment_id", root_comment_id_str))

            # Match by thread ID alias
            if claimed_thread.thread_id:
                matching_blockers.extend(snapshot.get_blockers_for_alias("github_thread", claimed_thread.thread_id))

        # Check explicit blocker ID embedded in root comment body or thread properties
        extracted_blocker_id: Optional[str] = None
        if claimed_thread.blocker_ids:
            extracted_blocker_id = claimed_thread.blocker_ids[0]
        else:
            match = _BLOCKER_ID_RE.search(claimed_thread.original_finding)
            if match:
                extracted_blocker_id = match.group(1) or match.group(2)

        extracted_gap_id: Optional[str] = None
        gap_match = _GAP_ID_RE.search(claimed_thread.original_finding)
        if gap_match:
            extracted_gap_id = gap_match.group(1) or gap_match.group(2) or gap_match.group(3)

        if snapshot is not None:
            if extracted_blocker_id:
                b = snapshot.get_blocker(extracted_blocker_id)
                if b and b not in matching_blockers:
                    matching_blockers.append(b)
            if extracted_gap_id:
                for b in snapshot.get_blockers_for_alias("test_oracle_gap", extracted_gap_id):
                    if b not in matching_blockers:
                        matching_blockers.append(b)

        # Deduplicate while preserving order
        unique_blockers: list[BlockerSnapshot] = []
        seen_ids: set[str] = set()
        for b in matching_blockers:
            if b.blocker_id not in seen_ids:
                seen_ids.add(b.blocker_id)
                unique_blockers.append(b)

        all_blocker_ids = tuple(b.blocker_id for b in unique_blockers)
        ledger_rev = snapshot.ledger_revision if snapshot is not None else 0

        if unique_blockers:
            for b in unique_blockers:
                req_ids = tuple(r.requirement_id for r in b.qualified_requirements)
                concern_ids = b.concern_ids or b.accepted_scope.concern_ids
                candidates.append(
                    ClosureCandidate(
                        api_origin=norm_origin,
                        repository=repository,
                        pr_number=pr_number,
                        thread_id=claimed_thread.thread_id,
                        root_comment_database_id=claimed_thread.root_comment_database_id,
                        blocker_id=b.blocker_id,
                        category=b.category or "IMPLEMENTATION",
                        authoritative_boundary=b.authoritative_boundary or claimed_thread.authoritative_boundary,
                        requirement_ids=req_ids,
                        original_finding=claimed_thread.original_finding,
                        accepted_scope_description=b.accepted_scope.description,
                        owned_concern_ids=concern_ids or claimed_thread.concern_ids,
                        reviewed_head_sha=reviewed_head_sha,
                        reviewed_base_sha=reviewed_base_sha,
                        requirement_manifest_revision=requirement_manifest_revision,
                        review_attempt_id=review_attempt_id,
                        ledger_revision=ledger_rev,
                        known_blocker_ids_for_thread=all_blocker_ids,
                        known_absent_apis=known_absent_apis,
                    )
                )
        else:
            # Blocker not explicitly recorded in ledger snapshot yet (e.g. standalone claimed thread)
            req_matches = tuple(sorted(set(_REQ_ID_RE.findall(claimed_thread.original_finding))))
            candidates.append(
                ClosureCandidate(
                    api_origin=norm_origin,
                    repository=repository,
                    pr_number=pr_number,
                    thread_id=claimed_thread.thread_id,
                    root_comment_database_id=claimed_thread.root_comment_database_id,
                    blocker_id=extracted_blocker_id,
                    category=claimed_thread.category or ("TEST_ORACLE" if extracted_gap_id else "IMPLEMENTATION"),
                    authoritative_boundary=claimed_thread.authoritative_boundary,
                    requirement_ids=req_matches,
                    original_finding=claimed_thread.original_finding,
                    accepted_scope_description="",
                    owned_concern_ids=claimed_thread.concern_ids,
                    reviewed_head_sha=reviewed_head_sha,
                    reviewed_base_sha=reviewed_base_sha,
                    requirement_manifest_revision=requirement_manifest_revision,
                    review_attempt_id=review_attempt_id,
                    ledger_revision=ledger_rev,
                    known_blocker_ids_for_thread=(extracted_blocker_id,) if extracted_blocker_id else (),
                    known_absent_apis=known_absent_apis,
                )
            )

    return tuple(candidates)


# ---------------------------------------------------------------------------
# Closure Candidate Evaluation (REQ-001 - REQ-005, REQ-007)
# ---------------------------------------------------------------------------


def evaluate_closure_candidate(
    candidate: ClosureCandidate,
    disposition: ReviewThreadDisposition,
    *,
    all_known_blocker_ids: Optional[Set[str]] = None,
    other_candidates: Sequence[ClosureCandidate] = (),
) -> ClosureEvaluationResult:
    """Evaluate an LLM review thread disposition against a candidate closure."""
    thread_id = candidate.thread_id
    if disposition.thread_id != thread_id:
        return ClosureEvaluationResult(
            candidate=candidate,
            is_accepted=False,
            effective_status="INCONCLUSIVE",
            rationale=disposition.rationale,
            evidence=disposition.evidence,
            rejection_reason=f"Disposition thread_id {disposition.thread_id!r} does not match candidate {thread_id!r}",
            thread_id=thread_id,
        )

    # REQ-007: Reject duplicate, unknown, cross-target, or mismatched disposition identities
    if disposition.blocker_id is not None:
        disp_blocker_id = disposition.blocker_id.strip()
        if all_known_blocker_ids is not None and disp_blocker_id not in all_known_blocker_ids:
            return ClosureEvaluationResult(
                candidate=candidate,
                is_accepted=False,
                effective_status="INCONCLUSIVE",
                rationale=disposition.rationale,
                evidence=disposition.evidence,
                rejection_reason=f"Disposition cited unknown blocker ID {disp_blocker_id!r}",
                evaluated_blocker_id=disp_blocker_id,
                thread_id=thread_id,
            )
        if candidate.blocker_id and disp_blocker_id != candidate.blocker_id and disp_blocker_id not in candidate.known_blocker_ids_for_thread:
            return ClosureEvaluationResult(
                candidate=candidate,
                is_accepted=False,
                effective_status="INCONCLUSIVE",
                rationale=disposition.rationale,
                evidence=disposition.evidence,
                rejection_reason=f"Disposition cited blocker ID {disp_blocker_id!r} which belongs to a different thread or blocker",
                evaluated_blocker_id=disp_blocker_id,
                thread_id=thread_id,
            )

    evaluated_blocker_id = candidate.blocker_id or disposition.blocker_id

    # If status is not ADDRESSED, disposition is not an acceptance candidate
    if disposition.status != "ADDRESSED":
        return ClosureEvaluationResult(
            candidate=candidate,
            is_accepted=False,
            effective_status=disposition.status,
            rationale=disposition.rationale,
            evidence=disposition.evidence,
            evaluated_blocker_id=evaluated_blocker_id,
            thread_id=thread_id,
        )

    combined_text = f"{disposition.rationale}\n{disposition.evidence}"

    # REQ-004: Treat an implementer assertion, commit push, green test result as insufficient by itself.
    # Evidence relying on an API or field absent from current producer cannot establish correction (AS-003).
    for absent_api in candidate.known_absent_apis:
        if absent_api and absent_api in combined_text:
            return ClosureEvaluationResult(
                candidate=candidate,
                is_accepted=False,
                effective_status="STILL_VALID",
                rationale=disposition.rationale,
                evidence=disposition.evidence,
                rejection_reason=f"Evidence relies on API or field absent from producer: {absent_api!r}",
                evaluated_blocker_id=evaluated_blocker_id,
                thread_id=thread_id,
            )

    # REQ-001, REQ-003: Bind each claimed correction and supporting evidence to its specific owned concern
    # and authoritative boundary. Evidence about a different blocker or boundary cannot authorize closure (AS-001).
    if candidate.authoritative_boundary:
        cand_boundary = candidate.authoritative_boundary.strip()
        # Check if other candidates have different boundaries that this disposition exclusively cites
        for other in other_candidates:
            if other.thread_id != candidate.thread_id and other.authoritative_boundary:
                other_b = other.authoritative_boundary.strip()
                if other_b and other_b != cand_boundary:
                    # If the evidence mentions the other boundary and does NOT mention candidate's boundary or scope:
                    if other_b in combined_text and cand_boundary not in combined_text:
                        # Check if any key part of candidate's accepted scope is mentioned
                        cand_scope_words = [w for w in re.findall(r"\w+", candidate.accepted_scope_description) if len(w) > 4]
                        if not any(w.lower() in combined_text.lower() for w in cand_scope_words):
                            return ClosureEvaluationResult(
                                candidate=candidate,
                                is_accepted=False,
                                effective_status="STILL_VALID" if "defect" in combined_text.lower() else "INCONCLUSIVE",
                                rationale=disposition.rationale,
                                evidence=disposition.evidence,
                                rejection_reason=(f"Evidence addresses distinct boundary {other_b!r} (from thread {other.thread_id}) " f"rather than candidate boundary {cand_boundary!r}"),
                                evaluated_blocker_id=evaluated_blocker_id,
                                thread_id=thread_id,
                            )

    # REQ-002, AS-002: Accept ADDRESSED only when current evidence establishes correction of every concrete
    # concern owned by that blocker. Partial fixes leave the blocker open with remaining concerns identified.
    if candidate.owned_concern_ids:
        owned_concerns = set(candidate.owned_concern_ids)
        # Check if disposition explicitly lists concern_ids
        if disposition.concern_ids:
            addressed_concerns = set(disposition.concern_ids)
            remaining = tuple(sorted(owned_concerns - addressed_concerns))
            if remaining:
                return ClosureEvaluationResult(
                    candidate=candidate,
                    is_accepted=False,
                    effective_status="STILL_VALID",
                    rationale=disposition.rationale,
                    evidence=disposition.evidence,
                    remaining_concern_ids=remaining,
                    rejection_reason=f"Partial correction: remaining concern(s) {remaining} not addressed",
                    evaluated_blocker_id=evaluated_blocker_id,
                    thread_id=thread_id,
                )

        # Check for textual indication of partial fix (e.g. "remaining concern", "partial fix", "still reproducible")
        partial_match = re.search(r"(?:remaining concern[s]?|still reproducible|unfixed concern[s]?|partially addressed)[:\s]+`?([a-zA-Z0-9_,-]+)`?", combined_text, re.IGNORECASE)
        if partial_match:
            remaining_str = partial_match.group(1)
            remaining = tuple(r.strip() for r in remaining_str.split(",") if r.strip() in owned_concerns)
            if not remaining and len(candidate.owned_concern_ids) > 1:
                # If there are multiple concerns and text indicates partial fix, identify unaddressed concerns
                remaining = tuple(c for c in candidate.owned_concern_ids if c.lower() not in combined_text.lower())
            if remaining:
                return ClosureEvaluationResult(
                    candidate=candidate,
                    is_accepted=False,
                    effective_status="STILL_VALID",
                    rationale=disposition.rationale,
                    evidence=disposition.evidence,
                    remaining_concern_ids=remaining,
                    rejection_reason=f"Partial correction: concern(s) {remaining} still reproducible",
                    evaluated_blocker_id=evaluated_blocker_id,
                    thread_id=thread_id,
                )

    # REQ-005, AS-003: Keep implementation and test-oracle dispositions distinct.
    # A missing required test oracle cannot be closed merely because a helper test passes.
    if candidate.category == "TEST_ORACLE":
        # Must have test oracle evidence, not merely implementation assertions
        cited_spec = re.search(r"\b[\w./-]+\.spec\.[cm]?[jt]sx?\b", combined_text, re.IGNORECASE)
        if "test" not in combined_text.lower() and "oracle" not in combined_text.lower() and cited_spec is None:
            return ClosureEvaluationResult(
                candidate=candidate,
                is_accepted=False,
                effective_status="STILL_VALID",
                rationale=disposition.rationale,
                evidence=disposition.evidence,
                rejection_reason="Test oracle gap disposition lacks specific regression test evidence",
                evaluated_blocker_id=evaluated_blocker_id,
                thread_id=thread_id,
            )

    # All criteria met: accepted closure
    return ClosureEvaluationResult(
        candidate=candidate,
        is_accepted=True,
        effective_status="ADDRESSED",
        rationale=disposition.rationale,
        evidence=disposition.evidence,
        evaluated_blocker_id=evaluated_blocker_id,
        thread_id=thread_id,
    )


def adjudicate_claimed_thread_closures(
    candidates: Sequence[ClosureCandidate],
    dispositions: Sequence[ReviewThreadDisposition],
    snapshot: Optional[BlockerLedgerSnapshot] = None,
) -> tuple[ClosureEvaluationResult, ...]:
    """Adjudicate all candidates against validator dispositions."""
    all_known_ids: Optional[Set[str]] = None
    if snapshot is not None:
        all_known_ids = {b.blocker_id for b in snapshot.blockers}

    evaluations: list[ClosureEvaluationResult] = []
    for cand in candidates:
        matching_disp = next((d for d in dispositions if d.thread_id == cand.thread_id), None)
        if matching_disp is None:
            # REQ-007: An omitted disposition must preserve the prior open obligation rather than imply success
            evaluations.append(
                ClosureEvaluationResult(
                    candidate=cand,
                    is_accepted=False,
                    effective_status="INCONCLUSIVE",
                    rationale="No disposition provided by validator",
                    evidence="",
                    rejection_reason="Disposition omitted from validator response",
                    evaluated_blocker_id=cand.blocker_id,
                    thread_id=cand.thread_id,
                )
            )
        else:
            eval_res = evaluate_closure_candidate(
                cand,
                matching_disp,
                all_known_blocker_ids=all_known_ids,
                other_candidates=candidates,
            )
            evaluations.append(eval_res)

    return tuple(evaluations)


# ---------------------------------------------------------------------------
# Durable Execution and Thread Resolution (REQ-006, REQ-008, AS-004, AS-005)
# ---------------------------------------------------------------------------


def execute_durable_thread_closures(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    validated_head_sha: str,
    evaluations: Sequence[ClosureEvaluationResult],
    *,
    ledger: Optional[CanonicalPRBlockerLedger] = None,
    api_origin: str = "https://api.github.com",
    stale_registry: Optional[StaleReviewThreadRegistry] = None,
    expected_ledger_revision: Optional[int] = None,
    claimed_threads: Sequence[ClaimedReviewThread] = (),
) -> ClosureExecutionResult:
    """Serialize receipt observation and effects across all local PR callers."""
    try:
        with closure_receipt_lock(repo_name, pr_number, api_origin=api_origin):
            return _execute_durable_thread_closures(
                github_client,
                repo_name,
                pr_number,
                validated_head_sha,
                evaluations,
                ledger=ledger,
                api_origin=api_origin,
                stale_registry=stale_registry,
                expected_ledger_revision=expected_ledger_revision,
                claimed_threads=claimed_threads,
            )
    except (LockAcquisitionTimeout, OSError) as exc:
        selected_ids = tuple(dict.fromkeys(e.thread_id for e in evaluations if e.thread_id))
        logger.warning(f"Closure receipt processing deferred for {repo_name}#{pr_number}: {exc}")
        return ClosureExecutionResult(
            unresolved_thread_ids=selected_ids,
            errors=tuple(f"{thread_id}|receipt-lock|{exc}" for thread_id in selected_ids),
        )


def _closure_operation_key(evaluations: Sequence[ClosureEvaluationResult]) -> str:
    """Bind effects to stable accepted scope, never to retry or model wording."""
    scopes = []
    for evaluation in evaluations:
        candidate = evaluation.candidate
        scopes.append(
            {
                "api_origin": candidate.api_origin,
                "repository": candidate.repository.casefold(),
                "pr_number": candidate.pr_number,
                "thread_id": candidate.thread_id,
                "root_id": candidate.root_comment_database_id,
                "head": candidate.reviewed_head_sha,
                "base": candidate.reviewed_base_sha,
                "manifest": candidate.requirement_manifest_revision,
                "blocker_id": evaluation.evaluated_blocker_id,
                "category": candidate.category,
                "boundary": candidate.authoritative_boundary,
                "requirements": sorted(candidate.requirement_ids),
                "finding": candidate.original_finding,
                "scope": candidate.accepted_scope_description,
                "concerns": sorted(candidate.owned_concern_ids),
                "owners": sorted(candidate.known_blocker_ids_for_thread),
            }
        )
    encoded = sorted(json.dumps(scope, sort_keys=True, separators=(",", ":")) for scope in scopes)
    return hashlib.sha256(json.dumps(encoded, separators=(",", ":")).encode("utf-8")).hexdigest()


def _execute_durable_thread_closures(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    validated_head_sha: str,
    evaluations: Sequence[ClosureEvaluationResult],
    *,
    ledger: Optional[CanonicalPRBlockerLedger] = None,
    api_origin: str = "https://api.github.com",
    stale_registry: Optional[StaleReviewThreadRegistry] = None,
    expected_ledger_revision: Optional[int] = None,
    claimed_threads: Sequence[ClaimedReviewThread] = (),
) -> ClosureExecutionResult:
    """Persist accepted closures to the ledger and resolve eligible review threads.

    Implements:
    - REQ-008: Persist accepted closures to the ledger with CAS before mutating GitHub.
      Recheck authority immediately before mutation, suppress known stale effects.
    - REQ-006: For a thread/root owning multiple blockers, resolve the thread ONLY
      when all of its owned obligations have valid current closure or authorized invalidation.
    - AS-004: Compound root with blockers A and B remains unresolved when only A is closed,
      while alias root owning A alone resolves.
    - AS-005: Stale completions suppressed; failed durable acceptance prevents resolution;
      reconciles unconfirmed mutations without treating visible thread flags as proof.
    """
    norm_origin = normalize_api_origin(api_origin)
    rollback_registry = stale_registry or StaleReviewThreadRegistry()

    def _discussion_digest(observation: Any, authenticated_controller_login: str) -> str:
        from .review_thread_validation import substantive_discussion_digest

        return substantive_discussion_digest(observation.thread, authenticated_controller_login)

    def _validated_substantive_digest(claimed_thread: ClaimedReviewThread, observation: Any) -> Optional[str]:
        """Normalize an authenticated receipt only after proving raw freshness."""
        if not claimed_thread.discussion_evidence_digest:
            return None
        try:
            substantive = _discussion_digest(observation, observation.authenticated_login)
            complete = _discussion_digest(observation, "")
            if claimed_thread.discussion_evidence_digest in (substantive, complete):
                return substantive
        except Exception as exc:
            logger.error(f"Could not verify discussion evidence for thread {claimed_thread.thread_id}: {exc}")
        return None

    def _head_is_still_current() -> bool:
        try:
            current_head = github_client.get_pull_request_head_sha_strict(repo_name, pr_number)
        except Exception as exc:
            logger.error(f"Could not verify current PR head on PR #{pr_number}: {exc}")
            return False
        return bool(validated_head_sha and current_head == validated_head_sha)

    if not _head_is_still_current():
        logger.warning(f"PR #{pr_number} head changed or unreachable before closure persistence; aborting resolution")
        return ClosureExecutionResult(
            accepted_closures=tuple(e for e in evaluations if e.is_accepted),
            resolved_thread_ids=(),
            unresolved_thread_ids=tuple(e.thread_id for e in evaluations),
            persisted_blocker_transitions=(),
            errors=("PR head changed before closure persistence",),
        )

    # 1. Persist accepted closures to the ledger (REQ-008, AS-005)
    persisted_blockers: list[str] = []
    persisted_evaluations: list[ClosureEvaluationResult] = []
    errors: list[str] = []

    current_snapshot: Optional[BlockerLedgerSnapshot] = None
    if ledger is not None:
        try:
            current_snapshot = ledger.get_snapshot(norm_origin, repo_name, pr_number, require_retained_state=True)
        except Exception as exc:
            logger.error(f"Could not load ledger snapshot for PR #{pr_number}: {exc}")
            return ClosureExecutionResult(
                accepted_closures=(),
                resolved_thread_ids=(),
                unresolved_thread_ids=tuple(e.thread_id for e in evaluations),
                persisted_blocker_transitions=(),
                errors=(f"Ledger unavailable: {exc}",),
            )

    # Fence canonical acceptance itself against discussion advancement.  This
    # read is intentionally before ledger persistence, not merely before the
    # later GitHub mutation.
    current_evaluations: list[ClosureEvaluationResult] = []
    validated_discussion_digests: dict[str, str] = {}
    curr_rev = current_snapshot.ledger_revision if current_snapshot is not None else expected_ledger_revision or 0

    def _revoke_prior_acceptance(evaluation: ClosureEvaluationResult) -> None:
        nonlocal current_snapshot, curr_rev
        blocker_id = evaluation.evaluated_blocker_id
        if ledger is None or current_snapshot is None or not blocker_id:
            return
        existing = current_snapshot.get_blocker(blocker_id)
        if existing is None or existing.disposition is not BlockerDisposition.VERIFIED_CORRECTION:
            return
        current_snapshot = ledger.record_transition(
            api_origin=norm_origin,
            repository=repo_name,
            pr_number=pr_number,
            operation_id=f"discussion_stale_{blocker_id}_{uuid.uuid4().hex}",
            expected_ledger_revision=curr_rev,
            blocker_id=blocker_id,
            target_disposition=BlockerDisposition.OPEN,
            evidence="Authoritative review-thread discussion is not the validated evidence",
            transition_reason="Independent closure evidence requires reevaluation",
            reviewed_head_sha=validated_head_sha,
            reviewed_base_sha=evaluation.candidate.reviewed_base_sha,
            review_attempt_id=evaluation.candidate.review_attempt_id,
            requirement_manifest_revision=evaluation.candidate.requirement_manifest_revision,
            review_observation_identity=f"discussion_stale_{evaluation.thread_id}",
        )
        curr_rev = current_snapshot.ledger_revision

    for evaluation in evaluations:
        if not evaluation.is_accepted:
            current_evaluations.append(evaluation)
            continue
        claimed_thread = _find_claimed_thread(claimed_threads, evaluation.thread_id)
        if claimed_thread is None or claimed_thread.root_comment_database_id is None:
            errors.append(f"{evaluation.thread_id}|discussion-freshness|Validated discussion identity is unavailable")
            _revoke_prior_acceptance(evaluation)
            continue
        try:
            observation = observe_closure_receipt(
                github_client,
                repo_name,
                pr_number,
                evaluation.thread_id,
                claimed_thread.root_comment_database_id,
                api_origin=norm_origin,
            )
        except Exception as exc:
            errors.append(f"{evaluation.thread_id}|discussion-freshness|Authoritative discussion is unavailable: {exc}")
            _revoke_prior_acceptance(evaluation)
            continue
        current_digest = _validated_substantive_digest(claimed_thread, observation)
        if current_digest is None:
            errors.append(f"{evaluation.thread_id}|discussion-freshness|Discussion changed since independent validation")
            _revoke_prior_acceptance(evaluation)
            continue
        validated_discussion_digests[evaluation.thread_id] = current_digest
        current_evaluations.append(evaluation)

    evaluations = tuple(current_evaluations)

    for eval_res in evaluations:
        if not eval_res.is_accepted:
            continue

        if ledger is not None and current_snapshot is not None and eval_res.evaluated_blocker_id:
            blocker_id = eval_res.evaluated_blocker_id
            cand = eval_res.candidate
            op_id = f"closure_{blocker_id}_{validated_head_sha[:8]}_{cand.review_attempt_id or uuid.uuid4().hex[:6]}"

            # AS-005: Check if ledger revision is stale
            if expected_ledger_revision is not None and curr_rev < expected_ledger_revision:
                logger.warning(f"Stale ledger revision {curr_rev} < expected {expected_ledger_revision}; suppressing closure")
                errors.append(f"Stale ledger revision for {blocker_id}")
                continue

            existing = current_snapshot.get_blocker(blocker_id)
            if existing is not None and existing.disposition == BlockerDisposition.AUTHORIZED_INVALIDATION:
                # The accepted lifecycle already closed this finding as INVALID; the thread
                # can complete without rewriting that authority as a verified correction.
                persisted_blockers.append(blocker_id)
                persisted_evaluations.append(eval_res)
                continue

            try:
                current_snapshot = ledger.record_transition(
                    api_origin=norm_origin,
                    repository=repo_name,
                    pr_number=pr_number,
                    operation_id=op_id,
                    expected_ledger_revision=curr_rev,
                    blocker_id=blocker_id,
                    target_disposition=BlockerDisposition.VERIFIED_CORRECTION,
                    evidence=eval_res.evidence,
                    transition_reason=eval_res.rationale or "Validated independent correction on current head",
                    reviewed_head_sha=validated_head_sha,
                    reviewed_base_sha=cand.reviewed_base_sha,
                    review_attempt_id=cand.review_attempt_id,
                    requirement_manifest_revision=cand.requirement_manifest_revision,
                    review_observation_identity=f"closure_{blocker_id}",
                )
                curr_rev = current_snapshot.ledger_revision
                persisted_blockers.append(blocker_id)
                persisted_evaluations.append(eval_res)
            except StaleLedgerRevisionError as exc:
                # AS-005: Stale revision rejects write; cannot overwrite state or resolve thread
                logger.warning(f"Closure for blocker {blocker_id} rejected due to stale ledger revision: {exc}")
                errors.append(str(exc))
                continue
            except Exception as exc:
                # AS-005: Separately fail durable acceptance before GitHub mutation and assert no resolution request
                logger.error(f"Durable acceptance failed for blocker {blocker_id} on PR #{pr_number}: {exc}")
                errors.append(str(exc))
                continue
        else:
            # Without ledger (e.g. lightweight unit tests or unrooted threads), treat as accepted
            persisted_evaluations.append(eval_res)

    # 2. Multi-owner Root & Compound Root Thread Gating (REQ-006, AS-004)
    # A thread may only be resolved on GitHub if ALL of its owned obligations have valid current closure.
    eligible_thread_ids: list[str] = []
    unresolved_thread_ids: list[str] = []

    # Map candidate threads to their evaluations
    threads_with_accepted_closures = {e.thread_id for e in persisted_evaluations}

    for thread_id in threads_with_accepted_closures:
        if current_snapshot is not None:
            # Find all blockers owned by this thread or its root comment
            thread_cand = next((e.candidate for e in persisted_evaluations if e.thread_id == thread_id), None)
            root_id_str = str(thread_cand.root_comment_database_id) if thread_cand and thread_cand.root_comment_database_id else None

            matching_blockers: list[BlockerSnapshot] = []
            for b in current_snapshot.blockers:
                is_owned = any((a.alias_type in ("github_root_comment", "root_comment_id", "historical_root_comment_id") and a.alias_value == root_id_str) or (a.alias_type == "github_thread" and a.alias_value == thread_id) for a in b.aliases)
                if is_owned:
                    matching_blockers.append(b)

            if matching_blockers:
                all_closed = all(b.disposition in (BlockerDisposition.VERIFIED_CORRECTION, BlockerDisposition.AUTHORIZED_INVALIDATION) for b in matching_blockers)
                if all_closed:
                    eligible_thread_ids.append(thread_id)
                else:
                    unresolved_thread_ids.append(thread_id)
                    open_ids = [b.blocker_id for b in matching_blockers if b.disposition not in (BlockerDisposition.VERIFIED_CORRECTION, BlockerDisposition.AUTHORIZED_INVALIDATION)]
                    logger.info(f"Thread {thread_id} owns open blocker(s) {open_ids}; compound root remains unresolved")
            else:
                eligible_thread_ids.append(thread_id)
        else:
            # Fallback without ledger: accepted evaluation controls
            eligible_thread_ids.append(thread_id)

    # 3. Perform GitHub resolve mutations for eligible threads (REQ-008, AS-005)
    resolved_thread_ids: list[str] = []

    for thread_id in eligible_thread_ids:
        try:
            pending_recovery = set(rollback_registry.pending_for_pr(repo_name, pr_number)) | set(rollback_registry.marker_cleanup_pending_for_pr(repo_name, pr_number)) | set(rollback_registry.rollback_transitions_for_pr(repo_name, pr_number))
        except Exception as exc:
            errors.append(f"{thread_id}|receipt-reconciliation|Could not inspect prior rollback recovery: {exc}")
            continue
        if thread_id in pending_recovery:
            # A stable operation key is reused across same-head cycles. Finish
            # old durable cleanup before re-arming it so historical proof can
            # never retire the risk from a newer cycle of the same operation.
            errors.append(f"{thread_id}|receipt-reconciliation|Prior rollback recovery must complete before another closure")
            continue
        claimed_thread = _find_claimed_thread(claimed_threads, thread_id)
        if claimed_thread is None:
            logger.warning(f"Thread {thread_id} was not claimed for this run; ignoring")
            errors.append(f"{thread_id}|claim-identity|Thread was not claimed for this validation invocation")
            continue
        if claimed_thread.root_comment_database_id is None:
            logger.error(f"Cannot record resolver explanation for thread {thread_id}: no root comment ID available")
            errors.append(f"{thread_id}|root-identity|Root comment identity is unavailable")
            continue

        owned_evaluations = tuple(e for e in persisted_evaluations if e.thread_id == thread_id)
        matching_eval = owned_evaluations[0]
        pending_receipt = ClosureReceipt(
            api_origin=norm_origin,
            repository=repo_name,
            pr_number=pr_number,
            thread_id=thread_id,
            root_comment_database_id=claimed_thread.root_comment_database_id,
            operation_key=_closure_operation_key(owned_evaluations),
            validated_head_sha=validated_head_sha,
            phase="PENDING",
            rationale=matching_eval.rationale,
            evidence=matching_eval.evidence,
        )
        try:
            observation = observe_closure_receipt(
                github_client,
                repo_name,
                pr_number,
                thread_id,
                claimed_thread.root_comment_database_id,
                api_origin=norm_origin,
            )
        except Exception as exc:
            logger.error(f"Could not reconcile closure receipt for thread {thread_id}: {exc}")
            errors.append(f"{thread_id}|receipt-reconciliation|{exc}")
            continue

        receipt = observation.receipt
        if receipt is not None and receipt.phase == "ROLLBACK_REQUIRED":
            errors.append(f"{thread_id}|receipt-reconciliation|Publication-failure rollback must complete before another closure")
            continue
        same_operation = receipt is not None and receipt.operation_key == pending_receipt.operation_key and receipt.validated_head_sha == validated_head_sha
        if receipt is not None and receipt.phase == "PENDING" and not same_operation:
            if observation.thread.is_resolved:
                errors.append(f"{thread_id}|receipt-reconciliation|An older pending resolution requires rollback reconciliation")
                continue
            try:
                observation = put_receipt(github_client, observation, replace(receipt, phase="ROLLED_BACK"))
            except Exception as exc:
                errors.append(f"{thread_id}|receipt-reconciliation|Could not retire the confirmed previous rollback: {exc}")
                continue

        # A fresh independent decision and an exact remote observation, not the
        # receipt alone, allow an accepted-but-unacknowledged effect to settle.
        if same_operation and observation.thread.is_resolved and receipt is not None and receipt.phase in ("PENDING", "RESOLVED"):
            if not _head_is_still_current():
                errors.append(f"{thread_id}|head-verification|PR head changed while reconciling the confirmed resolution")
                continue
            if receipt.phase == "PENDING":
                try:
                    put_receipt(github_client, observation, replace(receipt, phase="RESOLVED"))
                except Exception as exc:
                    errors.append(f"{thread_id}|marker-cleanup|{exc}")
            resolved_thread_ids.append(thread_id)
            continue

        # Every actual resolve gets a currently observable pending guard. A
        # completed receipt from an earlier cycle is deliberately re-armed.
        try:
            observation = put_receipt(github_client, observation, pending_receipt)
        except Exception as exc:
            logger.error(f"Could not durably record pending resolution for thread {thread_id}: {exc}")
            errors.append(f"{thread_id}|intent-publication|{exc}")
            continue

        observed_discussion_digest: Optional[str] = None
        expected_discussion_digest = validated_discussion_digests.get(thread_id)
        try:
            observed_discussion_digest = _discussion_digest(observation, observation.authenticated_login)
            discussion_is_current = observed_discussion_digest == expected_discussion_digest
        except Exception as exc:
            logger.error(f"Could not verify discussion evidence for thread {thread_id}: {exc}")
            discussion_is_current = False
        if not discussion_is_current:
            logger.warning(f"Discussion evidence changed before resolving {thread_id}: " f"expected {expected_discussion_digest}, observed {observed_discussion_digest}")
            errors.append(f"{thread_id}|discussion-freshness|Discussion changed immediately before resolve mutation")
            # The pre-effect acceptance is no longer current.  Compensate the
            # canonical transition before returning so neither the ledger nor
            # the typed outcome can advertise the stale evaluation as accepted.
            if ledger is not None and current_snapshot is not None:
                for stale_evaluation in owned_evaluations:
                    stale_blocker_id = stale_evaluation.evaluated_blocker_id
                    if not stale_blocker_id:
                        continue
                    try:
                        current_snapshot = ledger.record_transition(
                            api_origin=norm_origin,
                            repository=repo_name,
                            pr_number=pr_number,
                            operation_id=f"discussion_stale_{stale_blocker_id}_{uuid.uuid4().hex}",
                            expected_ledger_revision=curr_rev,
                            blocker_id=stale_blocker_id,
                            target_disposition=BlockerDisposition.OPEN,
                            evidence="Authoritative review-thread discussion changed after validation",
                            transition_reason="Independent closure evidence is stale and requires reevaluation",
                            reviewed_head_sha=validated_head_sha,
                            reviewed_base_sha=stale_evaluation.candidate.reviewed_base_sha,
                            review_attempt_id=stale_evaluation.candidate.review_attempt_id,
                            requirement_manifest_revision=stale_evaluation.candidate.requirement_manifest_revision,
                            review_observation_identity=f"discussion_stale_{thread_id}",
                        )
                        curr_rev = current_snapshot.ledger_revision
                        if stale_blocker_id in persisted_blockers:
                            persisted_blockers.remove(stale_blocker_id)
                    except Exception as exc:
                        errors.append(f"{thread_id}|discussion-freshness|Could not revoke stale canonical acceptance: {exc}")
                persisted_evaluations = [item for item in persisted_evaluations if item.thread_id != thread_id]
            try:
                put_receipt(github_client, observation, replace(pending_receipt, phase="ROLLED_BACK"))
            except Exception as exc:
                errors.append(f"{thread_id}|marker-cleanup|{exc}")
            continue

        if not _head_is_still_current():
            logger.warning(f"PR head changed before resolve mutation on thread {thread_id}")
            errors.append(f"{thread_id}|head-verification|PR head changed before resolve mutation")
            return ClosureExecutionResult(
                accepted_closures=tuple(persisted_evaluations),
                resolved_thread_ids=tuple(resolved_thread_ids),
                unresolved_thread_ids=tuple(unresolved_thread_ids),
                persisted_blocker_transitions=tuple(persisted_blockers),
                errors=tuple(errors),
            )

        # Resolve mutation
        try:
            github_client.resolve_review_thread(thread_id)
        except Exception as exc:
            logger.error(f"Failed to resolve review thread {thread_id}: {exc}")
            errors.append(f"{thread_id}|resolve-confirmation|Resolve delivery or confirmation failed: {exc}")
            continue

        # Post-mutation staleness recheck (REQ-006, REQ-008)
        if not _head_is_still_current():
            try:
                rollback_registry.record_rollback_transition(
                    repo_name,
                    pr_number,
                    thread_id,
                    claimed_thread.root_comment_database_id,
                    receipt_operation_key=pending_receipt.operation_key,
                )
            except Exception as transition_exc:
                logger.error(f"Could not record rollback transition for thread {thread_id}: {transition_exc}")
                raise StaleReviewThreadResolutionError(thread_id, repo_name, pr_number) from transition_exc

            last_exc: Optional[Exception] = None
            for attempt in range(1, UNRESOLVE_ROLLBACK_MAX_ATTEMPTS + 1):
                try:
                    github_client.unresolve_review_thread(thread_id)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.error(f"Attempt {attempt}/{UNRESOLVE_ROLLBACK_MAX_ATTEMPTS} to unresolve thread {thread_id} failed: {exc}")

            if last_exc is not None:
                try:
                    rollback_registry.record(repo_name, pr_number, thread_id)
                except Exception:
                    pass
                raise StaleReviewThreadResolutionError(thread_id, repo_name, pr_number) from last_exc

            # Rollback succeeded: retire risk in the same owned reply.
            if claimed_thread.root_comment_database_id is not None:
                try:
                    put_receipt(github_client, observation, replace(pending_receipt, phase="ROLLED_BACK"))
                except Exception as exc:
                    logger.error(f"Rolled back thread {thread_id} but failed to update its receipt: {exc}")
                    errors.append(f"{thread_id}|marker-cleanup|{exc}")
                    try:
                        rollback_registry.record_marker_cleanup(repo_name, pr_number, thread_id, claimed_thread.root_comment_database_id, receipt_operation_key=pending_receipt.operation_key)
                    except Exception:
                        pass
                else:
                    try:
                        rollback_registry.clear(repo_name, thread_id)
                    except Exception:
                        pass

            return ClosureExecutionResult(
                accepted_closures=tuple(persisted_evaluations),
                resolved_thread_ids=tuple(resolved_thread_ids),
                unresolved_thread_ids=tuple(unresolved_thread_ids),
                persisted_blocker_transitions=tuple(persisted_blockers),
                errors=tuple(errors),
            )

        # Success: update the existing reply instead of appending housekeeping.
        if claimed_thread.root_comment_database_id is not None:
            try:
                put_receipt(github_client, observation, replace(pending_receipt, phase="RESOLVED"))
            except Exception as exc:
                logger.error(f"Resolved thread {thread_id} but failed to complete its receipt: {exc}")
                errors.append(f"{thread_id}|marker-cleanup|{exc}")

        resolved_thread_ids.append(thread_id)

    selected_ids = tuple(dict.fromkeys(e.thread_id for e in evaluations if e.thread_id))
    return ClosureExecutionResult(
        accepted_closures=tuple(persisted_evaluations),
        resolved_thread_ids=tuple(resolved_thread_ids),
        unresolved_thread_ids=tuple(thread_id for thread_id in selected_ids if thread_id not in resolved_thread_ids),
        persisted_blocker_transitions=tuple(persisted_blockers),
        errors=tuple(errors),
    )
