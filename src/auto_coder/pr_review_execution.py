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
from .logger_config import get_logger
from .pr_review_cycle import ContractSnapshot, Finding, StrongPolicyIdentity
from .prompt_loader import render_prompt
from .security_utils import redact_string
from .utils import CommandExecutor, bind_command_execution_cwd, reset_command_execution_cwd

logger = get_logger(__name__)

REVIEW_RESPONSE_PREVIEW_LIMIT = 2000


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


def _bounded_review_preview(response: str) -> str:
    """Return a redacted, bounded preview suitable for normal diagnostics."""
    redacted = redact_string(response)
    if len(redacted) <= REVIEW_RESPONSE_PREVIEW_LIMIT:
        return redacted
    omitted = len(redacted) - REVIEW_RESPONSE_PREVIEW_LIMIT
    return f"{redacted[:REVIEW_RESPONSE_PREVIEW_LIMIT]}... [{omitted} characters omitted]"


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object while rejecting duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _strict_decoder() -> json.JSONDecoder:
    return json.JSONDecoder(object_pairs_hook=_reject_duplicate_object)


def _extract_claude_transport(response: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """Extract the authoritative answer from a recognized Claude CLI envelope.

    Returns ``(detected, answer, error)``.  When ``detected`` is True the
    caller must not fall back to any other candidate: either ``answer`` is the
    single terminal result string or ``error`` describes the transport failure.
    """
    if not isinstance(response, str) or not response.strip():
        return False, None, None
    stripped = response.strip()

    # Single JSON result envelope (``--output-format json``): the whole
    # captured output is one top-level ``type="result"`` object.
    try:
        whole = json.loads(stripped, object_pairs_hook=_reject_duplicate_object)
    except ValueError:
        whole = None
    if isinstance(whole, dict) and whole.get("type") == "result":
        subtype = whole.get("subtype")
        if subtype != "success":
            return True, None, f"Invalid Claude transport: unsuccessful result subtype {subtype!r}"
        if "is_error" in whole and whole.get("is_error") is not False:
            return True, None, "Invalid Claude transport: invalid is_error must be absent or false"
        result_text = whole.get("result")
        if not isinstance(result_text, str) or not result_text.strip():
            return True, None, "Invalid Claude transport: missing terminal result text"
        return True, result_text, None

    # Stream-json detection: any nonblank line that is a system/init or a
    # top-level result object marks the capture as a Claude event stream,
    # even when later lines are contaminated.  Detection alone forces the
    # strict stream validation below with no fallback to other candidates.
    nonblank = [line for line in response.splitlines() if line.strip()]
    if not nonblank:
        return False, None, None
    has_claude_marker = False
    for line in nonblank:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if (event.get("type") == "system" and event.get("subtype") == "init") or event.get("type") == "result":
            has_claude_marker = True
            break
    if not has_claude_marker:
        return False, None, None

    events: list[dict] = []
    for line_number, line in enumerate(nonblank, start=1):
        try:
            event = json.loads(line, object_pairs_hook=_reject_duplicate_object)
        except ValueError as exc:
            message = str(exc)
            if "duplicate" in message.lower():
                return True, None, f"Invalid Claude transport: duplicate JSON member at line {line_number}"
            return True, None, f"Invalid Claude transport: non-JSON content in event stream at line {line_number}"
        if not isinstance(event, dict):
            return True, None, f"Invalid Claude transport: event at line {line_number} is not an object"
        events.append(event)

    for line_number, event in enumerate(events, start=1):
        if event.get("type") == "error":
            return True, None, f"Invalid Claude transport: fatal error event at line {line_number}"

    init_indices = [index for index, event in enumerate(events) if event.get("type") == "system" and event.get("subtype") == "init"]
    if not init_indices:
        return True, None, "Invalid Claude transport: missing init event"
    result_indices = [index for index, event in enumerate(events) if event.get("type") == "result"]
    if not result_indices:
        return True, None, "Invalid Claude transport: missing terminal result"
    if len(result_indices) > 1:
        return True, None, f"Invalid Claude transport: multiple terminal results ({len(result_indices)})"
    result_index = result_indices[0]
    if result_index != len(events) - 1:
        return True, None, "Invalid Claude transport: event after terminal result"

    terminal = events[result_index]
    terminal_line = result_index + 1
    if terminal.get("subtype") != "success":
        return True, None, f"Invalid Claude transport: unsuccessful result subtype {terminal.get('subtype')!r} at line {terminal_line}"
    if "is_error" in terminal and terminal.get("is_error") is not False:
        return True, None, f"Invalid Claude transport: invalid is_error at line {terminal_line} must be absent or false"
    result_text = terminal.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        return True, None, f"Invalid Claude transport: missing terminal result text at line {terminal_line}"

    init_session_ids = {str(event.get("session_id")) for event in (events[index] for index in init_indices) if isinstance(event.get("session_id"), str) and str(event.get("session_id")).strip()}
    terminal_session_id = terminal.get("session_id")
    if isinstance(terminal_session_id, str) and terminal_session_id.strip():
        if len(init_session_ids) > 1 or (len(init_session_ids) == 1 and terminal_session_id not in init_session_ids):
            return True, None, "Invalid Claude transport: mismatched session_id between init and result"
    if len(init_session_ids) > 1:
        return True, None, "Invalid Claude transport: mismatched session_id between init events"
    return True, result_text, None


def _extract_fenced_review_document(answer: str) -> Tuple[Optional[dict], Optional[str], bool]:
    """Extract a review object from markdown-fenced presentation.

    Returns ``(document, error, handled)`` where ``handled`` indicates the
    answer contained fenced-block syntax and the non-fenced path must not run.
    """
    parts = answer.split("```")
    complete_blocks = (len(parts) - 1) // 2
    if len(parts) < 3:
        if "```" in answer:
            return None, "Reviewer output is not one JSON object: malformed fenced block", True
        return None, None, False
    if complete_blocks != 1 or len(parts) != 3:
        return None, "Reviewer output is not one JSON object: competing candidates from multiple fenced blocks", True
    block_content = parts[1]
    after = parts[2]
    before = parts[0]
    stripped_block = block_content.strip()
    if stripped_block.startswith("{"):
        language_tag = ""
        json_text = block_content.strip()
    else:
        if "\n" in block_content:
            first_line, rest = block_content.split("\n", 1)
            language_tag = first_line.strip()
            json_text = rest.strip()
        else:
            language_tag = block_content.strip()
            json_text = ""
        if language_tag.lower() != "json":
            return None, f"Reviewer output is not one JSON object: unsupported fence language {language_tag!r}", True
        if not json_text:
            return None, "Reviewer output is not one JSON object: empty fenced JSON document", True
    try:
        document = json.loads(json_text, object_pairs_hook=_reject_duplicate_object)
    except ValueError as exc:
        message = str(exc)
        if "duplicate" in message.lower():
            return None, "Reviewer output is not one JSON object: duplicate JSON member", True
        return None, "Reviewer output is not one JSON object: malformed fenced JSON document", True
    if not isinstance(document, dict):
        return None, "Reviewer output is not one JSON object: fenced document is not an object", True
    outside = before + after
    if "{" in outside or "[" in outside:
        return None, "Reviewer output is not one JSON object: competing candidates outside fenced block", True
    return document, None, True


def _extract_single_review_document(answer: str) -> Tuple[Optional[dict], Optional[str]]:
    """Extract exactly one top-level review object from authoritative text."""
    if not isinstance(answer, str) or not answer.strip():
        return None, "Reviewer output is not one JSON object: empty response"
    fenced_document, fenced_error, handled = _extract_fenced_review_document(answer)
    if handled:
        return fenced_document, fenced_error

    decoder = _strict_decoder()
    dict_spans: list[Tuple[int, int, dict]] = []
    list_spans: list[Tuple[int, int, list]] = []
    cursor = 0
    while cursor < len(answer):
        start = answer.find("{", cursor)
        bracket = answer.find("[", cursor)
        if start == -1 or (bracket != -1 and bracket < start):
            start = bracket
        if start == -1:
            break
        try:
            value, end_offset = decoder.raw_decode(answer[start:])
        except ValueError as exc:
            if "duplicate" in str(exc).lower():
                return None, "Reviewer output is not one JSON object: duplicate JSON member"
            cursor = start + 1
            continue
        end = start + end_offset
        if isinstance(value, dict):
            dict_spans.append((start, end, value))
        elif isinstance(value, list):
            list_spans.append((start, end, value))
        cursor = start + 1
        if cursor >= len(answer):
            break

    if not dict_spans:
        if list_spans:
            return None, "Reviewer output is not one JSON object: top-level array is not an accepted review"
        return None, "Reviewer output is not one JSON object"

    outermost: list[Tuple[int, int, dict]] = []
    for index, (start, end, value) in enumerate(dict_spans):
        contained = False
        for other_index, (other_start, other_end, _other) in enumerate(dict_spans):
            if other_index == index:
                continue
            if other_start <= start and end <= other_end and (other_start, other_end) != (start, end):
                contained = True
                break
        if not contained:
            outermost.append((start, end, value))
    if len(outermost) != 1:
        return None, "Reviewer output is not one JSON object: competing candidates from multiple JSON objects"
    selected_start, selected_end, selected = outermost[0]

    for list_start, list_end, _value in list_spans:
        if list_start <= selected_start and selected_end <= list_end:
            return None, "Reviewer output is not one JSON object: top-level array is not an accepted review"
        if not (list_end <= selected_start or list_start >= selected_end):
            # Overlapping but not containing is still ambiguous; containing
            # lists are rejected above and nested lists never reach here
            # because nested list spans are inside the selected object.
            pass
        if list_end <= selected_start or list_start >= selected_end:
            return None, "Reviewer output is not one JSON object: competing candidates from array and object"

    outside = answer[:selected_start] + answer[selected_end:]
    if "{" in outside or "[" in outside:
        return None, "Reviewer output is not one JSON object: competing candidates outside review document"
    return selected, None


def _log_review_parse_failure(reason: str, response: str) -> None:
    preview = _bounded_review_preview(response) if isinstance(response, str) and response else "<empty>"
    state = "empty" if not isinstance(response, str) or not response.strip() else "non-empty"
    logger.warning("Two-tier review output failed parsing: reason={}, response_state={}, response_length={}, preview={!r}", reason, state, len(response) if isinstance(response, str) else 0, preview)


def parse_review_result(response: str, expected: ReviewExecutionInput, reviewer_provenance: str) -> ReviewExecutionResult:
    """Fail closed on malformed, stale, incomplete, or identity-mismatched output."""
    detected, authoritative, transport_error = _extract_claude_transport(response)
    if detected:
        if transport_error is not None:
            _log_review_parse_failure(transport_error, response)
            return _diagnostic(expected, reviewer_provenance, transport_error)
        assert authoritative is not None
        answer_text = authoritative
    else:
        answer_text = response
    raw, presentation_error = _extract_single_review_document(answer_text)
    if presentation_error is not None or not isinstance(raw, dict):
        _log_review_parse_failure(presentation_error or "Reviewer output is not an object", response)
        return _diagnostic(expected, reviewer_provenance, presentation_error or "Reviewer output is not an object")

    identities = {
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
    if verdict == "PASS" and unresolved:
        return _diagnostic(expected, reviewer_provenance, "PASS contradicts unresolved finding dispositions")
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
