"""Tests for parent issue processing functionality - simplified test cases."""

from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _take_issue_actions


class TestParentIssueProcessing:
    """Test cases for parent issue processing."""

    def test_issue_with_open_sub_issues_handled(self):
        """Test that an issue with open sub-issues is processed via _apply_issue_actions_directly."""
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

            mock_apply_actions.assert_called_once()
            assert "Processed issue with open sub-issues" in result

    def test_issue_with_closed_sub_issues_triggers_direct_processing(self):
        """Test that a parent issue with all sub-issues closed triggers _apply_issue_actions_directly.

        Sub-issues are merged directly into main. When processing the parent issue,
        it should be analyzed and verified via _apply_issue_actions_directly.
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

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed parent issue verification"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_apply_actions.assert_called_once_with(
                repo_name,
                issue_data,
                config,
                github_client,
                backend_manager=None,
            )
            assert "Processed parent issue verification" in result


class TestParentIssueEdgeCases:
    """Additional edge case tests for parent issue processing."""

    def test_issue_with_no_sub_issues_not_processed_as_parent(self):
        """Test that an issue with no sub-issues is processed as regular issue."""
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

            mock_apply_actions.assert_called_once()
            assert "Processed child issue" in result
