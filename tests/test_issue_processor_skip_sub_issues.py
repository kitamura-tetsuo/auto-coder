"""
Tests for issue processor sub-issues skip logic.
"""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import (_process_issues_jules_mode,
                                            _process_issues_normal)


class TestIssueProcessorSkipSubIssues:
    """Test cases for skipping issues with open sub-issues."""

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_issues_normal_skips_issue_with_open_sub_issues(self, mock_logger):
        """Test that issues with open sub-issues are skipped in normal mode."""
        # Setup
        mock_github_client = Mock()
        config = AutomationConfig()
        config.max_issues_per_run = -1

        # Create mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_github_client.get_open_issues.return_value = [mock_issue]

        # Mock issue details
        mock_github_client.get_issue_details.return_value = {
            "number": 1,
            "title": "Parent issue",
            "body": "Depends on #100, #200",
            "labels": [],
        }

        # Mock get_open_sub_issues to return open sub-issues
        mock_github_client.get_open_sub_issues.return_value = [100, 200]

        # Execute
        result = _process_issues_normal(
            mock_github_client, config, dry_run=False, repo_name="owner/repo"
        )

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 1
        assert "Skipped - has open sub-issues: [100, 200]" in result[0]["actions_taken"]

        # Verify @auto-coder label was NOT added (skipped before processing)
        mock_github_client.try_add_work_in_progress_label.assert_not_called()
        # Verify @auto-coder label was NOT removed (never added)
        mock_github_client.remove_labels_from_issue.assert_not_called()

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_issues_normal_processes_issue_without_sub_issues(
        self, mock_logger
    ):
        """Test that issues without sub-issues are processed normally."""
        # Setup
        mock_github_client = Mock()
        config = AutomationConfig()
        config.max_issues_per_run = -1

        # Create mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_github_client.get_open_issues.return_value = [mock_issue]

        # Mock issue details
        mock_github_client.get_issue_details.return_value = {
            "number": 1,
            "title": "Regular issue",
            "body": "No dependencies",
            "labels": [],
        }

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock get_open_sub_issues to return empty list
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        result = _process_issues_normal(
            mock_github_client,
            config,
            dry_run=False,
            repo_name="owner/repo",
        )

        # Assert - issue should be processed (not skipped)
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 1
        # Should not have skip message
        assert not any(
            "Skipped - has open sub-issues" in action
            for action in result[0].get("actions_taken", [])
        )

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_issues_normal_processes_issue_with_closed_sub_issues(
        self, mock_logger
    ):
        """Test that issues with all closed sub-issues are processed."""
        # Setup
        mock_github_client = Mock()
        config = AutomationConfig()
        config.max_issues_per_run = -1

        # Create mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_github_client.get_open_issues.return_value = [mock_issue]

        # Mock issue details
        mock_github_client.get_issue_details.return_value = {
            "number": 1,
            "title": "Parent issue",
            "body": "Depends on #100, #200 (both closed)",
            "labels": [],
        }

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock get_open_sub_issues to return empty list (all closed)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False
        mock_github_client.has_linked_pr.return_value = False

        # Execute
        result = _process_issues_normal(
            mock_github_client,
            config,
            dry_run=False,
            repo_name="owner/repo",
        )

        # Assert - issue should be processed
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 1
        assert not any(
            "Skipped - has open sub-issues" in action
            for action in result[0].get("actions_taken", [])
        )

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_issues_jules_mode_skips_issue_with_open_sub_issues(
        self, mock_logger
    ):
        """Test that issues with open sub-issues are skipped in Jules mode."""
        # Setup
        mock_github_client = Mock()
        config = AutomationConfig()
        config.max_issues_per_run = -1

        # Create mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_github_client.get_open_issues.return_value = [mock_issue]

        # Mock issue details
        mock_github_client.get_issue_details.return_value = {
            "number": 1,
            "title": "Parent issue",
            "body": "Depends on #100",
            "labels": [],
        }

        # Mock get_open_sub_issues to return open sub-issues
        mock_github_client.get_open_sub_issues.return_value = [100]

        # Execute
        result = _process_issues_jules_mode(
            mock_github_client, config, dry_run=False, repo_name="owner/repo"
        )

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 1
        assert "Skipped - has open sub-issues: [100]" in result[0]["actions_taken"]

        # Verify @auto-coder label was NOT added (skipped before processing)
        mock_github_client.try_add_work_in_progress_label.assert_not_called()
        # Verify @auto-coder label was NOT removed (never added)
        mock_github_client.remove_labels_from_issue.assert_not_called()

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_issues_jules_mode_processes_issue_without_sub_issues(
        self, mock_logger
    ):
        """Test that issues without sub-issues get 'jules' label in Jules mode."""
        # Setup
        mock_github_client = Mock()
        config = AutomationConfig()
        config.max_issues_per_run = -1

        # Create mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_github_client.get_open_issues.return_value = [mock_issue]

        # Mock issue details
        mock_github_client.get_issue_details.return_value = {
            "number": 1,
            "title": "Regular issue",
            "body": "No dependencies",
            "labels": [],
        }

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock get_open_sub_issues to return empty list
        mock_github_client.get_open_sub_issues.return_value = []

        # Execute
        result = _process_issues_jules_mode(
            mock_github_client, config, dry_run=False, repo_name="owner/repo"
        )

        # Assert - issue should be processed
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 1
        # Should have added 'jules' label
        mock_github_client.add_labels_to_issue.assert_called_once_with(
            "owner/repo", 1, ["jules"]
        )

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_issues_normal_dry_run_skips_label_removal(self, mock_logger):
        """Test that dry run mode doesn't remove labels when skipping."""
        # Setup
        mock_github_client = Mock()
        config = AutomationConfig()
        config.max_issues_per_run = -1

        # Create mock issue
        mock_issue = Mock()
        mock_issue.number = 1
        mock_github_client.get_open_issues.return_value = [mock_issue]

        # Mock issue details
        mock_github_client.get_issue_details.return_value = {
            "number": 1,
            "title": "Parent issue",
            "body": "Depends on #100",
            "labels": [],
        }

        # Mock get_open_sub_issues to return open sub-issues
        mock_github_client.get_open_sub_issues.return_value = [100]

        # Execute in dry run mode
        result = _process_issues_normal(
            mock_github_client, config, dry_run=True, repo_name="owner/repo"
        )

        # Assert
        assert len(result) == 1
        assert "Skipped - has open sub-issues: [100]" in result[0]["actions_taken"]

        # Verify labels were NOT removed in dry run
        mock_github_client.remove_labels_from_issue.assert_not_called()
