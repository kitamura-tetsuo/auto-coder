"""Tests for parent issue processing functionality."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _process_parent_issue, _take_issue_actions


class TestParentIssueDetection:
    """Tests for parent issue detection and branching logic."""

    def test_regular_issue_not_detected_as_parent(self):
        """Test that a regular issue without sub-issues is not detected as a parent issue."""
        repo_name = "owner/repo"
        issue_number = 123
        issue_data = {"number": issue_number, "title": "Regular Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # No sub-issues, no parent
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _apply_issue_actions_directly, not _process_parent_issue
            mock_apply_actions.assert_called_once()
            assert "Processed issue" in result

    def test_issue_with_open_sub_issues_not_detected_as_parent(self):
        """Test that an issue with open sub-issues is not detected as a parent issue."""
        repo_name = "owner/repo"
        issue_number = 456
        issue_data = {"number": issue_number, "title": "Parent with Open Sub-Issues"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues, no parent, but has open sub-issues
        github_client.get_all_sub_issues.return_value = [101, 102, 103]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = [101, 102]  # Some open

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed issue with open sub-issues"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _apply_issue_actions_directly, not _process_parent_issue
            mock_apply_actions.assert_called_once()
            assert "Processed issue with open sub-issues" in result

    def test_child_issue_not_detected_as_parent(self):
        """Test that a child issue (has parent) is not detected as a parent issue."""
        repo_name = "owner/repo"
        issue_number = 789
        issue_data = {"number": issue_number, "title": "Child Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # No sub-issues, but has a parent
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = {"number": 100, "title": "Parent Issue"}
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed child issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _apply_issue_actions_directly, not _process_parent_issue
            mock_apply_actions.assert_called_once()
            assert "Processed child issue" in result

    def test_parent_issue_detected_correctly(self):
        """Test that a parent issue with all sub-issues closed is correctly detected."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Has sub-issues, no parent, all sub-issues closed
        github_client.get_all_sub_issues.return_value = [101, 102, 103, 104]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []  # All closed

        with patch("src.auto_coder.issue_processor._process_parent_issue") as mock_process_parent:
            mock_process_parent.return_value = ["Processed parent issue"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _process_parent_issue, not _apply_issue_actions_directly
            mock_process_parent.assert_called_once_with(repo_name, issue_data, config, github_client)
            assert "Processed parent issue" in result

    def test_parent_issue_with_only_closed_sub_issues_detected(self):
        """Test that a parent issue with only closed sub-issues is detected."""
        repo_name = "owner/repo"
        issue_number = 200
        issue_data = {"number": issue_number, "title": "Parent with All Closed"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [201, 202]
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._process_parent_issue") as mock_process_parent:
            mock_process_parent.return_value = ["Processed parent with all closed sub-issues"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            mock_process_parent.assert_called_once()
            assert "Processed parent with all closed sub-issues" in result

    def test_github_api_errors_handled_gracefully(self):
        """Test that GitHub API errors are handled gracefully."""
        repo_name = "owner/repo"
        issue_number = 300
        issue_data = {"number": issue_number, "title": "Issue with API Error"}
        config = AutomationConfig()

        # Mock GitHub client that raises an error
        github_client = MagicMock()
        github_client.get_all_sub_issues.side_effect = Exception("GitHub API error")

        result = _take_issue_actions(repo_name, issue_data, config, github_client)

        # Should handle the error gracefully
        assert len(result) > 0
        assert f"Error processing issue #{issue_number}" in result[0]


class TestProcessParentIssue:
    """Tests for _process_parent_issue function."""

    def test_process_parent_issue_stub(self):
        """Test that _process_parent_issue stub function works correctly."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should return a success action
        assert len(result) == 1
        assert f"Processed parent issue #{issue_number}" in result[0]

    def test_process_parent_issue_logs_correctly(self):
        """Test that _process_parent_issue logs correctly during processing."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()

        with patch("src.auto_coder.issue_processor.logger") as mock_logger:
            result = _process_parent_issue(repo_name, issue_data, config, github_client)

            # Should log info messages
            assert mock_logger.info.call_count >= 2
            # First log should be about processing the parent issue
            first_call_args = mock_logger.info.call_args_list[0][0]
            assert f"Processing parent issue #{issue_number}" in first_call_args[0]

            # Should return a success action
            assert len(result) == 1
            assert f"Processed parent issue #{issue_number}" in result[0]


class TestParentIssueBranchingIntegration:
    """Integration tests for parent issue detection and branching."""

    def test_detection_logic_with_multiple_conditions(self):
        """Test the complete detection logic with all conditions."""
        repo_name = "owner/repo"
        config = AutomationConfig()

        test_cases = [
            # (has_sub_issues, has_parent, open_sub_issues_count, should_be_parent)
            ([101, 102], None, 0, True),  # Has sub-issues, no parent, all closed -> Parent
            ([101, 102], None, 1, False),  # Has sub-issues, no parent, some open -> Not parent
            ([], None, 0, False),  # No sub-issues -> Not parent
            ([101, 102], {"number": 100}, 0, False),  # Has parent -> Not parent
            ([101, 102], {"number": 100}, 1, False),  # Has parent and open sub-issues -> Not parent
            ([], {"number": 100}, 0, False),  # Has parent, no sub-issues -> Not parent
        ]

        for sub_issues, parent, open_count, should_be_parent in test_cases:
            issue_number = 100
            issue_data = {"number": issue_number, "title": "Test Issue"}

            github_client = MagicMock()
            github_client.get_all_sub_issues.return_value = sub_issues
            github_client.get_parent_issue_details.return_value = parent
            github_client.get_open_sub_issues.return_value = list(range(open_count))

            with patch("src.auto_coder.issue_processor._process_parent_issue") as mock_process_parent, patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
                mock_process_parent.return_value = ["Parent processed"]
                mock_apply_actions.return_value = ["Issue processed"]

                result = _take_issue_actions(repo_name, issue_data, config, github_client)

                if should_be_parent:
                    mock_process_parent.assert_called_once()
                    mock_apply_actions.assert_not_called()
                    assert "Parent processed" in result
                else:
                    mock_apply_actions.assert_called_once()
                    mock_process_parent.assert_not_called()
                    assert "Issue processed" in result

    def test_empty_sub_issues_list_treated_as_no_sub_issues(self):
        """Test that an empty sub-issues list means no sub-issues."""
        repo_name = "owner/repo"
        issue_number = 150
        issue_data = {"number": issue_number, "title": "Issue with No Sub-Issues"}
        config = AutomationConfig()

        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = []
        github_client.get_parent_issue_details.return_value = None
        github_client.get_open_sub_issues.return_value = []

        with patch("src.auto_coder.issue_processor._apply_issue_actions_directly") as mock_apply_actions:
            mock_apply_actions.return_value = ["Processed"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should not be detected as parent (no sub-issues)
            mock_apply_actions.assert_called_once()
            assert "Processed" in result
