"""Tests for mergeability detection and remediation scaffolding."""

from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _get_mergeable_state, _handle_pr_merge, _start_mergeability_remediation


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


@patch("src.auto_coder.pr_processor.get_gh_logger")
def test_get_mergeable_state_uses_existing_data(mock_gh_logger):
    """Verify mergeable state uses existing data when available."""
    mock_result = Mock()
    mock_result.success = False  # GitHub API call not needed
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_result

    config = AutomationConfig()
    pr_data = {"mergeable": True, "mergeStateStatus": "CLEAN"}

    result = _get_mergeable_state("owner/repo", pr_data, config)

    assert result["mergeable"] is True
    assert result["merge_state_status"] == "CLEAN"
    # Should not call GitHub API when mergeable is not None
    mock_gh_logger.return_value.execute_with_logging.assert_not_called()


@patch("src.auto_coder.pr_processor.get_gh_logger")
def test_get_mergeable_state_refreshes_when_unknown(mock_gh_logger):
    """Verify mergeable state is refreshed when current value is unknown."""
    mock_result = Mock()
    mock_result.success = True
    mock_result.stdout = '{"mergeable": false, "mergeStateStatus": "DIRTY"}'
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_result

    config = AutomationConfig()
    pr_data = {"mergeable": None, "mergeStateStatus": None}

    result = _get_mergeable_state("owner/repo", pr_data, config)

    assert result["mergeable"] is False
    assert result["merge_state_status"] == "DIRTY"
    # Verify GitHub API was called
    mock_gh_logger.return_value.execute_with_logging.assert_called_once()


@patch("src.auto_coder.pr_processor.get_gh_logger")
def test_get_mergeable_state_handles_api_failure(mock_gh_logger):
    """Verify mergeable state handles API failure gracefully."""
    mock_result = Mock()
    mock_result.success = False
    mock_result.stderr = "API error"
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_result

    config = AutomationConfig()
    pr_data = {"mergeable": None, "mergeStateStatus": None}

    # Should not raise exception
    result = _get_mergeable_state("owner/repo", pr_data, config)

    assert result["mergeable"] is None
    assert result["merge_state_status"] is None


@patch("src.auto_coder.pr_processor.get_gh_logger")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_success(mock_update, mock_checkout, mock_gh_logger):
    """Verify successful mergeability remediation flow."""
    # Mock PR details retrieval
    mock_pr_result = Mock()
    mock_pr_result.success = True
    mock_pr_result.stdout = '{"base": {"ref": "develop"}}'
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_pr_result

    # Mock successful checkout and update
    mock_checkout.return_value = True
    mock_update.return_value = ["Pushed updated branch for PR #200", "ACTION_FLAG:SKIP_ANALYSIS"]

    actions = _start_mergeability_remediation(200, "DIRTY")

    assert any("Starting mergeability remediation for PR #200" in action for action in actions)
    assert any("Determined base branch for PR #200: develop" in action for action in actions)
    assert any("Checked out PR #200 branch" in action for action in actions)
    assert any("Mergeability remediation completed for PR #200" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    assert "Failed" not in str(actions)


@patch("src.auto_coder.pr_processor.get_gh_logger")
def test_start_mergeability_remediation_pr_details_fails(mock_gh_logger):
    """Verify remediation handles PR details retrieval failure."""
    # Mock PR details retrieval failure
    mock_pr_result = Mock()
    mock_pr_result.success = False
    mock_pr_result.stderr = "PR not found"
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_pr_result

    actions = _start_mergeability_remediation(201, "UNKNOWN")

    assert any("Starting mergeability remediation for PR #201" in action for action in actions)
    assert any("Failed to get PR #201 details" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions


@patch("src.auto_coder.pr_processor.get_gh_logger")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_checkout_fails(mock_update, mock_checkout, mock_gh_logger):
    """Verify remediation handles checkout failure."""
    # Mock PR details retrieval success
    mock_pr_result = Mock()
    mock_pr_result.success = True
    mock_pr_result.stdout = '{"base": {"ref": "main"}}'
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_pr_result

    # Mock failed checkout
    mock_checkout.return_value = False
    mock_update.return_value = []

    actions = _start_mergeability_remediation(202, "CLEAN")

    assert any("Starting mergeability remediation for PR #202" in action for action in actions)
    assert any("Failed to checkout PR #202 branch" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    # Update should not be called when checkout fails
    mock_update.assert_not_called()


@patch("src.auto_coder.pr_processor.get_gh_logger")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_update_fails(mock_update, mock_checkout, mock_gh_logger):
    """Verify remediation handles update failure."""
    # Mock PR details retrieval success
    mock_pr_result = Mock()
    mock_pr_result.success = True
    mock_pr_result.stdout = '{"base": {"ref": "main"}}'
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_pr_result

    # Mock successful checkout but failed update
    mock_checkout.return_value = True
    mock_update.return_value = ["Failed to merge: merge conflict resolution failed"]

    actions = _start_mergeability_remediation(203, "UNKNOWN")

    assert any("Starting mergeability remediation for PR #203" in action for action in actions)
    assert any("Mergeability remediation failed for PR #203" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions


@patch("src.auto_coder.pr_processor.get_gh_logger")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_handles_exception(mock_update, mock_checkout, mock_gh_logger):
    """Verify remediation handles unexpected exceptions."""
    # Mock PR details retrieval
    mock_pr_result = Mock()
    mock_pr_result.success = True
    mock_pr_result.stdout = '{"base": {"ref": "main"}}'
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_pr_result

    # Make checkout raise an exception
    mock_checkout.side_effect = RuntimeError("Checkout failed unexpectedly")

    actions = _start_mergeability_remediation(204, "UNKNOWN")

    assert any("Starting mergeability remediation for PR #204" in action for action in actions)
    assert any("Error during mergeability remediation" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions


@patch("src.auto_coder.pr_processor.get_gh_logger")
def test_start_mergeability_remediation_parses_base_branch_fallback(mock_gh_logger):
    """Verify remediation falls back to 'main' when JSON parsing fails."""
    # Mock PR details with invalid JSON response
    mock_pr_result = Mock()
    mock_pr_result.success = True
    mock_pr_result.stdout = "not valid json"
    mock_gh_logger.return_value.execute_with_logging.return_value = mock_pr_result

    # Mock checkout failure (any failure is fine, we just want to check the fallback)
    with patch("src.auto_coder.pr_processor._checkout_pr_branch", return_value=False):
        actions = _start_mergeability_remediation(205, "UNKNOWN")

        # Should have determined base branch as 'main' despite JSON parsing exception
        assert any("Determined base branch for PR #205: main" in action for action in actions)
