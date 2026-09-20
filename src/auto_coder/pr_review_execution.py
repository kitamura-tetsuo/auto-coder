"""Read-only execution boundary for two-tier PR review.

The durable state machine lives in :mod:`pr_review_cycle`.  This module owns
only role-specific prompt assembly, backend invocation, and strict validation
of the portable result passed to that state machine.  It deliberately has no
GitHub mutation or merge authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from .backend_manager import BackendManager, run_llm_prompt
from .pr_review_cycle import ContractSnapshot, Finding, StrongPolicyIdentity
from .prompt_loader import render_prompt
from .utils import CommandExecutor, bind_command_execution_cwd, reset_command_execution_cwd


class ReviewMode(str, Enum):
    STRONG_AUDIT = "STRONG_AUDIT"
    ORDINARY_CLOSURE = "ORDINARY_CLOSURE"


class ScopeAssessment(str, Enum):
    BOUNDED = "BOUNDED"
    EXPANDED = "EXPANDED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReviewExecutionInput:
    """Complete, provider-independent input for one reviewer invocation."""

    mode: ReviewMode
    round_id: str
    attempt_id: str
    head_sha: str
    base_sha: str
    contract: ContractSnapshot
    policy: StrongPolicyIdentity
    repository_evidence: str
    diff_evidence: str
    finding_set_revision: int = 0
    findings: Tuple[Finding, ...] = field(default_factory=tuple)
    audited_head_sha: str = ""


@dataclass(frozen=True)
class FindingDisposition:
    finding_id: str
    status: str
    evidence: str


@dataclass(frozen=True)
class ReviewExecutionResult:
    mode: ReviewMode
    round_id: str
    attempt_id: str
    head_sha: str
    base_sha: str
    contract_identity: str
    policy_identity: str
    finding_set_revision: int
    reviewer_provenance: str
    verdict: str
    findings: Tuple[Finding, ...] = field(default_factory=tuple)
    dispositions: Tuple[FindingDisposition, ...] = field(default_factory=tuple)
    scope: Optional[ScopeAssessment] = None
    scope_evidence: str = ""
    diagnostic: str = ""

    @property
    def is_complete(self) -> bool:
        return not self.diagnostic

    @property
    def grants_closure_evidence(self) -> bool:
        return self.is_complete and self.mode is ReviewMode.ORDINARY_CLOSURE and self.verdict == "PASS" and self.scope is ScopeAssessment.BOUNDED


def build_review_prompt(review_input: ReviewExecutionInput) -> str:
    """Render a self-contained role prompt; no provider memory is an input."""
    if review_input.mode is ReviewMode.STRONG_AUDIT and review_input.findings:
        raise ValueError("STRONG_AUDIT cannot preload prior findings")
    if review_input.mode is ReviewMode.ORDINARY_CLOSURE and not review_input.findings:
        raise ValueError("ORDINARY_CLOSURE requires the accepted finding bundle")
    findings = json.dumps([_finding_payload(item) for item in review_input.findings], indent=2, sort_keys=True)
    return render_prompt(
        "pr.two_tier_review_execution",
        mode=review_input.mode.value,
        round_id=review_input.round_id,
        attempt_id=review_input.attempt_id,
        head_sha=review_input.head_sha,
        base_sha=review_input.base_sha,
        contract_identity=review_input.contract.identity,
        policy_identity=review_input.policy.identity,
        issue_ids=json.dumps(review_input.contract.issue_ids),
        requirements_text=review_input.contract.requirements_text,
        audited_head_sha=review_input.audited_head_sha or review_input.head_sha,
        finding_set_revision=review_input.finding_set_revision,
        findings=findings,
        repository_evidence=review_input.repository_evidence,
        cumulative_diff=review_input.diff_evidence,
    )


def execute_review(
    review_input: ReviewExecutionInput,
    backend_manager: BackendManager,
    execution_cwd: str,
) -> ReviewExecutionResult:
    """Invoke a reviewer in read-only mode against the exact requested head."""
    execution_token = bind_command_execution_cwd(execution_cwd)
    try:
        _verify_head(execution_cwd, review_input.head_sha)
        response = run_llm_prompt(build_review_prompt(review_input), backend_manager=backend_manager, is_noedit=True)
        _verify_head(execution_cwd, review_input.head_sha)
    finally:
        reset_command_execution_cwd(execution_token)
    identity = backend_manager.get_current_backend_identity()
    provenance = "/".join(str(part) for part in identity) if isinstance(identity, tuple) else "unavailable"
    return parse_review_result(response, review_input, provenance)


def parse_review_result(response: str, expected: ReviewExecutionInput, reviewer_provenance: str) -> ReviewExecutionResult:
    """Fail closed on malformed, stale, incomplete, or identity-mismatched output."""
    try:
        raw = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        return _diagnostic(expected, reviewer_provenance, "Reviewer output is not one JSON object")
    if not isinstance(raw, dict):
        return _diagnostic(expected, reviewer_provenance, "Reviewer output is not an object")

    identities = {
        "mode": expected.mode.value,
        "round_id": expected.round_id,
        "attempt_id": expected.attempt_id,
        "head_sha": expected.head_sha,
        "base_sha": expected.base_sha,
        "contract_identity": expected.contract.identity,
        "policy_identity": expected.policy.identity,
        "finding_set_revision": expected.finding_set_revision,
    }
    for key, value in identities.items():
        if raw.get(key) != value:
            return _diagnostic(expected, reviewer_provenance, f"Mismatched or missing {key}")
    verdict = raw.get("verdict")
    if verdict not in {"PASS", "FINDINGS", "INCONCLUSIVE"}:
        return _diagnostic(expected, reviewer_provenance, "Invalid verdict")

    if expected.mode is ReviewMode.STRONG_AUDIT:
        findings = _parse_findings(raw.get("findings"), expected.round_id)
        if findings is None or (verdict == "FINDINGS") != bool(findings) or (verdict == "PASS" and findings):
            return _diagnostic(expected, reviewer_provenance, "Strong finding bundle is incomplete or contradicts verdict")
        return _result(expected, reviewer_provenance, str(verdict), findings=findings)

    dispositions = _parse_dispositions(raw.get("dispositions"))
    expected_ids = {item.finding_id for item in expected.findings}
    if dispositions is None or {item.finding_id for item in dispositions} != expected_ids:
        return _diagnostic(expected, reviewer_provenance, "Every accepted finding requires exactly one disposition")
    scope_text = raw.get("scope")
    try:
        scope = ScopeAssessment(scope_text)
    except ValueError:
        return _diagnostic(expected, reviewer_provenance, "Missing or invalid cumulative scope assessment")
    scope_evidence = raw.get("scope_evidence")
    if not isinstance(scope_evidence, str) or not scope_evidence.strip():
        return _diagnostic(expected, reviewer_provenance, "Cumulative scope assessment requires evidence")
    unresolved = any(item.status in {"OPEN", "INCONCLUSIVE"} for item in dispositions)
    if verdict == "PASS" and (unresolved or scope is not ScopeAssessment.BOUNDED):
        return _diagnostic(expected, reviewer_provenance, "PASS contradicts dispositions or cumulative scope")
    new_findings = _parse_findings(raw.get("findings", []), expected.round_id)
    if new_findings is None:
        return _diagnostic(expected, reviewer_provenance, "New ordinary findings are malformed")
    if new_findings and verdict == "PASS":
        return _diagnostic(expected, reviewer_provenance, "PASS cannot discard newly discovered findings")
    return _result(expected, reviewer_provenance, str(verdict), findings=new_findings, dispositions=dispositions, scope=scope, scope_evidence=scope_evidence)


def _parse_findings(value: object, origin: str) -> Optional[Tuple[Finding, ...]]:
    if not isinstance(value, list):
        return None
    parsed = []
    required = ("finding_id", "requirement_ids", "requirement_texts", "counterexample", "expected_behavior", "actual_behavior", "evidence", "affected_boundary", "focused_regression_scenario")
    for item in value:
        if not isinstance(item, dict) or any(key not in item for key in required):
            return None
        strings = [item[key] for key in required if key not in {"requirement_ids", "requirement_texts"}]
        if not all(isinstance(part, str) and part.strip() for part in strings):
            return None
        ids, texts = item["requirement_ids"], item["requirement_texts"]
        if not isinstance(ids, list) or not ids or not isinstance(texts, list) or len(ids) != len(texts) or not all(isinstance(x, str) and x.strip() for x in ids + texts):
            return None
        regression = bool(item.get("is_regression_gap", False))
        gap_fields = ("plausible_incorrect_implementation", "why_tests_admit_it", "material_consequence")
        if regression and any(not isinstance(item.get(key), str) or not item[key].strip() for key in gap_fields):
            return None
        parsed.append(
            Finding(
                finding_id=item["finding_id"],
                origin_round_id=origin,
                requirement_ids=tuple(ids),
                requirement_texts=tuple(texts),
                counterexample=item["counterexample"],
                expected_behavior=item["expected_behavior"],
                actual_behavior=item["actual_behavior"],
                evidence=item["evidence"],
                affected_boundary=item["affected_boundary"],
                is_regression_gap=regression,
                plausible_incorrect_implementation=str(item.get("plausible_incorrect_implementation", "")),
                why_tests_admit_it=str(item.get("why_tests_admit_it", "")),
                material_consequence=str(item.get("material_consequence", "")),
                focused_regression_scenario=item["focused_regression_scenario"],
            )
        )
    if len({item.finding_id for item in parsed}) != len(parsed):
        return None
    return tuple(parsed)


def _parse_dispositions(value: object) -> Optional[Tuple[FindingDisposition, ...]]:
    if not isinstance(value, list):
        return None
    parsed = []
    for item in value:
        if not isinstance(item, dict) or item.get("status") not in {"FIXED", "INVALID", "OPEN", "INCONCLUSIVE"}:
            return None
        finding_id, evidence = item.get("finding_id"), item.get("evidence")
        if not isinstance(finding_id, str) or not finding_id.strip() or not isinstance(evidence, str) or not evidence.strip():
            return None
        parsed.append(FindingDisposition(finding_id, item["status"], evidence))
    if len({item.finding_id for item in parsed}) != len(parsed):
        return None
    return tuple(parsed)


def _finding_payload(item: Finding) -> Mapping[str, object]:
    return {
        name: getattr(item, name)
        for name in (
            "finding_id",
            "origin_round_id",
            "requirement_ids",
            "requirement_texts",
            "counterexample",
            "expected_behavior",
            "actual_behavior",
            "evidence",
            "affected_boundary",
            "is_regression_gap",
            "plausible_incorrect_implementation",
            "why_tests_admit_it",
            "material_consequence",
            "focused_regression_scenario",
        )
    }


def _result(
    expected: ReviewExecutionInput,
    provenance: str,
    verdict: str,
    findings: Tuple[Finding, ...] = (),
    dispositions: Tuple[FindingDisposition, ...] = (),
    scope: Optional[ScopeAssessment] = None,
    scope_evidence: str = "",
    diagnostic: str = "",
) -> ReviewExecutionResult:
    return ReviewExecutionResult(
        mode=expected.mode,
        round_id=expected.round_id,
        attempt_id=expected.attempt_id,
        head_sha=expected.head_sha,
        base_sha=expected.base_sha,
        contract_identity=expected.contract.identity,
        policy_identity=expected.policy.identity,
        finding_set_revision=expected.finding_set_revision,
        reviewer_provenance=provenance,
        verdict=verdict,
        findings=findings,
        dispositions=dispositions,
        scope=scope,
        scope_evidence=scope_evidence,
        diagnostic=diagnostic,
    )


def _diagnostic(expected: ReviewExecutionInput, provenance: str, reason: str) -> ReviewExecutionResult:
    return _result(expected, provenance, "INCONCLUSIVE", diagnostic=reason)


def _verify_head(cwd: str, expected_head: str) -> None:
    result = CommandExecutor.run_command(["git", "rev-parse", "HEAD"], cwd=str(Path(cwd)))
    if not result.success or result.stdout.strip().lower() != expected_head.lower():
        raise RuntimeError(f"Review snapshot mismatch: expected {expected_head}, found {result.stdout.strip() or 'unavailable'}")
