"""Durable, provider-independent two-tier PR review-cycle model.

Persists the ordinary-review -> independent strong-audit -> ordinary-closure
lifecycle for a repository/PR so audit coverage and finding closure remain
correct across repair commits, retries, restarts, and overlapping
controllers. This module owns no model calls, no GitHub review publication,
no repair dispatch, and no merge mutation: it is a pure transition/authority
boundary that production orchestration and a reviewer-execution adapter
consume through the public API below.

A "round" is identified by repository/PR plus the audited head H, the
reviewed base/provenance commit B, a contract snapshot identity M (the
resolved authoritative Issue Requirements text) and, for the strong tier
only, a strong-policy identity P (route/model/options/protocol version).
Timestamps, quota observations, duplicate wakes, and provider conversation
identifiers never participate in these identities.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .runtime_locks import ensure_lock_directory, lock_path

OPEN = "OPEN"
FIXED = "FIXED"
INVALID = "INVALID"
RETIRED = "RETIRED"

DELIVERY_NONE = "NONE"
DELIVERY_PENDING = "PENDING"
DELIVERY_UNKNOWN = "UNKNOWN"
DELIVERY_RETIRED = "RETIRED"

PUBLICATION_PENDING = "PENDING"
PUBLICATION_ACKNOWLEDGED = "ACKNOWLEDGED"

VERDICT_PASS = "PASS"
VERDICT_FINDINGS = "FINDINGS"

PHASE_ORDINARY_REVIEW = "ORDINARY_REVIEW"
PHASE_STRONG_PENDING = "STRONG_PENDING"
PHASE_STRONG_RUNNING = "STRONG_RUNNING"
PHASE_ORDINARY_CLOSURE = "ORDINARY_CLOSURE"
PHASE_COMPLETE = "COMPLETE"
PHASE_CLOSED = "CLOSED"

CLAIM_STRONG_AUDIT = "STRONG_AUDIT"


def _stable_identity(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


class ReviewCycleError(RuntimeError):
    """Base error for public transition-API rejections."""


class StaleTransitionError(ReviewCycleError):
    """Raised when a caller's expected transition version is no longer current."""


class NotApplicableError(ReviewCycleError):
    """Raised when a transition is attempted outside its authorized precondition."""


class UnknownClaimError(ReviewCycleError):
    """Raised when a result references a claim that is not the active one."""


@dataclass(frozen=True)
class ContractSnapshot:
    """M: the resolved authoritative Issue identities and their Requirements text."""

    issue_ids: Tuple[str, ...] = field(default_factory=tuple)
    requirements_text: str = ""

    @property
    def identity(self) -> str:
        return _stable_identity("contract", *sorted(self.issue_ids), self.requirements_text)


@dataclass(frozen=True)
class StrongPolicyIdentity:
    """P: the configured strong route, review-affecting model/options, and protocol version."""

    strong_route: str = ""
    model_options: str = ""
    protocol_version: str = ""

    @property
    def identity(self) -> str:
        return _stable_identity("policy", self.strong_route, self.model_options, self.protocol_version)


@dataclass(frozen=True)
class RoundProvenance:
    """The audited head H and reviewed base/provenance commit B for a round."""

    head_sha: str
    base_sha: str


@dataclass(frozen=True)
class Finding:
    """A durable, model-independent strong-audit finding."""

    finding_id: str
    origin_round_id: str
    requirement_ids: Tuple[str, ...]
    counterexample: str
    expected_behavior: str
    actual_behavior: str
    evidence: str
    affected_boundary: str
    is_regression_gap: bool = False
    plausible_incorrect_implementation: str = ""
    why_tests_admit_it: str = ""
    material_consequence: str = ""
    focused_regression_scenario: str = ""
    status: str = OPEN
    disposition_evidence: str = ""
    disposition_head_sha: str = ""
    delivery_status: str = DELIVERY_NONE


@dataclass(frozen=True)
class StrongAuditRound:
    """An accepted strong-audit result bound to its exact H/B/M/P identity."""

    round_id: str
    sequence: int
    head_sha: str
    base_sha: str
    contract_identity: str
    policy_identity: str
    reviewer_provenance: str
    verdict: str
    finding_ids: Tuple[str, ...] = field(default_factory=tuple)
    finding_set_revision: int = 0
    accepted_at: float = 0.0
    publication_status: str = PUBLICATION_PENDING


@dataclass(frozen=True)
class ClosureCertification:
    """An accepted ordinary-closure result for a repair head H2."""

    head_sha: str
    base_sha: str
    contract_identity: str
    policy_identity: str
    references_round_id: str
    finding_set_revision: int
    bounded: bool
    bounded_evidence: str
    accepted_at: float = 0.0


