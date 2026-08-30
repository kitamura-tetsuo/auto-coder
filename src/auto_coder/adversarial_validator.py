"""Adversarial validation functionality for Auto-Coder automation engine.

Validates PR implementation against the original issue specification / acceptance criteria
using a strong model to catch false-success PRs before merge.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .automation_config import AutomationConfig
from .backend_manager import BackendManager, run_llm_prompt
from .git_info import get_commit_log
from .git_utils import git_commit_with_retry, git_push
from .issue_context import get_linked_issues_context
from .logger_config import get_logger
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .security_utils import redact_string
from .trace_logger import get_trace_logger
from .util.gh_cache import GitHubClient
from .utils import CommandExecutor

logger = get_logger(__name__)
cmd = CommandExecutor()

ADVERSARIAL_RESPONSE_PREVIEW_LIMIT = 2000


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
    - Any findings or 'NEEDS_FIX' evaluates to needs_fix=True.
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


def is_test_file(file_path: str) -> bool:
    """Determine whether a file path corresponds to a test file."""
    path_lower = file_path.lower()
    return path_lower.startswith("tests/") or path_lower.startswith("test/") or "/tests/" in path_lower or "/test/" in path_lower or path_lower.endswith("_test.py") or path_lower.endswith("test.py") or ".spec." in path_lower or ".test." in path_lower


def extract_all_changed_files(pr_diff: str) -> List[str]:
    """Extract all modified or added file paths from a unified diff string.

    Args:
        pr_diff: Unified diff string

    Returns:
        List of all changed file paths
    """
    if not pr_diff:
        return []

    files: List[str] = []
    patterns = [
        r"^\+\+\+\s+b/(.*)$",
        r"^diff\s+--git\s+a/.*?\s+b/(.*)$",
    ]

    for line in pr_diff.splitlines():
        for pattern in patterns:
            match = re.match(pattern, line)
            if match:
                path = match.group(1).strip()
                if path.startswith("dev/null"):
                    continue
                if path not in files:
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


def build_adversarial_validation_context(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> AdversarialValidationContext:
    """Compile issue specification, PR diff, and changed tests for validation.

    Recovers oracle/specification from PR body, title, branch name, and session.
    Guarantees that truncation warning and full changed-file list are never stripped.

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

    if client:
        try:
            raw_diff = client.get_pr_diff(repo_name, pr_number)
            if raw_diff:
                all_changed_files = extract_all_changed_files(raw_diff)
                max_diff_bytes = config.MAX_PR_DIFF_SIZE * 3

                if len(raw_diff) > max_diff_bytes:
                    is_diff_truncated = True
                    file_list_str = ", ".join(all_changed_files)
                    truncation_warning = (
                        f"\n\n[WARNING: PR Diff was truncated to first {max_diff_bytes} bytes due to size limit.\n"
                        f"Complete list of changed files in this PR ({len(all_changed_files)} files): {file_list_str}\n"
                        f"IMPORTANT: If requirements or implementation details cannot be verified because critical files were omitted, "
                        f"you MUST output result: 'INCONCLUSIVE' rather than 'PASS'.]"
                    )
                    pr_diff = raw_diff[:max_diff_bytes] + truncation_warning
                else:
                    pr_diff = raw_diff
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
    - Any non-empty findings on PASS, or malformed/empty elements in findings, fail closed to ERROR.
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
                    if findings or (raw_findings is not None and len(raw_findings) > 0):
                        # Contradictory: PASS claimed but findings present -> fail closed to ERROR
                        return _parse_error(
                            raw_response,
                            "contradictory_response",
                            "Contradictory validator response: PASS claimed with non-empty findings",
                            "PASS requires findings to be empty",
                        )
                    result_val = "PASS"
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
                    result_val = raw_result
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
        # If RESULT: PASS is accompanied by defect markers, fail closed to ERROR
        if has_text_findings:
            return _parse_error(
                raw_response,
                "contradictory_response",
                "Contradictory validator output (PASS with defect markers); failing closed to ERROR",
                "PASS response contains defect markers",
            )
        return AdversarialValidationResult(
            result="PASS",
            summary="Validation passed",
            findings=[],
            raw_response=raw_response,
        )

    if re.search(r"^\s*RESULT\s*:\s*INCONCLUSIVE", cleaned_response, re.MULTILINE | re.IGNORECASE):
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

    # 3. Render adversarial validation prompt (context.pr_diff already includes truncation warning if applicable)
    changed_tests_str = "\n".join(f"- {t}" for t in context.changed_tests) if context.changed_tests else "(No test files detected in diff)"
    prompt = render_prompt(
        "pr.adversarial_validation",
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=context.pr_title,
        pr_body=context.pr_body[: config.MAX_PROMPT_SIZE * 2],
        pr_diff=context.pr_diff,
        linked_issues_context=context.issue_context,
        changed_tests=changed_tests_str,
    )

    # 4. Invoke the strong model
    with ProgressStage("Adversarial validation"):
        response = run_llm_prompt(prompt, backend_manager=backend_manager, is_noedit=True)

    # 5. Parse response
    result = parse_adversarial_validation_response(response)
    _log_contextual_parse_diagnostics(result, response, backend_manager, pr_number, "initial")

    # 6. Dynamic validation on suspicion (if requested)
    if result.dynamic_check_requested and result.dynamic_check_requested.strip():
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

        except Exception as e:
            logger.warning(f"Failed to execute dynamic validation check '{check_target}': {e}")
            # Inability to complete a requested check must be treated as non-pass (fail-closed)
            result.result = "BLOCKED"
            result.summary = f"Dynamic validation check '{check_target}' could not be completed: {e}"

    get_trace_logger().log(
        "Adversarial Validation Result",
        f"Validation for PR #{pr_number}: {result.result}",
        item_type="pr",
        item_number=pr_number,
        details={"result": result.result, "findings_count": len(result.findings), "summary": result.summary},
    )

    return result


