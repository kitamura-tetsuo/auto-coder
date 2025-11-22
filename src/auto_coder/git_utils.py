"""
Git utilities for Auto-Coder.
"""

import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import urlparse

from auto_coder.automation_config import AutomationConfig
from auto_coder.backend_manager import get_message_backend_manager, run_llm_prompt

try:
    from git import InvalidGitRepositoryError, Repo

    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

from .git_branch import (
    branch_exists,
    extract_number_from_branch,
    git_checkout_branch,
    git_commit_with_retry,
    try_llm_commit_push,
    try_llm_dprint_fallback,
    validate_branch_name,
)
from .git_commit import (
    ensure_pushed,
    git_push,
    save_commit_failure_history,
)
from .git_info import (
    check_unpushed_commits,
    get_commit_log,
    get_current_branch,
    get_current_repo_name,
    is_git_repository,
    parse_github_repo_from_url,
)
from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor, CommandResult

logger = get_logger(__name__)


def switch_to_branch(
    branch_name: str,
    create_new: bool = False,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    publish: bool = True,
    pull_after_switch: bool = True,
) -> CommandResult:
    """
    Switch to a git branch and automatically pull latest changes.

    This function centralizes branch switching operations with automatic pull.
    It combines git_checkout_branch with automatic pull to ensure the branch
    is synchronized with the remote repository.

    Args:
        branch_name: Name of the branch to switch to
        create_new: If True, creates a new branch with -b flag
        base_branch: If create_new is True and base_branch is specified, creates
                     the new branch from base_branch (using -B flag)
        cwd: Optional working directory for the git command
        publish: If True and create_new is True, push the new branch to remote and set up tracking
        pull_after_switch: If True, automatically pull latest changes after successful checkout

    Returns:
        CommandResult with the result of the checkout operation.
        success=True only if checkout succeeded AND (pull succeeded if requested) AND
        current branch matches expected branch.
    """
    # First, checkout the branch using existing logic
    checkout_result = git_checkout_branch(
        branch_name=branch_name,
        create_new=create_new,
        base_branch=base_branch,
        cwd=cwd,
        publish=publish,
    )

    if not checkout_result.success:
        logger.error(f"Failed to checkout branch '{branch_name}': {checkout_result.stderr}")
        return checkout_result

    # If pull is not requested, return the checkout result
    if not pull_after_switch:
        logger.info(f"Successfully switched to branch '{branch_name}' (skipping pull)")
        return checkout_result

    # Pull the latest changes from remote
    logger.info(f"Pulling latest changes for branch '{branch_name}'...")
    pull_result = git_pull(remote="origin", branch=branch_name, cwd=cwd)

    if not pull_result.success:
        logger.error(f"Failed to pull latest changes for branch '{branch_name}': {pull_result.stderr}")
        # Return a combined result showing both the checkout and pull results
        return CommandResult(
            success=False,
            stdout=f"Checkout: {checkout_result.stdout}\nPull: {pull_result.stdout}",
            stderr=f"Checkout: {checkout_result.stderr}\nPull: {pull_result.stderr}",
            returncode=pull_result.returncode,
        )

    logger.info(f"Successfully switched to branch '{branch_name}' and pulled latest changes")
    return CommandResult(
        success=True,
        stdout=f"Checkout: {checkout_result.stdout}\nPull: {pull_result.stdout}",
        stderr=f"Checkout: {checkout_result.stderr}\nPull: {pull_result.stderr}",
        returncode=0,
    )


