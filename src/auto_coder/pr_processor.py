"""
PR processing functionality for Auto-Coder automation engine.
"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

from auto_coder.backend_manager import get_llm_backend_manager, run_llm_prompt
from auto_coder.github_client import GitHubClient
from auto_coder.util.github_action import DetailedChecksResult, _check_github_actions_status, _get_github_actions_logs, get_detailed_checks_from_history

from .automation_config import AutomationConfig
from .conflict_resolver import _get_merge_conflict_info, resolve_merge_conflicts_with_llm, resolve_pr_merge_conflicts
from .fix_to_pass_tests_runner import extract_important_errors, run_local_tests
from .git_utils import get_commit_log, git_checkout_branch, git_commit_with_retry, git_push, save_commit_failure_history
from .label_manager import LabelManager, LabelOperationError
from .logger_config import get_logger
from .progress_decorators import progress_stage
from .progress_footer import ProgressStage, newline_progress
from .prompt_loader import render_prompt
from .update_manager import check_for_updates_and_restart
from .utils import CommandExecutor, log_action

logger = get_logger(__name__)
cmd = CommandExecutor()


def process_pull_request(
    github_client: Any,
    config: AutomationConfig,
    dry_run: bool,
    repo_name: str,
    pr_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Process a single pull request with priority order."""
    try:
        processed_pr: Dict[str, Any] = {
            "pr_data": pr_data,
            "actions_taken": [],
            "priority": None,
            "analysis": None,
        }

        pr_number = pr_data["number"]

        # Skip immediately if PR already has @auto-coder label
        pr_labels = pr_data.get("labels", [])
        if "@auto-coder" in pr_labels:
            logger.info(f"Skipping PR #{pr_number} - already has @auto-coder label")
            processed_pr["actions_taken"] = ["Skipped - already being processed (@auto-coder label present)"]
            return processed_pr

        check_for_updates_and_restart()

        # Extract PR information
        branch_name = pr_data.get("head", {}).get("ref")
        pr_body = pr_data.get("body", "")
        related_issues = []
        if pr_body:
            # Extract linked issues from PR body
            related_issues = _extract_linked_issues_from_pr_body(pr_body)

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

                # Determine processing path
                if github_checks.success and mergeable:
                    logger.info(f"PR #{pr_number}: Actions PASSING and MERGEABLE - attempting merge")
                    processed_pr["priority"] = "merge"

                    with ProgressStage("Attempting merge"):
                        processed_pr_result = _process_pr_for_merge(
                            repo_name,
                            pr_data,
                            config,
                            dry_run,
                        )
                    processed_pr.update(processed_pr_result)
                else:
                    logger.info(f"PR #{pr_number}: Processing for issue resolution")
                    processed_pr["priority"] = "fix"

                    # Process for fixes
                    processed_pr_result = _process_pr_for_fixes(repo_name, pr_data, config, dry_run)
                    processed_pr.update(processed_pr_result)

            finally:
                # Clear progress header after processing
                newline_progress()

        return processed_pr

    except Exception as e:
        logger.error(f"Failed to process PR #{pr_data.get('number', 'unknown')}: {e}")
        return {
            "pr_data": pr_data,
            "actions_taken": [f"Error processing PR: {str(e)}"],
            "priority": "error",
            "analysis": None,
        }


def _is_dependabot_pr(pr_obj: Any) -> bool:
    """Return True if the PR is authored by Dependabot.

    Detects common Dependabot actors such as 'dependabot[bot]' or accounts containing 'dependabot'.
    """
    try:
        user = getattr(pr_obj, "user", None)
        login = getattr(user, "login", None) if user is not None else None
        if isinstance(login, str) and "dependabot" in login.lower():
            return True
    except Exception:
        pass
    return False


