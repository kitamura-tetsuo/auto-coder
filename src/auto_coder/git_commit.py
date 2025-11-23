"""
Git commit and push utilities for Auto-Coder.

This module contains functions for committing changes and pushing to remote repositories,
with enhanced error handling for various scenarios including non-fast-forward errors,
dprint formatting issues, and upstream branch setup.
"""

from typing import Any, Dict, Optional

from .git_branch import try_llm_commit_push
from .git_info import check_unpushed_commits
from .logger_config import get_logger
from .utils import CommandExecutor, CommandResult

logger = get_logger(__name__)


def _is_no_upstream_error(msg: str) -> bool:
    """
    Check if the error message indicates no upstream branch error.

    Args:
        msg: Error message from git push

    Returns:
        True if it's a no upstream error, False otherwise
    """
    if not msg:
        return False
    s = msg.lower()
    return "no upstream branch" in s or "has no upstream" in s or "set the remote as upstream" in s or "no configured push destination" in s


def _is_dprint_push_error(msg: str) -> bool:
    """
    Check if the error message indicates dprint push hook error.

    Args:
        msg: Error message from git push

    Returns:
        True if it's a dprint push error, False otherwise
    """
    if not msg:
        return False
    s = msg.lower()
    # Look for dprint push-hook guidance
    return "dprint" in s and "output-file-paths" in s


def _perform_git_push(
    cwd: Optional[str] = None,
    remote: str = "origin",
    branch: Optional[str] = None,
    skip_unpushed_check: bool = False,
) -> CommandResult:
    """
    Actual git push implementation without recursion.

    Args:
        cwd: Optional working directory for git command
        remote: Remote name (default: 'origin')
        branch: Optional branch name. If None, pushes current branch
        skip_unpushed_check: If True, skip unpushed commits check

    Returns:
        CommandResult object with success status and output
    """
    cmd = CommandExecutor()

    # Skip unpushed commits check if explicitly skipped
    if not skip_unpushed_check:
        if not check_unpushed_commits(cwd=cwd, remote=remote):
            logger.debug("No unpushed commits found")
            return CommandResult(success=True, stdout="No unpushed commits", stderr="", returncode=0)

    # If no branch specified, try to get current branch
    if branch is None:
        branch_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        if not branch_result.success:
            logger.warning(f"Failed to get current branch: {branch_result.stderr}")
            return branch_result

        branch = branch_result.stdout.strip()

    # Construct push command
    push_cmd = ["git", "push", remote, branch]

    # Execute push
    result = cmd.run_command(push_cmd, cwd=cwd)

    if result.success:
        logger.info(f"Successfully pushed {branch} to {remote}")
    else:
        logger.warning(f"Push failed: {result.stderr}")

    return result


def _retry_with_set_upstream(
    cmd: CommandExecutor,
    remote: str,
    branch: Optional[str],
    cwd: Optional[str],
) -> CommandResult:
    """
    Retry git push with --set-upstream flag.

    Args:
        cmd: CommandExecutor instance
        remote: Remote name
        branch: Optional branch name. If None, gets current branch
        cwd: Optional working directory for git command

    Returns:
        CommandResult object with success status and output
    """
    # Resolve branch if not provided
    if branch is None:
        from .git_info import get_current_branch

        branch = get_current_branch(cwd=cwd)
        if not branch:
            return CommandResult(
                success=False,
                stdout="",
                stderr="Failed to determine current branch for --set-upstream push",
                returncode=1,
            )
    return cmd.run_command(["git", "push", "--set-upstream", remote, branch], cwd=cwd)


