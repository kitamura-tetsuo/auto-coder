"""
PR processing functionality for Auto-Coder automation engine.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from auto_coder.backend_manager import BackendManager, get_llm_backend_manager, run_llm_prompt
from auto_coder.cli_helpers import create_high_score_backend_manager
from auto_coder.cloud_manager import CloudManager
from auto_coder.util.gh_cache import GitHubClient, ReviewThread, get_ghapi_client
from auto_coder.util.github_action import DetailedChecksResult, _check_github_actions_status, _get_github_actions_logs, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history

from .adversarial_validator import (
    AdversarialValidationResult,
    adversarial_validation_codex_feedback_marker,
    adversarial_validation_comment_marker,
    count_adversarial_validation_comments,
    format_adversarial_validation_comment,
    run_adversarial_validation,
)
from .attempt_manager import build_pr_attempt_trigger, get_current_attempt, increment_attempt
from .automation_config import AutomationConfig, EmptyPRResult, ProcessedPRResult, StaleJulesPRResult
from .branch_manager import BranchManager
from .conflict_resolver import _get_merge_conflict_info, resolve_merge_conflicts_with_llm, resolve_pr_merge_conflicts
from .fix_to_pass_tests_runner import run_local_tests
from .git_branch import branch_context, git_checkout_branch, git_commit_with_retry
from .git_commit import commit_and_push_changes, git_push, save_commit_failure_history
from .git_info import get_commit_log
from .github_app_reviewer import publish_adversarial_review, resolve_reviewer_app_identity
from .issue_context import extract_linked_issues_from_pr_body, get_linked_issues_context, resolve_issue_oracles, validate_issue_references
from .label_manager import LabelManager, LabelOperationError
from .llm_backend_config import get_pr_review_allowlist_from_config
from .logger_config import get_gh_logger, get_logger
from .pr_repair import build_existing_pr_repair_prompt, resolve_existing_pr_repair_target
from .progress_decorators import progress_stage
from .progress_footer import ProgressStage, newline_progress
from .prompt_loader import get_prompt_template, render_prompt
from .review_thread_validation import (
    ClaimedReviewThread,
    StaleReviewThreadRegistryError,
    StaleReviewThreadResolutionError,
    change_provenance_reply_fingerprint,
    classify_review_threads,
    is_change_provenance_thread,
    render_claimed_review_threads_section,
    resolve_addressed_review_threads,
    retry_pending_stale_review_thread_rollbacks,
)
from .reviewer_session_registry import ReviewerSessionRegistry
from .security_utils import redact_string
from .test_log_utils import extract_all_failed_tests, extract_first_failed_test, extract_important_errors
from .test_result import TestResult
from .trace_logger import get_trace_logger
from .util.github_action import _create_github_action_log_summary
from .utils import CommandExecutor, CommandResult, get_pr_author_login, log_action

logger = get_logger(__name__)
cmd = CommandExecutor()


def _remove_reviewer_sessions_for_closed_pr(repo_name: str, pr_number: int) -> None:
    """Best-effort removal of every backend's reviewer association for a closed PR."""
    try:
        ReviewerSessionRegistry().remove_pr(repo_name, pr_number)
    except OSError as exc:
        logger.warning(f"Failed to remove reviewer sessions for closed PR #{pr_number}: {exc}")


# Track active monitors to prevent duplicate execution within the same process
_active_monitors: set[int] = set()
_active_monitors_lock = threading.Lock()

CODEX_REVIEW_SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"
CODEX_REVIEW_BOT_LOGIN = "chatgpt-codex-connector[bot]"


@dataclass(frozen=True)
class CodexReviewState:
    """State of the latest Codex GitHub review summary for a pull request."""

    present: bool = False
    completed: bool = False
    lookup_error: Optional[str] = None


@dataclass(frozen=True)
class CodexCloudFeedbackResult:
    """Outcome of requesting CI-failure repair from an existing cloud task."""

    delivered: bool = False
    retryable: bool = False
    actions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AdversarialValidationEligibility:
    """Verified Issue-oracle eligibility for adversarial validation."""

    issue_numbers: Tuple[int, ...] = ()
    lookup_error: Optional[str] = None

    @property
    def is_applicable(self) -> bool:
        return bool(self.issue_numbers)


@dataclass(frozen=True)
class ReviewThreadGateState:
    """Tri-state review-thread result used by merge gates."""

    has_unresolved: bool = False
    lookup_error: Optional[str] = None


@dataclass(frozen=True)
class ClaimedReviewThreadGateState:
    """Unresolved-review-thread gate result that separates ordinary blockers
    from threads explicitly claimed as addressed by a supported automated
    reviewer's implementation agent (issue #1619, REQ-001/REQ-011)."""

    claimed: Tuple[Any, ...] = ()
    unresolved: Tuple[ReviewThread, ...] = ()
    blocking_unresolved: Tuple[ReviewThread, ...] = ()
    has_blocking_unresolved: bool = False
    lookup_error: Optional[str] = None


def _enforce_unresolved_provenance_gate(
    result: AdversarialValidationResult,
    claimed_review_threads: Sequence[ClaimedReviewThread],
    resolved_thread_ids: Sequence[str],
) -> Set[str]:
    """Prevent PASS while preserving the validator's concrete provenance result."""
    unresolved_ids = {thread.thread_id for thread in claimed_review_threads if thread.is_change_provenance} - set(resolved_thread_ids)
    if unresolved_ids and result.is_pass:
        result.result = "INCONCLUSIVE"
        result.summary = f"{result.summary.rstrip()} Change-provenance clarification remains unresolved after independent validation.".strip()
        result.diagnostic_category = "change_provenance_clarification"
        result.diagnostic_reason = f"Unresolved clarification thread(s): {', '.join(sorted(unresolved_ids))}"
    return unresolved_ids


def _resolve_eligible_review_thread_ids(repo_name: str) -> Set[int]:
    """Return configured stable identity IDs eligible for adjudication."""
    configured_ids = get_pr_review_allowlist_from_config(repo_name=repo_name)
    return set(configured_ids or [])