def _process_pr_for_merge(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
) -> Dict[str, Any]:
    """Process a PR for quick merging when GitHub Actions are passing."""
    processed_pr: Dict[str, Any] = {
        "pr_data": pr_data,
        "actions_taken": [],
        "priority": "merge",
        "analysis": None,
    }
    github_client = GitHubClient.get_instance()

    try:
        # Use LabelManager to ensure @auto-coder label is added and properly managed
        try:
            with LabelManager(github_client, repo_name, pr_data["number"], "pr", dry_run, config):
                if dry_run:
                    # Single execution policy - skip analysis phase
                    processed_pr["actions_taken"].append(f"[DRY RUN] Would merge PR #{pr_data['number']} (Actions passing)")
                    return processed_pr
                else:
                    # Since Actions are passing, attempt direct merge
                    merge_result = _merge_pr(repo_name, pr_data["number"], {}, config)
                    if merge_result:
                        processed_pr["actions_taken"].append(f"Successfully merged PR #{pr_data['number']}")
                    else:
                        processed_pr["actions_taken"].append(f"Failed to merge PR #{pr_data['number']}")
                    return processed_pr
        except LabelOperationError:
            logger.info(f"Skipping PR #{pr_data['number']} - already has @auto-coder label")
            processed_pr["actions_taken"] = ["Skipped - already being processed (@auto-coder label present)"]
            return processed_pr

    except Exception as e:
        processed_pr["actions_taken"].append(f"Error processing PR #{pr_data['number']} for merge: {str(e)}")
        return processed_pr


def _process_pr_for_fixes(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
) -> Dict[str, Any]:
    """Process a PR for issue resolution when GitHub Actions are failing or pending."""
    processed_pr: Dict[str, Any] = {"pr_data": pr_data, "actions_taken": [], "priority": "fix"}
    github_client = GitHubClient.get_instance()

    try:
        # Use LabelManager to ensure @auto-coder label is added and properly managed
        try:
            with LabelManager(github_client, repo_name, pr_data["number"], "pr", dry_run, config):
                # Use the existing PR actions logic for fixing issues
                with ProgressStage("Fixing issues"):
                    actions = _take_pr_actions(repo_name, pr_data, config, dry_run)
                processed_pr["actions_taken"] = actions
        except LabelOperationError:
            logger.info(f"Skipping PR #{pr_data['number']} - already has @auto-coder label")
            processed_pr["actions_taken"] = ["Skipped - already being processed (@auto-coder label present)"]

    except Exception as e:
        processed_pr["actions_taken"].append(f"Error processing PR #{pr_data['number']} for fixes: {str(e)}")

    return processed_pr


def _take_pr_actions(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
) -> List[str]:
    """Take actions on a PR including merge handling and analysis."""
    actions = []
    pr_number = pr_data["number"]

    try:
        if dry_run:
            logger.debug("Dry run requested for PR #%s; skipping merge workflow", pr_number)
            return [f"[DRY RUN] Would handle PR merge and analysis for PR #{pr_number}"]

        # First, handle the merge process (GitHub Actions, testing, etc.)
        # This doesn't depend on Gemini analysis
        merge_actions = _handle_pr_merge(repo_name, pr_data, config, dry_run, {})
        actions.extend(merge_actions)

        # If merge process completed successfully (PR was merged), skip analysis
        if any("Successfully merged" in action for action in merge_actions):
            actions.append(f"PR #{pr_number} was merged, skipping further analysis")
        elif "ACTION_FLAG:SKIP_ANALYSIS" in merge_actions or any("skipping to next PR" in action for action in merge_actions):
            actions.append(f"PR #{pr_number} processing deferred, skipping analysis")
        else:
            # Only do Gemini analysis if merge process didn't complete
            analysis_results = _apply_pr_actions_directly(repo_name, pr_data, config, dry_run)
            actions.extend(analysis_results)

    except Exception as e:
        actions.append(f"Error taking PR actions for PR #{pr_number}: {e}")

    return actions


def _apply_pr_actions_directly(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
) -> List[str]:
    """Ask LLM CLI to apply PR fixes directly; avoid posting PR comments.

    Expected LLM output formats:
    - "ACTION_SUMMARY: ..." single line when actions were taken
    - "CANNOT_FIX" when it cannot deterministically fix
    """
    actions = []
    pr_number = pr_data["number"]

    try:
        # Handle dry run mode - skip actual LLM processing
        if dry_run:
            actions.append(f"[DRY RUN] Would apply PR actions directly for PR #{pr_number}")
            return actions

        # Get PR diff for analysis
        with ProgressStage("Getting PR diff"):
            pr_diff = _get_pr_diff(repo_name, pr_number, config)

        # Create action-oriented prompt (no comments)
        with ProgressStage("Creating prompt"):
            action_prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config)
            logger.debug(
                "Prepared PR action prompt for #%s (preview: %s)",
                pr_data.get("number", "unknown"),
                action_prompt[:160].replace("\n", " "),
            )

        # Use LLM CLI to analyze and take actions
        log_action(f"Applying PR actions directly for PR #{pr_number}")

        # Call LLM client
        with ProgressStage("Running LLM"):
            response = get_llm_backend_manager()._run_llm_cli(action_prompt)

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
            else:
                # Fallback: record truncated raw response without posting comments
                actions.append(f"LLM response: {resp[: config.MAX_RESPONSE_SIZE]}...")

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
        else:
            actions.append("LLM CLI did not provide a clear response for PR actions")

    except Exception as e:
        actions.append(f"Error applying PR actions directly: {e}")

    return actions


