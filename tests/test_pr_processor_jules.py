"""Tests for Jules PR processing functionality in pr_processor.py"""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.cloud_manager import CloudManager
from src.auto_coder.pr_processor import (
    _extract_session_id_from_pr_body,
    _is_jules_pr,
    _process_jules_pr,
    _send_jules_error_feedback,
    _update_jules_pr_body,
)


class TestExtractSessionIdFromPrBody:
    """Test cases for _extract_session_id_from_pr_body function."""

    def test_extract_session_id_from_pr_body_simple(self):
        """Test extracting session ID from simple PR body."""
        pr_body = "Session ID: abc123def456"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "abc123def456"

    def test_extract_session_id_from_pr_body_with_session_keyword(self):
        """Test extracting session ID with 'Session:' keyword."""
        pr_body = "Session: xyz789"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "xyz789"

    def test_extract_session_id_from_pr_body_url_parameter(self):
        """Test extracting session ID from URL parameter."""
        pr_body = "https://example.com/session=session123&id=456"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "session123"

    def test_extract_session_id_from_pr_body_session_id_parameter(self):
        """Test extracting session ID from session_id parameter."""
        pr_body = "https://example.com/page?session_id=abcd1234"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "abcd1234"

    def test_extract_session_id_from_pr_body_mixed_format(self):
        """Test extracting session ID from mixed format PR body."""
        pr_body = """
        This PR was created by Jules.

        Session ID: sessionABC123
        More details here.
        """
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "sessionABC123"

    def test_extract_session_id_from_pr_body_no_session(self):
        """Test that None is returned when no session ID is found."""
        pr_body = "This PR fixes a bug."
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id is None

    def test_extract_session_id_from_pr_body_empty(self):
        """Test that None is returned for empty PR body."""
        pr_body = ""
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id is None

    def test_extract_session_id_from_pr_body_none(self):
        """Test that None is returned for None PR body."""
        pr_body = None
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id is None

    def test_extract_session_id_from_pr_body_alphanumeric(self):
        """Test extracting alphanumeric session ID."""
        pr_body = "Session ID: a1b2c3d4e5f6g7h8i9j0"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "a1b2c3d4e5f6g7h8i9j0"

    def test_extract_session_id_from_pr_body_with_dashes(self):
        """Test extracting session ID with dashes."""
        pr_body = "Session: session-abc-123-def"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "session-abc-123-def"

    def test_extract_session_id_from_pr_body_with_underscores(self):
        """Test extracting session ID with underscores."""
        pr_body = "Session ID: session_abc_123_def"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "session_abc_123_def"

    def test_extract_session_id_from_pr_body_case_insensitive(self):
        """Test that session ID extraction is case insensitive."""
        pr_body = "SESSION ID: MySession123"
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "MySession123"

    def test_extract_session_id_from_pr_body_long_format(self):
        """Test extracting session ID from long PR body."""
        pr_body = """
        # Bug Fix for Authentication

        ## Problem
        The authentication system has a bug.

        ## Solution
        Fixed the issue by updating the login logic.

        ## Session ID
        This issue was tracked with session ID: longSessionIdValue12345
        """
        session_id = _extract_session_id_from_pr_body(pr_body)
        assert session_id == "longSessionIdValue12345"


