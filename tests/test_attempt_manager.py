"""
Tests for attempt_manager.py - Unit and integration tests for the attempt mechanism.

This module tests the functionality for tracking, parsing, and managing attempts
made by Auto-Coder when processing issues and pull requests.
"""

import re
from datetime import datetime
from unittest.mock import ANY, Mock, patch

import pytest

from auto_coder.attempt_manager import (
    ATTEMPT_COMMENT_PREFIX,
    AttemptInfo,
    extract_attempts_from_comments,
    filter_attempts_by_status,
    format_attempt_comment,
    get_current_attempt,
    get_latest_attempt,
    group_attempts_by_status,
    increment_attempt,
    parse_attempt_from_comment,
)
from auto_coder.git_branch import extract_attempt_from_branch
from auto_coder.issue_processor import generate_work_branch_name


class TestAttemptInfo:
    """Test the AttemptInfo dataclass."""

    def test_attempt_info_creation(self):
        """Test creating AttemptInfo with basic fields."""
        timestamp = datetime.now()
        details = "Test details"
        attempt = AttemptInfo(timestamp=timestamp, details=details)

        assert attempt.timestamp == timestamp
        assert attempt.details == details
        assert attempt.status == "started"
        assert attempt.commit_sha is None
        assert attempt.error_message is None
        assert attempt.metadata == {}

    def test_attempt_info_creation_with_optional_fields(self):
        """Test creating AttemptInfo with all fields."""
        timestamp = datetime.now()
        details = "Test details"
        status = "completed"
        commit_sha = "abc123"
        error_message = "Some error"
        metadata = {"key": "value"}

        attempt = AttemptInfo(
            timestamp=timestamp,
            details=details,
            status=status,
            commit_sha=commit_sha,
            error_message=error_message,
            metadata=metadata,
        )

        assert attempt.timestamp == timestamp
        assert attempt.details == details
        assert attempt.status == status
        assert attempt.commit_sha == commit_sha
        assert attempt.error_message == error_message
        assert attempt.metadata == metadata

    def test_to_dict(self):
        """Test converting AttemptInfo to dictionary."""
        timestamp = datetime(2024, 1, 15, 10, 30, 0)
        attempt = AttemptInfo(
            timestamp=timestamp,
            details="Test details",
            status="completed",
            commit_sha="abc123",
            error_message="Error message",
            metadata={"key": "value"},
        )

        result = attempt.to_dict()

        assert result == {
            "timestamp": "2024-01-15T10:30:00",
            "details": "Test details",
            "status": "completed",
            "commit_sha": "abc123",
            "error_message": "Error message",
            "metadata": {"key": "value"},
        }

    def test_to_dict_minimal(self):
        """Test converting AttemptInfo to dictionary with minimal fields."""
        timestamp = datetime(2024, 1, 15, 10, 30, 0)
        attempt = AttemptInfo(timestamp=timestamp, details="Test details")

        result = attempt.to_dict()

        assert result == {
            "timestamp": "2024-01-15T10:30:00",
            "details": "Test details",
            "status": "started",
        }

    def test_from_dict(self):
        """Test creating AttemptInfo from dictionary."""
        data = {
            "timestamp": "2024-01-15T10:30:00",
            "details": "Test details",
            "status": "completed",
            "commit_sha": "abc123",
            "error_message": "Error message",
            "metadata": {"key": "value"},
        }

        attempt = AttemptInfo.from_dict(data)

        assert attempt.timestamp == datetime(2024, 1, 15, 10, 30, 0)
        assert attempt.details == "Test details"
        assert attempt.status == "completed"
        assert attempt.commit_sha == "abc123"
        assert attempt.error_message == "Error message"
        assert attempt.metadata == {"key": "value"}

    def test_from_dict_minimal(self):
        """Test creating AttemptInfo from dictionary with minimal fields."""
        data = {
            "timestamp": "2024-01-15T10:30:00",
            "details": "Test details",
        }

        attempt = AttemptInfo.from_dict(data)

        assert attempt.timestamp == datetime(2024, 1, 15, 10, 30, 0)
        assert attempt.details == "Test details"
        assert attempt.status == "started"  # Default value
        assert attempt.commit_sha is None
        assert attempt.error_message is None

    def test_round_trip_to_dict_from_dict(self):
        """Test that to_dict and from_dict are inverse operations."""
        timestamp = datetime(2024, 1, 15, 10, 30, 0)
        original = AttemptInfo(
            timestamp=timestamp,
            details="Test details",
            status="completed",
            commit_sha="abc123",
            error_message="Error message",
            metadata={"key": "value"},
        )

        # Round trip
        dict_repr = original.to_dict()
        reconstructed = AttemptInfo.from_dict(dict_repr)

        assert reconstructed.timestamp == original.timestamp
        assert reconstructed.details == original.details
        assert reconstructed.status == original.status
        assert reconstructed.commit_sha == original.commit_sha
        assert reconstructed.error_message == original.error_message
        assert reconstructed.metadata == original.metadata

    def test_format_comment(self):
        """Test formatting attempt info as comment string."""
        timestamp = datetime(2024, 1, 15, 10, 30, 0)
        attempt = AttemptInfo(
            timestamp=timestamp,
            details="Test details",
            status="started",
        )

        comment = attempt.format_comment()

        assert comment == f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:30:00 | Test details"
        assert ATTEMPT_COMMENT_PREFIX in comment


