"""Durable, generation-bound authorization for Issue implementation."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterator, Optional

from loguru import logger

from .github_pending_work import WorkIdentity, get_pending_work_store
from .invocation_admission import bind_invocation_target, take_pending_invocation_handle
from .issue_review_publication import find_confirmed_publication
from .issue_review_rerun import IssueReviewRerunStore, RerunAuthorityUnavailable, ReviewSubject
from .objective_evidence import ObjectiveAnchorStore
from .prompt_loader import load_prompts
from .reissue_required_store import ReissueRequiredStore
from .requirement_contract import NormativeIssueManifest, build_normative_issue_manifest
from .runtime_locks import ensure_lock_directory, lock_path
from .specification_analyzer import (
    SPECIFICATION_FINDING_CATEGORIES,
    FindingDisposition,
    IncrementalReviewContext,
    IndividualRelationshipContext,
    IndividualReviewEvidence,
    ReviewCoverage,
    SpecificationAnalysisResult,
    SpecificationFinding,
    analyze_issue_specification,
    individual_relationship_context,
    individual_review_evidence,
    objective_integrity_result,
)
from .specification_repair_rounds import RepairRoundApplication, SpecificationRepairRoundStore
from .util.gh_cache import IMPLEMENTATION_READY_LABEL, is_implementation_ready
from .util.github_request_outcome import GitHubRequestError

VALIDATION_SCHEMA_VERSION = "issue-specification-validation-v8-retained-findings"
FINDINGS_MARKER_PREFIX = "auto-coder-specification-validation"

# Pending-work stage for the two independently-trackable BLOCKED publication
# effects (Issue #1923): a diagnostic findings comment and, when required, the
# associated implementation-ready withdrawal. Both are durably completed via
# ``PendingWorkStore.complete_effect`` as each succeeds, so a controller
# restart or a governed retry after one effect fails never re-sends the other
# and never forgets the still-missing one.
VALIDATION_PUBLICATION_STAGE = "validation-publication"
DIAGNOSTIC_EFFECT = "diagnostic"
READINESS_WITHDRAWAL_EFFECT = "readiness-withdrawal"


def validation_publication_identity(repository: str, issue_number: int, decision_identity_key: str) -> WorkIdentity:
    """Durable obligation identity for one BLOCKED decision's publication effects."""
    return WorkIdentity(repository, f"issue:{issue_number}", VALIDATION_PUBLICATION_STAGE, decision_identity_key)


def publication_trusted_complete(decision: object) -> bool:
    """Whether a durable ``findings_published`` flag needs no re-verification.

    True for a pre-App-routing legacy record (``publication_schema_version
    == 0``) and for a post-routing record carrying its confirmed receipt. A
    tagged post-change record with a missing/corrupt receipt is not trusted:
    it falls through to authoritative re-verification against live comments
    instead of being silently grandfathered as historical success (Issue
    #2026, REQ-008).
    """
    if not getattr(decision, "findings_published", False):
        return False
    if getattr(decision, "publication_schema_version", 0) == 0:
        return True
    return getattr(decision, "publication_receipt", None) is not None


def configured_provider_identity() -> str:
    """Return the effective validator route and models (never credentials).

    This is execution *provenance*, not semantic *policy* identity (Issue
    #2081, REQ-001/REQ-005): it must never be folded into
    ``validation_policy_identity()``/``ValidationIdentity.policy_identity``,
    only captured fresh at the moment a decision is actually produced by a
    real analyzer call, so a durable READY/BLOCKED decision remains reusable
    across backend/model/fallback changes.
    """
    override = os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY")
    if override:
        return override
    from .llm_backend_config import get_llm_config

    # This is purely diagnostic provenance now (Issue #2081, REQ-005): unlike
    # before, `decide()` calls this on every fresh model-backed decision, not
    # only once when an `AutomationEngine` binds a repository's lifecycle. A
    # configuration lookup that is unavailable or shaped unexpectedly (for
    # example a caller's narrowly-scoped test double for `get_llm_config`)
    # must never abort or alter the actual review outcome, so any failure
    # here degrades to an honest "unavailable" marker instead of propagating.
    try:
        config = get_llm_config()
        if config is None:
            return "unconfigured"
        order = config.get_adversarial_validation_backend_order()
        if not order:
            default = config.get_adversarial_validation_default_backend()
            order = [default] if default else []
        if not order:
            getter = getattr(config, "get_high_score_backend_order", None)
            order = getter() if callable(getter) else list(getattr(config, "backend_with_high_score_order", []) or [])
        route = []
        for name in order:
            backend = config.get_backend_config(name)
            route.append(
                {
                    "alias": name,
                    "provider": (backend.backend_type or backend.name) if backend is not None else name,
                    "model": config.get_model_for_backend(name),
                }
            )
        return json.dumps(route, sort_keys=True, separators=(",", ":"))
    except Exception as exc:
        logger.debug(f"configured_provider_identity: route lookup unavailable ({exc}); using 'unavailable' provenance")
        return "unavailable"


@dataclass(frozen=True)
class ValidationIdentity:
    repository: str
    issue_number: int
    specification_digest: str
    policy_identity: str
    relationship_digest: str = "standalone"
    # Canonical, exact caller-supplied manifest binding.  Keeping the complete
    # value in the identity (rather than only its digest) makes the durable
    # authorization self-describing and prevents reconstruction from Markdown.
    manifest_binding: str = ""

    @property
    def key(self) -> str:
        value = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RetainedFinding:
    """One stable finding retained after resolution for recurrence detection."""

    finding_id: str
    finding: SpecificationFinding
    disposition: str
    reasoning: str