def _get_pr_diff(repo_name: str, pr_number: int, config: AutomationConfig) -> str:
    """Get PR diff for analysis."""
    try:
        result = cmd.run_command(["gh", "pr", "diff", str(pr_number), "--repo", repo_name])
        return result.stdout[: config.MAX_PR_DIFF_SIZE] if result.success else "Could not retrieve PR diff"
    except Exception:
        return "Could not retrieve PR diff"


def _create_pr_analysis_prompt(repo_name: str, pr_data: Dict[str, Any], pr_diff: str, config: AutomationConfig) -> str:
    """Create a PR prompt that prioritizes direct code changes over comments."""
    # Get commit log since branch creation
    commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

    body_text = (pr_data.get("body") or "")[: config.MAX_PROMPT_SIZE]
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
    )
    return result


def _handle_pr_merge(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
    analysis: Dict[str, Any],
) -> List[str]:
    """Handle PR merge process following the intended flow."""
    actions = []
    pr_number = pr_data["number"]

    try:
        # Ensure any unpushed commits are pushed before starting
        logger.info(f"Checking for unpushed commits before processing PR #{pr_number}...")
        push_result = git_push(
            commit_message=f"Auto-Coder: PR #{pr_number} processing",
        )
        if push_result.success and "No unpushed commits" not in push_result.stdout:
            actions.append(f"Pushed unpushed commits: {push_result.stdout}")
            logger.info("Successfully pushed unpushed commits")
        elif not push_result.success:
            logger.error(f"Failed to push unpushed commits: {push_result.stderr}")
            logger.error("Exiting application due to git push failure")
            sys.exit(1)

        # Step 1: Check GitHub Actions status
        github_checks = _check_github_actions_status(repo_name, pr_data, config)
        detailed_checks = get_detailed_checks_from_history(github_checks, repo_name)

        # Step 2: Skip if GitHub Actions are still in progress
        if detailed_checks.has_in_progress:
            actions.append(f"GitHub Actions checks are still in progress for PR #{pr_number}, skipping to next PR")
            return actions

        # Step 3: If GitHub Actions passed, merge directly
        if github_checks.success and detailed_checks.success:
            actions.append(f"All GitHub Actions checks passed for PR #{pr_number}")

            if not dry_run:
                merge_result = _merge_pr(repo_name, pr_number, analysis, config)
                if merge_result:
                    actions.append(f"Successfully merged PR #{pr_number}")
                else:
                    actions.append(f"Failed to merge PR #{pr_number}")
            else:
                actions.append(f"[DRY RUN] Would merge PR #{pr_number}")
            return actions

        # Step 4: GitHub Actions failed - checkout PR branch
        failed_checks = detailed_checks.failed_checks
        actions.append(f"GitHub Actions checks failed for PR #{pr_number}: {len(failed_checks)} failed")

        checkout_result = _checkout_pr_branch(repo_name, pr_data, config)
        if not checkout_result:
            actions.append(f"Failed to checkout PR #{pr_number} branch")
            return actions

        actions.append(f"Checked out PR #{pr_number} branch")

        # Step 5: Optionally update with latest base branch commits (configurable)
        if config.SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL:
            actions.append(f"[Policy] Skipping base branch update for PR #{pr_number} (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=True)")

            # Proceed directly to extracting GitHub Actions logs and attempting fixes
            if failed_checks:
                github_logs = _get_github_actions_logs(repo_name, config, failed_checks, pr_data)
                fix_actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, dry_run, github_logs)
                actions.extend(fix_actions)
            else:
                actions.append(f"No specific failed checks found for PR #{pr_number}")

            return actions
        else:
            actions.append(f"[Policy] Performing base branch update for PR #{pr_number} before fixes (config: SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL=False)")
            update_actions = _update_with_base_branch(repo_name, pr_data, config, dry_run)
            actions.extend(update_actions)

            # Step 6: If base branch update required pushing changes, skip to next PR
            if "ACTION_FLAG:SKIP_ANALYSIS" in update_actions or any("Pushed updated branch" in action for action in update_actions):
                actions.append(f"Updated PR #{pr_number} with base branch, skipping to next PR for GitHub Actions check")
                return actions

            # Step 7: If no main branch updates were needed, the test failures are due to PR content
            # Get GitHub Actions error logs and ask Gemini to fix
            if any("up to date with" in action for action in update_actions):
                actions.append(f"PR #{pr_number} is up to date with main branch, test failures are due to PR content")

                # Fix PR issues using GitHub Actions logs first, then local tests
                if failed_checks:
                    # Unit test expects _get_github_actions_logs(repo_name, failed_checks)
                    github_logs = _get_github_actions_logs(repo_name, config, failed_checks, pr_data)
                    fix_actions = _fix_pr_issues_with_testing(repo_name, pr_data, config, dry_run, github_logs)
                    actions.extend(fix_actions)
                else:
                    actions.append(f"No specific failed checks found for PR #{pr_number}")
            else:
                # If we reach here, some other update action occurred
                actions.append(f"PR #{pr_number} processing completed")

    except Exception as e:
        actions.append(f"Error handling PR merge for PR #{pr_number}: {e}")

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
        result = cmd.run_command(["gh", "pr", "checkout", str(pr_number)])

        if result.success:
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
        # Get PR branch information
        branch_name = pr_data.get("head", {}).get("ref", f"issue-{pr_number}")

        log_action(f"Attempting manual checkout of branch '{branch_name}' for PR #{pr_number}")

        # Fetch the PR branch
        fetch_result = cmd.run_command(["git", "fetch", "origin", f"pull/{pr_number}/head:{branch_name}"])
        if not fetch_result.success:
            log_action(f"Failed to fetch PR #{pr_number} branch", False, fetch_result.stderr)
            return False

        # Force checkout the branch (create or reset)
        checkout_result = git_checkout_branch(branch_name, create_new=True, base_branch=branch_name)
        if checkout_result.success:
            log_action(f"Successfully manually checked out PR #{pr_number}")
            return True
        else:
            log_action(
                f"Failed to manually checkout PR #{pr_number}",
                False,
                checkout_result.stderr,
            )
            return False

    except Exception as e:
        logger.error(f"Error manually checking out PR #{pr_number}: {e}")
        return False