def _get_modified_files_from_status() -> List[str]:
    """Get list of modified/added files from git status."""
    status_res = cmd.run_command(["git", "status", "--porcelain"])
    if not status_res.success or not status_res.stdout.strip():
        return []

    changed_files = []
    for line in status_res.stdout.splitlines():
        line_clean = line.strip()
        if len(line_clean) >= 3:
            # File path starts after status code (e.g., ' M file.py' -> 'file.py')
            file_part = line_clean[2:].strip()
            # Handle renames 'old -> new'
            if " -> " in file_part:
                file_part = file_part.split(" -> ")[1].strip()
            if file_part:
                changed_files.append(file_part)

    return changed_files


def apply_adversarial_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    validation_result: AdversarialValidationResult,
    github_client: Optional[Any] = None,
    backend_manager: Optional[BackendManager] = None,
) -> List[str]:
    """Apply corrective fix and regression tests on the same PR branch.

    Instructs the implementation agent to:
    1. Add a regression test exposing the counterexample.
    2. Fix the implementation to satisfy the specification.
    3. Verify regression test creation before pushing, requiring explicit exemption if impractical.

    Args:
        repo_name: Repository name ('owner/repo')
        pr_data: PR data dictionary
        config: AutomationConfig instance
        validation_result: The failing AdversarialValidationResult
        github_client: Optional GitHubClient instance
        backend_manager: Optional BackendManager for implementation

    Returns:
        List of action strings describing results
    """
    actions: List[str] = []
    pr_number = pr_data.get("number", 0)

    try:
        # Build findings summary for prompt
        findings_text_blocks = []
        for idx, finding in enumerate(validation_result.findings, start=1):
            block = f"### Finding {idx}:\n" f"- **Violated Requirement**: {finding.violated_requirement}\n" f"- **Counterexample**: {finding.counterexample}\n" f"- **Test Gap**: {finding.test_gap}\n" f"- **Suggested Regression Scenario**: {finding.suggested_regression_scenario}\n"
            findings_text_blocks.append(block)

        findings_summary = "\n".join(findings_text_blocks) if findings_text_blocks else validation_result.summary

        # Get commit log since branch creation
        commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

        # Extract linked issues context
        client = github_client
        if client is None:
            try:
                client = GitHubClient.get_instance()
            except Exception:
                client = None
        linked_issues_context = get_linked_issues_context(client, repo_name, pr_body=pr_data.get("body", ""), pr_data=pr_data)

        # Render adversarial fix prompt
        fix_prompt = render_prompt(
            "pr.adversarial_fix",
            pr_number=pr_number,
            repo_name=repo_name,
            pr_title=pr_data.get("title", "Unknown"),
            adversarial_findings=findings_summary,
            commit_log=commit_log or "(No commit history)",
            linked_issues_context=linked_issues_context,
        )

        with ProgressStage(f"Fixing adversarial violation (PR #{pr_number})"):
            response = run_llm_prompt(fix_prompt, backend_manager=backend_manager)

        if response:
            preview = response.strip()[: config.MAX_RESPONSE_SIZE] if response.strip() else "No response"
            actions.append(f"Applied adversarial regression fix: {preview}...")
        else:
            actions.append("No response from LLM for adversarial fix")

        # Inspect changed files
        changed_files = _get_modified_files_from_status()
        test_files_changed = [f for f in changed_files if is_test_file(f)]

        # If code was changed but no regression test was created, prompt specifically for the missing test or justification
        if changed_files and not test_files_changed:
            logger.warning(f"Adversarial fix modified files ({changed_files}) but no regression test file was added/modified. Prompting for regression test or explicit exemption...")
            retry_test_prompt = (
                f"You modified implementation files ({', '.join(changed_files)}), but did NOT create or update any test files under tests/.\n"
                f"You MUST either:\n"
                f"1. Add a focused regression test file under tests/ (e.g. tests/test_*.py) reproducing this counterexample:\n"
                f"{findings_summary}\n\n"
                f"2. OR if an automated test is truly impossible/impractical to write, you MUST explicitly output: NO_TEST_REASON: <detailed justification>."
            )
            retry_resp = run_llm_prompt(retry_test_prompt, backend_manager=backend_manager)
            if retry_resp:
                response = f"{response}\n{retry_resp}"
            changed_files = _get_modified_files_from_status()
            test_files_changed = [f for f in changed_files if is_test_file(f)]

        if changed_files:
            # Check if exemption reason was provided if no test was created
            no_test_match = re.search(r"NO_TEST_REASON:\s*(.+)", response or "", re.IGNORECASE)
            no_test_reason = no_test_match.group(1).strip() if no_test_match else ""

            if not test_files_changed and not no_test_reason:
                actions.append("Adversarial fix rejected: implementation was modified but no regression test was created and no NO_TEST_REASON justification was provided.")
                logger.warning("Adversarial fix rejected: code modified without regression test or exemption reason.")
                return actions

            cmd.run_command(["git", "add", "."])
            if test_files_changed:
                commit_msg = f"Auto-Coder: Add regression test and fix adversarial violation (PR #{pr_number})"
            else:
                commit_msg = f"Auto-Coder: Fix adversarial violation (test exemption: {no_test_reason}) (PR #{pr_number})"

            c_res = git_commit_with_retry(commit_msg)
            if c_res.success:
                if test_files_changed:
                    actions.append(f"Committed regression test ({', '.join(test_files_changed)}) and fix for adversarial violation")
                else:
                    actions.append(f"Committed adversarial fix with documented test exemption: {no_test_reason}")

                p_res = git_push()
                if p_res.success:
                    actions.append("Pushed adversarial fixes to GitHub to trigger new CI run")
                else:
                    actions.append(f"Failed to push adversarial fixes: {p_res.stderr}")
            else:
                actions.append(f"Failed to commit adversarial fixes: {c_res.stderr}")
        else:
            actions.append("No changes generated by adversarial fix")

    except Exception as e:
        logger.error(f"Error applying adversarial fix for PR #{pr_number}: {e}")
        actions.append(f"Error applying adversarial fix for PR #{pr_number}: {e}")

    return actions