def git_push(
    cwd: Optional[str] = None,
    remote: str = "origin",
    branch: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> CommandResult:
    """
    Push all unpushed commits to remote with enhanced error handling.

    This function handles non-fast-forward errors by pulling first, then pushing.
    Includes enhanced dprint formatting error handling and upstream branch setup.

    Args:
        cwd: Optional working directory for git command
        remote: Remote name (default: 'origin')
        branch: Optional branch name. If None, pushes current branch
        commit_message: Optional commit message for LLM fallback

    Returns:
        CommandResult object with success status and output
    """
    # Skip unpushed commits check when branch is specified (for tests)
    skip_unpushed_check = branch is not None

    # Push unpushed commits using the actual implementation
    logger.info("Pushing unpushed commits...")
    push_result = _perform_git_push(cwd=cwd, remote=remote, branch=branch, skip_unpushed_check=skip_unpushed_check)

    # If push succeeded, return the result
    if push_result.success:
        return push_result

    cmd = CommandExecutor()

    # Handle: no upstream/tracking branch -> retry with --set-upstream
    if _is_no_upstream_error(push_result.stderr):
        logger.info("Detected no-upstream error, retrying with --set-upstream...")
        return _retry_with_set_upstream(cmd, remote, branch, cwd)

    # Handle: dprint push-hook error -> format, stage, (amend|commit), and retry push
    if _is_dprint_push_error(push_result.stderr):
        logger.info("Detected dprint-related push hook error, formatting and retrying push...")
        fmt_result = cmd.run_command(["npx", "dprint", "fmt"], cwd=cwd)
        if not fmt_result.success:
            logger.warning(f"Failed to run dprint formatter: {fmt_result.stderr}")
            # Try LLM fallback when dprint formatter execution fails
            logger.info("Attempting to resolve dprint formatter failure using LLM...")
            from .git_branch import try_llm_dprint_fallback

            llm_success = try_llm_dprint_fallback(commit_message, fmt_result.stderr)
            if not llm_success:
                logger.error("LLM failed to resolve dprint formatter failure in push operation")
                return push_result
            # Re-run dprint after LLM intervention
            fmt_result = cmd.run_command(["npx", "dprint", "fmt"], cwd=cwd)
            if not fmt_result.success:
                logger.error("dprint still fails after LLM intervention")
                return push_result

        add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
        if not add_result.success:
            logger.warning(f"Failed to stage formatted files: {add_result.stderr}")
            return push_result

        if commit_message:
            amend_result = cmd.run_command(["git", "commit", "--amend", "--no-edit"], cwd=cwd)
            if not amend_result.success:
                # Fallback to regular commit with provided message
                commit_result = cmd.run_command(["git", "commit", "-m", commit_message], cwd=cwd)
                if not commit_result.success:
                    logger.warning(f"Commit after formatting failed: {commit_result.stderr}")
                    return push_result

        # Retry push (bare push to reuse upstream/remote defaults)
        retry_result = cmd.run_command(["git", "push"], cwd=cwd)
        if retry_result.success:
            return retry_result
        # If the retry hit a no-upstream case, set upstream and return
        if _is_no_upstream_error(retry_result.stderr):
            return _retry_with_set_upstream(cmd, remote, branch, cwd)
        return retry_result

    # Check if this is a non-fast-forward error
    is_non_fast_forward = "non-fast-forward" in push_result.stderr.lower() or "Updates were rejected because the tip of your current branch is behind" in push_result.stderr or "the tip of your current branch is behind its remote counterpart" in push_result.stderr

    if is_non_fast_forward:
        logger.info("Detected non-fast-forward error, attempting to pull and retry push...")

        # Use the centralized git_pull function
        from .git_branch import git_pull

        pull_result = git_pull(remote=remote, branch=branch, cwd=cwd)

        if not pull_result.success:
            logger.warning(f"Pull failed: {pull_result.stderr}")
            logger.info("Proceeding to retry push anyway...")

        logger.info("Retrying push...")
        # Retry push using the actual implementation
        retry_push_result = _perform_git_push(
            cwd=cwd,
            remote=remote,
            branch=branch,
            skip_unpushed_check=skip_unpushed_check,
        )

        if retry_push_result.success:
            logger.info("Successfully pushed after resolving non-fast-forward error")
            return retry_push_result
        else:
            logger.warning(f"Push still failed: {retry_push_result.stderr}")
            # Update push_result for LLM fallback
            push_result = retry_push_result

    # If push still failed and we have LLM clients, try LLM fallback
    logger.info("Attempting to resolve push failure using LLM...")
    from .git_branch import try_llm_commit_push

    llm_success = try_llm_commit_push(
        commit_message,
        push_result.stderr,
    )
    if llm_success:
        logger.info("LLM successfully resolved push failure")
        return CommandResult(
            success=True,
            stdout="Successfully resolved push failure using LLM",
            stderr="",
            returncode=0,
        )
    else:
        logger.error("LLM failed to resolve push failure")

    # Return the final push result
    return push_result


def ensure_pushed(cwd: Optional[str] = None, remote: str = "origin") -> CommandResult:
    """
    Ensure all commits are pushed to remote. If there are unpushed commits, push them.

    Args:
        cwd: Optional working directory for git command
        remote: Remote name (default: 'origin')

    Returns:
        CommandResult object with success status and output
    """
    # Check if there are unpushed commits
    if not check_unpushed_commits(cwd=cwd, remote=remote):
        logger.debug("No unpushed commits found")
        return CommandResult(success=True, stdout="No unpushed commits", stderr="", returncode=0)

    # Push unpushed commits
    logger.info("Pushing unpushed commits...")
    return git_push(cwd=cwd, remote=remote)


def save_commit_failure_history(
    error_message: str,
    context: Dict[str, Any],
    repo_name: Optional[str] = None,
) -> None:
    """
    Save commit failure history to a JSON file and exit the application.

    This function is called when git_commit_with_retry fails. It saves the
    failure details to a history file and immediately exits the application
    to prevent uncommitted changes from being lost.

    Args:
        error_message: The error message from the failed commit
        context: Additional context information (e.g., issue number, PR number, etc.)
        repo_name: Repository name (e.g., 'owner/repo'). If provided, saves to
                  ~/.auto-coder/{repository}/ directory.
    """
    import json
    import sys
    from datetime import datetime
    from pathlib import Path

    try:
        # Determine the history directory
        if repo_name:
            # Generate safe directory name from repository name
            safe_repo_name = repo_name.replace("/", "_")
            history_dir = Path.home() / ".auto-coder" / safe_repo_name
        else:
            history_dir = Path(".auto-coder")

        # Create directory if it doesn't exist
        history_dir.mkdir(parents=True, exist_ok=True)

        # Create history file path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_file = history_dir / f"commit_failure_{timestamp}.json"

        # Prepare history data
        history_data = {
            "timestamp": datetime.now().isoformat(),
            "error_message": error_message,
            "context": context,
        }

        # Save history to file
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(history_data, f, indent=2, ensure_ascii=False)

        logger.error(f"Commit failed. History saved to {history_file}")
        logger.error(f"Error: {error_message}")
        logger.error("Application will now exit to prevent data loss.")

    except Exception as e:
        logger.error(f"Failed to save commit failure history: {e}")
        logger.error(f"Original commit error: {error_message}")

    # Exit the application immediately
    sys.exit(1)


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

    Returns:
        Action message describing the commit result
    """
    from .git_branch import git_commit_with_retry as git_commit_with_retry_local
    from .git_branch import git_pull

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
    commit_result = git_commit_with_retry_local(summary)

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