def resolve_pull_conflicts(cwd: Optional[str] = None, merge_method: str = "merge") -> CommandResult:
    """
    Resolve pull conflicts by attempting merge/rebase strategies.

    Args:
        cwd: Optional working directory for the git command
        merge_method: Strategy to resolve conflicts - "merge" or "rebase"

    Returns:
        CommandResult with the result of the conflict resolution
    """
    cmd = CommandExecutor()
    logger.info(f"Attempting to resolve pull conflicts using {merge_method} strategy")

    # First, abort any ongoing merge/rebase to start clean
    abort_result = cmd.run_command(["git", "merge", "--abort"], cwd=cwd)
    if not abort_result.success:
        abort_result = cmd.run_command(["git", "rebase", "--abort"], cwd=cwd)

    try:
        if merge_method == "rebase":
            # Try rebase first
            logger.info("Attempting git rebase to resolve pull conflicts")
            rebase_result = cmd.run_command(["git", "rebase", "origin/HEAD"], cwd=cwd)

            if rebase_result.success:
                logger.info("Successfully resolved pull conflicts using rebase")
                return CommandResult(
                    success=True,
                    stdout="Pull conflicts resolved via rebase",
                    stderr="",
                    returncode=0,
                )
            else:
                # If rebase fails, fall back to merge
                logger.warning(f"Rebase failed: {rebase_result.stderr}, trying merge strategy")
                return resolve_pull_conflicts(cwd, "merge")
        else:
            # Default: try merge strategy
            logger.info("Attempting git merge to resolve pull conflicts")
            merge_result = cmd.run_command(["git", "merge", "--no-ff", "origin/HEAD"], cwd=cwd)

            if merge_result.success:
                logger.info("Successfully resolved pull conflicts using merge")
                return CommandResult(
                    success=True,
                    stdout="Pull conflicts resolved via merge",
                    stderr="",
                    returncode=0,
                )
            else:
                # Check if it's actually a conflict or another error
                if "conflict" in merge_result.stderr.lower():
                    logger.error(f"Merge conflicts detected: {merge_result.stderr}")
                    # For now, return the merge result so the caller can handle conflicts
                    return merge_result
                else:
                    logger.error(f"Merge failed for non-conflict reasons: {merge_result.stderr}")
                    return merge_result

    except Exception as e:
        logger.error(f"Error during pull conflict resolution: {e}")
        return CommandResult(
            success=False,
            stdout="",
            stderr=f"Error resolving pull conflicts: {e}",
            returncode=1,
        )


def git_pull(
    remote: str = "origin",
    branch: Optional[str] = None,
    cwd: Optional[str] = None,
) -> CommandResult:
    """
    Perform git pull with comprehensive error handling and conflict resolution.

    This function centralizes git pull operations and handles various scenarios:
    - Standard pull operations
    - Merge conflicts
    - Diverging branches
    - No tracking information (new branches)

    Args:
        remote: Remote name (default: 'origin')
        branch: Optional branch name. If None, uses current branch
        cwd: Optional working directory for the git command

    Returns:
        CommandResult with the result of the pull operation
    """
    cmd = CommandExecutor()

    # Determine which branch to pull
    target_branch = branch
    if not target_branch:
        branch_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        if not branch_result.success:
            logger.warning(f"Failed to get current branch: {branch_result.stderr}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Failed to get current branch: {branch_result.stderr}",
                returncode=branch_result.returncode,
            )
        target_branch = branch_result.stdout.strip()

    logger.info(f"Pulling latest changes from {remote}/{target_branch}...")
    pull_result = cmd.run_command(["git", "pull", remote, target_branch], cwd=cwd)

    if pull_result.success:
        logger.info(f"Successfully pulled latest changes from {remote}/{target_branch}")
        return pull_result

    # Handle various error cases
    error_msg = pull_result.stderr.lower()

    # Check if it's a "no tracking information" error (new branch)
    if "no tracking information" in error_msg or "fatal: no such ref was fetched" in error_msg:
        logger.warning(f"No remote tracking information for branch '{target_branch}', skipping pull")
        # This is not a critical error for new branches
        return CommandResult(
            success=True,  # Treat as success for new branches
            stdout=f"No remote tracking information for branch '{target_branch}'",
            stderr=pull_result.stderr,
            returncode=0,
        )

    # Check if it's a "diverging branches" error
    if "diverging branches" in error_msg or "not possible to fast-forward" in error_msg:
        logger.info(f"Detected diverging branches for branch '{target_branch}', attempting to resolve...")

        # Try to resolve pull conflicts using our conflict resolution function
        conflict_result = resolve_pull_conflicts(cwd=cwd, merge_method="merge")

        if conflict_result.success:
            logger.info(f"Successfully resolved pull conflicts for branch '{target_branch}'")
            return CommandResult(
                success=True,
                stdout=f"Pull with conflict resolution: {conflict_result.stdout}",
                stderr=conflict_result.stderr,
                returncode=0,
            )
        else:
            logger.warning(f"Failed to resolve pull conflicts for branch '{target_branch}': {conflict_result.stderr}")
            # Return the conflict resolution result
            return conflict_result

    # Check for merge conflicts during pull
    if "conflict" in error_msg or "merge conflict" in error_msg:
        logger.info(f"Detected merge conflicts during pull, attempting to resolve...")
        conflict_result = resolve_pull_conflicts(cwd=cwd, merge_method="merge")

        if conflict_result.success:
            logger.info(f"Successfully resolved pull conflicts")
            return CommandResult(
                success=True,
                stdout=f"Pull with conflict resolution: {conflict_result.stdout}",
                stderr=conflict_result.stderr,
                returncode=0,
            )
        else:
            logger.warning(f"Failed to resolve pull conflicts: {conflict_result.stderr}")
            return conflict_result

    # Other errors
    logger.error(f"Failed to pull latest changes: {pull_result.stderr}")
    return pull_result