def _get_claimed_review_thread_state(github_client: Any, repo_name: str, pr_number: int) -> ClaimedReviewThreadGateState:
    """Fetch review threads and separate ordinary blockers from claimed threads.

    Reuses the existing ``_get_review_thread_gate_state`` boolean/lookup-error
    result so every call site that has no unresolved threads (or a lookup
    failure) behaves exactly as before. Only when at least one thread is
    unresolved does this additionally fetch full thread detail (comments) to
    determine whether every unresolved thread is an eligible, explicitly
    claimed automated-review thread. Any failure in that additional lookup
    fails closed to treating all unresolved threads as ordinary blockers.
    """
    gate = _get_review_thread_gate_state(github_client, repo_name, pr_number)
    if gate.lookup_error:
        return ClaimedReviewThreadGateState(lookup_error=gate.lookup_error)
    if not gate.has_unresolved:
        return ClaimedReviewThreadGateState()

    try:
        client = github_client or GitHubClient.get_instance()
        strict_getter = getattr(type(client), "get_pr_review_threads_strict", None)
        if not callable(strict_getter):
            # No detailed lookup available (e.g. a test double); fail closed
            # to the ordinary "unresolved threads block merge" behavior.
            return ClaimedReviewThreadGateState(has_blocking_unresolved=True)
        threads = client.get_pr_review_threads_strict(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed detailed review-thread lookup for PR #{pr_number}: {e}")
        return ClaimedReviewThreadGateState(has_blocking_unresolved=True)

    eligible_author_ids = _resolve_eligible_review_thread_ids(repo_name)
    classification = classify_review_threads(threads, eligible_author_ids)
    claimed_thread_ids = {thread.thread_id for thread in classification.claimed}
    return ClaimedReviewThreadGateState(
        claimed=tuple(classification.claimed),
        unresolved=tuple(thread for thread in threads if not thread.is_resolved),
        # Repair delegation must receive only ordinary blockers. Claimed-addressed
        # threads remain unresolved for independent validation, but asking the
        # implementation agent to repair them again violates that protocol.
        blocking_unresolved=tuple(thread for thread in threads if not thread.is_resolved and thread.id not in claimed_thread_ids),
        has_blocking_unresolved=classification.blocking_unresolved_count > 0,
    )


def _comment_value(comment: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a REST dictionary or a GhApi object."""
    return comment.get(key, default) if isinstance(comment, dict) else getattr(comment, key, default)


def _add_unique_pr_comment(github_client: Any, repo_name: str, pr_number: int, body: str) -> bool:
    """Post an informational comment only when the same body is not present."""
    comments = github_client.get_pr_comments(repo_name, pr_number)
    if isinstance(comments, list):
        normalized_body = body.strip()
        for comment in comments:
            existing_body = _comment_value(comment, "body", "")
            if isinstance(existing_body, str) and existing_body.strip() == normalized_body:
                logger.info(f"Skipped duplicate informational comment on PR #{pr_number}")
                return False
    github_client.add_comment_to_pr(repo_name, pr_number, body)
    return True


def _get_codex_review_state(github_client: Any, repo_name: str, pr_number: int) -> CodexReviewState:
    """Return Codex review presence/completion without tying it to a reviewed SHA."""
    try:
        strict_getter = getattr(type(github_client), "get_pr_comments_strict", None)
        if callable(strict_getter):
            comments = github_client.get_pr_comments_strict(repo_name, pr_number)
        else:
            comments = github_client.get_pr_comments(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed to inspect Codex review state for PR #{pr_number}: {e}")
        return CodexReviewState(lookup_error=str(e))

    for comment in reversed(comments):
        body = _comment_value(comment, "body", "")
        user = _comment_value(comment, "user") or {}
        login = _comment_value(user, "login", "")
        if login != CODEX_REVIEW_BOT_LOGIN or not isinstance(body, str) or CODEX_REVIEW_SUMMARY_MARKER not in body:
            continue

        code_review_row = next((line for line in body.splitlines() if "Code Review" in line and line.lstrip().startswith("|")), "")
        columns = [column.strip() for column in code_review_row.split("|")]
        status_column = columns[2] if len(columns) > 2 else ""
        return CodexReviewState(present=True, completed=bool(re.search(r"\bCompleted\b", status_column, re.IGNORECASE)))

    return CodexReviewState()


def _get_adversarial_validation_eligibility(github_client: Any, repo_name: str, pr_data: Dict[str, Any]) -> AdversarialValidationEligibility:
    """Use the same verified Issue-oracle resolver as validation context."""
    resolution = resolve_issue_oracles(github_client, repo_name, pr_data=pr_data)
    if resolution.error:
        logger.error(f"Failed to verify adversarial-validation eligibility: {resolution.error}")
        return AdversarialValidationEligibility(lookup_error=resolution.error)
    return AdversarialValidationEligibility(issue_numbers=tuple(issue.number for issue in resolution.issues))


def _run_async_monitor(repo_name: str, pr_number: int, head_sha: str, workflow_id: str) -> None:
    """Run the async monitor in a separate thread."""
    try:
        asyncio.run(monitor_workflow_async(repo_name, pr_number, head_sha, workflow_id))
    finally:
        with _active_monitors_lock:
            _active_monitors.discard(pr_number)
            logger.debug(f"Removed PR #{pr_number} from active monitors")


async def monitor_workflow_async(repo_name: str, pr_number: int, head_sha: str, workflow_id: str) -> None:
    """Monitor a triggered workflow asynchronously until completion.

    1. Wait for workflow run to appear.
    2. Wait for workflow run to complete.
    3. Update commit status.
    4. Remove @auto-coder label.
    """
    from auto_coder.label_manager import LabelManager
    from auto_coder.util.github_action import _check_github_actions_status

    from .jules_client import JulesClient

    logger.info(f"Started async monitor for PR #{pr_number} (workflow: {workflow_id})")

    github_client = GitHubClient.get_instance()
    config = AutomationConfig()

    # Create a dummy PR data for _check_github_actions_status
    pr_data = {
        "number": pr_number,
        "head": {"sha": head_sha},
    }

    try:
        # 1. Wait for workflow run to appear (max 1 minutes)
        run_found = False
        run_id = None

        for _ in range(12):  # 12 * 5s = 1 minutes
            status_result = _check_github_actions_status(repo_name, pr_data, config)
            if status_result.ids:
                run_found = True
                run_id = status_result.ids[0]  # Take the first one found
                logger.info(f"Found workflow run {run_id} for PR #{pr_number}")
                break
            await asyncio.sleep(5)

        if not run_found:
            logger.error(f"Timeout waiting for workflow run to appear for PR #{pr_number}")
            # Remove label so it can be retried? Or leave it?
            # User said: "workflow_dispatchでActionを起動前から、 Action完了まで、PRに対して @auto-coder ラベルを付加して多重実行を防止してください。"
            # If it fails to start, we should probably remove the label so it can be retried or handled manually.
            with LabelManager(github_client, repo_name, pr_number, item_type="pr", skip_label_add=True) as lm:
                lm.remove_label()
            return

        # 2. Wait for workflow run to complete (max 60 minutes)
        completed = False
        final_status = "failure"

        for _ in range(360):  # 360 * 10s = 60 minutes
            status_result = _check_github_actions_status(repo_name, pr_data, config)

            if not status_result.in_progress:
                completed = True
                final_status = "success" if status_result.success else "failure"
                logger.info(f"Workflow run {run_id} completed with status: {final_status}")
                break

            await asyncio.sleep(10)

        if not completed:
            logger.error(f"Timeout waiting for workflow run {run_id} to complete for PR #{pr_number}")
            final_status = "error"  # Timeout treated as error

        # 3. Update commit status
        # Map our status to GitHub commit status state (pending, success, error, failure)
        # final_status is already success/failure/error
        commit_status_state = final_status

        target_url = f"https://github.com/{repo_name}/actions/runs/{run_id}" if run_id else ""
        description = f"Workflow {workflow_id} {final_status}"

        try:
            github_client.create_commit_status(repo_name=repo_name, sha=head_sha, state=commit_status_state, target_url=target_url, description=description, context=f"auto-coder/{workflow_id}")
        except Exception as e:
            logger.error(f"Failed to update commit status for PR #{pr_number}: {e}")

        # 4. Remove @auto-coder label
        try:
            with LabelManager(github_client, repo_name, pr_number, item_type="pr", skip_label_add=True) as lm:
                lm.remove_label()
            logger.info(f"Removed @auto-coder label from PR #{pr_number}")
        except Exception as e:
            logger.error(f"Failed to remove label from PR #{pr_number}: {e}")

    except Exception as e:
        logger.error(f"Error in async monitor for PR #{pr_number}: {e}")
        # Ensure label is removed on error to avoid sticking
        try:
            with LabelManager(github_client, repo_name, pr_number, item_type="pr", skip_label_add=True) as lm:
                lm.remove_label()
        except Exception:
            pass


def has_unresolved_review_threads(
    github_client: Any,
    repo_name: str,
    pr_number: int,
) -> bool:
    """Check if a pull request has unresolved review threads.

    Args:
        github_client: GitHub client instance or None
        repo_name: Repository name (owner/repo)
        pr_number: Pull request number

    Returns:
        True if at least one review thread is unresolved, False otherwise.
    """
    try:
        client = github_client or GitHubClient.get_instance()
        if hasattr(client, "has_unresolved_review_threads"):
            res = client.has_unresolved_review_threads(repo_name, pr_number)
            if isinstance(res, bool):
                return res
            if isinstance(res, (list, tuple)):
                return any(not getattr(t, "is_resolved", False) for t in res)
            if res is True:
                return True
            return False
        elif hasattr(client, "get_pr_review_threads"):
            threads = client.get_pr_review_threads(repo_name, pr_number)
            if isinstance(threads, (list, tuple)):
                return any(not getattr(t, "is_resolved", False) for t in threads)
            return False
        return False
    except Exception as e:
        logger.error(f"Error checking unresolved review threads for PR #{pr_number}: {e}")
        return False


def _get_review_thread_gate_state(github_client: Any, repo_name: str, pr_number: int) -> ReviewThreadGateState:
    """Fetch review threads strictly in production so lookup errors fail closed."""
    try:
        client = github_client or GitHubClient.get_instance()
        strict_getter = getattr(type(client), "get_pr_review_threads_strict", None)
        if callable(strict_getter):
            threads = client.get_pr_review_threads_strict(repo_name, pr_number)
            return ReviewThreadGateState(has_unresolved=any(not thread.is_resolved for thread in threads))
        return ReviewThreadGateState(has_unresolved=has_unresolved_review_threads(client, repo_name, pr_number))
    except Exception as e:
        logger.error(f"Failed strict review-thread lookup for PR #{pr_number}: {e}")
        return ReviewThreadGateState(lookup_error=str(e))


def process_pull_request(
    github_client: Any,
    config: AutomationConfig,
    repo_name: str,
    pr_data: Dict[str, Any],
) -> ProcessedPRResult:
    """Process a single pull request with priority order."""
    try:
        processed_pr = ProcessedPRResult(
            pr_data=pr_data,
            actions_taken=[],
            priority=None,
            analysis=None,
        )

        pr_number = pr_data["number"]

        # Close PRs with zero effective diff before any further processing.
        # This runs before the @auto-coder label check so empty PRs left from earlier
        # runs are closed and their source issues retried immediately.
        empty_pr_result = _close_empty_pr(github_client, repo_name, pr_data, config)
        if empty_pr_result.closed:
            processed_pr.actions_taken = empty_pr_result.actions
            processed_pr.priority = "close"
            return processed_pr

        # Close Jules PRs that could not get CI green within the configured timeout.
        # This runs before the @auto-coder label check on purpose: a stale Jules PR
        # usually still carries the label from an earlier run, and skipping on the
        # label would leave the PR open forever.
        stale_jules_result = _close_stale_jules_pr(github_client, repo_name, pr_data, config)
        if stale_jules_result.closed:
            processed_pr.actions_taken = stale_jules_result.actions
            processed_pr.priority = "close"
            return processed_pr

        # Skip immediately if PR already has @auto-coder label
        with LabelManager(
            github_client,
            repo_name,
            pr_number,
            item_type="pr",
            skip_label_add=True,
            check_labels=config.CHECK_LABELS,
            known_labels=pr_data.get("labels"),
        ) as should_process:
            if not should_process:
                logger.info(f"Skipping PR #{pr_number} - already has @auto-coder label")
                get_trace_logger().log("PR Processing", f"Skipping PR #{pr_number} - already processed", item_type="pr", item_number=pr_number, details={"skip_reason": "already_processed"})
                processed_pr.actions_taken = ["Skipped - already being processed (@auto-coder label present)"]
                return processed_pr

        # Check if we should skip this PR because it's waiting for Jules
        if _should_skip_waiting_for_jules(github_client, repo_name, pr_data, config):
            logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
            get_trace_logger().log("PR Processing", f"Skipping PR #{pr_number} - waiting for Jules", item_type="pr", item_number=pr_number, details={"skip_reason": "waiting_for_jules"})
            processed_pr.actions_taken = ["Skipped - waiting for Jules to fix CI failures"]
            return processed_pr

        # Process Jules PRs to detect session IDs and update PR body
        try:
            jules_success = _link_jules_pr_to_issue(repo_name, pr_data, github_client)
            if jules_success:
                logger.info(f"Successfully processed Jules PR #{pr_number} (or not a Jules PR)")
                get_trace_logger().log("Jules Link", f"Linked Jules PR #{pr_number}", item_type="pr", item_number=pr_number, details={"success": True})
            else:
                logger.warning(f"Failed to process Jules PR #{pr_number}, but continuing with normal processing")
                get_trace_logger().log("Jules Link", f"Failed to link Jules PR #{pr_number}", item_type="pr", item_number=pr_number, details={"success": False})
        except Exception as e:
            logger.error(f"Error in Jules PR processing for PR #{pr_number}: {e}")
            # Continue with normal processing even if Jules processing fails

        # Process Codex Cloud PRs to append Codex Cloud URL if linked issue was processed by Codex Cloud
        try:
            _link_codex_cloud_pr_to_issue(repo_name, pr_data, github_client)
        except Exception as e:
            logger.error(f"Error in Codex Cloud PR processing for PR #{pr_number}: {e}")

        # Check if we should skip this PR because it's waiting for Jules
        if _should_skip_waiting_for_jules(github_client, repo_name, pr_data, config):
            logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
            get_trace_logger().log("PR Processing", f"Skipping PR #{pr_number} - waiting for Jules", item_type="pr", item_number=pr_number, details={"skip_reason": "waiting_for_jules"})
            processed_pr.actions_taken = ["Skipped - waiting for Jules to fix CI failures"]
            return processed_pr

        # Extract PR information
        branch_name = pr_data.get("head", {}).get("ref")
        pr_body = pr_data.get("body", "")
        related_issues = []
        if pr_body:
            # Extract linked issues from PR body
            related_issues = extract_linked_issues_from_pr_body(pr_body)

        with ProgressStage(
            "PR",
            pr_number,
            "Processing",
            related_issues=related_issues,
            branch_name=branch_name,
        ):
            try:
                get_trace_logger().log("PR Processing", f"Processing PR #{pr_number}", item_type="pr", item_number=pr_number, details={"branch": branch_name})

                # Check GitHub Actions status and mergeability
                github_checks = _check_github_actions_status(repo_name, pr_data, config)

                get_trace_logger().log("CI Status", f"CI Status for PR #{pr_number}: {'Success' if github_checks.success else 'Failure/Pending'}", item_type="pr", item_number=pr_number, details={"success": github_checks.success, "in_progress": github_checks.in_progress})

                mergeable = pr_data.get("mergeable", True)
                get_trace_logger().log("Merge Check", f"Mergeable status for PR #{pr_number}: {mergeable}", item_type="pr", item_number=pr_number, details={"mergeable": mergeable})

                # Always use _take_pr_actions for unified processing
                # This ensures tests that mock _take_pr_actions continue to work
                logger.info(f"PR #{pr_number}: Processing for issue resolution and merge")
                processed_pr.priority = "fix"

                # Process using _take_pr_actions
                processed_pr_result = _process_pr_for_fixes(github_client, repo_name, pr_data, config)
                processed_pr.actions_taken = processed_pr_result.actions_taken
                processed_pr.priority = processed_pr_result.priority
                processed_pr.analysis = processed_pr_result.analysis
                # Copy error if it was set
                if processed_pr_result.error:
                    processed_pr.error = processed_pr_result.error

                get_trace_logger().log("Decision", f"Finished processing PR #{pr_number}", item_type="pr", item_number=pr_number, details={"actions_taken": processed_pr.actions_taken, "error": processed_pr.error})

            finally:
                # Clear progress header after processing
                newline_progress()

        return processed_pr

    except Exception as e:
        pr_number = pr_data.get("number", "unknown")
        logger.error(f"Failed to process PR #{pr_number}: {e}")
        get_trace_logger().log("Error", f"Exception processing PR #{pr_number}: {e}", item_type="pr", item_number=pr_number, details={"error": str(e)})  # type: ignore
        return ProcessedPRResult(
            pr_data=pr_data,
            actions_taken=[f"Error processing PR: {str(e)}"],
            priority="error",
            analysis=None,
        )


def _is_dependabot_pr(pr_obj: Any) -> bool:
    """Return True if the PR is authored by a dependency bot.

    Dependency bots include Dependabot, Renovate, and accounts whose login
    ends with '[bot]' when IGNORE_DEPENDABOT_PRS is enabled.
    """
    try:
        login = get_pr_author_login(pr_obj)
        if not login:
            return False
        login_lower = login.lower()
        if "google-labs-jules[bot]" in login_lower:
            return False
        if "dependabot" in login_lower or "renovate" in login_lower or login_lower.endswith("[bot]"):
            return True
    except Exception:
        # Best-effort detection only; never fail hard here
        return False
    return False


def _should_skip_waiting_for_jules(github_client: Any, repo_name: str, pr_data: Dict[str, Any], config: Optional[AutomationConfig] = None) -> bool:
    """Check if PR should be skipped because it's waiting for Jules to fix CI failures.

    Returns True if:
    1. The last comment on the PR is the specific "CI checks failed..." message from Auto-Coder.
    2. There are no commits after that comment.
    3. The wait has not exceeded ``config.JULES_WAIT_TIMEOUT_HOURS``.
    """
    if _is_codex_or_claude_pr(pr_data):
        return False

    wait_timeout_hours = (config or AutomationConfig()).JULES_WAIT_TIMEOUT_HOURS
    try:
        pr_number = pr_data["number"]

        # Check Jules session status
        try:
            # Extract session ID from PR body
            pr_body = pr_data.get("body", "")
            session_id = _extract_session_id_from_pr_body(pr_body)

            if session_id:
                from auto_coder.jules_client import JulesClient

                jules_client = JulesClient()
                # Get specific session directly
                try:
                    target_session = jules_client.get_session(session_id)
                except Exception:
                    # If get_session fails (e.g. 404), treat as not found/no session
                    target_session = None

                if target_session:
                    from auto_coder.jules_engine import get_session_pull_request

                    state = target_session.get("state")
                    pull_request = get_session_pull_request(target_session)

                    if state == "COMPLETED" and pull_request:
                        # Extract PR info
                        pull_request_url = pull_request["url"]
                        parts = pull_request_url.split("/")
                        pull_idx = parts.index("pull")
                        jules_repo_name = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                        try:
                            jules_pr_number = int(parts[pull_idx + 1])
                        except ValueError:
                            pass

                        if jules_repo_name and jules_pr_number and jules_pr_number != pr_number:
                            # Check PR status
                            jules_pr = github_client.get_pull_request(jules_repo_name, jules_pr_number)
                            if jules_pr.get("state") == "closed":
                                logger.info(f"Jules session {session_id} PR #{jules_pr_number} is closed. Resuming processing.")
                            else:
                                logger.info(f"Jules session {session_id} PR #{jules_pr_number} is open. Waiting...")
                                return True

                    else:
                        logger.info(f"Jules session {session_id} for PR #{pr_number} found (State: {state}). Waiting...")
                        return True

        except Exception as e:
            logger.warning(f"Failed to check Jules session for PR #{pr_number}: {e}")

        # Get comments
        comments = github_client.get_pr_comments(repo_name, pr_number)
        if not comments:
            return False

        # Sort comments by date (newest last) just to be safe, though API usually returns them sorted
        comments.sort(key=lambda x: x["created_at"])

        last_comment = comments[-1]
        last_comment_body = last_comment.get("body", "")

        # Check if last comment is the specific message
        target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
        if target_message not in last_comment_body:
            return False

        # Get last comment timestamp
        last_comment_time = last_comment["created_at"]

        # Get commits
        commits = github_client.get_pr_commits(repo_name, pr_number)
        if not commits:
            # If no commits found (unlikely for a PR), assume we shouldn't skip
            return False

        # Sort commits by date (newest last)
        commits.sort(key=lambda x: x["commit"]["committer"]["date"])

        last_commit = commits[-1]
        last_commit_time = last_commit["commit"]["committer"]["date"]

        # Compare timestamps
        # ISO format strings can be compared lexicographically if they are in the same timezone (usually UTC from GitHub)
        if last_commit_time > last_comment_time:
            logger.info(f"PR #{pr_number} has new commits after Jules wait message, processing...")
            return False

        # Check if it has been waiting longer than the configured timeout
        try:
            # Parse GitHub timestamp (ISO 8601)
            # Example: 2023-10-27T10:00:00Z
            last_comment_dt = datetime.fromisoformat(last_comment_time.replace("Z", "+00:00"))
            current_time = datetime.now(timezone.utc)

            if current_time - last_comment_dt > timedelta(hours=wait_timeout_hours):
                logger.info(f"PR #{pr_number} has been waiting for Jules for > {wait_timeout_hours} hour(s). Re-processing.")
                return False
        except Exception as e:
            logger.warning(f"Failed to parse timestamp or compare time for PR #{pr_number}: {e}")

        logger.info(f"PR #{pr_number} is waiting for Jules (last comment is wait message, no new commits)")
        return True

    except Exception as e:
        logger.error(f"Error checking if PR #{pr_data.get('number')} should be skipped: {e}")
        return False


def _is_empty_pr(
    pr_data: Dict[str, Any],
    repo_name: Optional[str] = None,
    github_client: Optional[Any] = None,
) -> bool:
    """Check if a pull request has no effective diff against its base branch.

    Do not use commit count as the emptiness check, as an empty PR may still contain commits.

    Args:
        pr_data: Pull request data dictionary
        repo_name: Optional repository name for fetching diff if changed_files not in pr_data
        github_client: Optional GitHub client for fetching diff if changed_files not in pr_data

    Returns:
        True if the PR has zero effective diff, False otherwise
    """
    changed_files = pr_data.get("changed_files")
    if changed_files is not None and isinstance(changed_files, int):
        return changed_files == 0

    additions = pr_data.get("additions")
    deletions = pr_data.get("deletions")
    if isinstance(additions, int) and isinstance(deletions, int):
        if additions > 0 or deletions > 0:
            return False
        if additions == 0 and deletions == 0:
            return True

    if repo_name and github_client:
        pr_number = pr_data.get("number")
        if pr_number is not None:
            try:
                diff = github_client.get_pr_diff(repo_name, pr_number)
                if isinstance(diff, str):
                    return len(diff.strip()) == 0
            except Exception as e:
                logger.debug(f"Failed to fetch PR diff for empty check on #{pr_number}: {e}")

    return False


def _resolve_pr_issue_numbers(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> List[int]:
    """Resolve associated source issue numbers for a PR using body, session ID, branch, or title.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        List of unique issue numbers associated with the PR
    """
    body = pr_data.get("body", "") or ""
    issue_numbers = extract_linked_issues_from_pr_body(body)
    if issue_numbers:
        return issue_numbers

    # Try resolving via session ID, branch name, or title
    resolved_issue = _resolve_jules_pr_issue_number(repo_name, pr_data, github_client)
    if resolved_issue:
        return [resolved_issue]

    # Additional generic fallback: check branch name for patterns like "issue-123" or "fix-123"
    branch_name = ""
    if isinstance(pr_data.get("head"), dict):
        branch_name = pr_data.get("head", {}).get("ref", "")
    if not branch_name:
        branch_name = pr_data.get("head_branch", "") or ""
    if branch_name:
        match = re.search(r"\b(?:issue|fix)[-_](\d+)\b", branch_name, re.IGNORECASE)
        if match:
            return [int(match.group(1))]

    # Additional generic fallback: check PR title
    pr_title = pr_data.get("title", "") or ""
    if pr_title:
        match = re.search(r"(?:issue|fix|close|resolve)s?\s*#(\d+)", pr_title, re.IGNORECASE)
        if match:
            return [int(match.group(1))]

    return []


def _is_cloud_run_retry_blocked(
    repo_name: str,
    issue_number: int,
    pr_number: int,
    reason: str,
) -> bool:
    """Return True if a durable `CloudRun` policy forbids a new attempt.

    Looks up the `CloudRun` (if any) persisted for the issue's current
    attempt and, when one exists, associates `pr_number` with it and asks
    its provider policy whether `reason` authorizes a new attempt. Issues
    with no persisted `CloudRun` (e.g. Jules, or any other path not yet
    migrated to this lifecycle) are unaffected and always return False so
    their existing behavior is preserved (see issue #1607, REQ-006/REQ-007).

    Centralizing this lookup keeps provider-name branching out of PR
    processing call sites (REQ-008): the actual retry decision comes from
    `cloud_run_policies.get_policy_for_provider()`.
    """
    try:
        from .cloud_run import CloudRunEvent, CloudRunRepository
        from .cloud_run_policies import get_policy_for_provider

        attempt = get_current_attempt(repo_name, issue_number)
        cloud_run_repo = CloudRunRepository(repo_name)
        run = cloud_run_repo.get(issue_number, attempt)
        if run is None:
            return False

        policy = get_policy_for_provider(run.provider)
        if policy is None:
            return False

        # Preserve this PR's association with the run without disturbing any
        # other PR already associated with it.
        cloud_run_repo.add_pull_request(issue_number, attempt, pr_number)

        event = CloudRunEvent(run=run, reason=reason, proposed_attempt=attempt + 1)
        return not policy.allow_new_attempt(event)
    except Exception as e:
        logger.error(f"Failed to evaluate CloudRun retry policy for issue #{issue_number}: {e}")
        return False


def _close_empty_pr(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> EmptyPRResult:
    """Close a PR that has no effective diff against the base branch and requeue its source issue.

    When an empty PR is detected:
    1. Skip normal review/merge/LLM processing for that PR.
    2. Close the empty PR on GitHub.
    3. Resolve the source issue(s) using existing PR-to-Issue association logic.
    4. Reopen the source issue(s) if closed.
    5. Increment the source issue's attempt counter.
    6. Remove the @auto-coder label so the issue can be processed again.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        config: Automation configuration

    Returns:
        EmptyPRResult; ``closed`` is True if the PR was closed due to having no diff.
    """
    result = EmptyPRResult()
    pr_number = pr_data.get("number")
    if pr_number is None:
        return result
    pr_number = int(pr_number)

    try:
        if pr_data.get("state") == "closed":
            _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
            logger.debug(f"PR #{pr_number} is already closed, skipping empty PR check")
            return result

        if not _is_empty_pr(pr_data, repo_name=repo_name, github_client=github_client):
            return result

        logger.info(f"PR #{pr_number} has zero effective diff against base branch. Closing it.")

        # Resolve the issue(s) that this PR was created for
        issue_numbers = _resolve_pr_issue_numbers(repo_name, pr_data, github_client)

        close_comment = f"Auto-Coder: Closing PR #{pr_number} because it has no effective diff against the base branch. " "The linked issue(s) will be retried with an incremented attempt count."
        client = github_client or GitHubClient.get_instance()
        client.close_pr(repo_name, pr_number, close_comment)
        _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
        result.closed = True
        result.actions.append(f"Closed empty PR #{pr_number} (zero effective diff)")
        get_trace_logger().log(
            "Empty PR",
            f"Closed empty PR #{pr_number}",
            item_type="pr",
            item_number=pr_number,
            details={"linked_issues": issue_numbers},
        )

        if not issue_numbers:
            logger.warning(f"No linked issue found for closed empty PR #{pr_number}, cannot increment attempt")
            result.actions.append(f"No linked issue found for empty PR #{pr_number} to increment attempt")
            return result

        for issue_number in issue_numbers:
            if _is_cloud_run_retry_blocked(repo_name, issue_number, pr_number, reason="empty_pr"):
                # The PR belongs to a durable CloudRun (e.g. Codex Cloud) whose
                # policy does not authorize an automatic new attempt from an
                # empty PR alone. Closing the PR must stay a PR-local action:
                # the Issue attempt is not incremented, the @auto-coder label
                # is not released, and no replacement task is enqueued (see
                # issue #1607).
                result.actions.append(f"Preserved issue #{issue_number} at its current attempt (CloudRun manual-only retry policy; empty PR #{pr_number} closed)")
                continue

            # Reopen the source issue if it was closed
            try:
                issue_obj = client.get_issue(repo_name, issue_number)
                if issue_obj:
                    state = issue_obj.get("state") if isinstance(issue_obj, dict) else getattr(issue_obj, "state", None)
                    if state == "closed":
                        logger.info(f"Reopening closed issue #{issue_number} due to empty PR #{pr_number}")
                        reopen_comment = f"Auto-Coder: Reopening issue #{issue_number} because PR #{pr_number} had no effective diff."
                        client.reopen_issue(repo_name, issue_number, reopen_comment)
                        result.actions.append(f"Reopened closed issue #{issue_number}")
            except Exception as e:
                logger.error(f"Failed to check/reopen issue #{issue_number}: {e}")

            try:
                new_attempt = increment_attempt(repo_name, issue_number)
                result.actions.append(f"Incremented attempt for issue #{issue_number} to {new_attempt}")
            except Exception as e:
                logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                result.actions.append(f"Failed to increment attempt for issue #{issue_number}: {e}")

            # Release the @auto-coder label so the issue can be picked up for the next attempt
            if _release_issue_processing_label(github_client, repo_name, issue_number, config):
                result.actions.append(f"Removed {config.AUTO_CODER_LABEL} label from issue #{issue_number}")

            result.issue_numbers.append(issue_number)

    except Exception as e:
        logger.error(f"Error handling empty PR #{pr_number}: {e}")

    return result


def _close_stale_jules_pr(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_checks: Optional[Any] = None,
) -> StaleJulesPRResult:
    """Close a Jules PR that failed to get CI green within the configured timeout.

    Jules is the only actor allowed to push to its own PR branch, so a Jules PR that
    still has failing CI after ``config.JULES_PR_CI_TIMEOUT_HOURS`` is considered
    unfixable. The PR is closed, the attempt count of the linked issue(s) is
    incremented, and the ``@auto-coder`` label that the dead Jules run left on those
    issues is removed so they can be picked up again from scratch.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        config: Automation configuration
        github_checks: Optional already-fetched GitHub Actions status result

    Returns:
        StaleJulesPRResult; ``closed`` is False when the PR was left open.
    """
    result = StaleJulesPRResult()
    pr_number = int(pr_data["number"])

    try:
        from .llm_backend_config import is_jules_mode_enabled

        if not is_jules_mode_enabled():
            return result

        if not _is_jules_pr(pr_data):
            return result

        if pr_data.get("state") == "closed":
            _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
            logger.debug(f"PR #{pr_number} is already closed, skipping Jules staleness check")
            return result

        created_at = pr_data.get("created_at")
        if not created_at:
            logger.debug(f"PR #{pr_number} has no created_at timestamp, skipping Jules staleness check")
            return result

        try:
            created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError as e:
            logger.warning(f"Failed to parse created_at '{created_at}' for PR #{pr_number}: {e}")
            return result

        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)

        age = datetime.now(timezone.utc) - created_dt
        timeout = timedelta(hours=config.JULES_PR_CI_TIMEOUT_HOURS)
        if age <= timeout:
            return result

        # Only close when CI is actually not passing. Completed runs are required:
        # a run still in progress may yet turn green.
        if github_checks is None:
            github_checks = _check_github_actions_status(repo_name, pr_data, config)

        if github_checks.success:
            logger.info(f"Jules PR #{pr_number} is older than {config.JULES_PR_CI_TIMEOUT_HOURS}h but CI passed, keeping it open")
            return result

        if getattr(github_checks, "in_progress", False):
            logger.info(f"Jules PR #{pr_number} is older than {config.JULES_PR_CI_TIMEOUT_HOURS}h but CI is still running, keeping it open")
            return result

        logger.info(f"Jules PR #{pr_number} did not pass CI within {config.JULES_PR_CI_TIMEOUT_HOURS} hours. Closing it.")

        # Resolve the issue(s) that this PR was created for
        issue_numbers = extract_linked_issues_from_pr_body(pr_data.get("body", "") or "")
        if not issue_numbers:
            resolved_issue = _resolve_jules_pr_issue_number(repo_name, pr_data, github_client)
            if resolved_issue:
                issue_numbers = [resolved_issue]

        close_comment = f"Auto-Coder: Closing this PR because Jules did not get CI to pass within {config.JULES_PR_CI_TIMEOUT_HOURS} hours after the PR was created. The linked issue(s) will be retried with an incremented attempt count."
        client = github_client or GitHubClient.get_instance()
        client.close_pr(repo_name, pr_number, close_comment)
        _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
        result.closed = True
        result.actions.append(f"Closed stale Jules PR #{pr_number} (no passing CI within {config.JULES_PR_CI_TIMEOUT_HOURS}h)")
        get_trace_logger().log(
            "Jules Timeout",
            f"Closed stale Jules PR #{pr_number}",
            item_type="pr",
            item_number=pr_number,
            details={"timeout_hours": config.JULES_PR_CI_TIMEOUT_HOURS, "linked_issues": issue_numbers},
        )

        if not issue_numbers:
            logger.warning(f"No linked issue found for closed Jules PR #{pr_number}, cannot increment attempt")
            result.actions.append(f"No linked issue found for PR #{pr_number} to increment attempt")
            return result

        for issue_number in issue_numbers:
            try:
                new_attempt = increment_attempt(repo_name, issue_number)
                result.actions.append(f"Incremented attempt for issue #{issue_number} to {new_attempt}")
            except Exception as e:
                logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                result.actions.append(f"Failed to increment attempt for issue #{issue_number}: {e}")

            # The dead Jules run kept the @auto-coder label on the issue to mark it as
            # being worked on. Release it, otherwise the issue is never processed again.
            if _release_issue_processing_label(github_client, repo_name, issue_number, config):
                result.actions.append(f"Removed {config.AUTO_CODER_LABEL} label from issue #{issue_number}")

            result.issue_numbers.append(issue_number)

    except Exception as e:
        logger.error(f"Error handling stale Jules PR #{pr_number}: {e}")

    return result


def _release_issue_processing_label(
    github_client: Any,
    repo_name: str,
    issue_number: int,
    config: AutomationConfig,
) -> bool:
    """Remove the @auto-coder label an aborted run left behind on an issue.

    Args:
        github_client: GitHub client instance
        repo_name: Repository name (owner/repo)
        issue_number: Issue to unlock
        config: Automation configuration

    Returns:
        True if the label was removed, False otherwise
    """
    label = config.AUTO_CODER_LABEL
    if not label or config.DISABLE_LABELS:
        return False

    try:
        client = github_client or GitHubClient.get_instance()
        client.remove_labels(repo_name, issue_number, [label], item_type="issue")
        logger.info(f"Removed '{label}' label from issue #{issue_number} so it can be processed again")
        return True
    except Exception as e:
        logger.warning(f"Failed to remove '{label}' label from issue #{issue_number}: {e}")
        return False


def _get_mergeable_state(
    repo_name: str,
    pr_data: Dict[str, Any],
    _config: AutomationConfig,
) -> Dict[str, Optional[Any]]:
    """Get latest mergeable state using existing data with optional refresh."""
    mergeable = pr_data.get("mergeable")
    merge_state_status = pr_data.get("mergeStateStatus")

    # Refresh mergeability only when value is unknown
    if mergeable is None:
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            # API: api.pulls.get(owner, repo, pull_number)
            pr_details = api.pulls.get(owner, repo, pull_number=pr_data.get("number"))
            mergeable = pr_details.get("mergeable", mergeable)
            merge_state_status = pr_details.get("mergeStateStatus", merge_state_status)
        except Exception as e:
            logger.debug(f"Unable to refresh mergeable state for PR #{pr_data.get('number')}: {e}")

    return {"mergeable": mergeable, "merge_state_status": merge_state_status}


def _start_mergeability_remediation(pr_number: int, merge_state_status: Optional[str], repo_name: str = "") -> List[str]:
    """Implement mergeability remediation flow for non-mergeable PRs.

    This function handles the end-to-end flow for non-mergeable PRs:
    1. Get PR details and determine the base branch
    2. Checkout the PR branch
    3. Update from the base branch
    4. Resolve conflicts using existing helpers (including package-lock handling)
    5. Push the updated branch
    6. Mark PR as processed once push succeeds (via ACTION_FLAG:SKIP_ANALYSIS)

    Args:
        pr_number: PR number
        merge_state_status: Current merge state status from GitHub

    Returns:
        List of action strings describing what was done
    """
    actions = []
    state_text = merge_state_status or "unknown"

    try:
        log_action(f"Starting mergeability remediation for PR #{pr_number} (state: {state_text})")
        actions.append(f"Starting mergeability remediation for PR #{pr_number} (state: {state_text})")
        get_trace_logger().log("Remediation", f"Starting remediation for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"state": state_text})

        # Step 1: Get PR details to determine the base branch and head branch
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            pr_details = api.pulls.get(owner, repo, pull_number=pr_number)
            base_branch = pr_details.get("base", {}).get("ref", "main")
            head_branch = pr_details.get("head", {}).get("ref")

            if not head_branch:
                error_msg = f"Failed to determine head branch for PR #{pr_number} (head.ref is missing)"
                actions.append(error_msg)
                log_action(error_msg, False)
                return actions

        except Exception as e:
            error_msg = f"Failed to get PR #{pr_number} details via GhApi: {e}"
            actions.append(error_msg)
            log_action(error_msg, False)
            return actions

        actions.append(f"Determined base branch: {base_branch}, head branch: {head_branch} for PR #{pr_number}")

        # Step 2: Ensure PR branch exists and is up to date, then use BranchManager
        # Create minimal PR data for checkout function
        pr_branch_name = head_branch
        pr_data_for_checkout = {"number": pr_number, "head": {"ref": pr_branch_name}}

        # Ensure branch exists and is fetched, but don't switch yet
        prepare_success = _checkout_pr_branch("", pr_data_for_checkout, AutomationConfig(), perform_checkout=False)

        if not prepare_success:
            error_msg = f"Failed to prepare PR #{pr_number} branch ({pr_branch_name})"
            actions.append(error_msg)
            log_action(error_msg, False)
            return actions

        with BranchManager(pr_branch_name) as manager:
            actions.append(f"Checked out PR #{pr_number} branch")

            # Step 3: Update from base branch with conflict resolution
            # The _update_with_base_branch function includes:
            # - Fetching latest changes
            # - Merging base branch
            # - Using _perform_base_branch_merge_and_conflict_resolution for conflicts
            # - Pushing updated branch with retry
            get_trace_logger().log("Remediation", f"Updating base branch for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"step": "update_base"})
            update_actions = _update_with_base_branch(repo_name, {"number": pr_number, "base_branch": base_branch}, AutomationConfig())
            actions.extend(update_actions)

            # Step 4: Check for degrading merge detection
        if "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE" in update_actions:
            # LLM determined merge would degrade code quality
            # The _trigger_fallback_for_conflict_failure has already been called in conflict_resolver
            # The linked issues have been reopened and attempt incremented
            # Now we need to close the PR
            try:
                get_trace_logger().log("Remediation", f"Degrading merge detected for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "degrading"})
                client = GitHubClient.get_instance()
                close_comment = "Auto-Coder: Closing PR because LLM determined merge would degrade code quality. The linked issue(s) have been reopened with incremented attempt count."
                client.close_pr(repo_name, pr_number, close_comment)
                _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
                actions.append(f"Closed PR #{pr_number} without merging due to quality degradation risk")

                # Checkout main branch after closing PR
                main_branch = AutomationConfig().MAIN_BRANCH
                checkout_result = cmd.run_command(["git", "checkout", main_branch])
                if checkout_result.success:
                    actions.append(f"Checked out {main_branch} branch")
                else:
                    logger.warning(f"Failed to checkout {main_branch} branch: {checkout_result.stderr}")
                    actions.append(f"Warning: Failed to checkout {main_branch} branch")
            except Exception as e:
                logger.error(f"Failed to close PR #{pr_number}: {e}")
                actions.append(f"Error closing PR #{pr_number}: {e}")
            return actions

        # Step 5: Verify successful remediation
        # If push succeeded, the action flag will be set
        if "ACTION_FLAG:SKIP_ANALYSIS" in update_actions or any("Pushed updated branch" in action for action in update_actions):
            actions.append(f"Mergeability remediation completed for PR #{pr_number}")
            actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            get_trace_logger().log("Remediation", f"Remediation success for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "success"})
        elif "Failed" in str(update_actions):
            # Remediation attempted but failed
            actions.append(f"Mergeability remediation failed for PR #{pr_number}")
            get_trace_logger().log("Remediation", f"Remediation failed for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "failed"})

    except Exception as e:
        error_msg = f"Error during mergeability remediation for PR #{pr_number}: {str(e)}"
        logger.error(error_msg)
        actions.append(error_msg)
        log_action(error_msg, False)

    return actions


def _process_pr_for_merge(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> ProcessedPRResult:
    """Process a passing PR through the shared merge transition workflow.

    This entry point intentionally delegates the decision to ``_handle_pr_merge``
    instead of maintaining a second unresolved-thread gate. In particular, a
    claimed-addressed thread must enter independent adversarial validation here
    exactly as it does during batch processing.
    """
    processed_pr = ProcessedPRResult(
        pr_data=pr_data,
        actions_taken=[],
        priority="merge",
        analysis=None,
    )
    github_client = GitHubClient.get_instance()

    # Use LabelManager context manager to handle @auto-coder label automatically
    with LabelManager(
        github_client,
        repo_name,
        pr_data["number"],
        item_type="pr",
        config=config,
        check_labels=config.CHECK_LABELS,
        known_labels=pr_data.get("labels"),
    ) as should_process:
        if not should_process:
            processed_pr.actions_taken = ["Skipped - already being processed (@auto-coder label present)"]
            return processed_pr

        processed_pr.actions_taken = _handle_pr_merge(github_client, repo_name, pr_data, config, {})
        if any("Successfully merged" in action for action in processed_pr.actions_taken):
            should_process.keep_label()
        return processed_pr


def _process_pr_for_fixes(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> ProcessedPRResult:
    """Process a PR for issue resolution when GitHub Actions are failing or pending."""
    processed_pr = ProcessedPRResult(
        pr_data=pr_data,
        actions_taken=[],
        priority="fix",
        analysis=None,
    )

    # Use LabelManager context manager to handle @auto-coder label automatically
    with LabelManager(github_client, repo_name, pr_data["number"], item_type="pr", config=config, check_labels=config.CHECK_LABELS) as should_process:
        if not should_process:
            processed_pr.actions_taken = ["Skipped - already being processed (@auto-coder label present)"]
            return processed_pr

        # Use the existing PR actions logic for fixing issues
        with ProgressStage("Fixing issues"):
            try:
                actions = _take_pr_actions(github_client, repo_name, pr_data, config)
                processed_pr.actions_taken = actions
                # Retain label on successful merge
                if any("Successfully merged" in action for action in actions):
                    should_process.keep_label()
            except Exception as e:
                # Set error in result instead of adding to actions
                processed_pr.error = f"Processing failed: {str(e)}"

    return processed_pr


def _take_pr_actions(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> List[str]:
    """Take actions on a PR including merge handling and analysis."""
    actions = []
    pr_number = pr_data["number"]

    try:
        # First, handle the merge process (GitHub Actions, testing, etc.)
        # This doesn't depend on Gemini analysis
        merge_actions = _handle_pr_merge(github_client, repo_name, pr_data, config, {})
        actions.extend(merge_actions)

        # If merge process completed successfully (PR was merged), skip analysis
        if any("Successfully merged" in action for action in merge_actions):
            actions.append(f"PR #{pr_number} was merged.")
        elif "ACTION_FLAG:SKIP_ANALYSIS" in merge_actions or any("skipping to next PR" in action for action in merge_actions) or any("Skipping merge" in action for action in merge_actions):
            actions.append(f"PR #{pr_number} processing deferred.")

    except Exception as e:
        actions.append(f"Error taking PR actions for PR #{pr_number}: {e}")

    return actions


def _trigger_fallback_for_pr_failure(
    repo_name: str,
    pr_data: Dict[str, Any],
    failure_reason: str,
) -> None:
    """Trigger fallback by incrementing attempts for linked issues when PR processing fails.

    Args:
        repo_name: Repository name in format 'owner/repo'
        pr_data: PR data dictionary
        failure_reason: Reason for the failure
    """
    try:
        # Extract linked issues from PR body
        pr_body = pr_data.get("body", "")
        if not pr_body:
            logger.debug(f"No PR body found for PR #{pr_data['number']}, cannot extract linked issues")
            return

        linked_issues = extract_linked_issues_from_pr_body(pr_body)

        if not linked_issues:
            logger.debug(f"No linked issues found in PR #{pr_data['number']} body")
            return

        # Identify this failure by the PR state it was observed on, so that a PR that
        # keeps failing without receiving new commits only bumps the attempt once.
        trigger = build_pr_attempt_trigger(pr_data["number"], pr_data.get("head_sha") or (pr_data.get("head") or {}).get("sha"))

        # Increment attempt for each linked issue
        for issue_number in linked_issues:
            try:
                logger.info(f"Incrementing attempt for issue #{issue_number} due to PR #{pr_data['number']} failure: {failure_reason}")
                increment_attempt(repo_name, issue_number, trigger=trigger)
            except Exception as e:
                logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
                # Continue with other issues even if one fails
                continue

        logger.info(f"Triggered fallback for {len(linked_issues)} linked issue(s) from PR #{pr_data['number']}")

    except Exception as e:
        logger.error(f"Error triggering fallback for PR #{pr_data['number']}: {e}")


def _apply_pr_actions_directly(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> List[str]:
    """Ask LLM CLI to apply PR fixes directly; avoid posting PR comments.

    Expected LLM output formats:
    - "ACTION_SUMMARY: ..." single line when actions were taken
    - "CANNOT_FIX" when it cannot deterministically fix
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        # Get PR diff for analysis
        with ProgressStage("Getting PR diff"):
            pr_diff = _get_pr_diff(repo_name, pr_number, config)

        # Create action-oriented prompt (no comments)
        with ProgressStage("Creating prompt"):
            # Create analysis prompt
            try:
                prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config, github_client)
            except Exception:
                # Fallback for old signature if needed (though we are updating it)
                prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)
            logger.debug(
                "Prepared PR action prompt for #%s (preview: %s)",
                pr_data.get("number", "unknown"),
                prompt[:160].replace("\n", " "),
            )

        # Use LLM CLI to analyze and take actions
        log_action(f"Applying PR actions directly for PR #{pr_number}")

        # Call LLM client
        with ProgressStage("Running LLM"):
            response = get_llm_backend_manager()._run_llm_cli(prompt)

        # Process the response
        if response and len(response.strip()) > 0:
            resp = response.strip()
            # Prefer ACTION_SUMMARY line if present
            summary_line = None
            for line in resp.splitlines():
                if line.startswith("ACTION_SUMMARY:"):
                    summary_line = line
                    break
            if summary_line:
                actions.append(summary_line[: config.MAX_RESPONSE_SIZE])
            elif "CANNOT_FIX" in resp:
                actions.append(f"LLM reported CANNOT_FIX for PR #{pr_data['number']}")
                # Trigger fallback due to LLM failure
                _trigger_fallback_for_pr_failure(repo_name, pr_data, "LLM merge risky/failed (CANNOT_FIX)")
            else:
                # Fallback: record truncated raw response without posting comments
                actions.append(f"LLM response: {resp[: config.MAX_RESPONSE_SIZE]}...")
                # Trigger fallback due to unclear LLM response
                _trigger_fallback_for_pr_failure(repo_name, pr_data, "LLM merge risky/failed (unclear response)")

            # Detect self-merged indication in summary/response
            lower = resp.lower()
            if "merged" in lower or "auto-merge" in lower:
                actions.append(f"Auto-merged PR #{pr_number} based on LLM action")
            else:
                # Stage, commit, and push via helpers (LLM must not commit directly)
                with ProgressStage("Staging changes"):
                    add_res = cmd.run_command(["git", "add", "."])
                    if not add_res.success:
                        actions.append(f"Failed to stage changes: {add_res.stderr}")
                        return actions

                # Commit using centralized helper with dprint retry logic
                with ProgressStage("Committing changes"):
                    commit_msg = f"Auto-Coder: Apply fix for PR #{pr_number}"
                    commit_res = git_commit_with_retry(commit_msg)

                if commit_res.success:
                    actions.append(f"Committed changes for PR #{pr_number}")

                    # Push changes to remote with retry
                    with ProgressStage("Pushing changes"):
                        push_res = git_push()
                        if push_res.success:
                            actions.append(f"Pushed changes for PR #{pr_number}")
                        else:
                            # Push failed - try one more time after a brief pause
                            logger.warning(f"First push attempt failed: {push_res.stderr}, retrying...")

                    if not push_res.success:
                        with ProgressStage("Retrying push"):
                            import time

                            time.sleep(2)
                            retry_push_res = git_push()
                            if retry_push_res.success:
                                actions.append(f"Pushed changes for PR #{pr_number} (after retry)")
                            else:
                                logger.error(f"Failed to push changes after retry: {retry_push_res.stderr}")
                                actions.append(f"CRITICAL: Committed but failed to push changes: {retry_push_res.stderr}")
                                # Trigger fallback due to push failure
                                _trigger_fallback_for_pr_failure(repo_name, pr_data, "Failed to push changes after retry")
                else:
                    # Check if it's a "nothing to commit" case
                    if "nothing to commit" in (commit_res.stdout or ""):
                        actions.append("No changes to commit")
                    else:
                        # Save history and exit immediately
                        context = {
                            "type": "pr",
                            "pr_number": pr_number,
                            "commit_message": commit_msg,
                        }
                        save_commit_failure_history(commit_res.stderr, context, repo_name=None)
                        # This line will never be reached due to sys.exit in save_commit_failure_history
                        actions.append(f"Failed to commit changes: {commit_res.stderr or commit_res.stdout}")
                        # Trigger fallback due to commit failure
                        _trigger_fallback_for_pr_failure(repo_name, pr_data, "Failed to commit changes")
        else:
            actions.append("LLM CLI did not provide a clear response for PR actions")
            # Trigger fallback due to no LLM response
            _trigger_fallback_for_pr_failure(repo_name, pr_data, "LLM merge risky/failed (no response)")

    except Exception as e:
        actions.append(f"Error applying PR actions directly: {e}")
        # Trigger fallback due to exception
        _trigger_fallback_for_pr_failure(repo_name, pr_data, f"Exception during LLM processing: {str(e)}")

    return actions


def _get_pr_diff(repo_name: str, pr_number: int, config: AutomationConfig) -> str:
    """Get PR diff for analysis."""
    try:
        return GitHubClient.get_instance().get_pr_diff(repo_name, pr_number)[: config.MAX_PR_DIFF_SIZE]

    except Exception as e:
        logger.error(f"Failed to get PR diff via GhApi: {e}")
        return "Could not retrieve PR diff"


def _create_pr_analysis_prompt(repo_name: str, pr_data: Dict[str, Any], pr_diff: str, config: AutomationConfig, github_client: Optional[Any] = None, is_jules: bool = False) -> str:
    """Create a PR prompt that prioritizes direct code changes over comments with label-based selection."""
    pr_body = pr_data.get("body") or ""

    # Extract linked issues context
    linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_body)

    # Get commit log since branch creation
    commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

    body_text = pr_body[: config.MAX_PROMPT_SIZE]
    # Extract PR labels for label-based prompt selection
    pr_labels_list = pr_data.get("labels", []) or []

    result: str = render_prompt(
        "pr.action",
        repo_name=repo_name,
        pr_number=pr_data.get("number", "unknown"),
        pr_title=pr_data.get("title", "Unknown"),
        pr_body=body_text,
        pr_author=pr_data.get("user", {}).get("login", "unknown"),
        pr_state=pr_data.get("state", "open"),
        pr_draft=pr_data.get("draft", False),
        pr_mergeable=pr_data.get("mergeable", False),
        diff_limit=config.MAX_PR_DIFF_SIZE,
        pr_diff=pr_diff,
        commit_log=commit_log or "(No commit history)",
        linked_issues_context=linked_issues_context,
        labels=pr_labels_list,
        label_prompt_mappings=config.pr_label_prompt_mappings,
        label_priorities=config.label_priorities,
        is_jules=is_jules,
    )
    return result


