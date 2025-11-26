import pytest
from unittest.mock import MagicMock, patch
from auto_coder.issue_processor import _apply_issue_actions_directly
from auto_coder.automation_config import AutomationConfig

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
@patch("auto_coder.issue_processor.branch_context")
@patch("auto_coder.issue_processor.get_llm_backend_manager")
def test_apply_issue_actions_directly_closed_parent(
    mock_get_llm_backend_manager,
    mock_branch_context,
    mock_get_current_attempt,
    mock_cmd,
    mock_github_client,
    mock_config
):
    # Setup
    repo_name = "owner/repo"
    issue_data = {"number": 123, "title": "Test Issue", "body": "Body", "labels": []}
    
    # Mock parent issue details as CLOSED
    mock_github_client.get_parent_issue_details.return_value = {
        "number": 100,
        "title": "Parent Issue",
        "state": "CLOSED",
        "url": "http://github.com/owner/repo/issues/100"
    }
    
    # Mock git commands
    mock_cmd.run_command.return_value.success = True
    mock_cmd.run_command.return_value.returncode = 0
    mock_cmd.run_command.return_value.stdout = "main"
    
    # Mock get_current_attempt
    mock_get_current_attempt.return_value = 1
    
    # Run
    _apply_issue_actions_directly(repo_name, issue_data, mock_config, mock_github_client)
    
    # Verify
    # Should NOT check for parent branch existence if parent is closed
    # The exact verification depends on implementation details, but we can check logs or calls
    # Here we verify that it didn't try to verify the parent branch
    
    # Check that it called get_parent_issue_details
    mock_github_client.get_parent_issue_details.assert_called_with(repo_name, 123)
    
    # Verify that it didn't try to use the parent branch
    # We can check calls to cmd.run_command to see if it checked for "issue-100"
    for call in mock_cmd.run_command.call_args_list:
        args, _ = call
        cmd_list = args[0]
        if "rev-parse" in cmd_list and "--verify" in cmd_list:
            # It should check work branch "issue-123_attempt-1"
            # It should NOT check parent branch "issue-100"
            assert "issue-100" not in cmd_list[3]

@patch("auto_coder.issue_processor.cmd")
@patch("auto_coder.issue_processor.get_current_attempt")
@patch("auto_coder.issue_processor.branch_context")
@patch("auto_coder.issue_processor.get_llm_backend_manager")
def test_apply_issue_actions_directly_open_parent(
    mock_get_llm_backend_manager,
    mock_branch_context,
    mock_get_current_attempt,
    mock_cmd,
    mock_github_client,
    mock_config
):
    # Setup
    repo_name = "owner/repo"
    issue_data = {"number": 124, "title": "Test Issue Open Parent", "body": "Body", "labels": []}
    
    # Mock parent issue details as OPEN
    mock_github_client.get_parent_issue_details.return_value = {
        "number": 101,
        "title": "Parent Issue Open",
        "state": "OPEN",
        "url": "http://github.com/owner/repo/issues/101"
    }
    
    # Mock git commands
    mock_cmd.run_command.return_value.success = True
    mock_cmd.run_command.return_value.returncode = 0
    mock_cmd.run_command.return_value.stdout = "main"
    
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