def commit_and_push_changes(
    result_data: Dict[str, Any],
    repo_name: Optional[str] = None,
    issue_number: Optional[int] = None,
) -> str:
    """
    Commit changes and push them to remote using centralized git helper.

    This function handles the complete commit-and-push workflow, including
    handling non-fast-forward errors by pulling and retrying.

    Args:
        result_data: Dictionary containing 'summary' key with commit message
        repo_name: Repository name (e.g., 'owner/repo') for history saving
        issue_number: Issue number for context in history
        llm_client: LLM backend manager for commit/push operations
        message_backend_manager: Message backend manager for commit/push operations

    Returns:
        Action message describing the commit result
    """
    cmd = CommandExecutor()

    summary = result_data.get("summary", "Auto-Coder: Automated changes")

    # Check if there are any changes to commit
    status_result = cmd.run_command(["git", "status", "--porcelain"])
    if not status_result.stdout.strip():
        return "No changes to commit"

    # Stage all changes
    add_result = cmd.run_command(["git", "add", "-A"])
    if not add_result.success:
        return f"Failed to stage changes: {add_result.stderr}"

    # Commit using centralized helper with dprint retry logic
    commit_result = git_commit_with_retry(summary)

    if commit_result.success:
        # Use ensure_pushed which handles non-fast-forward errors and LLM fallback
        push_result = git_push(
            commit_message=summary,
        )

        if push_result.success:
            return f"Successfully committed and pushed changes: {summary}"
        else:
            logger.error(f"Failed to push changes after retry: {push_result.stderr}")
            return f"Failed to commit and push changes: {push_result.stderr}"
    else:
        logger.info("Attempting to resolve commit failure using LLM...")
        llm_success = try_llm_commit_push(
            summary,
            commit_result.stderr,
        )
        if llm_success:
            return f"Successfully committed and pushed changes using LLM: {summary}"
        else:
            logger.error("LLM failed to resolve commit failure")
            # Save history and exit immediately
            context = {
                "type": "issue",
                "issue_number": issue_number,
                "commit_message": summary,
            }
            save_commit_failure_history(commit_result.stderr, context, repo_name)
            # This line will never be reached due to sys.exit in save_commit_failure_history
            return f"Failed to commit changes: {commit_result.stderr}"


