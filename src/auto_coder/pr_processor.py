"""
PR processing functionality for Auto-Coder automation engine.
"""

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from auto_coder.backend_manager import BackendManager, get_llm_backend_manager, run_llm_prompt
from auto_coder.cli_helpers import create_high_score_backend_manager
from auto_coder.cloud_manager import CloudManager
from auto_coder.github_client import GitHubClient
from auto_coder.llm_backend_config import get_jules_fallback_enabled_from_config
from auto_coder.util.gh_cache import get_ghapi_client
from auto_coder.util.github_action import DetailedChecksResult, _check_github_actions_status, _create_github_action_log_summary, _get_github_actions_logs, check_github_actions_and_exit_if_in_progress, get_detailed_checks_from_history, get_pr_head_sha

from .attempt_manager import get_current_attempt, increment_attempt
from .automation_config import AutomationConfig, ProcessedPRResult
from .conflict_resolver import _get_merge_conflict_info, resolve_merge_conflicts_with_llm, resolve_pr_merge_conflicts
from .fix_to_pass_tests_runner import extract_important_errors_from_local_tests, run_local_tests
from .gh_logger import get_gh_logger
from .git_branch import branch_context, git_checkout_branch, git_commit_with_retry
from .git_commit import commit_and_push_changes, git_push, save_commit_failure_history
from .git_info import get_commit_log
from .issue_context import extract_linked_issues_from_pr_body, get_linked_issues_context
from .label_manager import LabelManager, LabelOperationError
from .logger_config import get_logger
from .progress_decorators import progress_stage
from .progress_footer import ProgressStage, newline_progress
from .prompt_loader import render_prompt
from .test_log_utils import extract_first_failed_test
from .test_result import TestResult
from .utils import CommandExecutor, CommandResult, get_pr_author_login, log_action

logger = get_logger(__name__)
cmd = CommandExecutor()


def _run_async_monitor(repo_name: str, pr_number: int, head_sha: str, workflow_id: str) -> None:
    """Run the async monitor in a separate thread."""
    asyncio.run(monitor_workflow_async(repo_name, pr_number, head_sha, workflow_id))


async def monitor_workflow_async(repo_name: str, pr_number: int, head_sha: str, workflow_id: str) -> None:
    """Monitor a triggered workflow asynchronously until completion.

    1. Wait for workflow run to appear.
    2. Wait for workflow run to complete.
    3. Update commit status.
    4. Remove @auto-coder label.
    """
    from auto_coder.label_manager import LabelManager
    from auto_coder.util.github_action import _check_github_actions_status

    logger.info(f"Started async monitor for PR #{pr_number} (workflow: {workflow_id})")

    github_client = GitHubClient.get_instance()
    config = AutomationConfig()

    # Create a dummy PR data for _check_github_actions_status
    pr_data = {
        "number": pr_number,
        "head": {"sha": head_sha},
    }

    try:
        # 1. Wait for workflow run to appear (max 5 minutes)
        run_found = False
        run_id = None

        for _ in range(60):  # 60 * 5s = 5 minutes
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
                processed_pr.actions_taken = ["Skipped - already being processed (@auto-coder label present)"]
                return processed_pr

        # Check if we should skip this PR because it's waiting for Jules
        if _should_skip_waiting_for_jules(github_client, repo_name, pr_data):
            logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
            processed_pr.actions_taken = ["Skipped - waiting for Jules to fix CI failures"]
            return processed_pr

        # Process Jules PRs to detect session IDs and update PR body
        try:
            jules_success = _process_jules_pr(repo_name, pr_data, github_client)
            if jules_success:
                logger.info(f"Successfully processed Jules PR #{pr_number} (or not a Jules PR)")
            else:
                logger.warning(f"Failed to process Jules PR #{pr_number}, but continuing with normal processing")
        except Exception as e:
            logger.error(f"Error in Jules PR processing for PR #{pr_number}: {e}")
            # Continue with normal processing even if Jules processing fails

        # Check if we should skip this PR because it's waiting for Jules
        if _should_skip_waiting_for_jules(github_client, repo_name, pr_data):
            logger.info(f"Skipping PR #{pr_number} - waiting for Jules to fix CI failures")
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
                # Check GitHub Actions status and mergeability
                github_checks = _check_github_actions_status(repo_name, pr_data, config)
                mergeable = pr_data.get("mergeable", True)

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

            finally:
                # Clear progress header after processing
                newline_progress()

        return processed_pr

    except Exception as e:
        logger.error(f"Failed to process PR #{pr_data.get('number', 'unknown')}: {e}")
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