@dataclass(frozen=True)
class ValidationDecision:
    identity: ValidationIdentity
    verdict: str
    findings: tuple[SpecificationFinding, ...] = ()
    findings_published: bool = False
    readiness_removed: bool = False
    remediation: str = "NONE"
    remediation_reason: Optional[str] = None
    evaluation_source: str = "model"
    # Publication provenance (Issue #2026, REQ-008). A record predating this
    # change is never written with a nonzero version, so ``0`` durably means
    # "findings_published was set by the pre-App-routing code path" and must
    # never be relabeled as proven reviewer-App authorship. ``1`` means this
    # code initialized versioned ownership before handling the record;
    # ``publication_receipt`` is only trustworthy when both are set.
    publication_schema_version: int = 0
    publication_receipt: Optional[dict] = None
    # Execution provenance (REQ-005): the exact `configured_provider_identity()`
    # route snapshot captured at the moment a real analyzer call actually
    # produced this decision. ``None`` for local-only decisions (no analyzer
    # call was made) and for legacy decisions persisted before this field
    # existed. Reusing a stored decision must preserve this value verbatim;
    # it must never be recomputed or relabeled with the current configuration.
    execution_provenance: Optional[str] = None
    # Diagnostic only (REQ-006/REQ-009): the number of pre-migration on-disk
    # records that share this decision's non-policy identity fields but carry
    # a different (opaque, provider-mixed) `policy_identity`. Always 0 for a
    # stored-decision reuse (no legacy scan is performed on a hit) and for a
    # decision that never reached the legacy-candidate scan. Never used to
    # authorize reuse.
    legacy_candidates_detected: int = 0
    rerun_authority: int = 0
    rerun_request_id: Optional[str] = None
    review_mode: str = "FULL"
    predecessor_decision_key: Optional[str] = None
    assessed_delta: str = ""
    finding_ids: tuple[str, ...] = ()
    finding_dispositions: tuple[FindingDisposition, ...] = ()
    coverage: tuple[ReviewCoverage, ...] = ()
    review_inputs: str = ""
    accepted_sequence: int = 0
    retained_findings: tuple[RetainedFinding, ...] = ()


