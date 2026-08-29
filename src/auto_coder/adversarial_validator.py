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
from .backend_manager import BackendManager, get_llm_backend_manager, run_llm_prompt
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
    """Outcome of adversarial PR validation against the specification oracle."""

    result: str = "PASS"  # "PASS" or "NEEDS_FIX"
    summary: str = ""
    findings: List[AdversarialValidationFinding] = field(default_factory=list)
    raw_response: str = ""
    dynamic_check_requested: Optional[str] = None

    @property
    def is_pass(self) -> bool:
        """Return True if validation passed without blocking findings."""
        return self.result.strip().upper() == "PASS"

    @property
    def needs_fix(self) -> bool:
        """Return True if validation identified issues requiring a fix."""
        return self.result.strip().upper() == "NEEDS_FIX"


@dataclass
class AdversarialValidationContext:
    """Context gathered for adversarial PR validation."""

    repo_name: str = ""
    pr_number: int = 0
    pr_title: str = ""
    pr_body: str = ""
    pr_diff: str = ""
    changed_tests: List[str] = field(default_factory=list)
    issue_context: str = ""


def extract_changed_test_files(pr_diff: str) -> List[str]:
    """Extract test file paths modified or added in the PR diff.

    Args:
        pr_diff: Unified diff string

    Returns:
        List of changed test file paths
    """
    if not pr_diff:
        return []

    test_files: List[str] = []
    # Match +++ b/path/to/file or diff --git a/... b/...
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
                # Identify test files by convention
                is_test = "test" in path.lower() or path.startswith("tests/") or path.endswith("_test.py") or path.endswith("test.py") or "/tests/" in path or ".spec." in path or ".test." in path
                if is_test and path not in test_files:
                    test_files.append(path)

    return test_files


def build_adversarial_validation_context(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> AdversarialValidationContext:
    """Compile issue specification, PR diff, and changed tests for validation.

    Args:
        repo_name: Repository name in 'owner/repo' format
        pr_data: Pull request metadata dictionary
        config: AutomationConfig instance
        github_client: Optional GitHubClient instance

    Returns:
        AdversarialValidationContext populated with necessary review data
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
    if client:
        try:
            pr_diff = client.get_pr_diff(repo_name, pr_number)[: config.MAX_PR_DIFF_SIZE * 5]
        except Exception as e:
            logger.warning(f"Could not retrieve diff for PR #{pr_number}: {e}")

    # Extract linked issues context (contains requirements & acceptance criteria)
    issue_context = get_linked_issues_context(client, repo_name, pr_body)

    # Extract changed test files from diff
    changed_tests = extract_changed_test_files(pr_diff)

    return AdversarialValidationContext(
        repo_name=repo_name,
        pr_number=pr_number,
        pr_title=pr_title,
        pr_body=pr_body,
        pr_diff=pr_diff,
        changed_tests=changed_tests,
        issue_context=issue_context,
    )


def parse_adversarial_validation_response(response: str) -> AdversarialValidationResult:
    """Parse the strong model's adversarial validation output.

    Handles structured JSON responses as well as marked text fallbacks.

    Args:
        response: Raw LLM response string

    Returns:
        Parsed AdversarialValidationResult
    """
    if not response or not response.strip():
        logger.warning("Empty response received from adversarial validator; defaulting to PASS")
        return AdversarialValidationResult(result="PASS", summary="Empty response received", raw_response=response)

    cleaned_response = response.strip()

    # 1. Try parsing JSON block from markdown codeblock or direct JSON
    json_str = ""
    json_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned_response, re.DOTALL)
    if json_block_match:
        json_str = json_block_match.group(1)
    else:
        # Check for bare JSON object
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

                # Determine result
                if raw_result in ("PASS", "NEEDS_FIX"):
                    result_val = raw_result
                elif findings:
                    result_val = "NEEDS_FIX"
                else:
                    result_val = "PASS"

                return AdversarialValidationResult(
                    result=result_val,
                    summary=summary or ("Validation passed" if result_val == "PASS" else "Specification violations found"),
                    findings=findings,
                    raw_response=response,
                    dynamic_check_requested=dynamic_check,
                )
        except Exception as exc:
            logger.debug(f"JSON parsing failed for adversarial response: {exc}")

    # 2. Fallback: Parse structured text markers
    result_val = "PASS"
    summary = ""
    findings = []
    dynamic_check = None

    if re.search(r"^\s*RESULT\s*:\s*NEEDS_FIX", cleaned_response, re.MULTILINE | re.IGNORECASE):
        # Extract sections
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

        if counterexample or violated_req:
            result_val = "NEEDS_FIX"
            findings.append(
                AdversarialValidationFinding(
                    violated_requirement=violated_req or "Specification requirement violated",
                    counterexample=counterexample or "Specification violation identified",
                    test_gap=test_gap or "Existing tests do not assert this condition",
                    suggested_regression_scenario=suggested_reg or "Add regression test covering counterexample",
                )
            )

    return AdversarialValidationResult(
        result=result_val,
        summary=summary or ("Validation passed" if result_val == "PASS" else "Specification violations found"),
        findings=findings,
        raw_response=response,
        dynamic_check_requested=dynamic_check,
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

    Args:
        repo_name: Repository name ('owner/repo')
        pr_data: Pull request metadata dictionary
        config: AutomationConfig instance
        github_client: Optional GitHubClient instance
        backend_manager: Optional BackendManager (defaults to strong model manager)

    Returns:
        AdversarialValidationResult indicating PASS or NEEDS_FIX
    """
    pr_number = pr_data.get("number", 0)
    logger.info(f"Starting strong-model adversarial validation for PR #{pr_number}")
    get_trace_logger().log("Adversarial Validation", f"Validating PR #{pr_number} against specification", item_type="pr", item_number=pr_number)

    # 1. Build validation context
    context = build_adversarial_validation_context(repo_name, pr_data, config, github_client)

    # If no specification context exists (e.g. no linked issue), we cannot falsify against an oracle
    if not context.issue_context.strip():
        logger.info(f"PR #{pr_number} has no linked issue specification to falsify against. Passing validation.")
        return AdversarialValidationResult(
            result="PASS",
            summary="No linked issue specification available to falsify against",
        )

    # 2. Select strong backend manager
    if backend_manager is None:
        from .cli_helpers import create_adversarial_validation_backend_manager

        backend_manager = create_adversarial_validation_backend_manager()

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
                            counterexample=f"Test failure on dynamic check: {check_target}",
                            test_gap="Confirmed failing scenario during dynamic validation",
                            suggested_regression_scenario=f"Ensure {check_target} passes",
                        )
                    )
        except Exception as e:
            logger.warning(f"Failed to execute dynamic validation check: {e}")

    get_trace_logger().log(
        "Adversarial Validation Result",
        f"Validation for PR #{pr_number}: {result.result}",
        item_type="pr",
        item_number=pr_number,
        details={"result": result.result, "findings_count": len(result.findings), "summary": result.summary},
    )

    return result


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
    1. Add a regression test exposing the violation.
    2. Fix the implementation to satisfy the specification.
    3. Run tests and commit/push changes.

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

        # Check git status for changes, stage, commit, and push
        status_res = cmd.run_command(["git", "status", "--porcelain"])
        if status_res.success and status_res.stdout.strip():
            cmd.run_command(["git", "add", "."])
            commit_msg = f"Auto-Coder: Add regression test and fix adversarial violation (PR #{pr_number})"
            c_res = git_commit_with_retry(commit_msg)
            if c_res.success:
                actions.append("Committed regression test and fix for adversarial violation")
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
