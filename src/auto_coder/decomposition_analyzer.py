"""Provider-independent semantic analysis of parent/child Issue contracts."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence

from .backend_manager import BackendManager, run_llm_prompt
from .objective_evidence import ObjectiveAnchor, objective_evidence_json
from .prompt_loader import render_prompt
from .requirement_contract import NormativeIssueManifest
from .role_structural_assessment import (
    ROLE_IMPLEMENTATION_CHILD,
    ROLE_TRACKING_PARENT,
    StructuralDefect,
    assess_role_structure,
)
from .specification_analyzer import _reject_duplicate_json_members

DECOMPOSITION_FINDING_CATEGORIES = frozenset(
    {
        "missing_requirement_ownership",
        "cross_issue_contradiction",
        "unstated_cross_issue_dependency",
        "boundary_semantics_conflict",
        "decomposition_false_success",
        "objective_conflict",
        "invalid_issue_structure",
    }
)

PARENT_COVERAGE_FINDING_CATEGORIES = frozenset({"missing_requirement_ownership", "decomposition_false_success"})

_STRUCTURAL_DEFECT_CLARIFICATIONS = {
    "parent_requirements_forbidden": "Remove the Requirements heading and every REQ-NNN declaration from this tracking parent; implementation Requirements belong only in its direct children.",
    "parent_objective_required": "Give this tracking parent exactly one nonempty authored Objective section.",
    "child_requirements_invalid": "Give this implementation child an explicit, nonempty Requirements contract with unique REQ-NNN identifiers.",
}


@dataclass(frozen=True)
class DecompositionIssue:
    """One Issue's authoritative manifest and non-normative prose evidence."""

    manifest: NormativeIssueManifest
    body: str


@dataclass(frozen=True)
class AffectedIssue:
    """An Issue and its exact normative Requirements affected by a finding."""

    issue_number: int
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class DecompositionFinding:
    """One demonstrated material defect spanning supplied Issue contracts."""

    category: str
    affected_issues: tuple[AffectedIssue, ...]
    explanation: str
    clarification: str


@dataclass(frozen=True)
class DecompositionAnalysisResult:
    """Fail-closed verdict for exactly one authoritative decomposition set."""

    verdict: str
    findings: tuple[DecompositionFinding, ...] = ()
    error: Optional[str] = None
    remediation: str = "NONE"

    @property
    def is_ready(self) -> bool:
        return self.verdict == "READY"


@dataclass(frozen=True)
class DecompositionReviewEvidence:
    """Immutable set baseline and applied reviews used only for remediation."""

    baseline: str
    prior_applied_outcomes: tuple[str, ...] = ()
    objectives: tuple[ObjectiveAnchor, ...] = ()


_REVIEW_EVIDENCE: ContextVar[Optional[DecompositionReviewEvidence]] = ContextVar("decomposition_review_evidence", default=None)


@contextmanager
def decomposition_review_evidence(evidence: DecompositionReviewEvidence) -> Iterator[None]:
    """Make lifecycle evidence available without widening analyzer adapters."""
    token = _REVIEW_EVIDENCE.set(evidence)
    try:
        yield
    finally:
        _REVIEW_EVIDENCE.reset(token)


def _error(message: str) -> DecompositionAnalysisResult:
    return DecompositionAnalysisResult(verdict="ERROR", error=message)


def objective_integrity_result(
    evidence: DecompositionReviewEvidence,
    membership: Sequence[DecompositionIssue],
) -> Optional[DecompositionAnalysisResult]:
    """Fail closed unless Objective evidence exactly describes the reviewed set."""
    expected = {item.manifest.issue_number for item in membership}
    anchors = evidence.objectives
    if len(anchors) != len(expected) or {item.issue_number for item in anchors} != expected:
        return _error("Required decomposition Objective evidence is unavailable or does not match supplied membership")
    if len({item.issue_number for item in anchors}) != len(anchors):
        return _error("Required decomposition Objective evidence is malformed")
    for anchor in anchors:
        if not isinstance(anchor.source_identity, str) or not anchor.source_identity.strip():
            return _error(f"Required Objective evidence for Issue #{anchor.issue_number} is malformed")
        current = anchor.current
        if current.status not in {"PRESENT", "ABSENT", "INVALID"} or (current.status == "PRESENT" and (not isinstance(current.text, str) or not current.text.strip())) or (current.status != "PRESENT" and current.text is not None):
            return _error(f"Required Objective evidence for Issue #{anchor.issue_number} is malformed")
        if anchor.state == "UNANCHORED":
            if anchor.original_text is not None:
                return _error(f"Required Objective evidence for Issue #{anchor.issue_number} is malformed")
            continue
        if anchor.state != "ANCHORED" or not isinstance(anchor.original_text, str) or not anchor.original_text.strip():
            return _error(f"Required Objective evidence for Issue #{anchor.issue_number} is malformed")
        if current.status == "PRESENT" and current.text == anchor.original_text:
            continue
        mismatch = {
            "ABSENT": "The current Issue has no Objective section.",
            "INVALID": "The current Issue has a duplicate or empty Objective section.",
        }.get(current.status, "The current Objective text differs from the fixed Objective.")
        finding = DecompositionFinding(
            "objective_conflict",
            (AffectedIssue(anchor.issue_number, ()),),
            f"Fixed Objective {json.dumps(anchor.original_text, ensure_ascii=False)} was not preserved. {mismatch}",
            "Restore this member's fixed Objective text exactly (apart from CRLF-to-LF conversion and outer whitespace), or obtain a user decision to replace its purpose; do not rewrite other members or the anchor.",
        )
        return DecompositionAnalysisResult("BLOCKED", (finding,), remediation="EDIT_IN_PLACE")
    return None


