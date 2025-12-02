"""Tests for parent issue processing functionality."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_processor import _apply_issue_actions_directly, _create_pr_for_parent_issue, _process_parent_issue, _take_issue_actions
from src.auto_coder.prompt_loader import clear_prompt_cache, render_prompt


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
    @patch("src.auto_coder.issue_processor._create_pr_for_parent_issue")
    def test_process_parent_issue_verification_closes_issue(self, mock_create_pr, mock_run_llm):
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

        # Mock PR creation
        mock_create_pr.return_value = "Successfully created PR for parent issue #100"

        result = _process_parent_issue(repo_name, issue_data, config, github_client)

        # Should verify requirements
        assert mock_run_llm.called
        verification_prompt = mock_run_llm.call_args[0][0]
        assert f"Parent Issue\n**Number:** {issue_number}" in verification_prompt

        # Should create PR
        mock_create_pr.assert_called_once()

        # Should close the issue
        github_client.close_issue.assert_called_once()
        close_call = github_client.close_issue.call_args
        assert close_call[0][0] == repo_name
        assert close_call[0][1] == issue_number
        assert "Auto-Coder Verification" in close_call[0][2]

        # Should return success actions (verification, PR creation, and closing)
        assert len(result) >= 3
        assert f"Verified parent issue #{issue_number}" in result[0]
        assert "Successfully created PR for parent issue" in result[1]
        assert f"Closed parent issue #{issue_number}" in result[2]

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
        github_client.find_closing_pr.return_value = 201
        mock_repo.get_pull.return_value = mock_pr

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


class TestCreatePRForParentIssue:
    """Tests for _create_pr_for_parent_issue function."""

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.get_gh_logger")
    def test_create_pr_for_parent_issue_new_branch(self, mock_gh_logger, mock_cmd):
        """Test creating PR for parent issue with new branch."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Parent issue body",
        }
        config = AutomationConfig()
        summary = "All requirements met"
        reasoning = "All sub-issues closed and verified"

        # Mock GitHub client
        github_client = MagicMock()

        # Mock git commands - branch doesn't exist
        mock_cmd.run_command.side_effect = [
            MagicMock(returncode=1),  # Branch doesn't exist
            MagicMock(returncode=0, stdout=""),  # Create branch
            MagicMock(returncode=0, stdout=""),  # Push branch
            MagicMock(returncode=0, stdout=""),  # Git status (no changes)
            MagicMock(returncode=0),  # gh pr create
            MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/123"),
        ]

        # Mock gh_logger
        mock_gh_instance = MagicMock()
        mock_gh_instance.execute_with_logging.return_value = MagicMock(success=True, stdout="https://github.com/owner/repo/pull/123")
        mock_gh_logger.return_value = mock_gh_instance

        result = _create_pr_for_parent_issue(repo_name, issue_data, github_client, config, summary, reasoning)

        # Should create branch
        assert mock_cmd.run_command.call_count >= 2
        # Check that branch was created
        create_branch_call = mock_cmd.run_command.call_args_list[1]
        assert "checkout" in str(create_branch_call)
        assert "-b" in str(create_branch_call)

        # Should create PR
        assert "Successfully created PR for parent issue" in result
        assert str(issue_number) in result

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.get_gh_logger")
    def test_create_pr_for_parent_issue_existing_branch(self, mock_gh_logger, mock_cmd):
        """Test creating PR for parent issue with existing branch."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Parent issue body",
        }
        config = AutomationConfig()
        summary = "All requirements met"
        reasoning = "All sub-issues closed and verified"

        # Mock GitHub client
        github_client = MagicMock()

        # Mock git commands - branch exists
        mock_cmd.run_command.side_effect = [
            MagicMock(returncode=0, stdout=""),  # Branch exists
            MagicMock(returncode=0, stdout=""),  # Switch to branch
            MagicMock(returncode=0, stdout=""),  # Git status (no changes)
            MagicMock(returncode=0),  # gh pr create
            MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/123"),
        ]

        # Mock gh_logger
        mock_gh_instance = MagicMock()
        mock_gh_instance.execute_with_logging.return_value = MagicMock(success=True, stdout="https://github.com/owner/repo/pull/123")
        mock_gh_logger.return_value = mock_gh_instance

        result = _create_pr_for_parent_issue(repo_name, issue_data, github_client, config, summary, reasoning)

        # Should switch to existing branch
        assert mock_cmd.run_command.call_count >= 2
        # Check that we switched to branch
        switch_call = mock_cmd.run_command.call_args_list[1]
        assert "checkout" in str(switch_call)

        # Should create PR
        assert "Successfully created PR for parent issue" in result

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.get_gh_logger")
    @patch("src.auto_coder.git_branch.git_commit_with_retry")
    def test_create_pr_for_parent_issue_with_changes(self, mock_git_commit, mock_gh_logger, mock_cmd):
        """Test creating PR with changes to commit."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Parent issue body",
        }
        config = AutomationConfig()
        summary = "All requirements met"
        reasoning = "All sub-issues closed and verified"

        # Mock GitHub client
        github_client = MagicMock()

        # Mock git commands
        mock_cmd.run_command.side_effect = [
            MagicMock(returncode=0, stdout=""),  # Branch exists
            MagicMock(returncode=0, stdout=""),  # Switch to branch
            MagicMock(returncode=0, stdout="M file.py"),  # Git status (has changes)
            MagicMock(returncode=0),  # Create completion file
            MagicMock(returncode=0, stdout=""),  # Git add
            MagicMock(returncode=0, stdout=""),  # Git push
            MagicMock(returncode=0),  # gh pr create
            MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/123"),
        ]

        # Mock commit
        mock_git_commit.return_value = MagicMock(success=True)

        # Mock gh_logger
        mock_gh_instance = MagicMock()
        mock_gh_instance.execute_with_logging.return_value = MagicMock(success=True, stdout="https://github.com/owner/repo/pull/123")
        mock_gh_logger.return_value = mock_gh_instance

        result = _create_pr_for_parent_issue(repo_name, issue_data, github_client, config, summary, reasoning)

        # Should commit changes
        mock_git_commit.assert_called_once()
        commit_message = mock_git_commit.call_args[0][0]
        assert f"Mark parent issue #{issue_number} as complete" in commit_message

        # Should create PR
        assert "Successfully created PR for parent issue" in result

    @patch("src.auto_coder.issue_processor.cmd")
    def test_create_pr_for_parent_issue_branch_creation_fails(self, mock_cmd):
        """Test error handling when branch creation fails."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Parent issue body",
        }
        config = AutomationConfig()
        summary = "All requirements met"
        reasoning = "All sub-issues closed and verified"

        # Mock GitHub client
        github_client = MagicMock()

        # Mock git commands - first call checks if branch exists (doesn't)
        # second call tries to create branch and fails
        check_result = MagicMock(returncode=1, stderr="Branch doesn't exist")
        create_result = MagicMock(returncode=1, stderr="error: pathspec 'issue-100' did not match any file(s) known to git")
        mock_cmd.run_command.side_effect = [check_result, create_result]

        result = _create_pr_for_parent_issue(repo_name, issue_data, github_client, config, summary, reasoning)

        # Should return error message
        assert "Error creating PR" in result

    @patch("src.auto_coder.issue_processor.cmd")
    @patch("src.auto_coder.issue_processor.get_gh_logger")
    def test_create_pr_for_parent_issue_pr_creation_fails(self, mock_gh_logger, mock_cmd):
        """Test error handling when PR creation fails."""
        repo_name = "owner/repo"
        issue_number = 100
        issue_data = {
            "number": issue_number,
            "title": "Parent Issue",
            "body": "Parent issue body",
        }
        config = AutomationConfig()
        summary = "All requirements met"
        reasoning = "All sub-issues closed and verified"

        # Mock GitHub client
        github_client = MagicMock()

        # Mock git commands
        mock_cmd.run_command.side_effect = [
            MagicMock(returncode=0, stdout=""),  # Branch exists
            MagicMock(returncode=0, stdout=""),  # Switch to branch
            MagicMock(returncode=0, stdout=""),  # Git status (no changes)
        ]

        # Mock gh_logger - PR creation fails
        mock_gh_instance = MagicMock()
        mock_gh_instance.execute_with_logging.return_value = MagicMock(success=False, stderr="Error: resource not accessible by integration")
        mock_gh_logger.return_value = mock_gh_instance

        result = _create_pr_for_parent_issue(repo_name, issue_data, github_client, config, summary, reasoning)

        # Should return error message
        assert "Error creating PR" in result

    @patch("src.auto_coder.issue_processor.run_llm_noedit_prompt")
    def test_process_parent_issue_creates_pr_when_closing(self, mock_run_llm):
        """Test that _process_parent_issue creates PR when closing issue."""
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

        # Mock PRs
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

        # Mock _create_pr_for_parent_issue
        with patch("src.auto_coder.issue_processor._create_pr_for_parent_issue") as mock_create_pr:
            mock_create_pr.return_value = "Successfully created PR for parent issue #100"

            result = _process_parent_issue(repo_name, issue_data, config, github_client)

            # Should create PR before closing issue
            mock_create_pr.assert_called_once()

            # Should close the issue
            github_client.close_issue.assert_called_once()

            # Should return PR creation action
            assert "Successfully created PR for parent issue #100" in result[1]


class TestParentIssueContextInjection:
    """Tests for parent issue context injection in sub-issues."""

    def test_sub_issue_with_parent_context_injected(self):
        """Test that sub-issues correctly inject parent issue context into prompts."""
        repo_name = "owner/repo"
        issue_number = 201
        issue_data = {
            "number": issue_number,
            "title": "Sub-issue 1: Implement feature",
            "body": "Implement the first part of the feature",
            "labels": ["bug"],
            "state": "open",
            "author": "user1",
        }
        config = AutomationConfig()

        # Mock GitHub client
        github_client = MagicMock()
        # Sub-issue has a parent
        github_client.get_parent_issue_details.return_value = {
            "number": 200,
            "title": "Parent Issue: Implement feature X",
            "state": "open",
        }
        github_client.get_parent_issue_body.return_value = "Parent issue description with full context and requirements"

        # Mock commit log
        with patch("src.auto_coder.issue_processor.get_commit_log") as mock_commit_log:
            mock_commit_log.return_value = "Initial commit"

            # Mock LLM CLI call
            with patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend:
                mock_backend_manager = MagicMock()
                mock_backend_manager._run_llm_cli.return_value = "Implemented feature"
                mock_backend.return_value = mock_backend_manager

                # Mock commit_and_push_changes
                with patch("src.auto_coder.issue_processor.commit_and_push_changes") as mock_commit:
                    mock_commit.return_value = "Committed changes"

                    # Mock _create_pr_for_issue
                    with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                        mock_create_pr.return_value = "Created PR"

                        # Mock branch_context
                        with patch("src.auto_coder.issue_processor.branch_context"):
                            # Mock LabelManager
                            with patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr:
                                mock_label_mgr.return_value.__enter__.return_value = True
                                mock_label_mgr.return_value.__exit__.return_value = None

                                # Mock ProgressStage
                                with patch("src.auto_coder.issue_processor.ProgressStage"):
                                    result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

        # Verify get_parent_issue_body was called
        github_client.get_parent_issue_body.assert_called_once_with(repo_name, issue_number)

        # Verify parent context was fetched
        github_client.get_parent_issue_details.assert_called_once_with(repo_name, issue_number)

    def test_regular_issue_without_parent_not_affected(self):
        """Test that regular issues without parents do not get parent context."""
        repo_name = "owner/repo"
        issue_number = 300
        issue_data = {
            "number": issue_number,
            "title": "Regular Issue",
            "body": "A regular issue without parent",
            "labels": ["bug"],
            "state": "open",
            "author": "user2",
        }
        config = AutomationConfig()

        # Mock GitHub client - no parent
        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = None
        github_client.get_parent_issue_body.return_value = None  # Should not be called

        # Mock commit log
        with patch("src.auto_coder.issue_processor.get_commit_log") as mock_commit_log:
            mock_commit_log.return_value = "Initial commit"

            # Mock LLM CLI call
            with patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend:
                mock_backend_manager = MagicMock()
                mock_backend_manager._run_llm_cli.return_value = "Implemented feature"
                mock_backend.return_value = mock_backend_manager

                # Mock commit_and_push_changes
                with patch("src.auto_coder.issue_processor.commit_and_push_changes") as mock_commit:
                    mock_commit.return_value = "Committed changes"

                    # Mock _create_pr_for_issue
                    with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                        mock_create_pr.return_value = "Created PR"

                        # Mock branch_context
                        with patch("src.auto_coder.issue_processor.branch_context"):
                            # Mock LabelManager
                            with patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr:
                                mock_label_mgr.return_value.__enter__.return_value = True
                                mock_label_mgr.return_value.__exit__.return_value = None

                                # Mock ProgressStage
                                with patch("src.auto_coder.issue_processor.ProgressStage"):
                                    result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

        # Verify parent issue methods were called but parent_issue_body returned None
        github_client.get_parent_issue_details.assert_called_once_with(repo_name, issue_number)
        # get_parent_issue_body should not be called when there's no parent
        github_client.get_parent_issue_body.assert_not_called()

    def test_parent_issue_body_none_handled_correctly(self):
        """Test that None parent issue body is handled correctly."""
        repo_name = "owner/repo"
        issue_number = 201
        issue_data = {
            "number": issue_number,
            "title": "Sub-issue",
            "body": "Body",
            "labels": [],
            "state": "open",
            "author": "user1",
        }
        config = AutomationConfig()

        # Mock GitHub client - has parent but body is None
        github_client = MagicMock()
        github_client.get_parent_issue_details.return_value = {
            "number": 200,
            "title": "Parent Issue",
            "state": "open",
        }
        github_client.get_parent_issue_body.return_value = None

        # Mock commit log
        with patch("src.auto_coder.issue_processor.get_commit_log") as mock_commit_log:
            mock_commit_log.return_value = "Initial commit"

            # Mock LLM CLI call
            with patch("src.auto_coder.issue_processor.get_llm_backend_manager") as mock_backend:
                mock_backend_manager = MagicMock()
                mock_backend_manager._run_llm_cli.return_value = "Implemented"
                mock_backend.return_value = mock_backend_manager

                # Mock commit_and_push_changes
                with patch("src.auto_coder.issue_processor.commit_and_push_changes") as mock_commit:
                    mock_commit.return_value = "Committed"

                    # Mock _create_pr_for_issue
                    with patch("src.auto_coder.issue_processor._create_pr_for_issue") as mock_create_pr:
                        mock_create_pr.return_value = "Created PR"

                        # Mock branch_context
                        with patch("src.auto_coder.issue_processor.branch_context"):
                            # Mock LabelManager
                            with patch("src.auto_coder.issue_processor.LabelManager") as mock_label_mgr:
                                mock_label_mgr.return_value.__enter__.return_value = True
                                mock_label_mgr.return_value.__exit__.return_value = None

                                # Mock ProgressStage
                                with patch("src.auto_coder.issue_processor.ProgressStage"):
                                    result = _apply_issue_actions_directly(repo_name, issue_data, config, github_client)

        # get_parent_issue_body should be called even if it returns None
        github_client.get_parent_issue_body.assert_called_once_with(repo_name, issue_number)

    def test_render_prompt_with_parent_issue_body(self, tmp_path):
        """Test that render_prompt correctly handles parent_issue_body parameter."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" "  action: |\n" "    Issue #$issue_number: $issue_title\n" "    Body: $issue_body\n" "    Parent Context: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test with parent_issue_body
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="123",
            issue_title="Test Issue",
            issue_body="Test body",
            parent_issue_body="Parent context here",
        )

        # Should include parent context
        assert "Parent Context: Parent context here" in result
        assert "Issue #123: Test Issue" in result

    def test_render_prompt_without_parent_issue_body(self, tmp_path):
        """Test that render_prompt works correctly when parent_issue_body is not provided."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" "  action: |\n" "    Issue #$issue_number: $issue_title\n" "    Body: $issue_body\n" "    Parent Context: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test without parent_issue_body (regular issue)
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="456",
            issue_title="Regular Issue",
            issue_body="Regular body",
        )

        # Should show empty parent context (variable not substituted)
        assert "Issue #456: Regular Issue" in result
        assert "Regular body" in result
        # When parent_issue_body is not in params, it's not substituted
        assert "$parent_issue_body" in result

    def test_render_prompt_with_empty_parent_issue_body(self, tmp_path):
        """Test that render_prompt handles empty parent_issue_body correctly."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" "  action: |\n" "    Issue #$issue_number\n" "    Parent: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test with empty string parent_issue_body
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="789",
            parent_issue_body="",
        )

        # Empty string should be substituted as empty
        assert "Parent: " in result
        assert "Issue #789" in result

    def test_render_prompt_backward_compatibility_without_parent_param(self, tmp_path):
        """Test backward compatibility - render_prompt works without parent_issue_body parameter."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            'issue:\n  action: "Issue $issue_number"\n',
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test without parent_issue_body parameter at all (backward compatibility)
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_number="999",
        )

        # Should work without errors
        assert "Issue 999" in result

    def test_parent_context_integration_with_labels(self, tmp_path):
        """Test that parent context works correctly with label-based prompts."""
        prompt_file = tmp_path / "prompts.yaml"
        prompt_file.write_text(
            "issue:\n" '  action: "Default: $issue_title"\n' "  bugfix: |\n" "    Bug Fix: $issue_title\n" "    Parent: $parent_issue_body\n",
            encoding="utf-8",
        )

        clear_prompt_cache()

        # Test with parent context and label-based prompt
        result = render_prompt(
            "issue.action",
            path=str(prompt_file),
            issue_title="Fix bug",
            labels=["bug"],
            label_prompt_mappings={"bug": "issue.bugfix"},
            label_priorities=["bug"],
            parent_issue_body="Parent bug context",
        )

        # Should use bugfix prompt with parent context
        assert "Bug Fix:" in result
        assert "Parent: Parent bug context" in result
        assert "Fix bug" in result
