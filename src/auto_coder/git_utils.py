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

try:
    from git import InvalidGitRepositoryError, Repo

    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

from .logger_config import get_logger
from .utils import CommandExecutor, CommandResult

logger = get_logger(__name__)


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


def git_commit_with_retry(
    commit_message: str, cwd: Optional[str] = None, max_retries: int = 1
) -> CommandResult:
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
        result = cmd.run_command(
            ["git", "commit", "-m", commit_message], cwd=cwd
        )

        # If commit succeeded, return immediately
        if result.success:
            logger.info("Successfully committed changes")
            return result

        # Check if the failure is due to dprint formatting issues
        is_dprint_error = (
            "dprint fmt" in result.stderr
            or "Formatting issues detected" in result.stderr
            or "dprint fmt" in result.stdout
            or "Formatting issues detected" in result.stdout
        )

        if is_dprint_error:
            if attempt < max_retries:
                logger.info(
                    "Detected dprint formatting issues, running 'npx dprint fmt' and retrying..."
                )

                # Run dprint formatter
                fmt_result = cmd.run_command(
                    ["npx", "dprint", "fmt"], cwd=cwd
                )

                if fmt_result.success:
                    logger.info("Successfully ran dprint formatter")
                    # Stage the formatted files
                    add_result = cmd.run_command(
                        ["git", "add", "-u"], cwd=cwd
                    )
                    if add_result.success:
                        logger.info("Staged formatted files, retrying commit...")
                        continue
                    else:
                        logger.warning(
                            f"Failed to stage formatted files: {add_result.stderr}"
                        )
                else:
                    logger.warning(
                        f"Failed to run dprint formatter: {fmt_result.stderr}"
                    )
            else:
                logger.warning(
                    f"Max retries ({max_retries}) reached for commit with dprint formatting"
                )
        else:
            # Non-dprint error, exit immediately
            logger.warning(f"Failed to commit changes: {result.stderr}")
            return result

    # If we get here, all attempts failed (dprint error case)
    logger.warning(f"Failed to commit changes: {result.stderr}")
    return result


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
            max_retries=1
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
                max_retries=1
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
    verify_result = cmd.run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd
    )

    if not verify_result.success:
        logger.error(f"Failed to verify current branch after checkout: {verify_result.stderr}")
        return CommandResult(
            success=False,
            stdout=result.stdout,
            stderr=f"Checkout succeeded but verification failed: {verify_result.stderr}",
            returncode=1
        )

    current_branch = verify_result.stdout.strip()
    if current_branch != branch_name:
        error_msg = f"Branch mismatch after checkout: expected '{branch_name}', but currently on '{current_branch}'"
        logger.error(error_msg)
        return CommandResult(
            success=False,
            stdout=result.stdout,
            stderr=error_msg,
            returncode=1
        )

    logger.info(f"Successfully checked out branch '{branch_name}'")

    # If creating a new branch, push to remote and set up tracking
    if create_new and publish:
        logger.info(f"Publishing new branch '{branch_name}' to remote...")
        push_result = cmd.run_command(
            ["git", "push", "-u", "origin", branch_name],
            cwd=cwd
        )
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
    branch_result = cmd.run_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd
    )
    if not branch_result.success:
        logger.warning(f"Failed to get current branch: {branch_result.stderr}")
        return False

    current_branch = branch_result.stdout.strip()

    # Check if there are unpushed commits
    result = cmd.run_command(
        ["git", "rev-list", f"{remote}/{current_branch}..HEAD", "--count"],
        cwd=cwd
    )

    if not result.success:
        # Remote branch might not exist yet
        logger.debug(f"Could not check unpushed commits: {result.stderr}")
        return False

    unpushed_count = int(result.stdout.strip() or "0")
    if unpushed_count > 0:
        logger.info(f"Found {unpushed_count} unpushed commit(s) in {current_branch}")
        return True

    return False