def _should_skip_waiting_for_jules(github_client: Any, repo_name: str, pr_data: Dict[str, Any]) -> bool:
    """Check if PR should be skipped because it's waiting for Jules to fix CI failures.

    Returns True if:
    1. The last comment on the PR is the specific "CI checks failed..." message from Auto-Coder.
    2. There are no commits after that comment.
    """
    try:
        pr_number = pr_data["number"]

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

        # Check if it has been waiting for more than 2 hour
        try:
            # Parse GitHub timestamp (ISO 8601)
            # Example: 2023-10-27T10:00:00Z
            last_comment_dt = datetime.fromisoformat(last_comment_time.replace("Z", "+00:00"))
            current_time = datetime.now(timezone.utc)

            if current_time - last_comment_dt > timedelta(hours=2):
                logger.info(f"PR #{pr_number} has been waiting for Jules for > 2 hour. Switching to local processing.")
                return False
        except Exception as e:
            logger.warning(f"Failed to parse timestamp or compare time for PR #{pr_number}: {e}")

        logger.info(f"PR #{pr_number} is waiting for Jules (last comment is wait message, no new commits)")
        return True

    except Exception as e:
        logger.error(f"Error checking if PR #{pr_data.get('number')} should be skipped: {e}")
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
            merge_state_status = pr_details.get("merge_state_status", merge_state_status)
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

        # Step 1: Get PR details to determine the base branch
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            pr_details = api.pulls.get(owner, repo, pull_number=pr_number)
            base_branch = pr_details.get("base", {}).get("ref", "main")
        except Exception as e:
            error_msg = f"Failed to get PR #{pr_number} details via GhApi: {e}"
            actions.append(error_msg)
            log_action(error_msg, False)
            return actions

        actions.append(f"Determined base branch for PR #{pr_number}: {base_branch}")

        # Step 2: Checkout the PR branch
        # Create minimal PR data for checkout function
        pr_data_for_checkout = {"number": pr_number, "head": {"ref": f"pr-{pr_number}"}}
        checkout_success = _checkout_pr_branch("", pr_data_for_checkout, AutomationConfig())

        if not checkout_success:
            error_msg = f"Failed to checkout PR #{pr_number} branch"
            actions.append(error_msg)
            log_action(error_msg, False)
            return actions

        actions.append(f"Checked out PR #{pr_number} branch")

        # Step 3: Update from base branch with conflict resolution
        # The _update_with_base_branch function includes:
        # - Fetching latest changes
        # - Merging base branch
        # - Using _perform_base_branch_merge_and_conflict_resolution for conflicts
        # - Pushing updated branch with retry
        update_actions = _update_with_base_branch(repo_name, {"number": pr_number, "base_branch": base_branch}, AutomationConfig())
        actions.extend(update_actions)

        # Step 4: Check for degrading merge detection
        if "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE" in update_actions:
            # LLM determined merge would degrade code quality
            # The _trigger_fallback_for_conflict_failure has already been called in conflict_resolver
            # The linked issues have been reopened and attempt incremented
            # Now we need to close the PR

            try:
                client = GitHubClient.get_instance()
                close_comment = "Auto-Coder: Closing PR because LLM determined merge would degrade code quality. The linked issue(s) have been reopened with incremented attempt count."
                client.close_pr(repo_name, pr_number, close_comment)
                actions.append(f"Closed PR #{pr_number} without merging due to quality degradation risk")

                # Checkout main branch after closing PR
                main_branch = AutomationConfig().MAIN_BRANCH
                checkout_result = git_checkout_branch(main_branch, check_for_changes=False)
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
        elif "Failed" in str(update_actions):
            # Remediation attempted but failed
            actions.append(f"Mergeability remediation failed for PR #{pr_number}")

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
    """Process a PR for quick merging when GitHub Actions are passing."""
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

        # Since Actions are passing, attempt direct merge
        # Check if AUTO_MERGE is enabled before attempting merge
        if not config.AUTO_MERGE:
            processed_pr.actions_taken.append(f"Skipping merge for PR #{pr_data['number']} due to configuration (AUTO_MERGE=False)")
            return processed_pr

        merge_result = _merge_pr(repo_name, pr_data["number"], {}, config, github_client=github_client)
        if merge_result:
            processed_pr.actions_taken.append(f"Successfully merged PR #{pr_data['number']}")
            # Retain label on successful merge
            should_process.keep_label()
        else:
            processed_pr.actions_taken.append(f"Failed to merge PR #{pr_data['number']}")
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
        elif "ACTION_FLAG:SKIP_ANALYSIS" in merge_actions or any("skipping to next PR" in action for action in merge_actions):
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

        # Increment attempt for each linked issue
        for issue_number in linked_issues:
            try:
                logger.info(f"Incrementing attempt for issue #{issue_number} due to PR #{pr_data['number']} failure: {failure_reason}")
                increment_attempt(repo_name, issue_number)
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
        # Get GhApi client
        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # GhApi call for diff
        # We need to pass the custom media type header to get the diff
        # api.pulls.get normally returns JSON.
        # We'll use the lower-level api call or the __call__ method if supported,
        # or just rely on 'GET' verb manually via the adapter mechanism we built?
        # Actually, GhApi allows 'headers' argument in its calls if they are passed down?
        # fastai/ghapi generated methods might not accept 'headers'.
        # We can use api(path, verb, headers, ...)

        path = f"/repos/{owner}/{repo}/pulls/{pr_number}"
        # Use valid Accept header for diff
        headers = {"Accept": "application/vnd.github.v3.diff"}

        # We need to access the internal caching mechanism to pass these headers?
        # Our adapter added in gh_cache.py handles explicit headers passed to it.
        # But calling api.pulls.get() might not let us pass headers.
        # So we use api.full(path, headers=...) or similar?
        # valid way in ghapi: api(path, 'GET', headers=headers)

        # Note: 'api' object is callable: api(path, verb, headers, route_params, query_params, data)
        # Verify ghapi signature: __call__(self, path, verb=None, headers=None, route=None, query=None, data=None)

        diff_content = api(path, verb="GET", headers=headers)

        # diff_content might be bytes or str depending on the adapter return.
        # Our adapter calls props.json() which might fail for diff content if it's not JSON.
        # Wait, our gh_cache.py adapter attempts `return resp.json()`.
        # If response is NOT JSON (which diff is not), `resp.json()` will raise JSONDecodeError.
        # We need to fix gh_cache.py to handle non-JSON responses if we want to use it for diffs!
        # Or, we modify gh_cache.py adapter to return .text if content-type is not json?

        # Let's assume for now I will fix gh_cache.py to handle text/diff responses?
        # Or I can just bypass ghapi for this specific call if ghapi client is too rigid?
        # No, the goal is to use GhApi.

        # I should probably update gh_cache.py to return text if json fails or based on headers?
        # But assuming I haven't done that yet, this might fail.
        # I will handle it by updating gh_cache.py IN THE NEXT STEP if needed,
        # or checking if I can use a raw client here.
        # But I should stick to using the `api` object.

        # Let's write this to use `api` and assume the adapter handles it or I'll fix the adapter.
        # Actually, looking at my gh_cache.py implementation:
        # It calls `resp.json()`. This WILL fail for diffs.

        # I MUST fix gh_cache.py to check content-type or handle json error and return text.
        # Since I am in the middle of replacing, I will commit this change and THEN update gh_cache.py again
        # to support non-JSON responses.

        return str(diff_content)[: config.MAX_PR_DIFF_SIZE]

    except Exception as e:
        logger.debug(f"Failed to get PR diff via GhApi: {e}")
        return "Could not retrieve PR diff"