class TestParseAttemptFromComment:
    """Test parsing attempt information from GitHub comments."""

    def test_parse_valid_attempt_comment(self):
        """Test parsing a valid attempt comment."""
        comment_body = f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:30:00 | Attempt #1 - Processing issue"

        attempt = parse_attempt_from_comment(comment_body)

        assert attempt is not None
        assert attempt.timestamp == datetime(2024, 1, 15, 10, 30, 0)
        assert attempt.details == "Attempt #1 - Processing issue"

    def test_parse_attempt_comment_with_pipe_in_details(self):
        """Test parsing attempt comment where details contain pipe character."""
        comment_body = f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:30:00 | Attempt #1 | Testing | with pipes"

        attempt = parse_attempt_from_comment(comment_body)

        assert attempt is not None
        assert attempt.timestamp == datetime(2024, 1, 15, 10, 30, 0)
        assert attempt.details == "Attempt #1 | Testing | with pipes"

    def test_parse_comment_without_prefix(self):
        """Test that comment without prefix returns None."""
        comment_body = "This is a regular comment"

        attempt = parse_attempt_from_comment(comment_body)

        assert attempt is None

    def test_parse_empty_comment(self):
        """Test parsing an empty comment."""
        comment_body = ""

        attempt = parse_attempt_from_comment(comment_body)

        assert attempt is None

    def test_parse_comment_with_only_prefix(self):
        """Test parsing comment with only the prefix returns None (no match)."""
        comment_body = ATTEMPT_COMMENT_PREFIX

        attempt = parse_attempt_from_comment(comment_body)

        # The regex requires at least one character after the prefix, so no match
        assert attempt is None

    def test_parse_invalid_timestamp_format(self):
        """Test parsing comment with invalid timestamp format."""
        comment_body = f"{ATTEMPT_COMMENT_PREFIX}invalid-timestamp | Attempt details"

        attempt = parse_attempt_from_comment(comment_body)

        # Invalid timestamps should not block attempt detection
        assert attempt is not None
        assert attempt.details.endswith("Attempt details")

    def test_parse_malformed_comment(self):
        """Test parsing malformed comment still returns attempt with fallback."""
        comment_body = f"{ATTEMPT_COMMENT_PREFIX}malformed-comment-without-pipe"

        attempt = parse_attempt_from_comment(comment_body)

        assert attempt is not None
        assert attempt.details == "malformed-comment-without-pipe"

    def test_parse_comment_multiline(self):
        """Test parsing comment with newlines."""
        comment_body = f"""{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:30:00 | Attempt #1
Additional details on multiple lines"""

        attempt = parse_attempt_from_comment(comment_body)

        assert attempt is not None
        assert attempt.timestamp == datetime(2024, 1, 15, 10, 30, 0)
        # The regex has MULTILINE flag, should handle this correctly
        assert "Attempt #1" in attempt.details


