"""Application of the effective review decision at the shared PR-processing boundary.

``effective_review_decision`` derives a side-effect-free decision.  This module
owns the production consequences of that decision that must not live inside the
policy: folding accepted obligations into the result that publication and repair
consume, retaining the decision and its outstanding work durably, and the
pre-send authority that refuses a native APPROVE the current accepted state no
longer supports.

It reuses the owning stores: the accepted-finding bridge (identity, lifecycle
state, association), the validation-attempt repository (applicability ordering)
and the review/repair transports.  It adds only the small durable record that
lets later normal processing resume the same outstanding work.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, ContextManager, Iterator, Optional, Sequence

from .accepted_finding_bridge import (
    AMBIGUOUS,
    ASSOCIATED,
    BINDING_CURRENT,
    CATEGORY_IMPLEMENTATION,
    CATEGORY_REGRESSION_GAP,
    NOT_PUBLISHED,
    OUTCOME_STILL_VALID_OBSERVED,
    AcceptedFindingBridge,
    AcceptedFindingProjection,
    AcceptedFindingRecord,
    ProjectionTarget,
    known_gap_from_record,
)
from .adversarial_validation_attempts import AdversarialValidationAttemptRepository
from .adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult
from .canonical_pr_blocker_ledger import BlockerDisposition
from .effective_review_decision import (
    EffectiveNextAction,
    EffectiveReviewDecision,
    derive_effective_review_decision,
)
from .logger_config import get_logger
from .ordinary_closure_evidence import deserialize_ordinary_result, serialize_ordinary_result
from .pr_review_cycle import OPEN
from .pr_review_execution import ReviewExecutionInput
from .runtime_locks import ensure_lock_directory, lock_path

logger = get_logger(__name__)

# Publication of the review that carries the decision.
PUBLICATION_PENDING = "PENDING"
PUBLICATION_CONFIRMED = "CONFIRMED"

# Corrective handoff of the decision.
HANDOFF_NOT_REQUIRED = "NOT_REQUIRED"
HANDOFF_PENDING = "PENDING"
HANDOFF_DISPATCHED = "DISPATCHED"
HANDOFF_WAITING = "WAITING"

# Reasons a handoff or reconciliation is retained rather than completed.
WAIT_ROUTE_UNAVAILABLE = "ROUTE_UNAVAILABLE"
WAIT_QUOTA = "QUOTA_DEFERRED"
WAIT_INDETERMINATE = "INDETERMINATE_DELIVERY"
WAIT_ALLOWANCE_EXHAUSTED = "ALLOWANCE_EXHAUSTED"
WAIT_ASSOCIATION_EVIDENCE = "WAITING_FOR_ASSOCIATION_EVIDENCE"
WAIT_RECONCILIATION = "RECONCILIATION_PENDING"


class ReviewDisposition(str, Enum):
    """What one processing invocation actually accomplished for the review.

    An enqueued repair, a blocked approval, a COMMENT or a raw PASS is none of
    ``REVIEW_VERIFIED`` or ``MERGE_COMPLETED``; the distinct values keep
    aggregate reports from collapsing them into success.
    """

    CORRECTIVE_HANDOFF = "CORRECTIVE_HANDOFF"
    CORRECTION_WAITING = "CORRECTION_WAITING"
    RECONCILIATION_WAIT = "RECONCILIATION_WAIT"
    OPERATIONAL_FAILURE = "OPERATIONAL_FAILURE"
    REVIEW_VERIFIED = "REVIEW_VERIFIED"
    MERGE_COMPLETED = "MERGE_COMPLETED"


class DecisionRetentionError(RuntimeError):
    """The decision could not be durably retained; dependent effects must not run."""


@dataclass(frozen=True)
class OrdinaryCorrectionEvidence:
    """Normalized ordinary evidence and the controller authority evaluated by its attempt.

    This is part of the effective-decision record, independent of the optional
    closure extension. It confers neither closure nor approval and is usable
    only after its binding has been checked against the current owning stores.
    """

    version: int = 1
    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    contract_identity: str = ""
    policy_identity: str = ""
    open_epoch: int = 0
    attempt_id: str = ""
    attempt_sequence: int = 0
    strong_round_id: str = ""
    audited_head_sha: str = ""
    finding_set_revision: int = 0
    requires_strong_policy: bool = False
    accepted_sources: tuple[str, ...] = ()
    payload: str = ""

    def restore(self) -> AdversarialValidationResult:
        if self.version != 1 or not self.payload:
            raise DecisionRetentionError("Ordinary correction evidence has an unsupported or missing payload")
        try:
            result = deserialize_ordinary_result(self.payload)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DecisionRetentionError(f"Ordinary correction evidence is unreadable: {exc}") from exc
        if (result.attempt_id, result.attempt_sequence) != (self.attempt_id, self.attempt_sequence):
            raise DecisionRetentionError("Ordinary correction evidence does not match its producing attempt")
        return result


def retain_ordinary_correction(result: AdversarialValidationResult, projection: AcceptedFindingProjection, closure_input: Optional[ReviewExecutionInput] = None, authority: Optional[OrdinaryCorrectionEvidence] = None) -> OrdinaryCorrectionEvidence:
    """Capture the evaluated binding without relying on a returned closure assessment."""
    target = projection.target
    records = projection.records
    rounds = {record.round_id for record in records}
    contracts = {record.contract_identity for record in records}
    policies = {record.policy_identity for record in records}
    audited_heads = {record.originating_head_sha for record in records}
    if authority is not None:
        if (authority.repository, authority.pr_number, authority.head_sha, authority.base_sha, authority.attempt_id, authority.attempt_sequence, authority.open_epoch, authority.finding_set_revision) != (
            target.repository,
            target.pr_number,
            target.head_sha,
            target.base_sha,
            result.attempt_id,
            result.attempt_sequence,
            projection.open_epoch,
            projection.finding_set_revision,
        ):
            raise DecisionRetentionError("Captured ordinary authority differs from the evaluated context")
        return replace(authority, accepted_sources=tuple(sorted(record.source_identity for record in records)), payload=serialize_ordinary_result(result, include_closure=True))
    if closure_input is not None and (closure_input.repository, closure_input.pr_number, closure_input.head_sha, closure_input.base_sha, closure_input.attempt_id, closure_input.attempt_sequence) != (
        target.repository,
        target.pr_number,
        target.head_sha,
        target.base_sha,
        result.attempt_id,
        result.attempt_sequence,
    ):
        raise DecisionRetentionError("Captured ordinary context does not match the evaluated target and attempt")
    return OrdinaryCorrectionEvidence(
        repository=target.repository,
        pr_number=target.pr_number,
        head_sha=target.head_sha,
        base_sha=target.base_sha,
        contract_identity=closure_input.contract.identity if closure_input else target.contract_identity or (next(iter(contracts)) if len(contracts) == 1 else ""),
        policy_identity=closure_input.policy.identity if closure_input else target.policy_identity or (next(iter(policies)) if len(policies) == 1 else ""),
        open_epoch=closure_input.open_epoch if closure_input else projection.open_epoch,
        attempt_id=result.attempt_id,
        attempt_sequence=result.attempt_sequence,
        strong_round_id=closure_input.round_id if closure_input else (next(iter(rounds)) if len(rounds) == 1 else ""),
        audited_head_sha=closure_input.audited_head_sha if closure_input else (next(iter(audited_heads)) if len(audited_heads) == 1 else ""),
        finding_set_revision=closure_input.finding_set_revision if closure_input else projection.finding_set_revision,
        requires_strong_policy=closure_input is not None,
        accepted_sources=tuple(sorted(record.source_identity for record in records)),
        payload=serialize_ordinary_result(result, include_closure=True),
    )


@dataclass
class RetainedDecision:
    """The durable record of one accepted effective decision and its open work."""

    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    status: str = ""
    next_action: str = ""
    blocker_ids: list[str] = field(default_factory=list)
    source_identities: list[str] = field(default_factory=list)
    pending_closure_source_identities: list[str] = field(default_factory=list)
    pending_reconciliation_source_identities: list[str] = field(default_factory=list)
    ordinary_correction: Optional[OrdinaryCorrectionEvidence] = None
    attempt_id: str = ""
    attempt_sequence: int = 0
    source_revision: int = -1
    finding_set_revision: int = 0
    association_revision: int = 0
    reopen_epoch: int = 0
    evidence_revision: str = ""
    publication: str = PUBLICATION_PENDING
    handoff: str = HANDOFF_NOT_REQUIRED
    wait_reason: str = ""
    # Count of confirmed corrective deliveries and the provider observation taken
    # when the latest one was confirmed; together they identify "the request has
    # since completed" without trusting an implementer's claim.
    handoff_generation: int = 0
    handoff_observation: str = ""
    # Evidence revisions for which the one focused reconciliation attempt was spent.
    reconciliation_attempts: list[str] = field(default_factory=list)
    # Closure-acceptance attempts already spent, keyed by target/lifecycle revision.
    closure_attempts: list[str] = field(default_factory=list)
    updated_at: float = 0.0

    @property
    def requires_handoff(self) -> bool:
        return self.next_action in {EffectiveNextAction.IMPLEMENTATION_REPAIR.value, EffectiveNextAction.FOCUSED_TEST_REPAIR.value}

    @property
    def is_reconciliation_wait(self) -> bool:
        return self.next_action == EffectiveNextAction.RECONCILIATION.value and self.evidence_revision in self.reconciliation_attempts


class EffectiveDecisionStore:
    """Durable per-PR retention of the latest accepted effective decision."""

    def __init__(self, repo_name: str, storage_path: Optional[Path] = None):
        self.storage_path = storage_path or Path.home() / ".auto-coder" / repo_name / "effective_review_decisions.json"
        self.lock_path = lock_path(repo_name, self.storage_path, "effective-decision-store")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_lock_directory(self.lock_path)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, dict[str, object]]:
        if not self.storage_path.exists():
            return {}
        with self.storage_path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict):
            raise DecisionRetentionError("Effective-decision state is invalid")
        return {str(key): value for key, value in state.items() if isinstance(value, dict)}

    def _write(self, state: dict[str, dict[str, object]]) -> None:
        temporary = self.storage_path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.storage_path)

    @staticmethod
    def _decode(raw: dict[str, object]) -> RetainedDecision:
        known = {name for name in RetainedDecision.__dataclass_fields__}
        values = {key: value for key, value in raw.items() if key in known}
        ordinary = values.get("ordinary_correction")
        if ordinary is not None:
            if not isinstance(ordinary, dict):
                raise DecisionRetentionError("Retained ordinary correction is not an evidence object")
            try:
                evidence = dict(ordinary)
                evidence["accepted_sources"] = tuple(evidence.get("accepted_sources", ()))
                values["ordinary_correction"] = OrdinaryCorrectionEvidence(**evidence)
            except (TypeError, ValueError) as exc:
                raise DecisionRetentionError(f"Retained ordinary correction is invalid: {exc}") from exc
        return RetainedDecision(**values)  # type: ignore[arg-type]

    def load(self, pr_number: int) -> Optional[RetainedDecision]:
        """Return the retained decision; an unreadable store raises instead of reading as empty."""
        try:
            with self._locked():
                raw = self._read().get(str(pr_number))
        except DecisionRetentionError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise DecisionRetentionError(f"Effective-decision state is unreadable: {exc}") from exc
        return self._decode(raw) if raw is not None else None

    def save(self, record: RetainedDecision) -> RetainedDecision:
        """Durably write one record; failure is reported so dependent effects are suppressed."""
        stamped = replace(record, updated_at=time.time())
        try:
            with self._locked():
                state = self._read()
                state[str(record.pr_number)] = asdict(stamped)
                self._write(state)
        except DecisionRetentionError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise DecisionRetentionError(f"Effective decision could not be retained: {exc}") from exc
        return stamped

    def retain(self, record: RetainedDecision) -> RetainedDecision:
        """Retain a newly accepted decision without erasing spent target-bound attempts.

        Reconciliation keys already include their target, so historical keys
        survive target changes without spending the new target's allowance.
        Typed ordinary delivery receipts and resolved blocker identities survive
        re-derivation only for the same producing evidence and correction set.
        """
        previous = self.load(record.pr_number)
        waiting_actions = {EffectiveNextAction.RECONCILIATION.value, EffectiveNextAction.CLOSURE_ACCEPTANCE.value}
        attempts = list(previous.reconciliation_attempts) if previous is not None else []
        for key in record.reconciliation_attempts:
            if key not in attempts:
                attempts.append(key)
        if record.next_action in waiting_actions and record.evidence_revision not in attempts:
            attempts.append(record.evidence_revision)
        carried = replace(record, reconciliation_attempts=attempts)
        if previous is not None and (previous.head_sha, previous.base_sha) == (record.head_sha, record.base_sha):
            same_producer = previous.ordinary_correction == record.ordinary_correction and (previous.attempt_id, previous.attempt_sequence) == (record.attempt_id, record.attempt_sequence)
            same_ordinary = record.ordinary_correction is not None and same_producer and previous.next_action == record.next_action and previous.status == record.status and previous.source_identities == record.source_identities and set(record.blocker_ids).issubset(previous.blocker_ids)
            legacy_continuity = previous.ordinary_correction is None and record.ordinary_correction is None
            same_delivery = same_ordinary or (legacy_continuity and previous.blocker_ids == record.blocker_ids)
            carried = replace(
                carried,
                blocker_ids=list(previous.blocker_ids) if same_ordinary else list(record.blocker_ids),
                closure_attempts=list(previous.closure_attempts),
                # Generations are historical delivery accounting, not a receipt
                # for the newly derived payload. Resetting them would reuse a
                # spent closure key after a later confirmed corrective request.
                handoff_generation=previous.handoff_generation,
                handoff_observation=previous.handoff_observation if same_ordinary or legacy_continuity else record.handoff_observation,
                handoff=previous.handoff if same_delivery and previous.handoff == HANDOFF_DISPATCHED and record.requires_handoff else record.handoff,
                publication=previous.publication if same_producer else record.publication,
            )
        return self.save(carried)

    def update(self, pr_number: int, **changes: object) -> RetainedDecision:
        previous = self.load(pr_number)
        if previous is None:
            raise DecisionRetentionError(f"No retained effective decision exists for PR #{pr_number}")
        return self.save(replace(previous, **changes))  # type: ignore[arg-type]

    def record_closure_attempt(self, pr_number: int, key: str, head_sha: str = "") -> bool:
        """Spend a closure attempt once per key; returns False when it was already spent.

        A PR with no retained decision yet gets a decision-less record that only
        carries its spent attempts, so a first corrective generation is still
        assessed exactly once.
        """
        previous = self.load(pr_number) or RetainedDecision(pr_number=pr_number, head_sha=head_sha)
        if key in previous.closure_attempts:
            return False
        self.save(replace(previous, closure_attempts=[*previous.closure_attempts, key]))
        return True

    def spend_reconciliation(self, pr_number: int, key: str, head_sha: str, base_sha: str) -> bool:
        """Atomically admit one legacy recovery before its reviewer can be invoked.

        The key binds the evaluated target and evidence. An older envelope must
        neither suppress a new target's admission nor have its old binding
        rewritten to imply that its saved evidence applies to that new target.
        """
        try:
            with self._locked():
                state = self._read()
                raw = state.get(str(pr_number))
                record = self._decode(raw) if raw is not None else RetainedDecision(pr_number=pr_number, head_sha=head_sha, base_sha=base_sha)
                if key in record.reconciliation_attempts:
                    return False
                state[str(pr_number)] = asdict(replace(record, reconciliation_attempts=[*record.reconciliation_attempts, key], updated_at=time.time()))
                self._write(state)
                return True
        except DecisionRetentionError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise DecisionRetentionError(f"Legacy ordinary recovery admission could not be retained: {exc}") from exc


def evidence_revision(projection: AcceptedFindingProjection) -> str:
    """Identity of the authority/association evidence a reconciliation depends on.

    Storage/CAS counters, publication receipts and repeated observations are
    deliberately absent. Only relevant accepted-source, availability and exact
    association facts can renew a spent attempt. Target changes remain visible.
    """
    payload = {
        "complete": projection.complete,
        "target": {
            **asdict(projection.target),
            "contract_identity": [projection.target.contract_identity] if projection.target.contract_identity else sorted({record.contract_identity for record in projection.records}),
            "policy_identity": [projection.target.policy_identity] if projection.target.policy_identity else sorted({record.policy_identity for record in projection.records}),
        },
        "finding_set_revision": projection.finding_set_revision,
        "open_epoch": projection.open_epoch,
        "source_available": projection.source_revision >= 0,
        "diagnostics": sorted((diagnostic.code, diagnostic.source_identity) for diagnostic in projection.diagnostics),
        "records": sorted(
            (record.source_identity, record.accepted_state, record.association, record.canonical_blocker_id, record.target_binding, tuple(sorted(record.root_comment_ids)), record.contract_identity, record.policy_identity, record.category, record.ledger_disposition, record.closure_head_sha)
            for record in projection.records
        ),
    }
    if not projection.complete and closure_projection_readable(projection):
        # Older consumers spent reconciliation without a reachable independent
        # closure path. Admit that path once, preserving its normal attempt budget.
        payload["closure_recovery_protocol"] = "ledger-ahead-v1"
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]


def outstanding_records(projection: AcceptedFindingProjection) -> tuple[AcceptedFindingRecord, ...]:
    """Accepted findings whose obligation has not been closed by the lifecycle owner."""
    return tuple(record for record in projection.records if record.accepted_state == OPEN)


def saved_pass_is_clearance(projection: AcceptedFindingProjection) -> bool:
    """A saved PASS clears only when the accepted state is readable and fully closed for this target."""
    if not projection.complete:
        return False
    return all(record.accepted_state != OPEN and record.target_binding == BINDING_CURRENT for record in projection.records)


def _finding_from_record(record: AcceptedFindingRecord) -> AdversarialValidationFinding:
    qualified = [f"#{item.issue_number}/{item.requirement_id}" for item in record.qualified_requirements] or list(record.requirement_ids)
    return AdversarialValidationFinding(
        requirement_id=qualified[0] if qualified else "",
        requirement_ids=qualified[1:],
        finding_identity=record.source_identity,
        correction_identity=record.canonical_blocker_id,
        violated_requirement=record.requirement_texts[0] if record.requirement_texts else "",
        requirement_text=record.requirement_texts[0] if record.requirement_texts else "",
        reachability=record.affected_boundary,
        required_behavior=record.original_scope,
        actual_behavior=record.original_scope,
        evidence=record.evidence,
        counterexample=record.original_scope,
        anchor_path="",
    )


def apply_effective_decision(
    result: AdversarialValidationResult,
    decision: EffectiveReviewDecision,
    projection: AcceptedFindingProjection,
) -> AdversarialValidationResult:
    """Return the result publication and repair must consume for this decision.

    The input is not mutated, so a caller can re-derive from the same raw
    evidence after a refusal.  Accepted obligations are folded in under their
    existing identities (the TOG alias for a regression gap, the source alias for
    an implementation finding), so reconciliation references the existing root
    instead of creating a duplicate.  The raw model verdict is retained only as
    labeled historical evidence.
    """
    raw = result.raw_model_result or result.result
    status = decision.status
    corrective = decision.next_action in {EffectiveNextAction.IMPLEMENTATION_REPAIR, EffectiveNextAction.FOCUSED_TEST_REPAIR}
    findings = list(decision.ordinary_findings) if corrective else list(result.findings)
    gaps = [replace(gap) for gap in (decision.ordinary_gaps if corrective else result.test_oracle_gaps)]
    if corrective:
        actionable_sources = {correction.source_identity for correction in decision.corrections}
        promoted_gap_ids = {record.known_gap_id for record in projection.records if record.source_identity in actionable_sources and record.category == CATEGORY_REGRESSION_GAP and record.known_gap_id}
        # Independent normalization may establish an accepted test deliverable
        # as an implementation violation. Preserve that current representation
        # under its original gap identity instead of synthesizing another gap.
        findings.extend(finding for finding in result.findings if promoted_gap_ids.intersection((finding.finding_identity, finding.correction_identity)) and finding not in findings)
    if decision.pending_closures or decision.pending_reconciliation:
        pending_sources = {item.source_identity for item in (*decision.pending_closures, *decision.pending_reconciliation)}
        pending_ids = {identity for record in projection.records if record.source_identity in pending_sources for identity in (record.source_identity, record.known_gap_id) if identity}
        findings = [finding for finding in findings if finding.finding_identity not in pending_ids]
        gaps = [gap for gap in gaps if gap.gap_id not in pending_ids]
    if status in {"NEEDS_FIX", "NEEDS_TESTS"} and decision.corrections:
        by_source = {record.source_identity: record for record in projection.records}
        known_finding_ids = {finding.finding_identity for finding in findings}
        for correction in decision.corrections:
            record = by_source.get(correction.source_identity)
            if record is None:
                continue
            if record.category == CATEGORY_REGRESSION_GAP and record.known_gap_id:
                if record.known_gap_id in {identity for finding in findings for identity in (finding.finding_identity, finding.correction_identity)}:
                    continue  # already carried as its promoted implementation finding
                gap = known_gap_from_record(record)
                gaps = [existing for existing in gaps if existing.gap_id != gap.gap_id]
                gaps.append(gap)
            elif record.category == CATEGORY_IMPLEMENTATION and record.source_identity not in known_finding_ids:
                findings.append(_finding_from_record(record))
                known_finding_ids.add(record.source_identity)
    summary = result.summary
    if status != raw.strip().upper() or decision.reasons:
        reasons = "; ".join(dict.fromkeys(decision.reasons))
        if reasons:
            summary = f"{summary}\n\nEffective decision {status}: {reasons}".strip()
    return replace(result, result=status, summary=summary, findings=findings, test_oracle_gaps=gaps, raw_model_result=raw)


@dataclass(frozen=True)
class DecisionApplication:
    """The decision, the result that carries it, and the projection it was derived from."""

    result: AdversarialValidationResult
    decision: EffectiveReviewDecision
    projection: AcceptedFindingProjection
    ordinary_result: Optional[AdversarialValidationResult] = None


def build_retained_record(decision: EffectiveReviewDecision, projection: AcceptedFindingProjection, head_sha: str, base_sha: str, ordinary_correction: Optional[OrdinaryCorrectionEvidence] = None) -> RetainedDecision:
    binding = decision.binding
    return RetainedDecision(
        repository=binding.target.repository,
        pr_number=binding.target.pr_number,
        head_sha=head_sha,
        base_sha=base_sha,
        status=decision.status,
        next_action=decision.next_action.value,
        blocker_ids=sorted(decision.blocker_ids),
        source_identities=sorted(correction.source_identity for correction in decision.corrections),
        pending_closure_source_identities=sorted(correction.source_identity for correction in decision.pending_closures),
        pending_reconciliation_source_identities=sorted(correction.source_identity for correction in decision.pending_reconciliation),
        ordinary_correction=ordinary_correction,
        attempt_id=binding.validation_attempt_id,
        attempt_sequence=binding.validation_attempt_sequence,
        source_revision=binding.source_revision,
        finding_set_revision=binding.finding_set_revision,
        association_revision=binding.association_revision,
        reopen_epoch=binding.reopen_epoch,
        evidence_revision=evidence_revision(projection),
        publication=PUBLICATION_PENDING,
        handoff=HANDOFF_PENDING if decision.next_action in {EffectiveNextAction.IMPLEMENTATION_REPAIR, EffectiveNextAction.FOCUSED_TEST_REPAIR} else HANDOFF_NOT_REQUIRED,
        wait_reason=WAIT_RECONCILIATION if decision.next_action is EffectiveNextAction.RECONCILIATION else "",
    )


def settle_accepted_gaps(result: AdversarialValidationResult, projection: AcceptedFindingProjection) -> AdversarialValidationResult:
    """Separate the ordinary session's own verdict from obligations the lifecycle owns.

    The ordinary validator keeps every accepted regression gap open in its
    result, so its raw verdict is NEEDS_TESTS whenever such a finding exists.
    When that is the *only* reason, the session itself found nothing else wrong:
    the accepted obligation is represented by the authoritative projection, and
    the derivation must judge it there (open, closed, or awaiting closure)
    instead of inheriting the validator's session-local copy.
    """
    accepted = {record.known_gap_id for record in projection.records if record.known_gap_id}
    open_gaps = [gap for gap in result.test_oracle_gaps if gap.status == OPEN]
    if not accepted or result.result.strip().upper() != "NEEDS_TESTS" or result.findings or not any(gap.gap_id in accepted for gap in open_gaps):
        return result
    if any(gap.gap_id not in accepted for gap in open_gaps):
        return result
    return replace(result, result="PASS", test_oracle_gaps=[gap for gap in result.test_oracle_gaps if gap.gap_id not in accepted], raw_model_result=result.raw_model_result or result.result)


def derive_application(result: AdversarialValidationResult, projection: AcceptedFindingProjection) -> DecisionApplication:
    """Derive the effective decision and the result that carries it (no side effects)."""
    settled = settle_accepted_gaps(result, projection)
    decision = derive_effective_review_decision(settled, projection)
    return DecisionApplication(apply_effective_decision(settled, decision, projection), decision, projection, result)


@dataclass(frozen=True)
class AcceptanceFence:
    """Orders a closure acceptance against validation-attempt registration."""

    attempts: AdversarialValidationAttemptRepository
    pr_number: int
    head_sha: str
    attempt_sequence: int
    # Reads the authoritative PR head so a closure accepted after external review
    # can reject a proposal the target has since superseded.
    current_head: Optional[Callable[[], str]] = None


class ApprovalAuthority:
    """Last pre-send check for a native APPROVE (a backstop, not the normalizer).

    Returns an empty string while approval remains authorized and otherwise the
    refusal reason.  It re-reads the accepted state, so a newer accepted finding,
    a pending closure, an incomplete association, an unreadable store, or a newer
    registered validation attempt prevents transmission.
    """

    def __init__(
        self,
        *,
        bridge: AcceptedFindingBridge,
        target: ProjectionTarget,
        observe_roots: Callable[[], object],
        raw_result: AdversarialValidationResult,
        decision: EffectiveReviewDecision,
        attempts: AdversarialValidationAttemptRepository,
        attempt_sequence: int,
        head_sha: str,
    ) -> None:
        self._bridge = bridge
        self._target = target
        self._observe_roots = observe_roots
        self._raw_result = raw_result
        self._decision = decision
        self._attempts = attempts
        self._attempt_sequence = attempt_sequence
        self._head_sha = head_sha

    def transition_fence(self) -> ContextManager[None]:
        """The accepted-state owner's transition lock; holding it orders acceptance against transmission."""
        return self._bridge.cycle.serialized_transition()

    def __call__(self) -> str:
        if self._attempt_sequence and self._attempts.latest_sequence(self._target.pr_number, self._head_sha) > self._attempt_sequence:
            return "A newer applicable validation attempt is registered; this decision no longer has approval authority"
        try:
            projection = self._bridge.project(self._target, self._observe_roots())  # type: ignore[arg-type]
        except Exception as exc:
            return f"Accepted-finding state is unavailable: {type(exc).__name__}"
        if not projection.complete:
            codes = ", ".join(diagnostic.code for diagnostic in projection.diagnostics)
            return "Accepted-finding state is incomplete: " + (codes or "native root association is unavailable")
        current = derive_effective_review_decision(settle_accepted_gaps(self._raw_result, projection), projection)
        if not current.approval_eligible:
            return f"Current accepted state is {current.status}/{current.next_action.value}: " + "; ".join(dict.fromkeys(current.reasons))
        original = self._decision.binding
        if current.binding.finding_set_revision != original.finding_set_revision:
            return "Accepted-finding set changed after the decision was derived"
        return ""


