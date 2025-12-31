"""Test execution functionality for Auto-Coder automation engine."""

import csv
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .automation_config import AutomationConfig
from .git_utils import (
    extract_number_from_branch,
    get_commit_log,
    get_current_branch,
    get_current_repo_name,
    git_commit_with_retry,
    git_push,
    save_commit_failure_history,
    get_current_commit_sha,
    check_unpushed_commits,
)
import time
from .github_client import GitHubClient
from .llm_backend_config import get_isolate_single_test_on_failure_from_config
from .logger_config import get_logger, log_calls
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .test_log_utils import (
    _collect_playwright_candidates,
    _detect_failed_test_library,
    extract_first_failed_test,
    extract_playwright_passed_count,
)
from .test_result import TestResult
from .update_manager import check_for_updates_and_restart
from .utils import CommandExecutor, change_fraction, log_action
from .util.github_action import _get_github_actions_logs

if TYPE_CHECKING:
    from .backend_manager import BackendManager

logger = get_logger(__name__)
cmd = CommandExecutor()

# Test Watcher MCP integration flag
USE_TEST_WATCHER_MCP = os.environ.get("USE_TEST_WATCHER_MCP", "true").lower() == "true"


@dataclass
class WorkspaceFixResult:
    """Container for LLM workspace-fix responses used during test retries."""

    summary: str
    raw_response: Optional[str]
    backend: str
    provider: str
    model: str


def _to_test_result(data: Any) -> TestResult:
    """Convert legacy dict payloads into a TestResult.

    Accepts either a TestResult (returned as-is) or a Dict[str, Any] produced by
    run_local_tests and related helpers. Provides a compatibility bridge while
    the codebase migrates to structured results.
    """
    if isinstance(data, TestResult):
        return data
    if not isinstance(data, dict):  # Fallback empty container
        return TestResult(success=False, output="", errors="", return_code=-1)

    rc_raw = data.get("return_code", data.get("returncode", -1))
    try:
        rc = int(rc_raw) if rc_raw is not None else -1
    except Exception:
        rc = -1

    extraction_ctx = data.get("extraction_context", {})
    if not isinstance(extraction_ctx, dict):
        extraction_ctx = {}

    return TestResult(
        success=bool(data.get("success", False)),
        output=str(data.get("output", "")),
        errors=str(data.get("errors", "")),
        return_code=rc,
        command=str(data.get("command", "")),
        test_file=data.get("test_file"),
        stability_issue=bool(data.get("stability_issue", False)),
        extraction_context=extraction_ctx,
        framework_type=data.get("framework_type"),
    )


def _normalize_test_file(test_file: Optional[str]) -> str:
    """Return a friendly identifier for the target test file."""

    if test_file:
        return test_file
    return "ALL_TESTS"


def _sanitize_for_filename(value: str, *, default: str) -> str:
    """Sanitize arbitrary text so it is safe for filesystem use."""

    if not value:
        value = default
    cleaned = re.sub(r"[^\w.-]+", "_", value)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = default
    # Keep filenames reasonably short to avoid OS limits
    return cleaned[:80]


def _log_fix_attempt_metadata(test_file: Optional[str], backend: str, provider: str, model: str, timestamp: datetime) -> Path:
    """Append metadata about a fix attempt to the CSV summary log."""

    base_dir = Path(".auto-coder")
    base_dir.mkdir(parents=True, exist_ok=True)
    csv_path = base_dir / "fix_to_pass_tests_summury.csv"
    file_exists = csv_path.exists()
    record = [
        _normalize_test_file(test_file),
        backend or "unknown",
        provider or "default",
        model or "unknown",
        timestamp.isoformat(),
    ]
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(["current_test_file", "backend", "provider", "model", "timestamp"])
        writer.writerow(record)
    return csv_path


def _write_llm_output_log(
    *,
    raw_output: Optional[str],
    test_file: Optional[str],
    backend: str,
    provider: str,
    model: str,
    timestamp: datetime,
) -> Path:
    """Persist the raw LLM output for traceability."""

    log_dir = Path(".auto-coder") / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    filename = "{time}_{test}_{backend}_{provider}_{model}.txt".format(
        time=timestamp.strftime("%Y%m%d_%H%M%S"),
        test=_sanitize_for_filename(_normalize_test_file(test_file), default="tests"),
        backend=_sanitize_for_filename(backend or "unknown", default="backend"),
        provider=_sanitize_for_filename(provider or "default", default="provider"),
        model=_sanitize_for_filename(model or "unknown", default="model"),
    )
    log_path = log_dir / filename
    content = raw_output if raw_output is not None else "LLM produced no response"
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
    return log_path


def _write_test_log_json(
    *,
    repo_name: str,
    test_file: Optional[str],
    attempt: int,
    test_result: Dict[str, Any],
    fix_result: WorkspaceFixResult,
    post_test_result: Optional[Dict[str, Any]],
    timestamp: datetime,
) -> Path:
    """Save test execution details to a JSON log file.
    
    Path format: ~/.auto-coder/{repo_name}/test_log/{timestamp}_{test_file}_attempt_{attempt}.json
    """
    home_dir = Path.home()
    # Handle repo_name being a path or owner/repo string - sanitize just in case
    # If repo_name contains slashes, we might want to respect that hierarchy or just use the last part.
    # The requirement says: /home/node/.auto-coder/kitamura-tetsuo/outliner/test_log
    # So we should use repo_name as is but rely on it not starting with / to avoid absolute path issues?
    # Usually repo_name is "owner/repo".
    
    log_dir = home_dir / ".auto-coder" / repo_name / "test_log"
    log_dir.mkdir(parents=True, exist_ok=True)

    filename = "{time}_{test}_attempt_{attempt}.json".format(
        time=timestamp.strftime("%Y%m%d_%H%M%S"),
        test=_sanitize_for_filename(_normalize_test_file(test_file), default="tests"),
        attempt=attempt,
    )
    log_path = log_dir / filename

    data = {
        "timestamp": timestamp.isoformat(),
        "repo": repo_name,
        "test_file": _normalize_test_file(test_file),
        "attempt": attempt,
        "backend": fix_result.backend,
        "provider": fix_result.provider,
        "model": fix_result.model,
        "pre_fix_test_result": test_result,
        "llm_fix_summary": fix_result.summary,
        "llm_raw_response": fix_result.raw_response,
        "post_fix_test_result": post_test_result,
    }

    try:
        with log_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Failed to write JSON test log to {log_path}: {e}")

    return log_path


