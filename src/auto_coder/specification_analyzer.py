"""Provider-independent adversarial analysis of normative Issue contracts."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from .backend_manager import BackendManager, run_llm_prompt
from .objective_evidence import ObjectiveAnchor, objective_evidence_json
from .prompt_loader import render_prompt
from .requirement_contract import NormativeIssueManifest

SPECIFICATION_FINDING_CATEGORIES = frozenset(
    {
        "material_ambiguity",
        "normative_contradiction",
        "hidden_requirement",
        "unverifiable_requirement",
        "false_success_gap",
        "unstated_dependency",
        "objective_conflict",
    }
)


@dataclass(frozen=True)
class SpecificationFinding:
    """One demonstrated material defect in the written Issue contract."""

    category: str
    requirement_ids: tuple[str, ...]
    explanation: str
    clarification: str
    counterexample: str
    missing_normative_boundary: str


@dataclass(frozen=True)
class SpecificationAnalysisResult:
    """Fail-closed semantic verdict for one authoritative Issue manifest."""

    verdict: str
    findings: tuple[SpecificationFinding, ...] = ()
    error: Optional[str] = None
    remediation: str = "NONE"

    @property
    def is_ready(self) -> bool:
        return self.verdict == "READY"


@dataclass(frozen=True)
class IndividualReviewEvidence:
    """Durable, non-normative evidence used only for remediation selection."""

    baseline: str
    prior_applied_outcomes: tuple[str, ...] = ()
    objective: Optional[ObjectiveAnchor] = None


@dataclass(frozen=True)
class IndividualRelationshipContext:
    """Caller-reconciled relationship evidence for an individual review."""

    role: str = "standalone"
    related_contracts: str = "(No authoritative cross-Issue relationship context supplied.)"


_REVIEW_EVIDENCE: ContextVar[Optional[IndividualReviewEvidence]] = ContextVar("individual_review_evidence", default=None)
_RELATIONSHIP_CONTEXT: ContextVar[Optional[IndividualRelationshipContext]] = ContextVar("individual_relationship_context", default=None)


@contextmanager
def individual_review_evidence(evidence: IndividualReviewEvidence) -> Iterator[None]:
    """Make lifecycle evidence available without changing analyzer call sites."""
    token = _REVIEW_EVIDENCE.set(evidence)
    try:
        yield
    finally:
        _REVIEW_EVIDENCE.reset(token)


@contextmanager
def individual_relationship_context(context: IndividualRelationshipContext) -> Iterator[None]:
    """Carry reconciled role evidence through the lifecycle's stable analyzer API."""
    token = _RELATIONSHIP_CONTEXT.set(context)
    try:
        yield
    finally:
        _RELATIONSHIP_CONTEXT.reset(token)


def _error(message: str) -> SpecificationAnalysisResult:
    return SpecificationAnalysisResult(verdict="ERROR", error=message)


def objective_integrity_result(evidence: IndividualReviewEvidence, issue_number: Optional[int] = None) -> Optional[SpecificationAnalysisResult]:
    """Return the deterministic blocker for an altered anchored Objective."""
    anchor = evidence.objective
    if anchor is None:
        return _error("Required Objective evidence is unavailable")
    if (issue_number is not None and anchor.issue_number != issue_number) or not isinstance(anchor.source_identity, str) or not anchor.source_identity.strip():
        return _error("Required Objective evidence is malformed")
    if anchor.state == "UNANCHORED":
        return None
    if anchor.state != "ANCHORED" or not isinstance(anchor.original_text, str) or not anchor.original_text.strip():
        return _error("Required Objective evidence is malformed")
    current = anchor.current
    if current.status == "PRESENT" and current.text == anchor.original_text:
        return None
    mismatch = {
        "ABSENT": "The current Issue has no Objective section.",
        "INVALID": "The current Issue has a duplicate or empty Objective section.",
    }.get(current.status, "The current Objective text differs from the fixed Objective.")
    quoted = json.dumps(anchor.original_text, ensure_ascii=False)
    finding = SpecificationFinding(
        category="objective_conflict",
        requirement_ids=(),
        explanation=f"Fixed Objective {quoted} was not preserved. {mismatch}",
        clarification=("Restore the fixed Objective text exactly (apart from CRLF-to-LF conversion and outer whitespace), " "or obtain a user decision to create a replacement Issue for the changed purpose; do not rewrite the anchor."),
        counterexample="",
        missing_normative_boundary="",
    )
    return SpecificationAnalysisResult("BLOCKED", (finding,), remediation="EDIT_IN_PLACE")


def _reject_duplicate_json_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one JSON object while rejecting every duplicate member."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON member: {key}")
        result[key] = value
    return result