def _membership(parent: DecompositionIssue, children: Sequence[DecompositionIssue]) -> Optional[dict[int, NormativeIssueManifest]]:
    issues = (parent, *children)
    membership: dict[int, NormativeIssueManifest] = {}
    for issue in issues:
        manifest = issue.manifest
        if manifest.issue_number in membership:
            return None
        membership[manifest.issue_number] = manifest
    return membership


def _structural_defect_finding(issue_number: int, defect: StructuralDefect) -> DecompositionFinding:
    location_text = "; ".join(f"line {location.line_number}: {location.text}" for location in defect.locations)
    detail = f"{defect.detail} ({location_text})" if location_text else defect.detail
    clarification = _STRUCTURAL_DEFECT_CLARIFICATIONS[defect.reason]
    return DecompositionFinding("invalid_issue_structure", (AffectedIssue(issue_number, ()),), detail, clarification)


def _structural_assessment_result(
    parent: DecompositionIssue,
    children: Sequence[DecompositionIssue],
) -> Optional[DecompositionAnalysisResult]:
    """Fail closed on role-aware structural defects before any model use.

    Uses the shared assessment from :mod:`role_structural_assessment` as the
    single structural oracle: a contract-free tracking parent with exactly one
    nonempty Objective and no Requirements is valid, and each implementation
    child must carry a valid explicit Requirements contract.
    """
    findings: list[DecompositionFinding] = []
    members = ((parent, ROLE_TRACKING_PARENT), *((child, ROLE_IMPLEMENTATION_CHILD) for child in children))
    for issue, role in members:
        assessment = assess_role_structure(issue.manifest, issue.body, role)
        if assessment.status == "ERROR":
            return _error(assessment.error or f"Issue #{issue.manifest.issue_number} structural assessment failed")
        findings.extend(_structural_defect_finding(issue.manifest.issue_number, defect) for defect in assessment.defects)
    if not findings:
        return None
    return DecompositionAnalysisResult("BLOCKED", tuple(findings), remediation="EDIT_IN_PLACE")