def _update_with_base_branch(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
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
        result = cmd.run_command(["git", "rev-list", "--count", f"HEAD..origin/{target_branch}"])
        if not result.success:
            actions.append(f"Failed to check {target_branch} branch status: {result.stderr}")
            return actions

        commits_behind = int(result.stdout.strip())
        if commits_behind == 0:
            actions.append(f"PR #{pr_number} is up to date with {target_branch} branch")
            return actions

        actions.append(f"PR #{pr_number} is {commits_behind} commits behind {target_branch}, updating...")

        # Try to merge base branch
        result = cmd.run_command(["git", "merge", f"origin/{target_branch}"])
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
            from .conflict_resolver import _perform_base_branch_merge_and_conflict_resolution

            conflict_resolved = _perform_base_branch_merge_and_conflict_resolution(
                pr_number,
                target_branch,
                config,
                repo_name,
                pr_data,
                dry_run,
            )

            if conflict_resolved:
                actions.append(f"Successfully resolved merge conflicts for PR #{pr_number}")
                actions.append("ACTION_FLAG:SKIP_ANALYSIS")
            else:
                actions.append(f"Failed to resolve merge conflicts for PR #{pr_number}")

    except Exception as e:
        actions.append(f"Error updating with base branch for PR #{pr_number}: {e}")

    return actions


def _extract_linked_issues_from_pr_body(pr_body: str) -> List[int]:
    """Extract issue numbers from PR body using GitHub's linking keywords.

    Supports keywords: close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved
    Formats: #123, owner/repo#123

    Args:
        pr_body: PR description/body text

    Returns:
        List of issue numbers found in the PR body
    """
    if not pr_body:
        return []

    # GitHub's supported keywords for linking issues
    keywords = r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"

    # Pattern to match: keyword #123 or keyword owner/repo#123
    # We only extract the issue number, ignoring cross-repo references for now
    pattern = rf"{keywords}\s+(?:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)?#(\d+)"

    matches = re.finditer(pattern, pr_body, re.IGNORECASE)
    issue_numbers = [int(m.group(1)) for m in matches]

    # Remove duplicates while preserving order
    seen = set()
    unique_issues = []
    for num in issue_numbers:
        if num not in seen:
            seen.add(num)
            unique_issues.append(num)

    return unique_issues


def _close_linked_issues(repo_name: str, pr_number: int) -> None:
    """Close issues linked in the PR body after successful merge.

    Args:
        repo_name: Repository name (owner/repo)
        pr_number: PR number that was merged
    """
    try:
        # Get PR body
        result = cmd.run_command(["gh", "pr", "view", str(pr_number), "--repo", repo_name, "--json", "body"])

        if not result.success or not result.stdout:
            logger.debug(f"Could not retrieve PR #{pr_number} body for issue linking")
            return

        pr_data = json.loads(result.stdout)
        pr_body = pr_data.get("body", "")

        # Extract linked issues
        linked_issues = _extract_linked_issues_from_pr_body(pr_body)

        if not linked_issues:
            logger.debug(f"No linked issues found in PR #{pr_number} body")
            return

        # Close each linked issue
        for issue_num in linked_issues:
            try:
                close_result = cmd.run_command(
                    [
                        "gh",
                        "issue",
                        "close",
                        str(issue_num),
                        "--repo",
                        repo_name,
                        "--comment",
                        f"Closed by PR #{pr_number}",
                    ]
                )

                if close_result.success:
                    logger.info(f"Closed issue #{issue_num} linked from PR #{pr_number}")
                    log_action(f"Closed issue #{issue_num} (linked from PR #{pr_number})")
                else:
                    logger.warning(f"Failed to close issue #{issue_num}: {close_result.stderr}")
            except Exception as e:
                logger.warning(f"Error closing issue #{issue_num}: {e}")

    except Exception as e:
        logger.warning(f"Error processing linked issues for PR #{pr_number}: {e}")


def _merge_pr(
    repo_name: str,
    pr_number: int,
    analysis: Dict[str, Any],
    config: AutomationConfig,
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

        # Try with --auto first if enabled, but fallback to direct merge if it fails
        if config.MERGE_AUTO:
            auto_cmd = cmd_list + ["--auto", config.MERGE_METHOD]
            result = cmd.run_command(auto_cmd)

            if result.success:
                log_action(f"Successfully auto-merged PR #{pr_number}")
                _close_linked_issues(repo_name, pr_number)
                return True
            else:
                # Log the auto-merge failure but continue with direct merge
                logger.warning(f"Auto-merge failed for PR #{pr_number}: {result.stderr}")
                log_action(f"Auto-merge failed for PR #{pr_number}, attempting direct merge")

        # Direct merge without --auto flag
        direct_cmd = cmd_list + [config.MERGE_METHOD]
        result = cmd.run_command(direct_cmd)

        if result.success:
            log_action(f"Successfully merged PR #{pr_number}")
            _close_linked_issues(repo_name, pr_number)
            return True
        else:
            # Check if the failure is due to merge conflicts
            if "not mergeable" in result.stderr.lower() or "merge commit cannot be cleanly created" in result.stderr.lower():
                logger.info(f"PR #{pr_number} has merge conflicts, attempting to resolve...")
                log_action(f"PR #{pr_number} has merge conflicts, attempting resolution")

                # Try to resolve merge conflicts using the new function from conflict_resolver
                if resolve_pr_merge_conflicts(repo_name, pr_number, config):
                    # Retry merge after conflict resolution
                    retry_result = cmd.run_command(direct_cmd)
                    if retry_result.success:
                        log_action(f"Successfully merged PR #{pr_number} after conflict resolution")
                        _close_linked_issues(repo_name, pr_number)
                        return True
                    else:
                        # Simple non-LLM fallbacks
                        log_action(
                            f"Failed to merge PR #{pr_number} even after conflict resolution",
                            False,
                            retry_result.stderr,
                        )
                        # 1) Poll mergeable briefly (e.g., GitHub may still be computing)
                        if _poll_pr_mergeable(repo_name, pr_number, config):
                            retry_after_poll = cmd.run_command(direct_cmd)
                            if retry_after_poll.success:
                                log_action(f"Successfully merged PR #{pr_number} after waiting for mergeable state")
                                _close_linked_issues(repo_name, pr_number)
                                return True
                        # 2) Try alternative merge methods allowed by repo
                        allowed = _get_allowed_merge_methods(repo_name)
                        # Preserve order preference: configured first, then others
                        methods_order = [config.MERGE_METHOD] + [m for m in ["--squash", "--merge", "--rebase"] if m != config.MERGE_METHOD]
                        for m in methods_order:
                            if m not in allowed or m == config.MERGE_METHOD:
                                continue
                            alt_cmd = cmd_list + [m]
                            alt_result = cmd.run_command(alt_cmd)
                            if alt_result.success:
                                log_action(f"Successfully merged PR #{pr_number} with fallback method {m}")
                                _close_linked_issues(repo_name, pr_number)
                                return True
                        return False
                else:
                    log_action(f"Failed to resolve merge conflicts for PR #{pr_number}")
                    return False
            else:
                log_action(f"Failed to merge PR #{pr_number}", False, result.stderr)
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
            result = cmd.run_command(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo_name,
                    "--json",
                    "mergeable,mergeStateStatus",
                ]
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
        result = cmd.run_command(
            [
                "gh",
                "repo",
                "view",
                repo_name,
                "--json",
                "mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed",
            ]
        )
        allowed: List[str] = []
        if result.stdout:
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
        checkout_result = cmd.run_command(["gh", "pr", "checkout", str(pr_number)])

        if not checkout_result.success:
            logger.error(f"Failed to checkout PR #{pr_number}: {checkout_result.stderr}")
            return False

        # Step 1.5: Get PR details to determine the target base branch
        pr_details_result = cmd.run_command(["gh", "pr", "view", str(pr_number), "--json", "base"])
        if not pr_details_result.success:
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
        logger.info(f"Merging origin/{base_branch} into PR #{pr_number}")
        merge_result = cmd.run_command(["git", "merge", f"origin/{base_branch}"])

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
                False,
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
    dry_run: bool,
    github_logs: str,
) -> List[str]:
    """Fix PR issues using GitHub Actions logs first, then local testing loop."""
    actions = []
    pr_number = pr_data["number"]

    try:
        # Step 1: Initial fix using GitHub Actions logs
        actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")

        initial_fix_actions = _apply_github_actions_fix(repo_name, pr_data, config, dry_run, github_logs)
        actions.extend(initial_fix_actions)

        # Step 2: Local testing and iterative fixing loop
        attempts_limit = config.MAX_FIX_ATTEMPTS
        attempt = 0
        while True:
            with ProgressStage(f"attempt: {attempt}"):
                try:
                    check_for_updates_and_restart()
                except SystemExit:
                    raise
                except Exception:
                    logger.warning("Auto-update check failed during PR fix loop", exc_info=True)
                attempt += 1
                actions.append(f"Running local tests (attempt {attempt}/{attempts_limit})")

                with ProgressStage(f"Running local tests"):
                    test_result = run_local_tests(config)

                if test_result["success"]:
                    actions.append(f"Local tests passed on attempt {attempt}")
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
                        local_fix_actions = _apply_local_test_fix(repo_name, pr_data, config, dry_run, test_result)
                        actions.extend(local_fix_actions)

    except Exception as e:
        actions.append(f"Error fixing PR issues with testing for PR #{pr_number}: {e}")

    return actions


def _apply_github_actions_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
    github_logs: str,
) -> List[str]:
    """Apply initial fix using GitHub Actions error logs.

    Implementation updated: The LLM is instructed to edit files only; committing
    and pushing are handled by this code after a conflict-marker check.
    """
    actions: List[str] = []
    pr_number = pr_data["number"]

    try:
        # Get commit log since branch creation
        commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

        # Create prompt for GitHub Actions error fix (no commit/push by LLM)
        fix_prompt = render_prompt(
            "pr.github_actions_fix",
            pr_number=pr_number,
            repo_name=repo_name,
            pr_title=pr_data.get("title", "Unknown"),
            github_logs=(github_logs or "")[: config.MAX_PROMPT_SIZE],
            commit_log=commit_log or "(No commit history)",
        )
        logger.debug(
            "Prepared GitHub Actions fix prompt for PR #%s (preview: %s)",
            pr_number,
            fix_prompt[:160].replace("\n", " "),
        )

        if not dry_run:

            # Use LLM backend manager to run the prompt
            logger.info(f"Requesting LLM GitHub Actions fix for PR #{pr_number}")
            response = run_llm_prompt(fix_prompt)

            if response:
                response_preview = response.strip()[: config.MAX_RESPONSE_SIZE] if response.strip() else "No response"
                actions.append(f"Applied GitHub Actions fix: {response_preview}...")
            else:
                actions.append("No response from LLM for GitHub Actions fix")

            # Stage, then commit/push via helpers
            add_res = cmd.run_command(["git", "add", "."])
            if not add_res.success:
                actions.append(f"Failed to stage changes: {add_res.stderr}")
                return actions

            # flagged = _scan_conflict_markers()
            # if flagged:
            #     actions.append(
            #         f"Conflict markers detected in {len(flagged)} file(s): {', '.join(sorted(set(flagged)))}. Aborting commit."
            #     )
            #     return actions

            # commit_res = _commit_with_message(
            #     f"Auto-Coder: Fix GitHub Actions failures for PR #{pr_number}"
            # )
            # if commit_res.success:
            #     actions.append("Committed changes")
            #     push_res = _push_current_branch()
            #     if push_res.success:
            #         actions.append("Pushed changes")
            #     else:
            #         actions.append(f"Failed to push changes: {push_res.stderr}")
            # else:
            #     if 'nothing to commit' in (commit_res.stdout or ''):
            #         actions.append("No changes to commit")
            #     else:
            #         actions.append(f"Failed to commit changes: {commit_res.stderr or commit_res.stdout}")
        else:
            actions.append(f"[DRY RUN] Would apply GitHub Actions fix for PR #{pr_number}")

    except Exception as e:
        actions.append(f"Error applying GitHub Actions fix for PR #{pr_number}: {e}")

    return actions


