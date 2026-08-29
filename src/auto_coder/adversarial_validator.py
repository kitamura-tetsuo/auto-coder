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
from .trace_logger import get_trace_logger
from .util.gh_cache import GitHubClient
from .utils import CommandExecutor

logger = get_logger(__name__)
cmd = CommandExecutor()


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

    @property
    def is_pass(self) -> bool:
        """Return True if validation explicitly passed with no blocking findings."""
        return self.result.strip().upper() == "PASS" and len(self.findings) == 0

    @property
    def needs_fix(self) -> bool:
        """Return True if validation identified issues requiring a fix."""
        return self.result.strip().upper() == "NEEDS_FIX" or len(self.findings) > 0

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

    Informs the reviewer of complete changed file scope and explicitly notes
    any truncation so partial review is never misrepresented as full coverage.

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
                max_diff_bytes = config.MAX_PR_DIFF_SIZE * 5

                if len(raw_diff) > max_diff_bytes:
                    is_diff_truncated = True
                    file_list_str = ", ".join(all_changed_files)
                    truncation_warning = (
                        f"\n\n[WARNING: PR Diff was truncated at {max_diff_bytes} bytes due to size limit.\n"
                        f"Complete list of changed files in this PR ({len(all_changed_files)} files): {file_list_str}\n"
                        f"IMPORTANT: If requirements or implementation details cannot be verified because critical files were omitted, "
                        f"you MUST output result: 'INCONCLUSIVE' rather than 'PASS'.]"
                    )
                    pr_diff = raw_diff[:max_diff_bytes] + truncation_warning
                else:
                    pr_diff = raw_diff
        except Exception as e:
            logger.warning(f"Could not retrieve diff for PR #{pr_number}: {e}")

    # Extract linked issues context (contains requirements & acceptance criteria)
    issue_context = get_linked_issues_context(client, repo_name, pr_body)

    # Extract changed test files from diff
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


