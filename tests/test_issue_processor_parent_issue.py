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

    def test_process_parent_issue_with_no_sub_issues(self):
        """Test that _process_parent_issue handles parent issues with no sub-issues."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue", "body": "No sub-issues"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = []

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should return warning about no sub-issues
        assert len(result) == 1
        assert f"Parent issue #{issue_number} has no sub-issues" in result[0]
        github_client.get_all_sub_issues.assert_called_once_with(repo_name, issue_number)

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    def test_process_parent_issue_verification_closes_issue(self, mock_run_llm):
        """Test that _process_parent_issue closes issue when requirements are met."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Implement feature with sub-tasks",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [101, 102]

        # Mock repository and sub-issues
        mock_repo = MagicMock()
        github_client.get_repository.return_value = mock_repo

        sub_issue_101 = MagicMock()
        sub_issue_101.number = 101
        sub_issue_101.title = "Sub-task 1"
        sub_issue_101.state = "closed"
        sub_issue_101.body = "Implementation complete"
        sub_issue_101.html_url = "https://github.com/owner/repo/issues/101"

        sub_issue_102 = MagicMock()
        sub_issue_102.number = 102
        sub_issue_102.title = "Sub-task 2"
        sub_issue_102.state = "closed"
        sub_issue_102.body = "Tests passing"
        sub_issue_102.html_url = "https://github.com/owner/repo/issues/102"

        mock_repo.get_issue.side_effect = lambda n: {101: sub_issue_101, 102: sub_issue_102}[n]

        # Mock PRs (no PRs for simplicity)
        github_client.get_open_pull_requests.return_value = []
        github_client.get_pr_closing_issues.return_value = []

        # Mock LLM response - requirements met
        mock_run_llm.return_value = """```json
{
    "requirements_met": true,
    "summary": "All sub-issues are closed and requirements are satisfied",
    "reasoning": "Both sub-issues are closed with proper implementation",
    "recommendation": "close_issue"
}
```"""

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should verify requirements
        assert mock_run_llm.called
        verification_prompt = mock_run_llm.call_args[0][0]
        assert f"Parent Issue\n**Number:** {issue_number}" in verification_prompt

        # Should close the issue
        github_client.close_issue.assert_called_once()
        close_call = github_client.close_issue.call_args
        assert close_call[0][0] == repo_name
        assert close_call[0][1] == issue_number
        assert "Auto-Coder Verification" in close_call[0][2]

        # Should return success actions
        assert len(result) >= 2
        assert f"Verified parent issue #{issue_number}" in result[0]
        assert f"Closed parent issue #{issue_number}" in result[1]

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    def test_process_parent_issue_verification_keeps_open(self, mock_run_llm):
        """Test that _process_parent_issue keeps issue open when requirements not met."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Implement feature with sub-tasks",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [101]

        # Mock repository and sub-issues
        mock_repo = MagicMock()
        github_client.get_repository.return_value = mock_repo

        sub_issue_101 = MagicMock()
        sub_issue_101.number = 101
        sub_issue_101.title = "Sub-task 1"
        sub_issue_101.state = "closed"
        sub_issue_101.body = "Partial implementation"
        sub_issue_101.html_url = "https://github.com/owner/repo/issues/101"

        mock_repo.get_issue.return_value = sub_issue_101

        # Mock PRs
        github_client.get_open_pull_requests.return_value = []
        github_client.get_pr_closing_issues.return_value = []

        # Mock LLM response - requirements not met
        mock_run_llm.return_value = """```json
{
    "requirements_met": false,
    "summary": "Implementation incomplete",
    "reasoning": "Only one sub-issue completed, missing critical functionality",
    "recommendation": "keep_open"
}
```"""

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should verify requirements
        assert mock_run_llm.called

        # Should NOT close the issue
        github_client.close_issue.assert_not_called()

        # Should return verification message
        assert len(result) >= 1
        assert f"Verified parent issue #{issue_number}" in result[0]

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    def test_process_parent_issue_invalid_json_response(self, mock_run_llm):
        """Test that _process_parent_issue handles invalid LLM JSON response gracefully."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue", "body": "Test"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [101]

        # Mock repository and sub-issues
        mock_repo = MagicMock()
        github_client.get_repository.return_value = mock_repo

        sub_issue_101 = MagicMock()
        sub_issue_101.number = 101
        sub_issue_101.title = "Sub-task 1"
        sub_issue_101.state = "closed"
        sub_issue_101.body = "Test"
        sub_issue_101.html_url = "https://github.com/owner/repo/issues/101"

        mock_repo.get_issue.return_value = sub_issue_101

        # Mock PRs
        github_client.get_open_pull_requests.return_value = []
        github_client.get_pr_closing_issues.return_value = []

        # Mock LLM response - invalid JSON
        mock_run_llm.return_value = "This is not valid JSON"

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should handle invalid JSON gracefully
        assert len(result) >= 1
        assert "Warning: Could not parse verification response" in result[0]

        # Should NOT close the issue
        github_client.close_issue.assert_not_called()

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    def test_process_parent_issue_with_sub_issue_prs(self, mock_run_llm):
        """Test that _process_parent_issue correctly identifies PRs for sub-issues."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue", "body": "Test"}
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        github_client.get_all_sub_issues.return_value = [101]

        # Mock repository and sub-issues
        mock_repo = MagicMock()
        github_client.get_repository.return_value = mock_repo

        sub_issue_101 = MagicMock()
        sub_issue_101.number = 101
        sub_issue_101.title = "Sub-task 1"
        sub_issue_101.state = "closed"
        sub_issue_101.body = "Test"
        sub_issue_101.html_url = "https://github.com/owner/repo/issues/101"

        mock_repo.get_issue.return_value = sub_issue_101

        # Mock PR that closes the sub-issue
        mock_pr = MagicMock()
        mock_pr.number = 201
        mock_pr.state = "MERGED"
        mock_pr.mergeable = True
        github_client.get_open_pull_requests.return_value = [mock_pr]
        github_client.get_pr_closing_issues.return_value = [101]

        # Mock LLM response
        mock_run_llm.return_value = """```json
{
    "requirements_met": true,
    "summary": "PR is merged",
    "reasoning": "All PRs are merged",
    "recommendation": "close_issue"
}
```"""

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should include PR information in verification prompt
        assert mock_run_llm.called
        verification_prompt = mock_run_llm.call_args[0][0]
        assert "PRs Summary" in verification_prompt
        assert "Sub-issue #101 -> PR #201" in verification_prompt

    @patch("src.auto_coder.issue_processor.logger")
    def test_process_parent_issue_exception_handling(self, mock_logger):
        """Test that _process_parent_issue handles exceptions gracefully."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {"number": issue_number, "title": "Parent Issue", "body": "Test"}
        config = AutomationConfig()

        # Mock GitHub client that raises exception
        github_client = MagicMock()
        github_client.get_all_sub_issues.side_effect = Exception("API Error")

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should handle exception and return error action
        assert len(result) == 1
        assert f"Error processing parent issue #{issue_number}" in result[0]
        assert "API Error" in result[0]

        # Should log error
        mock_logger.error.assert_called()


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