def _create_pr_analysis_prompt(repo_name: str, pr_data: Dict[str, Any], pr_diff: str, config: AutomationConfig, github_client: Optional[Any] = None) -> str:
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
    )
    return result


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
            logger.info(f"No GitHub Actions found for PR #{pr_number} (SHA: {pr_data.get('head', {}).get('sha')[:8]}). Triggering pr-tests.yml...")

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

                head_branch = pr_data.get("head", {}).get("ref")
                workflow_id = "pr-tests.yml"

                triggered = trigger_workflow_dispatch(repo_name, workflow_id, head_branch)

                if triggered:
                    actions.append(f"Triggered {workflow_id} for PR #{pr_number}")

                    # 3. Start async monitor
                    head_sha = get_pr_head_sha(pr_data, repo_name)
                    monitor_thread = threading.Thread(target=_run_async_monitor, args=(repo_name, pr_number, head_sha, workflow_id), daemon=True)
                    monitor_thread.start()

                    actions.append(f"Started async monitor for {workflow_id}")

                    # Keep the label so async monitor can remove it later
                    lm.keep_label()

                    return actions
                else:
                    actions.append(f"Failed to trigger {workflow_id} for PR #{pr_number}")
                    # Label will be removed by LabelManager exit

        detailed_checks = get_detailed_checks_from_history(github_checks, repo_name)

        # Step 4: If GitHub Actions passed, merge directly
        if github_checks.success and detailed_checks.success:
            actions.append(f"All GitHub Actions checks passed for PR #{pr_number}")

            # Check if AUTO_MERGE is enabled before attempting merge
            if not config.AUTO_MERGE:
                actions.append(f"Skipping merge for PR #{pr_number} due to configuration (AUTO_MERGE=False)")
                return actions

            merge_result = _merge_pr(repo_name, pr_number, analysis, config, github_client=github_client)
            if merge_result:
                actions.append(f"Successfully merged PR #{pr_number}")
                return actions
            else:
                actions.append(f"Failed to merge PR #{pr_number}")

        # Step 4: GitHub Actions failed - handle Jules PR feedback loop
        failed_checks = detailed_checks.failed_checks
        actions.append(f"GitHub Actions checks failed for PR #{pr_number}: {len(failed_checks)} failed")

        # Check if we are already on the PR branch before checkout.
        pr_branch_name = pr_data.get("head", {}).get("ref", "")
        current_branch_res = cmd.run_command(
            ["git", "branch", "--show-current"],
            timeout=10,
            stream_output=False,
        )
        current_branch = current_branch_res.stdout.strip() if current_branch_res.success else ""
        already_on_pr_branch = (current_branch == pr_branch_name) and (current_branch != "")

        # Check if this is a Jules PR
        if _is_jules_pr(pr_data) and not already_on_pr_branch:
            # Check if we should fallback to local llm_backend due to too many Jules failures
            # First check if fallback is enabled in config
            fallback_enabled = get_jules_fallback_enabled_from_config()
            should_fallback = False

            if not fallback_enabled:
                logger.info(f"Jules fallback to local is disabled in config. Skipping fallback checks for PR #{pr_number}.")
            else:
                try:
                    # Count specific failure comments
                    comments = github_client.get_pr_comments(repo_name, pr_number)
                    target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
                    failure_count = sum(1 for c in comments if target_message in c.get("body", ""))

                    if failure_count > 10:
                        logger.info(f"PR #{pr_number} has {failure_count} Jules failure comments (> 10). Switching to local llm_backend.")
                        should_fallback = True
                    else:
                        # Check if the last failure comment was more than 2 hour ago
                        last_failure_comment = next((c for c in reversed(comments) if target_message in c.get("body", "")), None)
                        if last_failure_comment:
                            last_comment_time = last_failure_comment["created_at"]
                            last_comment_dt = datetime.fromisoformat(last_comment_time.replace("Z", "+00:00"))
                            current_time = datetime.now(timezone.utc)

                            if current_time - last_comment_dt > timedelta(hours=2):
                                logger.info(f"PR #{pr_number} has been waiting for Jules for > 2 hour (last failure). Switching to local llm_backend.")
                                should_fallback = True

                except Exception as e:
                    logger.error(f"Error checking Jules failure count/time for PR #{pr_number}: {e}")

            if not should_fallback:
                actions.append(f"PR #{pr_number} is a Jules-created PR, sending error logs to Jules session")
                # Send error logs to Jules and skip local fixing - let Jules handle it
                jules_feedback_actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)
                actions.extend(jules_feedback_actions)
                actions.append(f"Jules will handle fixing PR #{pr_number}, skipping local fixes")
                return actions

        # Step 5: Checkout PR branch for non-Jules PRs
        # checkout_ok: bool = _checkout_pr_branch(repo_name, pr_data, config)
        # if not checkout_ok:
        #     actions.append(f"Failed to checkout PR #{pr_number} branch")
        #     return actions

        # actions.append(f"Checked out PR #{pr_number} branch")

        # Step 6: Optionally update with latest base branch commits (configurable)
        if config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL:
            actions.append(f"[Policy] Skipping base branch update for PR #{pr_number} (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=True)")

            # Proceed directly to extracting GitHub Actions logs and attempting fixes
            if failed_checks:
                github_logs, failed_tests = _create_github_action_log_summary(repo_name, config, failed_checks, pr_data)
                fix_actions = _fix_pr_issues_with_testing(
                    repo_name,
                    pr_data,
                    config,
                    github_logs,
                    failed_tests=failed_tests,
                    skip_github_actions_fix=already_on_pr_branch,
                )
                actions.extend(fix_actions)
            else:
                actions.append(f"No specific failed checks found for PR #{pr_number}")

            return actions
        else:
            actions.append(f"[Policy] Performing base branch update for PR #{pr_number} before fixes (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=False)")
            update_actions = _update_with_base_branch(repo_name, pr_data, config)
            actions.extend(update_actions)

            # Step 7: Check for special cases from base branch update

            # Check if LLM determined merge would degrade code quality
            if "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE" in update_actions:
                actions.append(f"LLM determined merge would degrade code quality for PR #{pr_number}, closing PR without merge")
                # Close the PR without merging
                try:
                    client = GitHubClient.get_instance()
                    close_comment = f"Auto-Coder: Closing PR because LLM determined merge would degrade code quality. The linked issue(s) have been reopened with incremented attempt count."
                    client.close_pr(repo_name, pr_number, close_comment)
                    actions.append(f"Closed PR #{pr_number} without merging")

                    # Checkout main branch after closing PR
                    main_branch = config.MAIN_BRANCH
                    checkout_res = git_checkout_branch(main_branch, check_for_changes=False)
                    if checkout_res.success:
                        actions.append(f"Checked out {main_branch} branch")
                    else:
                        logger.warning(f"Failed to checkout {main_branch} branch: {checkout_res.stderr}")
                        actions.append(f"Warning: Failed to checkout {main_branch} branch")
                except Exception as e:
                    logger.error(f"Failed to close PR #{pr_number}: {e}")
                    actions.append(f"Error closing PR #{pr_number}: {e}")
                return actions

            # If base branch update required pushing changes, skip to next PR
            if "ACTION_FLAG:SKIP_ANALYSIS" in update_actions or any("Pushed updated branch" in action for action in update_actions):
                actions.append(f"Updated PR #{pr_number} with base branch, skipping to next PR for GitHub Actions check")
                return actions

            # Step 8: If no main branch updates were needed, the test failures are due to PR content
            # Get GitHub Actions error logs and ask Gemini to fix
            if any("up to date with" in action for action in update_actions):
                actions.append(f"PR #{pr_number} is up to date with main branch, test failures are due to PR content")

                # Fix PR issues using GitHub Actions logs first, then local tests
                if failed_checks:
                    github_logs, failed_tests = _create_github_action_log_summary(repo_name, config, failed_checks, pr_data)
                    fix_actions = _fix_pr_issues_with_testing(
                        repo_name,
                        pr_data,
                        config,
                        github_logs,
                        failed_tests=failed_tests,
                        skip_github_actions_fix=already_on_pr_branch,
                    )
                    actions.extend(fix_actions)
                else:
                    actions.append(f"No specific failed checks found for PR #{pr_number}")
            else:
                # If we reach here, some other update action occurred
                actions.append(f"PR #{pr_number} processing completed")

    except Exception as e:
        actions.append(f"Error handling PR merge for PR #{pr_number}: {e}")
        logger.error(f"Error handling PR merge for PR #{pr_number}: {e}")

    return actions


