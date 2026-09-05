"""Provider-independent semantic analysis of parent/child Issue contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .backend_manager import BackendManager, run_llm_prompt
from .prompt_loader import render_prompt
from .requirement_contract import NormativeIssueManifest
from .specification_analyzer import _reject_duplicate_json_members

DECOMPOSITION_FINDING_CATEGORIES = frozenset(
    {
        "missing_requirement_ownership",
        "cross_issue_contradiction",
        "unstated_cross_issue_dependency",
        "boundary_semantics_conflict",
        "decomposition_false_success",
    }
)


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

    @property
    def is_ready(self) -> bool:
        return self.verdict == "READY"


def _error(message: str) -> DecompositionAnalysisResult:
    return DecompositionAnalysisResult(verdict="ERROR", error=message)


def _membership(parent: DecompositionIssue, children: Sequence[DecompositionIssue]) -> Optional[dict[int, NormativeIssueManifest]]:
    issues = (parent, *children)
    membership: dict[int, NormativeIssueManifest] = {}
    for issue in issues:
        manifest = issue.manifest
        if manifest.issue_number in membership:
            return None
        membership[manifest.issue_number] = manifest
    return membership


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
    if not isinstance(payload, dict) or set(payload) != {"verdict", "findings"}:
        return _error("Decomposition analyzer output does not match the required top-level schema")
    verdict, raw_findings = payload["verdict"], payload["findings"]
    if not isinstance(verdict, str) or verdict not in {"READY", "BLOCKED", "ERROR"} or not isinstance(raw_findings, list):
        return _error("Decomposition analyzer output contains an invalid verdict or findings value")
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
        findings.append(DecompositionFinding(category, tuple(affected), explanation.strip(), clarification.strip()))

    if (verdict == "READY" and findings) or (verdict == "BLOCKED" and not findings):
        return _error("Decomposition verdict contradicts its findings")
    return DecompositionAnalysisResult(verdict, tuple(findings))


def analyze_issue_decomposition(
    parent: DecompositionIssue,
    children: Sequence[DecompositionIssue],
    *,
    parent_implemented_independently: bool = False,
    backend_manager: Optional[BackendManager] = None,
    prompt_runner: Optional[Callable[[str], str]] = None,
) -> DecompositionAnalysisResult:
    """Analyze exactly the supplied parent and complete direct-child set."""
    membership = _membership(parent, children)
    if membership is None:
        return _error("Decomposition membership contains duplicate Issue identities")
    for manifest in membership.values():
        if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
            return _error(manifest.error or f"Issue #{manifest.issue_number} requires a valid explicit normative Requirement manifest")

    def issue_payload(issue: DecompositionIssue) -> dict[str, object]:
        return {
            "issue_number": issue.manifest.issue_number,
            "title": issue.manifest.title,
            "requirements": [{"requirement_id": requirement.requirement_id, "text": requirement.text} for requirement in issue.manifest.requirements],
            "body_evidence": issue.body,
        }

    prompt = render_prompt(
        "issue.adversarial_decomposition_analysis",
        parent_implemented_independently=json.dumps(parent_implemented_independently),
        parent_specification=json.dumps(issue_payload(parent), ensure_ascii=False, indent=2),
        direct_child_specifications=json.dumps([issue_payload(child) for child in children], ensure_ascii=False, indent=2),
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
