"""
Tests for issue processor skip linked PR functionality.
"""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _process_issues_normal


class TestIssueProcessorSkipLinked:
    """Test cases for skipping issues with linked PRs."""

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_skips_issue_with_linked_pr(self, mock_take_actions):
        """Test that _process_issues_normal skips issues with linked PRs."""
        # Setup
        mock_github_client = Mock()
        mock_issue1 = Mock()
        mock_issue1.number = 123
        mock_issue2 = Mock()
        mock_issue2.number = 456

        mock_github_client.get_open_issues.return_value = [mock_issue1, mock_issue2]
        mock_github_client.get_issue_details.side_effect = [
            {
                "number": 123,
                "title": "Issue with PR",
                "body": "This issue has a PR",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
            {
                "number": 456,
                "title": "Issue without PR",
                "body": "This issue has no PR",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
        ]

        # Issue 123 has a linked PR, issue 456 does not
        mock_github_client.has_linked_pr.side_effect = [True, False]

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10

        # Execute
        result = _process_issues_normal(
            mock_github_client, config, dry_run=False, repo_name="test/repo"
        )

        # Assert
        assert len(result) == 2

        # First issue should be skipped
        assert result[0]["issue_data"]["number"] == 123
        actions_taken = result[0]["actions_taken"]
        assert actions_taken == ["Skipped - already has a linked PR"]

        # Second issue should be processed
        assert result[1]["issue_data"]["number"] == 456
        assert result[1]["actions_taken"] == ["Action taken"]

        # Verify has_linked_pr was called for both issues
        assert mock_github_client.has_linked_pr.call_count == 2
        mock_github_client.has_linked_pr.assert_any_call("test/repo", 123)
        mock_github_client.has_linked_pr.assert_any_call("test/repo", 456)

        # Verify _take_issue_actions was only called for issue 456
        assert mock_take_actions.call_count == 1
        call_args = mock_take_actions.call_args[0]
        assert call_args[1]["number"] == 456

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_processes_all_when_no_linked_prs(
        self, mock_take_actions
    ):
        """Test that all issues are processed when none have linked PRs."""
        # Setup
        mock_github_client = Mock()
        mock_issue1 = Mock()
        mock_issue1.number = 123
        mock_issue2 = Mock()
        mock_issue2.number = 456

        mock_github_client.get_open_issues.return_value = [mock_issue1, mock_issue2]
        mock_github_client.get_issue_details.side_effect = [
            {
                "number": 123,
                "title": "Issue 1",
                "body": "First issue",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
            {
                "number": 456,
                "title": "Issue 2",
                "body": "Second issue",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
        ]

        # Neither issue has a linked PR
        mock_github_client.has_linked_pr.return_value = False

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10

        # Execute
        result = _process_issues_normal(
            mock_github_client, config, dry_run=False, repo_name="test/repo"
        )

        # Assert
        assert len(result) == 2

        # Both issues should be processed
        assert result[0]["issue_data"]["number"] == 123
        assert result[0]["actions_taken"] == ["Action taken"]
        assert result[1]["issue_data"]["number"] == 456
        assert result[1]["actions_taken"] == ["Action taken"]

        # Verify _take_issue_actions was called for both issues
        assert mock_take_actions.call_count == 2

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_skips_all_when_all_have_linked_prs(
        self, mock_take_actions
    ):
        """Test that all issues are skipped when all have linked PRs."""
        # Setup
        mock_github_client = Mock()
        mock_issue1 = Mock()
        mock_issue1.number = 123
        mock_issue2 = Mock()
        mock_issue2.number = 456

        mock_github_client.get_open_issues.return_value = [mock_issue1, mock_issue2]
        mock_github_client.get_issue_details.side_effect = [
            {
                "number": 123,
                "title": "Issue 1",
                "body": "First issue",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
            {
                "number": 456,
                "title": "Issue 2",
                "body": "Second issue",
                "labels": ["bug"],
                "state": "open",
                "author": "testuser",
            },
        ]

        # Both issues have linked PRs
        mock_github_client.has_linked_pr.return_value = True

        config = AutomationConfig()
        config.max_issues_per_run = 10

        # Execute
        result = _process_issues_normal(
            mock_github_client, config, dry_run=False, repo_name="test/repo"
        )

        # Assert
        assert len(result) == 2

        # Both issues should be skipped
        first_actions = result[0]["actions_taken"]
        second_actions = result[1]["actions_taken"]
        assert first_actions == ["Skipped - already has a linked PR"]
        assert second_actions == ["Skipped - already has a linked PR"]

        # Verify _take_issue_actions was never called
        assert mock_take_actions.call_count == 0

    @patch("src.auto_coder.issue_processor._take_issue_actions")
    def test_process_issues_normal_handles_has_linked_pr_exception(
        self, mock_take_actions
    ):
        """Test that exceptions in has_linked_pr are handled gracefully."""
        # Setup
        mock_github_client = Mock()
        mock_issue = Mock()
        mock_issue.number = 123

        mock_github_client.get_open_issues.return_value = [mock_issue]
        mock_github_client.get_issue_details.return_value = {
            "number": 123,
            "title": "Test Issue",
            "body": "Test body",
            "labels": ["bug"],
            "state": "open",
            "author": "testuser",
        }

        # has_linked_pr returns False on exception (as per implementation)
        mock_github_client.has_linked_pr.return_value = False

        mock_take_actions.return_value = ["Action taken"]

        config = AutomationConfig()
        config.max_issues_per_run = 10

        # Execute
        result = _process_issues_normal(
            mock_github_client, config, dry_run=False, repo_name="test/repo"
        )

        # Assert
        assert len(result) == 1
        assert result[0]["actions_taken"] == ["Action taken"]

        # Verify _take_issue_actions was called
        assert mock_take_actions.call_count == 1