@dataclass(frozen=True)
class CompletionRecord:
    """The authorized completion of a review cycle for one exact head."""

    head_sha: str
    base_sha: str
    contract_identity: str
    policy_identity: str
    basis: str  # "STRONG_PASS" or "ORDINARY_CLOSURE"
    accepted_at: float = 0.0
    open_epoch: int = 0


@dataclass(frozen=True)
class ActiveClaim:
    """A fenced, in-progress phase claim (currently only the strong audit)."""

    claim_id: str
    phase: str
    head_sha: str
    base_sha: str
    contract_identity: str
    policy_identity: str
    based_on_version: int
    claimed_at: float = 0.0


@dataclass(frozen=True)
class FindingDisposition:
    """Caller-supplied disposition for one outstanding finding."""

    finding_id: str
    status: str
    evidence: str
    head_sha: str


@dataclass(frozen=True)
class PrReviewCycleSnapshot:
    """A read-only view for production orchestration to decide its next action."""

    repository: str
    pr_number: int
    phase: str
    transition_version: int
    waiting_reason: str
    open_epoch: int
    closed: bool
    ordinary_pass_head_sha: str
    ordinary_pass_base_sha: str
    ordinary_pass_contract_identity: str
    accepted_strong_round: Optional[StrongAuditRound]
    active_claim: Optional[ActiveClaim]
    open_findings: Tuple[Finding, ...]
    finding_set_revision: int
    completion: Optional[CompletionRecord]


