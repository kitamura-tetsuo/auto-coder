"""Adversarial validation functionality for Auto-Coder automation engine.

Validates PR implementation against the original issue specification / acceptance criteria
using a strong model to catch false-success PRs before merge.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .automation_config import AutomationConfig
from .backend_manager import BackendManager, run_llm_prompt
from .issue_context import get_linked_issues_context
from .logger_config import get_logger
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .security_utils import redact_string
from .trace_logger import get_trace_logger
from .util.gh_cache import GitHubClient

logger = get_logger(__name__)

ADVERSARIAL_RESPONSE_PREVIEW_LIMIT = 2000
ADVERSARIAL_VALIDATION_COMMENT_LIMIT = 60000
FILE_EVIDENCE_HEADER_RESERVE = 96


@dataclass
class AdversarialValidationFinding:
    """Individual specification violation or test gap identified during validation."""

    violated_requirement: str = ""
    counterexample: str = ""
    test_gap: str = ""
    suggested_regression_scenario: str = ""


@dataclass
class AdversarialValidationResult:
    """Outcome of adversarial PR validation against the specification oracle.

    Fail-closed design:
    - Only 'PASS' with 0 findings evaluates to is_pass=True.
    - Parsed concrete findings normalize the aggregate result to 'NEEDS_FIX'.
    - Empty, malformed, 'BLOCKED', 'INCONCLUSIVE', or 'ERROR' evaluates to is_blocked=True.
    """

    result: str = "ERROR"  # "PASS", "NEEDS_FIX", "BLOCKED", "INCONCLUSIVE", "ERROR"
    summary: str = ""
    findings: List[AdversarialValidationFinding] = field(default_factory=list)
    raw_response: str = ""
    dynamic_check_requested: Optional[str] = None
    diagnostic_category: Optional[str] = None
    diagnostic_reason: Optional[str] = None

    @property
    def is_pass(self) -> bool:
        """Return True if validation explicitly passed with no blocking findings."""
        return self.result.strip().upper() == "PASS" and len(self.findings) == 0

    @property
    def needs_fix(self) -> bool:
        """Return True if validation explicitly determined NEEDS_FIX with findings."""
        return self.result.strip().upper() == "NEEDS_FIX" and len(self.findings) > 0

    @property
    def is_blocked(self) -> bool:
        """Return True if validation could not complete or produced non-pass status."""
        return not self.is_pass and not self.needs_fix


@dataclass
class AdversarialValidationContext:
    """Context gathered for adversarial PR validation."""

    repo_name: str = ""
    pr_number: int = 0
    pr_title: str = ""
    pr_body: str = ""
    pr_diff: str = ""
    all_changed_files: List[str] = field(default_factory=list)
    changed_tests: List[str] = field(default_factory=list)
    issue_context: str = ""
    is_diff_truncated: bool = False
    unverified_files: List[str] = field(default_factory=list)

    @property
    def has_complete_file_coverage(self) -> bool:
        """Return whether every changed file has complete patch evidence."""
        return not self.unverified_files


@dataclass
class FileDiffEvidence:
    """Bounded patch evidence and coverage state for one changed file."""

    path: str = ""
    patch: str = ""
    original_size: int = 0
    is_complete: bool = False


def adversarial_validation_comment_marker(head_sha: str) -> str:
    """Return the stable marker used to deduplicate validation comments."""
    return f"<!-- auto-coder-adversarial-validation:{head_sha} -->"


def format_adversarial_validation_comment(result: AdversarialValidationResult, head_sha: str) -> str:
    """Render a bounded, human-readable PR comment for a validation result.

    The raw CLI response is intentionally excluded because it can contain a very
    large event stream.  The parsed summary and every structured finding retain
    the actionable reviewer output while keeping the GitHub comment usable.
    """
    status = result.result.strip().upper() or "ERROR"
    status_icon = "✅" if result.is_pass else "❌" if result.needs_fix else "⚠️"
    lines = [
        adversarial_validation_comment_marker(head_sha),
        f"## {status_icon} Auto-Coder adversarial validation: {status}",
        "",
        f"Validated commit: `{head_sha}`",
        "",
        result.summary.strip() or "No validation summary was provided.",
    ]

    if result.dynamic_check_requested:
        lines.extend(["", f"Dynamic check requested: `{result.dynamic_check_requested}`"])

    if result.findings:
        lines.extend(["", f"### Findings ({len(result.findings)})"])
        for index, finding in enumerate(result.findings, start=1):
            lines.extend(["", f"#### {index}. Violated requirement", finding.violated_requirement.strip() or "Not specified."])
            if finding.counterexample.strip():
                lines.extend(["", "**Counterexample**", "", finding.counterexample.strip()])
            if finding.test_gap.strip():
                lines.extend(["", "**Test gap**", "", finding.test_gap.strip()])
            if finding.suggested_regression_scenario.strip():
                lines.extend(["", "**Suggested regression scenario**", "", finding.suggested_regression_scenario.strip()])

    if result.diagnostic_category or result.diagnostic_reason:
        lines.extend(["", "### Diagnostic"])
        if result.diagnostic_category:
            lines.append(f"Category: `{result.diagnostic_category}`")
        if result.diagnostic_reason:
            lines.append(f"Reason: {result.diagnostic_reason}")

    comment = "\n".join(lines)
    if len(comment) > ADVERSARIAL_VALIDATION_COMMENT_LIMIT:
        suffix = "\n\n_Comment truncated by Auto-Coder; the complete response remains in the structured LLM interaction log._"
        comment = comment[: ADVERSARIAL_VALIDATION_COMMENT_LIMIT - len(suffix)].rstrip() + suffix
    return comment


def is_test_file(file_path: str) -> bool:
    """Determine whether a file path corresponds to a test file."""
    path_lower = file_path.lower()
    return path_lower.startswith("tests/") or path_lower.startswith("test/") or "/tests/" in path_lower or "/test/" in path_lower or path_lower.endswith("_test.py") or path_lower.endswith("test.py") or ".spec." in path_lower or ".test." in path_lower


def _decode_git_path_token(token: str) -> str:
    """Decode a pathname token emitted with Git's ``core.quotePath`` format."""
    value = token.strip()
    if not (value.startswith('"') and value.endswith('"')):
        return value

    try:
        decoded = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return value
    if not isinstance(decoded, str):
        return value

    # Git writes non-ASCII UTF-8 bytes as C-style octal escapes. Python's
    # string-literal decoder maps each byte to the same Latin-1 code point, so
    # convert those code points back to bytes before decoding the pathname.
    try:
        return decoded.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return decoded