class TestExtractAttemptsFromComments:
    """Test extracting attempts from a list of GitHub comments."""

    def test_extract_single_attempt(self):
        """Test extracting a single attempt from comments."""
        comments = [
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:30:00 | Attempt #1 - Processing", "created_at": "2024-01-15T10:30:00"},
            {"body": "Regular comment", "created_at": "2024-01-15T11:00:00"},
        ]

        attempts = extract_attempts_from_comments(comments)

        assert len(attempts) == 1
        assert attempts[0].details == "Attempt #1 - Processing"
        assert attempts[0].timestamp == datetime(2024, 1, 15, 10, 30, 0)

    def test_extract_multiple_attempts(self):
        """Test extracting multiple attempts from comments."""
        comments = [
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:00:00 | Attempt #1", "created_at": "2024-01-15T10:00:00"},
            {"body": "Regular comment", "created_at": "2024-01-15T10:30:00"},
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T11:00:00 | Attempt #2", "created_at": "2024-01-15T11:00:00"},
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T12:00:00 | Attempt #3", "created_at": "2024-01-15T12:00:00"},
        ]

        attempts = extract_attempts_from_comments(comments)

        assert len(attempts) == 3
        # Should be sorted by timestamp (oldest first)
        assert attempts[0].timestamp == datetime(2024, 1, 15, 10, 0, 0)
        assert attempts[1].timestamp == datetime(2024, 1, 15, 11, 0, 0)
        assert attempts[2].timestamp == datetime(2024, 1, 15, 12, 0, 0)

    def test_extract_no_attempts(self):
        """Test extracting attempts when none exist."""
        comments = [
            {"body": "Regular comment 1", "created_at": "2024-01-15T10:00:00"},
            {"body": "Regular comment 2", "created_at": "2024-01-15T11:00:00"},
        ]

        attempts = extract_attempts_from_comments(comments)

        assert len(attempts) == 0

    def test_extract_attempts_out_of_order(self):
        """Test extracting attempts sorted by timestamp even if comments are out of order."""
        comments = [
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T12:00:00 | Attempt #3", "created_at": "2024-01-15T12:00:00"},
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:00:00 | Attempt #1", "created_at": "2024-01-15T10:00:00"},
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T11:00:00 | Attempt #2", "created_at": "2024-01-15T11:00:00"},
        ]

        attempts = extract_attempts_from_comments(comments)

        assert len(attempts) == 3
        # Should be sorted by timestamp
        assert attempts[0].details == "Attempt #1"
        assert attempts[1].details == "Attempt #2"
        assert attempts[2].details == "Attempt #3"

    def test_extract_empty_comment_list(self):
        """Test extracting from empty comment list."""
        comments = []

        attempts = extract_attempts_from_comments(comments)

        assert len(attempts) == 0

    def test_extract_with_missing_body(self):
        """Test extracting attempts when comments have missing 'body' key."""
        comments = [
            {"created_at": "2024-01-15T10:00:00"},  # Missing 'body'
            {"body": f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T11:00:00 | Attempt #1", "created_at": "2024-01-15T11:00:00"},
        ]

        attempts = extract_attempts_from_comments(comments)

        assert len(attempts) == 1


class TestFormatAttemptComment:
    """Test formatting attempt comments."""

    def test_format_basic_comment(self):
        """Test formatting a basic attempt comment."""
        comment = format_attempt_comment(1)

        expected = f"{ATTEMPT_COMMENT_PREFIX}1"
        assert comment == expected

    def test_format_comment_with_different_status(self):
        """Test formatting comment with different status."""
        comment = format_attempt_comment(2, details="retry requested")

        expected = f"{ATTEMPT_COMMENT_PREFIX}2 | retry requested"
        assert comment == expected

    def test_format_comment_with_special_characters(self):
        """Test formatting comment with special characters in details."""
        details = "Testing | pipes & special chars: @#$%"

        comment = format_attempt_comment(3, details=details)

        expected = f"{ATTEMPT_COMMENT_PREFIX}3 | Testing | pipes & special chars: @#$%"
        assert comment == expected


class TestGetLatestAttempt:
    """Test getting the latest attempt from a list."""

    def test_get_latest_attempt(self):
        """Test getting the most recent attempt."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Attempt #1"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 11, 0, 0), details="Attempt #2"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 12, 0, 0), details="Attempt #3"),
        ]

        latest = get_latest_attempt(attempts)

        assert latest is not None
        assert latest.timestamp == datetime(2024, 1, 15, 12, 0, 0)
        assert latest.details == "Attempt #3"

    def test_get_latest_attempt_single(self):
        """Test getting latest attempt from single item list."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Only attempt"),
        ]

        latest = get_latest_attempt(attempts)

        assert latest is not None
        assert latest.details == "Only attempt"

    def test_get_latest_attempt_empty_list(self):
        """Test getting latest attempt from empty list."""
        attempts = []

        latest = get_latest_attempt(attempts)

        assert latest is None

    def test_get_latest_attempt_same_timestamp(self):
        """Test getting latest attempt when timestamps are the same."""
        timestamp = datetime(2024, 1, 15, 10, 0, 0)
        attempts = [
            AttemptInfo(timestamp=timestamp, details="Attempt #1"),
            AttemptInfo(timestamp=timestamp, details="Attempt #2"),
        ]

        latest = get_latest_attempt(attempts)

        assert latest is not None
        # When timestamps are equal, max() returns the first one encountered
        assert latest.details in ["Attempt #1", "Attempt #2"]