def parse_specification_analysis_response(response: str, manifest: NormativeIssueManifest) -> SpecificationAnalysisResult:
    """Validate a model response without recovering a verdict from partial prose."""
    try:
        payload = json.loads(response, object_pairs_hook=_reject_duplicate_json_members)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _error("Specification analyzer returned unparsable JSON")

    if not isinstance(payload, dict) or set(payload) != {"verdict", "remediation", "findings"}:
        return _error("Specification analyzer output does not match the required top-level schema")
    verdict = payload["verdict"]
    remediation = payload["remediation"]
    raw_findings = payload["findings"]
    if not isinstance(verdict, str) or verdict not in {"READY", "BLOCKED", "ERROR"} or not isinstance(raw_findings, list):
        return _error("Specification analyzer output contains an invalid verdict or findings value")
    if remediation not in {"NONE", "EDIT_IN_PLACE", "REISSUE_REQUIRED"}:
        return _error("Specification analyzer output contains an invalid remediation")
    if (verdict in {"READY", "ERROR"} and remediation != "NONE") or (verdict == "BLOCKED" and remediation not in {"EDIT_IN_PLACE", "REISSUE_REQUIRED"}):
        return _error("Specification analyzer verdict contradicts its remediation")
    if verdict == "ERROR":
        if raw_findings:
            return _error("An ERROR verdict cannot contain findings")
        return _error("The semantic analysis model could not produce a trustworthy verdict")

    expected_fields = {
        "category",
        "requirement_ids",
        "explanation",
        "clarification",
        "counterexample",
        "missing_normative_boundary",
    }
    known_ids = {requirement.requirement_id for requirement in manifest.requirements}
    findings: list[SpecificationFinding] = []
    for raw in raw_findings:
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            return _error("Specification analyzer finding does not match the required schema")
        category = raw["category"]
        ids = raw["requirement_ids"]
        prose_fields = [raw[name] for name in ("explanation", "clarification", "counterexample", "missing_normative_boundary")]
        if not isinstance(category, str) or category not in SPECIFICATION_FINDING_CATEGORIES:
            return _error("Specification analyzer finding uses an unknown category")
        if not isinstance(ids, list) or any(not isinstance(value, str) or value not in known_ids for value in ids) or len(ids) != len(set(ids)):
            return _error("Specification analyzer finding contains invalid Requirement IDs")
        if any(not isinstance(value, str) for value in prose_fields) or not raw["explanation"].strip() or not raw["clarification"].strip():
            return _error("Specification analyzer finding is incomplete")
        if category == "false_success_gap" and (not raw["counterexample"].strip() or not raw["missing_normative_boundary"].strip()):
            return _error("A false-success finding requires a counterexample and missing normative boundary")
        findings.append(
            SpecificationFinding(
                category=category,
                requirement_ids=tuple(ids),
                explanation=raw["explanation"].strip(),
                clarification=raw["clarification"].strip(),
                counterexample=raw["counterexample"].strip(),
                missing_normative_boundary=raw["missing_normative_boundary"].strip(),
            )
        )

    if (verdict == "READY" and findings) or (verdict == "BLOCKED" and not findings):
        return _error("Specification analyzer verdict contradicts its findings")
    return SpecificationAnalysisResult(verdict=verdict, findings=tuple(findings), remediation=remediation)


def analyze_issue_specification(
    manifest: NormativeIssueManifest,
    issue_body: str,
    parent_context: Optional[str] = None,
    backend_manager: Optional[BackendManager] = None,
    prompt_runner: Optional[Callable[[str], str]] = None,
    review_evidence: Optional[IndividualReviewEvidence] = None,
    relationship_context: Optional[IndividualRelationshipContext] = None,
) -> SpecificationAnalysisResult:
    """Adversarially decide whether an Issue is an independent contract.

    The caller supplies the shared authoritative manifest.  This operation never
    reparses ``issue_body`` and never inspects a repository.
    """
    if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
        return _error(manifest.error or "A valid explicit normative Requirement manifest is required")

    review_evidence = review_evidence or _REVIEW_EVIDENCE.get()
    if review_evidence is None:
        return _error("Required Objective evidence is unavailable")
    integrity = objective_integrity_result(review_evidence, manifest.issue_number)
    if integrity is not None:
        return integrity
    relationship_context = relationship_context or _RELATIONSHIP_CONTEXT.get() or IndividualRelationshipContext()
    if relationship_context.role not in {"standalone", "child"}:
        return _error("Specification analysis received an invalid authoritative relationship role")
    requirements = json.dumps(
        [{"requirement_id": item.requirement_id, "text": item.text} for item in manifest.requirements],
        ensure_ascii=False,
        indent=2,
    )
    prompt = render_prompt(
        "issue.adversarial_specification_analysis",
        issue_number=manifest.issue_number,
        issue_title=manifest.title,
        normative_manifest=requirements,
        issue_body=issue_body,
        parent_context=parent_context or "(No parent Issue context supplied.)",
        relationship_role=relationship_context.role,
        related_contracts=relationship_context.related_contracts,
        durable_baseline=review_evidence.baseline if review_evidence else "(No earlier baseline is available.)",
        prior_applied_outcomes=("\n\n".join(review_evidence.prior_applied_outcomes) if review_evidence and review_evidence.prior_applied_outcomes else "(No prior applied material review outcomes.)"),
        objective_evidence=(objective_evidence_json(review_evidence.objective) if review_evidence and review_evidence.objective else "(Required Objective evidence is unavailable.)"),
    )
    try:
        if prompt_runner is not None:
            response = prompt_runner(prompt)
        else:
            if backend_manager is None:
                from .cli_helpers import create_adversarial_validation_backend_manager

                backend_manager = create_adversarial_validation_backend_manager()
            if backend_manager is None:
                return _error("No strong specification-analysis backend is available")
            response = run_llm_prompt(prompt, backend_manager=backend_manager, is_noedit=True)
    except Exception as exc:
        return _error(f"Specification analysis execution failed: {type(exc).__name__}")
    return parse_specification_analysis_response(response, manifest)
