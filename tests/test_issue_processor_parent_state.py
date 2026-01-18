from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.issue_processor import _apply_issue_actions_directly


def _cmd_result(success=True, stdout="", stderr="", returncode=0):
    class R:
        def __init__(self):
            self.success = success
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return R()


@pytest.fixture
def mock_github_client():
    return MagicMock()


@pytest.fixture
def mock_config():
    config = MagicMock(spec=AutomationConfig)
    config.MAIN_BRANCH = "main"
    config.CHECK_DEPENDENCIES = False
    config.PR_LABEL_COPYING_ENABLED = False
    config.label_prompt_mappings = {}
    config.label_priorities = {}
    return config


@patch("auto_coder.issue_processor.cmd")
@patch("auto_coder.issue_processor.get_current_attempt")
@patch("auto_coder.git_branch.branch_context")
@patch("auto_coder.issue_processor.get_llm_backend_manager")
@patch("auto_coder.git_info.CommandExecutor")
def test_apply_issue_actions_directly_open_parent(mock_cmd_executor_class, mock_get_llm_backend_manager, mock_branch_context, mock_get_current_attempt, mock_cmd, mock_github_client, mock_config):
    # Setup
    repo_name = "owner/repo"
    issue_data = {"number": 124, "title": "Test Issue Open Parent", "body": "Body", "labels": []}

    # Mock parent issue details as OPEN
    mock_github_client.get_parent_issue_details.return_value = {"number": 101, "title": "Parent Issue Open", "state": "OPEN", "url": "http://github.com/owner/repo/issues/101"}

    # Mock git commands - use return_value to handle any number of calls
    mock_cmd.run_command.return_value = _cmd_result(success=True, returncode=0, stdout="main")

    # Mock CommandExecutor used by get_current_branch
    mock_git_info_cmd = MagicMock()
    mock_git_info_cmd.run_command.return_value = _cmd_result(success=True, returncode=0, stdout="main")
    mock_cmd_executor_class.return_value = mock_git_info_cmd

    # Mock get_current_attempt
    mock_get_current_attempt.return_value = 1

    # Run
    _apply_issue_actions_directly(repo_name, issue_data, mock_config, mock_github_client)

    # Verify
    # Should check for parent branch existence if parent is open

    # Check that it called get_parent_issue_details
    mock_github_client.get_parent_issue_details.assert_called_with(repo_name, 124)

    # Verify that it DID try to use the parent branch
    found_parent_check = False
    for call in mock_cmd.run_command.call_args_list:
        args, _ = call
        cmd_list = args[0]
        if "rev-parse" in cmd_list and "--verify" in cmd_list:
            if "issue-101" in cmd_list[3]:
                found_parent_check = True
                break

    assert found_parent_check, "Should have checked for parent branch issue-101"


@patch("auto_coder.issue_processor.cmd")
@patch("auto_coder.issue_processor.get_current_attempt")
@patch("auto_coder.git_branch.branch_context")
@patch("auto_coder.issue_processor.get_llm_backend_manager")
@patch("auto_coder.git_info.CommandExecutor")
def test_apply_issue_actions_directly_closed_parent_reopens(mock_cmd_executor_class, mock_get_llm_backend_manager, mock_branch_context, mock_get_current_attempt, mock_cmd, mock_github_client, mock_config):
    # Setup
    repo_name = "owner/repo"
    issue_data = {"number": 125, "title": "Test Issue Closed Parent", "body": "Body", "labels": []}

    # Mock parent issue details as CLOSED
    mock_github_client.get_parent_issue_details.return_value = {"number": 102, "title": "Parent Issue Closed", "state": "CLOSED", "url": "http://github.com/owner/repo/issues/102"}

    # Mock git commands - use return_value to handle any number of calls
    mock_cmd.run_command.return_value = _cmd_result(success=True, returncode=0, stdout="main")

    # Mock CommandExecutor used by get_current_branch
    mock_git_info_cmd = MagicMock()
    mock_git_info_cmd.run_command.return_value = _cmd_result(success=True, returncode=0, stdout="main")
    mock_cmd_executor_class.return_value = mock_git_info_cmd

    # Mock get_current_attempt
    mock_get_current_attempt.return_value = 1

    # Run
    _apply_issue_actions_directly(repo_name, issue_data, mock_config, mock_github_client)

    # Verify
    # Should call get_parent_issue_details
    mock_github_client.get_parent_issue_details.assert_called_with(repo_name, 125)

    # Verify that reopen_issue was called with the correct parameters
    expected_audit_comment = "Auto-Coder: Reopened this parent issue to process child issue #125. Branch and base selection will use the parent context."
    mock_github_client.reopen_issue.assert_called_with(repo_name, 102, expected_audit_comment)

    # Verify that after reopening, it checks for parent branch existence (same as open parent)
    found_parent_check = False
    for call in mock_cmd.run_command.call_args_list:
        args, _ = call
        cmd_list = args[0]
        if "rev-parse" in cmd_list and "--verify" in cmd_list:
            if "issue-102" in cmd_list[3]:
                found_parent_check = True
                break

    assert found_parent_check, "Should have checked for parent branch issue-102 after reopening"
