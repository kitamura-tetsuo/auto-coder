"""
Unit tests for Jules fallback configuration via [jules].enabled_fallback_to_local in config.toml.
"""

import os
import tempfile
from unittest.mock import Mock, patch

from src.auto_coder.llm_backend_config import get_jules_fallback_enabled_from_config
from src.auto_coder.pr_processor import _handle_pr_merge
from src.auto_coder.automation_config import AutomationConfig


def test_jules_fallback_enabled_via_config_toml():
    """Test Jules fallback enabled via [jules].enabled_fallback_to_local = true in config.toml."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create config.toml with Jules fallback enabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled_fallback_to_local = true
"""
            )

        # Test that Jules fallback is enabled
        assert get_jules_fallback_enabled_from_config(config_path) is True


def test_jules_fallback_disabled_via_config_toml():
    """Test Jules fallback disabled via [jules].enabled_fallback_to_local = false in config.toml."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create config.toml with Jules fallback disabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled_fallback_to_local = false
"""
            )

        # Test that Jules fallback is disabled
        assert get_jules_fallback_enabled_from_config(config_path) is False


def test_jules_fallback_default_no_config():
    """Test default behavior when config.toml doesn't exist."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        # Should return default (True) when config.toml doesn't exist
        assert get_jules_fallback_enabled_from_config(config_path) is True


def test_jules_fallback_default_missing_setting():
    """Test behavior when config.toml exists but has no [jules].enabled_fallback_to_local setting."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config.toml without the setting
        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = true
"""
            )

        # Should return default (True) when setting is missing
        assert get_jules_fallback_enabled_from_config(config_path) is True


@patch("src.auto_coder.pr_processor._is_jules_pr")
@patch("src.auto_coder.pr_processor._send_jules_error_feedback")
@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.get_detailed_checks_from_history")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor._get_mergeable_state")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
@patch("src.auto_coder.pr_processor._get_github_actions_logs")
@patch("src.auto_coder.pr_processor._fix_pr_issues_with_testing")
@patch("src.auto_coder.pr_processor.get_jules_fallback_enabled_from_config")
def test_handle_pr_merge_jules_fallback_disabled_in_config(
    mock_get_fallback_config,
    mock_fix_issues,
    mock_get_logs,
    mock_update_base,
    mock_checkout,
    mock_mergeable,
    mock_check_in_progress,
    mock_detailed_checks,
    mock_check_status,
    mock_send_feedback,
    mock_is_jules,
):
    """Test that fallback logic is skipped when disabled in config."""
    # Setup
    repo_name = "owner/repo"
    pr_data = {"number": 123, "title": "Test PR"}
    config = AutomationConfig()
    github_client = Mock()

    # Mock checks failure
    mock_check_in_progress.return_value = True
    mock_mergeable.return_value = {"mergeable": True}
    mock_check_status.return_value = Mock(success=False)
    mock_detailed_checks.return_value = Mock(success=False, failed_checks=[{"name": "test"}])

    # Mock Jules PR
    mock_is_jules.return_value = True

    # Mock config disabling fallback
    mock_get_fallback_config.return_value = False

    # Mock comments (would trigger fallback if enabled)
    target_message = "🤖 Auto-Coder: CI checks failed. I've sent the error logs to the Jules session and requested a fix. Please wait for the updates."
    comments = [{"body": target_message}] * 11  # 11 failures
    github_client.get_pr_comments.return_value = comments

    # Execute
    actions = _handle_pr_merge(github_client, repo_name, pr_data, config, {})

    # Assert
    # Should call send feedback (Jules handles it) instead of falling back to local
    mock_send_feedback.assert_called_once()
    assert "Jules will handle fixing PR #123, skipping local fixes" in actions[-1]
    
    # Should NOT proceed to checkout and fix
    mock_checkout.assert_not_called()
    mock_fix_issues.assert_not_called()
