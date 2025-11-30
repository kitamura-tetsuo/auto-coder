"""Tests for PR processor fallback logic implementation.

Tests the fallback backend switching functionality when attempt count reaches 3.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.pr_processor import (
    _apply_github_actions_fix,
    _apply_local_test_fix,
    _apply_pr_actions_directly,
    _should_use_fallback_backend,
    _switch_to_fallback_backend,
)


class MockGitHubClient:
    """Mock GitHub client for testing."""

    pass


class DummyClient:
    """Mock LLM client for testing."""

    def __init__(self, name: str):
        self.name = name
        self.model_name = name

    def _run_llm_cli(self, prompt: str) -> str:
        return f"{self.name}: response"

    def switch_to_default_model(self):
        pass

    def get_last_session_id(self) -> str:
        return f"{self.name}_session"


def test_should_use_fallback_backend_with_attempt_3():
    """Test that fallback backend is used when linked issue has attempt count >= 3."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }

    with patch("src.auto_coder.pr_processor.get_current_attempt") as mock_get_attempt:
        # Mock attempt count of 3 for issue #456
        mock_get_attempt.return_value = 3

        # Should use fallback when attempt >= 3
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is True

        # Verify get_current_attempt was called for the linked issue
        mock_get_attempt.assert_called_once_with(repo_name, 456)


def test_should_use_fallback_backend_with_attempt_2():
    """Test that fallback backend is NOT used when attempt count is less than 3."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }

    with patch("src.auto_coder.pr_processor.get_current_attempt") as mock_get_attempt:
        # Mock attempt count of 2 for issue #456
        mock_get_attempt.return_value = 2

        # Should NOT use fallback when attempt < 3
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is False

        # Verify get_current_attempt was called
        mock_get_attempt.assert_called_once_with(repo_name, 456)


def test_should_use_fallback_backend_with_multiple_linked_issues():
    """Test fallback logic with multiple linked issues (uses max attempt)."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456 and closes #789",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }

    with patch("src.auto_coder.pr_processor.get_current_attempt") as mock_get_attempt:
        # Mock different attempt counts for different issues
        # Issue 456 has attempt 2, issue 789 has attempt 3
        # Should use fallback because max attempt >= 3
        mock_get_attempt.side_effect = [2, 3]

        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is True

        # Verify both issues were checked
        assert mock_get_attempt.call_count == 2
        mock_get_attempt.assert_any_call(repo_name, 456)
        mock_get_attempt.assert_any_call(repo_name, 789)


def test_should_use_fallback_backend_no_linked_issues():
    """Test that fallback is NOT used when PR has no linked issues."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR has no linked issues",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }

    with patch("src.auto_coder.pr_processor.get_current_attempt") as mock_get_attempt:
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is False
        # Should not call get_current_attempt if no linked issues
        mock_get_attempt.assert_not_called()


def test_should_use_fallback_backend_no_pr_body():
    """Test that fallback is NOT used when PR has no body."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }

    with patch("src.auto_coder.pr_processor.get_current_attempt") as mock_get_attempt:
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is False
        mock_get_attempt.assert_not_called()


def test_switch_to_fallback_backend_success():
    """Test successful switch to fallback backend."""
    repo_name = "owner/repo"
    pr_number = 123

    # Mock LLM config with fallback backend
    mock_config = Mock(spec=LLMBackendConfiguration)
    mock_fallback_config = BackendConfig(name="gemini", model="gemini-2.0-flash")
    mock_config.get_backend_for_failed_pr.return_value = mock_fallback_config
    mock_config.get_model_for_failed_pr_backend.return_value = "gemini-2.0-flash"

    # Mock backend manager instance
    mock_backend_manager = Mock()
    mock_backend_manager._switch_to_backend_by_name = Mock()

    with patch("src.auto_coder.llm_backend_config.get_llm_config") as mock_get_config, patch("src.auto_coder.pr_processor.get_llm_backend_manager", return_value=mock_backend_manager):
        mock_get_config.return_value = mock_config

        # Should succeed
        result = _switch_to_fallback_backend(repo_name, pr_number)
        assert result is True

        # Verify backend was switched
        mock_backend_manager._switch_to_backend_by_name.assert_called_once_with("gemini")


def test_switch_to_fallback_backend_no_config():
    """Test switch when no fallback backend is configured."""
    repo_name = "owner/repo"
    pr_number = 123

    # Mock LLM config without fallback backend
    mock_config = Mock(spec=LLMBackendConfiguration)
    mock_config.get_backend_for_failed_pr.return_value = None

    with patch("src.auto_coder.llm_backend_config.get_llm_config") as mock_get_config, patch("src.auto_coder.backend_manager.get_llm_backend_manager") as mock_get_backend_manager:
        mock_get_config.return_value = mock_config

        # Should succeed (no error when no fallback configured)
        result = _switch_to_fallback_backend(repo_name, pr_number)
        assert result is True

        # Backend manager should not be called
        mock_get_backend_manager.assert_not_called()