def _contract_evidence(manifest: NormativeIssueManifest, title: str, body: str) -> str:
    value = {
        "issue_number": manifest.issue_number,
        "title": title,
        "body": body,
        "requirements": [{"requirement_id": item.requirement_id, "text": item.text} for item in manifest.requirements],
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def manifest_binding(manifest: NormativeIssueManifest) -> str:
    """Return the exact ordered authoritative-manifest binding for reuse."""
    value = {
        "issue_number": manifest.issue_number,
        "title": manifest.title,
        "explicit_contract_present": manifest.explicit_contract_present,
        "explicit_contract_valid": manifest.explicit_contract_valid,
        "requirements": [{"requirement_id": item.requirement_id, "text": item.text} for item in manifest.requirements],
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _review_inputs(
    manifest: NormativeIssueManifest,
    title: str,
    body: str,
    relationship: IndividualRelationshipContext,
    evidence: IndividualReviewEvidence,
) -> str:
    """Retain every semantic input needed to assess a later edit losslessly."""
    value = {
        "manifest": json.loads(manifest_binding(manifest)),
        "title": title,
        "body": body,
        "role": relationship.role,
        "parent_number": relationship.parent_number,
        "related_contracts": relationship.related_contracts,
        "objective": asdict(evidence.objective) if evidence.objective is not None else None,
        "baseline": evidence.baseline,
        "prior_applied_outcomes": list(evidence.prior_applied_outcomes),
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _lossless_delta(before: str, after: str) -> str:
    old = json.loads(before)
    new = json.loads(after)
    fields = sorted(set(old) | set(new))
    return json.dumps(
        {field: {"before": old.get(field), "after": new.get(field)} for field in fields if old.get(field) != new.get(field)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _predecessor_evidence(decision: ValidationDecision) -> str:
    value = {
        "decision_key": decision.identity.key,
        "review_inputs": json.loads(decision.review_inputs),
        "verdict": decision.verdict,
        "findings": [
            {
                "finding_id": item.finding_id,
                **asdict(item.finding),
                "last_disposition": item.disposition,
                "last_reasoning": item.reasoning,
            }
            for item in decision.retained_findings
        ],
        "finding_dispositions": [asdict(item) for item in decision.finding_dispositions],
        "coverage": [asdict(item) for item in decision.coverage],
        "execution_provenance": decision.execution_provenance,
        "accepted_sequence": decision.accepted_sequence,
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class IndividualReviewHistoryStore:
    """Atomic per-Issue baseline and applied BLOCKED-review history."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        state_root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or state_root / repository / "individual_review_history.json"
        self.repository = repository

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Individual-review history root must be a JSON object")
            return value
        except FileNotFoundError:
            return {}

    def evidence(self, issue_number: int, contract: str) -> IndividualReviewEvidence:
        """Create the immutable first valid baseline and return prior outcomes."""
        key = str(issue_number)
        with self._locked():
            state = self._read()
            raw = state.get(key)
            if raw is None:
                raw = {"baseline": contract, "applied_outcomes": []}
                state[key] = raw
                self._write(state)
            elif isinstance(raw, dict) and "baseline" not in raw:
                # A complete-set review may have captured the shared Objective
                # before this Issue's first individual review.
                raw["baseline"] = contract
                raw.setdefault("applied_outcomes", [])
                self._write(state)
            if not isinstance(raw, dict) or not isinstance(raw.get("baseline"), str):
                raise ValueError(f"Invalid individual-review history for Issue #{issue_number}")
            outcomes = raw.get("applied_outcomes", [])
            if not isinstance(outcomes, list) or any(not isinstance(item, str) for item in outcomes):
                raise ValueError(f"Invalid applied individual-review outcomes for Issue #{issue_number}")
            valid = tuple(outcomes)
            return IndividualReviewEvidence(str(raw["baseline"]), valid)

    def record_applied(self, issue_number: int, identity_key: str, outcome: str) -> None:
        key = str(issue_number)
        with self._locked():
            state = self._read()
            raw = state.get(key)
            if not isinstance(raw, dict):
                return
            applied = raw.setdefault("applied_outcomes", [])
            applied_keys = raw.setdefault("applied_identity_keys", [])
            if not isinstance(applied, list) or not isinstance(applied_keys, list) or identity_key in applied_keys:
                return
            applied.append(outcome)
            applied_keys.append(identity_key)
            self._write(state)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        runtime_path = lock_path(self.repository, self.path, "individual-review-history")
        ensure_lock_directory(runtime_path)
        with runtime_path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _write(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)


def specification_digest(title: str, body: str) -> str:
    """Digest exact authoritative fields without ambiguous concatenation."""
    encoded = json.dumps([title, body], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validation_policy_identity() -> str:
    """Identify every semantic input that can alter the individual review decision.

    Execution routing (configured/selected backend, alias, model, fallback
    order/membership, quota-based selection) never participates here
    (Issue #2081, REQ-001): only the versioned review contract, exact prompt,
    allowed finding categories, and result-schema/consistency rules do
    (REQ-002). ``VALIDATION_SCHEMA_VERSION`` is bumped whenever this contract
    changes in a decision-affecting way, including this migration itself.
    """
    issue_prompts = load_prompts().get("issue")
    prompt = issue_prompts.get("adversarial_specification_analysis") if isinstance(issue_prompts, dict) else None
    contract = {
        "version": VALIDATION_SCHEMA_VERSION,
        "prompt": prompt,
        "categories": sorted(SPECIFICATION_FINDING_CATEGORIES),
        "result_fields": ["verdict", "remediation", "findings"],
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


_IDENTITY_LOCKS: dict[str, threading.Lock] = {}
_IDENTITY_LOCKS_GUARD = threading.Lock()


class SpecificationValidationStore:
    """Atomic JSON store for completed READY/BLOCKED decisions."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        state_root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or state_root / repository / "specification_validations.json"
        self.repository = repository
        rerun_path = path.with_name("issue_review_reruns.sqlite3") if path is not None else None
        self.reruns = IssueReviewRerunStore(rerun_path)

    def _read(self) -> dict[str, object]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        """Serialize one identity in this process and across daemon processes."""
        import fcntl

        runtime_path = lock_path(self.repository, self.path, "specification-validation", key)
        with _IDENTITY_LOCKS_GUARD:
            lock = _IDENTITY_LOCKS.setdefault(str(runtime_path), threading.Lock())
        with lock:
            ensure_lock_directory(runtime_path)
            with runtime_path.open("a", encoding="utf-8") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def get(self, identity: ValidationIdentity) -> Optional[ValidationDecision]:
        raw = self._read().get(identity.key)
        if not isinstance(raw, dict) or raw.get("verdict") not in {"READY", "BLOCKED"}:
            return None
        if raw.get("identity") != asdict(identity):
            return None
        authority, _request_id, _state = self.reruns.authority(ReviewSubject(self.repository, "individual", identity.issue_number))
        if int(raw.get("rerun_authority") or 0) != authority:
            return None
        if not self._raw_has_complete_evidence(raw, identity):
            return None
        return self._decision_from_raw(identity, raw, evaluation_source="stored-decision-reuse")

    @staticmethod
    def _raw_has_complete_evidence(raw: dict[str, object], identity: ValidationIdentity) -> bool:
        """Require the complete evidence envelope before a record can authorize reuse."""
        try:
            inputs = json.loads(raw["review_inputs"] if isinstance(raw["review_inputs"], str) else "")
            binding = json.loads(identity.manifest_binding)
            expected = {str(item["requirement_id"]) for item in binding["requirements"]} | {"contract-wide"}
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        coverage = raw.get("coverage")
        dispositions = raw.get("finding_dispositions")
        retained = raw.get("retained_findings")
        finding_ids = raw.get("finding_ids")
        findings = raw.get("findings")
        if (
            not isinstance(inputs, dict)
            or not isinstance(coverage, list)
            or not isinstance(dispositions, list)
            or not isinstance(retained, list)
            or not isinstance(finding_ids, list)
            or not isinstance(findings, list)
            or len(finding_ids) != len(findings)
            or any(not isinstance(item, str) for item in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)
            or any(
                not isinstance(item, dict)
                or set(item) != {"finding_id", "disposition", "reasoning"}
                or not isinstance(item.get("finding_id"), str)
                or item.get("disposition") not in {"RESOLVED", "STILL_VALID", "REGRESSED", "UNVERIFIED"}
                or not isinstance(item.get("reasoning"), str)
                or not str(item.get("reasoning")).strip()
                for item in dispositions
            )
            or any(
                not isinstance(item, dict)
                or set(item) != {"finding_id", "finding", "disposition", "reasoning"}
                or not isinstance(item.get("finding_id"), str)
                or not isinstance(item.get("finding"), dict)
                or item.get("disposition") not in {"RESOLVED", "STILL_VALID", "REGRESSED", "UNVERIFIED"}
                or not isinstance(item.get("reasoning"), str)
                for item in retained
            )
        ):
            return False
        boundaries = [item.get("boundary") for item in coverage if isinstance(item, dict) and set(item) == {"boundary", "status", "no_impact_reason"} and item.get("status") in {"FRESH", "CARRIED"}]
        return len(boundaries) == len(set(boundaries)) and set(boundaries) == expected

    def _decision_from_raw(self, identity: ValidationIdentity, raw: dict[str, object], evaluation_source: str = "model") -> ValidationDecision:
        raw_findings = raw.get("findings")
        raw_dispositions = raw.get("finding_dispositions")
        raw_coverage = raw.get("coverage")
        raw_finding_ids = raw.get("finding_ids")
        raw_retained = raw.get("retained_findings")
        findings = (
            tuple(
                SpecificationFinding(
                    category=str(item["category"]),
                    requirement_ids=tuple(str(value) for value in item.get("requirement_ids", [])),
                    explanation=str(item["explanation"]),
                    clarification=str(item["clarification"]),
                    counterexample=str(item["counterexample"]),
                    missing_normative_boundary=str(item["missing_normative_boundary"]),
                )
                for item in raw_findings
                if isinstance(item, dict)
            )
            if isinstance(raw_findings, list)
            else ()
        )
        dispositions = tuple(FindingDisposition(**item) for item in raw_dispositions if isinstance(item, dict)) if isinstance(raw_dispositions, list) else ()
        coverage = tuple(ReviewCoverage(**item) for item in raw_coverage if isinstance(item, dict)) if isinstance(raw_coverage, list) else ()
        retained = (
            tuple(
                RetainedFinding(
                    finding_id=str(item["finding_id"]),
                    finding=SpecificationFinding(
                        category=str(item["finding"]["category"]),
                        requirement_ids=tuple(str(value) for value in item["finding"].get("requirement_ids", [])),
                        explanation=str(item["finding"]["explanation"]),
                        clarification=str(item["finding"]["clarification"]),
                        counterexample=str(item["finding"]["counterexample"]),
                        missing_normative_boundary=str(item["finding"]["missing_normative_boundary"]),
                    ),
                    disposition=str(item["disposition"]),
                    reasoning=str(item["reasoning"]),
                )
                for item in raw_retained
                if isinstance(item, dict) and isinstance(item.get("finding"), dict)
            )
            if isinstance(raw_retained, list)
            else ()
        )
        receipt = raw.get("publication_receipt")
        remediation_reason = raw.get("remediation_reason")
        execution_provenance = raw.get("execution_provenance")
        rerun_request_id = raw.get("rerun_request_id")
        predecessor_key = raw.get("predecessor_decision_key")
        publication_version = raw.get("publication_schema_version")
        rerun_authority = raw.get("rerun_authority")
        accepted_sequence = raw.get("accepted_sequence")
        return ValidationDecision(
            identity=identity,
            verdict=str(raw["verdict"]),
            findings=findings,
            findings_published=bool(raw.get("findings_published")),
            readiness_removed=bool(raw.get("readiness_removed")),
            remediation=str(raw.get("remediation", "NONE")),
            remediation_reason=remediation_reason if isinstance(remediation_reason, str) else None,
            evaluation_source=evaluation_source,
            publication_schema_version=publication_version if isinstance(publication_version, int) else 0,
            publication_receipt=receipt if isinstance(receipt, dict) else None,
            execution_provenance=execution_provenance if isinstance(execution_provenance, str) else None,
            rerun_authority=rerun_authority if isinstance(rerun_authority, int) else 0,
            rerun_request_id=rerun_request_id if isinstance(rerun_request_id, str) else None,
            review_mode=("REUSED" if evaluation_source == "stored-decision-reuse" else str(raw.get("review_mode", "FULL"))),
            predecessor_decision_key=predecessor_key if isinstance(predecessor_key, str) else None,
            assessed_delta=str(raw.get("assessed_delta", "")),
            finding_ids=tuple(item for item in raw_finding_ids if isinstance(item, str)) if isinstance(raw_finding_ids, list) else (),
            finding_dispositions=dispositions,
            coverage=coverage,
            review_inputs=str(raw.get("review_inputs", "")),
            accepted_sequence=accepted_sequence if isinstance(accepted_sequence, int) else 0,
            retained_findings=retained,
        )

    def compatible_predecessor(self, identity: ValidationIdentity, authority: int, role: str, parent_number: Optional[int]) -> Optional[ValidationDecision]:
        """Return the latest accepted evidence record, never a legacy partial record."""
        candidates: list[tuple[int, ValidationIdentity, dict[str, object]]] = []
        for raw in self._read().values():
            if not isinstance(raw, dict):
                continue
            identity_raw = raw.get("identity")
            if not isinstance(identity_raw, dict):
                continue
            try:
                candidate_identity = ValidationIdentity(**identity_raw)
            except (TypeError, ValueError):
                continue
            if candidate_identity.repository == identity.repository and candidate_identity.issue_number == identity.issue_number and candidate_identity.policy_identity == identity.policy_identity and int(raw.get("rerun_authority") or 0) == authority:
                candidates.append((int(raw.get("accepted_sequence") or 0), candidate_identity, raw))
        if not candidates:
            return None
        _sequence, candidate_identity, raw = max(candidates, key=lambda item: item[0])
        if not self._raw_has_complete_evidence(raw, candidate_identity):
            return None
        try:
            inputs = json.loads(str(raw["review_inputs"]))
        except (KeyError, TypeError, json.JSONDecodeError):
            return None
        if inputs.get("role") != role or inputs.get("parent_number") != parent_number:
            return None
        return self._decision_from_raw(candidate_identity, raw)

    def legacy_candidates(self, identity: ValidationIdentity) -> tuple[dict[str, object], ...]:
        """Read-only diagnostic scan for pre-migration terminal records (REQ-006).

        Finds every persisted READY/BLOCKED record whose identity matches
        ``identity`` on every field except ``policy_identity``. This never
        authorizes reuse, never causes ``get()`` to return a hit, and never
        picks a "preferred" verdict among conflicting candidates: it is
        purely a diagnostic signal that a pre-migration decision existed for
        this exact semantic subject and could not be proven compatible.
        """
        matches: list[dict[str, object]] = []
        for raw in self._read().values():
            if not isinstance(raw, dict) or raw.get("verdict") not in {"READY", "BLOCKED"}:
                continue
            candidate = raw.get("identity")
            if not isinstance(candidate, dict):
                continue
            if (
                candidate.get("repository") == identity.repository
                and candidate.get("issue_number") == identity.issue_number
                and candidate.get("specification_digest") == identity.specification_digest
                and candidate.get("relationship_digest") == identity.relationship_digest
                and candidate.get("policy_identity") != identity.policy_identity
            ):
                matches.append(raw)
        return tuple(matches)

    def save(self, decision: ValidationDecision) -> None:
        if decision.verdict not in {"READY", "BLOCKED"}:
            raise ValueError("ERROR decisions must not be persisted")
        with self.locked("repository-state"):
            state = self._read()
            state[decision.identity.key] = {
                "identity": asdict(decision.identity),
                "verdict": decision.verdict,
                "findings": [asdict(item) for item in decision.findings],
                "findings_published": decision.findings_published,
                "readiness_removed": decision.readiness_removed,
                "remediation": decision.remediation,
                "remediation_reason": decision.remediation_reason,
                "publication_schema_version": decision.publication_schema_version,
                "publication_receipt": decision.publication_receipt,
                "execution_provenance": decision.execution_provenance,
                "rerun_authority": decision.rerun_authority,
                "rerun_request_id": decision.rerun_request_id,
                "review_mode": decision.review_mode,
                "predecessor_decision_key": decision.predecessor_decision_key,
                "assessed_delta": decision.assessed_delta,
                "finding_ids": list(decision.finding_ids),
                "finding_dispositions": [asdict(item) for item in decision.finding_dispositions],
                "coverage": [asdict(item) for item in decision.coverage],
                "review_inputs": decision.review_inputs,
                "accepted_sequence": decision.accepted_sequence
                or 1
                + max(
                    (int(item.get("accepted_sequence") or 0) for item in state.values() if isinstance(item, dict)),
                    default=0,
                ),
                "retained_findings": [asdict(item) for item in decision.retained_findings],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
            temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)


Analyzer = Callable[[NormativeIssueManifest, str], SpecificationAnalysisResult]


class SpecificationValidationLifecycle:
    """Coalesce validation and apply generation-checked BLOCKED effects."""

    def __init__(self, repository: str, provider_identity: str, path: Optional[Path] = None, analyzer: Optional[Analyzer] = None) -> None:
        self.repository = repository
        # `provider_identity` is retained only for constructor-signature
        # compatibility with existing callers/tests. Execution routing no
        # longer participates in policy identity (Issue #2081, REQ-001): it
        # is intentionally unused here. A freshly computed decision's
        # execution provenance is captured fresh inside `decide()`, at the
        # moment its analyzer call actually runs, not cached at construction
        # time (REQ-004's "execution configuration effective when it
        # starts").
        del provider_identity
        self.policy_identity = validation_policy_identity()
        self.store = SpecificationValidationStore(repository, path)
        terminal_path = path.with_name("reissue_required.json") if path is not None else None
        history_path = path.with_name("individual_review_history.json") if path is not None else None
        self.reissue_store = ReissueRequiredStore(repository, terminal_path)
        self.history_store = IndividualReviewHistoryStore(repository, history_path)
        self.objective_store = ObjectiveAnchorStore(repository, history_path)
        rounds_path = path.with_name("specification_repair_rounds.json") if path is not None else None
        self.repair_rounds = SpecificationRepairRoundStore(repository, rounds_path)
        self.analyzer = analyzer
        self.reruns = self.store.reruns

    def _rerun_subject(self, issue_number: int) -> ReviewSubject:
        return ReviewSubject(self.repository, "individual", issue_number)

    def rerun_execution_key(self, issue_number: int, semantic_identity: str) -> str:
        return self.reruns.execution_key(self._rerun_subject(issue_number), semantic_identity)

    def identity(
        self,
        issue_number: int,
        title: str,
        body: str,
        relationship_context: Optional[IndividualRelationshipContext] = None,
        manifest: Optional[NormativeIssueManifest] = None,
    ) -> ValidationIdentity:
        relationship = relationship_context or IndividualRelationshipContext()
        relationship_digest = hashlib.sha256(json.dumps(asdict(relationship), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        # Most current-state callers own the authoritative GitHub body and use
        # the shared production producer here.  Review execution passes its
        # already supplied manifest explicitly, which preserves the supported
        # separation between Markdown evidence and the normative manifest.
        bound_manifest = manifest or build_normative_issue_manifest(issue_number, title, body)
        return ValidationIdentity(
            self.repository,
            issue_number,
            specification_digest(title, body),
            self.policy_identity,
            relationship_digest,
            manifest_binding(bound_manifest),
        )

    def decide(
        self,
        manifest: NormativeIssueManifest,
        title: str,
        body: str,
        relationship_context: Optional[IndividualRelationshipContext] = None,
    ) -> ValidationDecision:
        identity = self.identity(manifest.issue_number, title, body, relationship_context, manifest)
        subject = self._rerun_subject(manifest.issue_number)
        try:
            authority, request_id, _request_state = self.reruns.authority(subject)
        except RerunAuthorityUnavailable as exc:
            return ValidationDecision(
                identity,
                "ERROR",
                remediation_reason=f"Rerun authority unavailable: {exc}",
                evaluation_source="local-only",
            )
        with self.store.locked(identity.key):
            evidence: Optional[IndividualReviewEvidence] = None
            if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
                return ValidationDecision(
                    identity,
                    "ERROR",
                    remediation_reason=manifest.error or "A valid explicit normative Requirement manifest is required",
                    evaluation_source="local-only",
                    rerun_authority=authority,
                    rerun_request_id=request_id,
                )
            if manifest.explicit_contract_present and manifest.explicit_contract_valid:
                contract = _contract_evidence(manifest, title, body)
                try:
                    history = self.history_store.evidence(manifest.issue_number, contract)
                    objective = self.objective_store.capture(manifest.issue_number, body, "individual-current-snapshot:v1")
                    evidence = IndividualReviewEvidence(history.baseline, history.prior_applied_outcomes, objective)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    return ValidationDecision(identity, "ERROR", remediation_reason=f"Objective evidence unavailable: {exc}", evaluation_source="local-only", rerun_authority=authority, rerun_request_id=request_id)
                integrity = objective_integrity_result(evidence, manifest.issue_number)
                if integrity is not None:
                    relationship = relationship_context or IndividualRelationshipContext()
                    decision = ValidationDecision(
                        identity,
                        integrity.verdict,
                        integrity.findings,
                        remediation=integrity.remediation,
                        remediation_reason=integrity.error,
                        evaluation_source="local-only",
                        rerun_authority=authority,
                        rerun_request_id=request_id,
                        coverage=tuple(ReviewCoverage(item.requirement_id, "FRESH") for item in manifest.requirements) + (ReviewCoverage("contract-wide", "FRESH"),),
                        review_inputs=_review_inputs(manifest, title, body, relationship, evidence),
                    )
                    decision = replace(decision, finding_ids=self._finding_ids(decision, None))
                    decision = replace(decision, retained_findings=self._retained_findings(decision, None))
                    return self._settle_decision_checkpoint(decision)
            try:
                existing = self.store.get(identity)
            except RerunAuthorityUnavailable as exc:
                return ValidationDecision(
                    identity,
                    "ERROR",
                    remediation_reason=f"Rerun authority unavailable: {exc}",
                    evaluation_source="local-only",
                )
            if existing is not None and existing.rerun_authority == authority:
                return existing
            assert evidence is not None
            relationship = relationship_context or IndividualRelationshipContext()
            inputs = _review_inputs(manifest, title, body, relationship, evidence)
            predecessor = self.store.compatible_predecessor(identity, authority, relationship.role, relationship.parent_number)
            incremental = IncrementalReviewContext("FULL")
            if predecessor is not None:
                try:
                    incremental = IncrementalReviewContext(
                        "INCREMENTAL",
                        predecessor.identity.key,
                        _predecessor_evidence(predecessor),
                        _lossless_delta(predecessor.review_inputs, inputs),
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    # Optional predecessor evidence is never repaired or
                    # fabricated. Its absence/corruption selects FULL.
                    predecessor = None
            evidence = replace(evidence, incremental=incremental)
            # Diagnostic only (REQ-006/REQ-009): a current-format miss may
            # still have a pre-migration terminal record under the old,
            # opaque provider-mixed hash. Its semantic compatibility cannot
            # be proven from the hash alone, so normal review proceeds
            # unchanged; this only makes the distinct "legacy-compatibility
            # miss" signal greppable and testable, separate from a genuine
            # cache miss (no legacy candidates) or a real policy change.
            legacy_matches = self.store.legacy_candidates(identity)
            if legacy_matches:
                logger.warning(
                    "legacy_policy_unproven: repository={} issue_number={} legacy_candidates={}",
                    self.repository,
                    manifest.issue_number,
                    len(legacy_matches),
                )
            legacy_candidates_detected = len(legacy_matches)
            provenance = configured_provider_identity()
            with bind_invocation_target(self.repository, f"issue#{manifest.issue_number}", "specification_validation", defer_checkpoint=True):
                if self.analyzer is None:
                    analyzed = self._default_analyzer(manifest, body, evidence, relationship_context)
                else:
                    analyzed = self.analyzer(manifest, body)
            decision = ValidationDecision(
                identity,
                analyzed.verdict,
                analyzed.findings,
                remediation=analyzed.remediation,
                remediation_reason=analyzed.error,
                execution_provenance=provenance,
                legacy_candidates_detected=legacy_candidates_detected,
                rerun_authority=authority,
                rerun_request_id=request_id,
                review_mode=incremental.mode,
                predecessor_decision_key=incremental.predecessor_decision_key,
                assessed_delta=incremental.assessed_delta,
                finding_dispositions=analyzed.dispositions,
                coverage=(analyzed.coverage if analyzed.coverage else tuple(ReviewCoverage(item.requirement_id, "FRESH") for item in manifest.requirements) + (ReviewCoverage("contract-wide", "FRESH"),)),
                review_inputs=inputs,
            )
            decision = replace(decision, finding_ids=self._finding_ids(decision, predecessor))
            decision = replace(decision, retained_findings=self._retained_findings(decision, predecessor))
            return self._settle_decision_checkpoint(decision)

    @staticmethod
    def _finding_ids(decision: ValidationDecision, predecessor: Optional[ValidationDecision]) -> tuple[str, ...]:
        """Retain identity for the same counterexample; allocate only for new defects."""
        prior: dict[str, list[str]] = {}
        active_disposition_ids: list[str] = []
        if predecessor is not None:
            for retained in predecessor.retained_findings:
                if retained.finding.counterexample:
                    prior.setdefault(retained.finding.counterexample, []).append(retained.finding_id)
            active_disposition_ids = [item.finding_id for item in decision.finding_dispositions if item.disposition in {"STILL_VALID", "REGRESSED"}]
        values: list[str] = []
        for index, finding in enumerate(decision.findings, start=1):
            matching_ids = prior.get(finding.counterexample, []) if finding.counterexample else active_disposition_ids
            finding_id = matching_ids.pop(0) if matching_ids else f"{decision.identity.key}:finding:{index}"
            values.append(finding_id)
            active_disposition_ids = [value for value in active_disposition_ids if value != finding_id]
        return tuple(values)

    @staticmethod
    def _retained_findings(decision: ValidationDecision, predecessor: Optional[ValidationDecision]) -> tuple[RetainedFinding, ...]:
        retained = {item.finding_id: item for item in predecessor.retained_findings} if predecessor is not None else {}
        for disposition in decision.finding_dispositions:
            previous = retained.get(disposition.finding_id)
            if previous is not None:
                retained[disposition.finding_id] = replace(
                    previous,
                    disposition=disposition.disposition,
                    reasoning=disposition.reasoning,
                )
        dispositions = {item.finding_id: item for item in decision.finding_dispositions}
        for finding_id, finding in zip(decision.finding_ids, decision.findings):
            current_disposition = dispositions.get(finding_id)
            retained[finding_id] = RetainedFinding(
                finding_id,
                finding,
                current_disposition.disposition if current_disposition is not None else "STILL_VALID",
                current_disposition.reasoning if current_disposition is not None else "Finding introduced by this accepted review.",
            )
        return tuple(retained.values())

    def _settle_decision_checkpoint(self, decision: "ValidationDecision") -> "ValidationDecision":
        """Persist a fresh decision (if reusable) and confirm its invocation checkpoint.

        A READY/BLOCKED decision must be durably saved before the admitted
        invocation that produced it is allowed to settle: a failed save
        leaves the invocation CHECKPOINTING (protected, retriable) and
        re-raises so the caller sees the persistence problem (Issue #2009,
        REQ-003/REQ-009). An ERROR decision is never cached, so there is
        nothing further to protect once decide() returns it.
        """
        from .review_capture.issue_review_audit import observe_authorization_persistence, observe_native_decision

        observe_native_decision(decision)
        subject = self._rerun_subject(decision.identity.issue_number)
        try:
            authority, _request_id, _state = self.reruns.authority(subject)
        except RerunAuthorityUnavailable as exc:
            handle = take_pending_invocation_handle()
            if handle is not None:
                handle.confirm_settled()
            return replace(
                decision,
                verdict="ERROR",
                findings=(),
                remediation="NONE",
                remediation_reason=f"Rerun authority unavailable: {exc}",
            )
        if authority != decision.rerun_authority:
            handle = take_pending_invocation_handle()
            if handle is not None:
                handle.confirm_settled()
            return replace(
                decision,
                verdict="ERROR",
                findings=(),
                remediation="NONE",
                remediation_reason="review occurrence was revoked by a newer explicit rerun",
            )
        if decision.verdict in {"READY", "BLOCKED"}:
            try:
                self.store.save(decision)
            except Exception as exc:
                observe_authorization_persistence("failed", str(exc))
                handle = take_pending_invocation_handle()
                if handle is not None:
                    handle.record_checkpoint_attempt_failed(str(exc))
                raise
            observe_authorization_persistence("confirmed")
            try:
                accepted_authority, _request_id, _state = self.reruns.authority(subject)
            except RerunAuthorityUnavailable as exc:
                return replace(
                    decision,
                    verdict="ERROR",
                    findings=(),
                    remediation="NONE",
                    remediation_reason=f"Rerun authority unavailable: {exc}",
                )
            if accepted_authority != decision.rerun_authority:
                return replace(
                    decision,
                    verdict="ERROR",
                    findings=(),
                    remediation="NONE",
                    remediation_reason="review occurrence was revoked before decision acceptance",
                )
            if accepted_authority and not self.reruns.satisfy(subject, accepted_authority, decision.identity.key, decision.evaluation_source):
                return replace(
                    decision,
                    verdict="ERROR",
                    findings=(),
                    remediation="NONE",
                    remediation_reason="rerun authority changed before decision acceptance",
                )
        handle = take_pending_invocation_handle()
        if handle is not None:
            handle.confirm_settled()
        return decision

    def _default_analyzer(self, manifest: NormativeIssueManifest, body: str, evidence: IndividualReviewEvidence, relationship_context: Optional[IndividualRelationshipContext]) -> SpecificationAnalysisResult:
        with individual_review_evidence(evidence):
            if relationship_context is None:
                return analyze_issue_specification(manifest, body)
            with individual_relationship_context(relationship_context):
                return analyze_issue_specification(manifest, body)

    def is_reissue_required(self, issue_number: int) -> bool:
        """Return the durable authorization stop for this stable Issue number."""
        return self.reissue_store.contains(issue_number)

    def apply_blocked(
        self,
        github: object,
        decision: ValidationDecision,
        submission_is_current: Optional[Callable[[], bool]] = None,
    ) -> Optional[str]:
        """Apply idempotent effects only while BLOCKED evidence is authoritative.

        The diagnostic comment and the readiness withdrawal are independently
        completed against the durable pending-work store (REQ-001, REQ-002,
        REQ-007): each is confirmed via ``complete_effect`` exactly when its
        own GitHub mutation (or an already-recorded prior completion) is
        established, so a failure partway through never re-sends the sibling
        that already succeeded and never silently drops the one that has not.
        """
        issue_number = decision.identity.issue_number
        expected_identity = decision.identity
        publication_identity = validation_publication_identity(self.repository, issue_number, decision.identity.key)
        pending_work_store = get_pending_work_store()
        with self.store.locked(decision.identity.key):
            current_decision = self.store.get(decision.identity)
            if current_decision is None or current_decision.verdict != "BLOCKED":
                return "durable BLOCKED decision is unavailable"

            # Effects already durably confirmed by an earlier partial run are
            # not re-derived from live label state: the label's own absence
            # may be exactly that earlier withdrawal, and must not hide a
            # still-missing diagnostic behind a stale currentness check
            # (REQ-004).
            diagnostic_trusted = publication_trusted_complete(current_decision)
            if diagnostic_trusted:
                pending_work_store.complete_effect(publication_identity, DIAGNOSTIC_EFFECT)
            if current_decision.readiness_removed:
                pending_work_store.complete_effect(publication_identity, READINESS_WITHDRAWAL_EFFECT)

            def diagnostic_is_current() -> bool:
                """Currentness for the diagnostic comment.

                Requires the same full currentness check as the label
                withdrawal (identity match plus ``submission_is_current``),
                *except* when that check is failing only because this
                exact decision's own readiness withdrawal already
                completed: that specific absence of the label must not
                hide a still-missing diagnostic comment (REQ-004). Any
                other currentness failure (an added child, a changed
                hierarchy, and so on) still withholds the comment, exactly
                as before.
                """
                snapshot = github.get_issue_dispatch_snapshot_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                if not isinstance(snapshot, dict):
                    return False
                identity = self.identity(issue_number, str(snapshot.get("title") or ""), str(snapshot.get("body") or ""))
                if identity != expected_identity:
                    return False
                if submission_is_current is None or submission_is_current():
                    return True
                recorded = self.store.get(expected_identity)
                return recorded is not None and recorded.readiness_removed

            def blocked_snapshot() -> Optional[dict[str, object]]:
                """Currentness for the label withdrawal: identity match and label still present."""
                snapshot = github.get_issue_dispatch_snapshot_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                if not isinstance(snapshot, dict) or not is_implementation_ready(snapshot):
                    return None
                identity = self.identity(issue_number, str(snapshot.get("title") or ""), str(snapshot.get("body") or ""))
                if identity != expected_identity or (submission_is_current is not None and not submission_is_current()):
                    return None
                return snapshot

            # A changed/withdrawn submission must not receive stale effects. It is
            # not an operational failure: the old generation simply remains blocked.
            if not diagnostic_is_current():
                return None
            current_decision = self._apply_repair_round_policy(current_decision)
            self._record_applied_outcome(current_decision)
            if current_decision.remediation == "REISSUE_REQUIRED":
                try:
                    self.reissue_store.mark(issue_number)
                except OSError as exc:
                    return f"durable reissue-required marker failed: {exc}"
            if not diagnostic_trusted:
                marker = f"{FINDINGS_MARKER_PREFIX}:{current_decision.identity.key}"
                expected_body = self.findings_comment(current_decision)
                comments = github.get_issue_comments_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                # Comment enumeration is external I/O. An edit during that read
                # invalidates publication just as an edit before the read does.
                if not diagnostic_is_current():
                    return None
                if current_decision.publication_schema_version == 0:
                    # Initialize versioned publication ownership before this
                    # unfinished record is handled, so a crash partway through
                    # cannot later masquerade as legacy (pre-App) completion
                    # (REQ-008).
                    current_decision = replace(current_decision, publication_schema_version=1)
                    self.store.save(current_decision)
                try:
                    reviewer_identity = github.reviewer_app_identity(self.repository)  # type: ignore[attr-defined]
                except Exception as exc:
                    return f"reviewer identity unavailable: {exc}"
                receipt, conflicting = find_confirmed_publication(comments, marker, expected_body, reviewer_identity)
                if receipt is None and not conflicting:
                    receipt = github.publish_issue_review_comment(self.repository, issue_number, expected_body, diagnostic_is_current)  # type: ignore[attr-defined]
                if receipt is not None:
                    current_decision = replace(current_decision, findings_published=True, publication_schema_version=1, publication_receipt=receipt.as_dict())
                    self.store.save(current_decision)
                    pending_work_store.complete_effect(publication_identity, DIAGNOSTIC_EFFECT)
                elif conflicting:
                    # A marker-bearing comment exists but is not confirmed as
                    # authored by the reviewer App: this stays an explicit
                    # unconfirmed conflict rather than a duplicate repost or a
                    # silently accepted success (REQ-005).
                    return "findings publication conflict: existing marker comment is not confirmed reviewer-App authored"
            if blocked_snapshot() is None:
                return None
            # readiness_removed describes the previous submission, not all future
            # submissions. If the label is currently present it was explicitly
            # re-added and must be removed again, while the findings stay unique.
            github.remove_labels(self.repository, issue_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
            current_decision = replace(current_decision, readiness_removed=True)
            self.store.save(current_decision)
            pending_work_store.complete_effect(publication_identity, READINESS_WITHDRAWAL_EFFECT)
        return None

    def apply_inherited_blocked(
        self,
        github: object,
        decision: ValidationDecision,
        set_is_current: Callable[[], bool],
    ) -> Optional[str]:
        """Withdraw only the explicit submission of a current blocked child.

        See ``apply_blocked`` for the effect-completion contract this shares:
        the diagnostic comment and the child's readiness withdrawal are
        confirmed independently against the durable pending-work store.
        """
        issue_number = decision.identity.issue_number
        publication_identity = validation_publication_identity(self.repository, issue_number, decision.identity.key)
        pending_work_store = get_pending_work_store()
        with self.store.locked(decision.identity.key):
            failures: list[str] = []
            current = self.store.get(decision.identity)
            if current is None or current.verdict != "BLOCKED":
                return "durable child BLOCKED decision is unavailable"

            diagnostic_trusted = publication_trusted_complete(current)
            if diagnostic_trusted:
                pending_work_store.complete_effect(publication_identity, DIAGNOSTIC_EFFECT)
            if current.readiness_removed:
                pending_work_store.complete_effect(publication_identity, READINESS_WITHDRAWAL_EFFECT)

            def digest_matches() -> bool:
                snapshot = github.get_issue_dispatch_snapshot_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                return isinstance(snapshot, dict) and specification_digest(str(snapshot.get("title") or ""), str(snapshot.get("body") or "")) == decision.identity.specification_digest

            def still_current() -> bool:
                return digest_matches() and set_is_current()

            # The full parent-set currentness check (hierarchy, sibling
            # membership, and decomposition identity, not merely the
            # readiness label) still gates the diagnostic here: unlike the
            # standalone case, an inherited BLOCKED result is only
            # authoritative for a specific decomposition set, and a change
            # anywhere in that set must supersede it (REQ-003, REQ-005).
            if not still_current():
                return None
            current = self._apply_repair_round_policy(current)
            self._record_applied_outcome(current)
            if current.remediation == "REISSUE_REQUIRED":
                try:
                    self.reissue_store.mark(issue_number)
                except OSError as exc:
                    return f"durable reissue-required marker failed: {exc}"
            if not diagnostic_trusted:
                marker = f"{FINDINGS_MARKER_PREFIX}:{current.identity.key}"
                expected_body = self.findings_comment(current)
                try:
                    comments = github.get_issue_comments_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                except Exception as exc:
                    failures.append(f"findings lookup failed: {exc}")
                    if isinstance(exc, GitHubRequestError):
                        pending_work_store.defer(publication_identity, exc, (DIAGNOSTIC_EFFECT,))
                    comments = None
                if not still_current():
                    return "; ".join(failures) or None
                if comments is not None:
                    if current.publication_schema_version == 0:
                        current = replace(current, publication_schema_version=1)
                        self.store.save(current)
                    try:
                        reviewer_identity = github.reviewer_app_identity(self.repository)  # type: ignore[attr-defined]
                    except Exception as exc:
                        failures.append(f"reviewer identity unavailable: {exc}")
                        reviewer_identity = None
                    if reviewer_identity is not None:
                        receipt, conflicting = find_confirmed_publication(comments, marker, expected_body, reviewer_identity)
                        if receipt is None and not conflicting:
                            try:
                                receipt = github.publish_issue_review_comment(self.repository, issue_number, expected_body, still_current)  # type: ignore[attr-defined]
                            except Exception as exc:
                                failures.append(f"findings publication failed: {exc}")
                                if isinstance(exc, GitHubRequestError):
                                    pending_work_store.defer(publication_identity, exc, (DIAGNOSTIC_EFFECT,))
                                receipt = None
                        if receipt is not None:
                            current = replace(current, findings_published=True, publication_schema_version=1, publication_receipt=receipt.as_dict())
                            self.store.save(current)
                            pending_work_store.complete_effect(publication_identity, DIAGNOSTIC_EFFECT)
                        elif conflicting:
                            failures.append("findings publication conflict: existing marker comment is not confirmed reviewer-App authored")
            if not still_current():
                return "; ".join(failures) or None
            try:
                # A child may also have its own explicit submission. Withdraw
                # only that label; the parent submission remains available.
                snapshot = github.get_issue_dispatch_snapshot_strict(self.repository, issue_number)  # type: ignore[attr-defined]
                if not isinstance(snapshot, dict) or specification_digest(str(snapshot.get("title") or ""), str(snapshot.get("body") or "")) != decision.identity.specification_digest or not set_is_current():
                    return "; ".join(failures) or None
                if is_implementation_ready(snapshot):
                    github.remove_labels(self.repository, issue_number, [IMPLEMENTATION_READY_LABEL], item_type="issue")  # type: ignore[attr-defined]
                if not still_current():
                    return "; ".join(failures) or None
                self.store.save(replace(current, readiness_removed=True))
                pending_work_store.complete_effect(publication_identity, READINESS_WITHDRAWAL_EFFECT)
            except Exception as exc:
                failures.append(f"readiness withdrawal failed: {exc}")
                if isinstance(exc, GitHubRequestError):
                    pending_work_store.defer(publication_identity, exc, (READINESS_WITHDRAWAL_EFFECT,))
        return "; ".join(failures) or None

    def _apply_repair_round_policy(self, decision: ValidationDecision) -> ValidationDecision:
        """Apply the generation-deduplicated circuit breaker before GitHub effects."""
        from .llm_backend_config import get_specification_repair_round_limit_from_config

        applied = self.repair_rounds.apply(
            "individual",
            decision.identity.issue_number,
            decision.identity.specification_digest,
            decision.remediation,
            get_specification_repair_round_limit_from_config(repo_name=self.repository),
        )
        if (applied.remediation, applied.reason) == (decision.remediation, decision.remediation_reason):
            return decision
        # A repair-round policy change never touches publication state: an
        # already App-confirmed receipt must survive this reconstruction
        # rather than being silently reset to legacy (Issue #2026, REQ-003,
        # REQ-008).
        updated = replace(decision, remediation=applied.remediation, remediation_reason=applied.reason)
        self.store.save(updated)
        return updated

    def authorize_automatic_repair(
        self,
        decision: ValidationDecision,
        submission_is_current: Callable[[], bool],
        initiate: Callable[[], None],
    ) -> RepairRoundApplication:
        """Persist authorization before initiating an exact-current contract repair."""
        from .llm_backend_config import get_specification_repair_round_limit_from_config

        with self.store.locked(decision.identity.key):
            current = self.store.get(decision.identity)
            if current is None or current.verdict != "BLOCKED" or current.remediation != "EDIT_IN_PLACE" or not submission_is_current():
                return RepairRoundApplication(decision.remediation, self.repair_rounds.count("individual", decision.identity.issue_number))
            applied = self.repair_rounds.authorize(
                "individual",
                decision.identity.issue_number,
                decision.identity.specification_digest,
                current.remediation,
                get_specification_repair_round_limit_from_config(repo_name=self.repository),
            )
            if applied.automatic_repair_authorized:
                initiate()
            return applied

    def _record_applied_outcome(self, decision: ValidationDecision) -> None:
        outcome = json.dumps(
            {
                "specification_digest": decision.identity.specification_digest,
                "verdict": decision.verdict,
                "remediation": decision.remediation,
                "findings": [asdict(item) for item in decision.findings],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        self.history_store.record_applied(decision.identity.issue_number, decision.identity.key, outcome)

    @staticmethod
    def findings_comment(decision: ValidationDecision) -> str:
        remedy = "Replace this Issue with a new Issue number; editing this Issue cannot restore implementation eligibility." if decision.remediation == "REISSUE_REQUIRED" else "Edit this Issue in place and resubmit it for validation."
        lines = [f"<!-- {FINDINGS_MARKER_PREFIX}:{decision.identity.key} -->", "## Auto-Coder specification validation", "", "Implementation is blocked by material specification defects:", "", f"**Remediation:** {remedy}"]
        if decision.remediation_reason:
            lines.extend(["", f"**Reason:** `{decision.remediation_reason}`"])
        if decision.remediation_reason == "automatic_repair_paused(repair_round_limit_reached)":
            lines.extend(
                [
                    "",
                    "Automatic repair has paused because the repair-round limit was reached. " "The semantic remediation remains `EDIT_IN_PLACE`; replacement/reissue is not required by the circuit breaker itself.",
                ]
            )
        for finding in decision.findings:
            ids = ", ".join(finding.requirement_ids) or "contract-wide"
            lines.extend(["", f"- **{finding.category}** ({ids}): {finding.explanation}", f"  Clarification required: {finding.clarification}"])
        return "\n".join(lines)

    FindingDisposition,
    IncrementalReviewContext,
    ReviewCoverage,