@log_calls  # type: ignore[misc]
def _extract_backend_model(llm_client: Any) -> Tuple[str, str, str]:
    """Derive backend/provider/model identifiers from the provided LLM client."""

    if llm_client is None:
        return "unknown", "default", "unknown"

    backend_value: Optional[str] = None
    provider_value: Optional[str] = None
    model_value: Optional[str] = None

    getter = getattr(llm_client, "get_last_backend_provider_and_model", None)
    if callable(getter):
        try:
            details = getter()
            if isinstance(details, tuple):
                if len(details) == 3:
                    backend_value, provider_value, model_value = details
                elif len(details) == 2:
                    backend_value, model_value = details
        except Exception:
            pass

    if backend_value is None or model_value is None:
        getter = getattr(llm_client, "get_last_backend_and_model", None)
        if callable(getter):
            try:
                backend_value, model_value = getter()
            except Exception:
                backend_value = backend_value or None
                model_value = model_value or None

    if backend_value is None:
        backend_value = getattr(llm_client, "backend", None) or getattr(llm_client, "name", None)
    if backend_value is None:
        backend_value = llm_client.__class__.__name__

    if provider_value is None:
        provider_value = getattr(llm_client, "provider_name", None)

    if model_value is None:
        model_value = getattr(llm_client, "model_name", None)

    backend = str(backend_value) if backend_value else "unknown"
    provider = str(provider_value) if provider_value else "default"
    model = str(model_value) if model_value else "unknown"

    return backend, provider, model


def cleanup_llm_task_file(path: str = "./llm_task.md") -> None:
    """Remove the LLM task log file before committing final fixes."""
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug(f"Removed {path} prior to commit")
    except Exception as exc:
        logger.warning(f"Failed to remove {path}: {exc}")


def run_local_tests(config: AutomationConfig, test_file: Optional[str] = None) -> Dict[str, Any]:
    """Run local tests using configured script or pytest fallback.

    If test_file is specified, only that test file will be run.
    Otherwise, all tests will be run.

    Returns a dict: {success, output, errors, return_code, test_file, stability_issue}
    - stability_issue: True if test failed in full suite but passed in isolation
    - full_suite_result: Original full suite result (only present if stability_issue is True)
    """
    # Try to use test_watcher MCP if enabled and available
    if USE_TEST_WATCHER_MCP and not test_file:
        try:
            from .test_watcher_client import TestWatcherClient

            mcp_server_path = os.environ.get("TEST_WATCHER_MCP_SERVER_PATH")
            if mcp_server_path and Path(mcp_server_path).exists():
                logger.info("Querying test results from test_watcher MCP")

                with TestWatcherClient(mcp_server_path=mcp_server_path) as client:
                    # Query all test results
                    results = client.query_test_results(test_type="all")

                    if results.get("status") == "running":
                        logger.info("Tests are currently running in test_watcher, waiting...")
                        # Fall back to normal test execution
                    elif results.get("status") == "completed":
                        # Convert MCP results to expected format
                        summary = results.get("summary", {})
                        failed_tests = results.get("failed_tests", {}).get("tests", [])

                        success = summary.get("failed", 0) == 0

                        # Build output from test results
                        output_lines = [
                            f"Test Results (from test_watcher MCP):",
                            f"Total: {summary.get('total', 0)}",
                            f"Passed: {summary.get('passed', 0)}",
                            f"Failed: {summary.get('failed', 0)}",
                            f"Flaky: {summary.get('flaky', 0)}",
                            f"Skipped: {summary.get('skipped', 0)}",
                        ]

                        if failed_tests:
                            output_lines.append("\nFailed Tests:")
                            for test in failed_tests:
                                output_lines.append(f"  - {test.get('file', 'unknown')}: {test.get('title', '')}")
                                if test.get("error"):
                                    output_lines.append(f"    Error: {test['error']}")

                        return {
                            "success": success,
                            "output": "\n".join(output_lines),
                            "errors": ("" if success else "\n".join([t.get("error", "") for t in failed_tests if t.get("error")])),
                            "return_code": 0 if success else 1,
                            "command": "test_watcher_mcp_query",
                            "test_file": None,
                            "stability_issue": False,
                            "mcp_results": results,
                        }
        except Exception as e:
            logger.warning(f"Failed to query test_watcher MCP, falling back to normal test execution: {e}")

    try:
        # If a specific test file is specified, run only that test (always via TEST_SCRIPT_PATH)
        if test_file:
            with ProgressStage(f"Running only the specified test file via script: {test_file}"):
                logger.info(f"Running only the specified test file via script: {test_file}")
                cmd_list = ["bash", config.TEST_SCRIPT_PATH, test_file]
                result = cmd.run_command(cmd_list, timeout=cmd.DEFAULT_TIMEOUTS["test"])
                return {
                    "success": result.success,
                    "output": result.stdout,
                    "errors": result.stderr,
                    "return_code": result.returncode,
                    "command": " ".join(cmd_list),
                    "test_file": test_file,
                    "stability_issue": False,
                }

        # Always run via test script
        cmd_list = ["bash", config.TEST_SCRIPT_PATH]
        logger.info(f"Running local tests via script: {config.TEST_SCRIPT_PATH}")
        result = cmd.run_command(cmd_list, timeout=cmd.DEFAULT_TIMEOUTS["test"])
        logger.info(f"Finished local tests. {'Passed' if result.success else 'Failed'}")

        # If the test run failed and isolate_single_test_on_failure is enabled in config.toml,
        # try to extract the first failed test file and run it via the script
        if not result.success and get_isolate_single_test_on_failure_from_config():
            # Extract the first failed test file from the output
            first_failed_test = extract_first_failed_test(result.stdout, result.stderr)
            if first_failed_test:
                logger.info(f"Detected failing test file {first_failed_test}; rerunning targeted script")
                # Store the full suite result for comparison
                full_suite_result = {
                    "success": result.success,
                    "output": result.stdout,
                    "errors": result.stderr,
                    "return_code": result.returncode,
                    "command": " ".join(cmd_list),
                }

                # Run the isolated test
                isolated_cmd_list = ["bash", config.TEST_SCRIPT_PATH, first_failed_test]
                isolated_result = cmd.run_command(isolated_cmd_list, timeout=cmd.DEFAULT_TIMEOUTS["test"])

                # Check for stability issue: failed in full suite but passed in isolation
                if isolated_result.success:
                    logger.warning(f"Test stability issue detected: {first_failed_test} failed in full suite but passed in isolation")
                    return {
                        "success": False,
                        "output": isolated_result.stdout,
                        "errors": isolated_result.stderr,
                        "return_code": isolated_result.returncode,
                        "command": " ".join(isolated_cmd_list),
                        "test_file": first_failed_test,
                        "stability_issue": True,
                        "full_suite_result": full_suite_result,
                    }
                else:
                    # Test still fails in isolation, return the isolated result
                    return {
                        "success": isolated_result.success,
                        "output": isolated_result.stdout,
                        "errors": isolated_result.stderr,
                        "return_code": isolated_result.returncode,
                        "command": " ".join(isolated_cmd_list),
                        "test_file": first_failed_test,
                        "stability_issue": False,
                    }

        return {
            "success": result.success,
            "output": result.stdout,
            "errors": result.stderr,
            "return_code": result.returncode,
            "command": " ".join(cmd_list),
            "test_file": None,
            "stability_issue": False,
        }
    except Exception as e:
        logger.error(f"Local test execution failed: {e}")
        return {
            "success": False,
            "output": "",
            "errors": str(e),
            "return_code": -1,
            "command": "",
            "test_file": None,
            "stability_issue": False,
        }