def _apply_local_test_fix(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    dry_run: bool,
    test_result: Dict[str, Any],
) -> List[str]:
    """Apply fix using local test failure logs.

    This function uses the LLM backend manager to apply fixes based on local test failures,
    similar to apply_workspace_test_fix in fix_to_pass_tests_runner.py.
    """
    with ProgressStage(f"Local test fix"):
        actions = []
        pr_number = pr_data["number"]

        try:
            # Extract important error information
            error_summary = extract_important_errors(test_result)

            if not error_summary:
                actions.append(f"No actionable errors found in local test output for PR #{pr_number}")
                logger.info("Skipping LLM local test fix because no actionable errors were extracted")
                return actions

            # Get commit log since branch creation
            commit_log = get_commit_log(base_branch=config.MAIN_BRANCH)

            # Create prompt for local test error fix
            fix_prompt = render_prompt(
                "pr.local_test_fix",
                pr_number=pr_number,
                repo_name=repo_name,
                pr_title=pr_data.get("title", "Unknown"),
                error_summary=error_summary[: config.MAX_PROMPT_SIZE],
                test_command=test_result.get("command", "pytest -q --maxfail=1"),
                commit_log=commit_log or "(No commit history)",
            )
            logger.debug(
                "Prepared local test fix prompt for PR #%s (preview: %s)",
                pr_number,
                fix_prompt[:160].replace("\n", " "),
            )

            if dry_run:
                actions.append(f"[DRY RUN] Would apply local test fix for PR #{pr_number}")
                return actions

            # Use LLM backend manager to run the prompt
            # Check if llm_client has run_test_fix_prompt method (BackendManager)
            # or fall back to _run_llm_cli
            logger.info(f"Requesting LLM local test fix for PR #{pr_number}")

            # BackendManager with test file tracking
            response = get_llm_backend_manager().run_test_fix_prompt(fix_prompt, current_test_file=None)

            if response:
                response_preview = response.strip()[: config.MAX_RESPONSE_SIZE] if response.strip() else "No response"
                actions.append(f"Applied local test fix: {response_preview}...")
            else:
                actions.append("No response from LLM for local test fix")

        except Exception as e:
            actions.append(f"Error applying local test fix for PR #{pr_number}: {e}")
            logger.error(f"Error applying local test fix for PR #{pr_number}: {e}", exc_info=True)

        return actions
