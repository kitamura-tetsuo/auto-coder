"""
Attempt Manager for Auto-Coder - Handles attempt tracking and management.

This module provides functionality to track, parse, and manage attempts
made by Auto-Coder when processing issues and pull requests.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Union

from .logger_config import get_logger

logger = get_logger(__name__)

# Constants
ATTEMPT_COMMENT_PREFIX = "Auto-Coder Attempt: "
"""Prefix for comments added by Auto-Coder to track attempts."""

# Pattern to parse attempt information from comments
# Supports both legacy "Auto-Coder Attempt: <timestamp> | details"
# and new standardized "Auto-Coder Attempt: <number>[ | details]" comments.
_ATTEMPT_COMMENT_PATTERN_STR = rf"^{re.escape(ATTEMPT_COMMENT_PREFIX)}(.+)$"
ATTEMPT_COMMENT_PATTERN = re.compile(
    _ATTEMPT_COMMENT_PATTERN_STR,
    re.MULTILINE,
)

# Patterns to extract attempt numbers from comment bodies
_ATTEMPT_NUMBER_PREFIX_PATTERN = re.compile(rf"{re.escape(ATTEMPT_COMMENT_PREFIX)}\s*(\d+)(?:\s*$|\s*\|)", re.IGNORECASE)
_ATTEMPT_NUMBER_DETAILS_PATTERN = re.compile(r"attempt\s*#?\s*(\d+)", re.IGNORECASE)


@dataclass
class AttemptInfo:
    """Data structure to store attempt information.

    Attributes:
        timestamp: When the attempt was made
        details: Additional details about the attempt
        status: Current status of the attempt (e.g., "started", "completed", "failed")
        commit_sha: Optional SHA of the commit associated with this attempt
        error_message: Optional error message if the attempt failed
        metadata: Additional metadata about the attempt
    """

    timestamp: datetime
    details: str
    status: str = "started"
    commit_sha: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, str]:
        """Convert AttemptInfo to a dictionary representation.

        Returns:
            Dictionary representation of the attempt info
        """
        result = {
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
            "status": self.status,
        }
        if self.commit_sha:
            result["commit_sha"] = self.commit_sha
        if self.error_message:
            result["error_message"] = self.error_message
        if self.metadata:
            result["metadata"] = self.metadata
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "AttemptInfo":
        """Create AttemptInfo from a dictionary.

        Args:
            data: Dictionary representation of attempt info

        Returns:
            AttemptInfo instance
        """
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            details=data["details"],
            status=data.get("status", "started"),
            commit_sha=data.get("commit_sha"),
            error_message=data.get("error_message"),
            metadata=data.get("metadata", {}),
        )

    def format_comment(self) -> str:
        """Format attempt info as a comment string.

        Returns:
            Formatted comment string
        """
        return f"{ATTEMPT_COMMENT_PREFIX}{self.timestamp.isoformat()} | {self.details}"


def _coerce_timestamp(raw: Union[str, datetime, None]) -> Optional[datetime]:
    """Convert raw timestamp input to datetime if possible."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        raw_value = str(raw)
        if raw_value.endswith("Z"):
            raw_value = raw_value.replace("Z", "+00:00")
        return datetime.fromisoformat(raw_value)
    except Exception:
        return None


def extract_attempt_number(comment_body: str) -> Optional[int]:
    """Extract the attempt number from a comment body.

    Supports both standardized "Auto-Coder Attempt: <N>" and legacy
    "Attempt #N" suffixes.
    """
    if not comment_body:
        return None

    prefix_match = _ATTEMPT_NUMBER_PREFIX_PATTERN.search(comment_body)
    if prefix_match:
        try:
            return int(prefix_match.group(1))
        except ValueError:
            pass

    details_match = _ATTEMPT_NUMBER_DETAILS_PATTERN.search(comment_body)
    if details_match:
        try:
            return int(details_match.group(1))
        except ValueError:
            return None
    return None


def parse_attempt_from_comment(comment_body: str, created_at: Optional[Union[str, datetime]] = None) -> Optional[AttemptInfo]:
    """Parse attempt information from a GitHub comment.

    Args:
        comment_body: The body of the GitHub comment
        created_at: Optional timestamp to use when the comment body lacks one

    Returns:
        AttemptInfo if parsing succeeds, None otherwise
    """
    match = ATTEMPT_COMMENT_PATTERN.search(comment_body)
    if not match:
        logger.debug(f"Comment does not match attempt pattern: {comment_body[:50]}...")
        return None

    try:
        content = match.group(1).strip()
        timestamp: Optional[datetime] = None
        details = content

        if " | " in content:
            timestamp_str, details = content.split(" | ", 1)
            timestamp = _coerce_timestamp(timestamp_str.strip())

        if timestamp is None:
            timestamp = _coerce_timestamp(created_at) or datetime.now()

        return AttemptInfo(
            timestamp=timestamp,
            details=details,
        )
    except Exception as e:
        logger.error(f"Failed to parse attempt from comment: {e}")
        return None


def extract_attempts_from_comments(comments: List[Dict[str, str]]) -> List[AttemptInfo]:
    """Extract all attempts from a list of GitHub comments.

    Args:
        comments: List of GitHub comment dictionaries

    Returns:
        List of AttemptInfo objects found in comments
    """
    attempts: List[AttemptInfo] = []
    for comment in comments:
        body = comment.get("body", "")
        attempt = parse_attempt_from_comment(body, created_at=comment.get("created_at"))
        if attempt:
            attempts.append(attempt)
            logger.debug(f"Found attempt in comment: {attempt.details}")

    # Sort attempts by timestamp (oldest first)
    attempts.sort(key=lambda a: a.timestamp)
    return attempts