def _process_pr_jules_mode(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Any,
) -> List[str]:
    """Process a PR using Jules API for session-based AI interaction.

    This function:
    1. Starts a Jules session for the PR
    2. Comments on the PR with the session ID
    3. Updates PR body with Session ID to mark it as Jules-managed
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        from .jules_client import JulesClient

        # Check if already Jules PR (sanity check)
        if _is_jules_pr(pr_data):
            return ["PR is already a Jules PR"]

        logger.info(f"Converting PR #{pr_number} to Jules mode")
        actions.append(f"Converting PR #{pr_number} to Jules mode")

        # 1. Start session
        jules_client = JulesClient()

        # Get prompt
        pr_diff = _get_pr_diff(repo_name, pr_number, config)
        action_prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config, github_client, is_jules=True)

        pr_branch = pr_data.get("head", {}).get("ref")
        session_title = f"PR #{pr_number}: {pr_data.get('title', 'Unknown')}"

        session_id = jules_client.start_session(action_prompt, repo_name, pr_branch, title=session_title)

        # 2. Save session
        CloudManager(repo_name).add_session(pr_number, session_id)
        actions.append(f"Started Jules session {session_id}")

        get_trace_logger().log("Jules Mode", f"Started Jules session for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"session_id": session_id})

        # 3. Update PR body
        from auto_coder.util.gh_cache import GitHubClient, get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        pr_body = pr_data.get("body", "") or ""
        # Append session info
        new_body = f"{pr_body}\n\nSession ID: {session_id}\nhttps://jules.google.com/session/{session_id}"

        new_body = f"{pr_body}\n\nSession ID: {session_id}\nhttps://jules.google.com/session/{session_id}"

        # Validate issue references in new body
        try:
            validate_issue_references(new_body, github_client, repo_name)
        except ValueError as e:
            logger.error(f"Validation failed for Jules PR update: {e}")
            actions.append(f"Error: Validation failed for PR update: {e}")
            return actions

        api.pulls.update(owner, repo, pr_number, body=new_body)
        actions.append(f"Updated PR body with session ID: {session_id}")

        # 4. Comment
        comment = f"I started a Jules session to work on this PR. Session ID: {session_id}\n\nhttps://jules.google.com/session/{session_id}"
        github_client.add_comment_to_pr(repo_name, pr_number, comment)
        actions.append("Commented on PR with session details")

        return actions

    except Exception as e:
        msg = f"Error in _process_pr_jules_mode: {e}"
        logger.error(msg)
        actions.append(msg)
        return actions


def _find_authoritative_adversarial_review(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Find the dedicated reviewer App's native review body for an exact head SHA.

    Persisted adversarial-validation state now lives in a native PR review
    authored through the dedicated `auto-coder-reviewer` GitHub App instead of
    a regular user-authenticated PR comment. A review only counts as
    authoritative when it was authored by that exact App identity (resolved
    from the App's own credentials, not a configurable string) and carries
    the marker for this exact head SHA; a lookalike review from anyone else
    is ignored. Any failure to resolve the App identity or read PR reviews is
    reported as an error rather than treated as "no review", because this
    lookup gates merge decisions and must fail closed.
    """
    marker = adversarial_validation_comment_marker(head_sha)
    try:
        identity = resolve_reviewer_app_identity(repo_name)
    except Exception as e:
        logger.error(f"Failed to resolve dedicated reviewer App identity for PR #{pr_number}: {e}")
        return None, str(e)

    try:
        reviews = github_client.get_pr_reviews_strict(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed to read PR reviews for PR #{pr_number}: {e}")
        return None, str(e)

    for review in reversed(reviews):
        login = _comment_value(_comment_value(review, "user") or {}, "login", "")
        body = _comment_value(review, "body", "")
        if login == identity.login and isinstance(body, str) and body.startswith(marker):
            return body, None

    return None, None


def _get_legacy_adversarial_validation_comment(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Return a pre-migration validation comment for one PR head SHA, if any.

    Legacy comments were produced by the previous user-authenticated
    publication path. They are only ever read here for backward-compatible
    same-SHA deduplication; this path never creates or updates them.
    """
    marker = adversarial_validation_comment_marker(head_sha)
    try:
        comments = github_client.get_pr_comments(repo_name, pr_number)
    except Exception as e:
        logger.error(f"Failed to read legacy adversarial validation comments for PR #{pr_number}: {e}")
        return None, str(e)

    for comment in comments:
        body = _comment_value(comment, "body", "")
        if isinstance(body, str) and body.startswith(marker):
            return body, None
    return None, None


def _parse_adversarial_validation_status(body: str) -> str:
    """Extract the verdict recorded in a validation marker body."""
    match = re.search(r"^## .*adversarial validation: (PASS|NEEDS_FIX|BLOCKED|INCONCLUSIVE|ERROR)\s*$", body, re.MULTILINE)
    if not match:
        return "ERROR"
    if match.group(1) == "PASS" and "### Specification gaps (" in body:
        return "PASS_WITH_SPECIFICATION_GAPS"
    return match.group(1)


def _get_published_adversarial_validation_status(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Read the immutable validation status already published for a head SHA.

    The dedicated reviewer App's native review is the authoritative source.
    A legacy user-authenticated comment is only consulted when no native
    review exists yet, so PR heads validated before this change keep their
    persisted state without being re-validated or re-published.
    """
    native_body, native_error = _find_authoritative_adversarial_review(github_client, repo_name, pr_number, head_sha)
    if native_error:
        return None, native_error
    if native_body is not None:
        return _parse_adversarial_validation_status(native_body), None

    legacy_body, legacy_error = _get_legacy_adversarial_validation_comment(github_client, repo_name, pr_number, head_sha)
    if legacy_error:
        return None, legacy_error
    if legacy_body is not None:
        return _parse_adversarial_validation_status(legacy_body), None

    return None, None


def _get_published_adversarial_validation_comment(
    github_client: Any,
    repo_name: str,
    pr_number: int,
    head_sha: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Return the durable validation report for one PR head SHA."""
    native_body, native_error = _find_authoritative_adversarial_review(github_client, repo_name, pr_number, head_sha)
    if native_error:
        return None, native_error
    if native_body is not None:
        return native_body, None

    return _get_legacy_adversarial_validation_comment(github_client, repo_name, pr_number, head_sha)


@contextlib.contextmanager
def isolated_pr_head_worktree(repo_name: str, pr_number: int, head_sha: Optional[str] = None):
    """Context manager that creates an isolated, detached git worktree at head_sha for side-effect-free validation.

    Ensures that:
    1. Static repository inspection and dynamic test checks run against the exact CI-green head_sha.
    2. The caller's workspace, current branch, index, and untracked files remain completely untouched.
    3. No branch switching, pulling, pushing, resetting, or cleaning occurs on the main workspace during validation.
    """
    if not head_sha:
        raise ValueError(f"head_sha is required for isolated worktree validation of PR #{pr_number}")

    worktree_dir = None
    original_cwd = os.getcwd()
    try:
        # Ensure the exact head_sha / pull head is fetched locally
        cmd.run_command(["git", "fetch", "origin", f"pull/{pr_number}/head"])

        worktree_dir = tempfile.mkdtemp(prefix=f"auto_coder_val_pr{pr_number}_")

        add_res = cmd.run_command(["git", "worktree", "add", "--detach", worktree_dir, head_sha])
        if not add_res.success:
            logger.warning(f"Failed to create isolated git worktree at {head_sha[:8]}: {add_res.stderr}")
            raise RuntimeError(f"Failed to create isolated git worktree at {head_sha[:8]}: {add_res.stderr}")

        os.chdir(worktree_dir)
        logger.info(f"Entered isolated detached worktree at {head_sha[:8]} ({worktree_dir}) for validation")
        yield worktree_dir
    finally:
        try:
            os.chdir(original_cwd)
        except Exception as e:
            logger.error(f"Failed to restore original cwd '{original_cwd}': {e}")

        if worktree_dir and os.path.exists(worktree_dir):
            try:
                cmd.run_command(["git", "worktree", "remove", "--force", worktree_dir])
                shutil.rmtree(worktree_dir, ignore_errors=True)
                logger.debug(f"Cleaned up isolated worktree at {worktree_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up worktree dir {worktree_dir}: {e}")


def _handle_pr_merge(
    github_client: Any,
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    analysis: Dict[str, Any],
) -> List[str]:
    """Handle PR merge process following the intended flow."""
    actions = []
    pr_number = pr_data["number"]

    try:
        # A stale review-thread resolution (issue #1619) that could not be
        # rolled back in an earlier run is a persistent integrity failure:
        # retry it on every processing run, and refuse to merge while any
        # thread remains blocked, regardless of what CI/validation would
        # otherwise decide this run (REQ-006, REQ-008).
        stale_client = github_client or GitHubClient.get_instance()
        try:
            pending_stale_threads = retry_pending_stale_review_thread_rollbacks(stale_client, repo_name, pr_number)
        except StaleReviewThreadRegistryError as e:
            # The registry's storage cannot be trusted (corrupt, unreadable):
            # it may be hiding a real stale-resolution blocker, so this must
            # never be treated as "no blockers exist" (REQ-006, REQ-008).
            logger.error(f"Stale-review-thread registry is unreadable for PR #{pr_number}: {e}")
            actions.append(f"Skipping merge for PR #{pr_number}: stale-review-thread registry could not be read ({e})")
            return actions
        if pending_stale_threads:
            actions.append(f"Skipping merge for PR #{pr_number}: review thread(s) {', '.join(pending_stale_threads)} were resolved against a stale head and could not be reverted")
            return actions

        # Step 1: Check GitHub Actions status using utility function
        # Use switch_branch_on_in_progress=False to just skip instead of exit
        should_continue = check_github_actions_and_exit_if_in_progress(  # type: ignore[arg-type]
            repo_name=repo_name,
            pr_data=pr_data,
            config=config,  # type: ignore[arg-type]
            github_client=None,
            switch_branch_on_in_progress=False,
            item_number=pr_number,
            item_type="PR",
        )  # Not needed for this check

        mergeability = _get_mergeable_state(repo_name, pr_data, config)
        mergeable_flag = mergeability.get("mergeable")
        merge_state_status = mergeability.get("merge_state_status")

        if mergeable_flag is False:
            state_text = merge_state_status or "unknown"
            actions.append(f"PR #{pr_number} is not mergeable (state: {state_text})")

            if config.ENABLE_MERGEABILITY_REMEDIATION:
                remediation_actions = _start_mergeability_remediation(pr_number, merge_state_status, repo_name)
                actions.extend(remediation_actions)
                return actions

        # Step 2: If checks are in progress, skip this PR
        if not should_continue:
            actions.append(f"GitHub Actions checks are still in progress for PR #{pr_number}, skipping to next PR")
            return actions

        # Step 3: Get detailed status for merge decision
        github_checks = _check_github_actions_status(repo_name, pr_data, config)
        if github_checks.error:
            actions.append(f"Could not determine CI status for PR #{pr_number}: {github_checks.error}")
            logger.error(f"Could not determine CI status for PR #{pr_number}: {github_checks.error}")
            return actions

        # Check if no actions have started for the latest commit
        if not github_checks.ids:
            # No checks found for the current head SHA
            logger.info(f"No GitHub Actions found for PR #{pr_number} (SHA: {pr_data.get('head', {}).get('sha')[:8]}). Triggering ci.yml...")

            # 1. Add @auto-coder label to prevent multiple executions
            # We use LabelManager to add the label
            with LabelManager(
                github_client,
                repo_name,
                pr_number,
                item_type="pr",
                config=config,
                known_labels=pr_data.get("labels"),
            ) as lm:
                # Label added by entering context

                # 2. Trigger workflow_dispatch
                from auto_coder.util.github_action import trigger_workflow_dispatch

                # Check if monitor is already active BEFORE triggering workflow
                # This prevents duplicate workflow runs and duplicate monitors
                with _active_monitors_lock:
                    if pr_number in _active_monitors:
                        logger.info(f"Monitor already active for PR #{pr_number}, skipping trigger")
                        return actions
                    _active_monitors.add(pr_number)
                    logger.debug(f"Added PR #{pr_number} to active monitors")

                head_branch = pr_data.get("head", {}).get("ref")
                workflow_id = "ci.yml"

                try:
                    triggered = trigger_workflow_dispatch(repo_name, workflow_id, head_branch)

                    if triggered:
                        actions.append(f"Triggered {workflow_id} for PR #{pr_number}")
                        get_trace_logger().log("CI Trigger", f"Triggered {workflow_id} for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"workflow": workflow_id})

                        # 3. Start async monitor
                        head_sha = pr_data.get("head", {}).get("sha")

                        try:
                            monitor_thread = threading.Thread(target=_run_async_monitor, args=(repo_name, pr_number, head_sha, workflow_id), daemon=True)
                            monitor_thread.start()
                            actions.append(f"Started async monitor for {workflow_id}")
                            get_trace_logger().log("CI Trigger", f"Started async monitor for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"monitor": True})
                        except Exception as e:
                            # Clean up if thread fails to start
                            with _active_monitors_lock:
                                _active_monitors.discard(pr_number)
                            logger.error(f"Failed to start monitor thread for PR #{pr_number}: {e}")
                            actions.append(f"Failed to start monitor for {workflow_id}: {e}")

                        # Keep the label so async monitor can remove it later
                        lm.keep_label()
                        return actions

                    else:
                        actions.append(f"Failed to trigger {workflow_id} for PR #{pr_number}")
                        # Clean up active monitor since we failed to trigger
                        with _active_monitors_lock:
                            _active_monitors.discard(pr_number)
                        # Label will be removed by LabelManager exit

                except Exception as e:
                    # Clean up active monitor on exception
                    with _active_monitors_lock:
                        _active_monitors.discard(pr_number)
                    raise e

            return actions

        # Step 4: If GitHub Actions passed, merge directly
        if github_checks.success:
            actions.append(f"All GitHub Actions checks passed for PR #{pr_number}")

            # Check if AUTO_MERGE is enabled before attempting merge
            if not config.AUTO_MERGE:
                actions.append(f"Skipping merge for PR #{pr_number} due to configuration (AUTO_MERGE=False)")
                return actions

            # Check for disable-auto-merge label
            labels = pr_data.get("labels", [])
            if any((isinstance(label, dict) and label.get("name") == "disable-auto-merge") or (isinstance(label, str) and label == "disable-auto-merge") for label in labels):
                actions.append(f"Skipping merge for PR #{pr_number} due to 'disable-auto-merge' label")
                return actions

            # Check for unresolved review threads. A thread explicitly claimed as
            # addressed by a supported automated reviewer's implementation agent
            # (REQ-001, REQ-011) does not block merge outright; it is instead
            # carried into a fresh adversarial validation run so an independent
            # disposition can decide whether to resolve it.
            claimed_thread_state = _get_claimed_review_thread_state(github_client, repo_name, pr_number)
            if claimed_thread_state.lookup_error:
                actions.append(f"Skipping merge for PR #{pr_number} because review threads could not be checked: {claimed_thread_state.lookup_error}")
                return actions
            if claimed_thread_state.has_blocking_unresolved:
                actions.append(f"Skipping merge for PR #{pr_number} due to unresolved review threads")
                pending_provenance = tuple(thread for thread in claimed_thread_state.blocking_unresolved if is_change_provenance_thread(thread))
                repair_threads = tuple(thread for thread in claimed_thread_state.blocking_unresolved if not is_change_provenance_thread(thread))
                if pending_provenance:
                    actions.append(f"Awaiting implementer provenance clarification on {len(pending_provenance)} review thread(s); no code change was requested")
                if repair_threads:
                    actions.extend(
                        _delegate_codex_cloud_review_thread_repair(
                            repo_name,
                            pr_data,
                            github_client=github_client,
                            unresolved_threads=repair_threads,
                        )
                    )
                return actions
            claimed_review_threads = claimed_thread_state.claimed
            if claimed_review_threads:
                actions.append(f"PR #{pr_number} has {len(claimed_review_threads)} claimed-addressed review thread(s) pending independent validation")

            # Strong-model adversarial validation step. Issue-less PRs have no
            # independent specification oracle, so validation is not applicable.
            adversarial_validation_enabled = config.ENABLE_ADVERSARIAL_VALIDATION and not _is_dependabot_pr(pr_data)
            adversarial_eligibility = AdversarialValidationEligibility()
            if adversarial_validation_enabled:
                adversarial_eligibility = _get_adversarial_validation_eligibility(github_client, repo_name, pr_data)
                if adversarial_eligibility.lookup_error:
                    actions.append(f"Could not verify adversarial-validation eligibility for PR #{pr_number}: {adversarial_eligibility.lookup_error}; merge not attempted")
                    return actions

            adversarial_validation_applicable = adversarial_eligibility.is_applicable
            if adversarial_validation_enabled and not adversarial_validation_applicable:
                actions.append(f"Skipped adversarial validation for PR #{pr_number}: no linked Issue specification oracle")
                logger.info(f"PR #{pr_number} has no linked Issue; adversarial validation is not applicable")

            if adversarial_validation_enabled and adversarial_validation_applicable:
                max_adv_reviews = config.MAX_ADVERSARIAL_VALIDATIONS if config.MAX_ADVERSARIAL_VALIDATIONS is not None else config.MAX_ADVERSARIAL_REVIEWS
                if max_adv_reviews is not None and max_adv_reviews >= 0:
                    try:
                        existing_pr_comments = github_client.get_pr_comments(repo_name, pr_number) if github_client else []
                        existing_reviews = github_client.get_pr_reviews_strict(repo_name, pr_number) if github_client else []
                        reviewer_identity = resolve_reviewer_app_identity(repo_name)
                        authoritative_reviews = [review for review in existing_reviews if _comment_value(_comment_value(review, "user") or {}, "login", "") == reviewer_identity.login]
                        adv_review_count = count_adversarial_validation_comments(existing_pr_comments) + count_adversarial_validation_comments(authoritative_reviews)
                    except Exception as e:
                        logger.error(f"Failed to check adversarial validation count for PR #{pr_number}: {e}")
                        actions.append(f"Could not check prior adversarial validation count for PR #{pr_number}: {e}; validation not started")
                        return actions

                    provenance_fingerprint = change_provenance_reply_fingerprint(claimed_review_threads)
                    if adv_review_count >= max_adv_reviews and not provenance_fingerprint:
                        actions.append(f"Skipped adversarial validation for PR #{pr_number}: reached maximum adversarial review limit ({max_adv_reviews})")
                        logger.info(f"PR #{pr_number} reached maximum adversarial review limit ({adv_review_count}/{max_adv_reviews}); proceeding to merge")
                        current_head_sha = pr_data.get("head", {}).get("sha", "")
                        saved_status, saved_status_error = _get_published_adversarial_validation_status(
                            github_client,
                            repo_name,
                            pr_number,
                            current_head_sha,
                        )
                        if saved_status_error:
                            actions.append(f"Could not check unresolved specification gaps for PR #{pr_number}: {saved_status_error}; merge not attempted")
                            return actions
                        if saved_status == "PASS_WITH_SPECIFICATION_GAPS":
                            actions.append(f"Automatic merge disabled for PR #{pr_number}: unresolved specification gaps require human policy review")
                            return actions
                        should_run_validation = False
                    else:
                        if adv_review_count >= max_adv_reviews:
                            actions.append(f"Revalidating PR #{pr_number} beyond the review limit because new change-provenance evidence was supplied")
                        should_run_validation = True
                else:
                    should_run_validation = True

                if should_run_validation:
                    codex_review = _get_codex_review_state(github_client, repo_name, pr_number)
                    if codex_review.lookup_error:
                        actions.append(f"Could not determine Codex review state for PR #{pr_number}: {codex_review.lookup_error}; validation not started")
                        return actions
                    if codex_review.present and not codex_review.completed:
                        actions.append(f"Waiting for Codex GitHub review to complete for PR #{pr_number}; adversarial validation not started")
                        return actions
                    if codex_review.completed:
                        post_codex_thread_state = _get_claimed_review_thread_state(github_client, repo_name, pr_number)
                        if post_codex_thread_state.lookup_error:
                            actions.append(f"Codex review completed for PR #{pr_number}, but review threads could not be rechecked: {post_codex_thread_state.lookup_error}; validation not started")
                            return actions
                        if post_codex_thread_state.has_blocking_unresolved:
                            actions.append(f"Codex review completed for PR #{pr_number} with unresolved review threads; adversarial validation not started")
                            return actions
                        # Codex may have just posted its own review threads; use the
                        # freshest claimed-thread set for this validation run.
                        claimed_review_threads = post_codex_thread_state.claimed

                    head_sha = pr_data.get("head", {}).get("sha", "")

                    if not head_sha:
                        actions.append(f"Adversarial validation blocked PR #{pr_number}: Missing head.sha in PR data")
                        logger.warning(f"Adversarial validation blocked PR #{pr_number}: Missing head.sha in PR data")
                        return actions

                    published_status, lookup_error = _get_published_adversarial_validation_status(
                        github_client,
                        repo_name,
                        pr_number,
                        head_sha,
                    )
                    if lookup_error:
                        actions.append(f"Could not check prior adversarial validation for PR #{pr_number}: {lookup_error}; validation not started")
                        return actions
                    provenance_fingerprint = change_provenance_reply_fingerprint(claimed_review_threads)
                    has_new_provenance_evidence = False
                    if published_status and provenance_fingerprint:
                        published_report, report_error = _get_published_adversarial_validation_comment(
                            github_client,
                            repo_name,
                            pr_number,
                            head_sha,
                        )
                        if report_error:
                            actions.append(f"Could not compare prior provenance evidence for PR #{pr_number}: {report_error}; validation not started")
                            return actions
                        has_new_provenance_evidence = bool(published_report and provenance_fingerprint not in published_report)

                    if published_status and not has_new_provenance_evidence:
                        actions.append(f"Skipped adversarial validation for PR #{pr_number}: commit {head_sha[:8]} was already validated as {published_status}")
                        if published_status != "PASS":
                            actions.append(f"Adversarial validation remains non-pass for PR #{pr_number}: {published_status}")
                            if published_status == "NEEDS_FIX":
                                published_report, report_error = _get_published_adversarial_validation_comment(
                                    github_client,
                                    repo_name,
                                    pr_number,
                                    head_sha,
                                )
                                if report_error:
                                    actions.append(f"Could not read the published adversarial report for Codex Cloud: {report_error}")
                                elif published_report:
                                    actions.extend(
                                        _send_adversarial_validation_feedback_to_codex_cloud(
                                            repo_name,
                                            pr_data,
                                            head_sha,
                                            published_report,
                                            github_client,
                                        )
                                    )
                            return actions
                    else:
                        if has_new_provenance_evidence:
                            actions.append(f"Revalidating PR #{pr_number} at unchanged commit {head_sha[:8]} using new implementer provenance evidence")
                        claimed_review_threads_section = render_claimed_review_threads_section(claimed_review_threads)
                        try:
                            with isolated_pr_head_worktree(repo_name, pr_number, head_sha):
                                actions.append(f"Validated PR #{pr_number} in isolated worktree pinned to SHA {head_sha[:8]}")
                                val_result = run_adversarial_validation(
                                    repo_name,
                                    pr_data,
                                    config,
                                    github_client=github_client,
                                    claimed_review_threads_section=claimed_review_threads_section,
                                )
                        except Exception as e:
                            exception_preview = redact_string(str(e))[:2000]
                            logger.error(f"Adversarial validation execution failed for PR #{pr_number} " f"({type(e).__name__}): {exception_preview}")
                            val_result = AdversarialValidationResult(
                                result="BLOCKED",
                                summary="Adversarial validation execution failed; see the structured interaction log for details",
                                diagnostic_category="validation_execution_error",
                                diagnostic_reason=type(e).__name__,
                            )

                        val_result.clarification_reply_fingerprint = provenance_fingerprint
                        if provenance_fingerprint:
                            # The existing aggregated clarification thread remains
                            # authoritative until independently resolved.
                            val_result.publish_clarification_thread = False
                        val_result.provenance_thread_comment_ids = {thread.thread_id: thread.root_comment_database_id for thread in claimed_review_threads if thread.is_change_provenance and thread.root_comment_database_id is not None}

                        # Independent thread-completion validation (REQ-001..REQ-010): this
                        # runs whenever a fresh validation pass produced dispositions,
                        # regardless of the PR-level verdict (REQ-005 independence).
                        resolved_thread_ids: List[str] = []
                        if claimed_review_threads and val_result.thread_dispositions:
                            try:
                                resolved_thread_ids = resolve_addressed_review_threads(
                                    github_client,
                                    repo_name,
                                    pr_number,
                                    head_sha,
                                    claimed_review_threads,
                                    val_result.thread_dispositions,
                                )
                                if resolved_thread_ids:
                                    actions.append(f"Resolved {len(resolved_thread_ids)} claimed review thread(s) for PR #{pr_number} after independent validation")
                            except StaleReviewThreadResolutionError as e:
                                # A thread is durably resolved against a stale head and
                                # could not be rolled back: GitHub's authoritative
                                # unresolved-thread gate can no longer be trusted for
                                # this PR, so merge must not proceed this run even if
                                # every other gate would otherwise allow it.
                                logger.error(f"Stale review-thread resolution could not be rolled back for PR #{pr_number}: {e}")
                                actions.append(f"Skipping merge for PR #{pr_number}: review thread {e.thread_id} was resolved against a stale head and could not be reverted")
                                return actions
                            except Exception as e:
                                logger.error(f"Failed to process claimed review thread dispositions for PR #{pr_number}: {e}")

                        _enforce_unresolved_provenance_gate(val_result, claimed_review_threads, resolved_thread_ids)

                        publication = publish_adversarial_review(repo_name, pr_number, head_sha, val_result)
                        if not publication.success:
                            actions.append(f"Adversarial review publication blocked PR #{pr_number}: {publication.reason}")
                            logger.warning(f"Adversarial review publication blocked PR #{pr_number}")
                            return actions
                        actions.append(f"Published {publication.event} adversarial review for PR #{pr_number} at SHA {head_sha[:8]}")

                    if published_status == "PASS":
                        pass
                    elif val_result.needs_fix:
                        actions.append(f"Adversarial validation failed for PR #{pr_number}: {len(val_result.findings)} specification violation(s) found")
                        logger.warning(f"PR #{pr_number} failed adversarial validation: {val_result.summary}")
                        actions.extend(
                            _send_adversarial_validation_feedback_to_codex_cloud(
                                repo_name,
                                pr_data,
                                head_sha,
                                format_adversarial_validation_comment(val_result, head_sha),
                                github_client,
                            )
                        )
                        actions.append(f"Awaiting PR author or Codex Cloud changes for PR #{pr_number}; no local automatic adversarial fix was attempted")
                        return actions

                    elif not val_result.is_pass:
                        # Non-pass result (BLOCKED, INCONCLUSIVE, ERROR) - fail-closed: do not merge!
                        actions.append(f"Adversarial validation blocked PR #{pr_number}: {val_result.summary}")
                        logger.warning(f"Adversarial validation blocked PR #{pr_number}: {val_result.summary}")
                        return actions
                    elif not val_result.allows_auto_merge:
                        actions.append(f"Automatic merge disabled for PR #{pr_number}: {len(val_result.specification_gaps)} unresolved specification gap(s) require human policy review")
                        return actions
                    else:
                        actions.append(f"Adversarial validation passed for PR #{pr_number}: {val_result.summary}")

            # Verify remote PR head SHA hasn't changed since CI check and validation before merging (fail-closed)
            head_sha = pr_data.get("head", {}).get("sha", "")
            if not github_client:
                actions.append(f"Cannot verify remote head SHA for PR #{pr_number} without github_client; merge aborted.")
                logger.warning(f"No github_client available to verify PR #{pr_number} head SHA; aborting merge.")
                return actions

            try:
                current_pr = github_client.get_pull_request(repo_name, pr_number)
                current_head_sha = current_pr.get("head", {}).get("sha") if isinstance(current_pr, dict) else getattr(getattr(current_pr, "head", None), "sha", None)
                if not current_head_sha:
                    actions.append(f"Could not determine current remote head SHA for PR #{pr_number}; merge aborted.")
                    logger.warning(f"Could not determine remote head SHA for PR #{pr_number}; aborting merge.")
                    return actions

                if head_sha and current_head_sha != head_sha:
                    actions.append(f"PR #{pr_number} head SHA changed from {head_sha[:8]} to {current_head_sha[:8]} during validation; merge aborted.")
                    logger.warning(f"PR #{pr_number} head SHA changed during validation; skipping merge.")
                    return actions
            except Exception as e:
                actions.append(f"Failed to verify remote head SHA for PR #{pr_number}: {e}; merge aborted.")
                logger.warning(f"Failed to verify remote head SHA for PR #{pr_number}: {e}; skipping merge.")
                return actions

            merge_result = _merge_pr(
                repo_name,
                pr_number,
                analysis,
                config,
                github_client=github_client,
                expected_head_sha=current_head_sha or head_sha or None,
            )
            if merge_result:
                actions.append(f"Successfully merged PR #{pr_number}")
                _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)

                # Clean up old PRs if this is a Jules PR with a session ID
                try:
                    is_jules = _is_jules_pr(pr_data)
                    session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))
                    if is_jules and session_id:
                        # Note: search_issues returns Issue objects which can be PRs
                        query = f'repo:{repo_name} is:pr is:open "Session ID: {session_id}"'
                        logger.info(f"Searching for other PRs with session ID {session_id} to clean up: {query}")

                        related_issues = github_client.search_issues(query)

                        count = 0
                        for issue in related_issues:
                            # Skip the current PR (which is closed now effectively, or about to be)
                            if issue.number == pr_number:
                                continue

                            # Check if the issue object is actually a PR (search_issues returns issues/PRs)
                            # is:pr in query helps, but PyGithub object might need check?
                            # GitHubClient.search_issues returns list(self.github.search_issues(...))
                            # which are Issue objects.

                            # Remove @auto-coder label
                            if config.AUTO_CODER_LABEL:
                                try:
                                    github_client.remove_labels(repo_name, issue.number, [config.AUTO_CODER_LABEL], item_type="pr")
                                    actions.append(f"Removed {config.AUTO_CODER_LABEL} label from related PR #{issue.number} (Session ID: {session_id})")
                                    count += 1
                                except Exception as e:
                                    logger.error(f"Failed to remove label from related PR #{issue.number}: {e}")

                        if count > 0:
                            actions.append(f"Cleaned up {count} related PR(s) for session {session_id}")

                except Exception as e:
                    logger.error(f"Error cleaning up related PRs for PR #{pr_number}: {e}")
                    # Don't fail the whole process for cleanup error

                return actions
            else:
                actions.append(f"Failed to merge PR #{pr_number}")

        # Step 4: GitHub Actions failed - handle Jules PR feedback loop
        # Fetch detailed checks only when needed to save API calls
        detailed_checks = get_detailed_checks_from_history(github_checks, repo_name)
        failed_checks = detailed_checks.failed_checks
        actions.append(f"GitHub Actions checks failed for PR #{pr_number}: {len(failed_checks)} failed")

        # Codex Cloud owns corrective work for its PRs regardless of the local
        # checkout state. Resolve this execution origin before inspecting the
        # branch: CHECK_LABELS=False is used by WIP resumption and --only
        # processing, and must not turn cloud-originated work into local repair.
        if _is_codex_pr(pr_data):
            actions.append(f"PR #{pr_number} is a Codex-created PR, sending continuation request to Codex Cloud")
            feedback_result = _send_codex_cloud_error_feedback(repo_name, pr_data, failed_checks, config, github_client)
            actions.extend(feedback_result.actions)
            if feedback_result.delivered:
                actions.append(f"Codex Cloud will handle fixing PR #{pr_number}, skipping local fixes")
            else:
                actions.append(f"Codex Cloud repair request for PR #{pr_number} was not delivered; retry is required and local fixes remain disabled")
            return actions

        # Check if we are already on the PR branch before checkout.
        #
        # In WIP-resumption mode (CHECK_LABELS=False), assume we are already on the PR branch
        # to avoid unnecessary git calls and to keep label-handling deterministic.
        pr_branch_name = pr_data.get("head", {}).get("ref", "")
        if not config.CHECK_LABELS:
            already_on_pr_branch = True
        else:
            current_branch_res = cmd.run_command(
                ["git", "branch", "--show-current"],
                timeout=60,
                stream_output=False,
            )
            current_branch = current_branch_res.stdout.strip() if current_branch_res.success else ""
            already_on_pr_branch = (current_branch == pr_branch_name) and (current_branch != "")

        # Check if this is a Jules PR.
        #
        # Jules PRs are never fixed automatically: Jules does not pick up commits pushed to
        # its branch by anyone else, so auto-fix commits would silently diverge from the
        # Jules session. Either Jules fixes the PR itself, or the PR is closed once it has
        # not passed CI within JULES_PR_CI_TIMEOUT_HOURS. Only an explicit local run that
        # already sits on the PR branch keeps fixing the checkout directly.
        if _is_jules_pr(pr_data) and not already_on_pr_branch:
            stale_jules_result = _close_stale_jules_pr(github_client, repo_name, pr_data, config, github_checks)
            if stale_jules_result.closed:
                actions.extend(stale_jules_result.actions)
                return actions

            actions.append(f"PR #{pr_number} is a Jules-created PR, sending error logs to Jules session")
            # Send error logs to Jules and skip local fixing - let Jules handle it
            jules_feedback_actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)
            actions.extend(jules_feedback_actions)
            actions.append(f"Jules will handle fixing PR #{pr_number}, skipping local fixes")
            return actions

        # Step 5: Skip to process PR if it is dependabot PR
        if _is_dependabot_pr(pr_data):
            actions.append(f"PR #{pr_number} is a dependabot PR, skipping fixes")
            return actions

        # Step 6: Only PRs created by local LLM execution (or explicit local checkout) are fixed by local LLM
        if not _is_local_llm_pr(pr_data) and not already_on_pr_branch:
            actions.append(f"PR #{pr_number} was not created by local LLM, skipping local LLM fixes")
            return actions

        # Step 7: Checkout PR branch for non-Jules PRs
        # pr_branch_name is defined earlier (around line 1004)

        # Prepare branch (ensure fetched)
        prepare_ok = _checkout_pr_branch(repo_name, pr_data, config, perform_checkout=False)
        if not prepare_ok:
            actions.append(f"Failed to prepare PR #{pr_number} branch")
            return actions

        with BranchManager(pr_branch_name) as manager:
            actions.append(f"Checked out PR #{pr_number} branch")

            # Step 8: Optionally update with latest base branch commits (configurable)
            if config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL:
                actions.append(f"[Policy] Skipping base branch update for PR #{pr_number} (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=True)")
                get_trace_logger().log("Update Base", f"Skipped base branch update for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "skipped"})

                # Proceed directly to extracting GitHub Actions logs and attempting fixes
                if failed_checks:
                    github_logs, failed_test_files = _create_github_action_log_summary(repo_name, config, failed_checks)
                    fix_actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, github_logs, failed_test_files, skip_github_actions_fix=already_on_pr_branch)
                    actions.extend(fix_actions)
                else:
                    actions.append(f"No specific failed checks found for PR #{pr_number}")

                return actions
            else:
                actions.append(f"[Policy] Performing base branch update for PR #{pr_number} before fixes (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=False)")
                update_actions = _update_with_base_branch(repo_name, pr_data, config)
                actions.extend(update_actions)

                # Step 9: Check for special cases from base branch update

                # Check if LLM determined merge would degrade code quality
                if "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE" in update_actions:
                    actions.append(f"LLM determined merge would degrade code quality for PR #{pr_number}, closing PR without merge")
                    # Close the PR without merging
                    try:
                        client = GitHubClient.get_instance()
                        close_comment = f"Auto-Coder: Closing PR because LLM determined merge would degrade code quality. The linked issue(s) have been reopened with incremented attempt count."
                        client.close_pr(repo_name, pr_number, close_comment)
                        _remove_reviewer_sessions_for_closed_pr(repo_name, pr_number)
                        actions.append(f"Closed PR #{pr_number} without merging")

                        # BranchManager handles return to original branch
                    except Exception as e:
                        logger.error(f"Failed to close PR #{pr_number}: {e}")
                        actions.append(f"Error closing PR #{pr_number}: {e}")
                    return actions

                # If base branch update required pushing changes, skip to next PR
                if "ACTION_FLAG:SKIP_ANALYSIS" in update_actions or any("Pushed updated branch" in action for action in update_actions):
                    actions.append(f"Updated PR #{pr_number} with base branch, skipping to next PR for GitHub Actions check")
                    get_trace_logger().log("Update Base", f"Pushed updated branch for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"result": "pushed"})
                    return actions

                # Step 10: If no main branch updates were needed, the test failures are due to PR content
                # Get GitHub Actions error logs and ask Gemini to fix
                if any("up to date with" in action for action in update_actions):
                    actions.append(f"PR #{pr_number} is up to date with main branch, test failures are due to PR content")
                    get_trace_logger().log("Update Base", f"PR #{pr_number} is up to date", item_type="pr", item_number=pr_number, details={"result": "up_to_date"})

                    # Fix PR issues using GitHub Actions logs first, then local tests
                    if failed_checks:
                        # Unit test expects _get_github_actions_logs(repo_name, failed_checks)
                        github_logs = _get_github_actions_logs(repo_name, config, failed_checks, pr_data)  # type: ignore[arg-type]
                        fix_actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, github_logs, skip_github_actions_fix=already_on_pr_branch)
                        actions.extend(fix_actions)
                    else:
                        actions.append(f"No specific failed checks found for PR #{pr_number}")
                else:
                    # If we reach here, some other update action occurred
                    actions.append(f"PR #{pr_number} processing completed")

    except Exception as e:
        actions.append(f"Error handling PR merge for PR #{pr_number}: {e}")

    return actions


def _checkout_pr_branch(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig, perform_checkout: bool = True) -> bool:
    """Checkout the PR branch for local testing.

    If config.FORCE_CLEAN_BEFORE_CHECKOUT is True, forcefully discard any local changes
    before checkout (git reset --hard + git clean -fd).
    """
    pr_number = pr_data["number"]

    try:
        # Step 1: Optionally reset any local changes and clean untracked files
        if config.FORCE_CLEAN_BEFORE_CHECKOUT:
            log_action(f"Forcefully cleaning workspace before checkout PR #{pr_number}")

            # Reset any staged/unstaged changes
            reset_result = cmd.run_command(["git", "reset", "--hard", "HEAD"])
            if not reset_result.success:
                log_action(
                    f"Warning: git reset failed for PR #{pr_number}",
                    False,
                    reset_result.stderr,
                )

            # Clean untracked files and directories
            clean_result = cmd.run_command(["git", "clean", "-fd"])
            if not clean_result.success:
                log_action(
                    f"Warning: git clean failed for PR #{pr_number}",
                    False,
                    clean_result.stderr,
                )

        # Step 2: Try manual fetch and checkout (fallback is redundant now but keeps logic similar)
        log_action(f"Direct checkout failed for PR #{pr_number}, trying alternative approach", False)
        return _force_checkout_pr_manually(repo_name, pr_data, config, perform_checkout)

    except Exception as e:
        logger.error(f"Error checking out PR #{pr_number}: {e}")
        return False


def _force_checkout_pr_manually(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig, perform_checkout: bool = True) -> bool:
    """Manually fetch and checkout PR branch as fallback."""
    pr_number = pr_data["number"]

    try:
        # Get PR branch information from PR data
        branch_name = pr_data.get("head_branch") or pr_data.get("head", {}).get("ref")
        if not branch_name:
            log_action(f"Cannot determine branch name for PR #{pr_number}", False, "No head.ref in PR data")
            return False

        log_action(f"Attempting manual checkout of branch '{branch_name}' for PR #{pr_number}")

        # Clean up any existing merge conflicts before checkout
        log_action(f"Cleaning up workspace before checkout PR #{pr_number}")

        # Abort any ongoing merge
        abort_result = cmd.run_command(["git", "merge", "--abort"])
        # Ignore errors - there might not be a merge in progress

        # Reset any staged/unstaged changes
        reset_result = cmd.run_command(["git", "reset", "--hard", "HEAD"])
        if not reset_result.success:
            log_action(f"Warning: git reset failed for PR #{pr_number}", False, reset_result.stderr)

        # Clean untracked files and directories
        clean_result = cmd.run_command(["git", "clean", "-fd"])
        if not clean_result.success:
            log_action(f"Warning: git clean failed for PR #{pr_number}", False, clean_result.stderr)

        # Fetch the PR branch directly
        fetch_result = cmd.run_command(["git", "fetch", "origin", f"{branch_name}:{branch_name}"])
        if not fetch_result.success:
            # Try fetching from pull request ref
            fetch_result = cmd.run_command(["git", "fetch", "origin", f"pull/{pr_number}/head"])
            if not fetch_result.success:
                log_action(f"Failed to fetch PR #{pr_number} branch", False, fetch_result.stderr)
                return False

        # Checkout the branch
        if perform_checkout:
            checkout_result = cmd.run_command(["git", "checkout", branch_name])
            if not checkout_result.success:
                # If branch doesn't exist locally, checkout from fetched ref
                checkout_result = cmd.run_command(["git", "checkout", "-b", branch_name, "FETCH_HEAD"])

                if not checkout_result.success:
                    log_action(
                        f"Failed to checkout branch '{branch_name}' for PR #{pr_number}",
                        False,
                        checkout_result.stderr,
                    )
                    return False
        else:
            # If not checking out, ensure the branch exists/updates from the fetched head
            # If we fetched to branch_name:branch_name, it's already updated.
            # If we fetched to FETCH_HEAD (fallback), we need to update/create the local branch.
            if not fetch_result.success and "FETCH_HEAD" in str(fetch_result.stdout or ""):
                # This logic is tricky because we rely on previous fetch_result variable which might be from branch:branch attempt.
                pass

            # The structure above tries branch:branch first.
            # checks: fetch_result = ... branch:branch
            # if not fetch_result.success: fetch_result = ... pull/N/head

            # Re-evaluating fetch logic to account for perform_checkout=False
            pass

        # NOTE: The block above was complex. Re-implementing clearer logic for finish.

        if not perform_checkout:
            # Logic to ensure branch ref exists if we fetched to FETCH_HEAD
            # If branch:branch succeeded, the branch ref is updated.
            # If pull/N/head succeeded, we need to create/update local branch ptr.

            # We can't easily know which path succeeded without checking return codes or logic flow.
            # But we know at least one Fetch Succeeded if we reached here (wait, we didn't check success properly in original code flow?
            # Original code: if not fetch (branch:branch): if not fetch (pull): return False.
            # So if we are here, we fetched successfully.

            # If branch:branch failed, we used pull/N/head.
            # So we verify if branch exists?

            verify = cmd.run_command(["git", "rev-parse", "--verify", branch_name])
            if not verify.success:
                # It must have been the FETCH_HEAD case or branch didn't exist before.
                # Create/Update it.
                cmd.run_command(["git", "branch", "-f", branch_name, "FETCH_HEAD"])
            return True

        checkout_result = cmd.run_command(["git", "checkout", branch_name])
        if not checkout_result.success:
            # If branch doesn't exist locally, checkout from fetched ref
            checkout_result = cmd.run_command(["git", "checkout", "-b", branch_name, "FETCH_HEAD"])

            if not checkout_result.success:
                log_action(
                    f"Failed to checkout branch '{branch_name}' for PR #{pr_number}",
                    False,
                    checkout_result.stderr,
                )
                return False

        log_action(f"Successfully manually checked out PR #{pr_number}")
        return True

    except Exception as e:
        logger.error(f"Error manually checking out PR #{pr_number}: {e}")
        return False


def _update_with_base_branch(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> List[str]:
    """Update PR branch with latest base branch commits.

    This function merges the PR's base branch (e.g., main, develop) into the PR branch
    to bring it up to date before attempting fixes.
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        # Determine target base branch for this PR
        target_branch = pr_data.get("base_branch") or pr_data.get("base", {}).get("ref") or config.MAIN_BRANCH

        # Fetch latest changes from origin
        result = cmd.run_command(["git", "fetch", "origin"])
        if not result.success:
            actions.append(f"Failed to fetch latest changes: {result.stderr}")
            return actions

        # Check if base branch has new commits
        result = cmd.run_command(["git", "rev-list", "--count", f"HEAD..refs/remotes/origin/{target_branch}"])
        if not result.success:
            actions.append(f"Failed to check {target_branch} branch status: {result.stderr}")
            return actions

        commits_behind = int(result.stdout.strip())
        if commits_behind == 0:
            actions.append(f"PR #{pr_number} is up to date with {target_branch} branch")
            return actions

        actions.append(f"PR #{pr_number} is {commits_behind} commits behind {target_branch}, updating...")

        # Try to merge base branch
        result = cmd.run_command(["git", "merge", f"refs/remotes/origin/{target_branch}"])
        if result.success:
            actions.append(f"Successfully merged {target_branch} branch into PR #{pr_number}")

            # Push the updated branch using centralized helper with retry
            push_result = git_push()
            if push_result.success:
                actions.append(f"Pushed updated branch for PR #{pr_number}")
                # Signal to skip further LLM analysis for this PR in this run
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
                import time

                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    actions.append(f"Pushed updated branch for PR #{pr_number} (after retry)")
                    actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                else:
                    logger.error(f"Failed to push updated branch after retry: {retry_push_result.stderr}")
                    logger.error("Exiting application due to git push failure")
                    sys.exit(1)
        else:
            # Merge conflict occurred, check if Jules PR
            actions.append(f"Merge conflict detected for PR #{pr_number}")

            if _is_jules_pr(pr_data):
                actions.append(f"PR #{pr_number} is a Jules PR with merge conflicts. Requesting Jules to resolve it.")
                try:
                    from auto_coder.jules_client import JulesClient

                    jules_client = JulesClient()
                    session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))
                    if session_id:
                        prompt = render_prompt("pr.jules_merge_conflict_resolution")
                        jules_client.send_message(session_id, prompt)
                        actions.append(f"Requested Jules to resolve merge conflict in session {session_id}")
                        # We return here so we don't proceed with LLM fixing this PR right now
                        actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                        return actions
                    else:
                        actions.append(f"Jules PR #{pr_number} has merge conflicts but no session ID found. Cannot delegate.")
                except Exception as e:
                    actions.append(f"Error requesting Jules to resolve conflict: {e}")

            # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
            # the bot recreates the PR against the updated base branch by itself.
            if _is_dependabot_pr(pr_data):
                actions.append(f"PR #{pr_number} is a dependency-bot PR with merge conflicts. Skipping conflict resolution.")
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            if _delegate_cloud_merge_conflict_repair(repo_name, pr_data):
                cmd.run_command(["git", "merge", "--abort"])
                actions.append(f"Delegated merge-conflict repair for PR #{pr_number} to its existing cloud session")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            if not _is_local_llm_pr(pr_data):
                actions.append(f"PR #{pr_number} was not created by local LLM, skipping conflict resolution.")
                cmd.run_command(["git", "merge", "--abort"])
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
                return actions

            # Use the common subroutine for conflict resolution
            from .conflict_resolver import _perform_base_branch_merge_and_conflict_resolution, scan_conflict_markers

            conflict_resolved = _perform_base_branch_merge_and_conflict_resolution(
                pr_number,
                target_branch,
                config,
                pr_data,
                repo_name,
            )

            if conflict_resolved:
                actions.append(f"Successfully resolved merge conflicts for PR #{pr_number}")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                # Check if conflicts are still present (indicating LLM determined degradation)
                remaining_conflicts = scan_conflict_markers()
                if remaining_conflicts:
                    actions.append(f"LLM determined merge would degrade code quality for PR #{pr_number}, skipping merge attempt")
                    actions.append("ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE")
                else:
                    actions.append(f"Failed to resolve merge conflicts for PR #{pr_number}")

    except Exception as e:
        actions.append(f"Error updating with base branch for PR #{pr_number}: {e}")

    return actions


def _extract_session_id_from_pr_body(pr_body: str) -> Optional[str]:
    """Extract Session ID from PR body by looking for session links.

    Looks for patterns like:
    - Session ID: abc123
    - Session: abc123
    - GitHub PR URL: https://github.com/owner/repo/pull/123
    - URLs with session parameters

    Args:
        pr_body: PR description/body text

    Returns:
        Session ID if found, None otherwise
    """
    if not pr_body:
        return None

    # Pattern 1: Look for "Session ID:" or "Session:" followed by the session ID
    # This captures either a simple alphanumeric ID or a URL
    session_pattern = r"(?:session\s*id:|session:)\s*(.+?)(?:\n|$)"
    match = re.search(session_pattern, pr_body, re.IGNORECASE)
    if match:
        session_id = match.group(1).strip()
        # If the captured text contains a GitHub PR URL, use that
        github_url_in_session = re.search(r"https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/pull/\d+", session_id)
        if github_url_in_session:
            session_id = github_url_in_session.group(0)
            logger.debug(f"Found session ID pattern 1 (URL): {session_id}")
            return session_id

        # Allow alphanumeric session IDs (some tests use them)
        logger.debug(f"Found session ID pattern 1: {session_id}")
        return session_id

    # Pattern 2: Look for URLs that might contain session IDs
    # Common patterns: ?session=abc123, &session_id=abc123
    url_session_pattern = r"(?:session(?:_id)?=)([a-zA-Z0-9-_]+)"
    match = re.search(url_session_pattern, pr_body, re.IGNORECASE)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 2: {session_id}")
        return session_id

    # Pattern 3: Look for Jules session URLs (e.g., https://jules.google.com/session/901463134778726610)
    # This is the real Jules session URL format and must be checked before the
    # GitHub PR URL fallback below, otherwise a self-referencing PR link would be
    # mistaken for a session ID and passed to the Jules API, which always 404s.
    jules_session_url_pattern = r"jules\.google\.com/session/([a-zA-Z0-9-_]+)"
    match = re.search(jules_session_url_pattern, pr_body)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 3 (Jules Session URL): {session_id}")
        return session_id

    # Pattern 3a: Look for Claude Routine session URLs (e.g., https://claude.ai/code/session_01HJKLMNOPQRSTUVWXYZ)
    claude_session_url_pattern = r"claude\.ai/code/([a-zA-Z0-9-_]+)"
    match = re.search(claude_session_url_pattern, pr_body)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 3a (Claude Session URL): {session_id}")
        return session_id

    # Pattern 3b: Look for GitHub PR URLs (e.g., https://github.com/owner/repo/pull/123)
    # This pattern matches the full URL and extracts it as the session ID
    github_url_pattern = r"https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/pull/\d+"
    match = re.search(github_url_pattern, pr_body)
    if match:
        session_id = match.group(0).strip()
        logger.debug(f"Found session ID pattern 3b (GitHub PR URL): {session_id}")
        return session_id

    # Pattern 3c: Look for Codex Cloud task URLs (e.g., https://chatgpt.com/codex/tasks/task_01HJKLMNOPQRSTUVWXYZ)
    codex_session_url_pattern = r"(?:chatgpt\.com|chat\.openai\.com|[^\s/]+)/codex/tasks/(task_[a-zA-Z0-9_-]+)"
    match = re.search(codex_session_url_pattern, pr_body, re.IGNORECASE)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 3c (Codex Task URL): {session_id}")
        return session_id

    # Pattern 4: Look for Jules Task IDs (e.g., jules.google.com/task/12345 or "task 12345")
    # This is treated as a session ID
    task_url_pattern = r"jules\.google\.com/task/(\d+)"
    match = re.search(task_url_pattern, pr_body)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 4 (Jules Task URL): {session_id}")
        return session_id

    task_id_pattern = r"\btask\s+(\d+)\b"
    match = re.search(task_id_pattern, pr_body, re.IGNORECASE)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 5 (Jules Task ID): {session_id}")
        return session_id

    # Pattern 6: Look for standalone session IDs starting with "session_"
    # e.g., session_12345, session_abc-def
    # Must have a suffix beyond just "session_id" to avoid matching JSON keys.
    # We require at least one character after "session_" that isn't just "id" unless it's longer.
    session_prefix_pattern = r"\b(session_(?!id\b)[a-zA-Z0-9-_]+)\b"
    match = re.search(session_prefix_pattern, pr_body)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 6 (session_ prefix): {session_id}")
        return session_id

    # Pattern 7: Look for standalone Codex task IDs (e.g., task_e_...)
    codex_task_pattern = r"\b(task_[a-zA-Z0-9_-]+)\b"
    match = re.search(codex_task_pattern, pr_body)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 7 (Codex task_ prefix): {session_id}")
        return session_id

    logger.debug("No session ID found in PR body")
    return None


def _find_issue_by_session_id_in_comments(repo_name: str, session_id: str, github_client: Any) -> Optional[int]:
    """Find issue number by searching for session ID using GitHub Search API."""
    try:
        # Use GitHub Search API for efficiency
        # Query: repo:owner/repo "session_id" type:issue
        # We search specifically for the session_id string
        query = f"repo:{repo_name} {session_id} type:issue"
        logger.info(f"Searching for session ID '{session_id}' with query: '{query}'")

        # Use the new search_issues method
        # We only check the top 5 results to avoid indefinite processing if search returns many loose matches
        search_results = github_client.search_issues(query)

        # Iterate safely over the generator/list
        count = 0
        for issue in search_results:
            if count >= 5:
                break
            count += 1

            # Helper to get attributes from dict or object (GhApi returns AttrDict usually)
            def get_attr(obj, attr):
                return getattr(obj, attr, None) or (obj.get(attr) if isinstance(obj, dict) else None)

            issue_number = get_attr(issue, "number")
            issue_body = get_attr(issue, "body")

            # Double check if session_id is actually in body or comments to be sure
            # Search API might return loose matches, although exact string match usually ranks high
            if issue_body and session_id in issue_body:
                logger.info(f"Found session ID '{session_id}' in body of issue #{issue_number}")
                return issue_number

            # Check comments
            # This is still an API call per issue, but we only do it for a few candidates
            try:
                comments = github_client.get_issue_comments(repo_name, issue_number)
                for comment in comments:
                    comment_body = comment.get("body")
                    if comment_body and session_id in comment_body:
                        logger.info(f"Found session ID '{session_id}' in comment of issue #{issue_number}")
                        return issue_number
            except Exception as e:
                logger.warning(f"Failed to fetch comments for potential issue #{issue_number}: {e}")

        logger.warning(f"Session ID '{session_id}' not found via search query")
        return None
    except Exception as e:
        logger.error(f"Error searching for session ID in comments: {e}")
        return None


def _update_jules_pr_body(
    repo_name: str,
    pr_number: int,
    pr_body: str,
    issue_number: int,
    github_client: Any,
) -> bool:
    """Update Jules PR body to include close #<issue_number> and link to issue.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number
        pr_body: Current PR body text
        issue_number: Issue number to link to
        github_client: GitHub client instance

    Returns:
        True if PR body was updated successfully, False otherwise
    """
    try:
        # Check if PR body already has the close reference
        if f"close #{issue_number}" in pr_body.lower() or f"closes #{issue_number}" in pr_body.lower():
            logger.info(f"PR #{pr_number} body already references issue #{issue_number}, skipping update")
            return True

        # Create the issue link
        issue_link = f"https://github.com/{repo_name}/issues/{issue_number}"
        close_statement = f"close #{issue_number}"

        # Build new PR body
        separator = "\n\n" if pr_body and not pr_body.endswith("\n") else "\n"
        new_body = f"{pr_body}{separator}{close_statement}\n\nRelated issue: {issue_link}"

        # Update PR body via GitHub Client
        try:
            from auto_coder.util.gh_cache import GitHubClient, get_ghapi_client

            # Use github_client for API call if it's a real client with valid token
            # and has the necessary methods. Otherwise, use get_ghapi_client.
            token = getattr(github_client, "token", None)

            # Prefer get_ghapi_client when a valid string token is provided
            if isinstance(token, str):
                api = get_ghapi_client(token)
                owner, repo_name_split = repo_name.split("/")
                # Validate issue references in new body
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_name_split, pr_number, body=new_body)
            elif hasattr(github_client, "get_repository"):
                # Fallback to direct client methods
                repo = github_client.get_repository(repo_name)
                pr = repo.get_pull(pr_number)
                # Validate issue references in new body
                validate_issue_references(new_body, github_client, repo_name)
                pr.edit(body=new_body)
            else:
                # Last resort: try singleton token
                token = GitHubClient.get_instance().token
                api = get_ghapi_client(token)
                owner, repo_name_split = repo_name.split("/")
                # Validate issue references in new body
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_name_split, pr_number, body=new_body)

            logger.info(f"Updated PR #{pr_number} body to include reference to issue #{issue_number}")
            log_action(f"Updated PR #{pr_number} body with close #{issue_number} reference")
            return True
        except Exception as e:
            try:
                logger.error(f"Failed to update PR #{pr_number} body: {str(e)}")
            except Exception:
                pass  # Prevent logging failures from affecting the result
            return False

    except Exception as e:
        try:
            logger.error(f"Error updating Jules PR #{pr_number} body: {str(e)}")
        except Exception:
            pass  # Prevent logging failures from affecting the result
        return False


