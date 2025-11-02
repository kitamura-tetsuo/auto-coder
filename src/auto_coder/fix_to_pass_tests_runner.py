"""Test execution functionality for Auto-Coder automation engine."""

import csv
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from .automation_config import AutomationConfig
from .git_utils import git_commit_with_retry, git_push, save_commit_failure_history
from .logger_config import get_logger, log_calls
from .progress_footer import ProgressStage
from .prompt_loader import render_prompt
from .update_manager import check_for_updates_and_restart
from .utils import (
    CommandExecutor,
    change_fraction,
    extract_first_failed_test,
    log_action,
)

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
    model: str


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


def _log_fix_attempt_metadata(
    test_file: Optional[str], backend: str, model: str, timestamp: datetime
) -> Path:
    """Append metadata about a fix attempt to the CSV summary log."""

    base_dir = Path(".auto-coder")
    base_dir.mkdir(parents=True, exist_ok=True)
    csv_path = base_dir / "fix_to_pass_tests_summury.csv"
    file_exists = csv_path.exists()
    record = [
        _normalize_test_file(test_file),
        backend or "unknown",
        model or "unknown",
        timestamp.isoformat(),
    ]
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(["current_test_file", "backend", "model", "timestamp"])
        writer.writerow(record)
    return csv_path


def _write_llm_output_log(
    *,
    raw_output: Optional[str],
    test_file: Optional[str],
    backend: str,
    model: str,
    timestamp: datetime,
) -> Path:
    """Persist the raw LLM output for traceability."""

    log_dir = Path(".auto-coder") / "log"
    log_dir.mkdir(parents=True, exist_ok=True)

    filename = "{time}_{test}_{backend}_{model}.txt".format(
        time=timestamp.strftime("%Y%m%d_%H%M%S"),
        test=_sanitize_for_filename(_normalize_test_file(test_file), default="tests"),
        backend=_sanitize_for_filename(backend or "unknown", default="backend"),
        model=_sanitize_for_filename(model or "unknown", default="model"),
    )
    log_path = log_dir / filename
    content = raw_output if raw_output is not None else "LLM produced no response"
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
    return log_path


@log_calls
def _extract_backend_model(llm_client: Any) -> Tuple[str, str]:
    """Derive backend/model identifiers from the provided LLM client."""

    if llm_client is None:
        return "unknown", "unknown"

    getter = getattr(llm_client, "get_last_backend_and_model", None)
    if callable(getter):
        try:
            backend, model = getter()
            backend = backend or "unknown"
            model = model or getattr(llm_client, "model_name", "unknown")
            return backend, model
        except Exception:
            pass

    backend = getattr(llm_client, "backend", None) or getattr(llm_client, "name", None)
    if backend is None:
        backend = llm_client.__class__.__name__
    model = getattr(llm_client, "model_name", None)
    if not model:
        model = "unknown"
    return str(backend), str(model)


def cleanup_llm_task_file(path: str = "./llm_task.md") -> None:
    """Remove the LLM task log file before committing final fixes."""
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug(f"Removed {path} prior to commit")
    except Exception as exc:
        logger.warning(f"Failed to remove {path}: {exc}")


