"""Durable retention and cycle acceptance of ordinary strong-finding closure evidence.

The ordinary validator produces a closure assessment as a *proposal*. This
module journals that proposal together with its ordinary result and immutable
producing attempt identity, then applies it to the owning review cycle without
another model call. Authority comes only from the owning stores (the ordinary
attempt registry and the review-cycle repository) joined with an authoritative
current-target observation; never from audit history, logs, or in-memory
objects. It performs no GitHub mutation and grants no merge authority: an
accepted closure stays pending until the cycle's publication acknowledgement.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Optional, Tuple

from .adversarial_validation_attempts import AdversarialValidationAttemptRepository
from .pr_review_cycle import (
    FIXED,
    INVALID,
    ContractSnapshot,
    Finding,
)
from .pr_review_cycle import FindingDisposition as DurableFindingDisposition
from .pr_review_cycle import PrReviewCycleRepository, PrReviewCycleSnapshot, ReviewCycleError, RoundProvenance, StaleTransitionError, StrongPolicyIdentity, _stable_identity
from .pr_review_execution import ReviewExecutionResult, ReviewMode, ScopeAssessment
from .runtime_locks import ensure_lock_directory, lock_path

if TYPE_CHECKING:
    from .adversarial_validator import AdversarialValidationResult

_VERIFIED_COVERAGE = {"VERIFIED", "IRRELEVANT"}


class EvidenceState(str, Enum):
    """Distinguishable states of one retained ordinary closure source."""

    MISSING = "MISSING"  # no retained assessment (including legacy bare PASS records)
    RETAINED = "RETAINED"  # durable, not yet applied to the review cycle
    ACCEPTED = "ACCEPTED"  # applied by the owning review cycle
    REJECTED = "REJECTED"  # stale, superseded, or otherwise inapplicable
    UNAVAILABLE = "UNAVAILABLE"  # storage unreadable, corrupt, or unconfirmed


class ApplicationStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    NON_AUTHORIZING = "NON_AUTHORIZING"  # retained, but confers no closure authority
    DEFERRED = "DEFERRED"  # an authoritative observation or local write is unavailable
    REJECTED = "REJECTED"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class OrdinaryOutcome:
    """The ordinary evaluation that produced the assessment, retained whole."""

    result: str = ""
    summary: str = ""
    requirement_coverage: Tuple[Tuple[str, str], ...] = ()
    finding_count: int = 0
    open_test_oracle_gap_count: int = 0
    specification_gap_count: int = 0
    blockers: Tuple[str, ...] = ()
    raw_response_digest: str = ""

    @property
    def is_semantic_pass(self) -> bool:
        return self.result == "PASS" and not self.blockers


@dataclass(frozen=True)
class RetainedDisposition:
    finding_id: str
    status: str
    evidence: str


@dataclass(frozen=True)
class ClosureEvidenceRecord:
    """Immutable producing identity plus mutable application status."""

    source_id: str
    repository: str
    pr_number: int
    open_epoch: int
    attempt_id: str
    attempt_sequence: int
    head_sha: str
    base_sha: str
    contract_identity: str
    policy_identity: str
    round_id: str
    audited_head_sha: str
    finding_set_revision: int
    reviewer_provenance: str
    verdict: str
    dispositions: Tuple[RetainedDisposition, ...]
    new_findings: Tuple[Finding, ...]
    scope: str
    scope_evidence: str
    diagnostic: str
    ordinary: OrdinaryOutcome
    retained_at: float = 0.0
    state: EvidenceState = EvidenceState.RETAINED
    state_reason: str = ""
    closure_id: str = ""
    accepted_requires_strong_audit: bool = False

    @property
    def content_identity(self) -> str:
        """Identity of the immutable semantic payload; excludes application status."""
        payload = _record_to_raw(self)
        for volatile in ("state", "state_reason", "closure_id", "retained_at", "accepted_requires_strong_audit"):
            payload.pop(volatile, None)
        return _stable_identity(json.dumps(payload, sort_keys=True))


@dataclass(frozen=True)
class EvidenceView:
    state: EvidenceState
    record: Optional[ClosureEvidenceRecord] = None
    reason: str = ""


@dataclass(frozen=True)
class ObservedTarget:
    """Authoritative current H/B/M/P, observed from GitHub, Issues, and configuration."""

    head_sha: str
    base_sha: str
    contract: ContractSnapshot
    policy: StrongPolicyIdentity


TargetObserver = Callable[[], ObservedTarget]


@dataclass(frozen=True)
class ApplicationOutcome:
    status: ApplicationStatus
    source_id: str = ""
    reason: str = ""
    cycle_committed: bool = False
    closure_id: str = ""
    requires_strong_audit: bool = False
    pending_publication: str = ""
    reviewer_provenance: str = ""
    attempt_id: str = ""
    attempt_sequence: int = 0
    merge_authorized: bool = field(default=False, init=False)


def _finding_to_raw(finding: Finding) -> dict:
    raw = dict(finding.__dict__)
    raw["requirement_ids"] = list(finding.requirement_ids)
    raw["requirement_texts"] = list(finding.requirement_texts)
    return raw


def _finding_from_raw(raw: dict) -> Finding:
    values = dict(raw)
    values["requirement_ids"] = tuple(values.get("requirement_ids", ()))
    values["requirement_texts"] = tuple(values.get("requirement_texts", ()))
    return Finding(**values)


def _record_to_raw(record: ClosureEvidenceRecord) -> dict:
    ordinary = record.ordinary
    return {
        "source_id": record.source_id,
        "repository": record.repository,
        "pr_number": record.pr_number,
        "open_epoch": record.open_epoch,
        "attempt_id": record.attempt_id,
        "attempt_sequence": record.attempt_sequence,
        "head_sha": record.head_sha,
        "base_sha": record.base_sha,
        "contract_identity": record.contract_identity,
        "policy_identity": record.policy_identity,
        "round_id": record.round_id,
        "audited_head_sha": record.audited_head_sha,
        "finding_set_revision": record.finding_set_revision,
        "reviewer_provenance": record.reviewer_provenance,
        "verdict": record.verdict,
        "dispositions": [{"finding_id": item.finding_id, "status": item.status, "evidence": item.evidence} for item in record.dispositions],
        "new_findings": [_finding_to_raw(item) for item in record.new_findings],
        "scope": record.scope,
        "scope_evidence": record.scope_evidence,
        "diagnostic": record.diagnostic,
        "ordinary": {
            "result": ordinary.result,
            "summary": ordinary.summary,
            "requirement_coverage": [list(item) for item in ordinary.requirement_coverage],
            "finding_count": ordinary.finding_count,
            "open_test_oracle_gap_count": ordinary.open_test_oracle_gap_count,
            "specification_gap_count": ordinary.specification_gap_count,
            "blockers": list(ordinary.blockers),
            "raw_response_digest": ordinary.raw_response_digest,
        },
        "retained_at": record.retained_at,
        "state": record.state.value,
        "state_reason": record.state_reason,
        "closure_id": record.closure_id,
        "accepted_requires_strong_audit": record.accepted_requires_strong_audit,
    }


def _record_from_raw(raw: dict) -> ClosureEvidenceRecord:
    ordinary = raw["ordinary"]
    return ClosureEvidenceRecord(
        source_id=str(raw["source_id"]),
        repository=str(raw["repository"]),
        pr_number=int(raw["pr_number"]),
        open_epoch=int(raw["open_epoch"]),
        attempt_id=str(raw["attempt_id"]),
        attempt_sequence=int(raw["attempt_sequence"]),
        head_sha=str(raw["head_sha"]),
        base_sha=str(raw["base_sha"]),
        contract_identity=str(raw["contract_identity"]),
        policy_identity=str(raw["policy_identity"]),
        round_id=str(raw["round_id"]),
        audited_head_sha=str(raw["audited_head_sha"]),
        finding_set_revision=int(raw["finding_set_revision"]),
        reviewer_provenance=str(raw["reviewer_provenance"]),
        verdict=str(raw["verdict"]),
        dispositions=tuple(RetainedDisposition(str(item["finding_id"]), str(item["status"]), str(item["evidence"])) for item in raw["dispositions"]),
        new_findings=tuple(_finding_from_raw(item) for item in raw["new_findings"]),
        scope=str(raw["scope"]),
        scope_evidence=str(raw["scope_evidence"]),
        diagnostic=str(raw["diagnostic"]),
        ordinary=OrdinaryOutcome(
            result=str(ordinary["result"]),
            summary=str(ordinary["summary"]),
            requirement_coverage=tuple((str(item[0]), str(item[1])) for item in ordinary["requirement_coverage"]),
            finding_count=int(ordinary["finding_count"]),
            open_test_oracle_gap_count=int(ordinary["open_test_oracle_gap_count"]),
            specification_gap_count=int(ordinary["specification_gap_count"]),
            blockers=tuple(str(item) for item in ordinary["blockers"]),
            raw_response_digest=str(ordinary["raw_response_digest"]),
        ),
        retained_at=float(raw["retained_at"]),
        state=EvidenceState(raw["state"]),
        state_reason=str(raw["state_reason"]),
        closure_id=str(raw["closure_id"]),
        accepted_requires_strong_audit=bool(raw["accepted_requires_strong_audit"]),
    )


def source_identity(repository: str, pr_number: int, open_epoch: int, attempt_id: str, attempt_sequence: int) -> str:
    """Stable identity of the one ordinary attempt that produced evidence."""
    return _stable_identity("ordinary-closure-source", repository, str(pr_number), str(open_epoch), attempt_id, str(attempt_sequence))


class OrdinaryClosureEvidenceRepository:
    """Journal of retained ordinary closure sources, one file per repository."""

    def __init__(self, repo_name: str, storage_path: Optional[Path] = None):
        self.repo_name = repo_name
        self.storage_path = storage_path or Path.home() / ".auto-coder" / repo_name / "ordinary_closure_evidence.json"
        self.lock_path = lock_path(repo_name, self.storage_path, "ordinary-closure-evidence-store")

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

    def _read(self) -> dict:
        if not self.storage_path.exists():
            return {"records": {}}
        with self.storage_path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict) or not isinstance(state.get("records"), dict):
            raise RuntimeError("Ordinary closure evidence state is invalid")
        return state

    def _write(self, state: dict) -> None:
        temporary = self.storage_path.with_suffix(f".tmp.{os.getpid()}.{time.monotonic_ns()}")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.storage_path)

    def retain(self, record: ClosureEvidenceRecord) -> EvidenceView:
        """Durably retain a source; an existing source is never overwritten."""
        with self._locked():
            state = self._read()
            records = state["records"]
            existing = records.get(record.source_id)
            if existing is not None:
                retained = _record_from_raw(existing)
                if retained.content_identity != record.content_identity:
                    return EvidenceView(retained.state, retained, "conflicting evidence for this source was ignored; the first retained payload is immutable")
                return EvidenceView(retained.state, retained)
            records[record.source_id] = _record_to_raw(record)
            self._write(state)
            return EvidenceView(record.state, record)

    def inspect(self, source_id: str) -> EvidenceView:
        """Read one source, classifying absent, corrupt, and unreadable storage."""
        try:
            with self._locked():
                raw = self._read()["records"].get(source_id)
                if raw is None:
                    return EvidenceView(EvidenceState.MISSING, None, "no retained closure assessment for this source")
                return EvidenceView((record := _record_from_raw(raw)).state, record)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return EvidenceView(EvidenceState.UNAVAILABLE, None, f"retained closure evidence is unavailable: {exc}")

    def pending_for_pr(self, pr_number: int) -> Tuple[ClosureEvidenceRecord, ...]:
        """Return not-yet-applied sources for a PR, newest attempt first."""
        with self._locked():
            records = [_record_from_raw(raw) for raw in self._read()["records"].values()]
        pending = [item for item in records if item.pr_number == pr_number and item.state is EvidenceState.RETAINED]
        return tuple(sorted(pending, key=lambda item: item.attempt_sequence, reverse=True))

    def transition(self, source_id: str, state_value: EvidenceState, reason: str, closure_id: str = "", requires_strong_audit: bool = False) -> None:
        """Move one retained source to a new application state."""
        with self._locked():
            state = self._read()
            raw = state["records"].get(source_id)
            if raw is None:
                raise RuntimeError("Retained closure evidence identity is unknown")
            if raw["state"] == EvidenceState.ACCEPTED.value and state_value is not EvidenceState.ACCEPTED:
                return
            raw["state"] = state_value.value
            raw["state_reason"] = reason
            if closure_id:
                raw["closure_id"] = closure_id
            raw["accepted_requires_strong_audit"] = requires_strong_audit
            self._write(state)


def _ordinary_outcome(result: "AdversarialValidationResult") -> OrdinaryOutcome:
    verdict = result.result.strip().upper()
    blockers = []
    if verdict != "PASS":
        blockers.append(f"ordinary result is {verdict or 'EMPTY'}")
    if result.findings:
        blockers.append("ordinary findings remain")
    if result.open_test_oracle_gaps:
        blockers.append("open test-oracle gaps remain")
    if result.specification_gaps:
        blockers.append("unresolved specification gaps remain")
    if result.decision_critical_evidence_gaps:
        blockers.append("decision-critical evidence gaps remain")
    if result.unexplained_changes:
        blockers.append("unexplained changes remain")
    if any(item.status.strip().upper() in {"STILL_VALID", "INCONCLUSIVE"} for item in result.thread_dispositions):
        blockers.append("claimed review threads are still unresolved")
    if result.local_repair_verification_pending or result.unverified_local_repairs:
        blockers.append("local repair verification is pending")
    coverage = tuple((item.requirement_id, item.status.strip().upper()) for item in result.requirement_coverage)
    if not coverage or any(status not in _VERIFIED_COVERAGE for _, status in coverage):
        blockers.append("Issue requirement coverage is not completely verified")
    return OrdinaryOutcome(
        result=verdict,
        summary=result.summary,
        requirement_coverage=coverage,
        finding_count=len(result.findings),
        open_test_oracle_gap_count=len(result.open_test_oracle_gaps),
        specification_gap_count=len(result.specification_gaps),
        blockers=tuple(blockers),
        raw_response_digest=hashlib.sha256(result.raw_response.encode("utf-8")).hexdigest(),
    )


class OrdinaryClosureEvidence:
    """Retain ordinary closure assessments and apply them through the owning stores."""

    def __init__(
        self,
        evidence: OrdinaryClosureEvidenceRepository,
        cycle: PrReviewCycleRepository,
        attempts: AdversarialValidationAttemptRepository,
    ) -> None:
        self.evidence = evidence
        self.cycle = cycle
        self.attempts = attempts

    # -- retention -----------------------------------------------------

    def retain(self, result: "AdversarialValidationResult") -> EvidenceView:
        """Journal one ordinary result and its closure assessment together.

        A result with no assessment is MISSING evidence, never retained. Only
        a result whose assessment names the registered attempt that produced
        the ordinary result is accepted for retention, so evidence and an
        ordinary PASS from different attempts can never be combined.
        """
        assessment = result.closure_assessment
        if assessment is None:
            return EvidenceView(EvidenceState.MISSING, None, result.closure_assessment_diagnostic or "ordinary result carries no closure assessment")
        try:
            record = self._build_record(result, assessment)
        except ValueError as exc:
            return EvidenceView(EvidenceState.REJECTED, None, str(exc))
        try:
            return self.evidence.retain(record)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return EvidenceView(EvidenceState.UNAVAILABLE, None, f"closure evidence could not be durably retained: {exc}")

    def _build_record(self, result: "AdversarialValidationResult", assessment: ReviewExecutionResult) -> ClosureEvidenceRecord:
        if assessment.mode is not ReviewMode.ORDINARY_CLOSURE:
            raise ValueError("closure assessment was not produced by an ordinary closure evaluation")
        if not (assessment.attempt_id and result.attempt_id == assessment.attempt_id and result.attempt_sequence == assessment.attempt_sequence):
            raise ValueError("closure assessment does not belong to the ordinary result's attempt")
        required = (assessment.repository, assessment.head_sha, assessment.base_sha, assessment.contract_identity, assessment.policy_identity, assessment.round_id, assessment.audited_head_sha)
        if not all(required) or assessment.pr_number <= 0 or assessment.open_epoch < 0 or assessment.attempt_sequence <= 0:
            raise ValueError("closure assessment lacks repository, PR, epoch, attempt, or target identity")
        try:
            registered = self.attempts.get(assessment.attempt_id)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ValueError(f"ordinary attempt registry is unavailable: {exc}") from exc
        if registered is None or (registered.sequence, registered.pr_number, registered.head_sha) != (assessment.attempt_sequence, assessment.pr_number, assessment.head_sha):
            raise ValueError("closure assessment does not match a registered ordinary attempt for this PR and head")
        return ClosureEvidenceRecord(
            source_id=source_identity(assessment.repository, assessment.pr_number, assessment.open_epoch, assessment.attempt_id, assessment.attempt_sequence),
            repository=assessment.repository,
            pr_number=assessment.pr_number,
            open_epoch=assessment.open_epoch,
            attempt_id=assessment.attempt_id,
            attempt_sequence=assessment.attempt_sequence,
            head_sha=assessment.head_sha,
            base_sha=assessment.base_sha,
            contract_identity=assessment.contract_identity,
            policy_identity=assessment.policy_identity,
            round_id=assessment.round_id,
            audited_head_sha=assessment.audited_head_sha,
            finding_set_revision=assessment.finding_set_revision,
            reviewer_provenance=assessment.reviewer_provenance,
            verdict=assessment.verdict,
            dispositions=tuple(RetainedDisposition(item.finding_id, item.status, item.evidence) for item in assessment.dispositions),
            new_findings=assessment.findings,
            scope=assessment.scope.value if assessment.scope is not None else "",
            scope_evidence=assessment.scope_evidence,
            diagnostic=assessment.diagnostic,
            ordinary=_ordinary_outcome(result),
            retained_at=time.time(),
        )

    def inspect(self, source_id: str) -> EvidenceView:
        return self.evidence.inspect(source_id)

    # -- application ---------------------------------------------------

    def apply(self, source_id: str, observe: TargetObserver) -> ApplicationOutcome:
        """Apply one retained source to the owning review cycle without a model call.

        Idempotent: replaying a source the cycle already committed only
        completes local bookkeeping. Safe to call after any interruption.
        """
        view = self.evidence.inspect(source_id)
        if view.record is None:
            status = ApplicationStatus.UNAVAILABLE if view.state is EvidenceState.UNAVAILABLE else ApplicationStatus.MISSING
            return ApplicationOutcome(status, source_id, view.reason)
        record = view.record
        if view.state is EvidenceState.REJECTED:
            return self._outcome(ApplicationStatus.REJECTED, record, record.state_reason or "evidence was rejected")
        try:
            observed = observe()
        except Exception as exc:  # an unavailable authoritative observation defers; it never erases evidence
            return self._outcome(ApplicationStatus.DEFERRED, record, f"authoritative target observation unavailable: {exc}")
        try:
            with self.attempts.serialized_transition():
                return self._apply_locked(record, observed)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return self._outcome(ApplicationStatus.DEFERRED, record, f"owning review-cycle or attempt state unavailable: {exc}")

    def reconcile(self, pr_number: int, observe: TargetObserver) -> Tuple[ApplicationOutcome, ...]:
        """Resume every outstanding retained source for a PR after restart."""
        try:
            pending = self.evidence.pending_for_pr(pr_number)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return (ApplicationOutcome(ApplicationStatus.UNAVAILABLE, "", f"retained closure evidence is unavailable: {exc}"),)
        return tuple(self.apply(item.source_id, observe) for item in pending)

    def _apply_locked(self, record: ClosureEvidenceRecord, observed: ObservedTarget) -> ApplicationOutcome:
        snapshot = self.cycle.snapshot(record.pr_number)
        committed = next((item for item in snapshot.closures if item.source_identity == record.source_id), None)
        if committed is not None:
            # The cycle already owns this source. Never reopen or re-certify it;
            # only finish the local bookkeeping that may have been interrupted.
            return self._finish_bookkeeping(record, snapshot, committed.closure_id, not committed.bounded)

        registered = self.attempts.get(record.attempt_id)
        if registered is None or (registered.sequence, registered.pr_number, registered.head_sha) != (record.attempt_sequence, record.pr_number, record.head_sha):
            return self._reject(record, "the producing ordinary attempt is not registered for this PR and head")
        if self.attempts.latest_sequence(record.pr_number, record.head_sha) > record.attempt_sequence:
            return self._reject(record, "a newer applicable ordinary attempt supersedes the evidence")
        rejection = self._stale_reason(record, observed, snapshot)
        if rejection:
            return self._reject(record, rejection)
        authority_gap = self._authority_gap(record, snapshot)
        if authority_gap:
            return self._outcome(ApplicationStatus.NON_AUTHORIZING, record, authority_gap)

        provenance = RoundProvenance(record.head_sha, record.base_sha)
        bounded = record.scope == ScopeAssessment.BOUNDED.value
        try:
            if (snapshot.ordinary_pass_head_sha, snapshot.ordinary_pass_base_sha, snapshot.ordinary_pass_contract_identity) != (record.head_sha, record.base_sha, record.contract_identity):
                snapshot = self.cycle.record_ordinary_pass(record.pr_number, provenance, observed.contract, expected_version=snapshot.transition_version)
            snapshot = self.cycle.certify_closure(
                record.pr_number,
                provenance,
                observed.contract,
                observed.policy,
                record.round_id,
                record.finding_set_revision,
                [DurableFindingDisposition(item.finding_id, item.status, item.evidence, record.head_sha) for item in record.dispositions],
                bounded=bounded,
                bounded_evidence=record.scope_evidence,
                expected_version=snapshot.transition_version,
                source_identity=record.source_id,
                expected_open_epoch=record.open_epoch,
            )
        except StaleTransitionError as exc:
            return self._outcome(ApplicationStatus.DEFERRED, record, f"review cycle changed while applying; retry from current state: {exc}")
        except (ReviewCycleError, ValueError) as exc:
            return self._reject(record, f"owning review cycle refused the evidence: {exc}")
        committed = next((item for item in snapshot.closures if item.source_identity == record.source_id), None)
        if committed is None:
            return self._outcome(ApplicationStatus.DEFERRED, record, "cycle transition could not be confirmed from the owning state")
        return self._finish_bookkeeping(record, snapshot, committed.closure_id, not committed.bounded)

    @staticmethod
    def _stale_reason(record: ClosureEvidenceRecord, observed: ObservedTarget, snapshot: PrReviewCycleSnapshot) -> str:
        if (observed.head_sha, observed.base_sha) != (record.head_sha, record.base_sha):
            return "PR head or base changed since the evidence was produced"
        if observed.contract.identity != record.contract_identity:
            return "linked Issue Requirements changed since the evidence was produced"
        if observed.policy.identity != record.policy_identity:
            return "strong policy changed since the evidence was produced"
        if snapshot.closed or snapshot.open_epoch != record.open_epoch:
            return "PR open epoch changed since the evidence was produced"
        strong_round = snapshot.accepted_strong_round
        if snapshot.active_claim is not None:
            return "a newer strong-audit claim supersedes the evidence"
        if strong_round is None or strong_round.round_id != record.round_id or strong_round.head_sha != record.audited_head_sha:
            return "the accepted strong round changed since the evidence was produced"
        if (strong_round.base_sha, strong_round.contract_identity, strong_round.policy_identity) != (record.base_sha, record.contract_identity, record.policy_identity):
            return "base, Requirements, or strong policy differ from the accepted strong round"
        if snapshot.finding_set_revision != record.finding_set_revision or snapshot.requires_new_strong_round:
            return "the outstanding finding set changed since the evidence was produced"
        if not record.diagnostic and {item.finding_id for item in snapshot.open_findings} != {item.finding_id for item in record.dispositions}:
            return "the evidence does not cover exactly the current outstanding finding identities"
        return ""

    def _authority_gap(self, record: ClosureEvidenceRecord, snapshot: PrReviewCycleSnapshot) -> str:
        if record.diagnostic:
            return f"assessment is incomplete and remains pending evidence: {record.diagnostic}"
        if record.verdict != "PASS":
            return f"closure assessment verdict is {record.verdict}"
        if not record.ordinary.is_semantic_pass:
            return "ordinary evaluation is not a complete semantic PASS: " + "; ".join(record.ordinary.blockers or ("not PASS",))
        if any(item.status not in {FIXED, INVALID} or not item.evidence.strip() for item in record.dispositions):
            return "every outstanding finding requires an evidence-backed FIXED or INVALID disposition"
        if record.new_findings:
            return "newly discovered unresolved findings remain blocking"
        if record.scope not in {item.value for item in ScopeAssessment} or not record.scope_evidence.strip():
            return "cumulative scope evidence is missing"
        return ""

    def _reject(self, record: ClosureEvidenceRecord, reason: str) -> ApplicationOutcome:
        try:
            self.evidence.transition(record.source_id, EvidenceState.REJECTED, reason)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return self._outcome(ApplicationStatus.REJECTED, record, f"{reason} (rejection not yet journaled)")
        return self._outcome(ApplicationStatus.REJECTED, record, reason)

    def _finish_bookkeeping(self, record: ClosureEvidenceRecord, snapshot: PrReviewCycleSnapshot, closure_id: str, expanded: bool) -> ApplicationOutcome:
        reason = "accepted; renewed independent strong audit required" if expanded else "accepted; closure publication acknowledgement pending"
        pending = snapshot.pending_effect if closure_id and snapshot.accepted_closure is not None and snapshot.accepted_closure.closure_id == closure_id else ""
        try:
            self.evidence.transition(record.source_id, EvidenceState.ACCEPTED, reason, closure_id, expanded)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            return self._outcome(
                ApplicationStatus.DEFERRED,
                record,
                f"cycle committed this source; local bookkeeping pending: {exc}",
                cycle_committed=True,
                closure_id=closure_id,
                requires_strong_audit=expanded,
                pending_publication=pending,
            )
        return self._outcome(ApplicationStatus.ACCEPTED, record, reason, cycle_committed=True, closure_id=closure_id, requires_strong_audit=expanded, pending_publication=pending)

    @staticmethod
    def _outcome(status: ApplicationStatus, record: ClosureEvidenceRecord, reason: str, **fields: object) -> ApplicationOutcome:
        return ApplicationOutcome(
            status,
            record.source_id,
            reason,
            reviewer_provenance=record.reviewer_provenance,
            attempt_id=record.attempt_id,
            attempt_sequence=record.attempt_sequence,
            **fields,  # type: ignore[arg-type]
        )

    # -- dependent effects ---------------------------------------------

    def dependent_effects_allowed(self, source_id: str, observe: TargetObserver) -> Tuple[bool, str]:
        """Whether an effect depending on this source's closure may still run.

        Committed evidence is never reopened by a later attempt, but it cannot
        bypass the newer-attempt or changed-target gates for subsequent effects.
        Final merge additionally requires the cycle's own acknowledgements.
        """
        view = self.evidence.inspect(source_id)
        if view.state is not EvidenceState.ACCEPTED or view.record is None:
            return False, f"closure evidence is {view.state.value}"
        record = view.record
        try:
            observed = observe()
            with self.attempts.serialized_transition():
                snapshot = self.cycle.snapshot(record.pr_number)
                newest = self.attempts.latest_sequence(record.pr_number, record.head_sha)
        except Exception as exc:
            return False, f"authoritative state unavailable: {exc}"
        if not any(item.source_identity == source_id for item in snapshot.closures):
            return False, "the owning review cycle does not hold this source"
        if newest > record.attempt_sequence:
            return False, "a newer ordinary attempt is applicable"
        if (observed.head_sha, observed.base_sha) != (record.head_sha, record.base_sha) or observed.contract.identity != record.contract_identity or observed.policy.identity != record.policy_identity:
            return False, "the authoritative target changed"
        if snapshot.closed or snapshot.open_epoch != record.open_epoch:
            return False, "PR open epoch changed"
        return True, ""
