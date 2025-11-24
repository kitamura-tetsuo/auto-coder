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
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_mergeability_remediation_flow_invoked(mock_update, mock_checkout, mock_check_progress, mock_check_status):
    """Verify remediation flow is activated when enabled."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = True
    mock_update.return_value = ["Pushed updated branch for PR #99", "ACTION_FLAG:SKIP_ANALYSIS"]

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 99, "mergeable": False}

    actions = _handle_pr_merge(Mock(), "owner/repo", pr_data, config, {})

    # Verify that remediation flow was invoked and skip-analysis flag was set
    assert any("Starting mergeability remediation" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_mergeability_remediation_success_path(mock_update, mock_checkout, mock_check_progress, mock_check_status):
    """Verify remediation success path - checkout and update succeed."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = True
    mock_update.return_value = ["Determined base branch for PR #100: main", "Checked out PR #100 branch", "Pushed updated branch for PR #100", "ACTION_FLAG:SKIP_ANALYSIS"]

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 100, "mergeable": False}

    actions = _handle_pr_merge(Mock(), "owner/repo", pr_data, config, {})

    # Verify success
    assert any("Starting mergeability remediation" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    assert any("Mergeability remediation completed" in action for action in actions)
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
def test_mergeability_remediation_checkout_fails(mock_checkout, mock_check_progress, mock_check_status):
    """Verify remediation handles checkout failure."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = False

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 101, "mergeable": False}

    actions = _handle_pr_merge(Mock(), "owner/repo", pr_data, config, {})

    # Verify failure is handled
    assert any("Starting mergeability remediation" in action for action in actions)
    assert any("Failed to checkout" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_mergeability_remediation_update_fails(mock_update, mock_checkout, mock_check_progress, mock_check_status):
    """Verify remediation handles update failure."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = True
    mock_update.return_value = ["Failed to update: merge conflict resolution failed"]

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 102, "mergeable": False}

    actions = _handle_pr_merge(Mock(), "owner/repo", pr_data, config, {})

    # Verify failure is handled
    assert any("Starting mergeability remediation" in action for action in actions)
    assert any("Failed" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    mock_check_status.assert_not_called()