def run_local_tests(
    config: AutomationConfig, test_file: Optional[str] = None
) -> Dict[str, Any]:
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
                        logger.info(
                            "Tests are currently running in test_watcher, waiting..."
                        )
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
                                output_lines.append(
                                    f"  - {test.get('file', 'unknown')}: {test.get('title', '')}"
                                )
                                if test.get("error"):
                                    output_lines.append(f"    Error: {test['error']}")

                        return {
                            "success": success,
                            "output": "\n".join(output_lines),
                            "errors": (
                                ""
                                if success
                                else "\n".join(
                                    [
                                        t.get("error", "")
                                        for t in failed_tests
                                        if t.get("error")
                                    ]
                                )
                            ),
                            "return_code": 0 if success else 1,
                            "command": "test_watcher_mcp_query",
                            "test_file": None,
                            "stability_issue": False,
                            "mcp_results": results,
                        }
        except Exception as e:
            logger.warning(
                f"Failed to query test_watcher MCP, falling back to normal test execution: {e}"
            )

    try:
        # If a specific test file is specified, run only that test (always via TEST_SCRIPT_PATH)
        if test_file:
            with ProgressStage(
                f"Running only the specified test file via script: {test_file}"
            ):
                logger.info(
                    f"Running only the specified test file via script: {test_file}"
                )
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

        # If the test run failed, try to extract the first failed test file and run it via the script
        if not result.success:
            # Extract the first failed test file from the output
            first_failed_test = extract_first_failed_test(result.stdout, result.stderr)
            if first_failed_test:
                logger.info(
                    f"Detected failing test file {first_failed_test}; rerunning targeted script"
                )
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
                isolated_result = cmd.run_command(
                    isolated_cmd_list, timeout=cmd.DEFAULT_TIMEOUTS["test"]
                )

                # Check for stability issue: failed in full suite but passed in isolation
                if isolated_result.success:
                    logger.warning(
                        f"Test stability issue detected: {first_failed_test} failed in full suite but passed in isolation"
                    )
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
def apply_test_stability_fix(
    config: AutomationConfig,
    test_file: str,
    full_suite_result: Dict[str, Any],
    isolated_result: Dict[str, Any],
    llm_backend_manager: "BackendManager",
    dry_run: bool = False,
) -> WorkspaceFixResult:
    """Ask the LLM to fix test stability/dependency issues.

    Called when a test fails in the full suite but passes in isolation,
    indicating test isolation or dependency problems.
    """
    backend, model = _extract_backend_model(llm_backend_manager)

    try:
        full_suite_output = f"{full_suite_result.get('errors', '')}\n{full_suite_result.get('output', '')}".strip()
        isolated_output = f"{isolated_result.get('errors', '')}\n{isolated_result.get('output', '')}".strip()

        fix_prompt = render_prompt(
            "tests.test_stability_fix",
            test_file=test_file,
            full_suite_output=full_suite_output[: config.MAX_PROMPT_SIZE],
            isolated_test_output=isolated_output[: config.MAX_PROMPT_SIZE // 2],
        )

        if dry_run:
            return WorkspaceFixResult(
                summary="[DRY RUN] Would apply test stability fixes",
                raw_response=None,
                backend=backend,
                model=model,
            )

        logger.info(
            f"Requesting LLM test stability fix for {test_file} using backend {backend} model {model}"
        )

        # Use the LLM backend manager to run the prompt
        response = llm_backend_manager.run_test_fix_prompt(
            fix_prompt, current_test_file=test_file
        )

        backend, model = _extract_backend_model(llm_backend_manager)
        raw_response = response.strip() if response and response.strip() else None
        if raw_response:
            first_line = raw_response.splitlines()[0]
            summary = first_line[: config.MAX_RESPONSE_SIZE]
        else:
            summary = "LLM produced no response"

        logger.info(
            f"LLM test stability fix summary: {summary if summary else '<empty response>'}"
        )

        return WorkspaceFixResult(
            summary=summary,
            raw_response=raw_response,
            backend=backend,
            model=model,
        )
    except Exception as e:
        return WorkspaceFixResult(
            summary=f"Error applying test stability fix: {e}",
            raw_response=None,
            backend=backend,
            model=model,
        )


@log_calls
def apply_workspace_test_fix(
    config: AutomationConfig,
    test_result: Dict[str, Any],
    llm_backend_manager: "BackendManager",
    dry_run: bool = False,
    current_test_file: Optional[str] = None,
) -> WorkspaceFixResult:
    """Ask the LLM to apply workspace edits based on local test failures."""

    backend, model = _extract_backend_model(llm_backend_manager)

    try:
        error_summary = extract_important_errors(test_result)
        if not error_summary:
            logger.info(
                "Skipping LLM workspace fix because no actionable errors were extracted"
            )
            return WorkspaceFixResult(
                summary="No actionable errors found in local test output",
                raw_response=None,
                backend=backend,
                model=model,
            )

        fix_prompt = render_prompt(
            "tests.workspace_fix",
            error_summary=error_summary[: config.MAX_PROMPT_SIZE],
            test_command=test_result.get("command", "pytest -q --maxfail=1"),
        )
        logger.debug(f"0")

        if dry_run:
            return WorkspaceFixResult(
                summary="[DRY RUN] Would apply fixes for local test failures",
                raw_response=None,
                backend=backend,
                model=model,
            )
        logger.debug(f"0")

        # Use the LLM backend manager to run the prompt
        logger.debug(f"0")
        logger.info(
            f"Requesting LLM workspace fix using backend {backend} model {model} (custom prompt handler)"
        )
        response = llm_backend_manager.run_test_fix_prompt(
            fix_prompt, current_test_file=current_test_file
        )

        logger.debug(f"0")

        backend, model = _extract_backend_model(llm_backend_manager)
        raw_response = response.strip() if response and response.strip() else None
        if raw_response:
            logger.debug(f"0")
            first_line = raw_response.splitlines()[0]
            summary = first_line[: config.MAX_RESPONSE_SIZE]
        else:
            logger.debug(f"0")
            summary = "LLM produced no response"

        logger.info(
            f"LLM workspace fix summary: {summary if summary else '<empty response>'}"
        )

        return WorkspaceFixResult(
            summary=summary,
            raw_response=raw_response,
            backend=backend,
            model=model,
        )
    except Exception as e:
        return WorkspaceFixResult(
            summary=f"Error applying workspace test fix: {e}",
            raw_response=None,
            backend=backend,
            model=model,
        )


def fix_to_pass_tests(
    config: AutomationConfig,
    dry_run: bool = False,
    max_attempts: Optional[int] = None,
    llm_backend_manager: Optional["BackendManager"] = None,
    message_backend_manager: Optional["BackendManager"] = None,
) -> Dict[str, Any]:
    """Run tests and, if failing, repeatedly request LLM fixes until tests pass.

    If the LLM makes no edits (no changes to commit) in an iteration, raise an error and stop.
    Returns a summary dict.
    """
    attempts_limit = (
        max_attempts
        if isinstance(max_attempts, int) and max_attempts > 0
        else config.MAX_FIX_ATTEMPTS
    )
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
            attempt_label = (
                cached_result_attempt if cached_result_attempt is not None else attempt
            )
            target_label = current_test_file or "ALL_TESTS"
            logger.info(
                f"Reusing cached post-fix test result from attempt {attempt_label} for {target_label}"
            )
            cached_result_attempt = None
        else:
            attempt += 1
            summary["attempts"] = attempt
            logger.info(f"Running local tests (attempt {attempt}/{attempts_limit})")
            test_result = run_local_tests(config, test_file=current_test_file)
            # Update the current test file being fixed
            current_test_file = test_result.get("test_file")
        if test_result["success"]:
            if current_test_file is not None:
                logger.info(
                    f"Targeted test {current_test_file} passed; clearing focus before rerunning full suite"
                )
                current_test_file = None
                continue
            msg = f"Local tests passed on attempt {attempt}"
            logger.info(msg)
            summary["messages"].append(msg)
            summary["success"] = True
            if not dry_run:
                cleanup_llm_task_file()
            return summary

        # Check for test stability issue (failed in full suite but passed in isolation)
        if test_result.get("stability_issue", False):
            stability_msg = (
                f"Test stability issue detected for {test_result.get('test_file')}"
            )
            logger.warning(stability_msg)
            summary["messages"].append(stability_msg)

            # Apply LLM-based stability fix
            fix_response = apply_test_stability_fix(
                config,
                test_result["test_file"],
                test_result["full_suite_result"],
                test_result,
                llm_backend_manager,
                dry_run,
            )
            action_msg = fix_response.summary
            summary["messages"].append(action_msg)
        else:
            # Apply LLM-based fix for regular test failures
            fix_response = apply_workspace_test_fix(
                config,
                test_result,
                llm_backend_manager,
                dry_run,
                current_test_file=current_test_file,
            )
            action_msg = fix_response.summary
            summary["messages"].append(action_msg)

        if dry_run:
            # In dry-run we do not commit; just continue attempts
            continue

        # Baseline (pre-fix) outputs for comparison
        baseline_full_output = (
            f"{test_result.get('errors', '')}\n{test_result.get('output', '')}".strip()
        )
        baseline_error_summary = extract_important_errors(test_result)

        # Re-run tests AFTER LLM edits to measure change and decide commit
        attempt += 1
        summary["attempts"] = attempt
        logger.info(
            f"Re-running local tests after LLM fix (attempt {attempt}/{attempts_limit})"
        )
        post_result = run_local_tests(config, test_file=current_test_file)

        log_timestamp = datetime.now()
        backend_for_log = fix_response.backend
        model_for_log = fix_response.model
        try:
            _log_fix_attempt_metadata(
                current_test_file, backend_for_log, model_for_log, log_timestamp
            )
        except Exception:
            logger.warning(
                "Failed to record fix-to-pass-tests summary CSV entry", exc_info=True
            )
        try:
            _write_llm_output_log(
                raw_output=fix_response.raw_response,
                test_file=current_test_file,
                backend=backend_for_log,
                model=model_for_log,
                timestamp=log_timestamp,
            )
        except Exception:
            logger.warning(
                "Failed to write LLM output log for fix-to-pass-tests", exc_info=True
            )

        post_full_output = (
            f"{post_result.get('errors', '')}\n{post_result.get('output', '')}".strip()
        )
        post_error_summary = extract_important_errors(post_result)

        # Update previous context for next loop start
        cleanup_pending = False

        if post_result["success"]:
            # Tests passed after the fix; proceed to commit
            pass_msg = f"Local tests passed on attempt {attempt}"
            logger.info(pass_msg)
            summary["messages"].append(pass_msg)
            if not dry_run:
                cleanup_pending = True
        else:
            # Compute change ratios between pre-fix and post-fix results
            try:
                change_ratio_tests = change_fraction(
                    baseline_full_output or "", post_full_output or ""
                )
                change_ratio_errors = change_fraction(
                    baseline_error_summary or "", post_error_summary or ""
                )
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
                    if (
                        isinstance(attempts_limit, (int, float))
                        and math.isfinite(float(attempts_limit))
                        and attempt >= int(attempts_limit)
                    ):
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
        add_res = cmd.run_command(["git", "add", "."])
        if not add_res.success:
            errmsg = f"Failed to stage changes: {add_res.stderr}"
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
        if not dry_run and commit_msg:
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
                logger.info(
                    f"Targeted test {current_test_file} passed after LLM fix; rerunning full suite"
                )
                current_test_file = None
                continue
            summary["success"] = True

            # Push changes to remote
            if not dry_run:
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
            if (
                isinstance(attempts_limit, (int, float))
                and math.isfinite(float(attempts_limit))
                and attempt >= int(attempts_limit)
            ):
                logger.info(
                    f"Reached attempt limit ({attempts_limit}); exiting fix loop"
                )
                break
        except Exception:
            # If attempts_limit is not a number, treat as unlimited
            pass

    # Final test after exhausting attempts (optional): do not re-run here because we already
    # executed a post-fix run within the loop. Keep messages concise.
    summary["messages"].append("Local tests still failing after attempts")

    return summary


def generate_commit_message_via_llm(
    llm_backend_manager: Optional["BackendManager"],
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
        manager = (
            message_backend_manager
            if message_backend_manager is not None
            else llm_backend_manager
        )

        if manager is None:
            return ""

        prompt = render_prompt("tests.commit_message")

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
                return content

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


def format_commit_message(
    config: AutomationConfig, llm_summary: str, attempt: int
) -> str:
    """Create a concise commit message using the LLM-produced summary.

    - Prefix with "Auto-Coder:" to unify automation commits
    - Sanitize dry-run prefixes and trim to a reasonable length
    - Fallback to a generic message if empty
    """
    base = (llm_summary or "").strip()
    # Remove any dry-run indicator if accidentally present
    if base.startswith("[DRY RUN]"):
        base = base[len("[DRY RUN]") :].strip()
    if not base:
        base = "Fix local tests"
    # Limit length to ~100 chars
    if len(base) > 100:
        base = base[:100].rstrip()
    return f"Auto-Coder: {base}"


@log_calls
def extract_important_errors(test_result: Dict[str, Any]) -> str:
    """Extract important error information from test output.

    改良点:
    - Playwright 形式の失敗ブロック（"Error:   1) [suite] › ... .spec.ts ..."）を優先的に広めのコンテキストで抽出
    - 期待/受領や該当 expect 行、"X failed" サマリを含めやすくする
    """
    if test_result["success"]:
        return ""

    errors = test_result.get("errors", "")
    output = test_result.get("output", "")

    # Combine stderr and stdout
    full_output = f"{errors}\n{output}".strip()

    if not full_output:
        return "Tests failed but no error output available"

    lines = full_output.split("\n")

    # 0) 期待/受領（Playwright/Jest）の詳細行が含まれていれば、見出しからその周辺を優先抽出
    if (
        ("Expected substring:" in full_output)
        or ("Received string:" in full_output)
        or ("expect(received)" in full_output)
    ):
        try:
            import re

            # 見出し候補を後方に向かって探す
            # Playwright 見出し: 先頭に "Error:" がないケースや、先頭空白/× 記号を許容
            hdr_pat = re.compile(r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\).*\.spec\.ts:.*")
            idx_expect = None
            for i, ln in enumerate(lines):
                if (
                    ("Expected substring:" in ln)
                    or ("Received string:" in ln)
                    or ("expect(received)" in ln)
                ):
                    idx_expect = i
                    break
            if idx_expect is not None:
                start = 0
                for j in range(idx_expect, -1, -1):
                    if hdr_pat.search(lines[j]):
                        start = j
                        break
                end = min(len(lines), idx_expect + 60)
                block = "\n".join(lines[start:end])
                if block:
                    return block
        except Exception:
            pass

    # 1) Playwright の典型パターンを優先抽出
    try:
        import re

        # 失敗見出し: "Error:   1) [suite] › e2e/... .spec.ts:line:col › ..."
        header_indices = []
        # Playwright 見出し: 先頭に "Error:" がない/ある両方、先頭空白や × 記号も許容
        header_regex = re.compile(
            r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+\[[^\]]+\]\s+\u203a\s+.*\.spec\.ts:\d+:\d+\s+\u203a\s+.*|"
            r"^(?:Error:\s+)?\s*(?:[×xX]\s*)?\d+\)\s+.*\.spec\.ts:.*",
            re.UNICODE,
        )
        for idx, ln in enumerate(lines):
            if header_regex.search(ln):
                header_indices.append(idx)
        # 期待/受領の典型
        expect_regex = re.compile(
            r"expect\(received\).*|Expected substring:|Received string:"
        )

        blocks = []
        for start_idx in header_indices:
            end_idx = min(len(lines), start_idx + 120)  # 広めに120行
            # 次のエラー見出しで打ち切り（空行では打ち切らない）
            for j in range(start_idx + 1, min(len(lines), start_idx + 300)):
                if j >= len(lines):
                    break
                s = lines[j]
                if header_regex.search(s):
                    end_idx = j
                    break
            block = lines[start_idx:end_idx]
            # 期待/受領や該当 expect 行が含まれているかチェック
            if any(expect_regex.search(b) for b in block) or any(
                ".spec.ts" in b for b in block
            ):
                blocks.append("\n".join(block))
        if blocks:
            result = "\n\n".join(blocks)
            # 期待/受領の行が含まれていなければ追補する
            if "Expected substring:" not in result or "Received string:" not in result:
                extra_lines = []
                for i, ln in enumerate(lines):
                    if (
                        "Expected substring:" in ln
                        or "Received string:" in ln
                        or "expect(received)" in ln
                    ):
                        start = max(0, i - 2)
                        end = min(len(lines), i + 4)
                        extra_lines.extend(lines[start:end])
                if extra_lines:
                    result = (
                        result
                        + "\n\n--- Expectation Details ---\n"
                        + "\n".join(extra_lines)
                    )
            if len(result) > 3000:
                result = result[:3000] + "\n... (output truncated)"
            return result
    except Exception:
        pass

    # 2) キーワードベースのフォールバック抽出（従来ロジックを改善）
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
            # もう少し広めに文脈を抽出
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

    return result if result else "Tests failed but no specific error information found"


def run_pr_tests(config: AutomationConfig, pr_data: Dict[str, Any]) -> Dict[str, Any]:
    """Run tests for a PR and return results."""
    pr_number = pr_data["number"]

    try:
        log_action(f"Running tests for PR #{pr_number}")
        result = run_local_tests(config)
        log_action(
            f"Test result for PR #{pr_number}: {'PASS' if result['success'] else 'FAIL'}"
        )
        return result

    except Exception as e:
        error_msg = f"Error running tests for PR #{pr_number}: {e}"
        logger.error(error_msg)
        return {"success": False, "output": "", "errors": error_msg, "return_code": -1}