@log_calls
def run_github_action_tests(config: AutomationConfig, attempt: int) -> Dict[str, Any]:
    """Run tests via GitHub Action by committing and pushing.

    1. Commit current changes (if any).
    2. Push to current branch.
    3. Wait for GitHub Action checks to complete.
    4. Compile results.
    """
    logger.info("Preparing to run tests via GitHub Action...")

    # 1. Commit changes
    committed = False
    status_result = cmd.run_command(["git", "status", "--porcelain"])
    
    if status_result.success and status_result.stdout.strip():
        try:
            commit_msg = f"Auto-fix: attempting to pass tests (attempt {attempt})"
            result = git_commit_with_retry(commit_msg)
            committed = result.success
        except Exception as e:
            logger.warning(f"Commit failed: {e}")
    else:
        logger.info("No changes to commit (clean workspace)")

    # 2. Push changes
    # Only push if we committed something OR if there are unpushed commits
    should_push = committed or check_unpushed_commits()
    
    if should_push:
        try:
            git_push() # Should we force? safer not to, assuming we are on a synced branch
        except Exception as e:
            logger.error(f"Failed to push changes: {e}")
            return {
                "success": False,
                "output": "",
                "errors": f"Failed to push changes to GitHub: {e}",
                "return_code": -1,
                "command": "git push",
                "test_file": None,
                "stability_issue": False,
            }
    else:
        logger.info("No new commits and no unpushed changes. Using existing GitHub Action results.")

    # 3. Get current SHA
    sha = get_current_commit_sha()
    logger.info(f"Target commit {sha}. Waiting for checks...")

    # 4. Wait for checks
    # We poll get_check_runs every N seconds
    gh_client = GitHubClient.get_instance()
    repo_name = get_current_repo_name()
    if not repo_name:
         return {
            "success": False,
            "output": "",
            "errors": "Could not determine repository name",
            "return_code": -1,
            "command": "git push",
            "test_file": None,
            "stability_issue": False,
        }

    start_time = time.time()
    # If we didn't push, we expect results immediately. Don't wait too long if they don't exist.
    timeout = 60 * 30  # 30 minutes timeout
    
    # If we didn't push, allow a quick check for existing results without waiting loop if possible
    # But sticking to the loop is safer, just maybe fail fast if empty?
    
    while True:
        if time.time() - start_time > timeout:
            return {
                "success": False,
                "output": "",
                "errors": "Timed out waiting for GitHub Action checks",
                "return_code": -1,
                "command": "wait_for_checks",
                "test_file": None,
                "stability_issue": False,
            }

        check_runs = gh_client.get_check_runs(repo_name, sha)
        
        # Filter for relevant checks? For now convert all to a result.
        # If no checks found yet, wait.
        if not check_runs:
             if not should_push:
                 # If we didn't push, and there are no checks, maybe we shouldn't wait forever?
                 # But maybe checks are lagging?
                 # Let's wait a bit shorter time? Or just warn?
                 # User said "adopt the result ... that has already been executed". 
                 # If none executed, that's a problem. 
                 # Let's retry a few times then fail?
                 if time.time() - start_time > 30: # Wait at most 30 seconds for existing checks
                      return {
                        "success": False,
                        "output": "",
                        "errors": "No GitHub Action checks found for the current commit.",
                        "return_code": -1,
                        "command": "github_action_checks",
                        "test_file": None,
                        "stability_issue": False,
                    }

             logger.info("No check runs found yet. Waiting...")
             time.sleep(10)
             continue

        # Check statuses
        # We look for "completed" status.
        all_completed = all(run["status"] == "completed" for run in check_runs)
        
        if all_completed:
            # Analyze results
            failed_runs = [run for run in check_runs if run["conclusion"] != "success"]
            success = len(failed_runs) == 0
            
            output_lines = []
            error_lines = []
            
            if not success:
               # Use shared routine to get logs
               # failed_runs struct matches expectation (has details_url)
               try:
                   logs = _get_github_actions_logs(repo_name, config, failed_runs)
                   output_lines.append(logs)
               except Exception as e:
                   logger.error(f"Failed to get GitHub Action logs: {e}")
                   output_lines.append(f"Failed to retrieve detailed logs: {e}")

            for run in check_runs:
                # Brief summary for each run
                status_str = f"Check: {run['name']} - {run['conclusion']}"
                if run['conclusion'] != "success":
                   error_lines.append(status_str)
                   if run.get('output') and run['output'].get('title'):
                        error_lines.append(f"  Title: {run['output']['title']}")
                else:
                   output_lines.append(status_str)
            
            return {
                "success": success,
                "output": "\n".join(output_lines),
                "errors": "\n".join(error_lines),
                "return_code": 0 if success else 1,
                "command": "github_action_checks",
                "test_file": None,
                "stability_issue": False,
            }

        # If not all completed
        completed_count = sum(1 for run in check_runs if run["status"] == "completed")
        logger.info(f"Waiting for checks: {completed_count}/{len(check_runs)} completed")
        time.sleep(15)