class PrReviewCycleRepository:
    """Durably persist and fence the two-tier review-cycle lifecycle for one repo."""

    def __init__(self, repo_name: str, storage_path: Optional[Path] = None):
        self.repo_name = repo_name
        self.storage_path = storage_path or Path.home() / ".auto-coder" / repo_name / "pr_review_cycle.json"
        self.lock_path = lock_path(repo_name, self.storage_path, "pr-review-cycle-store")
        self.transition_lock_path = lock_path(repo_name, self.storage_path, "pr-review-cycle-transition")

    # -- storage plumbing -------------------------------------------------

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

    @contextmanager
    def serialized_transition(self) -> Iterator[None]:
        """Fence every accepted transition across overlapping controllers.

        Held for the whole read-validate-write span of a public transition
        call so a duplicate claim, a stale result, or a completion check
        cannot interleave with another controller's accepted transition.
        """
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_lock_directory(self.transition_lock_path)
        with self.transition_lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.transition_lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.storage_path.exists():
            return {"prs": {}}
        with self.storage_path.open(encoding="utf-8") as stream:
            state = json.load(stream)
        if not isinstance(state, dict) or not isinstance(state.get("prs"), dict):
            raise RuntimeError("PR review-cycle state is invalid")
        return state

    def _write(self, state: dict) -> None:
        temporary = self.storage_path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex}")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.storage_path)

    @staticmethod
    def _pr_key(pr_number: int) -> str:
        return str(pr_number)

    def _default_pr_state(self, pr_number: int) -> dict:
        return {
            "pr_number": pr_number,
            "open_epoch": 0,
            "closed": False,
            "transition_version": 0,
            "ordinary_pass": None,
            "strong_round_sequence": 0,
            "strong_rounds": [],
            "accepted_strong_round_id": "",
            "findings": {},
            "finding_set_revision": 0,
            "closures": [],
            "active_claim": None,
            "completion": None,
            "requires_new_strong_round": False,
        }

    # -- (de)serialization --------------------------------------------------

    def _finding_from_raw(self, raw: dict) -> Finding:
        return Finding(
            finding_id=str(raw["finding_id"]),
            origin_round_id=str(raw.get("origin_round_id", "")),
            requirement_ids=tuple(raw.get("requirement_ids", []) or []),
            counterexample=str(raw.get("counterexample", "")),
            expected_behavior=str(raw.get("expected_behavior", "")),
            actual_behavior=str(raw.get("actual_behavior", "")),
            evidence=str(raw.get("evidence", "")),
            affected_boundary=str(raw.get("affected_boundary", "")),
            is_regression_gap=bool(raw.get("is_regression_gap", False)),
            plausible_incorrect_implementation=str(raw.get("plausible_incorrect_implementation", "")),
            why_tests_admit_it=str(raw.get("why_tests_admit_it", "")),
            material_consequence=str(raw.get("material_consequence", "")),
            focused_regression_scenario=str(raw.get("focused_regression_scenario", "")),
            status=str(raw.get("status", OPEN)),
            disposition_evidence=str(raw.get("disposition_evidence", "")),
            disposition_head_sha=str(raw.get("disposition_head_sha", "")),
            delivery_status=str(raw.get("delivery_status", DELIVERY_NONE)),
        )

    def _strong_round_from_raw(self, raw: dict) -> StrongAuditRound:
        return StrongAuditRound(
            round_id=str(raw["round_id"]),
            sequence=int(raw["sequence"]),
            head_sha=str(raw["head_sha"]),
            base_sha=str(raw["base_sha"]),
            contract_identity=str(raw["contract_identity"]),
            policy_identity=str(raw["policy_identity"]),
            reviewer_provenance=str(raw.get("reviewer_provenance", "")),
            verdict=str(raw["verdict"]),
            finding_ids=tuple(raw.get("finding_ids", []) or []),
            finding_set_revision=int(raw.get("finding_set_revision", 0)),
            accepted_at=float(raw.get("accepted_at", 0.0)),
            publication_status=str(raw.get("publication_status", PUBLICATION_PENDING)),
        )

    def _active_claim_from_raw(self, raw: Optional[dict]) -> Optional[ActiveClaim]:
        if raw is None:
            return None
        return ActiveClaim(
            claim_id=str(raw["claim_id"]),
            phase=str(raw["phase"]),
            head_sha=str(raw["head_sha"]),
            base_sha=str(raw["base_sha"]),
            contract_identity=str(raw["contract_identity"]),
            policy_identity=str(raw["policy_identity"]),
            based_on_version=int(raw["based_on_version"]),
            claimed_at=float(raw.get("claimed_at", 0.0)),
        )

    def _completion_from_raw(self, raw: Optional[dict]) -> Optional[CompletionRecord]:
        if raw is None:
            return None
        return CompletionRecord(
            head_sha=str(raw["head_sha"]),
            base_sha=str(raw["base_sha"]),
            contract_identity=str(raw["contract_identity"]),
            policy_identity=str(raw.get("policy_identity", "")),
            basis=str(raw["basis"]),
            accepted_at=float(raw.get("accepted_at", 0.0)),
            open_epoch=int(raw.get("open_epoch", 0)),
        )

    # -- read-side helpers ----------------------------------------------

    def current_version(self, pr_number: int) -> int:
        with self._locked():
            state = self._read()
            pr_state = state["prs"].get(self._pr_key(pr_number))  # type: ignore[union-attr]
            if not isinstance(pr_state, dict):
                return 0
            return int(pr_state.get("transition_version", 0))

    def snapshot(self, pr_number: int) -> PrReviewCycleSnapshot:
        with self._locked():
            state = self._read()
            raw_pr = state["prs"].get(self._pr_key(pr_number))  # type: ignore[union-attr]
            if not isinstance(raw_pr, dict):
                raw_pr = self._default_pr_state(pr_number)
            return self._snapshot_from_raw(raw_pr)

    def _snapshot_from_raw(self, raw_pr: dict) -> PrReviewCycleSnapshot:
        findings_raw = raw_pr.get("findings", {})
        assert isinstance(findings_raw, dict)
        findings = {finding_id: self._finding_from_raw(raw) for finding_id, raw in findings_raw.items()}
        open_findings = tuple(sorted((f for f in findings.values() if f.status == OPEN), key=lambda f: f.finding_id))

        accepted_round_id = str(raw_pr.get("accepted_strong_round_id", ""))
        accepted_round = None
        for raw_round in raw_pr.get("strong_rounds", []) or []:
            assert isinstance(raw_round, dict)
            if raw_round.get("round_id") == accepted_round_id:
                accepted_round = self._strong_round_from_raw(raw_round)
                break

        ordinary_pass = raw_pr.get("ordinary_pass")
        ordinary_head = ordinary_base = ordinary_contract = ""
        if isinstance(ordinary_pass, dict):
            ordinary_head = str(ordinary_pass.get("head_sha", ""))
            ordinary_base = str(ordinary_pass.get("base_sha", ""))
            ordinary_contract = str(ordinary_pass.get("contract_identity", ""))

        active_claim = self._active_claim_from_raw(raw_pr.get("active_claim"))
        completion = self._completion_from_raw(raw_pr.get("completion"))
        closed = bool(raw_pr.get("closed", False))
        open_epoch = int(raw_pr.get("open_epoch", 0))
        applicable_completion = completion if completion is not None and completion.open_epoch == open_epoch else None
        requires_new_strong_round = bool(raw_pr.get("requires_new_strong_round", False))

        phase, waiting_reason = self._compute_phase(raw_pr, open_findings, accepted_round, active_claim, applicable_completion, closed, requires_new_strong_round)

        return PrReviewCycleSnapshot(
            repository=self.repo_name,
            pr_number=int(raw_pr["pr_number"]),
            phase=phase,
            transition_version=int(raw_pr.get("transition_version", 0)),
            waiting_reason=waiting_reason,
            open_epoch=int(raw_pr.get("open_epoch", 0)),
            closed=closed,
            ordinary_pass_head_sha=ordinary_head,
            ordinary_pass_base_sha=ordinary_base,
            ordinary_pass_contract_identity=ordinary_contract,
            accepted_strong_round=accepted_round,
            active_claim=active_claim,
            open_findings=open_findings,
            finding_set_revision=int(raw_pr.get("finding_set_revision", 0)),
            completion=applicable_completion,
        )

    @staticmethod
    def _compute_phase(
        raw_pr: dict,
        open_findings: Tuple[Finding, ...],
        accepted_round: Optional[StrongAuditRound],
        active_claim: Optional[ActiveClaim],
        completion: Optional[CompletionRecord],
        closed: bool,
        requires_new_strong_round: bool,
    ) -> Tuple[str, str]:
        if closed:
            return PHASE_CLOSED, "PR is closed; reopening requires fresh observations"
        if completion is not None:
            return PHASE_COMPLETE, ""
        if requires_new_strong_round:
            if isinstance(raw_pr.get("ordinary_pass"), dict):
                return PHASE_STRONG_PENDING, "prior closure was EXPANDED; a new independent strong round is required"
            return PHASE_ORDINARY_REVIEW, "prior closure was EXPANDED; awaiting a new applicable ordinary PASS"
        if accepted_round is not None and accepted_round.verdict == VERDICT_FINDINGS and open_findings:
            return PHASE_ORDINARY_CLOSURE, f"{len(open_findings)} strong finding(s) outstanding"
        if accepted_round is not None and accepted_round.verdict == VERDICT_FINDINGS and not open_findings:
            return PHASE_ORDINARY_CLOSURE, "all findings dispositioned; awaiting bounded closure certification"
        if active_claim is not None and active_claim.phase == CLAIM_STRONG_AUDIT:
            return PHASE_STRONG_RUNNING, "strong audit in progress"
        if isinstance(raw_pr.get("ordinary_pass"), dict):
            return PHASE_STRONG_PENDING, "ordinary PASS recorded; strong audit required"
        return PHASE_ORDINARY_REVIEW, "awaiting an applicable ordinary PASS"

    # -- transition guards -------------------------------------------------

    def _check_version(self, pr_state: dict, expected_version: Optional[int]) -> None:
        if expected_version is None:
            return
        current = int(pr_state.get("transition_version", 0))
        if current != expected_version:
            raise StaleTransitionError(f"Expected transition version {expected_version}, current is {current}")

    def _bump(self, pr_state: dict) -> None:
        pr_state["transition_version"] = int(pr_state.get("transition_version", 0)) + 1

    # -- public transition API ----------------------------------------------

    def record_ordinary_pass(
        self,
        pr_number: int,
        provenance: RoundProvenance,
        contract: ContractSnapshot,
        expected_version: Optional[int] = None,
    ) -> PrReviewCycleSnapshot:
        """Record an applicable ordinary PASS for the given H/B/M.

        This is the prerequisite for a required strong audit (REQ-003) and,
        after strong findings exist, the ordinary-convergence half of a
        closure certification for a repair head H2 (REQ-005).
        """
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.setdefault(self._pr_key(pr_number), self._default_pr_state(pr_number))
            self._check_version(pr_state, expected_version)
            if pr_state.get("closed"):
                raise NotApplicableError("Cannot record an ordinary PASS for a closed PR")
            pr_state["ordinary_pass"] = {
                "head_sha": provenance.head_sha,
                "base_sha": provenance.base_sha,
                "contract_identity": contract.identity,
                "open_epoch": pr_state.get("open_epoch", 0),
                "recorded_at": time.time(),
            }
            self._bump(pr_state)
            self._write(state)
            return self._snapshot_from_raw(pr_state)

    def claim_strong_audit(
        self,
        pr_number: int,
        provenance: RoundProvenance,
        contract: ContractSnapshot,
        policy: StrongPolicyIdentity,
        expected_version: Optional[int] = None,
    ) -> ActiveClaim:
        """Atomically claim the strong-audit phase for exactly one H/B/M/P.

        A duplicate claim for the same identity returns the existing claim
        unchanged (idempotent under retries/duplicate wakes). A claim for a
        newer identity supersedes any older in-progress claim so an older
        result cannot later publish authority (REQ-009).
        """
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.setdefault(self._pr_key(pr_number), self._default_pr_state(pr_number))
            self._check_version(pr_state, expected_version)
            if pr_state.get("closed"):
                raise NotApplicableError("Cannot claim a strong audit for a closed PR")

            ordinary_pass = pr_state.get("ordinary_pass")
            if not isinstance(ordinary_pass, dict):
                raise NotApplicableError("No applicable ordinary PASS recorded")
            if ordinary_pass.get("head_sha") != provenance.head_sha or ordinary_pass.get("base_sha") != provenance.base_sha or ordinary_pass.get("contract_identity") != contract.identity:
                raise NotApplicableError("Ordinary PASS does not apply to the requested H/B/M")

            existing_raw = pr_state.get("active_claim")
            if isinstance(existing_raw, dict) and existing_raw.get("phase") == CLAIM_STRONG_AUDIT:
                existing = self._active_claim_from_raw(existing_raw)
                assert existing is not None
                if existing.head_sha == provenance.head_sha and existing.base_sha == provenance.base_sha and existing.contract_identity == contract.identity and existing.policy_identity == policy.identity:
                    return existing

            claim = ActiveClaim(
                claim_id=uuid.uuid4().hex,
                phase=CLAIM_STRONG_AUDIT,
                head_sha=provenance.head_sha,
                base_sha=provenance.base_sha,
                contract_identity=contract.identity,
                policy_identity=policy.identity,
                based_on_version=int(pr_state.get("transition_version", 0)),
                claimed_at=time.time(),
            )
            pr_state["active_claim"] = {
                "claim_id": claim.claim_id,
                "phase": claim.phase,
                "head_sha": claim.head_sha,
                "base_sha": claim.base_sha,
                "contract_identity": claim.contract_identity,
                "policy_identity": claim.policy_identity,
                "based_on_version": claim.based_on_version,
                "claimed_at": claim.claimed_at,
            }
            self._bump(pr_state)
            self._write(state)
            return claim

    def abandon_claim(self, pr_number: int, claim_id: str) -> None:
        """Release a claim (deferred/errored attempt) without accepting a result."""
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.get(self._pr_key(pr_number))
            if not isinstance(pr_state, dict):
                return
            active_claim = pr_state.get("active_claim")
            if isinstance(active_claim, dict) and active_claim.get("claim_id") == claim_id:
                pr_state["active_claim"] = None
                self._bump(pr_state)
                self._write(state)

    def record_strong_result(
        self,
        pr_number: int,
        claim_id: str,
        verdict: str,
        reviewer_provenance: str,
        findings: Optional[List[Finding]] = None,
    ) -> StrongAuditRound:
        """Accept a strong-audit result for the claim that produced it.

        A result whose claim is not the currently active one is rejected: an
        older result arriving after a newer claim superseded it must not gain
        authority (REQ-009). Findings are appended as a new durable bundle;
        an already-known finding id is preserved rather than overwritten so
        the originating payload is never silently rewritten (REQ-004).
        """
        if verdict not in (VERDICT_PASS, VERDICT_FINDINGS):
            raise ValueError(f"Unknown strong-audit verdict: {verdict!r}")
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.get(self._pr_key(pr_number))
            if not isinstance(pr_state, dict):
                raise UnknownClaimError("No review-cycle state exists for this PR")
            active_claim = pr_state.get("active_claim")
            if not isinstance(active_claim, dict) or active_claim.get("claim_id") != claim_id or active_claim.get("phase") != CLAIM_STRONG_AUDIT:
                raise UnknownClaimError("Claim is not the currently active strong-audit claim")

            findings = findings or []
            if verdict == VERDICT_FINDINGS and not findings:
                raise ValueError("A FINDINGS verdict requires at least one finding")

            existing_findings = pr_state.get("findings", {})
            assert isinstance(existing_findings, dict)
            finding_set_revision = int(pr_state.get("finding_set_revision", 0))
            new_finding_ids: List[str] = []
            for finding in findings:
                if finding.finding_id in existing_findings:
                    # Preserve the originating payload; do not rewrite it.
                    continue
                existing_findings[finding.finding_id] = {
                    "finding_id": finding.finding_id,
                    "origin_round_id": finding.origin_round_id,
                    "requirement_ids": list(finding.requirement_ids),
                    "counterexample": finding.counterexample,
                    "expected_behavior": finding.expected_behavior,
                    "actual_behavior": finding.actual_behavior,
                    "evidence": finding.evidence,
                    "affected_boundary": finding.affected_boundary,
                    "is_regression_gap": finding.is_regression_gap,
                    "plausible_incorrect_implementation": finding.plausible_incorrect_implementation,
                    "why_tests_admit_it": finding.why_tests_admit_it,
                    "material_consequence": finding.material_consequence,
                    "focused_regression_scenario": finding.focused_regression_scenario,
                    "status": OPEN,
                    "disposition_evidence": "",
                    "disposition_head_sha": "",
                    "delivery_status": DELIVERY_NONE,
                }
                new_finding_ids.append(finding.finding_id)
            if new_finding_ids:
                finding_set_revision += 1

            sequence = int(pr_state.get("strong_round_sequence", 0)) + 1
            round_record = StrongAuditRound(
                round_id=uuid.uuid4().hex,
                sequence=sequence,
                head_sha=str(active_claim["head_sha"]),
                base_sha=str(active_claim["base_sha"]),
                contract_identity=str(active_claim["contract_identity"]),
                policy_identity=str(active_claim["policy_identity"]),
                reviewer_provenance=reviewer_provenance,
                verdict=verdict,
                finding_ids=tuple(new_finding_ids),
                finding_set_revision=finding_set_revision,
                accepted_at=time.time(),
                publication_status=PUBLICATION_PENDING,
            )
            pr_state["strong_round_sequence"] = sequence
            strong_rounds = pr_state.setdefault("strong_rounds", [])
            assert isinstance(strong_rounds, list)
            strong_rounds.append(
                {
                    "round_id": round_record.round_id,
                    "sequence": round_record.sequence,
                    "head_sha": round_record.head_sha,
                    "base_sha": round_record.base_sha,
                    "contract_identity": round_record.contract_identity,
                    "policy_identity": round_record.policy_identity,
                    "reviewer_provenance": round_record.reviewer_provenance,
                    "verdict": round_record.verdict,
                    "finding_ids": list(round_record.finding_ids),
                    "finding_set_revision": round_record.finding_set_revision,
                    "accepted_at": round_record.accepted_at,
                    "publication_status": round_record.publication_status,
                }
            )
            pr_state["accepted_strong_round_id"] = round_record.round_id
            pr_state["finding_set_revision"] = finding_set_revision
            pr_state["active_claim"] = None
            pr_state["requires_new_strong_round"] = False
            if verdict == VERDICT_FINDINGS:
                # A repair is now required; the prior ordinary PASS no
                # longer applies to whatever new head follows.
                pr_state["ordinary_pass"] = None
            self._bump(pr_state)
            self._write(state)
            return round_record

    def acknowledge_publication(self, pr_number: int, round_id: str) -> None:
        """Confirm that the strong-audit review (and any thread bookkeeping) published.

        Idempotent: acknowledging an already-acknowledged round is a no-op.
        """
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.get(self._pr_key(pr_number))
            if not isinstance(pr_state, dict):
                raise UnknownClaimError("No review-cycle state exists for this PR")
            for raw_round in pr_state.get("strong_rounds", []) or []:
                assert isinstance(raw_round, dict)
                if raw_round.get("round_id") == round_id:
                    if raw_round.get("publication_status") == PUBLICATION_ACKNOWLEDGED:
                        return
                    raw_round["publication_status"] = PUBLICATION_ACKNOWLEDGED
                    self._bump(pr_state)
                    self._write(state)
                    return
            raise UnknownClaimError("Unknown strong-audit round identity")

    def set_finding_delivery_status(self, pr_number: int, finding_id: str, delivery_status: str) -> None:
        """Record the repair-message delivery state for a finding (production concern)."""
        if delivery_status not in (DELIVERY_NONE, DELIVERY_PENDING, DELIVERY_UNKNOWN, DELIVERY_RETIRED):
            raise ValueError(f"Unknown delivery status: {delivery_status!r}")
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.get(self._pr_key(pr_number))
            if not isinstance(pr_state, dict):
                raise UnknownClaimError("No review-cycle state exists for this PR")
            findings = pr_state.get("findings", {})
            assert isinstance(findings, dict)
            raw_finding = findings.get(finding_id)
            if not isinstance(raw_finding, dict):
                raise UnknownClaimError("Unknown finding identity")
            raw_finding["delivery_status"] = delivery_status
            self._bump(pr_state)
            self._write(state)

    def certify_closure(
        self,
        pr_number: int,
        provenance: RoundProvenance,
        contract: ContractSnapshot,
        policy: StrongPolicyIdentity,
        references_round_id: str,
        finding_set_revision: int,
        dispositions: List[FindingDisposition],
        bounded: bool,
        bounded_evidence: str,
        new_findings: Optional[List[Finding]] = None,
        expected_version: Optional[int] = None,
    ) -> PrReviewCycleSnapshot:
        """Certify an ordinary-closure result for repair head H2 (REQ-005/REQ-006).

        Completes the cycle for H2 without another strong audit only when
        every precondition holds: the accepted strong round and finding-set
        revision are unchanged, B/M/P are unchanged from that round, an
        applicable ordinary PASS exists for H2, every outstanding finding has
        an evidence-backed FIXED/INVALID disposition, and the cumulative
        repair is BOUNDED. `new_findings` discovered during closure join the
        tracked obligations rather than being suppressed; if any remain OPEN
        after applying dispositions, the cycle does not complete.
        """
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.get(self._pr_key(pr_number))
            if not isinstance(pr_state, dict):
                raise NotApplicableError("No review-cycle state exists for this PR")
            self._check_version(pr_state, expected_version)
            if pr_state.get("closed"):
                raise NotApplicableError("Cannot certify closure for a closed PR")
            if pr_state.get("requires_new_strong_round"):
                raise NotApplicableError("A prior closure was EXPANDED; a new independent strong round is required first")

            if pr_state.get("accepted_strong_round_id") != references_round_id:
                raise NotApplicableError("Closure does not reference the currently accepted strong round")
            if int(pr_state.get("finding_set_revision", 0)) != finding_set_revision:
                raise NotApplicableError("Closure references a stale finding-set revision")

            accepted_round_raw = None
            for raw_round in pr_state.get("strong_rounds", []) or []:
                assert isinstance(raw_round, dict)
                if raw_round.get("round_id") == references_round_id:
                    accepted_round_raw = raw_round
                    break
            if accepted_round_raw is None:
                raise NotApplicableError("Referenced strong round does not exist")
            if accepted_round_raw.get("base_sha") != provenance.base_sha or accepted_round_raw.get("contract_identity") != contract.identity or accepted_round_raw.get("policy_identity") != policy.identity:
                raise NotApplicableError("Base commit, contract, or strong policy changed since the accepted strong round")

            ordinary_pass = pr_state.get("ordinary_pass")
            if not isinstance(ordinary_pass, dict) or ordinary_pass.get("head_sha") != provenance.head_sha or ordinary_pass.get("base_sha") != provenance.base_sha or ordinary_pass.get("contract_identity") != contract.identity:
                raise NotApplicableError("No applicable ordinary PASS recorded for the repair head")

            findings = pr_state.get("findings", {})
            assert isinstance(findings, dict)

            for new_finding in new_findings or []:
                if new_finding.finding_id in findings:
                    continue
                findings[new_finding.finding_id] = {
                    "finding_id": new_finding.finding_id,
                    "origin_round_id": new_finding.origin_round_id or references_round_id,
                    "requirement_ids": list(new_finding.requirement_ids),
                    "counterexample": new_finding.counterexample,
                    "expected_behavior": new_finding.expected_behavior,
                    "actual_behavior": new_finding.actual_behavior,
                    "evidence": new_finding.evidence,
                    "affected_boundary": new_finding.affected_boundary,
                    "is_regression_gap": new_finding.is_regression_gap,
                    "plausible_incorrect_implementation": new_finding.plausible_incorrect_implementation,
                    "why_tests_admit_it": new_finding.why_tests_admit_it,
                    "material_consequence": new_finding.material_consequence,
                    "focused_regression_scenario": new_finding.focused_regression_scenario,
                    "status": OPEN,
                    "disposition_evidence": "",
                    "disposition_head_sha": "",
                    "delivery_status": DELIVERY_NONE,
                }
                pr_state["finding_set_revision"] = int(pr_state.get("finding_set_revision", 0)) + 1

            for disposition in dispositions:
                if disposition.status not in (FIXED, INVALID):
                    raise ValueError(f"Disposition status must be FIXED or INVALID, got {disposition.status!r}")
                if not disposition.evidence:
                    raise ValueError("A finding disposition requires evidence")
                raw_finding = findings.get(disposition.finding_id)
                if not isinstance(raw_finding, dict):
                    raise NotApplicableError(f"Disposition references unknown finding {disposition.finding_id!r}")
                if raw_finding.get("status") != OPEN:
                    continue
                raw_finding["status"] = disposition.status
                raw_finding["disposition_evidence"] = disposition.evidence
                raw_finding["disposition_head_sha"] = disposition.head_sha
                if raw_finding.get("delivery_status") in (DELIVERY_PENDING, DELIVERY_UNKNOWN):
                    raw_finding["delivery_status"] = DELIVERY_RETIRED

            outstanding = [raw for raw in findings.values() if isinstance(raw, dict) and raw.get("status") == OPEN]

            if not bounded:
                # EXPANDED (or unproven bounded) repair scope requires a new
                # independent strong round on the current ordinary-passed H2.
                closures = pr_state.setdefault("closures", [])
                assert isinstance(closures, list)
                closures.append(
                    {
                        "head_sha": provenance.head_sha,
                        "base_sha": provenance.base_sha,
                        "contract_identity": contract.identity,
                        "policy_identity": policy.identity,
                        "references_round_id": references_round_id,
                        "finding_set_revision": int(pr_state.get("finding_set_revision", 0)),
                        "bounded": False,
                        "bounded_evidence": bounded_evidence,
                        "accepted_at": time.time(),
                    }
                )
                pr_state["requires_new_strong_round"] = True
                self._bump(pr_state)
                self._write(state)
                return self._snapshot_from_raw(pr_state)
            if not bounded_evidence:
                raise ValueError("A bounded closure requires reviewer-produced cumulative-diff evidence")

            closure = ClosureCertification(
                head_sha=provenance.head_sha,
                base_sha=provenance.base_sha,
                contract_identity=contract.identity,
                policy_identity=policy.identity,
                references_round_id=references_round_id,
                finding_set_revision=int(pr_state.get("finding_set_revision", 0)),
                bounded=bounded,
                bounded_evidence=bounded_evidence,
                accepted_at=time.time(),
            )
            closures = pr_state.setdefault("closures", [])
            assert isinstance(closures, list)
            closures.append(
                {
                    "head_sha": closure.head_sha,
                    "base_sha": closure.base_sha,
                    "contract_identity": closure.contract_identity,
                    "policy_identity": closure.policy_identity,
                    "references_round_id": closure.references_round_id,
                    "finding_set_revision": closure.finding_set_revision,
                    "bounded": closure.bounded,
                    "bounded_evidence": closure.bounded_evidence,
                    "accepted_at": closure.accepted_at,
                }
            )

            if not outstanding:
                pr_state["completion"] = {
                    "head_sha": provenance.head_sha,
                    "base_sha": provenance.base_sha,
                    "contract_identity": contract.identity,
                    "policy_identity": policy.identity,
                    "basis": "ORDINARY_CLOSURE",
                    "accepted_at": time.time(),
                    "open_epoch": pr_state.get("open_epoch", 0),
                }

            self._bump(pr_state)
            self._write(state)
            return self._snapshot_from_raw(pr_state)

    def accept_strong_pass_completion(
        self,
        pr_number: int,
        round_id: str,
        expected_version: Optional[int] = None,
    ) -> PrReviewCycleSnapshot:
        """Authorize completion from an accepted, published strong PASS.

        Requires the referenced round to still be the accepted round, carry
        verdict PASS, and have a confirmed publication before merge authority
        is granted (REQ-003).
        """
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.get(self._pr_key(pr_number))
            if not isinstance(pr_state, dict):
                raise NotApplicableError("No review-cycle state exists for this PR")
            self._check_version(pr_state, expected_version)
            if pr_state.get("closed"):
                raise NotApplicableError("Cannot authorize completion for a closed PR")
            if pr_state.get("accepted_strong_round_id") != round_id:
                raise NotApplicableError("Round is not the currently accepted strong round")

            round_raw = None
            for raw_round in pr_state.get("strong_rounds", []) or []:
                assert isinstance(raw_round, dict)
                if raw_round.get("round_id") == round_id:
                    round_raw = raw_round
                    break
            if round_raw is None:
                raise NotApplicableError("Unknown strong-audit round identity")
            if round_raw.get("verdict") != VERDICT_PASS:
                raise NotApplicableError("Referenced round is not a PASS")
            if round_raw.get("publication_status") != PUBLICATION_ACKNOWLEDGED:
                raise NotApplicableError("Strong-audit publication is not yet confirmed")

            pr_state["completion"] = {
                "head_sha": round_raw["head_sha"],
                "base_sha": round_raw["base_sha"],
                "contract_identity": round_raw["contract_identity"],
                "policy_identity": round_raw["policy_identity"],
                "basis": "STRONG_PASS",
                "accepted_at": time.time(),
                "open_epoch": pr_state.get("open_epoch", 0),
            }
            self._bump(pr_state)
            self._write(state)
            return self._snapshot_from_raw(pr_state)

    def is_completion_authorized(self, pr_number: int, head_sha: str) -> bool:
        """Return whether completion is currently authorized for exactly this head."""
        snapshot = self.snapshot(pr_number)
        if snapshot.closed or snapshot.completion is None:
            return False
        if snapshot.completion.open_epoch != snapshot.open_epoch:
            return False
        return snapshot.completion.head_sha == head_sha

    def mark_closed(self, pr_number: int) -> None:
        """Record that the PR is closed; it grants no dispatch or merge authority."""
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.setdefault(self._pr_key(pr_number), self._default_pr_state(pr_number))
            pr_state["closed"] = True
            self._bump(pr_state)
            self._write(state)

    def mark_reopened(self, pr_number: int) -> None:
        """Record that the PR reopened: any reuse now requires fresh observations.

        Advances the open epoch so a completion or claim accepted under the
        prior opening can never again authorize an effect, even though its
        historical record is retained for audit purposes.
        """
        with self.serialized_transition(), self._locked():
            state = self._read()
            prs = state["prs"]
            assert isinstance(prs, dict)
            pr_state = prs.setdefault(self._pr_key(pr_number), self._default_pr_state(pr_number))
            pr_state["closed"] = False
            pr_state["open_epoch"] = int(pr_state.get("open_epoch", 0)) + 1
            pr_state["ordinary_pass"] = None
            pr_state["active_claim"] = None
            self._bump(pr_state)
            self._write(state)
