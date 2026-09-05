"""Provider-independent adversarial analysis of normative Issue contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from .backend_manager import BackendManager, run_llm_prompt
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

    @property
    def is_ready(self) -> bool:
        return self.verdict == "READY"


def _error(message: str) -> SpecificationAnalysisResult:
    return SpecificationAnalysisResult(verdict="ERROR", error=message)


def parse_specification_analysis_response(response: str, manifest: NormativeIssueManifest) -> SpecificationAnalysisResult:
    """Validate a model response without recovering a verdict from partial prose."""
    try:
        payload = json.loads(response)
    except (json.JSONDecodeError, TypeError):
        return _error("Specification analyzer returned unparsable JSON")

    if not isinstance(payload, dict) or set(payload) != {"verdict", "findings"}:
        return _error("Specification analyzer output does not match the required top-level schema")
    verdict = payload["verdict"]
    raw_findings = payload["findings"]
    if verdict not in {"READY", "BLOCKED", "ERROR"} or not isinstance(raw_findings, list):
        return _error("Specification analyzer output contains an invalid verdict or findings value")
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
        if category not in SPECIFICATION_FINDING_CATEGORIES:
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
    return SpecificationAnalysisResult(verdict=verdict, findings=tuple(findings))


def analyze_issue_specification(
    manifest: NormativeIssueManifest,
    issue_body: str,
    parent_context: Optional[str] = None,
    backend_manager: Optional[BackendManager] = None,
    prompt_runner: Optional[Callable[[str], str]] = None,
) -> SpecificationAnalysisResult:
    """Adversarially decide whether an Issue is an independent contract.

    The caller supplies the shared authoritative manifest.  This operation never
    reparses ``issue_body`` and never inspects a repository.
    """
    if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
        return _error(manifest.error or "A valid explicit normative Requirement manifest is required")

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