def get_all_branches(cwd: Optional[str] = None, remote: bool = True) -> List[str]:
    """
    Get all branch names from the repository.

    Args:
        cwd: Optional working directory for git command
        remote: If True, include remote branches; if False, only local branches

    Returns:
        List of branch names (without remote prefix if remote is True)
    """
    cmd = CommandExecutor()
    if remote:
        result = cmd.run_command(["git", "branch", "-r", "--format=%(refname:short)"], cwd=cwd)
    else:
        result = cmd.run_command(["git", "branch", "--format=%(refname:short)"], cwd=cwd)

    if not result.success:
        logger.error(f"Failed to get branches: {result.stderr}")
        return []

    branches = [b.strip() for b in result.stdout.split("\n") if b.strip()]
    return branches


def get_branches_by_pattern(pattern: str, cwd: Optional[str] = None, remote: bool = True) -> List[str]:
    """
    Get all branches matching a specific pattern.

    Args:
        pattern: Branch name pattern to match (e.g., "pr-*", "issue-*")
        cwd: Optional working directory for git command
        remote: If True, search in remote branches; if False, only local branches

    Returns:
        List of branch names matching the pattern
    """
    all_branches = get_all_branches(cwd=cwd, remote=remote)
    matching_branches = []

    for branch in all_branches:
        # Remove remote prefix if present
        branch_name = branch.split("/", 1)[-1] if "/" in branch else branch
        # Check if branch matches the pattern (support wildcards)
        if "*" in pattern:
            # Convert glob pattern to regex
            regex_pattern = "^" + pattern.replace("*", ".*") + "$"
            if re.match(regex_pattern, branch_name, re.IGNORECASE):
                matching_branches.append(branch)
        else:
            # Exact match
            if branch_name.lower() == pattern.lower():
                matching_branches.append(branch)

    return matching_branches