class TestUpdateJulesPrBody:
    """Test cases for _update_jules_pr_body function."""

    def test_update_jules_pr_body_success(self):
        """Test successfully updating PR body."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "Original PR body content."
        issue_number = 456
        github_client = Mock()

        # Setup mocks
        mock_repo = Mock()
        mock_pr = Mock()
        github_client.get_repository.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is True
        github_client.get_repository.assert_called_with(repo_name)
        mock_repo.get_pull.assert_called_with(pr_number)
        mock_pr.edit.assert_called_once()

        # Verify the body contains the close statement and issue link
        edit_kwargs = mock_pr.edit.call_args.kwargs
        body_content = edit_kwargs["body"]
        assert "close #456" in body_content
        assert "https://github.com/owner/repo/issues/456" in body_content
        assert "Original PR body content." in body_content

    def test_update_jules_pr_body_already_has_close(self):
        """Test that PR body update is skipped if already has close reference."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "This PR closes #456 and fixes the issue."
        issue_number = 456
        github_client = Mock()

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is True
        # GitHub client should not be called
        github_client.get_repository.assert_not_called()

    def test_update_jules_pr_body_already_has_closes(self):
        """Test that PR body update is skipped if already has closes reference."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "This PR closes #456 and fixes the issue."
        issue_number = 456
        github_client = Mock()

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is True
        # GitHub client should not be called
        github_client.get_repository.assert_not_called()

    def test_update_jules_pr_body_case_insensitive_check(self):
        """Test that close reference check is case insensitive."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "This PR CLOSES #456 and fixes the issue."
        issue_number = 456
        github_client = Mock()

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is True
        # GitHub client should not be called
        github_client.get_repository.assert_not_called()

    def test_update_jules_pr_body_failure(self):
        """Test failure when updating PR body."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "Original PR body content."
        issue_number = 456
        github_client = Mock()

        # Setup mocks to raise exception
        github_client.get_repository.side_effect = Exception("API Error")

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is False

    def test_update_jules_pr_body_empty_original(self):
        """Test updating PR body when original body is empty."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = ""
        issue_number = 456
        github_client = Mock()

        # Setup mocks
        mock_repo = Mock()
        mock_pr = Mock()
        github_client.get_repository.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is True
        mock_pr.edit.assert_called_once()

        # Verify body is properly formatted even when original is empty
        edit_kwargs = mock_pr.edit.call_args.kwargs
        body_content = edit_kwargs["body"]
        assert "close #456" in body_content
        assert "https://github.com/owner/repo/issues/456" in body_content

    def test_update_jules_pr_body_with_newline_ending(self):
        """Test updating PR body when original body ends with newline."""
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "Original PR body content.\n"
        issue_number = 456
        github_client = Mock()

        # Setup mocks
        mock_repo = Mock()
        mock_pr = Mock()
        github_client.get_repository.return_value = mock_repo
        mock_repo.get_pull.return_value = mock_pr

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, github_client)

        # Assert
        assert result is True
        mock_pr.edit.assert_called_once()

        # Verify body is properly formatted
        edit_kwargs = mock_pr.edit.call_args.kwargs
        body_content = edit_kwargs["body"]
        assert "close #456" in body_content
        assert "https://github.com/owner/repo/issues/456" in body_content
        assert "Original PR body content.\n" in body_content