def parse_decomposition_analysis_response(
    response: str,
    parent: DecompositionIssue,
    children: Sequence[DecompositionIssue],
) -> DecompositionAnalysisResult:
    """Strictly validate model output against the supplied direct membership."""
    membership = _membership(parent, children)
    if membership is None:
        return _error("Decomposition membership contains duplicate Issue identities")
    try:
        payload = json.loads(response, object_pairs_hook=_reject_duplicate_json_members)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _error("Decomposition analyzer returned unparsable JSON")
    if not isinstance(payload, dict) or set(payload) != {"verdict", "remediation", "findings"}:
        return _error("Decomposition analyzer output does not match the required top-level schema")
    verdict, remediation, raw_findings = payload["verdict"], payload["remediation"], payload["findings"]
    if not isinstance(verdict, str) or verdict not in {"READY", "BLOCKED", "ERROR"} or not isinstance(raw_findings, list):
        return _error("Decomposition analyzer output contains an invalid verdict or findings value")
    if remediation not in {"NONE", "EDIT_IN_PLACE", "REISSUE_REQUIRED"}:
        return _error("Decomposition analyzer output contains an invalid remediation")
    if (verdict in {"READY", "ERROR"} and remediation != "NONE") or (verdict == "BLOCKED" and remediation not in {"EDIT_IN_PLACE", "REISSUE_REQUIRED"}):
        return _error("Decomposition analyzer verdict contradicts its remediation")
    if verdict == "ERROR":
        if raw_findings:
            return _error("An ERROR verdict cannot contain findings")
        return _error("The decomposition model could not produce a trustworthy verdict")

    findings: list[DecompositionFinding] = []
    finding_fields = {"category", "affected_issues", "explanation", "clarification"}
    affected_fields = {"issue_number", "requirement_ids"}
    for raw in raw_findings:
        if not isinstance(raw, dict) or set(raw) != finding_fields:
            return _error("Decomposition finding does not match the required schema")
        category, raw_affected = raw["category"], raw["affected_issues"]
        if not isinstance(category, str) or category not in DECOMPOSITION_FINDING_CATEGORIES:
            return _error("Decomposition finding uses an unknown category")
        if not isinstance(raw_affected, list) or not raw_affected:
            return _error("Decomposition finding must identify affected supplied Issues")
        explanation, clarification = raw["explanation"], raw["clarification"]
        if not isinstance(explanation, str) or not explanation.strip() or not isinstance(clarification, str) or not clarification.strip():
            return _error("Decomposition finding is incomplete")
        affected: list[AffectedIssue] = []
        seen_issues: set[int] = set()
        for item in raw_affected:
            if not isinstance(item, dict) or set(item) != affected_fields:
                return _error("Affected Issue entry does not match the required schema")
            issue_number, requirement_ids = item["issue_number"], item["requirement_ids"]
            if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number not in membership or issue_number in seen_issues:
                return _error("Finding references an Issue outside or inconsistently within supplied membership")
            known_ids = {requirement.requirement_id for requirement in membership[issue_number].requirements}
            if not isinstance(requirement_ids, list) or any(not isinstance(value, str) or value not in known_ids for value in requirement_ids) or len(requirement_ids) != len(set(requirement_ids)):
                return _error("Finding contains invalid Requirement IDs for an affected Issue")
            seen_issues.add(issue_number)
            affected.append(AffectedIssue(issue_number, tuple(requirement_ids)))
        if category in PARENT_COVERAGE_FINDING_CATEGORIES:
            parent_reference = next(
                (item for item in affected if item.issue_number == parent.manifest.issue_number),
                None,
            )
            if parent_reference is None or parent_reference.requirement_ids:
                return _error(f"{category} finding must identify the parent with an empty Requirement list")
        findings.append(DecompositionFinding(category, tuple(affected), explanation.strip(), clarification.strip()))

    if (verdict == "READY" and findings) or (verdict == "BLOCKED" and not findings):
        return _error("Decomposition verdict contradicts its findings")
    return DecompositionAnalysisResult(verdict, tuple(findings), remediation=remediation)


def analyze_issue_decomposition(
    parent: DecompositionIssue,
    children: Sequence[DecompositionIssue],
    *,
    backend_manager: Optional[BackendManager] = None,
    prompt_runner: Optional[Callable[[str], str]] = None,
    review_evidence: Optional[DecompositionReviewEvidence] = None,
) -> DecompositionAnalysisResult:
    """Analyze exactly the supplied parent and complete direct-child set.

    The parent is a contract-free tracking coordinator: it must carry no
    Requirements and exactly one nonempty Objective, and always contributes
    an empty implementation-Requirements manifest. Each child must carry a
    valid explicit Requirements contract. These role-aware structural
    defects are checked deterministically, before any model use.
    """
    membership = _membership(parent, children)
    if membership is None:
        return _error("Decomposition membership contains duplicate Issue identities")
    structural = _structural_assessment_result(parent, children)
    if structural is not None:
        return structural

    def issue_payload(issue: DecompositionIssue) -> dict[str, object]:
        return {
            "issue_number": issue.manifest.issue_number,
            "title": issue.manifest.title,
            "requirements": [{"requirement_id": requirement.requirement_id, "text": requirement.text} for requirement in issue.manifest.requirements],
            "body_evidence": issue.body,
        }

    review_evidence = review_evidence or _REVIEW_EVIDENCE.get()
    prompt = render_prompt(
        "issue.adversarial_decomposition_analysis",
        parent_specification=json.dumps(issue_payload(parent), ensure_ascii=False, indent=2),
        direct_child_specifications=json.dumps([issue_payload(child) for child in children], ensure_ascii=False, indent=2),
        durable_decomposition_baseline=review_evidence.baseline if review_evidence else "(No earlier decomposition baseline is available.)",
        prior_applied_decomposition_outcomes=("\n\n".join(review_evidence.prior_applied_outcomes) if review_evidence and review_evidence.prior_applied_outcomes else "(No prior applied material decomposition-review outcomes.)"),
        objective_evidence=(json.dumps([json.loads(objective_evidence_json(item)) for item in review_evidence.objectives], ensure_ascii=False, indent=2) if review_evidence and review_evidence.objectives else "(Required Objective evidence is unavailable.)"),
    )
    try:
        if prompt_runner is not None:
            response = prompt_runner(prompt)
        else:
            if backend_manager is None:
                from .cli_helpers import create_adversarial_validation_backend_manager

                backend_manager = create_adversarial_validation_backend_manager()
            if backend_manager is None:
                return _error("No strong decomposition-analysis backend is available")
            response = run_llm_prompt(prompt, backend_manager=backend_manager, is_noedit=True)
    except Exception as exc:
        return _error(f"Decomposition analysis execution failed: {type(exc).__name__}")
    return parse_decomposition_analysis_response(response, parent, children)
