import pytest
from unittest.mock import MagicMock, patch
from src.auto_coder.util.github_action import _check_github_actions_status, GitHubActionsStatusResult
from src.auto_coder.pr_processor import _handle_pr_merge
from src.auto_coder.utils import CommandResult


@patch("src.auto_coder.util.github_action.get_gh_logger")
def test_check_github_actions_status_gh_command_failure(mock_get_gh_logger):
    """
    Test that _check_github_actions_status handles gh command failures correctly.
    """
    # Arrange
    mock_gh_logger = MagicMock()
    mock_get_gh_logger.return_value = mock_gh_logger

    repo_name = "test/repo"
    pr_data = {"number": 123, "head": {"sha": "test_sha"}}
    config = MagicMock()

    error_message = "gh command failed"
    mock_gh_logger.execute_with_logging.return_value = CommandResult(stdout="", stderr=error_message, returncode=1, success=False)

    # Act
    result = _check_github_actions_status(repo_name, pr_data, config)

    # Assert
    assert result.success is False
    assert error_message in result.error


@patch("src.auto_coder.pr_processor._check_github_actions_status")
def test_handle_pr_merge_ci_status_error(mock_check_status):
    """
    Test that _handle_pr_merge skips processing when there is an error in the CI status.
    """
    # Arrange
    repo_name = "test/repo"
    pr_data = {"number": 123, "head": {"sha": "test_sha"}}
    config = MagicMock()
    analysis = {}
    github_client = MagicMock()

    error_message = "CI status check failed"
    mock_check_status.return_value = GitHubActionsStatusResult(success=False, error=error_message)

    # Act
    actions = _handle_pr_merge(github_client, repo_name, pr_data, config, analysis)

    # Assert
    assert any(error_message in action for action in actions)
