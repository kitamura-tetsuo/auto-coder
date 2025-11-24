"""
Git information retrieval utilities for Auto-Coder.

This module contains read-only functions for extracting information from git repositories.
"""

import os
import re
from typing import Optional
from urllib.parse import urlparse

try:
    from git import InvalidGitRepositoryError, Repo

    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

from .logger_config import get_logger
from .utils import CommandExecutor

# Re-export CommandExecutor for test compatibility
__all__ = [
    "CommandExecutor",
    # Function names
    "check_unpushed_commits",
    "get_commit_log",
    "get_current_branch",
    "get_current_repo_name",
    "is_git_repository",
    "parse_github_repo_from_url",
]

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

        # Resolve base reference preferring remote-tracking ref to avoid ambiguity
        origin_ref = f"refs/remotes/origin/{base_branch}"
        base_check = cmd.run_command(["git", "rev-parse", "--verify", origin_ref], cwd=cwd)
        if base_check.success:
            resolved_base = origin_ref
        else:
            # Try without remote prefix
            base_check = cmd.run_command(["git", "rev-parse", "--verify", base_branch], cwd=cwd)
            if not base_check.success:
                logger.warning(f"Base branch {base_branch} not found")
                return ""
            resolved_base = base_branch

        # Get the common ancestor (merge base) between current branch and base branch
        merge_base_result = cmd.run_command(["git", "merge-base", "HEAD", resolved_base], cwd=cwd)

        if not merge_base_result.success:
            logger.warning(f"Failed to find merge base with {resolved_base}: {merge_base_result.stderr}")
            return ""

        merge_base_commit = merge_base_result.stdout.strip()

        # Get commit log since the merge base
        log_result = cmd.run_command(
            [
                "git",
                "log",
                f"{merge_base_commit}..HEAD",
                f"--max-count={max_commits}",
                "--pretty=format:%s",
            ],
            cwd=cwd,
        )

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
