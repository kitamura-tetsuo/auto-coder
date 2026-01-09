"""
Git information retrieval utilities for Auto-Coder.

This module contains read-only functions for extracting information from git repositories.
"""

import os
import re
from typing import Optional
from urllib.parse import urlparse

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
    "get_current_commit_sha",
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
    try:
        # Use provided path or current directory
        repo_path = path or os.getcwd()
        cmd = CommandExecutor()

        # Get remote origin URL
        # git remote get-url origin
        result = cmd.run_command(["git", "remote", "get-url", "origin"], cwd=repo_path, stream_output=False)

        if not result.success:
            logger.debug(f"No 'origin' remote found or not a git repository in {repo_path}: {result.stderr}")
            return None

        origin_url = result.stdout.strip()
        logger.debug(f"Found origin URL: {origin_url}")

        # Parse GitHub repository name from URL
        repo_name = parse_github_repo_from_url(origin_url)
        if repo_name:
            logger.info(f"Auto-detected repository: {repo_name}")
            return repo_name
        else:
            logger.debug(f"Could not parse GitHub repository from URL: {origin_url}")
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
    try:
        repo_path = path or os.getcwd()
        cmd = CommandExecutor()
        # Check if we are inside a git work tree
        result = cmd.run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_path, stream_output=False)
        return result.success and result.stdout.strip() == "true"
    except Exception:
        return False


def get_commit_log(cwd: Optional[str] = None, base_branch: str = "main", max_commits: int = 50, current_branch: Optional[str] = None) -> str:
    """
    Get commit messages since the current branch diverged from the base branch.

    Args:
        cwd: Optional working directory for git command
        base_branch: The base branch to compare against (default: 'main')
        max_commits: Maximum number of commits to retrieve (default: 50)
        current_branch: Optional current branch name. If provided, skips calling get_current_branch.

    Returns:
        String containing commit log messages, one per line, or empty string if no commits
    """
    cmd = CommandExecutor()

    try:
        # Get current branch if not provided
        if not current_branch:
            current_branch = get_current_branch(cwd=cwd)

        if not current_branch:
            logger.warning("Failed to get current branch")
            return ""

        # If we're already on the base branch, return empty string
        if current_branch == base_branch:
            return ""

        # Try getting log using remote-tracking branch first (optimistic approach)
        origin_ref = f"refs/remotes/origin/{base_branch}"
        log_result = cmd.run_command(
            [
                "git",
                "log",
                f"{origin_ref}..HEAD",
                f"--max-count={max_commits}",
                "--pretty=format:%s",
            ],
            cwd=cwd,
            stream_output=False,
        )

        if not log_result.success:
            # Fallback to local base branch
            log_result = cmd.run_command(
                [
                    "git",
                    "log",
                    f"{base_branch}..HEAD",
                    f"--max-count={max_commits}",
                    "--pretty=format:%s",
                ],
                cwd=cwd,
                stream_output=False,
            )

            if not log_result.success:
                logger.warning(f"Failed to get commit log (base: {base_branch}): {log_result.stderr}")
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


def get_current_commit_sha(cwd: Optional[str] = None) -> Optional[str]:
    """
    Get the current commit SHA (HEAD).

    Args:
        cwd: Optional working directory for git command

    Returns:
        Full SHA of the current commit or None if failed
    """
    cmd = CommandExecutor()
    result = cmd.run_command(["git", "rev-parse", "HEAD"], cwd=cwd)
    if result.success:
        return result.stdout.strip()
    return None