def _is_codex_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Codex based on session/task URL in PR body."""
    pr_author = get_pr_author_login(pr_data) or ""
    if pr_author.lower().startswith("codex"):
        return True

    pr_body = pr_data.get("body", "") or ""
    if not pr_body:
        return False

    # Check for Codex task / session URLs
    if re.search(r"https?://(?:chatgpt\.com|chat\.openai\.com|[^\s/]+)/codex/tasks/[a-zA-Z0-9_-]+", pr_body, re.IGNORECASE):
        return True
    if "/codex/tasks/" in pr_body:
        return True

    return False


def _is_claude_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Claude based on session URL in PR body."""
    pr_author = get_pr_author_login(pr_data) or ""
    if pr_author.lower().startswith("claude"):
        return True

    pr_body = pr_data.get("body", "") or ""
    if not pr_body:
        return False

    # Check for Claude Routine / Code session URLs
    if re.search(r"https?://claude\.ai/code/[a-zA-Z0-9_-]+", pr_body, re.IGNORECASE) or "claude.ai/code/" in pr_body:
        return True
    if re.search(r"\bClaude session\b", pr_body, re.IGNORECASE):
        return True

    return False


def _is_codex_or_claude_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Codex or Claude based on session URL in PR body."""
    return _is_codex_pr(pr_data) or _is_claude_pr(pr_data)


def _find_codex_cloud_task_for_issue(
    repo_name: str,
    issue_number: int,
    github_client: Optional[Any] = None,
) -> Optional[str]:
    """Find the Codex Cloud task URL for an issue if it was processed by Codex Cloud.

    Args:
        repo_name: Repository name (owner/repo)
        issue_number: GitHub issue number
        github_client: Optional GitHub client instance

    Returns:
        Codex Cloud task URL if found, None otherwise
    """
    try:
        # 1. Check CloudManager
        cloud_manager = CloudManager(repo_name)
        session_id = cloud_manager.get_session_id(issue_number)
        if session_id:
            if session_id.startswith("http") and "codex/tasks" in session_id:
                return session_id
            if re.match(r"^task_[a-zA-Z0-9_-]+$", session_id):
                return f"https://chatgpt.com/codex/tasks/{session_id}"

        # 2. Check comments on the issue if github_client is available
        if github_client:
            try:
                comments = github_client.get_issue_comments(repo_name, issue_number)
                for comment in comments:
                    comment_body = comment.get("body", "") or ""
                    # Check for direct URL in comment
                    url_match = re.search(r"(https?://[^\s]+/codex/tasks/[a-zA-Z0-9_-]+)", comment_body)
                    if url_match:
                        return url_match.group(1)

                    # Check for "Codex Cloud task ... Task ID: <id>"
                    task_match = re.search(r"Codex Cloud task.*?Task ID:\s*(task_[a-zA-Z0-9_-]+)", comment_body, re.IGNORECASE | re.DOTALL)
                    if task_match:
                        return f"https://chatgpt.com/codex/tasks/{task_match.group(1)}"
            except Exception as e:
                logger.debug(f"Failed to fetch comments for issue #{issue_number}: {e}")

        return None
    except Exception as e:
        logger.error(f"Error finding Codex Cloud task for issue #{issue_number}: {e}")
        return None


def _link_codex_cloud_pr_to_issue(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> bool:
    """If PR body contains 'Closes #xxx' and issue #xxx was processed by Codex Cloud,
    append the Codex Cloud URL to the PR body.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        True if updated or no update needed, False on error
    """
    try:
        pr_number = pr_data.get("number")
        pr_body = pr_data.get("body", "") or ""

        # Extract linked issues from PR body using linking keywords (close, closes, fix, etc.)
        linked_issues = extract_linked_issues_from_pr_body(pr_body)
        if not linked_issues:
            # Also check direct regex for Closes #xxx just in case
            matches = re.findall(r"\b(?:close|closes|closed|closing|fix|fixes|fixed|resolve|resolves|resolved)\s*#(\d+)", pr_body, re.IGNORECASE)
            linked_issues = [int(m) for m in matches]

        if not linked_issues:
            return True

        urls_to_append: List[str] = []
        for issue_number in linked_issues:
            codex_url = _find_codex_cloud_task_for_issue(repo_name, issue_number, github_client)
            if codex_url and codex_url not in pr_body and codex_url not in urls_to_append:
                urls_to_append.append(codex_url)

        if not urls_to_append:
            return True

        # Append URLs to PR body
        separator = "\n\n" if pr_body and not pr_body.endswith("\n") else "\n"
        new_body = f"{pr_body}{separator}" + "\n\n".join(urls_to_append)

        # Update PR body on GitHub
        try:
            from auto_coder.util.gh_cache import GitHubClient, get_ghapi_client

            token = getattr(github_client, "token", None)
            if isinstance(token, str):
                api = get_ghapi_client(token)
                owner, repo_split = repo_name.split("/")
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_split, pr_number, body=new_body)
            elif hasattr(github_client, "get_repository"):
                repo = github_client.get_repository(repo_name)
                pr = repo.get_pull(pr_number)
                validate_issue_references(new_body, github_client, repo_name)
                pr.edit(body=new_body)
            else:
                token = GitHubClient.get_instance().token
                api = get_ghapi_client(token)
                owner, repo_split = repo_name.split("/")
                validate_issue_references(new_body, github_client, repo_name)
                api.pulls.update(owner, repo_split, pr_number, body=new_body)

            pr_data["body"] = new_body
            logger.info(f"Updated PR #{pr_number} body to include Codex Cloud URL(s): {', '.join(urls_to_append)}")
            log_action(f"Updated PR #{pr_number} body with Codex Cloud URL(s)")
            return True
        except Exception as e:
            logger.error(f"Failed to update PR #{pr_number} body with Codex Cloud URL: {e}")
            return False

    except Exception as e:
        logger.error(f"Error linking Codex Cloud PR #{pr_data.get('number')}: {e}")
        return False


def _is_jules_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Jules (google-labs-jules).

    Args:
        pr_data: PR data dictionary

    Returns:
        True if the PR is created by Jules, False otherwise
    """
    # Codex or Claude PRs should never be treated as Jules
    if _is_codex_or_claude_pr(pr_data):
        return False

    # Check author first
    pr_author = get_pr_author_login(pr_data) or ""
    if pr_author.startswith("claude") or pr_author.startswith("codex"):
        return False
    if pr_author.startswith("google-labs-jules"):
        return True

    # Fallback: Check if PR body contains a valid Jules session reference
    pr_body = pr_data.get("body", "") or ""
    if not pr_body:
        return False

    # Jules URL indicators
    if re.search(r"jules\.google\.com/(?:session|task)/", pr_body) or re.search(r"\bJules session\b", pr_body, re.IGNORECASE):
        return True

    # Check for Session ID format without Claude, Codex, or generic GitHub URL
    session_id = _extract_session_id_from_pr_body(pr_body)
    if session_id:
        if "claude.ai" in session_id or "github.com" in session_id or "codex" in session_id:
            return False
        # Only treat as Jules session if "Session ID:" or "Session:" is explicitly in body
        session_pattern = r"(?:session\s*id:|session:)\s*(.+?)(?:\n|$)"
        if re.search(session_pattern, pr_body, re.IGNORECASE):
            return True

    return False