def _checkout_pr_branch(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig) -> bool:
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

        # Step 2: Attempt to checkout the PR
        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            ["gh", "pr", "checkout", str(pr_number)],
            repo=repo_name,
            capture_output=True,
        )

        if result.success:  # type: ignore[attr-defined]
            log_action(f"Successfully checked out PR #{pr_number}")
            return True
        else:
            # If gh pr checkout fails, try alternative approach
            log_action(
                f"gh pr checkout failed for PR #{pr_number}, trying alternative approach",
                False,
                result.stderr,
            )

            # Step 3: Try manual fetch and checkout
            return _force_checkout_pr_manually(repo_name, pr_data, config)

    except Exception as e:
        logger.error(f"Error checking out PR #{pr_number}: {e}")
        return False


def _force_checkout_pr_manually(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig) -> bool:
    """Manually fetch and checkout PR branch as fallback."""
    pr_number = pr_data["number"]

    try:
        # Get PR branch information from PR data
        branch_name = pr_data.get("head", {}).get("ref")
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
        checkout_result = git_checkout_branch(branch_name)
        if not checkout_result.success:
            # If branch doesn't exist locally, checkout from fetched ref
            checkout_result = git_checkout_branch(branch_name, create_new=True, base_branch="FETCH_HEAD")

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
            # Merge conflict occurred, use common subroutine for conflict resolution
            actions.append(f"Merge conflict detected for PR #{pr_number}, using common subroutine for resolution...")

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

    # Pattern 3: Look for GitHub PR URLs (e.g., https://github.com/owner/repo/pull/123)
    # This pattern matches the full URL and extracts it as the session ID
    github_url_pattern = r"https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/pull/\d+"
    match = re.search(github_url_pattern, pr_body)
    if match:
        session_id = match.group(0).strip()
        logger.debug(f"Found session ID pattern 3 (GitHub PR URL): {session_id}")
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
    session_prefix_pattern = r"\b(session_[a-zA-Z0-9-_]+)\b"
    match = re.search(session_prefix_pattern, pr_body)
    if match:
        session_id = match.group(1).strip()
        logger.debug(f"Found session ID pattern 6 (session_ prefix): {session_id}")
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

            # Double check if session_id is actually in body or comments to be sure
            # Search API might return loose matches, although exact string match usually ranks high
            if issue.body and session_id in issue.body:
                logger.info(f"Found session ID '{session_id}' in body of issue #{issue.number}")
                return issue.number

            # Check comments
            # This is still an API call per issue, but we only do it for a few candidates
            comments = issue.get_comments()
            for comment in comments:
                if comment.body and session_id in comment.body:
                    logger.info(f"Found session ID '{session_id}' in comment of issue #{issue.number}")
                    return issue.number

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

        # Update PR body via GitHub Client (PyGithub)
        try:
            repo = github_client.get_repository(repo_name)
            pr = repo.get_pull(pr_number)
            pr.edit(body=new_body)

            logger.info(f"Updated PR #{pr_number} body to include reference to issue #{issue_number}")
            log_action(f"Updated PR #{pr_number} body with close #{issue_number} reference")
            return True
        except Exception as e:
            logger.error(f"Failed to update PR #{pr_number} body: {e}")
            return False

    except Exception as e:
        logger.error(f"Error updating Jules PR #{pr_number} body: {e}")
        return False


