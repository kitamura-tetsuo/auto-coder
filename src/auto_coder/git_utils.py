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
            ["git", "commit", "-m", commit_message], cwd=cwd, check_success=False
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
                    ["npx", "dprint", "fmt"], cwd=cwd, check_success=False
                )

                if fmt_result.success:
                    logger.info("Successfully ran dprint formatter")
                    # Stage the formatted files
                    add_result = cmd.run_command(
                        ["git", "add", "-u"], cwd=cwd, check_success=False
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
        cwd=cwd,
        check_success=False
    )
    if not branch_result.success:
        logger.warning(f"Failed to get current branch: {branch_result.stderr}")
        return False

    current_branch = branch_result.stdout.strip()

    # Check if there are unpushed commits
    result = cmd.run_command(
        ["git", "rev-list", f"{remote}/{current_branch}..HEAD", "--count"],
        cwd=cwd,
        check_success=False
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
    cwd: Optional[str] = None, remote: str = "origin", branch: Optional[str] = None
) -> CommandResult:
    """
    Push changes to remote repository.

    This function centralizes git push operations for consistent error handling.

    Args:
        cwd: Optional working directory for the git command
        remote: Remote name (default: 'origin')
        branch: Optional branch name. If None, pushes current branch

    Returns:
        CommandResult with the result of the push operation
    """
    cmd = CommandExecutor()

    push_cmd: List[str] = ["git", "push"]
    if branch:
        push_cmd.extend([remote, branch])

    result = cmd.run_command(push_cmd, cwd=cwd, check_success=False)

    if result.success:
        logger.info(
            f"Successfully pushed changes to {remote}"
            + (f"/{branch}" if branch else "")
        )
    else:
        logger.warning(f"Failed to push changes: {result.stderr}")

    return result


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