def _is_local_llm_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR was created by local LLM execution.

    Returns True if:
    1. The PR is not from Jules, Codex Cloud, Claude Routine, or Dependabot/bots.
    2. And either:
       - PR body contains the explicit local marker `<!-- auto-coder:local-llm -->` or `<!-- auto-coder:local -->`.
       - PR head branch matches the Auto-Coder work branch pattern (e.g. `issue-<number>`, `issue-<number>_attempt-<attempt>`, `issue-<number>/attempt-<attempt>`).
       - PR body contains standard Auto-Coder text like `This PR addresses issue #` or `Auto-Coder: Address issue #`.
    """
    if not pr_data:
        return False

    # Exclude cloud LLM PRs and dependency bots
    if _is_jules_pr(pr_data):
        return False
    if _is_codex_or_claude_pr(pr_data):
        return False
    if _is_dependabot_pr(pr_data):
        return False

    pr_body = pr_data.get("body", "") or ""

    # 1. Check explicit local LLM markers
    if "<!-- auto-coder:local-llm -->" in pr_body or "<!-- auto-coder:local -->" in pr_body:
        return True

    # 2. Check head branch name
    head_branch = pr_data.get("head_branch") or (pr_data.get("head") or {}).get("ref") or ""
    if head_branch:
        # Pattern matches: issue-123, issue-123_attempt-1, issue-123/attempt-1
        if re.match(r"^issue-\d+(?:[_/]attempt-\d+)?$", head_branch):
            return True

    # 3. Check standard PR body signatures for Auto-Coder local issue PRs
    if re.search(r"\bThis PR addresses issue #\d+", pr_body):
        return True
    if "Auto-Coder: Address issue #" in pr_body:
        return True

    return False


is_local_llm_pr = _is_local_llm_pr


def _resolve_jules_pr_issue_number(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> Optional[int]:
    """Find the issue a Jules PR was created for.

    The lookup order is session ID (local DB, then issue comments), branch name,
    and finally the PR title.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        Issue number if found, None otherwise
    """
    pr_number = pr_data.get("number")
    pr_body = pr_data.get("body", "") or ""

    session_id = _extract_session_id_from_pr_body(pr_body)

    issue_number: Optional[int] = None
    if session_id:
        logger.info(f"Extracted session ID '{session_id}' from Jules PR #{pr_number}")
        # Store session_id in pr_data for later use in the feedback loop
        pr_data["_jules_session_id"] = session_id

        # Use CloudManager to find the original issue number
        cloud_manager = CloudManager(repo_name)
        issue_number = cloud_manager.get_issue_by_session(session_id)

        if not issue_number:
            logger.warning(f"No issue found for session ID '{session_id}' in local DB. Searching comments...")
            issue_number = _find_issue_by_session_id_in_comments(repo_name, session_id, github_client)
    else:
        logger.warning(f"No session ID found in Jules PR #{pr_number} body")

    # Fallback: Extract from branch name
    if not issue_number:
        branch_name = pr_data.get("head", {}).get("ref", "")
        if branch_name:
            # Match patterns like issue-123
            match = re.search(r"\bissue[-_](\d+)\b", branch_name, re.IGNORECASE)
            if match:
                issue_number = int(match.group(1))
                logger.info(f"Extracted issue #{issue_number} from branch name '{branch_name}'")

    # Fallback: Extract from PR title
    if not issue_number:
        pr_title = pr_data.get("title", "")
        if pr_title:
            # Match patterns like "Issue #123" or "Fix #123"
            match = re.search(r"(?:issue|fix|close|resolve)s?\s*#(\d+)", pr_title, re.IGNORECASE)
            if match:
                issue_number = int(match.group(1))
                logger.info(f"Extracted issue #{issue_number} from PR title '{pr_title}'")

    return issue_number


def _link_jules_pr_to_issue(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> bool:
    """Process a Jules, Claude Code, or session PR to detect session ID and update PR body.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        github_client: GitHub client instance

    Returns:
        True if PR body was updated successfully, False otherwise
    """
    try:
        pr_number = pr_data["number"]
        pr_body = pr_data.get("body", "") or ""
        pr_author = pr_data.get("user", {}).get("login", "")

        is_jules = _is_jules_pr(pr_data)
        is_claude = "claude" in pr_author.lower() or "claude.ai/code/" in pr_body or bool(re.search(r"\bClaude session\b", pr_body, re.IGNORECASE))
        has_session = bool(re.search(r"claude\.ai/code/|jules\.google\.com/(?:session|task)/|\bsession_[a-zA-Z0-9-_]+\b", pr_body, re.IGNORECASE))

        # If not a Jules PR, has no session indicator, and is not a Claude PR, skip
        if not is_jules and not is_claude and not has_session:
            logger.debug(f"PR #{pr_number} has no session ID or cloud author, skipping session issue linking")
            return True  # Not an error, just not a session PR

        logger.info(f"Processing session PR #{pr_number} by {pr_author}")

        # Check for special Jules PRs that don't need issue linking
        pr_title = pr_data.get("title", "")
        special_prefixes = ["🛡️ Sentinel: ", "🎨 Palette: ", "⚡ Bolt: "]
        if any(pr_title.startswith(prefix) for prefix in special_prefixes):
            logger.info(f"Skipping issue lookup for Jules special PR #{pr_number} ('{pr_title}')")
            return True

        issue_number = _resolve_jules_pr_issue_number(repo_name, pr_data, github_client)

        if not issue_number:
            logger.warning(f"No issue found for session PR #{pr_number} (checked session, branch, and title)")
            return False

        logger.info(f"Found issue #{issue_number} for session PR #{pr_number}")

        # Step 4: Update PR body to include close #<issue_number> and link to issue
        success = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        if success:
            logger.info(f"Successfully processed PR #{pr_number}, updated body to reference issue #{issue_number}")
            # Update local pr_data body so downstream logic in the same run has the updated body
            if f"close #{issue_number}" not in pr_body.lower() and f"closes #{issue_number}" not in pr_body.lower():
                separator = "\n\n" if pr_body and not pr_body.endswith("\n") else "\n"
                pr_data["body"] = f"{pr_body}{separator}close #{issue_number}\n\nRelated issue: https://github.com/{repo_name}/issues/{issue_number}"
        else:
            logger.error(f"Failed to update PR #{pr_number} body")

        return success

    except Exception as e:
        logger.error(f"Error processing session PR {pr_data.get('number', 'unknown')}: {e}")
        return False


# Alias for backwards compatibility with tests
_process_jules_pr = _link_jules_pr_to_issue


def _close_linked_issues(repo_name: str, pr_number: int, github_client: Optional[Any] = None) -> None:
    """Close issues linked in the PR body after successful merge.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number that was merged
        github_client: Optional GitHubClient instance
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        client = github_client or GitHubClient.get_instance()
        token = getattr(client, "token", None) or GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # Get PR body
        try:
            pr_info = api.pulls.get(owner, repo, pr_number)
            pr_body = pr_info.get("body", "") or ""
        except Exception as e:
            logger.debug(f"Could not retrieve PR #{pr_number} body for issue linking: {e}")
            return

        # Extract linked issues
        linked_issues = extract_linked_issues_from_pr_body(pr_body)

        if not linked_issues:
            # Fallback: resolve from session ID (Jules, Claude Code, etc.), branch name, or title
            resolved_issue = _resolve_jules_pr_issue_number(repo_name, pr_info, client)
            if resolved_issue:
                logger.info(f"Resolved issue #{resolved_issue} from session/branch/title for merged PR #{pr_number}")
                linked_issues = [resolved_issue]

        if not linked_issues:
            logger.debug(f"No linked issues found in PR #{pr_number} body")
            return

        # Close each linked issue
        for issue_num in linked_issues:
            try:
                # Add comment
                try:
                    api.issues.create_comment(owner, repo, issue_num, body=f"Closed by PR #{pr_number}")
                except Exception as e:
                    logger.warning(f"Failed to comment on issue #{issue_num}: {e}")

                # Close issue
                api.issues.update(owner, repo, issue_num, state="closed")

                logger.info(f"Closed issue #{issue_num} linked from PR #{pr_number}")
                log_action(f"Closed issue #{issue_num} (linked from PR #{pr_number})")
            except Exception as e:
                logger.warning(f"Error closing issue #{issue_num}: {e}")

    except Exception as e:
        logger.warning(f"Error processing linked issues for PR #{pr_number}: {e}")


