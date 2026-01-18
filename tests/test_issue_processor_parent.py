"""Tests for parent issue processing functionality - simplified test cases."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _create_pr_for_parent_issue, _take_issue_actions


class TestParentIssueProcessing:
    """Test cases for parent issue processing as specified in issue #960."""

    def test_issue_with_open_sub_issues_skipped(self):
        """Test that an issue with open sub-issues is skipped (existing behavior).

        An issue should NOT be processed as a parent issue if it has open sub-issues.
        This verifies the existing behavior where parent processing is deferred.
        """
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue with Open Sub-Issues",
            "body": "Has sub-issues but some are still open",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues, no parent, but has OPEN sub-issues
        github_client.get_all_sub_issues.return_value = [101, 102, 103]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = [101, 102]  # Some are still open

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed issue with open sub-issues"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _apply_issue_actions_directly, NOT _process_parent_issue
            # This verifies the existing behavior is maintained
            mock_apply_actions.assert_called_once()
            assert "Processed issue with open sub-issues" in result

    def test_issue_with_closed_sub_issues_triggers_parent_processing(self):
        """Test that an issue with closed sub-issues and no parent triggers parent processing.

        An issue should be detected as a parent issue when:
        1. It has sub-issues
        2. It has no parent itself
        3. All sub-issues are closed
        """
        repo_name = "owner/repo"
        issue_number = 200
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue - All Sub-Issues Closed",
            "body": "All sub-issues are now closed",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues, no parent, ALL sub-issues closed
        github_client.get_all_sub_issues.return_value = [201, 202, 203]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []  # All closed

        with patch("src.auto_coder.issue_processor._create_pr_for_parent_issue") as mock_create_pr:
            mock_create_pr.return_value = "Successfully created PR for parent issue"

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _create_pr_for_parent_issue directly
            mock_create_pr.assert_called_once()
            assert "Successfully created PR for parent issue" in result

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.gh_logger.get_gh_logger")
    @patch("src.auto_coder.issue_processor.get_ghapi_client")
    def test_pr_creation_on_success_verification(self, mock_get_ghapi_client, mock_gh_logger, mock_cmd):
        """Test that PR is successfully created when verification succeeds.

        This test verifies the PR creation flow specifically when all conditions are met:
        1. Parent issue is detected
        2. All sub-issues are closed
        3. backend_for_noedit verification passes
        4. PR is created to document the completion
        """
        repo_name = "owner/repo"
        issue_number = 500
        issue_data = {
            "number": issue_number,
            "title": "Complete Feature Implementation",
            "body": "Parent issue for feature with multiple sub-tasks",
        }
        config = AutomationConfig()
        summary = "All sub-issues completed successfully"
        reasoning = "Both sub-issues merged, all requirements verified"

        # Mock GitHub client - needs to return issue 500 as closing issue
        github_client = MagicMock()
        github_client.token = "fake-token"
        github_client.get_pr_closing_issues.return_value = [500]  # PR is linked to issue 500
        github_client.find_pr_by_head_branch.return_value = None  # No existing PR

        # Mock GhApi client
        mock_api = MagicMock()
        mock_api.pulls.create.return_value = MagicMock(html_url="https://github.com/owner/repo/pull/500", number=500)
        mock_get_ghapi_client.return_value = mock_api

        # Mock git commands - branch exists (no changes to commit)
        mock_cmd.run_command.side_effect = [
            MagicMock(returncode=0, stdout=""),  # Branch check (exists)
            MagicMock(returncode=0, stdout=""),  # Switch to branch
            MagicMock(returncode=0, stdout=""),  # Git status (no changes)
            MagicMock(returncode=0),  # Test if completion file exists (doesn't)
        ]

        # Mock gh_logger - successful PR creation
        mock_gh_instance = MagicMock()
        mock_gh_instance.execute_with_logging.return_value = MagicMock(success=True, stdout="https://github.com/owner/repo/pull/500")
        mock_gh_logger.return_value = mock_gh_instance

        result = _create_pr_for_parent_issue(repo_name, issue_data, github_client, config, summary, reasoning)

        # Verify PR was created successfully
        assert "Successfully created PR for parent issue" in result
        assert str(issue_number) in result
        assert "Complete parent issue #500: Complete Feature Implementation" in result

        # Verify the PR linkage check was called
        github_client.get_pr_closing_issues.assert_called_once()


class TestParentIssueEdgeCases:
    """Additional edge case tests for parent issue processing."""

    def test_issue_with_no_sub_issues_not_processed_as_parent(self):
        """Test that an issue with no sub-issues is not processed as parent."""
        repo_name = "owner/repo"
        issue_number = 600
        issue_data = {
            "number": issue_number,
            "title": "Regular Issue without Sub-Issues",
            "body": "No sub-issues defined",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # No sub-issues
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed regular issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should be processed as regular issue, not parent
            mock_apply_actions.assert_called_once()
            assert "Processed regular issue" in result

    def test_child_issue_not_processed_as_parent(self):
        """Test that a child issue (has parent) is not processed as parent."""
        repo_name = "owner/repo"
        issue_number = 700
        issue_data = {
            "number": issue_number,
            "title": "Child Issue",
            "body": "This is a sub-issue",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues but also has a parent
        github_client.get_all_sub_issues.return_value = [701]
        github_client.get_parent_issue_details.return_value = {"number": 699, "title": "Parent Issue"}
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed child issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should be processed as regular issue, not parent
            mock_apply_actions.assert_called_once()
            assert "Processed child issue" in result