def format_attempt_comment(attempt: int, details: Optional[str] = None) -> str:
    """Format a standardized attempt comment.

    The standardized format is:
        "Auto-Coder Attempt: <attempt>[ | <details>]"

    Args:
        attempt: Attempt number (>=1 for retries)
        details: Optional additional context to append

    Returns:
        Formatted comment string
    """
    detail_suffix = f" | {details}" if details else ""
    return f"{ATTEMPT_COMMENT_PREFIX}{attempt}{detail_suffix}"


def get_latest_attempt(attempts: List[AttemptInfo]) -> Optional[AttemptInfo]:
    """Get the most recent attempt from a list.

    Args:
        attempts: List of AttemptInfo objects

    Returns:
        The most recent attempt, or None if list is empty
    """
    if not attempts:
        return None
    return max(attempts, key=lambda a: a.timestamp)


def filter_attempts_by_status(attempts: List[AttemptInfo], status: str) -> List[AttemptInfo]:
    """Filter attempts by status.

    Args:
        attempts: List of AttemptInfo objects
        status: Status to filter by

    Returns:
        List of attempts matching the status
    """
    return [a for a in attempts if a.status == status]


def group_attempts_by_status(attempts: List[AttemptInfo]) -> Dict[str, List[AttemptInfo]]:
    """Group attempts by their status.

    Args:
        attempts: List of AttemptInfo objects

    Returns:
        Dictionary mapping status to list of attempts
    """
    grouped: Dict[str, List[AttemptInfo]] = {}
    for attempt in attempts:
        grouped.setdefault(attempt.status, []).append(attempt)
    return grouped


def get_current_attempt(repo_name: str, issue_number: int) -> int:
    """Get the current attempt number for an issue.

    Fetches issue comments, parses them looking for ATTEMPT_COMMENT_PREFIX,
    and returns the highest attempt number found (default 0).

    Args:
        repo_name: Repository name in format 'owner/repo'
        issue_number: Issue number to get attempt count for

    Returns:
        The current attempt number (0 if no attempts found)
    """
    from .github_client import GitHubClient

    try:
        client = GitHubClient.get_instance()
        repo = client.get_repository(repo_name)
        issue = repo.get_issue(issue_number)

        # Get all comments for the issue
        comments = issue.get_comments()

        attempt_numbers: List[int] = []
        comments_data = []
        for comment in comments:
            body = getattr(comment, "body", "")
            attempt_number = extract_attempt_number(body)
            if attempt_number is not None:
                attempt_numbers.append(attempt_number)
            comments_data.append(
                {
                    "body": body,
                    "created_at": getattr(comment, "created_at", None),
                }
            )

        # Fallback to counting attempt comments when numbers are unavailable
        if not attempt_numbers:
            attempts = extract_attempts_from_comments(comments_data)
            attempt_numbers = [idx + 1 for idx, _ in enumerate(attempts)]

        current_attempt = max(attempt_numbers) if attempt_numbers else 0

        logger.info(f"Found {current_attempt} attempt(s) for issue #{issue_number}")
        return current_attempt

    except Exception as e:
        logger.error(f"Failed to get current attempt for issue #{issue_number}: {e}")
        return 0


def increment_attempt(repo_name: str, issue_number: int, attempt_number: Optional[int] = None) -> int:
    """Increment the attempt count for an issue.

    Gets the current attempt count, increments by 1, and posts a new comment
    with the new attempt number. If the issue has sub-issues, propagates the
    attempt increment to all sub-issues and reopens any closed sub-issues.

    Args:
        repo_name: Repository name in format 'owner/repo'
        issue_number: Issue number to increment attempt for
        attempt_number: Optional specific attempt number to use instead of auto-incrementing

    Returns:
        The new attempt number after incrementing
    """
    from .github_client import GitHubClient

    try:
        # Get current attempt
        current_attempt = get_current_attempt(repo_name, issue_number)

        # Use provided attempt_number if available, otherwise increment
        if attempt_number is not None:
            new_attempt = attempt_number
        else:
            new_attempt = current_attempt + 1

        # Create comment with new attempt number
        comment_body = format_attempt_comment(new_attempt)

        # Add comment to the issue
        client = GitHubClient.get_instance()
        client.add_comment_to_issue(repo_name, issue_number, comment_body)

        logger.info(f"Incremented attempt for issue #{issue_number} from {current_attempt} to {new_attempt}")

        # Propagate attempt increment to all sub-issues
        sub_issues = client.get_all_sub_issues(repo_name, issue_number)
        if sub_issues:
            logger.info(f"Propagating attempt increment to {len(sub_issues)} sub-issue(s): {sub_issues}")

            for sub_issue_number in sub_issues:
                try:
                    # Get the state of the sub-issue to check if it's closed
                    repo = client.get_repository(repo_name)
                    sub_issue = repo.get_issue(sub_issue_number)

                    # If sub-issue is closed, reopen it
                    if sub_issue.state == "closed":
                        logger.info(f"Reopening closed sub-issue #{sub_issue_number}")
                        reopen_comment = f"Auto-Coder: Reopened due to attempt increment on parent issue #{issue_number}"
                        client.reopen_issue(repo_name, sub_issue_number, reopen_comment)

                    # Increment attempt for the sub-issue
                    increment_attempt(repo_name, sub_issue_number, attempt_number=new_attempt)

                except Exception as e:
                    logger.error(f"Failed to propagate attempt to sub-issue #{sub_issue_number}: {e}")
                    # Continue with other sub-issues even if one fails
                    continue

        return new_attempt

    except Exception as e:
        logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
        raise