def git_push(
    cwd: Optional[str] = None,
    remote: str = "origin",
    branch: Optional[str] = None,
    commit_message: Optional[str] = None,
) -> CommandResult:
    """
    Push changes to remote repository.

    This function centralizes git push operations for consistent error handling.
    Automatically handles upstream branch setup if needed.
    Also handles dprint formatting errors by running formatter and retrying push.

    Args:
        cwd: Optional working directory for the git command
        remote: Remote name (default: 'origin')
        branch: Optional branch name. If None, pushes current branch
        commit_message: Optional commit message for re-committing after dprint formatting

    Returns:
        CommandResult with the result of the push operation
    """
    cmd = CommandExecutor()

    # Determine which branch to push
    # If branch is specified, use it directly
    # If not, get the current branch
    target_branch = branch
    if not target_branch:
        branch_result = cmd.run_command(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd
        )
        if branch_result.returncode == 0:
            target_branch = branch_result.stdout.strip()
        else:
            logger.warning(f"Failed to get current branch: {branch_result.stderr}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Failed to get current branch: {branch_result.stderr}",
                returncode=branch_result.returncode
            )

    # Build push command
    push_cmd: List[str] = ["git", "push"]
    if branch:
        push_cmd.extend([remote, branch])

    result = cmd.run_command(push_cmd, cwd=cwd)

    # Check if push failed due to missing upstream
    if result.returncode != 0:
        is_upstream_error = (
            "has no upstream branch" in result.stderr
            or "--set-upstream" in result.stderr
        )

        if is_upstream_error:
            logger.info(
                f"Branch {target_branch} has no upstream, setting upstream to {remote}/{target_branch}"
            )
            # Retry with --set-upstream
            push_cmd_with_upstream = ["git", "push", "--set-upstream", remote, target_branch]
            result = cmd.run_command(push_cmd_with_upstream, cwd=cwd)

    # Check if push failed due to dprint formatting issues
    if result.returncode != 0:
        is_dprint_error = (
            "dprint output-file-paths" in result.stderr
            or "You may want to try using `dprint output-file-paths`" in result.stderr
        )

        if is_dprint_error:
            logger.info(
                "Detected dprint formatting issues in push hook, running 'npx dprint fmt' and retrying..."
            )

            # Run dprint formatter
            fmt_result = cmd.run_command(
                ["npx", "dprint", "fmt"], cwd=cwd
            )

            if fmt_result.success:
                logger.info("Successfully ran dprint formatter")
                # Stage all changes including formatted files
                add_result = cmd.run_command(
                    ["git", "add", "-A"], cwd=cwd
                )
                if add_result.success:
                    logger.info("Staged formatted files")

                    # Re-commit with the same message if provided
                    if commit_message:
                        logger.info("Re-committing changes after dprint formatting...")
                        commit_result = cmd.run_command(
                            ["git", "commit", "--amend", "--no-edit"],
                            cwd=cwd
                        )
                        if not commit_result.success:
                            logger.warning(
                                f"Failed to amend commit: {commit_result.stderr}"
                            )
                            # Try regular commit if amend fails
                            commit_result = cmd.run_command(
                                ["git", "commit", "-m", commit_message],
                                cwd=cwd
                            )
                            if not commit_result.success:
                                logger.warning(
                                    f"Failed to commit formatted changes: {commit_result.stderr}"
                                )
                                return CommandResult(
                                    success=False,
                                    stdout=commit_result.stdout,
                                    stderr=commit_result.stderr,
                                    returncode=commit_result.returncode,
                                )

                    logger.info("Retrying push...")
                    # Retry push
                    result = cmd.run_command(push_cmd, cwd=cwd)

                    # If still failing due to upstream, try with --set-upstream
                    if result.returncode != 0:
                        is_upstream_error = (
                            "has no upstream branch" in result.stderr
                            or "--set-upstream" in result.stderr
                        )
                        if is_upstream_error:
                            logger.info(
                                f"Branch {target_branch} has no upstream, setting upstream to {remote}/{target_branch}"
                            )
                            push_cmd_with_upstream = ["git", "push", "--set-upstream", remote, target_branch]
                            result = cmd.run_command(push_cmd_with_upstream, cwd=cwd)
                else:
                    logger.warning(
                        f"Failed to stage formatted files: {add_result.stderr}"
                    )
            else:
                logger.warning(
                    f"Failed to run dprint formatter: {fmt_result.stderr}"
                )

    if result.returncode == 0:
        logger.info(
            f"Successfully pushed changes to {remote}/{target_branch}"
        )
        return CommandResult(
            success=True,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=0
        )
    else:
        logger.warning(f"Failed to push changes: {result.stderr}")
        return CommandResult(
            success=False,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode
        )


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
        return CommandResult(
            success=True,
            stdout="No unpushed commits",
            stderr="",
            returncode=0
        )

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
            # リポジトリ名から安全なディレクトリ名を生成
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
