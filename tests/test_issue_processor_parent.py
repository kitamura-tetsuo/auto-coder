"""Tests for parent issue processing functionality - simplified test cases."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _create_pr_for_parent_issue, _process_parent_issue, _take_issue_actions


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

        with patch("src.auto_coder.issue_processor._process_parent_issue") as mock_process_parent:
            mock_process_parent.return_value = ["Processed parent issue successfully"]

            result = _take_issue_actions(repo_name, issue_data, config, github_client)

            # Should call _process_parent_issue to verify and potentially close the parent issue
            mock_process_parent.assert_called_once_with(repo_name, issue_data, config, github_client)
            assert "Processed parent issue successfully" in result

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    @patch("src.auto_coder.issue_processor._create_pr_for_parent_issue")
    def test_backend_for_noedit_response_success_creates_pr(self, mock_create_pr, mock_run_llm):
        """Test that backend_for_noedit success response triggers PR creation.

        When the backend_for_noedit (verification LLM) responds with requirements_met=true,
        the system should create a PR and close the parent issue.
        """
        repo_name = "owner/repo"
        issue_number = 300
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue - Verification Success",
            "body": "Test parent issue for verification",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [301, 302]

        # Mock repository and sub-issues
        mock_repo = MagicMock()
        github_client.get_repository.return_value = mock_repo

        sub_issue_301 = MagicMock()
        sub_issue_301.number = 301
        sub_issue_301.title = "Sub-task 1"
        sub_issue_301.state = "closed"
        sub_issue_301.body = "Implementation complete"
        sub_issue_301.html_url = "https://github.com/owner/repo/issues/301"

        sub_issue_302 = MagicMock()
        sub_issue_302.number = 302
        sub_issue_302.title = "Sub-task 2"
        sub_issue_302.state = "closed"
        sub_issue_302.body = "Tests passing"
        sub_issue_302.html_url = "https://github.com/owner/repo/issues/302"

        mock_repo.get_issue.side_effect = lambda n: {301: sub_issue_301, 302: sub_issue_302}[n]

        # Mock PRs (none in this case)
        github_client.get_open_pull_requests.return_value = []
        github_client.get_pr_closing_issues.return_value = []

        # Mock backend_for_noedit response - SUCCESS
        mock_run_llm.return_value = """```json
{
    "requirements_met": true,
    "summary": "All sub-issues are closed and requirements are satisfied",
    "reasoning": "Both sub-issues are completed with proper implementation and testing",
    "recommendation": "close_issue"
}
```"""

        # Mock PR creation success
        mock_create_pr.return_value = "Successfully created PR for parent issue #300"

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Verify backend_for_noedit was called
        assert mock_run_llm.called, "backend_for_noedit should be invoked for verification"
        verification_prompt = mock_run_llm.call_args[0][0]
        assert f"Parent Issue\n**Number:** {issue_number}" in verification_prompt
        assert "Sub-task 1" in verification_prompt
        assert "Sub-task 2" in verification_prompt

        # Verify PR creation was triggered
        mock_create_pr.assert_called_once()

        # Verify issue was closed
        github_client.close_issue.assert_called_once()

        # Verify success actions were returned
        assert len(result) >= 3
        assert f"Verified parent issue #{issue_number}" in result[0]
        assert "Successfully created PR for parent issue #300" in result[1]
        assert f"Closed parent issue #{issue_number}" in result[2]

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    def test_backend_for_noedit_response_failure_keeps_issue_open(self, mock_run_llm):
        """Test that backend_for_noedit failure response keeps issue open.

        When the backend_for_noedit (verification LLM) responds with requirements_met=false,
        the system should keep the parent issue open without creating a PR.
        """
        repo_name = "owner/repo"
        issue_number = 400
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue - Verification Failed",
            "body": "Test parent issue for failed verification",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [401]

        # Mock repository and sub-issues
        mock_repo = MagicMock()
        github_client.get_repository.return_value = mock_repo

        sub_issue_401 = MagicMock()
        sub_issue_401.number = 401
        sub_issue_401.title = "Sub-task 1"
        sub_issue_401.state = "closed"
        sub_issue_401.body = "Partial implementation only"
        sub_issue_401.html_url = "https://github.com/owner/repo/issues/401"

        mock_repo.get_issue.return_value = sub_issue_401

        # Mock PRs (none)
        github_client.get_open_pull_requests.return_value = []
        github_client.get_pr_closing_issues.return_value = []

        # Mock backend_for_noedit response - FAILURE
        mock_run_llm.return_value = """```json
{
    "requirements_met": false,
    "summary": "Implementation incomplete",
    "reasoning": "Only basic implementation present, missing critical functionality and tests",
    "recommendation": "keep_open"
}
```"""

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Verify backend_for_noedit was called
        assert mock_run_llm.called, "backend_for_noedit should be invoked for verification"

        # Verify PR creation was NOT triggered
        # (check that close_issue was not called, which happens after PR creation)
        github_client.close_issue.assert_not_called()

        # Verify verification message was returned
        assert len(result) >= 1
        assert f"Verified parent issue #{issue_number}" in result[0]
        # When requirements are not met, a separate message indicates the issue is kept open
        assert len(result) >= 2, f"Expected at least 2 actions, got {len(result)}: {result}"
        assert f"kept open" in result[1].lower() or "not fully met" in result[1].lower()

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.get_gh_logger")
    def test_pr_creation_on_success_verification(self, mock_gh_logger, mock_cmd):
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
        github_client.get_pr_closing_issues.return_value = [500]  # PR is linked to issue 500

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