def _is_jules_pr(pr_data: Dict[str, Any]) -> bool:
    """Check if a PR is created by Jules (google-labs-jules).

    Args:
        pr_data: PR data dictionary

    Returns:
        True if the PR is created by Jules, False otherwise
    """
    # Check author first
    pr_author = get_pr_author_login(pr_data) or ""
    if pr_author.startswith("google-labs-jules"):
        return True

    # Fallback: Check if PR body contains a valid session ID
    # This handles cases where the PR was created by a different user (e.g. manual creation)
    # but is still associated with a Jules session
    pr_body = pr_data.get("body", "") or ""
    if _extract_session_id_from_pr_body(pr_body):
        return True

    return False


def _process_jules_pr(
    repo_name: str,
    pr_data: Dict[str, Any],
    github_client: Any,
) -> bool:
    """Process a Jules PR to detect session ID and update PR body.

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

        # Check if this is a Jules PR using the robust detection logic
        if not _is_jules_pr(pr_data):
            logger.debug(f"PR #{pr_number} author is not google-labs-jules ({pr_author}) and no session ID found, skipping Jules processing")
            return True  # Not an error, just not a Jules PR

        logger.info(f"Processing Jules PR #{pr_number} by {pr_author}")

        # Step 1: Extract Session ID from PR body
        session_id = _extract_session_id_from_pr_body(pr_body)
        if not session_id:
            logger.warning(f"No session ID found in Jules PR #{pr_number} body")
            return False

        logger.info(f"Extracted session ID '{session_id}' from Jules PR #{pr_number}")

        # Step 2: Store session_id in pr_data for later use in the feedback loop
        pr_data["_jules_session_id"] = session_id

        # Step 3: Use CloudManager to find the original issue number
        cloud_manager = CloudManager(repo_name)
        issue_number = cloud_manager.get_issue_by_session(session_id)

        if not issue_number:
            logger.warning(f"No issue found for session ID '{session_id}' in local DB. Searching comments...")
            issue_number = _find_issue_by_session_id_in_comments(repo_name, session_id, github_client)

        if not issue_number:
            logger.warning(f"No issue found for session ID '{session_id}' in Jules PR #{pr_number}")
            return False

        logger.info(f"Found issue #{issue_number} for session ID '{session_id}' in Jules PR #{pr_number}")

        # Step 4: Update PR body to include close #<issue_number> and link to issue
        success = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        if success:
            logger.info(f"Successfully processed Jules PR #{pr_number}, updated body to reference issue #{issue_number}")
        else:
            logger.error(f"Failed to update Jules PR #{pr_number} body")

        return success

    except Exception as e:
        logger.error(f"Error processing Jules PR {pr_data.get('number', 'unknown')}: {e}")
        return False


def _close_linked_issues(repo_name: str, pr_number: int) -> None:
    """Close issues linked in the PR body after successful merge.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number that was merged
    """
    try:
        # Get PR body
        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            ["gh", "pr", "view", str(pr_number), "--repo", repo_name, "--json", "body"],
            repo=repo_name,
            capture_output=True,
        )

        if not result.success or not result.stdout:  # type: ignore[attr-defined]
            logger.debug(f"Could not retrieve PR #{pr_number} body for issue linking")
            return

        pr_data = json.loads(result.stdout)
        pr_body = pr_data.get("body", "")

        # Extract linked issues
        linked_issues = extract_linked_issues_from_pr_body(pr_body)

        if not linked_issues:
            logger.debug(f"No linked issues found in PR #{pr_number} body")
            return

        # Close each linked issue
        for issue_num in linked_issues:
            try:
                gh_logger = get_gh_logger()
                close_result = gh_logger.execute_with_logging(
                    [
                        "gh",
                        "issue",
                        "close",
                        str(issue_num),
                        "--repo",
                        repo_name,
                        "--comment",
                        f"Closed by PR #{pr_number}",
                    ],
                    repo=repo_name,
                    capture_output=True,
                )

                if close_result.success:  # type: ignore[attr-defined]
                    logger.info(f"Closed issue #{issue_num} linked from PR #{pr_number}")
                    log_action(f"Closed issue #{issue_num} (linked from PR #{pr_number})")
                else:
                    logger.warning(f"Failed to close issue #{issue_num}: {close_result.stderr}")
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
        # Get PR data to check if it's a Jules PR and extract session ID
        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            ["gh", "pr", "view", str(pr_number), "--repo", repo_name, "--json", "user,body"],
            repo=repo_name,
            capture_output=True,
        )

        if not result.success or not result.stdout:  # type: ignore[attr-defined]
            logger.debug(f"Could not retrieve PR #{pr_number} data for Jules session archiving")
            return

        pr_data = json.loads(result.stdout)
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

    try:
        # Get the session ID from pr_data
        session_id = pr_data.get("_jules_session_id")
        if not session_id:
            actions.append(f"Cannot send error feedback to Jules for PR #{pr_number}: no session ID found")
            logger.error(f"No session ID found in PR #{pr_number} data for Jules error feedback")
            return actions

        # Get GitHub Actions error logs
        github_logs, _ = _create_github_action_log_summary(repo_name, config, failed_checks, pr_data)

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

        actions.append(f"Sent CI failure logs to Jules session '{session_id}' for PR #{pr_number}")
        logger.info(f"Jules response for PR #{pr_number}: {response[:200]}...")

        # Post a comment on the PR stating that a fix has been requested
        if github_client:
            comment_body = f"🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
            try:
                github_client.add_comment_to_pr(repo_name, pr_number, comment_body)
                actions.append(f"Posted comment on PR #{pr_number} stating that a fix has been requested from Jules")
            except Exception as e:
                error_msg = f"Failed to post comment on PR #{pr_number}: {e}"
                logger.error(error_msg)
                actions.append(error_msg)
        else:
            actions.append(f"Skipped posting comment on PR #{pr_number}: no GitHub client available")

    except Exception as e:
        error_msg = f"Error sending Jules error feedback for PR #{pr_number}: {e}"
        logger.error(error_msg)
        actions.append(error_msg)

    return actions


def _merge_pr(
    repo_name: str,
    pr_number: int,
    analysis: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[Any] = None,
) -> bool:
    """Merge a PR using GitHub CLI with conflict resolution and simple fallbacks.

    Fallbacks (no LLM):
    - After conflict resolution and retry failure, poll mergeable state briefly
    - Try alternative merge methods allowed by repo settings (--merge/--rebase/--squash)

    After successful merge, automatically closes any issues referenced in the PR body
    using GitHub's linking keywords (closes, fixes, resolves, etc.)
    """
    try:
        cmd_list = ["gh", "pr", "merge", str(pr_number)]
        gh_logger = get_gh_logger()

        # Try with --auto first if enabled, but fallback to direct merge if it fails
        if config.MERGE_AUTO:
            auto_cmd = cmd_list + ["--auto", config.MERGE_METHOD]
            result = gh_logger.execute_with_logging(auto_cmd, repo=repo_name, capture_output=True)

            if result.success:  # type: ignore[attr-defined]
                log_action(f"Successfully auto-merged PR #{pr_number}")
                _close_linked_issues(repo_name, pr_number)
                _archive_jules_session(repo_name, pr_number)
                return True
            else:
                # Log the auto-merge failure but continue with direct merge
                logger.warning(f"Auto-merge failed for PR #{pr_number}: {result.stderr}")
                log_action(f"Auto-merge failed for PR #{pr_number}, attempting direct merge")

        # Direct merge without --auto flag
        direct_cmd = cmd_list + [config.MERGE_METHOD]
        result = gh_logger.execute_with_logging(direct_cmd, repo=repo_name, capture_output=True)

        if result.success:  # type: ignore[attr-defined]
            log_action(f"Successfully merged PR #{pr_number}")
            _close_linked_issues(repo_name, pr_number)
            _archive_jules_session(repo_name, pr_number)
            return True
        else:
            # Check if the failure is due to merge conflicts
            if "not mergeable" in result.stderr.lower() or "merge commit cannot be cleanly created" in result.stderr.lower():
                logger.info(f"PR #{pr_number} has merge conflicts, attempting to resolve...")
                log_action(f"PR #{pr_number} has merge conflicts, attempting resolution")

                # Try to resolve merge conflicts using the new function from conflict_resolver
                if resolve_pr_merge_conflicts(repo_name, pr_number, config):
                    # Poll for mergeability BEFORE attempting merge to avoid race condition
                    logger.info(f"Conflicts resolved for PR #{pr_number}, waiting for GitHub to update mergeable state")
                    log_action(f"Polling mergeable state for PR #{pr_number} after conflict resolution")

                    polling_succeeded = _poll_pr_mergeable(repo_name, pr_number, config)

                    if polling_succeeded:
                        logger.info(f"GitHub confirmed PR #{pr_number} is mergeable, attempting merge")
                        log_action(f"GitHub confirmed PR #{pr_number} is mergeable")
                    else:
                        # Still attempt merge even if polling timed out
                        logger.warning(f"Polling timed out for PR #{pr_number} (waited 60s), " "attempting merge anyway since conflicts were resolved")
                        log_action(f"Mergeable state polling timed out for PR #{pr_number}, proceeding with merge attempt")

                    # Attempt merge after polling (or timeout)
                    gh_logger = get_gh_logger()
                    retry_result = gh_logger.execute_with_logging(direct_cmd, repo=repo_name, capture_output=True)
                    if retry_result.success:  # type: ignore[attr-defined]
                        log_action(f"Successfully merged PR #{pr_number} after conflict resolution")
                        _close_linked_issues(repo_name, pr_number)
                        _archive_jules_session(repo_name, pr_number)
                        return True
                    else:
                        # Merge failed even after conflict resolution and polling
                        logger.warning(f"Merge failed for PR #{pr_number} after conflict resolution: {retry_result.stderr}")
                        log_action(
                            f"Failed to merge PR #{pr_number} even after conflict resolution and polling",
                            False,
                            retry_result.stderr,
                        )
                        # Try alternative merge methods allowed by repo
                        allowed = _get_allowed_merge_methods(repo_name)
                        # Preserve order preference: configured first, then others
                        methods_order = [config.MERGE_METHOD] + [m for m in ["--squash", "--merge", "--rebase"] if m != config.MERGE_METHOD]
                        for m in methods_order:
                            if m not in allowed or m == config.MERGE_METHOD:
                                continue
                            alt_cmd = cmd_list + [m]
                            gh_logger = get_gh_logger()
                            alt_result = gh_logger.execute_with_logging(alt_cmd, repo=repo_name, capture_output=True)
                            if alt_result.success:  # type: ignore[attr-defined]
                                log_action(f"Successfully merged PR #{pr_number} with fallback method {m}")
                                _close_linked_issues(repo_name, pr_number)
                                _archive_jules_session(repo_name, pr_number)
                                return True
                        # All merge attempts failed, trigger fallback
                        # Get PR data to extract linked issues
                        try:
                            pr_data = {"number": pr_number, "body": ""}
                            gh_logger_temp = get_gh_logger()
                            pr_view_result = gh_logger_temp.execute_with_logging(
                                ["gh", "pr", "view", str(pr_number), "--repo", repo_name, "--json", "body"],
                                repo=repo_name,
                                capture_output=True,
                            )
                            if pr_view_result.success and pr_view_result.stdout:
                                pr_info = json.loads(pr_view_result.stdout)
                                pr_data["body"] = pr_info.get("body", "")
                            _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed (conflict resolution and fallbacks exhausted)")
                        except Exception:
                            # Don't fail the merge function if we can't trigger fallback
                            pass
                        return False
                else:
                    log_action(f"Failed to resolve merge conflicts for PR #{pr_number}")
                    # Trigger fallback for merge conflict resolution failure
                    try:
                        pr_data = {"number": pr_number, "body": ""}
                        gh_logger_temp = get_gh_logger()
                        pr_view_result = gh_logger_temp.execute_with_logging(
                            ["gh", "pr", "view", str(pr_number), "--repo", repo_name, "--json", "body"],
                            repo=repo_name,
                            capture_output=True,
                        )
                        if pr_view_result.success and pr_view_result.stdout:
                            pr_info = json.loads(pr_view_result.stdout)
                            pr_data["body"] = pr_info.get("body", "")
                        _trigger_fallback_for_pr_failure(repo_name, pr_data, "Automatic merge failed (conflict resolution failed)")
                    except Exception:
                        pass
                    return False
            else:
                log_action(f"Failed to merge PR #{pr_number}", False, result.stderr)
                # Trigger fallback for general merge failure
                try:
                    pr_data = {"number": pr_number, "body": ""}
                    gh_logger_temp = get_gh_logger()
                    pr_view_result = gh_logger_temp.execute_with_logging(
                        ["gh", "pr", "view", str(pr_number), "--repo", repo_name, "--json", "body"],
                        repo=repo_name,
                        capture_output=True,
                    )
                    if pr_view_result.success and pr_view_result.stdout:
                        pr_info = json.loads(pr_view_result.stdout)
                        pr_data["body"] = pr_info.get("body", "")
                    _trigger_fallback_for_pr_failure(repo_name, pr_data, f"Automatic merge failed: {result.stderr[:200]}")
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
        deadline = datetime.now().timestamp() + timeout_seconds
        while datetime.now().timestamp() < deadline:
            gh_logger = get_gh_logger()
            result = gh_logger.execute_with_logging(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo_name,
                    "--json",
                    "mergeable,mergeStateStatus",
                ],
                repo=repo_name,
            )
            if result.stdout:
                try:
                    data = json.loads(result.stdout)
                    # GitHub may return mergeable true/false/null
                    mergeable = data.get("mergeable")
                    if mergeable is True:
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
    try:
        # gh repo view --json mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed
        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            [
                "gh",
                "repo",
                "view",
                repo_name,
                "--json",
                "mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed",
            ],
            repo=repo_name,
            capture_output=True,
        )
        allowed: List[str] = []
        if result.stdout and result.success:  # type: ignore[attr-defined]
            try:
                data = json.loads(result.stdout)
                if data.get("squashMergeAllowed"):
                    allowed.append("--squash")
                if data.get("mergeCommitAllowed"):
                    allowed.append("--merge")
                if data.get("rebaseMergeAllowed"):
                    allowed.append("--rebase")
            except Exception:
                pass
        return allowed
    except Exception:
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

        # Step 1: Checkout the PR branch
        logger.info(f"Checking out PR #{pr_number} to resolve merge conflicts")
        gh_logger = get_gh_logger()
        checkout_result = gh_logger.execute_with_logging(
            ["gh", "pr", "checkout", str(pr_number)],
            repo=repo_name,
            capture_output=True,
        )

        if not checkout_result.success:  # type: ignore[attr-defined]
            logger.error(f"Failed to checkout PR #{pr_number}: {checkout_result.stderr}")
            return False

        # Step 1.5: Get PR details to determine the target base branch
        pr_details_result = gh_logger.execute_with_logging(
            ["gh", "pr", "view", str(pr_number), "--json", "base"],
            repo=repo_name,
            capture_output=True,
        )
        if not pr_details_result.success:  # type: ignore[attr-defined]
            logger.error(f"Failed to get PR #{pr_number} details: {pr_details_result.stderr}")
            return False

        try:
            pr_data = json.loads(pr_details_result.stdout)
            base_branch = pr_data.get("base", {}).get("ref", config.MAIN_BRANCH)
        except Exception:
            base_branch = config.MAIN_BRANCH

        # Step 2: Fetch the latest base branch
        logger.info(f"Fetching latest {base_branch} branch")
        fetch_result = cmd.run_command(["git", "fetch", "origin", base_branch])

        if not fetch_result.success:
            logger.error(f"Failed to fetch {base_branch} branch: {fetch_result.stderr}")
            return False

        # Step 3: Attempt to merge base branch
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
    failed_tests: Optional[List[str]] = None,
    skip_github_actions_fix: bool = False,
) -> List[str]:
    if skip_github_actions_fix:
        return _fix_pr_issues_with_local_testing(repo_name, pr_data, config, github_logs, test_files=failed_tests)
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

    try:
        actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")
        initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs)
        actions.extend(initial_fix_actions)

        # Convert failed_tests list to a single test_file string
        test_file_str = ",".join(failed_tests) if failed_tests else None
        test_result = run_local_tests(config, test_file=test_file_str)

        # Check if we should use local fix strategy (1-3 failed tests)
        # Import backend_manager from module level or pass as parameters
        from auto_coder.backend_manager import BackendManager
        from auto_coder.cli_helpers import create_high_score_backend_manager

        # attempt_history is not available in this context, use empty list
        attempt_history: list[dict[str, Any]] = []
        current_backend_manager: Optional[BackendManager] = create_high_score_backend_manager()

        while test_result.get("failed_tests") and 1 <= len(test_result.get("failed_tests", [])) <= 3:
            local_fix_actions, llm_response = _apply_local_test_fix(
                repo_name,
                pr_data,
                config,
                test_result,
                attempt_history,
                backend_manager=current_backend_manager,
            )
            test_result = run_local_tests(config, test_file=test_file_str)

        # Strategy: GHA Iteration (Log Fix -> Commit -> Push)
        count_msg = f"{len(failed_tests)} failed tests" if failed_tests is not None else "failed tests (unknown count)"
        actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs ({count_msg})")

        # 1. Apply fix based on GHA logs
        initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs)
        actions.extend(initial_fix_actions)

        # 2. Commit and Push
        # Check if any changes were made
        result = cmd.run_command(["git", "status", "--porcelain"])
        if result.success and result.stdout.strip():
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
        actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")
        initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, github_logs)
        actions.extend(initial_fix_actions)

        # Step 2: Local testing and iterative fixing loop
        attempts_limit = config.MAX_FIX_ATTEMPTS
        attempt = 0
        while True:
            with ProgressStage(f"attempt: {attempt}"):
                attempt += 1

                # Backend switching logic: switch to fallback after 2 attempts
                if attempt >= 2 and high_score_backend_manager:
                    if current_backend_manager != high_score_backend_manager:
                        logger.info(f"Switching to fallback backend for PR #{pr_number} after {attempt} attempts")
                        current_backend_manager = high_score_backend_manager
                        actions.append(f"Switched to fallback backend for PR #{pr_number}")

                actions.append(f"Running local tests (attempt {attempt}/{attempts_limit})")

                target_tests = " ".join(test_files) if test_files else None
                with ProgressStage(f"Running local tests"):
                    test_result = run_local_tests(config, test_file=target_tests)

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

        # Extract important error information from GitHub Actions logs using extract_important_errors
        github_test_result = TestResult(
            success=False,
            output=github_logs or "",
            errors="",
            return_code=1,
            command="github_actions_logs",
            test_file=None,
            stability_issue=False,
            extraction_context={},
            framework_type="github_actions",
        )

        # For GitHub Actions, we trust the github_logs summary and do not run further extraction.
        # This prevents accidental truncation of the curated summary.
        extracted_errors = github_logs

        # Legacy fallback if logs are empty (though github_logs is usually passed in)
        if not extracted_errors:
            extracted_errors = "No error information available"

        logger.info(f"Extracted important errors from GitHub Actions logs for PR #{pr_number}")

        # Extract linked issues context
        linked_issues_context = get_linked_issues_context(github_client, repo_name, pr_data.get("body", ""))

        # Create prompt for GitHub Actions error fix (no commit/push by LLM)
        fix_prompt = render_prompt(
            "pr.github_actions_fix",
            pr_number=pr_number,
            repo_name=repo_name,
            pr_title=pr_data.get("title", "Unknown"),
            extracted_errors=extracted_errors,
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

        if config.JULES_MODE:
            # Use LLM backend manager to run the prompt
            logger.info(f"Starting Jules session for GitHub Actions fix for PR #{pr_number}")

            # Import JulesClient here to avoid circular imports
            from .issue_processor import _notify_jules_session_start
            from .jules_client import JulesClient

            session_id = JulesClient().start_session(fix_prompt, repo_name, pr_data["head"]["ref"], is_noedit=False, title=f"Fix for PR #{pr_number} {pr_data['title']}")
            # Get GitHubClient instance if not provided
            gh_client = github_client if github_client else GitHubClient.get_instance()
            _notify_jules_session_start(repo_name, pr_number, session_id, gh_client, actions)
        else:
            # Use LLM backend manager to run the prompt
            logger.info(f"Requesting LLM GitHub Actions fix for PR #{pr_number}")
            response = run_llm_prompt(fix_prompt)

        if response:
            response_preview = response.strip()[: config.MAX_RESPONSE_SIZE] if response.strip() else "No response"
            actions.append(f"Applied GitHub Actions fix: {response_preview}...")
        else:
            actions.append("No response from LLM for GitHub Actions fix")

    except Exception as e:
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
    with ProgressStage(f"Local test fix"):
        actions = []
        pr_number = pr_data["number"]
        llm_response = ""

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
            error_summary = extract_important_errors_from_local_tests(tr)

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