def migrate_pr_branches(
    config: AutomationConfig,
    cwd: Optional[str] = None,
    delete_after_merge: bool = True,
    force: bool = False,
    execute: bool = False,
) -> Dict[str, Any]:
    """
    Migrate existing pr-<number> branches to their corresponding issue-<number> branches.

    This function:
    1. Scans for all branches matching the pr-<number> pattern
    2. For each pr-xx branch, checks if an issue-xx branch exists
    3. If issue-xx exists, merges pr-xx into issue-xx
    4. If issue-xx doesn't exist, creates it from pr-xx
    5. Deletes the pr-xx branch after successful merge (if delete_after_merge is True)

    Args:
        config: AutomationConfig instance
        cwd: Optional working directory for git command
        delete_after_merge: If True, delete pr-<number> branch after successful merge
        force: If True, proceed even if there are merge conflicts
        execute: If True, perform actual migration. If False, only preview what would be done.

    Returns:
        Dictionary with migration results:
        - 'success': Overall success status
        - 'migrated': List of successfully migrated branches
        - 'skipped': List of skipped branches with reasons
        - 'failed': List of failed migrations with error messages
        - 'conflicts': List of branches with merge conflicts
    """
    cmd = CommandExecutor()
    results: Dict[str, Any] = {
        "success": True,
        "migrated": [],
        "skipped": [],
        "failed": [],
        "conflicts": [],
    }

    mode = "EXECUTE" if execute else "DRY-RUN"
    logger.info(f"Starting branch migration ({mode} mode, delete_after_merge={delete_after_merge})")

    # Get all pr-<number> branches
    pr_branches = get_branches_by_pattern("pr-*", cwd=cwd, remote=False)

    if not pr_branches:
        logger.info("No pr-<number> branches found")
        return results

    logger.info(f"Found {len(pr_branches)} pr-<number> branch(es): {', '.join(pr_branches)}")

    for pr_branch in pr_branches:
        # Remove local branch prefix
        branch_name = pr_branch.split("/", 1)[-1] if "/" in pr_branch else pr_branch

        # Extract number from pr-<number> branch
        pr_number = extract_number_from_branch(branch_name)
        if pr_number is None:
            logger.warning(f"Could not extract number from branch '{branch_name}', skipping")
            results["skipped"].append({"branch": branch_name, "reason": "Could not extract issue number"})
            continue

        # Determine corresponding issue-<number> branch name
        issue_branch_name = f"issue-{pr_number}"

        logger.info(f"Processing: {branch_name} -> {issue_branch_name}")

        # Actual migration
        try:
            # Check if we're already on the branch we want to migrate
            current_branch = get_current_branch(cwd=cwd)
            if current_branch == branch_name:
                # Switch to a safe branch first
                logger.info(f"Currently on {branch_name}, switching to main before migration")
                if execute:
                    switch_result = cmd.run_command(["git", "checkout", "main"], cwd=cwd)
                    if not switch_result.success:
                        # Try main as fallback
                        switch_result = cmd.run_command(["git", "checkout", "refs/remotes/origin/main"], cwd=cwd)
                else:
                    logger.info(f"[DRY-RUN] Would switch from {branch_name} to main")

            # Check if issue-<number> branch exists
            if branch_exists(issue_branch_name, cwd=cwd):
                # Issue branch exists, perform merge
                logger.info(f"Issue branch '{issue_branch_name}' exists, merging {branch_name}")

                if execute:
                    # Switch to issue branch
                    checkout_result = git_checkout_branch(issue_branch_name, create_new=False, cwd=cwd)
                    if not checkout_result.success:
                        error_msg = f"Failed to checkout issue branch '{issue_branch_name}': {checkout_result.stderr}"
                        logger.error(error_msg)
                        results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": error_msg})
                        results["success"] = False
                        continue

                    # Pull latest changes from issue branch
                    logger.info(f"Pulling latest changes for {issue_branch_name}")
                    pull_result = git_pull(remote="origin", branch=issue_branch_name, cwd=cwd)
                    if not pull_result.success:
                        logger.warning(f"Failed to pull latest changes for {issue_branch_name}: {pull_result.stderr}")
                else:
                    logger.info(f"[DRY-RUN] Would checkout and merge {branch_name} into {issue_branch_name}")

                # Merge pr branch
                logger.info(f"Merging {branch_name} into {issue_branch_name}")
                if execute:
                    merge_result = cmd.run_command(["git", "merge", f"origin/{branch_name}" if "/" not in branch_name else branch_name, "--no-ff", "-m", f"Merge {branch_name} into {issue_branch_name}"], cwd=cwd)

                    if not merge_result.success:
                        # Check if it's a merge conflict
                        if "conflict" in merge_result.stderr.lower():
                            logger.error(f"Merge conflict detected while merging {branch_name} into {issue_branch_name}")
                            results["conflicts"].append({"from": branch_name, "to": issue_branch_name, "error": merge_result.stderr})

                            if not force:
                                # Abort the merge and skip
                                cmd.run_command(["git", "merge", "--abort"], cwd=cwd)
                                logger.info(f"Aborted merge, skipping {branch_name}")
                                results["skipped"].append({"from": branch_name, "to": issue_branch_name, "reason": "Merge conflict (use --force to auto-resolve)"})
                                results["success"] = False
                                continue
                            else:
                                # Try to auto-resolve conflicts
                                logger.info(f"Attempting to auto-resolve conflicts for {branch_name}")
                                add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
                                if add_result.success:
                                    commit_result = git_commit_with_retry(f"Resolve conflicts from {branch_name}", cwd=cwd)
                                    if not commit_result.success:
                                        error_msg = f"Failed to commit conflict resolution: {commit_result.stderr}"
                                        logger.error(error_msg)
                                        results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": error_msg})
                                        results["success"] = False
                                        continue
                                else:
                                    error_msg = f"Failed to stage conflict resolution: {add_result.stderr}"
                                    logger.error(error_msg)
                                    results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": error_msg})
                                    results["success"] = False
                                    continue
                        else:
                            # Non-conflict error
                            error_msg = f"Merge failed: {merge_result.stderr}"
                            logger.error(error_msg)
                            results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": error_msg})
                            results["success"] = False
                            continue

                    # Push the merged changes
                    push_result = git_push(cwd=cwd, commit_message=f"Merged {branch_name} into {issue_branch_name}")
                    if not push_result.success:
                        logger.warning(f"Failed to push merged changes: {push_result.stderr}")
                        # Don't fail the entire migration for push issues
                else:
                    if force:
                        logger.info(f"[DRY-RUN] Would merge {branch_name} into {issue_branch_name} (with --force auto-resolve)")
                    else:
                        logger.info(f"[DRY-RUN] Would merge {branch_name} into {issue_branch_name}")
                    logger.info(f"[DRY-RUN] Would push merged changes to origin")
            else:
                # Issue branch doesn't exist, rename pr branch to issue branch
                logger.info(f"Issue branch '{issue_branch_name}' does not exist, creating from {branch_name}")

                if execute:
                    # Get the commit hash of pr branch
                    rev_result = cmd.run_command(["git", "rev-parse", branch_name], cwd=cwd)
                    if not rev_result.success:
                        error_msg = f"Failed to get commit hash for {branch_name}: {rev_result.stderr}"
                        logger.error(error_msg)
                        results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": error_msg})
                        results["success"] = False
                        continue

                    # Create new issue branch from pr branch
                    checkout_result = git_checkout_branch(issue_branch_name, create_new=True, base_branch=branch_name, cwd=cwd)
                    if not checkout_result.success:
                        error_msg = f"Failed to create issue branch '{issue_branch_name}': {checkout_result.stderr}"
                        logger.error(error_msg)
                        results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": error_msg})
                        results["success"] = False
                        continue

                    # Push the new branch
                    push_result = git_push(cwd=cwd, commit_message=f"Created {issue_branch_name} from {branch_name}")
                    if not push_result.success:
                        logger.warning(f"Failed to push new branch: {push_result.stderr}")
                        # Don't fail the entire migration for push issues
                else:
                    logger.info(f"[DRY-RUN] Would create new branch '{issue_branch_name}' from {branch_name}")
                    logger.info(f"[DRY-RUN] Would push new branch to origin")

            # Delete pr branch after successful migration
            if delete_after_merge:
                logger.info(f"Deleting pr branch '{branch_name}'")
                if execute:
                    delete_result = cmd.run_command(["git", "branch", "-D", branch_name], cwd=cwd)
                    if delete_result.success:
                        # Also delete from remote
                        cmd.run_command(["git", "push", "origin", "--delete", branch_name], cwd=cwd)
                        logger.info(f"Successfully deleted pr branch '{branch_name}'")
                    else:
                        logger.warning(f"Failed to delete local pr branch '{branch_name}': {delete_result.stderr}")
                else:
                    logger.info(f"[DRY-RUN] Would delete pr branch '{branch_name}' (local and remote)")

            if execute:
                logger.info(f"Successfully migrated {branch_name} -> {issue_branch_name}")
            else:
                logger.info(f"[DRY-RUN] Would mark as migrated: {branch_name} -> {issue_branch_name}")
            results["migrated"].append({"from": branch_name, "to": issue_branch_name})

        except Exception as e:
            error_msg = f"Unexpected error during migration: {e}"
            logger.error(error_msg)
            results["failed"].append({"from": branch_name, "to": issue_branch_name, "error": str(e)})
            results["success"] = False

    logger.info(f"Branch migration completed. Migrated: {len(results['migrated'])}, Skipped: {len(results['skipped'])}, Failed: {len(results['failed'])}")
    return results