def _extract_diff_header_path(line: str) -> Optional[str]:
    """Extract the destination path from quoted or unquoted ``diff --git`` headers."""
    prefix = "diff --git "
    if not line.startswith(prefix):
        return None
    payload = line[len(prefix) :]

    if payload.startswith('"'):
        quoted_pair = re.fullmatch(r'("(?:\\.|[^"\\])*")\s+("(?:\\.|[^"\\])*")', payload)
        if not quoted_pair:
            return None
        destination = _decode_git_path_token(quoted_pair.group(2))
    else:
        unquoted_pair = re.fullmatch(r"a/(.*)\s+b/(.*)", payload)
        if not unquoted_pair:
            return None
        destination = f"b/{unquoted_pair.group(2)}"

    return destination[2:] if destination.startswith("b/") else destination


def _extract_marker_path(line: str) -> Optional[str]:
    """Extract a path from a unified diff ``+++`` destination marker."""
    if not line.startswith("+++ "):
        return None
    destination = _decode_git_path_token(line[4:])
    if destination == "/dev/null":
        return None
    return destination[2:] if destination.startswith("b/") else destination


def extract_all_changed_files(pr_diff: str) -> List[str]:
    """Extract all modified or added file paths from a unified diff string.

    Args:
        pr_diff: Unified diff string

    Returns:
        List of all changed file paths
    """
    if not pr_diff:
        return []

    files = [file_patch.path for file_patch in _split_diff_by_file(pr_diff)]
    if files:
        return files

    # Retain support for a bare unified patch without ``diff --git`` headers.
    for line in pr_diff.splitlines():
        path = _extract_marker_path(line)
        if path and path not in files:
            files.append(path)

    return files


def extract_changed_test_files(pr_diff: str) -> List[str]:
    """Extract test file paths modified or added in the PR diff.

    Args:
        pr_diff: Unified diff string

    Returns:
        List of changed test file paths
    """
    all_files = extract_all_changed_files(pr_diff)
    return [f for f in all_files if is_test_file(f)]


def _split_diff_by_file(pr_diff: str) -> List[FileDiffEvidence]:
    """Split a unified Git diff into ordered per-file patches."""
    matches = list(re.finditer(r"^diff --git .*$", pr_diff, re.MULTILINE))
    evidence: List[FileDiffEvidence] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(pr_diff)
        patch = pr_diff[match.start() : end].rstrip()
        path = next((_extract_marker_path(line) for line in patch.splitlines() if _extract_marker_path(line)), None)
        path = path or _extract_diff_header_path(match.group(0)) or f"<unparsed diff header at character {match.start()}>"
        evidence.append(FileDiffEvidence(path=path, patch=patch, original_size=len(patch)))
    return evidence


