"""Session-independent bridge from accepted Strong findings to ordinary rereview.

``PrReviewCycleRepository`` owns accepted Strong rounds/findings and their
lifecycle; ``CanonicalPRBlockerLedger`` owns stable blocker identity and native
root associations.  This module owns neither.  It joins them: every finding
durably accepted by the Strong result-acceptance path becomes a *known* finding
for ordinary rereview, keyed by its exact accepted source identity
(``<round_id>:<finding_id>``) rather than by reviewer-session state, an ordinary
TOG label, a provider session, or the evaluated head.

The bridge is idempotent and reconstructs everything from the two owning
stores.  Ledger writes are compare-and-set; a contested or failed write leaves
the prior accepted state intact and yields an explicitly incomplete projection.
A projection is complete only when the source read, the canonical join and the
source revision re-check all succeeded.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Optional, Sequence

from .canonical_pr_blocker_ledger import (
    BlockerAdmissionPayload,
    BlockerAlias,
    BlockerDisposition,
    BlockerLedgerError,
    BlockerLedgerSnapshot,
    BlockerSnapshot,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    EvidenceAvailability,
    QualifiedRequirement,
    StaleLedgerRevisionError,
)
from .logger_config import get_logger
from .pr_review_cycle import FIXED, INVALID, OPEN, PUBLICATION_ACKNOWLEDGED, RETIRED, Finding, PrReviewCycleRepository, PrReviewCycleSnapshot, StrongAuditRound
from .pr_review_effects import AcceptedReviewPayload
from .reviewer_session_registry import TestOracleGap
from .util.github_request_outcome import normalize_api_origin

logger = get_logger(__name__)

DEFAULT_API_ORIGIN = "https://api.github.com"
SOURCE_ALIAS_TYPE = "strong_audit_finding"
GAP_ALIAS_TYPE = "test_oracle_gap"
ROOT_ALIAS_TYPE = "github_root_comment"
THREAD_ALIAS_TYPE = "github_thread"
FINDING_ROOT_MARKER = re.compile(r"<!-- auto-coder-two-tier-finding:v1:([0-9a-f]{64}):([0-9a-f]{64}) -->")
_EVIDENCE_ANCHOR = re.compile(r"([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):(\d+)")
_QUALIFIED_REQUIREMENT = re.compile(r"^#(\d+)/(.+)$")
_MAX_ATTEMPTS = 3

CATEGORY_IMPLEMENTATION = "IMPLEMENTATION"
CATEGORY_REGRESSION_GAP = "REGRESSION_GAP"

ASSOCIATED = "ASSOCIATED"
NOT_PUBLISHED = "NOT_PUBLISHED"
UNKNOWN = "UNKNOWN"
AMBIGUOUS = "AMBIGUOUS"
UNAVAILABLE = "UNAVAILABLE"

BINDING_CURRENT = "CURRENT"
BINDING_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"

CURRENCY_CURRENT_HEAD = "CURRENT_HEAD"
CURRENCY_HISTORICAL = "HISTORICAL"

OUTCOME_STILL_VALID_OBSERVED = "STILL_VALID_OBSERVED"
OUTCOME_UNRESOLVED_RETAINED = "UNRESOLVED_RETAINED"
OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED = "CLOSURE_PROPOSAL_NOT_ACCEPTED"
OUTCOME_ACCEPTED_CLOSURE_RETAINED = "ACCEPTED_CLOSURE_RETAINED"
OUTCOME_UNRECOGNIZED = "UNRECOGNIZED"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_UNAVAILABLE = "UNAVAILABLE"


class OrdinaryStatus(str, Enum):
    """Ordinary-review dispositions the bridge understands."""

    ADDRESSED = "ADDRESSED"
    STILL_VALID = "STILL_VALID"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class ProjectionTarget:
    """The current target a projection is evaluated against.

    An empty ``contract_identity`` or ``policy_identity`` means the caller does
    not assert that identity; a non-empty value that differs from the
    originating round requires reconciliation instead of reuse.
    """

    repository: str = ""
    pr_number: int = 0
    head_sha: str = ""
    base_sha: str = ""
    contract_identity: str = ""
    policy_identity: str = ""
    api_origin: str = DEFAULT_API_ORIGIN


@dataclass(frozen=True)
class ObservedRoot:
    """One native review root observed through an authenticated adapter."""

    comment_id: int = 0
    body: str = ""
    authenticated: bool = False
    thread_id: str = ""


@dataclass(frozen=True)
class RootObservation:
    """A native-root listing; ``complete`` is true only for a fully successful read."""

    roots: tuple[ObservedRoot, ...] = ()
    complete: bool = False
    reason: str = ""


@dataclass(frozen=True)
class OrdinaryDisposition:
    """An independent ordinary-review disposition naming one accepted finding.

    The finding is identified by exactly one of ``finding_id`` / ``source_identity``
    (direct) or by ``root_comment_id`` / ``thread_id`` (via an associated root).
    """

    status: str = ""
    rationale: str = ""
    evidence: str = ""
    head_sha: str = ""
    finding_id: str = ""
    source_identity: str = ""
    root_comment_id: Optional[int] = None
    thread_id: str = ""


@dataclass(frozen=True)
class DispositionOutcome:
    """What the bridge did with one ordinary disposition."""

    outcome: str = ""
    source_identity: str = ""
    status: str = ""
    detail: str = ""


@dataclass(frozen=True)
class BridgeDiagnostic:
    """An explicit completeness, unavailable or ambiguity diagnostic."""

    code: str = ""
    source_identity: str = ""
    detail: str = ""


@dataclass(frozen=True)
class AcceptedFindingRecord:
    """One accepted Strong finding with its canonical identity and current observations."""

    source_identity: str = ""
    finding_id: str = ""
    round_id: str = ""
    api_origin: str = ""
    repository: str = ""
    pr_number: int = 0
    open_epoch: int = 0
    originating_head_sha: str = ""
    originating_base_sha: str = ""
    contract_identity: str = ""
    policy_identity: str = ""
    qualified_requirements: tuple[QualifiedRequirement, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    requirement_texts: tuple[str, ...] = ()
    original_scope: str = ""
    affected_boundary: str = ""
    category: str = CATEGORY_IMPLEMENTATION
    evidence: str = ""
    accepted_state: str = OPEN
    closure_evidence: str = ""
    closure_head_sha: str = ""
    canonical_blocker_id: str = ""
    ledger_disposition: str = ""
    root_comment_ids: tuple[int, ...] = ()
    association: str = UNKNOWN
    target_binding: str = BINDING_CURRENT
    evidence_currency: str = CURRENCY_HISTORICAL
    current_observations: tuple[DispositionOutcome, ...] = ()
    source_revision: int = 0
    association_revision: int = 0
    plausible_incorrect_implementation: str = ""
    why_tests_admit_it: str = ""
    material_consequence: str = ""
    focused_regression_scenario: str = ""

    @property
    def is_unresolved(self) -> bool:
        return self.accepted_state == OPEN

    @property
    def known_gap_id(self) -> str:
        """The ordinary TOG representation; only derived from an established canonical identity."""
        if self.category == CATEGORY_REGRESSION_GAP and self.canonical_blocker_id:
            return tog_id_for(self.source_identity)
        return ""


@dataclass(frozen=True)
class AcceptedFindingProjection:
    """Structured projection of accepted findings for later stages to consume."""

    target: ProjectionTarget = field(default_factory=ProjectionTarget)
    records: tuple[AcceptedFindingRecord, ...] = ()
    complete: bool = False
    diagnostics: tuple[BridgeDiagnostic, ...] = ()
    disposition_outcomes: tuple[DispositionOutcome, ...] = ()
    open_epoch: int = 0
    source_revision: int = -1
    finding_set_revision: int = 0
    ledger_revision: int = 0

    @property
    def unresolved(self) -> tuple[AcceptedFindingRecord, ...]:
        return tuple(record for record in self.records if record.is_unresolved)

    @property
    def known_gap_records(self) -> tuple[AcceptedFindingRecord, ...]:
        """Unresolved regression-gap findings with an established canonical identity and current binding."""
        return tuple(record for record in self.unresolved if record.known_gap_id and record.target_binding == BINDING_CURRENT)


def source_identity_for(round_id: str, finding_id: str) -> str:
    return f"{round_id}:{finding_id}"


def _short_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:24]


def tog_id_for(source_identity: str) -> str:
    """Stable ordinary TOG label derived only from the exact accepted source identity."""
    return f"TOG-ACCEPTED-{_short_hash('tog', source_identity)[:16]}"


def _qualified(requirement_id: str, issue_ids: Sequence[str]) -> QualifiedRequirement:
    match = _QUALIFIED_REQUIREMENT.match(requirement_id)
    if match:
        return QualifiedRequirement(int(match.group(1)), match.group(2))
    if len(issue_ids) == 1 and issue_ids[0].lstrip("#").isdigit():
        return QualifiedRequirement(int(issue_ids[0].lstrip("#")), requirement_id)
    return QualifiedRequirement(0, requirement_id)


def _original_scope(finding: Finding) -> str:
    return f"Scenario: {finding.counterexample}\nExpected: {finding.expected_behavior}\nActual: {finding.actual_behavior}"


class _Contended(Exception):
    """Internal: a compare-and-set lost a race; the pass must be recomputed."""


class AcceptedFindingBridge:
    """Join accepted Strong findings with canonical blocker bookkeeping."""

    def __init__(
        self,
        cycle: PrReviewCycleRepository,
        ledger: CanonicalPRBlockerLedger,
        checkpoint: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.cycle = cycle
        self.ledger = ledger
        self._checkpoint = checkpoint or (lambda _name: None)

    # -- public API ----------------------------------------------------------

    def project(
        self,
        target: ProjectionTarget,
        observation: Optional[RootObservation] = None,
        dispositions: Sequence[OrdinaryDisposition] = (),
    ) -> AcceptedFindingProjection:
        """Reconstruct, associate and project every accepted finding for the target PR.

        Idempotent: repeating it with unchanged authoritative inputs changes nothing.
        """
        target = replace(target, api_origin=normalize_api_origin(target.api_origin or DEFAULT_API_ORIGIN))
        last_diagnostic = "source revision changed during projection"
        for _attempt in range(_MAX_ATTEMPTS):
            try:
                source = self._read_source(target)
            except Exception as exc:  # unreadable source is never an empty set
                return AcceptedFindingProjection(target=target, complete=False, diagnostics=(BridgeDiagnostic("source_unavailable", "", str(exc)),))
            self._checkpoint("after_source_read")
            try:
                projection = self._reconcile(target, source, observation, dispositions)
            except _Contended as exc:
                last_diagnostic = str(exc)
                continue
            self._checkpoint("before_projection_commit")
            try:
                current_version = self.cycle.current_version(target.pr_number) if self.cycle.storage_path.exists() else 0
            except Exception as exc:
                return replace(projection, complete=False, diagnostics=projection.diagnostics + (BridgeDiagnostic("source_revision_unconfirmed", "", str(exc)),))
            if current_version != source.transition_version:
                last_diagnostic = f"source revision moved from {source.transition_version} to {current_version}"
                continue
            return projection
        return AcceptedFindingProjection(
            target=target,
            complete=False,
            diagnostics=(BridgeDiagnostic("projection_contended", "", last_diagnostic),),
            source_revision=-1,
        )

    # -- internals -----------------------------------------------------------

    def _read_source(self, target: ProjectionTarget) -> PrReviewCycleSnapshot:
        if not self.cycle.storage_path.exists():
            # A successful authoritative read of "no accepted state": nothing to project.
            return _empty_snapshot(self.cycle.repo_name, target.pr_number)
        return self.cycle.snapshot(target.pr_number)

    @staticmethod
    def _accepted_findings(source: PrReviewCycleSnapshot) -> list[tuple[StrongAuditRound, Finding]]:
        """Findings durably accepted through a Strong result, paired with their round."""
        by_claim = {record.claim_id: record for record in source.strong_rounds}
        accepted: list[tuple[StrongAuditRound, Finding]] = []
        for finding in source.findings:
            record = by_claim.get(finding.origin_round_id)
            if record is not None and finding.finding_id in record.finding_ids:
                accepted.append((record, finding))
        return accepted

    def _reconcile(
        self,
        target: ProjectionTarget,
        source: PrReviewCycleSnapshot,
        observation: Optional[RootObservation],
        dispositions: Sequence[OrdinaryDisposition],
    ) -> AcceptedFindingProjection:
        accepted = self._accepted_findings(source)
        diagnostics: list[BridgeDiagnostic] = []
        if not accepted:
            return AcceptedFindingProjection(target=target, complete=True, open_epoch=source.open_epoch, source_revision=source.transition_version, finding_set_revision=source.finding_set_revision)

        eligible: list[tuple[StrongAuditRound, Finding]] = []
        for record, finding in accepted:
            identity = source_identity_for(record.round_id, finding.finding_id)
            if record.open_epoch != source.open_epoch:
                diagnostics.append(BridgeDiagnostic("epoch_reconciliation_required", identity, f"accepted in open epoch {record.open_epoch}; PR is in epoch {source.open_epoch}"))
                continue
            eligible.append((record, finding))

        ledger_snapshot: Optional[BlockerLedgerSnapshot] = None
        outcomes: list[DispositionOutcome] = []
        try:
            ledger_snapshot = self._ledger_snapshot(target)
            ledger_snapshot = self._ensure_blockers(target, source, eligible, ledger_snapshot, diagnostics)
            ledger_snapshot = self._sync_lifecycle(target, eligible, ledger_snapshot, diagnostics)
            ledger_snapshot = self._associate_roots(target, source, eligible, ledger_snapshot, observation, diagnostics)
            ledger_snapshot = self._record_dispositions(target, eligible, ledger_snapshot, dispositions, diagnostics, outcomes)
        except StaleLedgerRevisionError as exc:
            raise _Contended(str(exc)) from exc
        except BlockerLedgerError as exc:
            diagnostics.append(BridgeDiagnostic("association_unavailable", "", str(exc)))
            ledger_snapshot = self._try_ledger_snapshot(target) or ledger_snapshot

        records = tuple(self._build_record(target, source, record, finding, ledger_snapshot, observation, diagnostics, outcomes) for record, finding in accepted)
        return AcceptedFindingProjection(
            target=target,
            records=records,
            # An unknown native root stays explicit per record, but only blocks completeness while a repair obligation is open.
            complete=not diagnostics and all(record.association in {ASSOCIATED, NOT_PUBLISHED} or not record.is_unresolved for record in records),
            diagnostics=tuple(diagnostics),
            disposition_outcomes=tuple(outcomes),
            open_epoch=source.open_epoch,
            source_revision=source.transition_version,
            finding_set_revision=source.finding_set_revision,
            ledger_revision=ledger_snapshot.ledger_revision if ledger_snapshot is not None else 0,
        )

    def _try_ledger_snapshot(self, target: ProjectionTarget) -> Optional[BlockerLedgerSnapshot]:
        try:
            return self.ledger.get_snapshot(target.api_origin, target.repository, target.pr_number, require_retained_state=True)
        except BlockerLedgerError:
            return None

    def _ledger_snapshot(self, target: ProjectionTarget) -> BlockerLedgerSnapshot:
        try:
            return self.ledger.get_snapshot(target.api_origin, target.repository, target.pr_number, require_retained_state=True)
        except BlockerLedgerError:
            # Missing namespace is initialized; a corrupt/unreadable store re-raises on the next read.
            self.ledger.initialize_namespace(target.api_origin, target.repository, target.pr_number)
            return self.ledger.get_snapshot(target.api_origin, target.repository, target.pr_number, require_retained_state=True)

    @staticmethod
    def _blocker_for_source(snapshot: BlockerLedgerSnapshot, source_identity: str) -> Optional[BlockerSnapshot]:
        owners = snapshot.get_blockers_for_alias(SOURCE_ALIAS_TYPE, source_identity)
        return owners[0] if len(owners) == 1 else None

    def _ensure_blockers(
        self,
        target: ProjectionTarget,
        source: PrReviewCycleSnapshot,
        eligible: Sequence[tuple[StrongAuditRound, Finding]],
        snapshot: BlockerLedgerSnapshot,
        diagnostics: list[BridgeDiagnostic],
    ) -> BlockerLedgerSnapshot:
        for record, finding in eligible:
            identity = source_identity_for(record.round_id, finding.finding_id)
            owners = snapshot.get_blockers_for_alias(SOURCE_ALIAS_TYPE, identity)
            if len(owners) > 1:
                diagnostics.append(BridgeDiagnostic("duplicate_source_association", identity, "multiple canonical blockers claim one accepted source identity"))
                continue
            if owners:
                snapshot = self._ensure_gap_alias(target, snapshot, owners[0], identity, finding, diagnostics)
                continue
            self._checkpoint("before_association_commit")
            requirements = tuple(_qualified(requirement_id, record.contract_snapshot.issue_ids) for requirement_id in finding.requirement_ids)
            payload = BlockerAdmissionPayload(
                category="TEST_ORACLE" if finding.is_regression_gap else "IMPLEMENTATION",
                qualified_requirements=requirements,
                authoritative_boundary=finding.affected_boundary,
                incorrect_behavior_or_missing_invariant=finding.actual_behavior,
                required_correction_outcome=finding.expected_behavior,
                evidence_needed=finding.focused_regression_scenario or finding.expected_behavior,
                accepted_scope=CorrectionScope(description=_original_scope(finding), concern_ids=(identity,)),
                aliases=(BlockerAlias(alias_type=SOURCE_ALIAS_TYPE, alias_value=identity),) + ((BlockerAlias(alias_type=GAP_ALIAS_TYPE, alias_value=tog_id_for(identity)),) if finding.is_regression_gap else ()),
                evidence=finding.evidence,
                reviewed_head_sha=record.head_sha,
                reviewed_base_sha=record.base_sha,
                review_attempt_id=record.round_id,
                requirement_manifest_revision=record.contract_identity,
                observation_identity=identity,
            )
            try:
                _, snapshot = self.ledger.admit_blocker(
                    target.api_origin,
                    target.repository,
                    target.pr_number,
                    operation_id=f"accepted-finding-admit:{_short_hash(identity)}",
                    expected_ledger_revision=snapshot.ledger_revision,
                    payload=payload,
                    review_observation_identity=identity,
                )
            except StaleLedgerRevisionError:
                raise
            except BlockerLedgerError as exc:
                diagnostics.append(BridgeDiagnostic("association_write_failed", identity, str(exc)))
        return self._ledger_snapshot(target)

    def _ensure_gap_alias(self, target: ProjectionTarget, snapshot: BlockerLedgerSnapshot, blocker: BlockerSnapshot, identity: str, finding: Finding, diagnostics: list[BridgeDiagnostic]) -> BlockerLedgerSnapshot:
        """Let the existing TOG reconciliation resolve the ordinary label to this blocker and its native root."""
        gap_id = tog_id_for(identity)
        if not finding.is_regression_gap or any(alias.alias_type == GAP_ALIAS_TYPE and alias.alias_value == gap_id for alias in blocker.aliases):
            return snapshot
        try:
            return self.ledger.add_alias(
                target.api_origin,
                target.repository,
                target.pr_number,
                operation_id=f"accepted-finding-gap-alias:{_short_hash(identity)}",
                expected_ledger_revision=snapshot.ledger_revision,
                blocker_id=blocker.blocker_id,
                alias_type=GAP_ALIAS_TYPE,
                alias_value=gap_id,
            )
        except StaleLedgerRevisionError:
            raise
        except BlockerLedgerError as exc:
            diagnostics.append(BridgeDiagnostic("association_write_failed", identity, str(exc)))
            return snapshot

    def _sync_lifecycle(
        self,
        target: ProjectionTarget,
        eligible: Sequence[tuple[StrongAuditRound, Finding]],
        snapshot: BlockerLedgerSnapshot,
        diagnostics: list[BridgeDiagnostic],
    ) -> BlockerLedgerSnapshot:
        """Project accepted FIXED/INVALID state into the ledger; never the reverse."""
        for record, finding in eligible:
            identity = source_identity_for(record.round_id, finding.finding_id)
            blocker = self._blocker_for_source(snapshot, identity)
            if blocker is None:
                continue
            closed_dispositions = {BlockerDisposition.VERIFIED_CORRECTION, BlockerDisposition.AUTHORIZED_INVALIDATION}
            if finding.status == OPEN:
                if blocker.disposition in closed_dispositions:
                    diagnostics.append(BridgeDiagnostic("cross_store_disagreement", identity, f"ledger records {blocker.disposition.value} but the accepted lifecycle is OPEN"))
                continue
            wanted = BlockerDisposition.VERIFIED_CORRECTION if finding.status == FIXED else BlockerDisposition.AUTHORIZED_INVALIDATION
            if blocker.disposition == wanted:
                continue
            if blocker.disposition in closed_dispositions:
                diagnostics.append(BridgeDiagnostic("cross_store_disagreement", identity, f"ledger records {blocker.disposition.value} but accepted lifecycle is {finding.status}"))
                continue
            self._checkpoint("before_association_commit")
            try:
                snapshot = self.ledger.record_transition(
                    target.api_origin,
                    target.repository,
                    target.pr_number,
                    operation_id=f"accepted-finding-close:{_short_hash(identity, finding.status)}",
                    expected_ledger_revision=snapshot.ledger_revision,
                    blocker_id=blocker.blocker_id,
                    target_disposition=wanted,
                    evidence=finding.disposition_evidence,
                    transition_reason=f"Accepted Strong lifecycle state {finding.status}",
                    reviewed_head_sha=finding.disposition_head_sha,
                    reviewed_base_sha=record.base_sha,
                    review_attempt_id=record.round_id,
                    requirement_manifest_revision=record.contract_identity,
                    review_observation_identity=identity,
                )
            except StaleLedgerRevisionError:
                raise
            except BlockerLedgerError as exc:
                diagnostics.append(BridgeDiagnostic("association_write_failed", identity, str(exc)))
        return self._ledger_snapshot(target)

    def _associate_roots(
        self,
        target: ProjectionTarget,
        source: PrReviewCycleSnapshot,
        eligible: Sequence[tuple[StrongAuditRound, Finding]],
        snapshot: BlockerLedgerSnapshot,
        observation: Optional[RootObservation],
        diagnostics: list[BridgeDiagnostic],
    ) -> BlockerLedgerSnapshot:
        if observation is None:
            return snapshot
        if not observation.complete and observation.reason:
            diagnostics.append(BridgeDiagnostic("root_observation_unavailable", "", observation.reason))
        # Exact marker: the payload identity of the accepted round plus the hash of the finding id.
        expected: dict[tuple[str, str], str] = {}
        for record, finding in eligible:
            try:
                payload = AcceptedReviewPayload.strong(target.repository, target.pr_number, record, source.findings)
            except ValueError:
                continue
            marker = (payload.identity, hashlib.sha256(finding.finding_id.encode()).hexdigest())
            expected[marker] = source_identity_for(record.round_id, finding.finding_id)
        for root in observation.roots:
            if not root.authenticated:
                diagnostics.append(BridgeDiagnostic("unauthenticated_root_ignored", "", f"root comment {root.comment_id} lacks authenticated reviewer provenance"))
                continue
            match = FINDING_ROOT_MARKER.search(root.body)
            identity = expected.get((match.group(1), match.group(2))) if match else None
            if identity is None:
                continue
            blocker = self._blocker_for_source(snapshot, identity)
            if blocker is None:
                continue
            comment_id = str(root.comment_id)
            if any(alias.alias_type == ROOT_ALIAS_TYPE and alias.alias_value == comment_id for alias in blocker.aliases):
                continue
            other_owners = [owner for owner in snapshot.get_blockers_for_alias(ROOT_ALIAS_TYPE, comment_id) if owner.blocker_id != blocker.blocker_id]
            if other_owners:
                diagnostics.append(BridgeDiagnostic("root_association_ambiguous", identity, f"root comment {comment_id} is already owned by another canonical blocker"))
                continue
            self._checkpoint("before_association_commit")
            try:
                snapshot = self.ledger.add_alias(
                    target.api_origin,
                    target.repository,
                    target.pr_number,
                    operation_id=f"accepted-finding-root:{_short_hash(identity, comment_id)}",
                    expected_ledger_revision=snapshot.ledger_revision,
                    blocker_id=blocker.blocker_id,
                    alias_type=ROOT_ALIAS_TYPE,
                    alias_value=comment_id,
                )
            except StaleLedgerRevisionError:
                raise
            except BlockerLedgerError as exc:
                diagnostics.append(BridgeDiagnostic("association_write_failed", identity, str(exc)))
                continue
            if root.thread_id and not any(a.alias_type == THREAD_ALIAS_TYPE and a.alias_value == root.thread_id for a in blocker.aliases):
                try:
                    snapshot = self.ledger.add_alias(
                        target.api_origin,
                        target.repository,
                        target.pr_number,
                        operation_id=f"accepted-finding-thread:{_short_hash(identity, root.thread_id)}",
                        expected_ledger_revision=snapshot.ledger_revision,
                        blocker_id=blocker.blocker_id,
                        alias_type=THREAD_ALIAS_TYPE,
                        alias_value=root.thread_id,
                    )
                except StaleLedgerRevisionError:
                    raise
                except BlockerLedgerError as exc:
                    diagnostics.append(BridgeDiagnostic("association_write_failed", identity, str(exc)))
        return self._ledger_snapshot(target)

    def _record_dispositions(
        self,
        target: ProjectionTarget,
        eligible: Sequence[tuple[StrongAuditRound, Finding]],
        snapshot: BlockerLedgerSnapshot,
        dispositions: Sequence[OrdinaryDisposition],
        diagnostics: list[BridgeDiagnostic],
        outcomes: list[DispositionOutcome],
    ) -> BlockerLedgerSnapshot:
        if not dispositions:
            return snapshot
        by_source = {source_identity_for(record.round_id, finding.finding_id): (record, finding) for record, finding in eligible}
        by_finding: dict[str, list[str]] = {}
        for known_identity, (_, known_finding) in by_source.items():
            by_finding.setdefault(known_finding.finding_id, []).append(known_identity)
        for disposition in dispositions:
            identity, problem = self._resolve_disposition(disposition, snapshot, by_source, by_finding)
            if identity is None:
                outcomes.append(DispositionOutcome(OUTCOME_AMBIGUOUS if problem == "ambiguous" else OUTCOME_UNRECOGNIZED, "", disposition.status, problem))
                continue
            record, finding = by_source[identity]
            blocker = self._blocker_for_source(snapshot, identity)
            if finding.status != OPEN:
                outcomes.append(DispositionOutcome(OUTCOME_ACCEPTED_CLOSURE_RETAINED, identity, disposition.status, f"accepted lifecycle state {finding.status} is retained"))
                continue
            if disposition.status == OrdinaryStatus.ADDRESSED.value:
                outcomes.append(DispositionOutcome(OUTCOME_CLOSURE_PROPOSAL_NOT_ACCEPTED, identity, disposition.status, "closure requires independent exact-finding acceptance by the owning lifecycle"))
                continue
            if blocker is None:
                outcomes.append(DispositionOutcome(OUTCOME_UNAVAILABLE, identity, disposition.status, "no canonical blocker is established for this finding"))
                continue
            well_formed = disposition.status == OrdinaryStatus.STILL_VALID.value and bool(disposition.rationale.strip()) and bool(disposition.evidence.strip())
            availability = EvidenceAvailability.KNOWN if well_formed else EvidenceAvailability.INCONCLUSIVE
            outcome = OUTCOME_STILL_VALID_OBSERVED if well_formed else OUTCOME_UNRESOLVED_RETAINED
            text = f"{disposition.status}: {disposition.rationale}\nEvidence: {disposition.evidence}".strip()
            self._checkpoint("before_association_commit")
            try:
                snapshot = self.ledger.record_evidence(
                    target.api_origin,
                    target.repository,
                    target.pr_number,
                    operation_id=f"accepted-finding-observation:{_short_hash(identity, target.head_sha, disposition.status, text)}",
                    expected_ledger_revision=snapshot.ledger_revision,
                    blocker_id=blocker.blocker_id,
                    evidence_availability=availability,
                    evidence=text,
                    reviewed_head_sha=disposition.head_sha or target.head_sha,
                    reviewed_base_sha=target.base_sha,
                    review_attempt_id=f"ordinary:{target.head_sha}",
                    review_observation_identity=identity,
                )
            except StaleLedgerRevisionError:
                raise
            except BlockerLedgerError as exc:
                diagnostics.append(BridgeDiagnostic("association_write_failed", identity, str(exc)))
                outcomes.append(DispositionOutcome(OUTCOME_UNAVAILABLE, identity, disposition.status, str(exc)))
                continue
            outcomes.append(DispositionOutcome(outcome, identity, disposition.status, text))
        return self._ledger_snapshot(target)

    @staticmethod
    def _resolve_disposition(
        disposition: OrdinaryDisposition,
        snapshot: BlockerLedgerSnapshot,
        by_source: dict[str, tuple[StrongAuditRound, Finding]],
        by_finding: dict[str, list[str]],
    ) -> tuple[Optional[str], str]:
        if disposition.status not in {item.value for item in OrdinaryStatus}:
            return None, "unrecognized disposition status"
        if disposition.source_identity:
            return (disposition.source_identity, "") if disposition.source_identity in by_source else (None, "unknown source identity")
        if disposition.finding_id:
            candidates = by_finding.get(disposition.finding_id, [])
            if len(candidates) == 1:
                return candidates[0], ""
            return None, "ambiguous" if candidates else "unknown finding id"
        owners: set[str] = set()
        if disposition.root_comment_id is not None:
            owners.update(blocker.blocker_id for blocker in snapshot.get_blockers_for_alias(ROOT_ALIAS_TYPE, str(disposition.root_comment_id)))
        if disposition.thread_id:
            owners.update(blocker.blocker_id for blocker in snapshot.get_blockers_for_alias(THREAD_ALIAS_TYPE, disposition.thread_id))
        if not owners:
            return None, "no authenticated association for this root/thread"
        # A compound or previously imported root can have non-Strong owners.
        # Resolve only the accepted Strong component; other owners retain their
        # independent scope and disposition in the ledger.
        sources = {identity for blocker_id in owners if (blocker := snapshot.get_blocker(blocker_id)) is not None for identity in by_source if any(a.alias_type == SOURCE_ALIAS_TYPE and a.alias_value == identity for a in blocker.aliases)}
        if len(sources) == 1:
            return next(iter(sources)), ""
        return None, "ambiguous" if sources else "associated blocker is not an accepted Strong finding"

    def _build_record(
        self,
        target: ProjectionTarget,
        source: PrReviewCycleSnapshot,
        round_record: StrongAuditRound,
        finding: Finding,
        ledger_snapshot: Optional[BlockerLedgerSnapshot],
        observation: Optional[RootObservation],
        diagnostics: list[BridgeDiagnostic],
        outcomes: Sequence[DispositionOutcome],
    ) -> AcceptedFindingRecord:
        identity = source_identity_for(round_record.round_id, finding.finding_id)
        blocker = self._blocker_for_source(ledger_snapshot, identity) if ledger_snapshot is not None else None
        in_epoch = round_record.open_epoch == source.open_epoch
        contract_ok = not target.contract_identity or target.contract_identity == round_record.contract_identity
        policy_ok = not target.policy_identity or target.policy_identity == round_record.policy_identity
        binding = BINDING_CURRENT if (in_epoch and contract_ok and policy_ok) else BINDING_RECONCILIATION_REQUIRED
        if in_epoch and not (contract_ok and policy_ok):
            diagnostics.append(BridgeDiagnostic("contract_or_policy_reconciliation_required", identity, "originating contract/policy identity differs from the current target"))
        root_ids = blocker.get_root_comment_ids() if blocker is not None else ()
        if blocker is None:
            association = AMBIGUOUS if any(d.code == "duplicate_source_association" and d.source_identity == identity for d in diagnostics) else UNAVAILABLE
        elif root_ids:
            association = ASSOCIATED
        elif round_record.publication_status != PUBLICATION_ACKNOWLEDGED:
            association = NOT_PUBLISHED
        elif any(d.code == "root_association_ambiguous" and d.source_identity == identity for d in diagnostics):
            association = AMBIGUOUS
        elif observation is not None and observation.complete:
            association = UNKNOWN
            diagnostics.append(BridgeDiagnostic("root_not_observed", identity, "published round has no uniquely associated native root"))
        else:
            association = UNKNOWN
        closed_head = finding.disposition_head_sha
        reference_head = closed_head if finding.status != OPEN and closed_head else round_record.head_sha
        return AcceptedFindingRecord(
            source_identity=identity,
            finding_id=finding.finding_id,
            round_id=round_record.round_id,
            api_origin=target.api_origin,
            repository=target.repository,
            pr_number=target.pr_number,
            open_epoch=round_record.open_epoch,
            originating_head_sha=round_record.head_sha,
            originating_base_sha=round_record.base_sha,
            contract_identity=round_record.contract_identity,
            policy_identity=round_record.policy_identity,
            qualified_requirements=tuple(_qualified(requirement_id, round_record.contract_snapshot.issue_ids) for requirement_id in finding.requirement_ids),
            requirement_ids=finding.requirement_ids,
            requirement_texts=finding.requirement_texts,
            original_scope=_original_scope(finding),
            affected_boundary=finding.affected_boundary,
            category=CATEGORY_REGRESSION_GAP if finding.is_regression_gap else CATEGORY_IMPLEMENTATION,
            evidence=finding.evidence,
            accepted_state=finding.status,
            closure_evidence=finding.disposition_evidence,
            closure_head_sha=closed_head,
            canonical_blocker_id=blocker.blocker_id if blocker is not None else "",
            ledger_disposition=blocker.disposition.value if blocker is not None else "",
            root_comment_ids=root_ids,
            association=association,
            target_binding=binding,
            evidence_currency=CURRENCY_CURRENT_HEAD if reference_head == target.head_sha else CURRENCY_HISTORICAL,
            current_observations=tuple(outcome for outcome in outcomes if outcome.source_identity == identity),
            source_revision=source.finding_set_revision,
            association_revision=blocker.last_updated_revision if blocker is not None else 0,
            plausible_incorrect_implementation=finding.plausible_incorrect_implementation,
            why_tests_admit_it=finding.why_tests_admit_it,
            material_consequence=finding.material_consequence,
            focused_regression_scenario=finding.focused_regression_scenario,
        )


def _empty_snapshot(repo_name: str, pr_number: int) -> PrReviewCycleSnapshot:
    """A successful authoritative read of a store holding no accepted state for the PR."""
    return PrReviewCycleSnapshot(
        repository=repo_name,
        pr_number=pr_number,
        phase="ORDINARY_REVIEW",
        transition_version=0,
        waiting_reason="",
        open_epoch=0,
        closed=False,
        ordinary_pass_head_sha="",
        ordinary_pass_base_sha="",
        ordinary_pass_contract_identity="",
        accepted_strong_round=None,
        active_claim=None,
        findings=(),
        open_findings=(),
        finding_set_revision=0,
        completion=None,
        accepted_closure=None,
        retry_not_before=0.0,
        attempt_error_reason="",
        pending_effect="",
        requires_new_strong_round=False,
    )


def known_gap_from_record(record: AcceptedFindingRecord) -> TestOracleGap:
    """Ordinary TOG representation of an accepted regression-gap finding.

    Only constructed for a record whose canonical identity is established; the
    original scope is carried verbatim and is never rewritten by rereview.
    """
    requirement_id = record.requirement_ids[0] if record.requirement_ids else ""
    anchor = _EVIDENCE_ANCHOR.search(record.evidence)
    return TestOracleGap(
        gap_id=record.known_gap_id,
        requirement_id=requirement_id,
        requirement_text=record.requirement_texts[0] if record.requirement_texts else "",
        authoritative_boundary=record.affected_boundary,
        invariant=record.original_scope,
        plausible_incorrect_implementation=record.plausible_incorrect_implementation,
        why_tests_still_pass=record.why_tests_admit_it,
        material_consequence=record.material_consequence,
        focused_regression_scenario=record.focused_regression_scenario,
        anchor_path=anchor.group(1) if anchor else "",
        anchor_line=int(anchor.group(2)) if anchor else None,
        discovery_phase="INITIAL",
        status="OPEN",
    )


def render_accepted_findings(projection: Optional[AcceptedFindingProjection]) -> str:
    """Render known accepted findings for the ordinary-review prompt."""
    if projection is None or not projection.records:
        return "(No accepted Strong audit findings are recorded for this PR.)"
    lines: list[str] = []
    if not projection.complete:
        lines.append("Projection completeness: INCOMPLETE — " + "; ".join(f"{d.code}" for d in projection.diagnostics))
    for record in projection.records:
        lines.append(f"- Accepted finding `{record.source_identity}` (canonical blocker `{record.canonical_blocker_id or 'unavailable'}`; state {record.accepted_state}; {record.target_binding}; evidence {record.evidence_currency})")
        lines.append(f"  Category: {record.category}; boundary: {record.affected_boundary}")
        for qualified, text in zip(record.requirement_ids, record.requirement_texts or ("",) * len(record.requirement_ids)):
            lines.append(f"  Requirement `{qualified}`: {text}")
        lines.append("  Original correction scope (authoritative; do not weaken or replace):")
        lines.extend(f"    {line}" for line in record.original_scope.splitlines())
        if record.known_gap_id and record.is_unresolved and record.target_binding == BINDING_CURRENT:
            lines.append(f"  Ordinary gap identity: `{record.known_gap_id}`")
    return "\n".join(lines)