@contextmanager
def branch_context(
    branch_name: str,
    create_new: bool = False,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    check_unpushed: bool = True,
    remote: str = "origin",
) -> Generator[None, None, None]:
    """
    Context manager for Git branch management.

    This context manager automatically switches to the specified branch on entry,
    checks for unpushed commits, and returns to the main branch on exit (even if
    an exception occurs).

    Args:
        branch_name: Name of the branch to switch to
        create_new: If True, creates a new branch with -b flag
        base_branch: If create_new is True and base_branch is specified, creates
                     the new branch from base_branch (using -B flag)
        cwd: Optional working directory for the git command
        check_unpushed: If True, automatically check and push unpushed commits
                       on entry (default: True)
        remote: Remote name to use for unpushed commit checks (default: 'origin')

    Example Usage:
        # Work on a feature branch
        with branch_context("feature/issue-123"):
            # Perform work on feature/issue-123 branch
            # Branch is automatically pulled on entry
            # Unpushed commits are automatically pushed
            perform_work()
        # Automatically back on main branch after exiting context

        # Create and work on new branch
        with branch_context("feature/new-feature", create_new=True, base_branch="main"):
            # New branch created from main
            # Automatic pull after switch
            # Unpushed commits are automatically pushed
            perform_work()
        # Automatically returns to main

    Raises:
        Exception: Propagates any exceptions from branch operations
    """
    # Store the current branch to return to on exit
    original_branch = get_current_branch(cwd=cwd)

    if not original_branch:
        raise RuntimeError("Failed to get current branch before switching")

    # If already on the target branch, just yield without switching
    if original_branch == branch_name and not create_new:
        logger.info(f"Already on branch '{branch_name}', staying on current branch")
        try:
            yield
        finally:
            # Even if we're already on the branch, still need to handle cleanup properly
            pass
        return

    try:
        # On entry: switch to the target branch with automatic pull
        logger.info(f"Switching to branch '{branch_name}'")
        switch_result = switch_to_branch(
            branch_name=branch_name,
            create_new=create_new,
            base_branch=base_branch,
            cwd=cwd,
            publish=True,  # Default to publishing new branches
            pull_after_switch=True,  # Always pull after switch
        )

        if not switch_result.success:
            raise RuntimeError(f"Failed to switch to branch '{branch_name}': {switch_result.stderr}")

        # Check for and push unpushed commits if requested
        if check_unpushed:
            try:
                # Import ProgressStage here to avoid circular imports
                from .progress_footer import ProgressStage

                with ProgressStage("Checking unpushed commits"):
                    logger.info("Checking for unpushed commits before processing...")
                    push_result = ensure_pushed(cwd=cwd, remote=remote)
                    if push_result.success and "No unpushed commits" not in push_result.stdout:
                        logger.info("Successfully pushed unpushed commits")
                    elif not push_result.success:
                        logger.warning(f"Failed to push unpushed commits: {push_result.stderr}")
            except ImportError:
                # ProgressStage not available, just check and push without progress indicator
                logger.info("Checking for unpushed commits before processing...")
                push_result = ensure_pushed(cwd=cwd, remote=remote)
                if push_result.success and "No unpushed commits" not in push_result.stdout:
                    logger.info("Successfully pushed unpushed commits")
                elif not push_result.success:
                    logger.warning(f"Failed to push unpushed commits: {push_result.stderr}")

        # Yield control to the with block
        yield

    finally:
        # On exit: always return to the original branch
        # First, check if we're still in a git repository
        if is_git_repository(cwd):
            # Check if the current branch is different from the original
            current_branch = get_current_branch(cwd=cwd)

            if current_branch != original_branch:
                logger.info(f"Returning to original branch '{original_branch}'")
                return_result = switch_to_branch(
                    branch_name=original_branch,
                    cwd=cwd,
                    pull_after_switch=True,  # Always pull after switch
                )

                if not return_result.success:
                    logger.warning(f"Failed to return to branch '{original_branch}': {return_result.stderr}")
                    # Don't raise here - we're in cleanup mode
            else:
                logger.info(f"Already on branch '{original_branch}', no need to switch back")
        else:
            logger.warning("Not in a git repository during cleanup, cannot return to original branch")