@log_calls  # type: ignore[misc]
def apply_test_stability_fix(
    config: AutomationConfig,
    test_file: str,
    full_suite_result: Dict[str, Any],
    isolated_result: Dict[str, Any],
    llm_backend_manager: "BackendManager",
) -> WorkspaceFixResult:
    """Ask the LLM to fix test stability/dependency issues.

    Called when a test fails in the full suite but passes in isolation,
    indicating test isolation or dependency problems.
    """
    backend, provider, model = _extract_backend_model(llm_backend_manager)

    try:
        full_suite_output = f"{full_suite_result.get('errors', '')}\n{full_suite_result.get('output', '')}".strip()
        isolated_output = f"{isolated_result.get('errors', '')}\n{isolated_result.get('output', '')}".strip()

        fix_prompt = render_prompt(
            "tests.test_stability_fix",
            test_file=test_file,
            full_suite_output=full_suite_output[: config.MAX_PROMPT_SIZE],
            isolated_test_output=isolated_output[: config.MAX_PROMPT_SIZE // 2],
        )

        logger.info(f"Requesting LLM test stability fix for {test_file} using backend {backend} " f"provider {provider} model {model}")

        # Use the LLM backend manager to run the prompt
        response = llm_backend_manager.run_test_fix_prompt(fix_prompt, current_test_file=test_file)

        backend, provider, model = _extract_backend_model(llm_backend_manager)
        raw_response = response.strip() if response and response.strip() else None
        if raw_response:
            first_line = raw_response.splitlines()[0]
            summary = first_line[: config.MAX_RESPONSE_SIZE]
        else:
            summary = "LLM produced no response"

        logger.info(f"LLM test stability fix summary: {summary if summary else '<empty response>'}")

        return WorkspaceFixResult(
            summary=summary,
            raw_response=raw_response,
            backend=backend,
            provider=provider,
            model=model,
        )
    except Exception as e:
        return WorkspaceFixResult(
            summary=f"Error applying test stability fix: {e}",
            raw_response=None,
            backend=backend,
            provider=provider,
            model=model,
        )


@log_calls
def _resolve_issue_body(repo_name: str, branch_name: str, gh_client: GitHubClient) -> Optional[str]:
    """
    Resolve the relevant issue or PR body for a given branch.
    
    Logic:
    1. Extract number from branch.
    2. If number found:
       - Check if it's a PR.
       - If PR: check for linked/closing issues.
         - If linked issue found: return linked issue body (highest priority context).
         - Else: return PR body.
       - If not PR (or is just Issue): return Issue body.
    3. If no number found in branch (or extraction failed):
       - Search for open PR where head branch matches `branch_name`.
       - If matching PR found, recurse logic as if it was a PR number.
       
    Returns:
        The body text of the most relevant Issue or PR, or None if not found.
    """
    try:
        # 1. Try to extract number from branch
        item_number = extract_number_from_branch(branch_name)
        
        if item_number:
            repo = gh_client.get_repository(repo_name)
            
            # Check if it is a PR
            try:
                # Note: PyGithub get_pull raises UnknownObjectException if number is not a PR (even if it's an Issue)
                # But get_issue works for both (mostly).
                # We want to treat it as PR if possible to check for linked issues.
                pr = repo.get_pull(item_number)
                
                # It is a PR
                logger.info(f"Branch '{branch_name}' corresponds to PR #{item_number}")
                
                # Check for closing issues
                closing_issue_ids = gh_client.get_pr_closing_issues(repo_name, item_number)
                if closing_issue_ids:
                    # Fetch the first closing issue
                    closing_issue_id = closing_issue_ids[0]
                    logger.info(f"PR #{item_number} closes issue #{closing_issue_id}. Using issue body.")
                    issue = repo.get_issue(closing_issue_id)
                    return issue.body
                else:
                    logger.info(f"PR #{item_number} has no linked closing issues. Using PR body.")
                    return pr.body
                    
            except Exception:
                # Not a PR, or get_pull failed. Treat as Issue.
                logger.info(f"Branch '{branch_name}' number #{item_number} treated as Issue")
                issue = repo.get_issue(item_number)
                return issue.body
                
        else:
            # 2. No number in branch name (e.g. feature-branch)
            # Find PR by branch name
            logger.info(f"No number in branch '{branch_name}'. Searching for PRs with this head branch.")
            pr_data = gh_client.find_pr_by_head_branch(repo_name, branch_name)
            
            if pr_data:
                pr_number = pr_data.get("number")
                if pr_number:
                    logger.info(f"Found PR #{pr_number} for branch '{branch_name}'. processing as PR.")
                    # Recurse or duplicate logic? Duplicate slightly to avoid infinite recursion risk if simple
                    # Reuse the same logic by calling with mocked branch name or just jumping to PR logic
                    return _resolve_issue_body(repo_name, f"pr-{pr_number}", gh_client)
            
            logger.info(f"No context found for branch '{branch_name}'")
            return None
            
    except Exception as e:
        logger.warning(f"Error resolving issue body for branch '{branch_name}': {e}")
        return None


@log_calls  # type: ignore[misc]
def apply_workspace_test_fix(
    config: AutomationConfig,
    test_result: Dict[str, Any],
    llm_backend_manager: "BackendManager",
    current_test_file: Optional[str] = None,
    attempt_history: Optional[list[Dict[str, Any]]] = None,
) -> WorkspaceFixResult:
    """Ask the LLM to apply workspace edits based on local test failures.

    Args:
        config: AutomationConfig instance
        test_result: Test result dictionary from run_local_tests
        llm_backend_manager: Backend manager instance
        current_test_file: Current test file being fixed
        attempt_history: List of previous attempts with LLM outputs and test results

    Returns:
        WorkspaceFixResult containing the LLM response and metadata
    """

    backend, provider, model = _extract_backend_model(llm_backend_manager)

    try:
        # Convert legacy dict payloads to TestResult for structured extraction
        tr = _to_test_result(test_result)
        error_summary = extract_important_errors(tr)
        if not error_summary:
            logger.info("Skipping LLM workspace fix because no actionable errors were extracted")
            return WorkspaceFixResult(
                summary="No actionable errors found in local test output",
                raw_response=None,
                backend=backend,
                provider=provider,
                model=model,
            )

        # Format attempt history for inclusion in prompt
        history_text = ""
        if attempt_history:
            history_parts = []
            for hist in attempt_history:
                attempt_num = hist.get("attempt_number", "N/A")
                llm_output = hist.get("llm_output", "No output")
                test_out = hist.get("test_result", {})
                test_errors = test_out.get("errors", "") or test_out.get("output", "")
                # Truncate long outputs
                test_errors_truncated = (test_errors[:500] + "...") if len(test_errors) > 500 else test_errors
                llm_output_truncated = (llm_output[:300] + "...") if len(str(llm_output)) > 300 else llm_output
                history_parts.append(f"Attempt {attempt_num}:\n" f"  LLM Output: {llm_output_truncated}\n" f"  Test Result: {test_errors_truncated}")
            history_text = "\n\n".join(history_parts)

        # Try to resolve issue/PR body
        issue_body = None
        try:
            current_branch = get_current_branch()
            repo_name = get_current_repo_name()
            if current_branch and repo_name:
                logger.info(f"Resolving issue/PR context for branch '{current_branch}'")
                gh_client = GitHubClient.get_instance()
                issue_body = _resolve_issue_body(repo_name, current_branch, gh_client)
        except Exception as e:
            logger.warning(f"Failed to fetch issue context for test fix: {e}")

        test_command = test_result.get("command", "pytest -q --maxfail=1")
        if test_command == "github_action_checks":
            test_command = "scripts/test.sh"

        fix_prompt = render_prompt(
            "tests.workspace_fix",
            error_summary=error_summary,
            test_command=test_command,
            attempt_history=history_text,
            issue_body=issue_body,
        )

        # Use the LLM backend manager to run the prompt
        logger.info(f"Requesting LLM workspace fix using backend {backend} provider {provider} " f"model {model} (custom prompt handler)")
        response = llm_backend_manager.run_test_fix_prompt(fix_prompt, current_test_file=current_test_file)

        backend, provider, model = _extract_backend_model(llm_backend_manager)
        raw_response = response.strip() if response and response.strip() else None
        if raw_response:
            first_line = raw_response.splitlines()[0]
            summary = first_line[: config.MAX_RESPONSE_SIZE]
        else:
            summary = "LLM produced no response"

        logger.info(f"LLM workspace fix summary: {summary if summary else '<empty response>'}")

        return WorkspaceFixResult(
            summary=summary,
            raw_response=raw_response,
            backend=backend,
            provider=provider,
            model=model,
        )
    except Exception as e:
        return WorkspaceFixResult(
            summary=f"Error applying workspace test fix: {e}",
            raw_response=None,
            backend=backend,
            provider=provider,
            model=model,
        )


def fix_to_pass_tests(
    config: AutomationConfig,
    llm_backend_manager: "BackendManager",
    max_attempts: Optional[int] = None,
    message_backend_manager: Optional["BackendManager"] = None,
    enable_github_action: bool = False,
) -> Dict[str, Any]:
    """Run tests and, if failing, repeatedly request LLM fixes until tests pass.

    If the LLM makes no edits (no changes to commit) in an iteration, raise an error and stop.
    Returns a summary dict.
    """
    attempts_limit = max_attempts if isinstance(max_attempts, int) and max_attempts > 0 else config.MAX_FIX_ATTEMPTS
    summary: Dict[str, Any] = {
        "mode": "fix-to-pass-tests",
        "attempts": 0,
        "success": False,
        "messages": [],
    }

    # Track previous test output and the error summary given to LLM (from last completed test run)
    # Cache the latest post-fix test result to avoid redundant runs in the next loop
    cached_test_result: Optional[Dict[str, Any]] = None
    cached_result_attempt: Optional[int] = None

    # Track the test file that is currently being fixed
    current_test_file: Optional[str] = None

    # Track history of previous attempts for context
    attempt_history: list[Dict[str, Any]] = []

    # Support infinite attempts (math.inf) by using a while loop
    attempt = 0  # counts actual test executions
    while True:
        try:
            check_for_updates_and_restart()
        except SystemExit:
            raise
        except Exception:
            logger.warning("Auto-update check failed during fix loop", exc_info=True)

        # Use cached result (from previous post-fix run) if available; otherwise run tests now
        if cached_test_result is not None:
            test_result = cached_test_result
            cached_test_result = None
            attempt_label = cached_result_attempt if cached_result_attempt is not None else attempt
            target_label = current_test_file or "ALL_TESTS"
            logger.info(f"Reusing cached post-fix test result from attempt {attempt_label} for {target_label}")
            cached_result_attempt = None
        else:
            attempt += 1
            summary["attempts"] = attempt
            logger.info(f"Running local tests (attempt {attempt}/{attempts_limit})")
            if enable_github_action:
                test_result = run_github_action_tests(config, attempt)
            else:
                test_result = run_local_tests(config, test_file=current_test_file)
            # Update the current test file being fixed
            current_test_file = test_result.get("test_file")
        if test_result["success"]:
            if current_test_file is not None:
                logger.info(f"Targeted test {current_test_file} passed; clearing focus before rerunning full suite")
                current_test_file = None
                continue
            msg = f"Local tests passed on attempt {attempt}"
            logger.info(msg)
            summary["messages"].append(msg)
            summary["success"] = True
            cleanup_llm_task_file()
            return summary

        # Check for test stability issue (failed in full suite but passed in isolation)
        if test_result.get("stability_issue", False):
            stability_msg = f"Test stability issue detected for {test_result.get('test_file')}"
            logger.warning(stability_msg)
            summary["messages"].append(stability_msg)

            # Apply LLM-based stability fix
            fix_response = apply_test_stability_fix(
                config,
                test_result["test_file"],
                test_result["full_suite_result"],
                test_result,
                llm_backend_manager,
            )
            action_msg = fix_response.summary
            summary["messages"].append(action_msg)
        else:
            # Apply LLM-based fix for regular test failures
            fix_response = apply_workspace_test_fix(
                config,
                test_result,
                llm_backend_manager,
                current_test_file=current_test_file,
                attempt_history=attempt_history,
            )
            action_msg = fix_response.summary
            summary["messages"].append(action_msg)

            # Store this attempt in history for future reference
            if fix_response.raw_response:
                attempt_history.append(
                    {
                        "attempt_number": attempt,
                        "llm_output": fix_response.raw_response,
                        "test_result": test_result,
                    }
                )

        # Baseline (pre-fix) outputs for comparison
        baseline_full_output = f"{test_result.get('errors', '')}\n{test_result.get('output', '')}".strip()
        baseline_error_summary = extract_important_errors(_to_test_result(test_result))

        # Re-run tests AFTER LLM edits to measure change and decide commit
        attempt += 1
        summary["attempts"] = attempt
        # Re-run tests AFTER LLM edits to measure change and decide commit
        attempt += 1
        summary["attempts"] = attempt
        logger.info(f"Re-running tests after LLM fix (attempt {attempt}/{attempts_limit})")
        if enable_github_action:
            post_result = run_github_action_tests(config, attempt)
        else:
            post_result = run_local_tests(config, test_file=current_test_file)

        log_timestamp = datetime.now()
        backend_for_log = fix_response.backend
        provider_for_log = fix_response.provider
        model_for_log = fix_response.model
        try:
            _log_fix_attempt_metadata(current_test_file, backend_for_log, provider_for_log, model_for_log, log_timestamp)
        except Exception:
            logger.warning("Failed to record fix-to-pass-tests summary CSV entry", exc_info=True)
        try:
            _write_llm_output_log(
                raw_output=fix_response.raw_response,
                test_file=current_test_file,
                backend=backend_for_log,
                provider=provider_for_log,
                model=model_for_log,
                timestamp=log_timestamp,
            )
        except Exception:
            logger.warning("Failed to write LLM output log for fix-to-pass-tests", exc_info=True)

        try:
            repo_name_for_log = get_current_repo_name() or "unknown_repo"
            _write_test_log_json(
                repo_name=repo_name_for_log,
                test_file=current_test_file,
                attempt=attempt,
                test_result=test_result,
                fix_result=fix_response,
                post_test_result=post_result,
                timestamp=log_timestamp,
            )
        except Exception:
            logger.warning("Failed to write JSON test log", exc_info=True)

        post_full_output = f"{post_result.get('errors', '')}\n{post_result.get('output', '')}".strip()
        post_error_summary = extract_important_errors(_to_test_result(post_result))

        # Update previous context for next loop start
        cleanup_pending = False

        if post_result["success"]:
            # Tests passed after the fix; proceed to commit
            pass_msg = f"Local tests passed on attempt {attempt}"
            logger.info(pass_msg)
            summary["messages"].append(pass_msg)
            cleanup_pending = True
        else:
            # Compute change ratios between pre-fix and post-fix results
            try:
                change_ratio_tests = change_fraction(baseline_full_output or "", post_full_output or "")
                change_ratio_errors = change_fraction(baseline_error_summary or "", post_error_summary or "")
                max_change = max(change_ratio_tests, change_ratio_errors)
            except Exception:
                max_change = 1.0  # default to commit if comparison fails

            if max_change < 0.10:
                # Consider this as insufficient change; skip commit and ask LLM again next loop
                info = "Change below 10% threshold; skipping commit and retrying"
                logger.info(f"{info} (max change {max_change * 100:.2f}%)")
                summary["messages"].append(info)
                # Use this post-fix test result as the starting point for the next loop
                cached_test_result = post_result
                cached_result_attempt = attempt
                # Stop if finite limit reached
                try:
                    if isinstance(attempts_limit, (int, float)) and math.isfinite(float(attempts_limit)) and attempt >= int(attempts_limit):
                        break
                except Exception:
                    pass
                # Continue to next loop (will request another LLM fix)
                continue
            else:
                info = f"Significant change detected ({max_change:.2%}); committing and continuing"
            logger.info(info)
            summary["messages"].append(info)

        if cleanup_pending:
            cleanup_llm_task_file()

        # Stage and commit; detect 'no changes' as an immediate error per requirement
        # Stage and commit; detect 'no changes' as an immediate error per requirement
        # Use -A to ensure all changes (including deletions) are staged
        add_res = cmd.run_command(["git", "add", "-A"])
        if not add_res.success:
            errmsg = f"Failed to stage changes: {add_res.stderr}"
            logger.error(errmsg)
            summary["messages"].append(errmsg)
            break

        # Verify that changes were actually staged
        # git diff --cached --quiet returns 0 if NO changes are staged, 1 if changes EXIST
        diff_res = cmd.run_command(["git", "diff", "--cached", "--quiet"])
        if diff_res.returncode == 0:
            errmsg = "No changes staged for commit despite significant test output change being detected."
            logger.error(errmsg)
            summary["messages"].append(errmsg)
            break

        llm_backend_manager.switch_to_default_backend()
        # Ask LLM to craft a clear, concise commit message for the applied change
        commit_msg = generate_commit_message_via_llm(
            llm_backend_manager=llm_backend_manager,
            message_backend_manager=message_backend_manager,
        )
        if not commit_msg:
            commit_msg = format_commit_message(config, action_msg, attempt)

        # Commit the changes with the generated message using centralized helper
        if commit_msg:
            commit_res = git_commit_with_retry(commit_msg)
            if not commit_res.success:
                # Save history and exit immediately
                context = {
                    "type": "fix_to_pass_tests",
                    "attempt": attempt,
                    "test_file": current_test_file,
                    "commit_message": commit_msg,
                }
                save_commit_failure_history(commit_res.stderr, context, repo_name=None)
                # This line will never be reached due to sys.exit in save_commit_failure_history
                logger.warning(f"Failed to commit changes: {commit_res.stderr}")
            else:
                logger.info(f"Committed changes: {commit_msg}")

        # If tests passed, mark success and push changes
        if post_result["success"]:
            if current_test_file is not None:
                logger.info(f"Targeted test {current_test_file} passed after LLM fix; rerunning full suite")
                current_test_file = None
                continue
            summary["success"] = True

            # Push changes to remote
            logger.info("Tests passed, pushing changes to remote...")
            push_result = git_push()
            if push_result.success:
                logger.info("Successfully pushed changes to remote")
                summary["messages"].append("Pushed changes to remote")
            else:
                logger.error(f"Failed to push changes: {push_result.stderr}")
                logger.error("Exiting application due to git push failure")
                sys.exit(1)

            return summary

        # Cache the failing post-fix result for the next loop to avoid re-running before LLM edits
        cached_test_result = post_result
        cached_result_attempt = attempt

        # Stop if finite limit reached
        try:
            if isinstance(attempts_limit, (int, float)) and math.isfinite(float(attempts_limit)) and attempt >= int(attempts_limit):
                logger.info(f"Reached attempt limit ({attempts_limit}); exiting fix loop")
                break
        except Exception:
            # If attempts_limit is not a number, treat as unlimited
            pass

    # Final test after exhausting attempts (optional): do not re-run here because we already
    # executed a post-fix run within the loop. Keep messages concise.
    summary["messages"].append("Local tests still failing after attempts")

    return summary


def generate_commit_message_via_llm(
    llm_backend_manager: "BackendManager",
    message_backend_manager: Optional["BackendManager"] = None,
) -> str:
    """Use LLM to generate a concise commit message based on the fix context.

    Keeps the call minimal and instructs the model to output a single-line subject only.
    Never asks the LLM to run git commands.

    Args:
        llm_backend_manager: Main LLM backend manager (used as fallback)
        message_backend_manager: Dedicated message backend manager (preferred)
    """
    try:
        # Use message_backend_manager if available, otherwise fall back to llm_backend_manager
        manager = message_backend_manager if message_backend_manager is not None else llm_backend_manager

        # Get commit log since branch creation for commit message context
        commit_log = get_commit_log()

        prompt = render_prompt("tests.commit_message", commit_log=commit_log or "(No commit history)")

        response = manager._run_llm_cli(prompt)
        if not response:
            return ""

        # Extract message from code blocks if present (```...```)
        response = response.strip()
        # Find the first ``` and the last ``` in the response
        first_marker = response.find("```")
        if first_marker != -1:
            # Find the closing ``` after the first one
            last_marker = response.rfind("```", first_marker + 3)
            if last_marker != -1 and last_marker > first_marker:
                # Extract content between the markers
                content = response[first_marker + 3 : last_marker].strip()
                # If the content starts with a language identifier (e.g., ```bash), remove it
                lines = content.splitlines()
                if lines:
                    # Skip the first line if it looks like a language identifier (no spaces, short)
                    first_line = lines[0].strip()
                    if first_line and len(first_line) < 20 and " " not in first_line:
                        content = "\n".join(lines[1:]).strip()
                return str(content) if content else ""

        # Take first non-empty line, sanitize length
        for line in response.splitlines():
            line = line.strip().strip('"').strip("'").strip("`")
            if line:
                if len(line) > 72:
                    line = line[:72].rstrip()
                return f"Auto-Coder: {line}"
        return ""
    except Exception:
        return ""


def format_commit_message(config: AutomationConfig, llm_summary: str, attempt: int) -> str:
    """Create a concise commit message using the LLM-produced summary.

    - Prefix with "Auto-Coder:" to unify automation commits
    - Trim to a reasonable length
    - Fallback to a generic message if empty
    """
    base = (llm_summary or "").strip()
    if not base:
        base = "Fix local tests"
    # Limit length to ~100 chars
    if len(base) > 100:
        base = base[:100].rstrip()
    return f"Auto-Coder: {base}"


@log_calls  # type: ignore[misc]
def extract_important_errors(test_result: TestResult) -> str:
    """Extract important error information from test output.

    Preserves multi-framework detection (pytest, Playwright, Vitest),
    Unicode markers (×, ›), and ANSI-friendly parsing.
    """
    if test_result.success:
        return ""

    errors = test_result.errors or ""
    output = test_result.output or ""

    # Combine stderr and stdout
    full_output = f"{errors}\n{output}".strip()
    logger.debug(
        "extract_important_errors: len(output)=%d len(errors)=%d framework=%s",
        len(output),
        len(errors),
        test_result.framework_type or "unknown",
    )

    # Prepend stability warning if detected
    prefix = ""
    if test_result.stability_issue:
        prefix = f"Test stability issue detected: {test_result.test_file or 'unknown'} failed in full suite but passed in isolation.\n\n"

    # Detect Playwright and prepend summary
    if _detect_failed_test_library(full_output) == "playwright":
        passed_count = extract_playwright_passed_count(full_output)
        failed_tests = _collect_playwright_candidates(full_output)

        summary_lines = ["Playwright Test Summary:"]
        summary_lines.append(f"Passed: {passed_count}")
        summary_lines.append(f"Failed: {len(failed_tests)}")
        if failed_tests:
            summary_lines.append("Failed Tests:")
            for t in failed_tests:
                summary_lines.append(f"- {t}")
        summary_lines.append("\n")

        prefix += "\n".join(summary_lines)

    if not full_output:
        return prefix + "Tests failed but no error output available"

    lines = full_output.split("\n")

    # 0) If test detail lines with expected/received (Playwright/Jest) are included, prioritize extracting around the header
    if ("Expected substring:" in full_output) or ("Received string:" in full_output) or ("expect(received)" in full_output):
        try:
            import re

            # Find candidate header by scanning backwards
            # Playwright header: allow cases without leading "Error:" and allow leading spaces or the × mark
            hdr_pat = re.compile(r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\).*\.spec\.ts:.*")
            idx_expect = None
            for i, ln in enumerate(lines):
                if ("Expected substring:" in ln) or ("Received string:" in ln) or ("expect(received)" in ln):
                    idx_expect = i
                    break
            if idx_expect is not None:
                start = 0
                for j in range(idx_expect, -1, -1):
                    if hdr_pat.search(lines[j]):
                        start = j
                        break
                end = min(len(lines), idx_expect + 60)
                block_str = "\n".join(lines[start:end])
                if block_str:
                    return prefix + block_str
        except Exception:
            pass

    # 1) Prefer Playwright-typical error pattern extraction
    try:
        import re

        # Failure header: "Error:   1) [suite] › e2e/... .spec.ts:line:col › ..."
        header_indices = []
        # Playwright header: allow both with/without leading "Error:" and also leading whitespace or the × mark
        header_regex = re.compile(
            r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+\[[^\]]+\]\s+\u203a\s+.*\.spec\.ts:\d+:\d+\s+\u203a\s+.*|" r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+.*\.spec\.ts:.*",
            re.UNICODE,
        )
        for idx, ln in enumerate(lines):
            if header_regex.search(ln):
                header_indices.append(idx)
        # Typical expected/received patterns
        expect_regex = re.compile(r"expect\(received\).*|Expected substring:|Received string:")

        blocks = []
        for start_idx in header_indices:
            end_idx = min(len(lines), start_idx + 120)  # wider context up to 120 lines
            # Stop at the next error header (do not stop at empty lines)
            for j in range(start_idx + 1, min(len(lines), start_idx + 300)):
                if j >= len(lines):
                    break
                s = lines[j]
                if header_regex.search(s):
                    end_idx = j
                    break
            block = "\n".join(lines[start_idx:end_idx])
            # Check if it includes expectation/received lines or the corresponding expect line
            if any(expect_regex.search(b) for b in lines[start_idx:end_idx]) or any(".spec.ts" in b for b in lines[start_idx:end_idx]):
                blocks.append(block)
        if blocks:
            result = "\n\n".join(blocks)
            # Append expectation/received lines if not included
            if "Expected substring:" not in result or "Received string:" not in result:
                extra_lines = []
                for i, ln in enumerate(lines):
                    if "Expected substring:" in ln or "Received string:" in ln or "expect(received)" in ln:
                        start = max(0, i - 2)
                        end = min(len(lines), i + 4)
                        extra_lines.extend(lines[start:end])
                if extra_lines:
                    result = result + "\n\n--- Expectation Details ---\n" + "\n".join(extra_lines)
            if len(result) > 3000:
                result = result[:3000] + "\n... (output truncated)"
            return prefix + result
    except Exception:
        pass

    # 2) Keyword-based fallback extraction
    important_lines = []
    # Keywords that indicate important error information
    error_keywords = [
        # error detection
        "error:",
        "Error:",
        "ERROR:",
        "error",
        # failed detection
        "failed:",
        "Failed:",
        "FAILED:",
        "failed",
        # exceptions and traces
        "exception:",
        "Exception:",
        "EXCEPTION:",
        "traceback:",
        "Traceback:",
        "TRACEBACK:",
        # assertions and common python errors
        "assertion",
        "Assertion",
        "ASSERTION",
        "syntax error",
        "SyntaxError",
        "import error",
        "ImportError",
        "module not found",
        "ModuleNotFoundError",
        "test failed",
        "Test failed",
        "TEST FAILED",
        # e2e / Playwright related
        "e2e/",
        ".spec.ts",
        "playwright",
    ]

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(keyword.lower() in line_lower for keyword in error_keywords):
            # Extract a slightly broader context
            start = max(0, i - 5)
            end = min(len(lines), i + 8)
            context_lines = lines[start:end]
            important_lines.extend(context_lines)

    # Remove duplicates while preserving order
    seen = set()
    unique_lines = []
    for line in important_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)

    # Limit output length
    result = "\n".join(unique_lines)
    if len(result) > 2000:
        result = result[:2000] + "\n... (output truncated)"

    return prefix + (result if result else "Tests failed but no specific error information found")


def run_pr_tests(config: AutomationConfig, pr_data: Dict[str, Any]) -> Dict[str, Any]:
    """Run tests for a PR and return results."""
    pr_number = pr_data["number"]

    try:
        log_action(f"Running tests for PR #{pr_number}")
        result = run_local_tests(config)
        log_action(f"Test result for PR #{pr_number}: {'PASS' if result['success'] else 'FAIL'}")
        return result

    except Exception as e:
        error_msg = f"Error running tests for PR #{pr_number}: {e}"
        logger.error(error_msg)
        return {"success": False, "output": "", "errors": error_msg, "return_code": -1}