def _archive_jules_session(repo_name: str, pr_number: int) -> None:
    """Archive Jules session for Jules-created PRs after successful merge.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number that was merged
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # Get PR data to check if it's a Jules PR and extract session ID
        try:
            pr_data = api.pulls.get(owner, repo, pr_number)
        except Exception as e:
            logger.debug(f"Could not retrieve PR #{pr_number} data for Jules session archiving: {e}")
            return

        pr_author = pr_data.get("user", {}).get("login", "")
        pr_body = pr_data.get("body", "")

        # Check if this is a Jules-created PR
        if pr_author != "google-labs-jules":
            logger.debug(f"PR #{pr_number} is not created by Jules ({pr_author}), skipping session archiving")
            return

        # Extract session ID from PR body
        session_id = _extract_session_id_from_pr_body(pr_body)
        if not session_id:
            logger.warning(f"No session ID found in Jules PR #{pr_number} body")
            return

        # Archive the Jules session
        try:
            from .jules_client import JulesClient

            jules_client = JulesClient()
            success = jules_client.archive_session(session_id)

            if success:
                logger.info(f"Archived Jules session '{session_id}' for PR #{pr_number}")
                log_action(f"Archived Jules session for PR #{pr_number}")
                # Check and restart recurrent tasks
                from .jules_engine import check_and_restart_recurrent_jules_task_for_pr

                check_and_restart_recurrent_jules_task_for_pr(repo_name, pr_number, session_id)
            else:
                logger.warning(f"Failed to archive Jules session '{session_id}' for PR #{pr_number}")
        except Exception as e:
            logger.warning(f"Error archiving Jules session for PR #{pr_number}: {e}")

    except Exception as e:
        logger.warning(f"Error processing Jules session archiving for PR #{pr_number}: {e}")


def _send_jules_error_feedback(
    repo_name: str,
    pr_data: Dict[str, Any],
    failed_checks: List[Dict[str, Any]],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> List[str]:
    """Send CI error logs to Jules session for Jules-created PRs.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        failed_checks: List of failed GitHub Actions checks
        config: AutomationConfig instance
        github_client: Optional GitHub client instance

    Returns:
        List of action strings describing what was done
    """
    actions = []
    pr_number = pr_data["number"]

    # Never send error feedback to Jules for PRs created by Codex or Claude
    if _is_codex_or_claude_pr(pr_data):
        logger.info(f"PR #{pr_number} is created by Codex/Claude, skipping Jules error feedback")
        return [f"Skipped Jules error feedback for PR #{pr_number} (created by Codex/Claude)"]

    try:
        # Get the session ID from pr_data
        session_id = pr_data.get("_jules_session_id")
        if not session_id:
            session_id = _extract_session_id_from_pr_body(pr_data.get("body", ""))

        if not session_id:
            actions.append(f"Cannot send error feedback to Jules for PR #{pr_number}: no session ID found")
            try:
                logger.error(f"No session ID found in PR #{pr_number} data for Jules error feedback")
            except Exception:
                pass  # Prevent logging failures from affecting the result
            return actions

        # Get GitHub Actions error logs
        github_logs = _get_github_actions_logs(repo_name, config, failed_checks, pr_data)

        # Format the message to send to Jules
        message = f"""CI checks failed for PR #{pr_number} in {repo_name}.