class TestProcessJulesPr:
    """Test cases for _process_jules_pr function."""

    def test_process_jules_pr_not_author(self):
        """Test that non-Jules PRs are skipped."""
        pr_data = {
            "number": 123,
            "body": "This PR fixes a bug.",
            "user": {"login": "otheruser"},
        }
        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is True  # Not an error, just not a Jules PR
        # CloudManager should not be called
        # gh command should not be called

    def test_process_jules_pr_no_session_id(self):
        """Test that PRs without session ID return False."""
        pr_data = {
            "number": 123,
            "body": "This PR fixes a bug.",
            "user": {"login": "google-labs-jules"},
        }
        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is False
        # CloudManager.get_issue_by_session should not be called

    def test_process_jules_pr_no_matching_issue(self):
        """Test that PRs with no matching issue return False."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "Session ID: nonexistent123",
            "user": {"login": "google-labs-jules"},
        }

        with patch("src.auto_coder.pr_processor.CloudManager") as mock_cloud_manager_class:
            mock_cloud_manager = Mock()
            mock_cloud_manager.get_issue_by_session.return_value = None
            mock_cloud_manager_class.return_value = mock_cloud_manager

            github_client = Mock()
            repo_name = "owner/repo"

            # Execute
            result = _process_jules_pr(repo_name, pr_data, github_client)

            # Assert
            assert result is False
            mock_cloud_manager.get_issue_by_session.assert_called_once_with("nonexistent123")

    @patch("src.auto_coder.pr_processor._update_jules_pr_body")
    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_success(self, mock_cloud_manager_class, mock_update_body):
        """Test successful Jules PR processing."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "Session ID: sessionABC123",
            "user": {"login": "google-labs-jules"},
        }

        mock_cloud_manager = Mock()
        mock_cloud_manager.get_issue_by_session.return_value = 456
        mock_cloud_manager_class.return_value = mock_cloud_manager

        mock_update_body.return_value = True

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is True
        mock_cloud_manager.get_issue_by_session.assert_called_once_with("sessionABC123")
        mock_update_body.assert_called_once_with(repo_name, 123, "Session ID: sessionABC123", 456, github_client)

    @patch("src.auto_coder.pr_processor._update_jules_pr_body")
    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_update_failure(self, mock_cloud_manager_class, mock_update_body):
        """Test that PR body update failure is handled correctly."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "Session ID: sessionXYZ789",
            "user": {"login": "google-labs-jules"},
        }

        mock_cloud_manager = Mock()
        mock_cloud_manager.get_issue_by_session.return_value = 789
        mock_cloud_manager_class.return_value = mock_cloud_manager

        mock_update_body.return_value = False

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is False
        mock_cloud_manager.get_issue_by_session.assert_called_once_with("sessionXYZ789")
        mock_update_body.assert_called_once_with(repo_name, 123, "Session ID: sessionXYZ789", 789, github_client)

    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_exception_handling(self, mock_cloud_manager_class):
        """Test that exceptions are handled gracefully."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "Session ID: sessionError",
            "user": {"login": "google-labs-jules"},
        }

        mock_cloud_manager = Mock()
        mock_cloud_manager.get_issue_by_session.side_effect = Exception("Cloud error")
        mock_cloud_manager_class.return_value = mock_cloud_manager

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is False

    @patch("src.auto_coder.pr_processor._extract_session_id_from_pr_body")
    @patch("src.auto_coder.pr_processor._update_jules_pr_body")
    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_with_url_session_id(self, mock_cloud_manager_class, mock_update_body, mock_extract_session):
        """Test Jules PR processing with session ID from URL."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "https://example.com/session=urlSession123",
            "user": {"login": "google-labs-jules"},
        }

        mock_extract_session.return_value = "urlSession123"

        mock_cloud_manager = Mock()
        mock_cloud_manager.get_issue_by_session.return_value = 999
        mock_cloud_manager_class.return_value = mock_cloud_manager

        mock_update_body.return_value = True

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is True
        mock_extract_session.assert_called_once_with("https://example.com/session=urlSession123")
        mock_cloud_manager.get_issue_by_session.assert_called_once_with("urlSession123")
        mock_update_body.assert_called_once_with(repo_name, 123, "https://example.com/session=urlSession123", 999, github_client)

    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_empty_body(self, mock_cloud_manager_class):
        """Test Jules PR processing with empty body."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "",
            "user": {"login": "google-labs-jules"},
        }

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is False
        # CloudManager should not be called when body is empty
        mock_cloud_manager_class.assert_not_called()

    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_none_body(self, mock_cloud_manager_class):
        """Test Jules PR processing with None body."""
        # Setup
        pr_data = {
            "number": 123,
            "body": None,
            "user": {"login": "google-labs-jules"},
        }

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is False
        # CloudManager should not be called when body is None
        mock_cloud_manager_class.assert_not_called()

    @patch("src.auto_coder.pr_processor._update_jules_pr_body")
    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_different_repo_formats(self, mock_cloud_manager_class, mock_update_body):
        """Test Jules PR processing with different repository name formats."""
        # Setup
        pr_data = {
            "number": 123,
            "body": "Session ID: sessionRepo123",
            "user": {"login": "google-labs-jules"},
        }

        mock_cloud_manager = Mock()
        mock_cloud_manager.get_issue_by_session.return_value = 321
        mock_cloud_manager_class.return_value = mock_cloud_manager

        mock_update_body.return_value = True

        github_client = Mock()

        # Test with different repository name formats
        test_repos = [
            "owner/repo",
            "user-name/repo-name",
            "org_with_underscore/project_with_underscore",
        ]

        for repo_name in test_repos:
            # Execute
            result = _process_jules_pr(repo_name, pr_data, github_client)

            # Assert
            assert result is True
            # CloudManager is instantiated with the correct repo_name
            mock_cloud_manager_class.assert_called_with(repo_name)

    @patch("src.auto_coder.pr_processor._update_jules_pr_body")
    @patch("src.auto_coder.pr_processor.CloudManager")
    def test_process_jules_pr_long_session_id(self, mock_cloud_manager_class, mock_update_body):
        """Test Jules PR processing with a long session ID."""
        # Setup
        long_session_id = "very_long_session_id_with_many_characters_1234567890"
        pr_data = {
            "number": 123,
            "body": f"Session ID: {long_session_id}",
            "user": {"login": "google-labs-jules"},
        }

        mock_cloud_manager = Mock()
        mock_cloud_manager.get_issue_by_session.return_value = 555
        mock_cloud_manager_class.return_value = mock_cloud_manager

        mock_update_body.return_value = True

        github_client = Mock()
        repo_name = "owner/repo"

        # Execute
        result = _process_jules_pr(repo_name, pr_data, github_client)

        # Assert
        assert result is True
        mock_cloud_manager.get_issue_by_session.assert_called_once_with(long_session_id)


