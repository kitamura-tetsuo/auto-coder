"""
Tests for PR creation with issue linking functionality.
"""

import json
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.issue_processor import _create_pr_for_issue


class TestPRIssueLinking:
    """Test cases for PR creation with issue linking."""

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("time.sleep")
    def test_create_pr_with_closes_keyword(self, mock_sleep, mock_cmd):
        """Test that PR body contains 'Closes #<issue_number>' keyword."""
        # Setup
        mock_cmd.run_command.return_value = Mock(
            success=True,
            stdout="https://github.com/test/repo/pull/456",
            stderr="",
            returncode=0,
        )

        mock_github_client = Mock()
        mock_github_client.get_pr_closing_issues.return_value = [123]

        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "This is a test issue",
        }

        # Execute
        result = _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="fix-issue-123",
            base_branch="main",
            llm_response="Fixed the issue",
            github_client=mock_github_client,
            message_backend_manager=None,
            dry_run=False,
        )

        # Assert
        assert "Successfully created PR" in result
        # Verify that gh pr create was called with body containing "Closes #123"
        call_args = mock_cmd.run_command.call_args[0][0]
        body_index = call_args.index("--body")
        pr_body = call_args[body_index + 1]
        assert "Closes #123" in pr_body

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("time.sleep")
    def test_create_pr_verifies_issue_link(self, mock_sleep, mock_cmd):
        """Test that PR creation verifies the issue link."""
        # Setup
        mock_cmd.run_command.return_value = Mock(
            success=True,
            stdout="https://github.com/test/repo/pull/456",
            stderr="",
            returncode=0,
        )

        mock_github_client = Mock()
        mock_github_client.get_pr_closing_issues.return_value = [123]

        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "This is a test issue",
        }

        # Execute
        result = _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="fix-issue-123",
            base_branch="main",
            llm_response="Fixed the issue",
            github_client=mock_github_client,
            message_backend_manager=None,
            dry_run=False,
        )

        # Assert
        assert "Successfully created PR" in result
        mock_github_client.get_pr_closing_issues.assert_called_once_with("test/repo", 456)

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("time.sleep")
    @patch("sys.exit")
    def test_create_pr_exits_if_link_not_verified(self, mock_exit, mock_sleep, mock_cmd):
        """Test that PR creation exits if issue link is not verified."""
        # Setup
        mock_cmd.run_command.return_value = Mock(
            success=True,
            stdout="https://github.com/test/repo/pull/456",
            stderr="",
            returncode=0,
        )

        mock_github_client = Mock()
        # Return empty list - issue is not linked
        mock_github_client.get_pr_closing_issues.return_value = []

        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "This is a test issue",
        }

        # Execute
        _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="fix-issue-123",
            base_branch="main",
            llm_response="Fixed the issue",
            github_client=mock_github_client,
            message_backend_manager=None,
            dry_run=False,
        )

        # Assert
        mock_exit.assert_called_once_with(1)

    @patch("src.auto_coder.issue_processor.cmd")
    def test_create_pr_dry_run_skips_verification(self, mock_cmd):
        """Test that dry run skips PR creation and verification."""
        # Setup
        mock_github_client = Mock()

        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "This is a test issue",
        }

        # Execute
        result = _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="fix-issue-123",
            base_branch="main",
            llm_response="Fixed the issue",
            github_client=mock_github_client,
            message_backend_manager=None,
            dry_run=True,
        )

        # Assert
        assert "[DRY RUN]" in result
        mock_cmd.run_command.assert_not_called()
        mock_github_client.get_pr_closing_issues.assert_not_called()

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("time.sleep")
    def test_create_pr_with_custom_body_adds_closes_keyword(self, mock_sleep, mock_cmd):
        """Test that custom PR body gets 'Closes #' keyword prepended."""
        # Setup
        mock_cmd.run_command.return_value = Mock(
            success=True,
            stdout="https://github.com/test/repo/pull/456",
            stderr="",
            returncode=0,
        )

        mock_github_client = Mock()
        mock_github_client.get_pr_closing_issues.return_value = [123]

        mock_message_backend = Mock()
        mock_message_backend._run_llm_cli.return_value = "Custom PR Title\n\nCustom PR body content"

        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "This is a test issue",
        }

        # Execute
        result = _create_pr_for_issue(
            repo_name="test/repo",
            issue_data=issue_data,
            work_branch="fix-issue-123",
            base_branch="main",
            llm_response="Fixed the issue",
            github_client=mock_github_client,
            message_backend_manager=mock_message_backend,
            dry_run=False,
        )

        # Assert
        assert "Successfully created PR" in result
        call_args = mock_cmd.run_command.call_args[0][0]
        body_index = call_args.index("--body")
        pr_body = call_args[body_index + 1]
        # Should have "Closes #123" at the beginning
        assert pr_body.startswith("Closes #123\n\n")
        assert "Custom PR body content" in pr_body