Please review and fix the following errors:

{github_logs}

PR Title: {pr_data.get('title', 'Unknown')}
PR Author: {pr_data.get('user', {}).get('login', 'Unknown')}
"""

        # Import JulesClient here to avoid circular imports
        from .jules_client import JulesClient

        # Send the error logs to Jules
        logger.info(f"Sending CI failure logs to Jules session '{session_id}' for PR #{pr_number}")
        jules_client = JulesClient()
        response = jules_client.send_message(session_id, message)

        get_trace_logger().log("Jules Feedback", f"Sent CI failure logs to Jules for PR #{pr_number}", item_type="pr", item_number=pr_number, details={"session_id": session_id})

        actions.append(f"Sent CI failure logs to Jules session '{session_id}' for PR #{pr_number}")
        try:
            logger.info(f"Jules response for PR #{pr_number}: {response[:200]}...")
        except Exception:
            pass  # Prevent logging failures from affecting the result

        # Post a comment on the PR stating that a fix has been requested
        if github_client:
            comment_body = f"🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
            try:
                if _add_unique_pr_comment(github_client, repo_name, pr_number, comment_body):
                    actions.append(f"Posted comment on PR #{pr_number} stating that a fix has been requested from Jules")
                else:
                    actions.append(f"Skipped duplicate Jules fix-request comment on PR #{pr_number}")
            except Exception as e:
                error_msg = f"Failed to post comment on PR #{pr_number}: {e}"
                try:
                    logger.error(error_msg)
                except Exception:
                    pass  # Prevent logging failures from affecting the result
                actions.append(error_msg)
        else:
            actions.append(f"Skipped posting comment on PR #{pr_number}: no GitHub client available")

    except Exception as e:
        error_msg = f"Error sending Jules error feedback for PR #{pr_number}: {e}"
        try:
            logger.error(error_msg)
        except Exception:
            pass  # Prevent logging failures from affecting the result
        actions.append(error_msg)

    return actions


def _resolve_codex_cloud_task_id(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> Optional[str]:
    """Resolve the Codex Cloud task associated with a pull request."""
    task_id = pr_data.get("_codex_task_id")
    pr_body = pr_data.get("body", "") or ""

    if not task_id:
        direct_match = re.search(r"\b(task_[a-zA-Z0-9_-]+)\b", pr_body)
        if direct_match:
            task_id = direct_match.group(1)

    if not task_id:
        for issue_num in extract_linked_issues_from_pr_body(pr_body):
            found_url = _find_codex_cloud_task_for_issue(repo_name, issue_num, github_client)
            if found_url:
                task_match = re.search(r"\b(task_[a-zA-Z0-9_-]+)\b", found_url)
                if task_match:
                    task_id = task_match.group(1)
                    break

    if isinstance(task_id, str):
        task_match = re.search(r"\b(task_[a-zA-Z0-9_-]+)\b", task_id)
        if task_match:
            return task_match.group(1)
    return None


def _resolve_cloud_conflict_origin(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> Optional[Tuple[Any, str]]:
    """Return the capable client and existing task ID that originated a PR."""
    if _is_codex_pr(pr_data):
        task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
        if task_id:
            from .codex_cloud_client import CodexCloudClient

            return CodexCloudClient(repo_name=repo_name), task_id

    if _is_claude_pr(pr_data):
        task_id = _extract_session_id_from_pr_body(pr_data.get("body", "") or "")
        if not task_id:
            manager = CloudManager(repo_name)
            for issue_number in extract_linked_issues_from_pr_body(pr_data.get("body", "") or ""):
                task_id = manager.get_session_id(issue_number)
                if task_id:
                    break
        if task_id and not task_id.startswith("http"):
            from .claude_routine_client import ClaudeRoutineClient

            return ClaudeRoutineClient(repo_name=repo_name), task_id

    return None


def _cloud_conflict_state_path(repo_name: str) -> Path:
    """Return the durable deduplication state path for cloud conflict work."""
    return Path.home() / ".auto-coder" / repo_name / "cloud_conflict_repairs.json"


def _cloud_review_repair_state_path(repo_name: str) -> Path:
    """Return the durable deduplication state path for Codex review work."""
    return Path.home() / ".auto-coder" / repo_name / "cloud_review_repairs.json"


def _review_thread_repair_fingerprint(
    repo_name: str,
    pr_number: int,
    task_id: str,
    head_sha: str,
    unresolved_threads: Tuple[ReviewThread, ...],
) -> str:
    """Identify one exact PR-head and unresolved-review state."""
    thread_state = [
        {
            "id": thread.id,
            "comments": [
                {
                    "database_id": comment.database_id,
                    "body": comment.body,
                    "author": comment.author_login,
                }
                for comment in thread.comments
            ],
            "comments_truncated": thread.comments_truncated,
        }
        for thread in unresolved_threads
    ]
    serialized = json.dumps(thread_state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    review_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{repo_name}#{pr_number}:{task_id}:{head_sha}:{review_digest}"


def _delegate_codex_cloud_review_thread_repair(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
    unresolved_threads: Tuple[ReviewThread, ...] = (),
) -> List[str]:
    """Assign unresolved review feedback to the PR's existing Codex Cloud task.

    Delivery is durable and state-sensitive: an unchanged PR head and review
    discussion is sent only once, while a new commit or changed discussion can
    be assigned again. A missing Codex task simply preserves the generic
    review-thread merge gate for non-Codex pull requests.
    """
    pr_number = int(pr_data["number"])
    if not _is_codex_pr(pr_data):
        return []

    task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
    if not task_id:
        return []

    target = resolve_existing_pr_repair_target(repo_name, pr_data)
    if not target:
        logger.warning(f"PR #{pr_number} lacks complete head/base metadata; cannot delegate review repair")
        return [f"Cannot request Codex Cloud review repair for PR #{pr_number}: PR head/base branch metadata is unavailable"]

    fingerprint = _review_thread_repair_fingerprint(
        repo_name,
        pr_number,
        task_id,
        target.head_sha,
        unresolved_threads,
    )
    state_path = _cloud_review_repair_state_path(repo_name)
    delivered: dict[str, str] = {}
    try:
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                delivered = {str(key): str(value) for key, value in loaded.items()}
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not read Codex Cloud review repair state: {exc}")

    if fingerprint in delivered:
        logger.info(f"Review-thread repair for PR #{pr_number} at the current state was already delegated")
        return [f"Codex Cloud review repair was already requested for PR #{pr_number} at the current state"]

    details = get_prompt_template("codex_cloud.review_thread_repair_details")
    prompt = build_existing_pr_repair_prompt(target, details)
    try:
        from .codex_cloud_client import CodexCloudClient

        accepted = CodexCloudClient(repo_name=repo_name).send_followup(task_id, prompt)
    except Exception as exc:
        logger.warning(f"Codex Cloud review repair delegation failed for PR #{pr_number}: {exc}")
        return [f"Failed to request Codex Cloud review repair for PR #{pr_number}: {exc}"]
    if not accepted:
        return [f"Codex Cloud task '{task_id}' could not receive review repair work for PR #{pr_number}"]

    delivered[fingerprint] = task_id
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(delivered, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not persist Codex Cloud review repair state: {exc}")

    get_trace_logger().log(
        "Codex Cloud Review Repair",
        f"Assigned unresolved review threads to Codex Cloud task '{task_id}' for PR #{pr_number}",
        item_type="pr",
        item_number=pr_number,
        details={"task_id": task_id, "head_sha": target.head_sha},
    )
    return [f"Requested Codex Cloud task '{task_id}' to address unresolved review threads for PR #{pr_number}"]


def _delegate_cloud_merge_conflict_repair(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Optional[Any] = None,
) -> bool:
    """Delegate a current conflict to its originating cloud session when possible.

    ``True`` means this conflict state was either just delegated or was already
    delegated, so local repair must stop for this processing pass. ``False``
    preserves the caller's existing fallback path.
    """
    origin = _resolve_cloud_conflict_origin(repo_name, pr_data, github_client)
    if origin is None:
        return False
    client, task_id = origin

    # An inherited default method means that the provider does not opt in to
    # follow-up work. Do not use continue_if_paused as a substitute.
    from .cloud_task_client_base import CloudTaskClientBase

    if type(client).send_followup is CloudTaskClientBase.send_followup:
        return False

    pr_number = pr_data.get("number")
    base = pr_data.get("base") or {}
    base_state = base.get("sha") or pr_data.get("base_sha") or pr_data.get("base_branch") or base.get("ref")
    target = resolve_existing_pr_repair_target(repo_name, pr_data)
    if not target or not base_state:
        logger.warning(f"PR #{pr_number} lacks complete head/base metadata; cannot delegate conflict repair")
        return False

    fingerprint = f"{repo_name}#{pr_number}:{target.head_sha}:{base_state}"
    state_path = _cloud_conflict_state_path(repo_name)
    delivered: dict[str, str] = {}
    try:
        if state_path.exists():
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                delivered = {str(key): str(value) for key, value in loaded.items()}
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not read cloud conflict repair state: {exc}")

    if fingerprint in delivered:
        logger.info(f"Conflict repair for PR #{pr_number} at the current head/base state was already delegated")
        return True

    details = Template(get_prompt_template("pr.cloud_merge_conflict_repair_details")).safe_substitute(base_branch=target.base_branch)
    message = build_existing_pr_repair_prompt(target, details)
    try:
        accepted = client.send_followup(task_id, message)
    except Exception as exc:
        logger.warning(f"Cloud conflict repair delegation failed for PR #{pr_number}: {exc}")
        return False
    if not accepted:
        return False

    delivered[fingerprint] = task_id
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(delivered, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        # Delivery succeeded, so local repair must still stop. A persistence
        # failure can cause a later retry but must never create parallel repair.
        logger.warning(f"Could not persist cloud conflict repair state: {exc}")
    logger.info(f"Delegated merge-conflict repair for PR #{pr_number} to existing cloud task {task_id}")
    return True


def _send_codex_cloud_error_feedback(
    repo_name: str,
    pr_data: Dict[str, Any],
    failed_checks: List[Dict[str, Any]],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> CodexCloudFeedbackResult:
    """Send continuation request via continue_if_paused to Codex Cloud for Codex-created PRs.

    Args:
        repo_name: Repository name (owner/repo)
        pr_data: PR data dictionary
        failed_checks: List of failed GitHub Actions checks
        config: AutomationConfig instance
        github_client: Optional GitHub client instance

    Returns:
        Structured delivery status and action strings describing what was done.
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
        if not task_id:
            actions.append(f"Cannot resume Codex Cloud task for PR #{pr_number}: no valid Codex task ID found")
            logger.warning(f"No valid Codex task ID found in PR #{pr_number} data for continuation")
            return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))

        from .codex_cloud_client import CodexCloudClient

        logger.info(f"Triggering continue_if_paused for Codex Cloud task '{task_id}' on PR #{pr_number}")
        client = CodexCloudClient(repo_name=repo_name)

        target = resolve_existing_pr_repair_target(repo_name, pr_data)
        if target:
            details = get_prompt_template("codex_cloud.ci_review_repair_details")
            prompt = build_existing_pr_repair_prompt(target, details)
            resumed = client.continue_if_paused(task_id, prompt=prompt)
        else:
            resumed = client.continue_if_paused(task_id)

        if resumed:
            get_trace_logger().log(
                "Codex Cloud Feedback",
                f"Resumed Codex Cloud task '{task_id}' for PR #{pr_number}",
                item_type="pr",
                item_number=pr_number,
                details={"task_id": task_id},
            )
            actions.append(f"Sent continuation request to Codex Cloud task '{task_id}' for PR #{pr_number}")

            # Post a comment on the PR if github_client is available
            if github_client:
                comment_body = "🤖 Auto-Coder: CI checks failed. I've requested continuation from Codex Cloud to resolve the failures. Please wait for updates."
                try:
                    if _add_unique_pr_comment(github_client, repo_name, pr_number, comment_body):
                        actions.append(f"Posted comment on PR #{pr_number} stating that a fix has been requested from Codex Cloud")
                    else:
                        actions.append(f"Skipped duplicate Codex Cloud fix-request comment on PR #{pr_number}")
                except Exception as e:
                    error_msg = f"Failed to post comment on PR #{pr_number}: {e}"
                    logger.error(error_msg)
                    actions.append(error_msg)
            else:
                actions.append(f"Skipped posting comment on PR #{pr_number}: no GitHub client available")
        else:
            actions.append(f"Codex Cloud task '{task_id}' could not be resumed for PR #{pr_number}")
            return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))

    except Exception as e:
        error_msg = f"Error resuming Codex Cloud task for PR #{pr_number}: {e}"
        logger.error(error_msg)
        actions.append(error_msg)
        return CodexCloudFeedbackResult(retryable=True, actions=tuple(actions))

    return CodexCloudFeedbackResult(delivered=True, actions=tuple(actions))


def _send_adversarial_validation_feedback_to_codex_cloud(
    repo_name: str,
    pr_data: Dict[str, Any],
    head_sha: str,
    validation_report: str,
    github_client: Optional[Any] = None,
) -> List[str]:
    """Send a NEEDS_FIX validation report to the PR's existing Codex Cloud task."""
    pr_number = pr_data["number"]
    feedback_marker = adversarial_validation_codex_feedback_marker(head_sha)
    if github_client:
        try:
            comments = github_client.get_pr_comments(repo_name, pr_number)
        except Exception as e:
            logger.error(f"Failed to check prior Codex Cloud adversarial feedback for PR #{pr_number}: {e}")
            return [f"Could not check prior Codex Cloud adversarial feedback for PR #{pr_number}: {e}"]
        for comment in comments:
            body = _comment_value(comment, "body", "")
            if isinstance(body, str) and body.startswith(feedback_marker):
                return [f"Skipped duplicate adversarial feedback to Codex Cloud for PR #{pr_number} at {head_sha[:8]}"]

    task_id = _resolve_codex_cloud_task_id(repo_name, pr_data, github_client)
    if not task_id:
        return [f"Cannot send adversarial feedback to Codex Cloud for PR #{pr_number}: no valid Codex task ID found"]

    target = resolve_existing_pr_repair_target(repo_name, pr_data)
    if not target:
        return [f"Cannot send adversarial feedback to Codex Cloud for PR #{pr_number}: PR head/base branch metadata is unavailable"]
    # The validated commit is authoritative; it may be newer than cached PR head metadata.
    target = replace(target, head_sha=head_sha)

    details = Template(get_prompt_template("pr.adversarial_validation_fix")).safe_substitute(
        repo_name=repo_name,
        pr_number=pr_number,
        head_sha=head_sha,
        validation_report=validation_report,
    )
    prompt = build_existing_pr_repair_prompt(target, details)

    try:
        from .codex_cloud_client import CodexCloudClient

        client = CodexCloudClient(repo_name=repo_name)
        resumed = client.continue_if_paused(task_id, prompt=prompt)
    except Exception as e:
        logger.error(f"Error sending adversarial feedback to Codex Cloud for PR #{pr_number}: {e}")
        return [f"Error sending adversarial feedback to Codex Cloud for PR #{pr_number}: {e}"]

    if not resumed:
        return [f"Codex Cloud task '{task_id}' could not receive adversarial feedback for PR #{pr_number}"]

    get_trace_logger().log(
        "Codex Cloud Adversarial Feedback",
        f"Sent NEEDS_FIX report to Codex Cloud task '{task_id}' for PR #{pr_number}",
        item_type="pr",
        item_number=pr_number,
        details={"task_id": task_id, "head_sha": head_sha},
    )
    actions = [f"Sent adversarial NEEDS_FIX report to Codex Cloud task '{task_id}' for PR #{pr_number}"]
    if github_client:
        comment_body = "\n".join(
            [
                feedback_marker,
                "🤖 Auto-Coder: I sent the adversarial validation findings to the existing Codex Cloud task and requested a fix.",
            ]
        )
        try:
            github_client.add_comment_to_pr(repo_name, pr_number, comment_body)
            actions.append(f"Recorded Codex Cloud adversarial feedback delivery on PR #{pr_number}")
        except Exception as e:
            logger.error(f"Failed to record Codex Cloud adversarial feedback delivery on PR #{pr_number}: {e}")
            actions.append(f"Failed to record Codex Cloud adversarial feedback delivery on PR #{pr_number}: {e}")
    return actions


