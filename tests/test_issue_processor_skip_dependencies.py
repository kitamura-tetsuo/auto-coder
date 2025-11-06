"""
Tests for issue processor skip dependencies functionality.
"""

from unittest.mock import Mock, patch

import pytest
from github.GithubException import GithubException

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _process_issues_normal


class TestIssueProcessorSkipDependencies:
    """Test cases for skipping issues with unresolved dependencies."""

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_skips_unresolved_dependencies(self, mock_take_actions):
        """Test that _process_issues_normal skips issues with unresolved dependencies."""
        # Setup
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_issue1 = Mock()
        mock_issue1.number = 123
        mock_issue2 = Mock()
        mock_issue2.number = 456

        mock_github_client.get_open_issues.return_value = [mock_issue1, mock_issue2]
        mock_github_client.get_issue_details.side_effect = [
            {
                "number": 123,
                "title": "Issue with dependency",
                "body": "This issue depends on #100\nDepends on: #100",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
            {
                "number": 456,
                "title": "Issue without dependencies",
                "body": "This issue has no dependencies",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
        ]

        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False (no linked PRs)
        mock_github_client.has_linked_pr.return_value = False

        # Mock get_issue_dependencies to return dependencies
        mock_github_client.get_issue_dependencies.side_effect = [
            [100],  # Issue 123 depends on issue 100
            [],  # Issue 456 has no dependencies
        ]

        # Mock check_issue_dependencies_resolved to return unresolved dependencies
        # Issue 100 is still open (unresolved)
        mock_github_client.check_issue_dependencies_resolved.return_value = [100]

        # Mock try_add_work_in_progress_label to succeed for issue 456
        mock_github_client.try_add_work_in_progress_label.return_value = True

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10
        config.CHECK_DEPENDENCIES = True

        # Execute
        result = _process_issues_normal(mock_github_client, config, dry_run=False, repo_name="test/repo")

        # Assert
        assert len(result) == 2

        # First issue should be skipped (has unresolved dependency)
        assert result[0]["issue_data"]["number"] == 123
        actions_taken = result[0]["actions_taken"]
        assert actions_taken == ["Skipped - has unresolved dependencies: [100]"]

        # Second issue should be processed
        assert result[1]["issue_data"]["number"] == 456
        assert result[1]["actions_taken"] == ["Action taken"]

        # Verify get_issue_dependencies was called for both issues
        assert mock_github_client.get_issue_dependencies.call_count == 2
        mock_github_client.get_issue_dependencies.assert_any_call("This issue depends on #100\nDepends on: #100")
        mock_github_client.get_issue_dependencies.assert_any_call("This issue has no dependencies")

        # Verify check_issue_dependencies_resolved was only called for issue 123 (which has dependencies)
        assert mock_github_client.check_issue_dependencies_resolved.call_count == 1
        mock_github_client.check_issue_dependencies_resolved.assert_called_once_with("test/repo", [100])

        # Verify @auto-coder label was only added for issue 456
        mock_github_client.try_add_work_in_progress_label.assert_called_once_with("test/repo", 456, label="@auto-coder")

        # Verify _take_issue_actions was only called for issue 456
        assert mock_take_actions.call_count == 1
        call_args = mock_take_actions.call_args[0]
        assert call_args[1]["number"] == 456

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_processes_when_dependencies_resolved(self, mock_take_actions):
        """Test that issues are processed when all dependencies are resolved."""
        # Setup
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Issue with resolved dependency",
            "body": "This issue depends on #100\nDepends on: #100",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False (no linked PRs)
        mock_github_client.has_linked_pr.return_value = False

        # Mock get_issue_dependencies to return dependencies
        mock_github_client.get_issue_dependencies.return_value = [100]

        # Mock check_issue_dependencies_resolved to return empty list (all resolved)
        mock_github_client.check_issue_dependencies_resolved.return_value = []

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10
        config.CHECK_DEPENDENCIES = True

        # Execute
        result = _process_issues_normal(mock_github_client, config, dry_run=False, repo_name="test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 123
        assert result[0]["actions_taken"] == ["Action taken"]

        # Verify check_issue_dependencies_resolved was called
        assert mock_github_client.check_issue_dependencies_resolved.call_count == 1
        mock_github_client.check_issue_dependencies_resolved.assert_called_once_with("test/repo", [100])

        # Verify @auto-coder label was added
        mock_github_client.try_add_work_in_progress_label.assert_called_once_with("test/repo", 123, label="@auto-coder")

        # Verify _take_issue_actions was called
        assert mock_take_actions.call_count == 1

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_skips_dependency_check_when_disabled(self, mock_take_actions):
        """Test that dependency check is skipped when CHECK_DEPENDENCIES is False."""
        # Setup
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Issue with dependency",
            "body": "This issue depends on #100\nDepends on: #100",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False (no linked PRs)
        mock_github_client.has_linked_pr.return_value = False

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10
        config.CHECK_DEPENDENCIES = False  # Disabled

        # Execute
        result = _process_issues_normal(mock_github_client, config, dry_run=False, repo_name="test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 123
        assert result[0]["actions_taken"] == ["Action taken"]

        # Verify get_issue_dependencies was NOT called
        mock_github_client.get_issue_dependencies.assert_not_called()

        # Verify check_issue_dependencies_resolved was NOT called
        mock_github_client.check_issue_dependencies_resolved.assert_not_called()

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_handles_missing_dependency_issue(self, mock_take_actions):
        """Test that missing dependency issues are treated as unresolved."""
        # Setup
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Issue with missing dependency",
            "body": "This issue depends on #99999\nDepends on: #99999",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False (no linked PRs)
        mock_github_client.has_linked_pr.return_value = False

        # Mock get_issue_dependencies to return dependencies
        mock_github_client.get_issue_dependencies.return_value = [99999]

        # Mock check_issue_dependencies_resolved to return unresolved (issue doesn't exist, so consider it unresolved)
        mock_github_client.check_issue_dependencies_resolved.return_value = [99999]

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        config = AutomationConfig()
        config.max_issues_per_run = 10
        config.CHECK_DEPENDENCIES = True

        # Execute - the issue should be skipped due to unresolved dependencies
        result = _process_issues_normal(mock_github_client, config, dry_run=False, repo_name="test/repo")

        # Verify the issue was skipped
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 123
        assert "unresolved dependencies" in result[0]["actions_taken"][0]
        assert "99999" in result[0]["actions_taken"][0]

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_handles_multiple_dependencies(self, mock_take_actions):
        """Test that multiple dependencies are properly checked."""
        # Setup
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Issue with multiple dependencies",
            "body": "Depends on #100\nDepends on #200\nAlso blocked by #300",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock has_linked_pr to return False (no linked PRs)
        mock_github_client.has_linked_pr.return_value = False

        # Mock get_issue_dependencies to return multiple dependencies
        mock_github_client.get_issue_dependencies.return_value = [100, 200, 300]

        # Mock check_issue_dependencies_resolved to return unresolved dependencies
        # Issues 100 and 200 are resolved, but 300 is not
        mock_github_client.check_issue_dependencies_resolved.return_value = [300]

        # Mock try_add_work_in_progress_label to succeed
        mock_github_client.try_add_work_in_progress_label.return_value = True

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10
        config.CHECK_DEPENDENCIES = True

        # Execute
        result = _process_issues_normal(mock_github_client, config, dry_run=False, repo_name="test/repo")

        # Assert
        assert len(result) == 1
        assert result[0]["issue_data"]["number"] == 123
        assert result[0]["actions_taken"] == ["Skipped - has unresolved dependencies: [300]"]

        # Verify all dependencies were checked
        assert mock_github_client.check_issue_dependencies_resolved.call_count == 1
        mock_github_client.check_issue_dependencies_resolved.assert_called_once_with("test/repo", [100, 200, 300])

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_jules_mode_skips_unresolved_dependencies(self, mock_take_actions):
        """Test that jules mode also skips issues with unresolved dependencies."""
        from src.auto_coder.issue_processor import _process_issues_jules_mode

        # Setup
        mock_github_client = Mock()
        mock_github_client.disable_labels = False
        mock_issue1 = Mock()
        mock_issue1.number = 123
        mock_issue2 = Mock()
        mock_issue2.number = 456

        mock_github_client.get_open_issues.return_value = [mock_issue1, mock_issue2]
        mock_github_client.get_issue_details.side_effect = [
            {
                "number": 123,
                "title": "Issue with dependency",
                "body": "Depends on #100",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
            {
                "number": 456,
                "title": "Issue without dependencies",
                "body": "No dependencies",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
        ]

        # Mock get_open_sub_issues to return empty list (no sub-issues)
        mock_github_client.get_open_sub_issues.return_value = []

        # Mock get_issue_dependencies
        mock_github_client.get_issue_dependencies.side_effect = [[100], []]

        # Mock check_issue_dependencies_resolved
        mock_github_client.check_issue_dependencies_resolved.return_value = [100]

        # Mock try_add_work_in_progress_label
        mock_github_client.try_add_work_in_progress_label.return_value = True

        # Mock add_labels_to_issue for jules mode
        mock_github_client.add_labels_to_issue = Mock()

        config = AutomationConfig()
        config.max_issues_per_run = 10
        config.CHECK_DEPENDENCIES = True

        # Execute
        result = _process_issues_jules_mode(mock_github_client, config, dry_run=False, repo_name="test/repo")

        # Assert
        assert len(result) == 2

        # First issue should be skipped (has unresolved dependency)
        assert result[0]["issue_data"]["number"] == 123
        assert result[0]["actions_taken"] == ["Skipped - has unresolved dependencies: [100]"]

        # Second issue should have jules label added
        assert result[1]["issue_data"]["number"] == 456
        assert result[1]["actions_taken"] == ["Added 'jules' label to issue #456"]