class TestIsJulesPr:
    """Test cases for _is_jules_pr function."""

    def test_is_jules_pr_true(self):
        """Test that Jules PRs are correctly identified."""
        pr_data = {
            "number": 123,
            "user": {"login": "google-labs-jules"},
        }
        assert _is_jules_pr(pr_data) is True

    def test_is_jules_pr_false(self):
        """Test that non-Jules PRs are correctly identified."""
        pr_data = {
            "number": 123,
            "user": {"login": "otheruser"},
        }
        assert _is_jules_pr(pr_data) is False

    def test_is_jules_pr_no_user(self):
        """Test that PRs without user are not identified as Jules PRs."""
        pr_data = {
            "number": 123,
        }
        assert _is_jules_pr(pr_data) is False

    def test_is_jules_pr_empty_login(self):
        """Test that PRs with empty login are not identified as Jules PRs."""
        pr_data = {
            "number": 123,
            "user": {"login": ""},
        }
        assert _is_jules_pr(pr_data) is False

    def test_is_jules_pr_none_login(self):
        """Test that PRs with None login are not identified as Jules PRs."""
        pr_data = {
            "number": 123,
            "user": {"login": None},
        }
        assert _is_jules_pr(pr_data) is False

    def test_is_jules_pr_different_case(self):
        """Test that login comparison is case-sensitive."""
        pr_data = {
            "number": 123,
            "user": {"login": "Google-Labs-Jules"},
        }
        assert _is_jules_pr(pr_data) is False


