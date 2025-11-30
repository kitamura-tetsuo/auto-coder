"""Integration test for fallback logic without complex mocking.

This test verifies the fallback logic implementation works correctly
by testing the actual functions with minimal mocking.
"""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.pr_processor import _should_use_fallback_backend, _switch_to_fallback_backend


def test_should_use_fallback_backend_integration():
    """Integration test: Verify fallback logic with mocked attempt count."""

    # PR with linked issue #456
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

    # Mock GitHub client and attempt manager
    with patch("src.auto_coder.pr_processor.get_current_attempt") as mock_get_attempt:
        # Test with attempt = 3
        mock_get_attempt.return_value = 3
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is True, "Should use fallback when attempt >= 3"

        # Test with attempt = 2
        mock_get_attempt.return_value = 2
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is False, "Should NOT use fallback when attempt < 3"

        # Test with attempt = 4
        mock_get_attempt.return_value = 4
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is True, "Should use fallback when attempt > 3"


def test_switch_to_fallback_backend_integration():
    """Integration test: Verify backend switching logic."""

    repo_name = "owner/repo"
    pr_number = 123

    # Mock the configuration and backend manager
    mock_config = Mock(spec=LLMBackendConfiguration)
    mock_fallback_config = BackendConfig(name="gemini", model="gemini-2.0-flash")
    mock_config.get_backend_for_failed_pr.return_value = mock_fallback_config
    mock_config.get_model_for_failed_pr_backend.return_value = "gemini-2.0-flash"

    # Create a mock backend manager that doesn't require initialization
    mock_backend_manager = Mock()
    mock_backend_manager._switch_to_backend_by_name = Mock()

    with patch("src.auto_coder.llm_backend_config.get_llm_config", return_value=mock_config), patch("src.auto_coder.pr_processor.get_llm_backend_manager", return_value=mock_backend_manager):
        # Test successful switch
        result = _switch_to_fallback_backend(repo_name, pr_number)
        assert result is True, "Should successfully switch to fallback backend"
        mock_backend_manager._switch_to_backend_by_name.assert_called_once_with("gemini")


def test_pr_with_no_linked_issues():
    """Test that PRs without linked issues don't trigger fallback."""
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
        assert result is False, "Should NOT use fallback when PR has no linked issues"
        mock_get_attempt.assert_not_called()


def test_multiple_linked_issues_takes_max():
    """Test that multiple linked issues use max attempt count."""
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
        # Issue 456 has attempt 1, issue 789 has attempt 3
        # Should use fallback because max >= 3
        mock_get_attempt.side_effect = [1, 3]
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is True, "Should use fallback when max attempt >= 3"

        # Issue 456 has attempt 1, issue 789 has attempt 2
        # Should NOT use fallback because max < 3
        mock_get_attempt.side_effect = [1, 2]
        result = _should_use_fallback_backend(repo_name, pr_data)
        assert result is False, "Should NOT use fallback when max attempt < 3"