def test_apply_pr_actions_directly_uses_fallback():
    """Test that _apply_pr_actions_directly uses fallback backend when attempt >= 3."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }
    config = AutomationConfig()

    # Mock dependencies
    with (
        patch("src.auto_coder.pr_processor._should_use_fallback_backend") as mock_should_use_fallback,
        patch("src.auto_coder.pr_processor._switch_to_fallback_backend") as mock_switch_fallback,
        patch("src.auto_coder.pr_processor._get_pr_diff") as mock_get_diff,
        patch("src.auto_coder.pr_processor._create_pr_analysis_prompt") as mock_create_prompt,
        patch("src.auto_coder.pr_processor.get_llm_backend_manager") as mock_get_manager,
        patch("src.auto_coder.pr_processor.get_gh_logger"),
        patch("src.auto_coder.pr_processor.log_action"),
    ):
        # Setup mocks
        mock_should_use_fallback.return_value = True
        mock_switch_fallback.return_value = True
        mock_get_diff.return_value = "diff --git a/test.py"
        mock_create_prompt.return_value = "Test prompt"
        mock_manager_instance = Mock()
        mock_manager_instance._run_llm_cli.return_value = "ACTION_SUMMARY: test"
        mock_get_manager.return_value = mock_manager_instance

        # Call the function
        actions = _apply_pr_actions_directly(MockGitHubClient(), repo_name, pr_data, config)

        # Verify fallback was checked and switched
        mock_should_use_fallback.assert_called_once_with(repo_name, pr_data)
        mock_switch_fallback.assert_called_once_with(repo_name, 123)


def test_apply_github_actions_fix_uses_fallback():
    """Test that _apply_github_actions_fix uses fallback backend when attempt >= 3."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }
    config = AutomationConfig()

    with (
        patch("src.auto_coder.pr_processor._should_use_fallback_backend") as mock_should_use_fallback,
        patch("src.auto_coder.pr_processor._switch_to_fallback_backend") as mock_switch_fallback,
        patch("src.auto_coder.pr_processor.get_commit_log"),
        patch("src.auto_coder.pr_processor.extract_important_errors") as mock_extract,
        patch("src.auto_coder.pr_processor.render_prompt") as mock_render,
        patch("src.auto_coder.pr_processor.run_llm_prompt") as mock_run,
    ):
        # Setup mocks
        mock_should_use_fallback.return_value = True
        mock_switch_fallback.return_value = True
        mock_extract.return_value = "Error details"
        mock_render.return_value = "Test prompt"
        mock_run.return_value = "Test response"

        # Call the function
        actions = _apply_github_actions_fix(repo_name, pr_data, config, "GitHub logs")

        # Verify fallback was checked and switched
        mock_should_use_fallback.assert_called_once_with(repo_name, pr_data)
        mock_switch_fallback.assert_called_once_with(repo_name, 123)


def test_apply_local_test_fix_uses_fallback():
    """Test that _apply_local_test_fix uses fallback backend when attempt >= 3."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }
    config = AutomationConfig()
    test_result = {
        "success": False,
        "output": "Test output",
        "errors": "Test errors",
        "return_code": 1,
        "command": "pytest",
    }

    with (
        patch("src.auto_coder.pr_processor._should_use_fallback_backend") as mock_should_use_fallback,
        patch("src.auto_coder.pr_processor._switch_to_fallback_backend") as mock_switch_fallback,
        patch("src.auto_coder.pr_processor.extract_important_errors") as mock_extract,
        patch("src.auto_coder.pr_processor.get_commit_log"),
        patch("src.auto_coder.pr_processor.render_prompt") as mock_render,
        patch("src.auto_coder.pr_processor.get_llm_backend_manager") as mock_get_manager,
    ):
        # Setup mocks
        mock_should_use_fallback.return_value = True
        mock_switch_fallback.return_value = True
        mock_extract.return_value = "Error details"
        mock_render.return_value = "Test prompt"
        mock_manager_instance = Mock()
        mock_manager_instance.run_test_fix_prompt.return_value = "Test response"
        mock_get_manager.return_value = mock_manager_instance

        # Call the function
        actions, llm_response = _apply_local_test_fix(repo_name, pr_data, config, test_result, [])

        # Verify fallback was checked and switched
        mock_should_use_fallback.assert_called_once_with(repo_name, pr_data)
        mock_switch_fallback.assert_called_once_with(repo_name, 123)


def test_apply_pr_actions_directly_no_fallback():
    """Test that _apply_pr_actions_directly does NOT use fallback when attempt < 3."""
    repo_name = "owner/repo"
    pr_data = {
        "number": 123,
        "title": "Test PR",
        "body": "This PR closes #456",
        "user": {"login": "testuser"},
        "state": "open",
        "draft": False,
        "mergeable": True,
        "labels": [],
    }
    config = AutomationConfig()

    # Mock dependencies
    with (
        patch("src.auto_coder.pr_processor._should_use_fallback_backend") as mock_should_use_fallback,
        patch("src.auto_coder.pr_processor._switch_to_fallback_backend") as mock_switch_fallback,
        patch("src.auto_coder.pr_processor._get_pr_diff") as mock_get_diff,
        patch("src.auto_coder.pr_processor._create_pr_analysis_prompt") as mock_create_prompt,
        patch("src.auto_coder.pr_processor.get_llm_backend_manager") as mock_get_manager,
        patch("src.auto_coder.pr_processor.get_gh_logger"),
        patch("src.auto_coder.pr_processor.log_action"),
    ):
        # Setup mocks - attempt < 3, so no fallback
        mock_should_use_fallback.return_value = False
        mock_get_diff.return_value = "diff --git a/test.py"
        mock_create_prompt.return_value = "Test prompt"
        mock_manager_instance = Mock()
        mock_manager_instance._run_llm_cli.return_value = "ACTION_SUMMARY: test"
        mock_get_manager.return_value = mock_manager_instance

        # Call the function
        actions = _apply_pr_actions_directly(MockGitHubClient(), repo_name, pr_data, config)

        # Verify fallback was checked but NOT switched
        mock_should_use_fallback.assert_called_once_with(repo_name, pr_data)
        mock_switch_fallback.assert_not_called()