class TestSendJulesErrorFeedback:
    """Test cases for _send_jules_error_feedback function."""

    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.jules_client.JulesClient")
    def test_send_jules_error_feedback_success(self, mock_jules_client_class, mock_get_logs):
        """Test successful sending of error feedback to Jules."""
        # Setup
        mock_jules_client = Mock()
        mock_jules_client.send_message.return_value = "Acknowledged, will fix the issues"
        mock_jules_client_class.return_value = mock_jules_client

        mock_get_logs.return_value = "Error: Test failed\nStack trace here"

        pr_data = {
            "number": 123,
            "title": "Fix authentication bug",
            "user": {"login": "google-labs-jules"},
            "_jules_session_id": "sessionABC123",
        }

        failed_checks = [{"name": "test", "status": "failed"}]
        repo_name = "owner/repo"
        config = Mock()
        github_client = Mock()

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)

        # Assert
        assert len(actions) == 2
        assert "Sent CI failure logs to Jules session 'sessionABC123' for PR #123" in actions[0]
        assert "Posted comment on PR #123 stating that a fix has been requested from Jules" in actions[1]
        mock_get_logs.assert_called_once_with(repo_name, config, failed_checks, pr_data)
        mock_jules_client.send_message.assert_called_once()

        # Check the message sent to Jules
        call_args = mock_jules_client.send_message.call_args
        assert call_args[0][0] == "sessionABC123"
        message = call_args[0][1]
        assert "CI checks failed for PR #123 in owner/repo" in message
        assert "Error: Test failed" in message
        assert "Fix authentication bug" in message

        # Check that comment was posted on PR
        github_client.add_comment_to_pr.assert_called_once_with(repo_name, 123, "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates.")

    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.jules_client.JulesClient")
    def test_send_jules_error_feedback_no_session_id(self, mock_jules_client_class, mock_get_logs):
        """Test that error is returned when no session ID is found."""
        # Setup
        pr_data = {
            "number": 123,
            "title": "Fix authentication bug",
            "user": {"login": "google-labs-jules"},
            # No _jules_session_id
        }

        failed_checks = [{"name": "test", "status": "failed"}]
        repo_name = "owner/repo"
        config = Mock()
        github_client = Mock()

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)

        # Assert
        assert len(actions) == 1
        assert "Cannot send error feedback to Jules for PR #123: no session ID found" in actions[0]
        # JulesClient should not be instantiated
        mock_jules_client_class.assert_not_called()
        # GitHub client should not be called
        github_client.add_comment_to_pr.assert_not_called()

    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.jules_client.JulesClient")
    def test_send_jules_error_feedback_exception(self, mock_jules_client_class, mock_get_logs):
        """Test that exceptions are handled gracefully."""
        # Setup
        mock_jules_client = Mock()
        mock_jules_client.send_message.side_effect = Exception("Connection error")
        mock_jules_client_class.return_value = mock_jules_client

        pr_data = {
            "number": 123,
            "title": "Fix authentication bug",
            "user": {"login": "google-labs-jules"},
            "_jules_session_id": "sessionABC123",
        }

        failed_checks = [{"name": "test", "status": "failed"}]
        repo_name = "owner/repo"
        config = Mock()
        github_client = Mock()

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)

        # Assert
        assert len(actions) == 1
        assert "Error sending Jules error feedback for PR #123" in actions[0]
        assert "Connection error" in actions[0]

    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.jules_client.JulesClient")
    def test_send_jules_error_feedback_with_empty_logs(self, mock_jules_client_class, mock_get_logs):
        """Test sending error feedback with empty logs."""
        # Setup
        mock_jules_client = Mock()
        mock_jules_client.send_message.return_value = "Will review"
        mock_jules_client_class.return_value = mock_jules_client

        mock_get_logs.return_value = ""

        pr_data = {
            "number": 456,
            "title": "Update documentation",
            "user": {"login": "google-labs-jules"},
            "_jules_session_id": "sessionXYZ789",
        }

        failed_checks = []
        repo_name = "owner/repo"
        config = Mock()
        github_client = Mock()

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)

        # Assert
        assert len(actions) == 2
        assert "Sent CI failure logs to Jules session 'sessionXYZ789' for PR #456" in actions[0]
        assert "Posted comment on PR #456 stating that a fix has been requested from Jules" in actions[1]
        mock_get_logs.assert_called_once_with(repo_name, config, failed_checks, pr_data)
        mock_jules_client.send_message.assert_called_once()

        # Check the message includes empty logs
        call_args = mock_jules_client.send_message.call_args
        message = call_args[0][1]
        assert "CI checks failed for PR #456 in owner/repo" in message

        # Check that comment was posted on PR
        github_client.add_comment_to_pr.assert_called_once_with(repo_name, 456, "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates.")

    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.jules_client.JulesClient")
    def test_send_jules_error_feedback_no_github_client(self, mock_jules_client_class, mock_get_logs):
        """Test that PR comment is skipped when no GitHub client is provided."""
        # Setup
        mock_jules_client = Mock()
        mock_jules_client.send_message.return_value = "Acknowledged"
        mock_jules_client_class.return_value = mock_jules_client

        mock_get_logs.return_value = "Error: Test failed"

        pr_data = {
            "number": 789,
            "title": "Fix bug",
            "user": {"login": "google-labs-jules"},
            "_jules_session_id": "sessionNoClient",
        }

        failed_checks = [{"name": "test", "status": "failed"}]
        repo_name = "owner/repo"
        config = Mock()
        # No github_client provided (None)

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client=None)

        # Assert
        assert len(actions) == 2
        assert "Sent CI failure logs to Jules session 'sessionNoClient' for PR #789" in actions[0]
        assert "Skipped posting comment on PR #789: no GitHub client available" in actions[1]
        mock_jules_client.send_message.assert_called_once()

    @patch("src.auto_coder.pr_processor._get_github_actions_logs")
    @patch("src.auto_coder.jules_client.JulesClient")
    def test_send_jules_error_feedback_comment_exception(self, mock_jules_client_class, mock_get_logs):
        """Test that exception in posting PR comment is handled gracefully."""
        # Setup
        mock_jules_client = Mock()
        mock_jules_client.send_message.return_value = "Acknowledged"
        mock_jules_client_class.return_value = mock_jules_client

        mock_get_logs.return_value = "Error: Test failed"

        pr_data = {
            "number": 999,
            "title": "Fix bug",
            "user": {"login": "google-labs-jules"},
            "_jules_session_id": "sessionCommentError",
        }

        failed_checks = [{"name": "test", "status": "failed"}]
        repo_name = "owner/repo"
        config = Mock()
        github_client = Mock()
        # Make add_comment_to_pr raise an exception
        github_client.add_comment_to_pr.side_effect = Exception("GitHub API error")

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)

        # Assert
        assert len(actions) == 2
        assert "Sent CI failure logs to Jules session 'sessionCommentError' for PR #999" in actions[0]
        assert "Failed to post comment on PR #999: GitHub API error" in actions[1]
        mock_jules_client.send_message.assert_called_once()
