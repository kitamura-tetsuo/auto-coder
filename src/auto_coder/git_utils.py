"""
Git utilities for Auto-Coder.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from auto_coder.backend_manager import get_message_backend_manager, run_message_prompt

try:
    from git import InvalidGitRepositoryError, Repo

    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

from .logger_config import get_logger
from .prompt_loader import render_prompt
from .utils import CommandExecutor, CommandResult

logger = get_logger(__name__)


def get_current_branch(cwd: Optional[str] = None) -> Optional[str]:
    """
    Get the current git branch name.

    Args:
        cwd: Optional working directory for git command

    Returns:
        Current branch name or None if failed
    """
    cmd = CommandExecutor()
    branch_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if branch_result.success:
        return branch_result.stdout.strip()
    else:
        logger.warning(f"Failed to get current branch: {branch_result.stderr}")
        return None


def extract_number_from_branch(branch_name: str) -> Optional[int]:
    """
    Extract issue or PR number from branch name.

    Supports patterns like:
    - issue-123
    - pr-456
    - feature/issue-789
    - fix/pr-101

    Args:
        branch_name: Branch name to parse

    Returns:
        Extracted number or None if no number found
    """
    if not branch_name:
        return None

    # Try to match issue-XXX or pr-XXX pattern
    patterns = [
        r"issue-(\d+)",
        r"pr-(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, branch_name, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def validate_branch_name(branch_name: str) -> None:
    """
    Validate that a branch name does not match the prohibited pr-<number> pattern.

    Args:
        branch_name: The branch name to validate

    Raises:
        ValueError: If the branch name matches the prohibited pattern
    """
    if not branch_name:
        return

    # Check if branch name matches the prohibited pr-<number> pattern
    pattern = r"^pr-\d+$"
    if re.match(pattern, branch_name, re.IGNORECASE):
        raise ValueError(
            f"Branch name '{branch_name}' matches the prohibited pattern 'pr-<number>'. "
            f"Use 'issue-<number>' naming convention instead (e.g., 'issue-{branch_name.split('-')[1] if '-' in branch_name else '123'}')."
        )


def get_commit_log(cwd: Optional[str] = None, base_branch: str = "main", max_commits: int = 50) -> str:
    """
    Get commit messages since the current branch diverged from the base branch.

    Args:
        cwd: Optional working directory for git command
        base_branch: The base branch to compare against (default: 'main')
        max_commits: Maximum number of commits to retrieve (default: 50)

    Returns:
        String containing commit log messages, one per line, or empty string if no commits
    """
    cmd = CommandExecutor()

    try:
        # Get current branch
        current_branch = get_current_branch(cwd=cwd)
        if not current_branch:
            logger.warning("Failed to get current branch")
            return ""

        # If we're already on the base branch, return empty string
        if current_branch == base_branch:
            return ""

        # Check if the base branch exists
        base_check = cmd.run_command(["git", "rev-parse", "--verify", f"origin/{base_branch}"], cwd=cwd)
        if not base_check.success:
            # Try without 'origin/' prefix
            base_check = cmd.run_command(["git", "rev-parse", "--verify", base_branch], cwd=cwd)
            if not base_check.success:
                logger.warning(f"Base branch {base_branch} not found")
                return ""

        # Get the common ancestor (merge base) between current branch and base branch
        merge_base_result = cmd.run_command(["git", "merge-base", "HEAD", base_branch], cwd=cwd)

        if not merge_base_result.success:
            logger.warning(f"Failed to find merge base with {base_branch}: {merge_base_result.stderr}")
            return ""

        merge_base_commit = merge_base_result.stdout.strip()

        # Get commit log since the merge base
        log_result = cmd.run_command(["git", "log", f"{merge_base_commit}..HEAD", f"--max-count={max_commits}", "--pretty=format:%s"], cwd=cwd)

        if not log_result.success:
            logger.warning(f"Failed to get commit log: {log_result.stderr}")
            return ""

        commit_messages = log_result.stdout.strip()
        if not commit_messages:
            return ""

        return commit_messages

    except Exception as e:
        logger.warning(f"Error getting commit log: {e}")
        return ""


def get_current_repo_name(path: Optional[str] = None) -> Optional[str]:
    """
    Get the GitHub repository name (owner/repo) from the current directory.

    Args:
        path: Optional path to check. If None, uses current directory.

    Returns:
        Repository name in format "owner/repo" or None if not found.
    """
    if not GIT_AVAILABLE:
        logger.warning("GitPython not available. Cannot auto-detect repository.")
        return None

    try:
        # Use provided path or current directory
        repo_path = path or os.getcwd()

        # Try to find git repository
        repo = Repo(repo_path, search_parent_directories=True)

        # Get remote origin URL
        if "origin" not in repo.remotes:
            logger.debug("No 'origin' remote found in repository")
            return None

        origin_url = repo.remotes.origin.url
        logger.debug(f"Found origin URL: {origin_url}")

        # Parse GitHub repository name from URL
        repo_name = parse_github_repo_from_url(origin_url)
        if repo_name:
            logger.info(f"Auto-detected repository: {repo_name}")
            return repo_name
        else:
            logger.debug(f"Could not parse GitHub repository from URL: {origin_url}")
            return None

    except InvalidGitRepositoryError:
        logger.debug(f"No git repository found in {repo_path}")
        return None
    except Exception as e:
        logger.debug(f"Error detecting repository: {e}")
        return None


def parse_github_repo_from_url(url: str) -> Optional[str]:
    """
    Parse GitHub repository name from various URL formats.

    Args:
        url: Git remote URL

    Returns:
        Repository name in format "owner/repo" or None if not a GitHub URL.
    """
    if not url:
        return None

    # Remove .git suffix if present
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    # Handle different URL formats
    patterns = [
        # HTTPS: https://github.com/owner/repo
        r"https://github\.com/([^/]+)/([^/]+)",
        # SSH: git@github.com:owner/repo
        r"git@github\.com:([^/]+)/([^/]+)",
        # SSH alternative: ssh://git@github.com/owner/repo
        r"ssh://git@github\.com/([^/]+)/([^/]+)",
    ]

    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            owner, repo = match.groups()
            return f"{owner}/{repo}"

    # Try parsing as URL
    try:
        parsed = urlparse(url)
        if parsed.hostname == "github.com" and parsed.path:
            # Remove leading slash and split path
            path_parts = parsed.path.lstrip("/").split("/")
            if len(path_parts) >= 2:
                owner, repo = path_parts[0], path_parts[1]
                return f"{owner}/{repo}"
    except Exception:
        pass

    return None


def is_git_repository(path: Optional[str] = None) -> bool:
    """
    Check if the given path (or current directory) is a Git repository.

    Args:
        path: Optional path to check. If None, uses current directory.

    Returns:
        True if it's a Git repository, False otherwise.
    """
    if not GIT_AVAILABLE:
        return False

    try:
        repo_path = path or os.getcwd()
        Repo(repo_path, search_parent_directories=True)
        return True
    except InvalidGitRepositoryError:
        return False
    except Exception:
        return False


def git_commit_with_retry(commit_message: str, cwd: Optional[str] = None, max_retries: int = 1) -> CommandResult:
    """
    Commit changes with automatic handling of formatter hook failures.

    This function centralizes git commit operations and handles well-known
    hook failures like dprint formatting errors by automatically running
    the formatter and retrying the commit once.

    Args:
        commit_message: The commit message to use
        cwd: Optional working directory for the git command
        max_retries: Maximum number of retries (default: 1)

    Returns:
        CommandResult with the result of the commit operation
    """
    cmd = CommandExecutor()

    for attempt in range(max_retries + 1):
        result = cmd.run_command(["git", "commit", "-m", commit_message], cwd=cwd)

        # If commit succeeded, return immediately
        if result.success:
            logger.info("Successfully committed changes")
            return result

        # Check if the failure is due to dprint formatting issues
        is_dprint_error = "dprint fmt" in result.stderr or "Formatting issues detected" in result.stderr or "dprint fmt" in result.stdout or "Formatting issues detected" in result.stdout

        if is_dprint_error:
            if attempt < max_retries:
                logger.info("Detected dprint formatting issues, running 'npx dprint fmt' and retrying...")

                # Run dprint formatter
                fmt_result = cmd.run_command(["npx", "dprint", "fmt"], cwd=cwd)

                if fmt_result.success:
                    logger.info("Successfully ran dprint formatter")
                    # Stage the formatted files
                    add_result = cmd.run_command(["git", "add", "-u"], cwd=cwd)
                    if add_result.success:
                        logger.info("Staged formatted files, retrying commit...")
                        continue
                    else:
                        logger.warning(f"Failed to stage formatted files: {add_result.stderr}")
                else:
                    logger.warning(f"Failed to run dprint formatter: {fmt_result.stderr}")
            else:
                logger.warning(f"Max retries ({max_retries}) reached for commit with dprint formatting")
        else:
            # Non-dprint error, exit immediately
            logger.warning(f"Failed to commit changes: {result.stderr}")
            return result

    # If we get here, all attempts failed (dprint error case)
    logger.warning(f"Failed to commit changes: {result.stderr}")
    return result


def branch_exists(branch_name: str, cwd: Optional[str] = None) -> bool:
    """
    Check if a branch with the given name exists.

    Args:
        branch_name: Name of the branch to check
        cwd: Optional working directory for the git command

    Returns:
        True if the branch exists, False otherwise
    """
    cmd = CommandExecutor()
    result = cmd.run_command(["git", "branch", "--list", branch_name], cwd=cwd)
    return result.success and bool(result.stdout.strip())


def git_checkout_branch(
    branch_name: str,
    create_new: bool = False,
    base_branch: Optional[str] = None,
    cwd: Optional[str] = None,
    publish: bool = True,
) -> CommandResult:
    """
    Switch to a git branch and verify the checkout was successful.

    This function centralizes git checkout operations and ensures that after
    switching branches, the current branch matches the expected branch.
    If creating a new branch, it will automatically push to remote and set up tracking.

    Args:
        branch_name: Name of the branch to checkout
        create_new: If True, creates a new branch with -b flag
        base_branch: If create_new is True and base_branch is specified, creates
                     the new branch from base_branch (using -B flag)
        cwd: Optional working directory for the git command
        publish: If True and create_new is True, push the new branch to remote and set up tracking

    Returns:
        CommandResult with the result of the checkout operation.
        success=True only if checkout succeeded AND current branch matches expected branch.
    """
    cmd = CommandExecutor()

    # Check if branch already exists locally (only when create_new is True)
    branch_exists = False
    if create_new:
        list_result = cmd.run_command(["git", "branch", "--list", branch_name], cwd=cwd)
        branch_exists = list_result.success and list_result.stdout.strip()

    # Validate branch name only when creating a NEW branch that doesn't exist
    # Existing branches with pr-<number> pattern can be checked out without validation
    if create_new and not branch_exists:
        try:
            validate_branch_name(branch_name)
        except ValueError as e:
            logger.error(str(e))
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(e),
                returncode=1,
            )

    # Check for uncommitted changes before checkout
    status_result = cmd.run_command(["git", "status", "--porcelain"], cwd=cwd)
    has_changes = status_result.success and status_result.stdout.strip()

    if has_changes:
        logger.info("Detected uncommitted changes before checkout, committing them first")
        # Add all changes
        add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
        if not add_result.success:
            logger.warning(f"Failed to add changes: {add_result.stderr}")

        # Commit changes
        commit_result = git_commit_with_retry(
            commit_message="WIP: Auto-commit before branch checkout",
            cwd=cwd,
            max_retries=1,
        )
        if not commit_result.success:
            logger.warning(f"Failed to commit changes before checkout: {commit_result.stderr}")

    # Build checkout command
    checkout_cmd: List[str] = ["git", "checkout"]
    if create_new:
        if base_branch:
            # Create new branch from base_branch (or reset if exists)
            checkout_cmd.append("-B")
        else:
            # Create new branch
            checkout_cmd.append("-b")
    checkout_cmd.append(branch_name)

    # Execute checkout
    result = cmd.run_command(checkout_cmd, cwd=cwd)

    if not result.success:
        # If checkout failed due to uncommitted changes, try to commit and retry
        if "would be overwritten by checkout" in result.stderr:
            logger.warning("Checkout failed due to uncommitted changes, attempting to commit and retry")

            # Add all changes
            add_result = cmd.run_command(["git", "add", "-A"], cwd=cwd)
            if not add_result.success:
                logger.error(f"Failed to add changes: {add_result.stderr}")
                return result

            # Commit changes
            commit_result = git_commit_with_retry(
                commit_message="WIP: Auto-commit before branch checkout (retry)",
                cwd=cwd,
                max_retries=1,
            )
            if not commit_result.success:
                logger.error(f"Failed to commit changes: {commit_result.stderr}")
                return result

            # Retry checkout
            result = cmd.run_command(checkout_cmd, cwd=cwd)
            if not result.success:
                logger.error(f"Failed to checkout branch '{branch_name}' after commit: {result.stderr}")
                return result
        else:
            logger.error(f"Failed to checkout branch '{branch_name}': {result.stderr}")
            return result

    # Verify that we're now on the expected branch
    verify_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)

    if not verify_result.success:
        logger.error(f"Failed to verify current branch after checkout: {verify_result.stderr}")
        return CommandResult(
            success=False,
            stdout=result.stdout,
            stderr=f"Checkout succeeded but verification failed: {verify_result.stderr}",
            returncode=1,
        )

    current_branch = verify_result.stdout.strip()
    if current_branch != branch_name:
        error_msg = f"Branch mismatch after checkout: expected '{branch_name}', but currently on '{current_branch}'"
        logger.error(error_msg)
        return CommandResult(success=False, stdout=result.stdout, stderr=error_msg, returncode=1)

    logger.info(f"Successfully checked out branch '{branch_name}'")

    # If creating a new branch, push to remote and set up tracking
    if create_new and publish:
        logger.info(f"Publishing new branch '{branch_name}' to remote...")
        push_result = cmd.run_command(["git", "push", "-u", "origin", branch_name], cwd=cwd)
        if not push_result.success:
            logger.warning(f"Failed to push new branch to remote: {push_result.stderr}")
            # Don't exit on push failure - the branch is still created locally
        else:
            logger.info(f"Successfully published branch '{branch_name}' to remote")

    return result


def check_unpushed_commits(cwd: Optional[str] = None, remote: str = "origin") -> bool:
    """
    Check if there are unpushed commits in the current branch.

    Args:
        cwd: Optional working directory for git command
        remote: Remote name (default: 'origin')

    Returns:
        True if there are unpushed commits, False otherwise
    """
    cmd = CommandExecutor()

    # Get current branch
    branch_result = cmd.run_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if not branch_result.success:
        logger.warning(f"Failed to get current branch: {branch_result.stderr}")
        return False

    current_branch = branch_result.stdout.strip()

    # Check if there are unpushed commits
    result = cmd.run_command(["git", "rev-list", f"{remote}/{current_branch}..HEAD", "--count"], cwd=cwd)

    if not result.success:
        # Remote branch might not exist yet
        logger.debug(f"Could not check unpushed commits: {result.stderr}")
        return False

    # Handle cases where git returns informational messages like "Everything up-to-date"
    output = result.stdout.strip()
    if output and not output.replace("\n", "").replace("\r", "").isdigit():
        # If output is not a number (e.g., "Everything up-to-date"), treat as no unpushed commits
        logger.debug(f"Git returned informational message: {output}")
        return False

    try:
        unpushed_count = int(output or "0")
    except ValueError:
        # If conversion fails, treat as no unpushed commits
        logger.debug(f"Could not parse unpushed commits count: {output}")
        return False

    if unpushed_count > 0:
        logger.info(f"Found {unpushed_count} unpushed commit(s) in {current_branch}")
        return True

    return False


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


# --- Helpers for enhanced push error handling ---


def _is_no_upstream_error(msg: str) -> bool:
    if not msg:
        return False
    s = msg.lower()
    return "no upstream branch" in s or "has no upstream" in s or "set the remote as upstream" in s or "no configured push destination" in s


def _is_dprint_push_error(msg: str) -> bool:
    if not msg:
        return False
    s = msg.lower()
    # Look for dprint push-hook guidance
    return ("dprint" in s) and ("output-file-paths" in s)


def _retry_with_set_upstream(
    cmd: CommandExecutor,
    remote: str,
    branch: Optional[str],
    cwd: Optional[str],
) -> CommandResult:
    # Resolve branch if not provided
    if branch is None:
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
    if commit_message:
        logger.info("Attempting to resolve push failure using LLM...")
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
    else:
        logger.warning("No LLM client available for push failure resolution")

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


def try_llm_commit_push(
    commit_message: str,
    error_message: str,
) -> bool:
    """
    Try to use LLM to resolve commit/push failures.

    Args:
        commit_message: The commit message that was attempted
        error_message: The error message from the failed commit/push

    Returns:
        True if LLM successfully resolved the issue, False otherwise
    """
    cmd = CommandExecutor()

    try:

        # Create prompt for LLM to resolve commit/push failure
        prompt = render_prompt(
            "tests.commit_and_push",
            commit_message=commit_message,
            error_message=error_message,
        )

        # Execute LLM to resolve the issue
        response = run_message_prompt(prompt)

        if not response:
            logger.error("No response from LLM for commit/push resolution")
            return False

        # Check if LLM indicated success
        if "COMMIT_PUSH_RESULT: SUCCESS" in response:
            logger.info("LLM successfully resolved commit/push failure")

            # Verify that there are no uncommitted changes
            status_result = cmd.run_command(["git", "status", "--porcelain"])
            if status_result.stdout.strip():
                logger.error("LLM claimed success but there are still uncommitted changes")
                logger.error(f"Uncommitted changes: {status_result.stdout}")
                return False

            # Verify that the push was successful by checking if there are unpushed commits
            unpushed_result = cmd.run_command(["git", "log", "@{u}..HEAD", "--oneline"])
            if unpushed_result.success and unpushed_result.stdout.strip():
                logger.error("LLM claimed success but there are still unpushed commits")
                logger.error(f"Unpushed commits: {unpushed_result.stdout}")
                return False

            return True
        elif "COMMIT_PUSH_RESULT: FAILED:" in response:
            # Extract failure reason
            failure_reason = response.split("COMMIT_PUSH_RESULT: FAILED:", 1)[1].strip()
            logger.error(f"LLM failed to resolve commit/push: {failure_reason}")
            return False
        else:
            logger.error("LLM did not provide a clear success/failure indication")
            logger.error(f"LLM response: {response[:500]}")
            return False

    except Exception as e:
        logger.error(f"Error while trying to use LLM for commit/push: {e}")
        return False


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
    global cmd  # Use the existing CommandExecutor instance
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