def raw_ordinary_clear(result: AdversarialValidationResult) -> bool:
    """Whether the ordinary session itself fully cleared requirement coverage.

    This is closure eligibility, deliberately separate from the effective
    decision: an accepted finding can keep the effective result nonpassing while
    the ordinary review of everything else is complete, and bounded closure
    must remain reachable in exactly that state.
    """
    return result.result.strip().upper() == "PASS" and bool(result.requirement_coverage) and all(entry.status in {"VERIFIED", "IRRELEVANT"} for entry in result.requirement_coverage) and not result.specification_gaps and not result.unexplained_changes


def closure_projection_readable(projection: AcceptedFindingProjection) -> bool:
    """Allow independent closure of a ledger-ahead finding, never approval reuse.

    Local correction verification may close a canonical blocker before the
    owning Strong lifecycle accepts closure. Its exact identity remains usable
    for independent reassessment; only that owner can close the obligation.
    Other disagreements, missing reads and ambiguous associations fail closed.
    """
    if projection.complete:
        return True
    if not projection.diagnostics or projection.source_revision < 0 or not projection.records:
        return False
    if any(not record.canonical_blocker_id or record.target_binding != BINDING_CURRENT or record.association not in {ASSOCIATED, NOT_PUBLISHED} for record in projection.records):
        return False
    ahead = {record.source_identity for record in projection.records if record.accepted_state == OPEN and record.ledger_disposition == BlockerDisposition.VERIFIED_CORRECTION.value}
    return all(diagnostic.code == "cross_store_disagreement" and diagnostic.source_identity in ahead for diagnostic in projection.diagnostics)


