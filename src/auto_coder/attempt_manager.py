"""
Attempt Manager for Auto-Coder - Handles attempt tracking and management.

This module provides functionality to track, parse, and manage attempts
made by Auto-Coder when processing issues and pull requests.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .logger_config import get_logger

logger = get_logger(__name__)

# Constants
ATTEMPT_COMMENT_PREFIX = "Auto-Coder Attempt: "
"""Prefix for comments added by Auto-Coder to track attempts."""

# Pattern to parse attempt information from comments
# Format: "Auto-Coder Attempt: [timestamp] | [details]"
_ATTEMPT_COMMENT_PATTERN_STR = rf"^{re.escape(ATTEMPT_COMMENT_PREFIX)}(.+)$"
ATTEMPT_COMMENT_PATTERN = re.compile(
    _ATTEMPT_COMMENT_PATTERN_STR,
    re.MULTILINE,
)


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


def parse_attempt_from_comment(comment_body: str) -> Optional[AttemptInfo]:
    """Parse attempt information from a GitHub comment.

    Args:
        comment_body: The body of the GitHub comment

    Returns:
        AttemptInfo if parsing succeeds, None otherwise
    """
    match = ATTEMPT_COMMENT_PATTERN.search(comment_body)
    if not match:
        logger.debug(f"Comment does not match attempt pattern: {comment_body[:50]}...")
        return None

    try:
        # Extract timestamp and details from the match
        # Format: "timestamp | details"
        content = match.group(1)
        if " | " in content:
            timestamp_str, details = content.split(" | ", 1)
            timestamp = datetime.fromisoformat(timestamp_str.strip())
        else:
            # Fallback: use current time if format doesn't match
            logger.warning(f"Attempt comment format unexpected: {content}")
            timestamp = datetime.now()
            details = content

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
    attempts = []
    for comment in comments:
        body = comment.get("body", "")
        attempt = parse_attempt_from_comment(body)
        if attempt:
            attempts.append(attempt)
            logger.debug(f"Found attempt in comment: {attempt.details}")

    # Sort attempts by timestamp (oldest first)
    attempts.sort(key=lambda a: a.timestamp)
    return attempts


def format_attempt_comment(timestamp: datetime, details: str, status: str = "started") -> str:
    """Format a comment for an attempt.

    Args:
        timestamp: When the attempt was made
        details: Details about the attempt
        status: Status of the attempt

    Returns:
        Formatted comment string
    """
    attempt = AttemptInfo(timestamp=timestamp, details=details, status=status)
    return attempt.format_comment()


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

        # Convert comments to list of dicts for parsing
        comments_data = []
        for comment in comments:
            comments_data.append(
                {
                    "body": comment.body,
                    "created_at": comment.created_at,
                }
            )

        # Extract attempts from comments
        attempts = extract_attempts_from_comments(comments_data)

        # Get the current attempt number by counting attempts
        # Each attempt is sequential, so the count is the attempt number
        current_attempt = len(attempts)

        logger.info(f"Found {current_attempt} attempt(s) for issue #{issue_number}")
        return current_attempt

    except Exception as e:
        logger.error(f"Failed to get current attempt for issue #{issue_number}: {e}")
        return 0


def increment_attempt(repo_name: str, issue_number: int) -> int:
    """Increment the attempt count for an issue.

    Gets the current attempt count, increments by 1, and posts a new comment
    with the new attempt number.

    Args:
        repo_name: Repository name in format 'owner/repo'
        issue_number: Issue number to increment attempt for

    Returns:
        The new attempt number after incrementing
    """
    from .github_client import GitHubClient

    try:
        # Get current attempt
        current_attempt = get_current_attempt(repo_name, issue_number)
        new_attempt = current_attempt + 1

        # Create comment with new attempt number
        timestamp = datetime.now()
        details = f"Attempt #{new_attempt} - Processing issue"
        comment_body = format_attempt_comment(timestamp, details, status="started")

        # Add comment to the issue
        client = GitHubClient.get_instance()
        client.add_comment_to_issue(repo_name, issue_number, comment_body)

        logger.info(f"Incremented attempt for issue #{issue_number} from {current_attempt} to {new_attempt}")
        return new_attempt

    except Exception as e:
        logger.error(f"Failed to increment attempt for issue #{issue_number}: {e}")
        raise