class TestFilterAttemptsByStatus:
    """Test filtering attempts by status."""

    def test_filter_by_status(self):
        """Test filtering attempts by status."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Started", status="started"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 11, 0, 0), details="Completed", status="completed"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 12, 0, 0), details="Failed", status="failed"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 13, 0, 0), details="Another completed", status="completed"),
        ]

        completed = filter_attempts_by_status(attempts, "completed")

        assert len(completed) == 2
        for attempt in completed:
            assert attempt.status == "completed"

    def test_filter_by_status_none_found(self):
        """Test filtering by status when no matches."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Started", status="started"),
        ]

        failed = filter_attempts_by_status(attempts, "failed")

        assert len(failed) == 0

    def test_filter_by_status_empty_list(self):
        """Test filtering empty list."""
        attempts = []

        result = filter_attempts_by_status(attempts, "started")

        assert len(result) == 0

    def test_filter_by_status_with_default_status(self):
        """Test filtering when many attempts have default status."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Attempt #1"),  # Default: "started"
            AttemptInfo(timestamp=datetime(2024, 1, 15, 11, 0, 0), details="Attempt #2", status="started"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 12, 0, 0), details="Completed", status="completed"),
        ]

        started = filter_attempts_by_status(attempts, "started")

        assert len(started) == 2


class TestGroupAttemptsByStatus:
    """Test grouping attempts by status."""

    def test_group_by_status(self):
        """Test grouping attempts by status."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Started", status="started"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 11, 0, 0), details="Completed #1", status="completed"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 12, 0, 0), details="Failed", status="failed"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 13, 0, 0), details="Completed #2", status="completed"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 14, 0, 0), details="Another started", status="started"),
        ]

        grouped = group_attempts_by_status(attempts)

        assert len(grouped) == 3
        assert len(grouped["started"]) == 2
        assert len(grouped["completed"]) == 2
        assert len(grouped["failed"]) == 1

    def test_group_by_status_empty_list(self):
        """Test grouping empty list."""
        attempts = []

        grouped = group_attempts_by_status(attempts)

        assert len(grouped) == 0

    def test_group_by_status_single_attempt(self):
        """Test grouping single attempt."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Attempt #1", status="started"),
        ]

        grouped = group_attempts_by_status(attempts)

        assert len(grouped) == 1
        assert len(grouped["started"]) == 1

    def test_group_by_status_all_same_status(self):
        """Test grouping when all attempts have same status."""
        attempts = [
            AttemptInfo(timestamp=datetime(2024, 1, 15, 10, 0, 0), details="Attempt #1", status="started"),
            AttemptInfo(timestamp=datetime(2024, 1, 15, 11, 0, 0), details="Attempt #2", status="started"),
        ]

        grouped = group_attempts_by_status(attempts)

        assert len(grouped) == 1
        assert "started" in grouped
        assert len(grouped["started"]) == 2


class TestGetCurrentAttempt:
    """Test getting the current attempt count from GitHub issue."""

    @patch("auto_coder.github_client.GitHubClient")
    def test_get_current_attempt_with_comments(self, mock_github_client):
        """Test getting current attempt when comments exist."""
        # Mock the GitHub client and repository
        mock_repo = Mock()
        mock_issue = Mock()
        mock_comment1 = Mock()
        mock_comment2 = Mock()

        mock_comment1.body = f"{ATTEMPT_COMMENT_PREFIX}1"
        mock_comment2.body = f"{ATTEMPT_COMMENT_PREFIX}2"

        mock_issue.get_comments.return_value = [mock_comment1, mock_comment2]
        mock_repo.get_issue.return_value = mock_issue
        mock_github_client.get_instance.return_value = mock_github_client
        mock_github_client.get_repository.return_value = mock_repo

        result = get_current_attempt("owner/repo", 123)

        assert result == 2

    @patch("auto_coder.github_client.GitHubClient")
    def test_get_current_attempt_no_comments(self, mock_github_client):
        """Test getting current attempt when no attempt comments exist."""
        # Mock the GitHub client and repository
        mock_repo = Mock()
        mock_issue = Mock()
        mock_comment = Mock()

        mock_comment.body = "Regular comment"
        mock_issue.get_comments.return_value = [mock_comment]
        mock_repo.get_issue.return_value = mock_issue
        mock_github_client.get_instance.return_value = mock_github_client
        mock_github_client.get_repository.return_value = mock_repo

        result = get_current_attempt("owner/repo", 123)

        assert result == 0

    @patch("auto_coder.github_client.GitHubClient")
    def test_get_current_attempt_empty_comments(self, mock_github_client):
        """Test getting current attempt when issue has no comments."""
        # Mock the GitHub client and repository
        mock_repo = Mock()
        mock_issue = Mock()

        mock_issue.get_comments.return_value = []
        mock_repo.get_issue.return_value = mock_issue
        mock_github_client.get_instance.return_value = mock_github_client
        mock_github_client.get_repository.return_value = mock_repo

        result = get_current_attempt("owner/repo", 123)

        assert result == 0

    @patch("auto_coder.github_client.GitHubClient")
    def test_get_current_attempt_with_mixed_comments(self, mock_github_client):
        """Test getting current attempt with mix of attempt and regular comments."""
        # Mock the GitHub client and repository
        mock_repo = Mock()
        mock_issue = Mock()

        comments = [
            Mock(body="Regular comment 1"),
            Mock(body=f"{ATTEMPT_COMMENT_PREFIX}1"),
            Mock(body="Another regular comment"),
            Mock(body=f"{ATTEMPT_COMMENT_PREFIX}2 | retry after conflicts"),
            Mock(body="Regular comment 3"),
        ]

        mock_issue.get_comments.return_value = comments
        mock_repo.get_issue.return_value = mock_issue
        mock_github_client.get_instance.return_value = mock_github_client
        mock_github_client.get_repository.return_value = mock_repo

        result = get_current_attempt("owner/repo", 123)

        assert result == 2

    @patch("auto_coder.github_client.GitHubClient")
    def test_get_current_attempt_with_legacy_timestamp_comments(self, mock_github_client):
        """Timestamp-prefixed comments still produce the right attempt number."""
        mock_repo = Mock()
        mock_issue = Mock()

        legacy_comments = [
            Mock(body=f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T10:00:00 | Attempt #1"),
            Mock(body=f"{ATTEMPT_COMMENT_PREFIX}2024-01-15T11:00:00 | Attempt #2"),
        ]

        mock_issue.get_comments.return_value = legacy_comments
        mock_repo.get_issue.return_value = mock_issue
        mock_github_client.get_instance.return_value = mock_github_client
        mock_github_client.get_repository.return_value = mock_repo

        result = get_current_attempt("owner/repo", 555)

        assert result == 2

    @patch("auto_coder.github_client.GitHubClient")
    def test_get_current_attempt_error(self, mock_github_client):
        """Test getting current attempt when error occurs."""
        # Mock the GitHub client to raise an exception
        mock_github_client.get_instance.return_value = mock_github_client
        mock_github_client.get_repository.side_effect = Exception("API error")

        result = get_current_attempt("owner/repo", 123)

        assert result == 0


class TestIncrementAttempt:
    """Test incrementing attempt count and propagation."""

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_synchronizes_sub_issue_attempts(self, mock_get_current_attempt, mock_github_client):
        """Test that sub-issues receive the same attempt number as parent, even with different current attempts."""
        # Parent issue #123 has current attempt 3
        # Sub-issue #456 has current attempt 1 (different from parent)
        # Sub-issue #789 has current attempt 0 (different from parent)
        # After incrementing parent to attempt 4, all should have attempt 4
        attempts = {123: 3, 456: 1, 789: 0}
        mock_get_current_attempt.side_effect = lambda repo, issue_num: attempts.get(issue_num, 0)

        mock_client = Mock()
        mock_repo = Mock()
        issues = {123: Mock(state="open"), 456: Mock(state="closed"), 789: Mock(state="closed")}

        mock_repo.get_issue.side_effect = lambda issue_num: issues[issue_num]
        mock_client.get_repository.return_value = mock_repo
        # Parent issue #123 has two sub-issues: #456 and #789
        mock_client.get_all_sub_issues.side_effect = lambda repo, issue_num: [456, 789] if issue_num == 123 else []
        mock_github_client.get_instance.return_value = mock_client

        result = increment_attempt("owner/repo", 123)

        assert result == 4
        # Should have 3 calls: parent + 2 sub-issues
        assert mock_client.add_comment_to_issue.call_count == 3

        # Get all calls to add_comment_to_issue
        calls = mock_client.add_comment_to_issue.call_args_list

        # Verify parent issue #123 gets attempt 4
        parent_call = calls[0]
        assert parent_call[0][1] == 123
        assert parent_call[0][2] == f"{ATTEMPT_COMMENT_PREFIX}4"

        # Verify sub-issue #456 gets attempt 4 (synchronized with parent, not its old attempt 1)
        sub_call_1 = calls[1]
        assert sub_call_1[0][1] == 456
        assert sub_call_1[0][2] == f"{ATTEMPT_COMMENT_PREFIX}4"

        # Verify sub-issue #789 gets attempt 4 (synchronized with parent, not its old attempt 0)
        sub_call_2 = calls[2]
        assert sub_call_2[0][1] == 789
        assert sub_call_2[0][2] == f"{ATTEMPT_COMMENT_PREFIX}4"

        # All issues should have the SAME attempt number (4), proving synchronization
        assert sub_call_1[0][2] == parent_call[0][2] == sub_call_2[0][2]

        # Both closed sub-issues should be reopened
        assert mock_client.reopen_issue.call_count == 2
        reopen_calls = mock_client.reopen_issue.call_args_list
        reopened_issues = [call[0][1] for call in reopen_calls]
        assert 456 in reopened_issues
        assert 789 in reopened_issues

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_posts_comment_and_increments(self, mock_get_current_attempt, mock_github_client):
        """Incrementing adds a new attempt comment based on the current count."""
        mock_get_current_attempt.return_value = 1

        mock_client = Mock()
        mock_repo = Mock()
        mock_issue = Mock(state="open")

        mock_repo.get_issue.return_value = mock_issue
        mock_client.get_repository.return_value = mock_repo
        mock_client.get_all_sub_issues.return_value = []
        mock_github_client.get_instance.return_value = mock_client

        result = increment_attempt("owner/repo", 321)

        assert result == 2
        mock_get_current_attempt.assert_called_once_with("owner/repo", 321)
        mock_client.add_comment_to_issue.assert_called_once()
        args = mock_client.add_comment_to_issue.call_args[0]
        assert args[0] == "owner/repo"
        assert args[1] == 321
        assert ATTEMPT_COMMENT_PREFIX in args[2]
        assert args[2].strip() == f"{ATTEMPT_COMMENT_PREFIX}2"

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_propagates_and_reopens_sub_issues(self, mock_get_current_attempt, mock_github_client):
        """Attempt increment posts comments for sub-issues and reopens closed ones."""
        attempts = {123: 1, 456: 0}
        mock_get_current_attempt.side_effect = lambda repo, issue_num: attempts.get(issue_num, 0)

        mock_client = Mock()
        mock_repo = Mock()
        issues = {123: Mock(state="open"), 456: Mock(state="closed")}

        mock_repo.get_issue.side_effect = lambda issue_num: issues[issue_num]
        mock_client.get_repository.return_value = mock_repo
        mock_client.get_all_sub_issues.side_effect = lambda repo, issue_num: [456] if issue_num == 123 else []
        mock_github_client.get_instance.return_value = mock_client

        result = increment_attempt("owner/repo", 123)

        assert result == 2
        assert mock_client.add_comment_to_issue.call_count == 2
        parent_call, sub_call = mock_client.add_comment_to_issue.call_args_list
        assert parent_call[0][1] == 123
        assert parent_call[0][2] == f"{ATTEMPT_COMMENT_PREFIX}2"
        assert sub_call[0][1] == 456
        assert sub_call[0][2] == f"{ATTEMPT_COMMENT_PREFIX}2"
        mock_client.reopen_issue.assert_called_once()
        reopen_args = mock_client.reopen_issue.call_args[0]
        assert reopen_args[0] == "owner/repo"
        assert reopen_args[1] == 456
        assert "parent issue #123" in reopen_args[2]

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_does_not_reopen_open_sub_issue(self, mock_get_current_attempt, mock_github_client):
        """Open sub-issues are left open but still receive a new attempt comment."""
        attempts = {123: 0, 456: 0}
        mock_get_current_attempt.side_effect = lambda repo, issue_num: attempts.get(issue_num, 0)

        mock_client = Mock()
        mock_repo = Mock()
        issues = {123: Mock(state="open"), 456: Mock(state="open")}

        mock_repo.get_issue.side_effect = lambda issue_num: issues[issue_num]
        mock_client.get_repository.return_value = mock_repo
        mock_client.get_all_sub_issues.side_effect = lambda repo, issue_num: [456] if issue_num == 123 else []
        mock_github_client.get_instance.return_value = mock_client

        result = increment_attempt("owner/repo", 123)

        assert result == 1
        assert mock_client.add_comment_to_issue.call_count == 2
        mock_client.reopen_issue.assert_not_called()

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_continues_when_sub_issue_fails(self, mock_get_current_attempt, mock_github_client):
        """Propagating attempts should continue even if one sub-issue errors."""
        attempts = {123: 1, 456: 0, 789: 0}
        mock_get_current_attempt.side_effect = lambda repo, issue_num: attempts.get(issue_num, 0)

        mock_client = Mock()
        mock_repo = Mock()
        issues = {123: Mock(state="open"), 456: Mock(state="closed")}

        def get_issue_side_effect(issue_num):
            if issue_num == 789:
                raise Exception("failed to load sub-issue")
            return issues.get(issue_num, Mock(state="open"))

        mock_repo.get_issue.side_effect = get_issue_side_effect
        mock_client.get_repository.return_value = mock_repo
        mock_client.get_all_sub_issues.side_effect = lambda repo, issue_num: [456, 789] if issue_num == 123 else []
        mock_github_client.get_instance.return_value = mock_client

        result = increment_attempt("owner/repo", 123)

        assert result == 2
        # Parent and first sub-issue should get comments; failing sub-issue is skipped
        assert mock_client.add_comment_to_issue.call_count == 2
        mock_client.reopen_issue.assert_called_once_with("owner/repo", 456, ANY)

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_with_explicit_attempt_number(self, mock_get_current_attempt, mock_github_client):
        """When attempt_number is provided, it should be used instead of incrementing."""
        # Current attempt is 5, but we explicitly set attempt_number to 10
        mock_get_current_attempt.return_value = 5

        mock_client = Mock()
        mock_repo = Mock()
        mock_issue = Mock(state="open")

        mock_repo.get_issue.return_value = mock_issue
        mock_client.get_repository.return_value = mock_repo
        mock_client.get_all_sub_issues.return_value = []
        mock_github_client.get_instance.return_value = mock_client

        result = increment_attempt("owner/repo", 321, attempt_number=10)

        # Should return the explicit attempt number, not current + 1
        assert result == 10
        mock_get_current_attempt.assert_called_once_with("owner/repo", 321)
        mock_client.add_comment_to_issue.assert_called_once()
        args = mock_client.add_comment_to_issue.call_args[0]
        assert args[0] == "owner/repo"
        assert args[1] == 321
        assert args[2].strip() == f"{ATTEMPT_COMMENT_PREFIX}10"

    @patch("auto_coder.github_client.GitHubClient")
    @patch("auto_coder.attempt_manager.get_current_attempt")
    def test_increment_attempt_with_explicit_attempt_number_propagates_to_sub_issues(self, mock_get_current_attempt, mock_github_client):
        """Explicit attempt_number should be propagated to sub-issues for synchronized increment."""
        attempts = {123: 1, 456: 0}
        mock_get_current_attempt.side_effect = lambda repo, issue_num: attempts.get(issue_num, 0)

        mock_client = Mock()
        mock_repo = Mock()
        issues = {123: Mock(state="open"), 456: Mock(state="closed")}

        mock_repo.get_issue.side_effect = lambda issue_num: issues[issue_num]
        mock_client.get_repository.return_value = mock_repo
        mock_client.get_all_sub_issues.side_effect = lambda repo, issue_num: [456] if issue_num == 123 else []
        mock_github_client.get_instance.return_value = mock_client

        # Explicitly set attempt_number to 5 for parent issue #123
        result = increment_attempt("owner/repo", 123, attempt_number=5)

        # Should return the explicit attempt number
        assert result == 5
        assert mock_client.add_comment_to_issue.call_count == 2
        parent_call, sub_call = mock_client.add_comment_to_issue.call_args_list
        # Both parent and sub-issue should have attempt 5 (synchronized)
        assert parent_call[0][2] == f"{ATTEMPT_COMMENT_PREFIX}5"
        assert sub_call[0][2] == f"{ATTEMPT_COMMENT_PREFIX}5"


class TestGenerateWorkBranchName:
    """Test branch name generation for attempts."""

    def test_generate_branch_without_attempt(self):
        """Branch omits attempt segment when attempt is zero."""
        assert generate_work_branch_name(42, 0) == "issue-42"

    def test_generate_branch_with_attempt(self):
        """Branch includes attempt segment when attempt is positive."""
        assert generate_work_branch_name(42, 3) == "issue-42_attempt-3"


class TestExtractAttemptFromBranch:
    """Test extracting attempt number from branch names."""

    def test_extract_attempt_from_branch_with_attempt(self):
        """Test extracting attempt number from branch with attempt suffix."""
        branch_name = "issue-123_attempt-1"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt == 1

    def test_extract_attempt_from_branch_attempt_2(self):
        """Test extracting attempt number 2."""
        branch_name = "issue-456_attempt-2"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt == 2

    def test_extract_attempt_from_branch_attempt_10(self):
        """Test extracting higher attempt number."""
        branch_name = "issue-789_attempt-10"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt == 10

    def test_extract_attempt_from_branch_without_attempt(self):
        """Test extracting attempt when branch doesn't have attempt suffix."""
        branch_name = "issue-123"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt is None

    def test_extract_attempt_from_branch_with_other_suffix(self):
        """Test extracting attempt when branch has other suffixes."""
        branch_name = "issue-123/feature-new-code"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt is None

    def test_extract_attempt_from_branch_empty(self):
        """Test extracting attempt from empty branch name."""
        branch_name = ""
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt is None

    def test_extract_attempt_from_branch_none(self):
        """Test extracting attempt from None."""
        attempt = extract_attempt_from_branch(None)

        assert attempt is None

    def test_extract_attempt_from_branch_case_insensitive(self):
        """Test that extraction is case insensitive."""
        branch_name = "issue-123_ATTEMPT-5"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt == 5

    def test_extract_attempt_from_branch_mixed_case(self):
        """Test extraction with mixed case."""
        branch_name = "Issue-456_AtTeMpT-3"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt == 3

    def test_extract_attempt_from_branch_special_chars(self):
        """Test extraction when branch has multiple segments (returns None due to strict pattern)."""
        # The pattern requires attempt- to come immediately after issue-XXX_
        # So this won't match because there are extra segments
        branch_name = "issue-123/special-chars_attempt-7"
        attempt = extract_attempt_from_branch(branch_name)

        # Pattern is strict: issue-\d+_attempt-(\d+)
        # So this returns None
        assert attempt is None

    def test_extract_attempt_from_branch_with_feature_prefix(self):
        """Test extraction from branch with valid feature/ prefix."""
        branch_name = "feature/issue-456_attempt-3"
        attempt = extract_attempt_from_branch(branch_name)

        assert attempt == 3

    def test_extract_attempt_from_branch_multiple_attempt_patterns(self):
        """Test extraction when multiple attempt patterns exist."""
        branch_name = "issue-123_attempt-1_attempt-2"
        attempt = extract_attempt_from_branch(branch_name)

        # Should match the first attempt pattern
        assert attempt == 1

    def test_extract_attempt_from_branch_complex_name(self):
        """Test extraction from complex branch name (returns None due to strict pattern)."""
        # The pattern is strict and doesn't allow extra segments
        branch_name = "feature/issue-789/complex-name_attempt-4"
        attempt = extract_attempt_from_branch(branch_name)

        # Pattern requires exact format: issue-\d+_attempt-(\d+)
        assert attempt is None

    def test_extract_attempt_legacy_slash_format(self):
        """Test that legacy slash format is still supported."""
        branch_name = "issue-123/attempt-1"
        assert extract_attempt_from_branch(branch_name) == 1

    def test_extract_attempt_new_underscore_format(self):
        """Test new underscore format."""
        branch_name = "issue-123_attempt-1"
        assert extract_attempt_from_branch(branch_name) == 1