def closure_ready(projection: AcceptedFindingProjection, head_sha: str, completion_marker: str = "") -> bool:
    """Whether independent closure assessment of every outstanding finding is applicable.

    Closure covers the complete outstanding set, so it is applicable only when the
    projection is readable, every outstanding finding has an unambiguous current
    identity, none is currently upheld by an independent STILL_VALID observation,
    and a corrective generation exists: either the target is no longer the audited
    head or a delivered request has completed at the same head (``completion_marker``).
    """
    outstanding = outstanding_records(projection)
    if not closure_projection_readable(projection) or not outstanding:
        return False
    for record in outstanding:
        if record.target_binding != BINDING_CURRENT or not record.canonical_blocker_id or record.association == AMBIGUOUS:
            return False
        if any(outcome.outcome == OUTCOME_STILL_VALID_OBSERVED for outcome in record.current_observations):
            return False
    return bool(completion_marker) or any(record.originating_head_sha != head_sha for record in outstanding)


def classify_repair_handoff(result: Sequence[str]) -> tuple[str, str]:
    """Map a repair-routing result to a handoff state and, when waiting, its distinct reason.

    Only a delivered/confirmed request or an in-progress local correction is
    ``DISPATCHED``; every other outcome keeps the work pending under its own
    reason so it can never be reported as delivered or completed.
    """
    text = " ".join(result).lower()
    disposition = str(getattr(result, "route_disposition", "CLOUD"))
    declared = str(getattr(result, "wait_reason", ""))
    if declared in {WAIT_ROUTE_UNAVAILABLE, WAIT_QUOTA, WAIT_INDETERMINATE, WAIT_ALLOWANCE_EXHAUSTED}:
        return HANDOFF_WAITING, declared
    if getattr(result, "quota_deferred", False) or ("until" in text and "deferred claude" in text):
        return HANDOFF_WAITING, WAIT_QUOTA
    if "allowance is exhausted" in text or disposition == "ALLOWANCE_EXHAUSTED":
        return HANDOFF_WAITING, WAIT_ALLOWANCE_EXHAUSTED
    if "unconfirmed durable receipt" in text or disposition == "INDETERMINATE":
        return HANDOFF_WAITING, WAIT_INDETERMINATE
    if getattr(result, "delivered", False) or "already delivered" in text or "already requested" in text or "sent adversarial" in text:
        return HANDOFF_DISPATCHED, ""
    local_phase = str(getattr(result, "local_phase", ""))
    if disposition == "LOCAL_EXECUTION" and local_phase and local_phase not in {"not_admitted", "not_started", "backend_unavailable", "terminal_failure"}:
        return HANDOFF_DISPATCHED, ""
    if getattr(result, "deferred", False):
        return HANDOFF_WAITING, WAIT_QUOTA
    return HANDOFF_WAITING, WAIT_ROUTE_UNAVAILABLE


def correction_blocker_ids(decision: EffectiveReviewDecision) -> tuple[str, ...]:
    return tuple(correction.blocker_id for correction in decision.corrections if correction.blocker_id)


def closure_attempt_key(head_sha: str, finding_set_revision: int, handoff_generation: int) -> str:
    """One closure attempt per target, finding set and completed corrective generation."""
    return f"{head_sha}:{finding_set_revision}:{handoff_generation}"