def _merge_pr(
    repo_name: str,
    pr_number: int,
    analysis: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
    expected_head_sha: Optional[str] = None,
) -> bool:
    """Merge a PR using GitHub CLI with conflict resolution and simple fallbacks.

    Fallbacks (no LLM):
    - After conflict resolution and retry failure, poll mergeable state briefly
    - Try alternative merge methods allowed by repo settings (--merge/--rebase/--squash)

    After successful merge, automatically closes any issues referenced in the PR body
    using GitHub's linking keywords (closes, fixes, resolves, etc.)
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        client = github_client or GitHubClient.get_instance()
        review_thread_state = _get_review_thread_gate_state(client, repo_name, pr_number)
        if review_thread_state.lookup_error:
            logger.info(f"PR #{pr_number} review threads could not be checked. Skipping merge.")
            log_action(f"Skipping merge for PR #{pr_number} because review threads could not be checked: {review_thread_state.lookup_error}")
            return False
        if review_thread_state.has_unresolved:
            logger.info(f"PR #{pr_number} has unresolved review threads. Skipping merge.")
            log_action(f"Skipping merge for PR #{pr_number} due to unresolved review threads")
            return False

        token = client.token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        def _attempt_api_merge(method: str) -> bool:
            try:
                # GhApi method names for merge_method are: 'merge', 'squash', 'rebase'
                # method argument from config (e.g. '--squash') needs to be stripped
                api_method = method.replace("--", "")
                kwargs: Dict[str, Any] = {"merge_method": api_method}
                if expected_head_sha:
                    kwargs["sha"] = expected_head_sha
                result = api.pulls.merge(owner, repo, pr_number, **kwargs)
                if result.get("merged"):
                    get_trace_logger().log("Merging", f"Successfully merged PR #{pr_number}", item_type="pr", item_number=pr_number, details={"method": method})
                    log_action(f"Successfully merged PR #{pr_number} (method: {method})")
                    _close_linked_issues(repo_name, pr_number)
                    _archive_jules_session(repo_name, pr_number)
                    return True
                return False
            except Exception as e:
                # 405/409 errors come here
                sha_info = f" (expected SHA {expected_head_sha[:8]})" if expected_head_sha else ""
                logger.warning(f"Merge failed for PR #{pr_number} with method {method}{sha_info}: {e}")
                return False

        # Check if the PR is authored by a dependency bot and auto-approve it
        try:
            pr_info = api.pulls.get(owner, repo, pr_number)
            if _is_dependabot_pr(pr_info):
                logger.info(f"Auto-approving Dependabot PR #{pr_number}")
                api.pulls.create_review(owner, repo, pr_number, event="APPROVE", body="Auto-approved by Auto-Coder")
                log_action(f"Auto-approved Dependabot PR #{pr_number}")
        except Exception as e:
            logger.warning(f"Could not auto-approve Dependabot PR #{pr_number}: {e}")

        # Attempt merge with configured method
        if _attempt_api_merge(config.MERGE_METHOD):
            return True

        # Try alternative merge methods if the primary method failed (even when not a conflict)
        try:
            allowed = _get_allowed_merge_methods(repo_name)
            methods_order = [m for m in ["--squash", "--merge", "--rebase"] if m != config.MERGE_METHOD]
            for m in methods_order:
                if m in allowed:
                    logger.info(f"Primary merge method {config.MERGE_METHOD} failed. Trying fallback allowed method {m} for PR #{pr_number}")
                    if _attempt_api_merge(m):
                        return True
        except Exception as e:
            logger.warning(f"Error trying fallback merge methods: {e}")

        # If failed, check if it was due to conflicts (check mergeable state)
        is_conflict = False
        try:
            pr_info = api.pulls.get(owner, repo, pr_number)
            if pr_info.get("mergeable") is False:
                is_conflict = True
        except Exception:
            pass

        if is_conflict:
            logger.info(f"PR #{pr_number} has merge conflicts, attempting to resolve...")
            log_action(f"PR #{pr_number} has merge conflicts, attempting resolution")

            if _is_jules_pr(pr_info):
                logger.info(f"PR #{pr_number} is a Jules PR with merge conflicts. Requesting Jules to resolve it.")
                try:
                    from auto_coder.jules_client import JulesClient

                    jules_client = JulesClient()
                    session_id = _extract_session_id_from_pr_body(pr_info.get("body", ""))
                    if session_id:
                        prompt = render_prompt("pr.jules_merge_conflict_resolution")
                        jules_client.send_message(session_id, prompt)
                        logger.info(f"Requested Jules to resolve merge conflict in session {session_id}")
                        log_action(f"Requested Jules to resolve merge conflicts for PR #{pr_number}")
                        return False
                    else:
                        logger.warning(f"Jules PR #{pr_number} has merge conflicts but no session ID found. Cannot delegate.")
                except Exception as e:
                    logger.error(f"Error requesting Jules to resolve conflict: {e}")

            # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
            # the bot recreates the PR against the updated base branch by itself.
            if _is_dependabot_pr(pr_info):
                logger.info(f"PR #{pr_number} is a dependency-bot PR with merge conflicts. Skipping conflict resolution.")
                log_action(f"Skipped merge conflict resolution for dependency-bot PR #{pr_number}")
                return False

            if _delegate_cloud_merge_conflict_repair(repo_name, pr_info, client):
                log_action(f"Delegated merge-conflict repair for PR #{pr_number} to its existing cloud session")
                return False

            # Try to resolve merge conflicts
            if _resolve_pr_merge_conflicts(repo_name, pr_number, config):
                # Poll for mergeability
                logger.info(f"Conflicts resolved for PR #{pr_number}, waiting for GitHub to update mergeable state")
                log_action(f"Polling mergeable state for PR #{pr_number} after conflict resolution")

                polling_succeeded = _poll_pr_mergeable(repo_name, pr_number, config)

                if polling_succeeded:
                    logger.info(f"GitHub confirmed PR #{pr_number} is mergeable, attempting merge")
                else:
                    logger.warning(f"Polling timed out for PR #{pr_number}, attempting merge anyway")

                # Retry merge
                if _attempt_api_merge(config.MERGE_METHOD):
                    log_action(f"Successfully merged PR #{pr_number} after conflict resolution")
                    return True
                else:
                    logger.warning(f"Merge failed for PR #{pr_number} even after conflict resolution")
                    log_action(f"Failed to merge PR #{pr_number} after conflict resolution", False, "Merge API failed")

                    # Try alternative merge methods
                    allowed = _get_allowed_merge_methods(repo_name)
                    methods_order = [config.MERGE_METHOD] + [m for m in ["--squash", "--merge", "--rebase"] if m != config.MERGE_METHOD]
                    for m in methods_order:
                        if m not in allowed or m == config.MERGE_METHOD:
                            continue
                        if _attempt_api_merge(m):
                            return True

                    # Trigger fallback
                    try:
                        pr_data = {"number": pr_number, "body": pr_info.get("body", "")}
                        _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed (conflict resolution exhausted)")
                    except Exception:
                        pass
                    return False
            else:
                log_action(f"Failed to resolve merge conflicts for PR #{pr_number}")
                try:
                    pr_data = {"number": pr_number, "body": pr_info.get("body", "")}
                    _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed (resolution failed)")
                except Exception:
                    pass
                return False

        else:
            # Not a conflict, but merge failed (maybe checks pending or not approved?)
            log_action(f"Failed to merge PR #{pr_number}", False, "Merge API failed (not conflict)")
            try:
                pr_info = api.pulls.get(owner, repo, pr_number)
                pr_data = {"number": pr_number, "body": pr_info.get("body", "")}
                _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed")
            except Exception:
                pass
            return False

    except Exception as e:
        logger.error(f"Error merging PR #{pr_number}: {e}")
        return False


def _poll_pr_mergeable(
    repo_name: str,
    pr_number: int,
    config: AutomationConfig,
    timeout_seconds: int = 60,
    interval: int = 5,
) -> bool:
    """Poll PR mergeable state for a short period. Returns True if becomes mergeable.
    Uses: gh pr view <num> --repo <repo> --json mergeable,mergeStateStatus
    """
    try:
        from auto_coder.util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                pr_info = api.pulls.get(owner, repo, pr_number)
                if pr_info.get("mergeable") is True:
                    return True
            except Exception:
                pass

            # Sleep before next poll
            time.sleep(max(1, interval))
        return False
    except Exception:
        return False


def _get_allowed_merge_methods(repo_name: str) -> List[str]:
    """Return list of allowed merge method flags for the repository.
    Maps GitHub repo settings to gh merge flags.
    """
    allowed: List[str] = []
    try:
        # Use GhApi to get allowed merge methods
        from auto_coder.util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        repo_data = api.repos.get(owner, repo)

        if repo_data.get("allow_squash_merge"):
            allowed.append("--squash")
        if repo_data.get("allow_merge_commit"):
            allowed.append("--merge")
        if repo_data.get("allow_rebase_merge"):
            allowed.append("--rebase")

        return allowed
    except Exception as e:
        logger.warning(f"Failed to get allowed merge methods via GhApi: {e}")
        return []


def _resolve_pr_merge_conflicts(repo_name: str, pr_number: int, config: AutomationConfig) -> bool:
    """Resolve merge conflicts for a PR by checking it out and merging with its base branch (not necessarily main)."""
    try:
        # Step 0: Clean up any existing git state
        logger.info(f"Cleaning up git state before resolving conflicts for PR #{pr_number}")

        # Reset any uncommitted changes
        reset_result = cmd.run_command(["git", "reset", "--hard"])
        if not reset_result.success:
            logger.warning(f"Failed to reset git state: {reset_result.stderr}")

        # Clean untracked files
        clean_result = cmd.run_command(["git", "clean", "-fd"])
        if not clean_result.success:
            logger.warning(f"Failed to clean untracked files: {clean_result.stderr}")

        # Abort any ongoing merge
        abort_result = cmd.run_command(["git", "merge", "--abort"])
        if abort_result.success:
            logger.info("Aborted ongoing merge")

        # Step 1: Get PR details to determine the target base branch and the author
        pr_data = None
        try:
            from auto_coder.util.gh_cache import get_ghapi_client

            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            pr_data = api.pulls.get(owner, repo, pr_number)
            base_branch = pr_data.get("base", {}).get("ref", config.MAIN_BRANCH)
        except Exception as e:
            logger.warning(f"Failed to get PR #{pr_number} details via GhApi: {e}")
            base_branch = config.MAIN_BRANCH

        # Dependency-bot PRs (Dependabot/Renovate) are never conflict-resolved:
        # the bot recreates the PR against the updated base branch by itself.
        if pr_data is not None and _is_dependabot_pr(pr_data):
            logger.info(f"Skipping merge conflict resolution for dependency-bot PR #{pr_number}")
            return False

        # Step 2: Checkout the PR branch
        logger.info(f"Checking out PR #{pr_number} to resolve merge conflicts")
        # Use reusable _checkout_pr_branch which uses direct git commands
        checkout_success = _checkout_pr_branch(repo_name, {"number": pr_number}, config)

        if not checkout_success:
            logger.error(f"Failed to checkout PR #{pr_number}")
            return False

        # Step 3: Fetch the latest base branch
        logger.info(f"Fetching latest {base_branch} branch")
        fetch_result = cmd.run_command(["git", "fetch", "origin", base_branch])

        if not fetch_result.success:
            logger.error(f"Failed to fetch {base_branch} branch: {fetch_result.stderr}")
            return False

        # Step 4: Attempt to merge base branch
        logger.info(f"Merging refs/remotes/origin/{base_branch} into PR #{pr_number}")
        merge_result = cmd.run_command(["git", "merge", f"refs/remotes/origin/{base_branch}"])

        if merge_result.success:
            # No conflicts, push the updated branch using centralized helper with retry
            logger.info(f"Successfully merged {base_branch} into PR #{pr_number}, pushing changes")
            push_result = git_push()

            if push_result.success:
                logger.info(f"Successfully pushed updated branch for PR #{pr_number}")
                return True
            else:
                # Push failed - try one more time after a brief pause
                logger.warning(f"First push attempt failed: {push_result.stderr}, retrying...")
                import time

                time.sleep(2)
                retry_push_result = git_push()
                if retry_push_result.success:
                    logger.info(f"Successfully pushed updated branch for PR #{pr_number} (after retry)")
                    return True
                else:
                    logger.error(f"Failed to push updated branch after retry: {retry_push_result.stderr}")
                    return False
        else:
            # Merge conflicts detected, use LLM to resolve them
            logger.info(f"Merge conflicts detected for PR #{pr_number}, using LLM to resolve")

            # Get conflict information
            conflict_info = _get_merge_conflict_info()

            # Use LLM to resolve conflicts
            resolve_actions = resolve_merge_conflicts_with_llm(
                {"number": pr_number, "base_branch": base_branch},
                conflict_info,
                config,
            )

            # Log the resolution actions
            for action in resolve_actions:
                logger.info(f"Conflict resolution action: {action}")

            # Check if conflicts were resolved successfully
            status_result = cmd.run_command(["git", "status", "--porcelain"])

            if status_result.success and not status_result.stdout.strip():
                logger.info(f"Merge conflicts resolved for PR #{pr_number}")
                get_trace_logger().log("Conflict Resolution", f"Resolved merge conflicts for PR #{pr_number}", item_type="pr", item_number=pr_number)
                return True
            else:
                logger.error(f"Failed to resolve merge conflicts for PR #{pr_number}")
                return False

    except Exception as e:
        logger.error(f"Error resolving merge conflicts for PR #{pr_number}: {e}")
        return False


def _fix_pr_issues_with_testing(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    failed_tests: List[str] | None = None,
    skip_github_actions_fix: bool = False,
) -> List[str]:
    # Extract failed tests from GitHub Actions logs
    if failed_tests is None:
        failed_tests = extract_all_failed_tests(github_logs)

    if skip_github_actions_fix:
        return _fix_pr_issues_with_local_testing(repo_name, pr_data, config, github_logs, test_files=failed_tests, skip_github_actions_fix=True)
    else:
        return _fix_pr_issues_with_github_actions_testing(repo_name, pr_data, config, github_logs, failed_tests=failed_tests)


def _fix_pr_issues_with_github_actions_testing(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    failed_tests: Optional[List[str]] = None,
) -> List[str]:
    """Fix PR issues using GitHub Actions logs, with intelligent routing.

    If 1-3 tests failed: Run local testing/fixing loop (targeted).
    If 4+ or 0 tests: Apply GHA log fix, commit, and push (trigger new run).
    """
    actions = []
    pr_number = pr_data["number"]

    # Initialize backend managers
    current_backend_manager = get_llm_backend_manager()
    high_score_backend_manager = create_high_score_backend_manager()

    # Track history
    attempt_history: List[Dict[str, Any]] = []

    try:
        # Strategy: GHA Iteration (Log Fix -> Commit -> Push)
        # 1. Apply fix based on GHA logs
        get_trace_logger().log("Fixing Issues", f"Fixing PR #{pr_number} using GHA logs", item_type="pr", item_number=pr_number)
        actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")
        initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs, backend_manager=high_score_backend_manager)
        actions.extend(initial_fix_actions)

        # 2. Apply fix based on local tests when 1-3 tests failed
        if failed_tests and 1 <= len(failed_tests) <= 3:
            test_result = run_local_tests(config, test_file=failed_tests[0])

            # Check if we should use local fix strategy (1-3 failed tests)
            attempts_limit = config.MAX_FIX_ATTEMPTS
            attempt = 0

            while not test_result.get("success") and 1 <= len(failed_tests) <= 3 and attempt < attempts_limit:
                attempt += 1

                # Check if PR is closed
                from .util.gh_cache import GitHubClient
                from .util.github_action import is_item_closed_on_github

                if is_item_closed_on_github(repo_name, "pr", pr_number, GitHubClient.get_instance()):
                    msg = f"PR #{pr_number} is closed on GitHub. Aborting fix loop."
                    logger.info(msg)
                    actions.append(msg)
                    return actions

                # Backend switching logic
                if attempt >= 2 and high_score_backend_manager:
                    if current_backend_manager != high_score_backend_manager:
                        logger.info(f"Switching to fallback backend for PR #{pr_number} after {attempt} attempts")
                        current_backend_manager = high_score_backend_manager
                        actions.append(f"Switched to fallback backend for PR #{pr_number}")

                with ProgressStage(f"Low-failure fix attempt {attempt}"):
                    local_fix_actions, llm_response = _apply_local_test_fix(
                        repo_name,
                        pr_data,
                        config,
                        test_result,
                        attempt_history,
                        backend_manager=current_backend_manager,
                    )
                    actions.extend(local_fix_actions)

                test_result = run_local_tests(config, test_file=failed_tests[0])

        # 3. Commit and Push
        # Check if any changes were made
        result = cmd.run_command(["git", "status", "--porcelain"])
        if result.success and result.stdout.strip():
            # Stage changes before committing
            cmd.run_command(["git", "add", "."])

            commit_msg = f"Auto-Coder: Fix issues based on GitHub Actions logs (PR #{pr_number})"
            c_res = git_commit_with_retry(commit_msg)
            if c_res.success:
                actions.append("Committed fixes based on GitHub Actions logs")
                p_res = git_push()
                if p_res.success:
                    actions.append("Pushed fixes to GitHub to trigger new Actions run")
                else:
                    actions.append(f"Failed to push fixes: {p_res.stderr}")
            else:
                actions.append(f"Failed to commit fixes: {c_res.stderr}")
        else:
            actions.append("No changes generated by GitHub Actions fix")

    except Exception as e:
        actions.append(f"Error fixing PR issues with testing for PR #{pr_number}: {e}")

    return actions


def _fix_pr_issues_with_local_testing(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    test_files: Optional[List[str]] = None,
    skip_github_actions_fix: bool = False,
) -> List[str]:
    """Fix PR issues using local testing loop."""
    actions = []
    pr_number = pr_data["number"]

    # Initialize backend managers
    current_backend_manager = get_llm_backend_manager()
    high_score_backend_manager = create_high_score_backend_manager()

    # Track history of previous attempts for context
    attempt_history: List[Dict[str, Any]] = []

    try:
        # Step 1: Initial fix using GitHub Actions logs
        if skip_github_actions_fix:
            msg = "Skipping GitHub Actions fix as we were already on the PR branch (assuming resumption)"
            logger.info(msg)
            actions.append(msg)
        else:
            get_trace_logger().log("Fixing Issues", f"Fixing PR #{pr_number} using GHA logs (local loop)", item_type="pr", item_number=pr_number)
            actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")
            initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs)
            actions.extend(initial_fix_actions)

        # Step 2: Local testing and iterative fixing loop
        attempts_limit = config.MAX_FIX_ATTEMPTS
        attempt = 0
        while True:
            with ProgressStage(f"attempt: {attempt}"):
                attempt += 1

                # Check if PR is closed
                from .util.gh_cache import GitHubClient
                from .util.github_action import is_item_closed_on_github

                if is_item_closed_on_github(repo_name, "pr", pr_number, GitHubClient.get_instance()):
                    msg = f"PR #{pr_number} is closed on GitHub. Aborting fix loop."
                    logger.info(msg)
                    actions.append(msg)
                    return actions

                # Backend switching logic: switch to fallback after 2 attempts
                if attempt >= 2 and high_score_backend_manager:
                    if current_backend_manager != high_score_backend_manager:
                        logger.info(f"Switching to fallback backend for PR #{pr_number} after {attempt} attempts")
                        current_backend_manager = high_score_backend_manager
                        actions.append(f"Switched to fallback backend for PR #{pr_number}")

                actions.append(f"Running local tests (attempt {attempt}/{attempts_limit})")

                with ProgressStage(f"Running local tests"):
                    test_result = run_local_tests(config)

                if test_result["success"]:
                    actions.append(f"Local tests passed on attempt {attempt}")
                    commit_and_push_changes({"summary": f"Auto-Coder: Address PR #{pr_number}"})
                    break
                else:
                    actions.append(f"Local tests failed on attempt {attempt}")

                    # Apply local test failure fix (always try unless finite limit reached)
                    # Stop if finite limit reached after this attempt
                    # Otherwise, continue attempting fixes
                    # Determine if we have remaining attempts (finite limit)
                    finite_limit_reached = False
                    try:
                        if math.isfinite(float(attempts_limit)) and attempt >= int(attempts_limit):
                            finite_limit_reached = True
                    except Exception:
                        finite_limit_reached = False

                    if finite_limit_reached:
                        actions.append(f"Max fix attempts ({attempts_limit}) reached for PR #{pr_number}")
                        break
                    else:
                        local_fix_actions, llm_response = _apply_local_test_fix(
                            repo_name,
                            pr_data,
                            config,
                            test_result,
                            attempt_history,
                            backend_manager=current_backend_manager,
                        )
                        actions.extend(local_fix_actions)

                        # Store this attempt in history for future reference
                        if llm_response:
                            attempt_history.append(
                                {
                                    "attempt_number": attempt,
                                    "llm_output": llm_response,
                                    "test_result": test_result,
                                }
                            )

    except Exception as e:
        actions.append(f"Error fixing PR issues with testing for PR #{pr_number}: {e}")

    return actions


def _apply_github_actions_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_logs: str,
    test_result: Optional[TestResult] = None,
    github_client: Optional[Any] = None,
    backend_manager: Optional[BackendManager] = None,
) -> List[str]:
    """Apply initial fix using GitHub Actions error logs.

    Enhanced: Optionally accepts a TestResult to pass structured error metadata
    and framework context to the LLM prompt for more targeted fixes.
    The LLM is instructed to edit files only; committing and pushing are handled
    by this code after a conflict-marker check.
    """
    actions: List[str] = []
    pr_number = pr_data["number"]

    try:
        # Get commit log since branch creation
        commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

        logger.info(f"Extracted important errors from GitHub Actions logs for PR #{pr_number}")

        # Extract linked issues context
        linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_data.get("body", ""))

        # Create prompt for GitHub Actions error fix (no commit/push by LLM)
        fix_prompt = render_prompt(
            "pr.github_actions_fix",
            pr_number=pr_number,
            repo_name=repo_name,
            pr_title=pr_data.get("title", "Unknown"),
            extracted_errors=github_logs,
            commit_log=commit_log or "(No commit history)",
            linked_issues_context=linked_issues_context,
            # Structured additions (safe if None)
            structured_errors=(test_result.extraction_context if test_result else {}),
            framework_type=(test_result.framework_type if test_result else None),
        )
        logger.debug(
            "Prepared GitHub Actions fix prompt for PR #%s (preview: %s)",
            pr_number,
            fix_prompt[:160].replace("\n", " "),
        )

        # Use LLM backend manager to run the prompt
        logger.info(f"Requesting LLM GitHub Actions fix for PR #{pr_number}")
        response = run_llm_prompt(fix_prompt, backend_manager=backend_manager)

        if response:
            response_preview = response.strip()[: config.MAX_RESPONSE_SIZE] if response.strip() else "No response"
            actions.append(f"Applied GitHub Actions fix: {response_preview}...")
        else:
            actions.append("No response from LLM for GitHub Actions fix")

    except Exception as e:
        logger.error(f"Error applying GitHub Actions fix for PR #{pr_number}: {e}")
        actions.append(f"Error applying GitHub Actions fix for PR #{pr_number}: {e}")

    return actions


def _apply_local_test_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    test_result: Dict[str, Any],
    attempt_history: List[Dict[str, Any]],
    backend_manager: Optional[BackendManager] = None,
    github_client: Optional[Any] = None,
) -> Tuple[List[str], str]:
    """Apply fix using local test failure logs.

    This function uses the LLM backend manager to apply fixes based on local test failures,
    similar to apply_workspace_test_fix in fix_to_pass_tests_runner.py.

    Args:
        repo_name: Repository name
        pr_data: PR data dictionary
        config: AutomationConfig instance
        test_result: Test result dictionary from run_local_tests
        attempt_history: List of previous attempts with LLM outputs and test results
        backend_manager: Optional BackendManager instance to use (defaults to global singleton)

    Returns:
        Tuple of (actions_list, llm_response)
    """
    actions = []
    llm_response = ""
    with ProgressStage(f"Local test fix"):
        pr_number = pr_data["number"]

        try:
            # Extract important error information (convert legacy dict to TestResult)
            tr = TestResult(
                success=bool(test_result.get("success", False)),
                output=str(test_result.get("output", "")),
                errors=str(test_result.get("errors", "")),
                return_code=int(test_result.get("return_code", test_result.get("returncode", -1)) or -1),
                command=str(test_result.get("command", "")),
                test_file=test_result.get("test_file"),
                stability_issue=bool(test_result.get("stability_issue", False)),
                extraction_context=(test_result.get("extraction_context", {}) if isinstance(test_result.get("extraction_context", {}), dict) else {}),
                framework_type=test_result.get("framework_type"),
            )
            error_summary = extract_important_errors(tr)

            if not error_summary:
                actions.append(f"No actionable errors found in local test output for PR #{pr_number}")
                logger.info("Skipping LLM local test fix because no actionable errors were extracted")
                return actions, llm_response

            # Get commit log since branch creation
            commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

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

            # Extract linked issues context
            linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_data.get("body", ""))

            # Create prompt for local test error fix
            fix_prompt = render_prompt(
                "pr.local_test_fix",
                pr_number=pr_number,
                repo_name=repo_name,
                pr_title=pr_data.get("title", "Unknown"),
                error_summary=error_summary[: config.MAX_PROMPT_SIZE],
                test_command=test_result.get("command", "pytest -q --maxfail=1"),
                commit_log=commit_log or "(No commit history)",
                attempt_history=history_text,
                linked_issues_context=linked_issues_context,
            )
            logger.debug(
                "Prepared local test fix prompt for PR #%s (preview: %s)",
                pr_number,
                fix_prompt[:160].replace("\n", " "),
            )

            # Use LLM backend manager to run the prompt
            # Check if llm_client has run_test_fix_prompt method (BackendManager)
            # or fall back to _run_llm_cli
            logger.info(f"Requesting LLM local test fix for PR #{pr_number}")

            # If test_file is not in the result, try to extract it from the output
            if not tr.test_file:
                tr.test_file = extract_first_failed_test(tr.output, tr.errors)

            # BackendManager with test file tracking
            manager = backend_manager or get_llm_backend_manager()
            llm_response = manager.run_test_fix_prompt(fix_prompt, current_test_file=tr.test_file)

            if llm_response:
                response_preview = llm_response.strip()[: config.MAX_RESPONSE_SIZE] if llm_response.strip() else "No response"
                actions.append(f"Applied local test fix: {response_preview}...")
            else:
                actions.append("No response from LLM for local test fix")

        except Exception as e:
            actions.append(f"Error applying local test fix for PR #{pr_number}: {e}")
            logger.error(f"Error applying local test fix for PR #{pr_number}: {e}", exc_info=True)

    return actions, llm_response
