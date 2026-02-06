from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _create_pr_analysis_prompt


class TestPRPromptGeneration:
    @patch("src.auto_coder.pr_processor.get_commit_log")
    def test_link_issue_context_injection(self, mock_get_commit_log):
        # Setup
        config = AutomationConfig()
        repo_name = "test/repo"
        pr_data = {
            "number": 100,
            "title": "Fix bug",
            "body": "This fixes #123",
            "user": {"login": "tester"},
            "state": "open",
            "labels": [],
        }
        pr_diff = "diff content"

        # Mock GitHubClient
        mock_client = Mock()

        # Mock Issue #123
        mock_issue = Mock()
        mock_issue.title = "Bug in login"
        mock_issue.body = "Login fails with 500 error"
        mock_client.get_issue.return_value = mock_issue

        # Mock Parent Issue #456 for #123
        mock_client.get_parent_issue_details.return_value = {"number": 456, "title": "Epic: Auth Refactor"}
        mock_client.get_parent_issue_body.return_value = "Refactor the entire auth system"

        # Mock get_commit_log to prevent git command execution
        mock_get_commit_log.return_value = "No commits"

        # Execute
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config, mock_client)

        # Assert
        assert "Linked Issues Context:" in prompt
        assert "Linked Issue #123: Bug in login" in prompt
        assert "Issue Description:\nLogin fails with 500 error" in prompt
        assert "Parent Issue #456 (of #123): Epic: Auth Refactor" in prompt
        assert "Parent Issue Description:\nRefactor the entire auth system" in prompt

    @patch("src.auto_coder.pr_processor.get_commit_log")
    def test_no_linked_issues(self, mock_get_commit_log):
        # Setup
        config = AutomationConfig()
        repo_name = "test/repo"
        pr_data = {
            "number": 101,
            "title": "Refactor",
            "body": "Just a refactor",
            "user": {"login": "tester"},
            "state": "open",
            "labels": [],
        }
        pr_diff = "diff content"

        mock_client = Mock()

        # Mock get_commit_log to prevent git command execution
        mock_get_commit_log.return_value = "No commits"

        # Execute
        prompt = _create_pr_analysis_prompt(repo_name, pr_data, pr_diff, config, mock_client)

        # Assert
        assert "Linked Issues Context:" not in prompt
