"""Tests for mergeability detection and remediation scaffolding."""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _handle_pr_merge


@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
def test_non_mergeable_detection_is_reported(mock_check_progress):
    """Ensure non-mergeable PRs surface a detection action."""
    mock_check_progress.return_value = False
    config = AutomationConfig()
    pr_data = {"number": 42, "mergeable": False}

    actions = _handle_pr_merge(Mock(), "owner/repo", pr_data, config, {})

    assert any("not mergeable" in action.lower() for action in actions)


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
def test_mergeability_remediation_stub_invoked(mock_check_progress, mock_check_status):
    """Verify remediation stub path is activated when enabled."""
    mock_check_progress.return_value = True
    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 99, "mergeable": False}

    actions = _handle_pr_merge(Mock(), "owner/repo", pr_data, config, {})

    assert any("remediation stub" in action.lower() for action in actions)
    mock_check_status.assert_not_called()
