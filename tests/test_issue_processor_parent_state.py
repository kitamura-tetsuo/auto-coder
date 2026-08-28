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
@patch("auto_coder.issue_processor.BranchManager")
@patch("auto_coder.issue_processor.LabelManager")
@patch("auto_coder.issue_processor.get_current_branch")
@patch("auto_coder.issue_processor.get_commit_log")
@patch("auto_coder.issue_processor.get_llm_backend_manager")
@patch("auto_coder.git_info.CommandExecutor")
def test_apply_issue_actions_directly_open_parent(mock_cmd_executor_class, mock_get_llm_backend_manager, mock_get_commit_log, mock_get_current_branch, mock_label_manager, mock_branch_manager, mock_get_current_attempt, mock_cmd, mock_github_client, mock_config):
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

    # Mock get_commit_log to return empty string
    mock_get_commit_log.return_value = ""

    # Mock get_current_branch to return main
    mock_get_current_branch.return_value = "main"

    # Mock BranchManager context manager
    mock_branch_manager.return_value.__enter__ = MagicMock(return_value=None)
    mock_branch_manager.return_value.__exit__ = MagicMock(return_value=None)

    # Mock LabelManager context manager
    mock_label_manager.return_value.__enter__ = MagicMock(return_value=True)
    mock_label_manager.return_value.__exit__ = MagicMock(return_value=None)

    # Run
    _apply_issue_actions_directly(repo_name, issue_data, mock_config, mock_github_client)

    # Verify
    mock_github_client.get_parent_issue_details.assert_called_with(repo_name, 124)
    mock_github_client.reopen_issue.assert_not_called()


@patch("auto_coder.issue_processor.cmd")
@patch("auto_coder.issue_processor.get_current_attempt")
@patch("auto_coder.issue_processor.BranchManager")
@patch("auto_coder.issue_processor.LabelManager")
@patch("auto_coder.issue_processor.get_current_branch")
@patch("auto_coder.issue_processor.get_commit_log")
@patch("auto_coder.issue_processor.get_llm_backend_manager")
@patch("auto_coder.git_info.CommandExecutor")
def test_apply_issue_actions_directly_closed_parent_not_reopened(
    mock_cmd_executor_class,
    mock_get_llm_backend_manager,
    mock_get_commit_log,
    mock_get_current_branch,
    mock_label_manager,
    mock_branch_manager,
    mock_get_current_attempt,
    mock_cmd,
    mock_github_client,
    mock_config,
):
    # Setup
    repo_name = "owner/repo"
    issue_data = {"number": 125, "title": "Test Issue Closed Parent", "body": "Body", "labels": []}

    # Mock parent issue details as CLOSED
    mock_github_client.get_parent_issue_details.return_value = {
        "number": 102,
        "title": "Parent Issue Closed",
        "state": "CLOSED",
        "url": "http://github.com/owner/repo/issues/102",
    }

    # Mock git commands - use return_value to handle any number of calls
    mock_cmd.run_command.return_value = _cmd_result(success=True, returncode=0, stdout="main")

    # Mock CommandExecutor used by get_current_branch
    mock_git_info_cmd = MagicMock()
    mock_git_info_cmd.run_command.return_value = _cmd_result(success=True, returncode=0, stdout="main")
    mock_cmd_executor_class.return_value = mock_git_info_cmd

    # Mock get_current_attempt
    mock_get_current_attempt.return_value = 1

    # Mock get_commit_log to return empty string
    mock_get_commit_log.return_value = ""

    # Mock get_current_branch to return main
    mock_get_current_branch.return_value = "main"

    # Mock BranchManager context manager
    mock_branch_manager.return_value.__enter__ = MagicMock(return_value=None)
    mock_branch_manager.return_value.__exit__ = MagicMock(return_value=None)

    # Mock LabelManager context manager
    mock_label_manager.return_value.__enter__ = MagicMock(return_value=True)
    mock_label_manager.return_value.__exit__ = MagicMock(return_value=None)

    # Run
    _apply_issue_actions_directly(repo_name, issue_data, mock_config, mock_github_client)

    # Verify
    mock_github_client.get_parent_issue_details.assert_called_with(repo_name, 125)
    # Ensure reopen_issue is NOT called
    mock_github_client.reopen_issue.assert_not_called()