def _allocate_file_evidence_budget(file_patches: List[FileDiffEvidence], budget: int) -> List[int]:
    """Water-fill a total patch budget so small late files remain fully visible."""
    allocations = [0] * len(file_patches)
    remaining = max(0, budget)
    if not file_patches or remaining <= 0:
        return allocations

    # Give every file a small deterministic excerpt first. Coverage metadata is
    # always emitted separately even when the patch budget is exceptionally low.
    base_allocation = min(32, remaining // len(file_patches))
    for index, file_patch in enumerate(file_patches):
        allocations[index] = min(base_allocation, file_patch.original_size)
        remaining -= allocations[index]

    pending = [index for index, file_patch in enumerate(file_patches) if allocations[index] < file_patch.original_size]

    # Complete the smallest patches next. This is what prevents a huge early
    # generated file from hiding a later focused implementation or security test.
    for index in sorted(pending, key=lambda item: file_patches[item].original_size - allocations[item]):
        additional = file_patches[index].original_size - allocations[index]
        if additional <= remaining:
            allocations[index] += additional
            remaining -= additional

    pending = [index for index in pending if allocations[index] < file_patches[index].original_size]

    while pending and remaining > 0:
        fair_share = remaining // len(pending)
        if fair_share <= 0:
            for index in pending[:remaining]:
                allocations[index] += 1
            break
        for offset, index in enumerate(pending):
            allocations[index] += fair_share + (1 if offset < remaining % len(pending) else 0)
        break

    return allocations


def _bounded_file_patch(patch: str, allocation: int) -> str:
    """Represent one oversized patch with bounded beginning and ending evidence."""
    if allocation <= 0:
        return ""
    if len(patch) <= allocation:
        return patch
    marker = "\n... [per-file patch evidence omitted] ...\n"
    if allocation <= len(marker):
        return patch[:allocation]

    content_budget = max(0, allocation - len(marker))
    prefix_size = (content_budget + 1) // 2
    suffix_size = content_budget // 2
    return patch[:prefix_size] + marker + (patch[-suffix_size:] if suffix_size else "")


def build_file_aware_diff(pr_diff: str, max_evidence_size: int) -> tuple[str, List[str]]:
    """Build bounded, per-file diff evidence without allowing early files to starve later ones.

    Every file receives an independent coverage record. Small patches are included
    completely before the remaining budget is shared across oversized patches.
    Files without complete patches are explicitly returned as unverified so a
    downstream PASS can be rejected deterministically.
    """
    file_patches = _split_diff_by_file(pr_diff)
    if not file_patches:
        if len(pr_diff) <= max_evidence_size:
            return pr_diff, []
        return _bounded_file_patch(pr_diff, max_evidence_size), ["<unparsed unified diff>"]

    patch_budget = max(0, max_evidence_size - FILE_EVIDENCE_HEADER_RESERVE * len(file_patches))
    allocations = _allocate_file_evidence_budget(file_patches, patch_budget)
    rendered: List[str] = []
    unverified_files: List[str] = []

    for file_patch, allocation in zip(file_patches, allocations):
        file_patch.is_complete = allocation >= file_patch.original_size
        file_patch.patch = _bounded_file_patch(file_patch.patch, allocation)
        coverage = "COMPLETE" if file_patch.is_complete else "PARTIAL / UNVERIFIED"
        rendered.append(f"### Changed file: {file_patch.path}\n" f"Coverage: {coverage} ({len(file_patch.patch)}/{file_patch.original_size} patch characters supplied)\n" f"{file_patch.patch or '[No patch content fits within the bounded evidence budget.]'}")
        if not file_patch.is_complete:
            unverified_files.append(file_patch.path)

    if unverified_files:
        rendered.append("## COVERAGE INCOMPLETE\n" "The following material changed files have partial or unavailable patch evidence. " "A PASS verdict is forbidden unless their irrelevance to every Issue requirement is explicitly established: " + ", ".join(unverified_files))
    return "\n\n".join(rendered), unverified_files


def build_adversarial_validation_context(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> AdversarialValidationContext:
    """Compile issue specification, PR diff, and changed tests for validation.

    Recovers oracle/specification from PR body, title, branch name, and session.
    Uses a bounded, file-aware budget so an early oversized patch cannot hide
    later implementation or test files.

    Args:
        repo_name: Repository name in 'owner/repo' format
        pr_data: Pull request metadata dictionary
        config: AutomationConfig instance
        github_client: Optional GitHubClient instance

    Returns:
        AdversarialValidationContext populated with review data
    """
    pr_number = pr_data.get("number", 0)
    pr_title = pr_data.get("title", "Unknown")
    pr_body = pr_data.get("body") or ""

    client = github_client
    if client is None:
        try:
            client = GitHubClient.get_instance()
        except Exception:
            client = None

    # Retrieve PR diff
    pr_diff = ""
    is_diff_truncated = False
    all_changed_files: List[str] = []
    unverified_files: List[str] = []

    if client:
        try:
            raw_diff = client.get_pr_diff(repo_name, pr_number)
            if raw_diff:
                all_changed_files = extract_all_changed_files(raw_diff)
                max_diff_size = config.MAX_PR_DIFF_SIZE * 3
                pr_diff, unverified_files = build_file_aware_diff(raw_diff, max_diff_size)
                is_diff_truncated = bool(unverified_files)
        except Exception as e:
            logger.warning(f"Could not retrieve diff for PR #{pr_number}: {e}")

    # Extract linked issues context (from body, title, branch name, session)
    issue_context = get_linked_issues_context(client, repo_name, pr_body=pr_body, pr_data=pr_data)

    # Extract changed test files from diff or all_changed_files
    changed_tests = [f for f in all_changed_files if is_test_file(f)] if all_changed_files else extract_changed_test_files(pr_diff)

    return AdversarialValidationContext(
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        pr_diff=pr_diff,
        all_changed_files=all_changed_files,
        changed_tests=changed_tests,
        issue_context=issue_context,
        is_diff_truncated=is_diff_truncated,
        unverified_files=unverified_files,
    )


def _extract_finding_from_dict(f: Dict[str, Any]) -> Optional[AdversarialValidationFinding]:
    """Helper to parse an AdversarialValidationFinding from a dictionary."""
    req = str(f.get("violated_requirement", "")).strip()
    ce = str(f.get("counterexample", "")).strip()
    gap = str(f.get("test_gap", "")).strip()
    scen = str(f.get("suggested_regression_scenario", "")).strip()

    if req or ce or gap or scen:
        return AdversarialValidationFinding(
            violated_requirement=req or "Specification requirement violated",
            counterexample=ce or "Specification violation identified",
            test_gap=gap or "Existing tests do not assert this condition",
            suggested_regression_scenario=scen or "Add regression test covering counterexample",
        )
    return None


def _bounded_response_preview(response: str) -> str:
    """Return a redacted, bounded response preview suitable for normal logs."""
    redacted = redact_string(response)
    if len(redacted) <= ADVERSARIAL_RESPONSE_PREVIEW_LIMIT:
        return redacted
    omitted = len(redacted) - ADVERSARIAL_RESPONSE_PREVIEW_LIMIT
    return f"{redacted[:ADVERSARIAL_RESPONSE_PREVIEW_LIMIT]}... [{omitted} characters omitted]"


def _extract_codex_jsonl_message(response: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Extract the final assistant message from a Codex ``--json`` event stream.

    Returns ``(detected, message, error)``. Once a Codex JSONL envelope is
    detected, malformed lines and failed events are reported instead of being
    ignored so stderr contamination cannot turn an invalid response into PASS.
    """
    lines = [line for line in response.splitlines() if line.strip()]
    if not lines:
        return False, None, None

    try:
        first_event = json.loads(lines[0])
    except json.JSONDecodeError:
        return False, None, None

    if not isinstance(first_event, dict):
        return False, None, None
    first_type = first_event.get("type")
    if not isinstance(first_type, str) or not first_type.startswith(("thread.", "turn.", "item.")):
        return False, None, None

    messages: List[str] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            reason = f"non-JSON content in Codex event stream at line {line_number}: {exc.msg}"
            return True, None, reason
        if not isinstance(event, dict):
            return True, None, f"Codex event at line {line_number} is not an object"

        event_type = event.get("type")
        if event_type in ("turn.failed", "error"):
            return True, None, f"Codex emitted failure event {event_type} at line {line_number}"

        item = event.get("item")
        if event_type == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text)

    if not messages:
        return True, None, "Codex event stream contained no completed agent message"
    return True, messages[-1], None


def _parse_error(response: str, category: str, summary: str, reason: str) -> AdversarialValidationResult:
    """Create and diagnose a fail-closed adversarial parse result."""
    response_length = len(response)
    state = "empty" if not response.strip() else "non-empty"
    preview = _bounded_response_preview(response) if response else "<empty>"
    logger.warning(
        "Adversarial validator response failed parsing: category={}, reason={}, " "response_state={}, response_length={}, preview={!r}; failing closed to ERROR",
        category,
        reason,
        state,
        response_length,
        preview,
    )
    return AdversarialValidationResult(
        result="ERROR",
        summary=summary,
        raw_response=response,
        diagnostic_category=category,
        diagnostic_reason=reason,
    )


def _log_contextual_parse_diagnostics(
    result: AdversarialValidationResult,
    response: str,
    backend_manager: BackendManager,
    pr_number: int,
    attempt: str,
) -> None:
    """Log a bounded parse diagnostic correlated to a validation attempt."""
    if result.result != "ERROR" or not result.diagnostic_category:
        return

    backend: Optional[str] = None
    model: Optional[str] = None
    interaction_log: Optional[str] = None
    try:
        backend_value, model_value = backend_manager.get_last_backend_and_model()
        backend = backend_value if isinstance(backend_value, str) else None
        model = model_value if isinstance(model_value, str) else None
    except Exception:
        pass
    try:
        log_path = backend_manager.get_last_interaction_log_path()
        interaction_log = log_path if isinstance(log_path, str) else None
    except Exception:
        pass

    response_state = "empty" if not response.strip() else "non-empty"
    preview = _bounded_response_preview(response) if response else "<empty>"
    details = {
        "result": result.result,
        "attempt": attempt,
        "backend": backend or "unknown",
        "model": model or "unknown",
        "response_state": response_state,
        "response_length": len(response),
        "response_preview": preview,
        "diagnostic_category": result.diagnostic_category,
        "diagnostic_reason": result.diagnostic_reason or result.summary,
        "interaction_log": interaction_log or "unavailable",
    }
    logger.warning(
        "Adversarial validation parse failure for PR #{} (attempt={}, backend={}, model={}): " "category={}, reason={}, response_state={}, response_length={}, preview={!r}, " "full_response_log={}",
        pr_number,
        attempt,
        details["backend"],
        details["model"],
        details["diagnostic_category"],
        details["diagnostic_reason"],
        response_state,
        len(response),
        preview,
        details["interaction_log"],
    )
    get_trace_logger().log(
        "Adversarial Validation Parse Failure",
        f"Validator response parsing failed for PR #{pr_number} ({attempt})",
        item_type="pr",
        item_number=pr_number,
        details=details,
    )


def parse_adversarial_validation_response(response: str) -> AdversarialValidationResult:
    """Parse the strong model's adversarial validation output.

    Fail-closed policy:
    - Entire response schema must be strictly valid and consistent.
    - PASS requires raw_result == "PASS" and findings to be strictly empty ([] or None).
    - Valid concrete findings override PASS/INCONCLUSIVE/BLOCKED labels and normalize to NEEDS_FIX.
    - Malformed or empty finding elements fail closed to ERROR.
    - NEEDS_FIX requires at least one finding with a concrete counterexample; missing counterexamples are never synthesized.
    - Malformed or unparseable schemas fail closed to ERROR.

    Args:
        response: Raw LLM response string

    Returns:
        Parsed AdversarialValidationResult
    """
    if not response or not response.strip():
        return _parse_error(
            response or "",
            "empty_response",
            "Empty response received from validator; validation incomplete",
            "validator returned no non-whitespace content",
        )

    raw_response = response
    jsonl_detected, extracted_message, jsonl_error = _extract_codex_jsonl_message(raw_response)
    if jsonl_detected and jsonl_error:
        return _parse_error(
            raw_response,
            "cli_event_stream_error",
            f"Invalid Codex validator event stream: {jsonl_error}",
            jsonl_error,
        )
    effective_response = extracted_message if jsonl_detected and extracted_message is not None else raw_response
    cleaned_response = effective_response.strip()

    # 1. Try parsing JSON block from markdown codeblock or direct JSON
    json_str = ""
    json_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_response, re.DOTALL)
    if json_block_match:
        json_str = json_block_match.group(1)
    else:
        bare_match = re.search(r"(\{[\s\S]*\})", cleaned_response)
        if bare_match:
            json_str = bare_match.group(1)

    json_failure_reason: Optional[str] = None
    if json_str:
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                raw_result = str(parsed.get("result", "")).strip().upper()
                summary = str(parsed.get("summary", "")).strip()
                dynamic_check = parsed.get("dynamic_check_requested")
                if dynamic_check:
                    dynamic_check = str(dynamic_check).strip()

                raw_findings = parsed.get("findings")
                findings: List[AdversarialValidationFinding] = []

                if raw_findings is not None:
                    if not isinstance(raw_findings, list):
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Malformed validator schema: findings must be a list",
                            f"findings must be a list, got {type(raw_findings).__name__}",
                        )

                    for item in raw_findings:
                        if not isinstance(item, dict):
                            # Non-dict item in findings list is malformed schema
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Malformed finding entry in validator response",
                                f"finding entry must be an object, got {type(item).__name__}",
                            )

                        if not item:
                            # Empty dict in findings is malformed
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Malformed empty finding entry in validator response",
                                "finding entry must not be empty",
                            )

                        ce = str(item.get("counterexample", "")).strip()
                        if not ce:
                            # Counterexample is mandatory for a valid finding. Do NOT synthesize fake counterexamples.
                            return _parse_error(
                                raw_response,
                                "schema_error",
                                "Reviewer reported a defect without providing the required concrete counterexample",
                                "finding entry is missing a non-empty counterexample",
                            )

                        req = str(item.get("violated_requirement", "")).strip() or "Specification requirement violated"
                        gap = str(item.get("test_gap", "")).strip() or "Existing tests do not assert this condition"
                        scen = str(item.get("suggested_regression_scenario", "")).strip() or "Add regression test covering counterexample"

                        findings.append(
                            AdversarialValidationFinding(
                                violated_requirement=req,
                                counterexample=ce,
                                test_gap=gap,
                                suggested_regression_scenario=scen,
                            )
                        )

                # Determine result - enforce consistency and fail-closed
                if raw_result == "PASS":
                    # Valid concrete findings outrank the contradictory top-level
                    # label. Malformed findings have already failed schema parsing.
                    result_val = "NEEDS_FIX" if findings else "PASS"
                elif raw_result == "NEEDS_FIX":
                    if not findings:
                        # NEEDS_FIX claimed but no valid counterexample findings provided -> fail closed to ERROR
                        return _parse_error(
                            raw_response,
                            "schema_error",
                            "Reviewer claimed NEEDS_FIX without supplying concrete behavioral counterexamples",
                            "NEEDS_FIX requires at least one valid finding",
                        )
                    result_val = "NEEDS_FIX"
                elif raw_result in ("INCONCLUSIVE", "BLOCKED"):
                    # A proven counterexample outranks uncertainty or an
                    # infrastructure diagnostic. Never discard concrete findings.
                    result_val = "NEEDS_FIX" if findings else raw_result
                else:
                    return _parse_error(
                        raw_response,
                        "schema_error",
                        f"Unrecognized validator result: {raw_result or '<missing>'}",
                        "result must be PASS, NEEDS_FIX, INCONCLUSIVE, or BLOCKED",
                    )

                return AdversarialValidationResult(
                    result=result_val,
                    summary=summary or ("Validation passed" if result_val == "PASS" else f"Validation status: {result_val}"),
                    findings=findings,
                    raw_response=raw_response,
                    dynamic_check_requested=dynamic_check,
                )
            else:
                return _parse_error(
                    raw_response,
                    "schema_error",
                    "Malformed validator schema: JSON root must be an object",
                    f"JSON root must be an object, got {type(parsed).__name__}",
                )
        except json.JSONDecodeError as exc:
            json_failure_reason = f"{exc.msg} at line {exc.lineno}, column {exc.colno}"

    # 2. Fallback: Parse structured text markers
    violated_req = ""
    counterexample = ""
    test_gap = ""
    suggested_reg = ""

    for line in cleaned_response.splitlines():
        line_str = line.strip()
        if line_str.upper().startswith("VIOLATED_REQUIREMENT:") or line_str.upper().startswith("VIOLATED REQUIREMENT:"):
            violated_req = line.split(":", 1)[1].strip()
        elif line_str.upper().startswith("COUNTEREXAMPLE:"):
            counterexample = line.split(":", 1)[1].strip()
        elif line_str.upper().startswith("TEST_GAP:") or line_str.upper().startswith("WHY_TESTS_PASS:"):
            test_gap = line.split(":", 1)[1].strip()
        elif line_str.upper().startswith("SUGGESTED_REGRESSION_SCENARIO:") or line_str.upper().startswith("SUGGESTED_REGRESSION:"):
            suggested_reg = line.split(":", 1)[1].strip()

    has_text_findings = bool(violated_req or counterexample or test_gap or suggested_reg)

    if re.search(r"^\s*RESULT\s*:\s*NEEDS_FIX", cleaned_response, re.MULTILINE | re.IGNORECASE):
        if not counterexample:
            return _parse_error(
                raw_response,
                "schema_error",
                "Reviewer claimed NEEDS_FIX without supplying concrete behavioral counterexample",
                "text NEEDS_FIX response is missing COUNTEREXAMPLE",
            )
        findings_list = [
            AdversarialValidationFinding(
                violated_requirement=violated_req or "Specification requirement violated",
                counterexample=counterexample,
                test_gap=test_gap or "Existing tests do not assert this condition",
                suggested_regression_scenario=suggested_reg or "Add regression test covering counterexample",
            )
        ]
        return AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Specification violations found",
            findings=findings_list,
            raw_response=raw_response,
        )

    if re.search(r"^\s*RESULT\s*:\s*PASS", cleaned_response, re.MULTILINE | re.IGNORECASE):
        if has_text_findings:
            if not counterexample:
                return _parse_error(
                    raw_response,
                    "schema_error",
                    "Reviewer reported a defect without a concrete behavioral counterexample",
                    "PASS response contains defect markers but no COUNTEREXAMPLE",
                )
            return AdversarialValidationResult(
                result="NEEDS_FIX",
                summary="Concrete specification violation overrides contradictory PASS label",
                findings=[
                    AdversarialValidationFinding(
                        violated_requirement=violated_req or "Specification requirement violated",
                        counterexample=counterexample,
                        test_gap=test_gap or "Existing tests do not assert this condition",
                        suggested_regression_scenario=suggested_reg or "Add regression test covering counterexample",
                    )
                ],
                raw_response=raw_response,
            )
        return AdversarialValidationResult(
            result="PASS",
            summary="Validation passed",
            findings=[],
            raw_response=raw_response,
        )

    if re.search(r"^\s*RESULT\s*:\s*INCONCLUSIVE", cleaned_response, re.MULTILINE | re.IGNORECASE):
        if counterexample:
            return AdversarialValidationResult(
                result="NEEDS_FIX",
                summary="Specification violation proven despite incomplete review coverage",
                findings=[
                    AdversarialValidationFinding(
                        violated_requirement=violated_req or "Specification requirement violated",
                        counterexample=counterexample,
                        test_gap=test_gap or "Existing tests do not assert this condition",
                        suggested_regression_scenario=suggested_reg or "Add regression test covering counterexample",
                    )
                ],
                raw_response=raw_response,
            )
        return AdversarialValidationResult(
            result="INCONCLUSIVE",
            summary="Validation inconclusive",
            findings=[],
            raw_response=raw_response,
        )

    if json_failure_reason:
        return _parse_error(
            raw_response,
            "json_parse_error",
            f"Invalid JSON in validator response: {json_failure_reason}",
            json_failure_reason,
        )

    return _parse_error(
        raw_response,
        "unrecognized_format",
        "Unparseable validator response; validation incomplete",
        "response contained neither a JSON object nor a recognized RESULT marker",
    )


def _apply_coverage_and_verdict_precedence(
    result: AdversarialValidationResult,
    context: AdversarialValidationContext,
) -> AdversarialValidationResult:
    """Apply deterministic finding-first and complete-coverage verdict rules."""
    if result.findings:
        result.result = "NEEDS_FIX"
        if context.unverified_files:
            coverage_note = f"Review coverage also remains incomplete for: {', '.join(context.unverified_files)}"
            result.summary = f"{result.summary.rstrip()} {coverage_note}".strip()
            result.diagnostic_category = result.diagnostic_category or "incomplete_evidence_coverage"
            result.diagnostic_reason = result.diagnostic_reason or coverage_note
        return result

    if result.result.strip().upper() == "PASS" and not context.has_complete_file_coverage:
        reason = f"Material changed-file evidence was incomplete for: {', '.join(context.unverified_files)}"
        result.result = "INCONCLUSIVE"
        result.summary = f"PASS rejected because review coverage was incomplete. {reason}"
        result.diagnostic_category = "incomplete_evidence_coverage"
        result.diagnostic_reason = reason
    return result


def run_adversarial_validation(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
    backend_manager: Optional[BackendManager] = None,
) -> AdversarialValidationResult:
    """Run strong-model adversarial validation on a green PR.

    Treats the issue specification & acceptance criteria as the test oracle,
    inspects the PR diff and changed tests, and attempts to falsify the implementation.

    Fail-closed protections:
    - If issue context cannot be retrieved (missing oracle), blocks merge (BLOCKED).
    - If no strong backend is available, blocks merge (BLOCKED).
    - If dynamic check fails or cannot complete, blocks merge (NEEDS_FIX / BLOCKED).
    - If dynamic check passes, re-queries reviewer with test execution result for final decision.

    Args:
        repo_name: Repository name ('owner/repo')
        pr_data: Pull request metadata dictionary
        config: AutomationConfig instance
        github_client: Optional GitHubClient instance
        backend_manager: Optional BackendManager (defaults to strong model manager)

    Returns:
        AdversarialValidationResult indicating validation outcome
    """
    pr_number = pr_data.get("number", 0)
    logger.info(f"Starting strong-model adversarial validation for PR #{pr_number}")
    get_trace_logger().log("Adversarial Validation", f"Validating PR #{pr_number} against specification", item_type="pr", item_number=pr_number)

    # 1. Build validation context
    context = build_adversarial_validation_context(repo_name, pr_data, config, github_client)

    # Oracle acquisition check: If no specification context exists, fail closed
    if not context.issue_context or not context.issue_context.strip():
        logger.warning(f"PR #{pr_number} has no linked issue specification / oracle. Blocking merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=f"Oracle acquisition failed for PR #{pr_number}: no linked issue specification found to falsify against",
        )

    # Diff accessibility check: If PR diff could not be retrieved, fail closed
    if not context.pr_diff or not context.pr_diff.strip():
        logger.warning(f"PR #{pr_number} has no accessible diff. Blocking merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary=f"Diff retrieval failed for PR #{pr_number}: no PR diff available for adversarial validation",
        )

    # 2. Select strong backend manager
    if backend_manager is None:
        from .cli_helpers import create_adversarial_validation_backend_manager

        backend_manager = create_adversarial_validation_backend_manager()

    if backend_manager is None:
        logger.error("No strong adversarial validation backend configured or available. Blocking merge.")
        return AdversarialValidationResult(
            result="BLOCKED",
            summary="No strong adversarial validation backend configured or available",
        )

    # 3. Render adversarial validation prompt with complete manifests and
    # deterministic file-evidence coverage metadata.
    changed_tests_str = "\n".join(f"- {t}" for t in context.changed_tests) if context.changed_tests else "(No test files detected in diff)"
    changed_files_str = "\n".join(f"- {path}" for path in context.all_changed_files) if context.all_changed_files else "(No changed files detected)"
    coverage_status = "COMPLETE: every changed file has complete patch evidence." if context.has_complete_file_coverage else "INCOMPLETE: PASS is forbidden because these files have partial/unavailable evidence: " + ", ".join(context.unverified_files)
    prompt = render_prompt(
        "pr.adversarial_validation",
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=context.pr_title,
        pr_body=context.pr_body[: config.MAX_PROMPT_SIZE * 2],
        pr_diff=context.pr_diff,
        linked_issues_context=context.issue_context,
        changed_tests=changed_tests_str,
        changed_files=changed_files_str,
        coverage_status=coverage_status,
    )

    # 4. Invoke the strong model
    with ProgressStage("Adversarial validation"):
        response = run_llm_prompt(prompt, backend_manager=backend_manager, is_noedit=True)

    # 5. Parse response
    result = parse_adversarial_validation_response(response)
    _log_contextual_parse_diagnostics(result, response, backend_manager, pr_number, "initial")
    result = _apply_coverage_and_verdict_precedence(result, context)

    # 6. Dynamic validation on suspicion (if requested)
    if result.dynamic_check_requested and result.dynamic_check_requested.strip() and not result.needs_fix:
        check_target = result.dynamic_check_requested.strip()
        logger.info(f"Adversarial reviewer requested dynamic validation check: {check_target}")
        try:
            from .fix_to_pass_tests_runner import run_local_tests
            from .test_result import TestResult

            test_res = run_local_tests(config, test_file=check_target if check_target != "all" else None)

            if isinstance(test_res, TestResult):
                test_success = test_res.success
                test_output = (test_res.output + "\n" + test_res.errors).strip()
            elif isinstance(test_res, dict):
                test_success = bool(test_res.get("success", False))
                out = str(test_res.get("output") or test_res.get("stdout") or "")
                err = str(test_res.get("errors") or test_res.get("stderr") or "")
                test_output = (out + "\n" + err).strip()
            else:
                test_success = False
                test_output = str(test_res)

            # Preserve original counterexamples and findings in the follow-up
            original_findings_blocks = []
            for idx, f in enumerate(result.findings, start=1):
                original_findings_blocks.append(f"Finding {idx}:\n" f"- Violated Requirement: {f.violated_requirement}\n" f"- Suspected Counterexample: {f.counterexample}\n" f"- Test Gap: {f.test_gap}\n" f"- Suggested Regression Scenario: {f.suggested_regression_scenario}\n")
            original_findings_str = "\n".join(original_findings_blocks) if original_findings_blocks else "(No initial findings recorded)"

            logger.info(f"Dynamic check executed on {check_target} (success={test_success}); querying reviewer with raw test output for final decision")
            followup_prompt = render_prompt(
                "pr.adversarial_validation_followup",
                repo_name=repo_name,
                pr_number=pr_number,
                pr_title=context.pr_title,
                check_target=check_target,
                test_status="PASSED" if test_success else "FAILED",
                test_success="True" if test_success else "False",
                test_output=test_output[: config.MAX_PROMPT_SIZE * 2],
                original_summary=result.summary,
                original_findings=original_findings_str,
                linked_issues_context=context.issue_context,
                pr_diff=context.pr_diff,
            )
            with ProgressStage("Adversarial dynamic check follow-up"):
                followup_response = run_llm_prompt(followup_prompt, backend_manager=backend_manager, is_noedit=True)
            result = parse_adversarial_validation_response(followup_response)
            _log_contextual_parse_diagnostics(result, followup_response, backend_manager, pr_number, "dynamic_check_followup")
            result = _apply_coverage_and_verdict_precedence(result, context)

        except Exception as e:
            logger.warning(f"Failed to execute dynamic validation check '{check_target}': {e}")
            # Inability to complete a requested check must be treated as non-pass (fail-closed)
            result.result = "BLOCKED"
            result.summary = f"Dynamic validation check '{check_target}' could not be completed: {e}"

    result = _apply_coverage_and_verdict_precedence(result, context)

    get_trace_logger().log(
        "Adversarial Validation Result",
        f"Validation for PR #{pr_number}: {result.result}",
        item_type="pr",
        item_number=pr_number,
        details={"result": result.result, "findings_count": len(result.findings), "summary": result.summary},
    )

    return result