def parse_adversarial_validation_response(response: str) -> AdversarialValidationResult:
    """Parse the strong model's adversarial validation output.

    Fail-closed policy:
    - Empty, missing, unparseable, or malformed responses default to ERROR (non-pass).
    - Contradictory responses (result=PASS with non-empty findings) convert to NEEDS_FIX.
    - PASS is only returned for internally consistent, successfully parsed explicit PASS with 0 findings.

    Args:
        response: Raw LLM response string

    Returns:
        Parsed AdversarialValidationResult
    """
    if not response or not response.strip():
        logger.warning("Empty response received from adversarial validator; failing closed to ERROR")
        return AdversarialValidationResult(
            result="ERROR",
            summary="Empty response received from validator; validation incomplete",
            raw_response=response or "",
        )

    cleaned_response = response.strip()

    # 1. Try parsing JSON block from markdown codeblock or direct JSON
    json_str = ""
    json_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_response, re.DOTALL)
    if json_block_match:
        json_str = json_block_match.group(1)
    else:
        bare_match = re.search(r"(\{[\s\S]*\})", cleaned_response)
        if bare_match:
            json_str = bare_match.group(1)

    if json_str:
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                raw_result = str(parsed.get("result", "")).strip().upper()
                summary = str(parsed.get("summary", "")).strip()
                dynamic_check = parsed.get("dynamic_check_requested")
                if dynamic_check:
                    dynamic_check = str(dynamic_check).strip()

                raw_findings = parsed.get("findings", [])
                findings: List[AdversarialValidationFinding] = []
                if isinstance(raw_findings, list):
                    for f in raw_findings:
                        if isinstance(f, dict):
                            findings.append(
                                AdversarialValidationFinding(
                                    violated_requirement=str(f.get("violated_requirement", "")).strip(),
                                    counterexample=str(f.get("counterexample", "")).strip(),
                                    test_gap=str(f.get("test_gap", "")).strip(),
                                    suggested_regression_scenario=str(f.get("suggested_regression_scenario", "")).strip(),
                                )
                            )

                # Determine result - enforce consistency and fail-closed
                if findings:
                    # Non-empty findings always require a fix, even if raw_result claimed PASS
                    result_val = "NEEDS_FIX"
                elif raw_result == "PASS":
                    result_val = "PASS"
                elif raw_result in ("NEEDS_FIX", "INCONCLUSIVE", "BLOCKED"):
                    result_val = raw_result
                else:
                    # Unrecognized result tag without findings -> fail-closed to ERROR
                    result_val = "ERROR"

                return AdversarialValidationResult(
                    result=result_val,
                    summary=summary or ("Validation passed" if result_val == "PASS" else f"Validation status: {result_val}"),
                    findings=findings,
                    raw_response=response,
                    dynamic_check_requested=dynamic_check,
                )
        except Exception as exc:
            logger.debug(f"JSON parsing failed for adversarial response: {exc}")

    # 2. Fallback: Parse structured text markers
    if re.search(r"^\s*RESULT\s*:\s*NEEDS_FIX", cleaned_response, re.MULTILINE | re.IGNORECASE):
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

        findings_list = [
            AdversarialValidationFinding(
                violated_requirement=violated_req or "Specification requirement violated",
                counterexample=counterexample or "Specification violation identified",
                test_gap=test_gap or "Existing tests do not assert this condition",
                suggested_regression_scenario=suggested_reg or "Add regression test covering counterexample",
            )
        ]
        return AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Specification violations found",
            findings=findings_list,
            raw_response=response,
        )

    if re.search(r"^\s*RESULT\s*:\s*PASS", cleaned_response, re.MULTILINE | re.IGNORECASE):
        return AdversarialValidationResult(
            result="PASS",
            summary="Validation passed",
            findings=[],
            raw_response=response,
        )

    if re.search(r"^\s*RESULT\s*:\s*INCONCLUSIVE", cleaned_response, re.MULTILINE | re.IGNORECASE):
        return AdversarialValidationResult(
            result="INCONCLUSIVE",
            summary="Validation inconclusive",
            findings=[],
            raw_response=response,
        )

    # If format is unparseable, fail closed to ERROR
    logger.warning("Unparseable response received from adversarial validator; failing closed to ERROR")
    return AdversarialValidationResult(
        result="ERROR",
        summary="Unparseable validator response; validation incomplete",
        raw_response=response,
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

    # 3. Render adversarial validation prompt
    changed_tests_str = "\n".join(f"- {t}" for t in context.changed_tests) if context.changed_tests else "(No test files detected in diff)"
    prompt = render_prompt(
        "pr.adversarial_validation",
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=context.pr_title,
        pr_body=context.pr_body[: config.MAX_PROMPT_SIZE * 2],
        pr_diff=context.pr_diff[: config.MAX_PR_DIFF_SIZE * 3],
        linked_issues_context=context.issue_context,
        changed_tests=changed_tests_str,
    )

    # 4. Invoke the strong model
    with ProgressStage("Adversarial validation"):
        response = run_llm_prompt(prompt, backend_manager=backend_manager)

    # 5. Parse response
    result = parse_adversarial_validation_response(response)

    # 6. Dynamic validation on suspicion (if requested)
    if result.dynamic_check_requested and result.dynamic_check_requested.strip():
        check_target = result.dynamic_check_requested.strip()
        logger.info(f"Adversarial reviewer requested dynamic validation check: {check_target}")
        try:
            from .fix_to_pass_tests_runner import run_local_tests

            test_res = run_local_tests(config, test_file=check_target if check_target != "all" else None)
            if not test_res.get("success"):
                logger.info(f"Dynamic check confirmed failure on {check_target}")
                result.result = "NEEDS_FIX"
                if not result.findings:
                    result.findings.append(
                        AdversarialValidationFinding(
                            violated_requirement="Dynamic check failed",
                            counterexample=f"Dynamic test execution failed on: {check_target}",
                            test_gap="Confirmed failing scenario during dynamic validation",
                            suggested_regression_scenario=f"Ensure {check_target} passes",
                        )
                    )
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
    3. Verify regression test creation before pushing.

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
        linked_issues_context = get_linked_issues_context(client, repo_name, pr_data.get("body", ""))

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

        # If code was changed but no regression test was created, prompt specifically for the missing test
        if changed_files and not test_files_changed:
            logger.warning(f"Adversarial fix modified files ({changed_files}) but no regression test file was added/modified. Prompting for regression test...")
            retry_test_prompt = (
                f"You modified implementation files ({', '.join(changed_files)}), but did NOT create or update any test files.\n"
                f"You MUST add a focused regression test under tests/ (e.g. tests/test_*.py) reproducing the following counterexample:\n"
                f"{findings_summary}\n\n"
                f"Create the test file now and ensure it passes."
            )
            run_llm_prompt(retry_test_prompt, backend_manager=backend_manager)
            changed_files = _get_modified_files_from_status()
            test_files_changed = [f for f in changed_files if is_test_file(f)]

        if changed_files:
            cmd.run_command(["git", "add", "."])
            commit_msg = f"Auto-Coder: Add regression test and fix adversarial violation (PR #{pr_number})"
            c_res = git_commit_with_retry(commit_msg)
            if c_res.success:
                if test_files_changed:
                    actions.append(f"Committed regression test ({', '.join(test_files_changed)}) and fix for adversarial violation")
                else:
                    actions.append("Committed adversarial fix (warning: no separate test file detected)")

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
