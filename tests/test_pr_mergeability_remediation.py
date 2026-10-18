"""Tests for mergeability detection and remediation scaffolding."""

from unittest.mock import Mock, patch

import pytest

pytestmark = pytest.mark.pr_repair_contract


def _client():
    """A Mock GitHub client pre-configured so the stale-review-thread
    rollback retry at the top of _handle_pr_merge is a no-op."""
    client = Mock()
    client.get_pr_review_threads_strict.return_value = []
    return client


from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import PRActionList, _get_mergeable_state, _handle_pr_merge, _start_mergeability_remediation
from src.auto_coder.pr_repair_dispatch import PRRepairResult, RepairDisposition


class _CloudFollowupClient:
    def __init__(self) -> None:
        self.messages = []

    def send_followup(self, task_id, message):
        self.messages.append((task_id, message))
        return True


@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
def test_non_mergeable_detection_is_reported(mock_check_progress):
    """Ensure non-mergeable PRs surface a detection action."""
    mock_check_progress.return_value = False
    config = AutomationConfig()
    pr_data = {"number": 42, "head": {"ref": "feature-42", "sha": "head-42"}, "mergeable": False}

    actions = _handle_pr_merge(_client(), "owner/repo", pr_data, config, {})

    assert any("not mergeable" in action.lower() for action in actions)


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_mergeability_remediation_flow_invoked(mock_github_client, mock_get_ghapi_client, mock_update, mock_checkout, mock_branch_manager, mock_check_progress, mock_check_status):
    """Verify remediation flow is activated when enabled."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = True
    mock_update.return_value = PRActionList(("legacy display text",))
    mock_update.return_value.repair_result = PRRepairResult(RepairDisposition.APPLIED)

    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 99, "head": {"ref": "feature-99", "sha": "head-99"}, "mergeable": False}

    actions = _handle_pr_merge(_client(), "owner/repo", pr_data, config, {})

    # Verify that remediation flow was invoked and skip-analysis flag was set
    assert any("Starting mergeability remediation" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch", return_value=True)
@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_production_remediation_preserves_cloud_pr_metadata_and_defers_after_delegation(
    mock_github_client,
    mock_get_ghapi_client,
    _mock_checkout,
    _mock_branch_manager,
    _mock_check_progress,
    mock_check_status,
    tmp_path,
):
    """The merge gate must carry API ownership/state through to cloud repair."""
    from src.auto_coder.pr_processor import _delegate_cloud_merge_conflict_repair

    cloud_client = _CloudFollowupClient()
    api_pr = {
        "number": 1660,
        "body": "Closes #1659\nTask task_e_existing",
        "user": {"login": "chatgpt-codex-connector"},
        "head": {"ref": "issue-1659-release-beta-channels", "sha": "H1"},
        "base": {"ref": "release/beta", "sha": "B1"},
    }
    mock_api = Mock()
    mock_api.pulls.get.return_value = api_pr
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    def conflicting_update(repo_name, preserved_pr_data, _config):
        assert preserved_pr_data["body"] == api_pr["body"]
        assert preserved_pr_data["head"] == api_pr["head"]
        assert preserved_pr_data["base"] == api_pr["base"]
        assert _delegate_cloud_merge_conflict_repair(repo_name, preserved_pr_data)
        actions = PRActionList(("Merge conflict detected for PR #1660", "Delegated merge-conflict repair for PR #1660 to its existing cloud session", "ACTION_FLAG:SKIP_ANALYSIS"))
        actions.repair_result = PRRepairResult(RepairDisposition.ACCEPTED_OR_UNCERTAIN)
        return actions

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    with (
        patch("src.auto_coder.pr_processor._update_with_base_branch", side_effect=conflicting_update),
        patch("src.auto_coder.pr_processor._resolve_cloud_conflict_origin", return_value=(cloud_client, "task_e_existing")),
        patch("src.auto_coder.pr_processor._cloud_conflict_state_path", return_value=tmp_path / "repairs.json"),
    ):
        actions = _handle_pr_merge(_client(), "owner/repo", {**api_pr, "mergeable": False}, config, {})

    assert len(cloud_client.messages) == 1
    assert "release/beta" in cloud_client.messages[0][1]
    assert "issue-1659-release-beta-channels" in cloud_client.messages[0][1]
    assert any("remediation delegated" in action.lower() and "later pass" in action for action in actions)
    assert not any("remediation completed" in action.lower() for action in actions)
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_mergeability_remediation_success_path(mock_github_client, mock_get_ghapi_client, mock_update, mock_checkout, mock_branch_manager, mock_check_progress, mock_check_status):
    """Verify remediation success path - checkout and update succeed."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = True
    mock_update.return_value = PRActionList(("misleading display text",))
    mock_update.return_value.repair_result = PRRepairResult(RepairDisposition.APPLIED)

    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 100, "head": {"ref": "feature-100", "sha": "head-100"}, "mergeable": False}

    actions = _handle_pr_merge(_client(), "owner/repo", pr_data, config, {})

    # Verify success
    assert any("Starting mergeability remediation" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    assert any("Mergeability remediation completed" in action for action in actions)
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_mergeability_remediation_checkout_fails(mock_github_client, mock_get_ghapi_client, mock_checkout, mock_check_progress, mock_check_status):
    """Verify remediation handles checkout failure."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = False

    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 101, "head": {"ref": "feature-101", "sha": "head-101"}, "mergeable": False}

    actions = _handle_pr_merge(_client(), "owner/repo", pr_data, config, {})

    # Verify failure is handled
    assert any("Starting mergeability remediation" in action for action in actions)
    assert any("Failed to prepare PR #101 branch" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor._check_github_actions_status")
@patch("src.auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_mergeability_remediation_update_fails(mock_github_client, mock_get_ghapi_client, mock_update, mock_checkout, mock_branch_manager, mock_check_progress, mock_check_status):
    """Verify remediation handles update failure."""
    mock_check_progress.return_value = True
    mock_checkout.return_value = True
    mock_update.return_value = PRActionList(("Pushed updated branch for PR #102", "ACTION_FLAG:SKIP_ANALYSIS"))
    mock_update.return_value.repair_result = PRRepairResult(RepairDisposition.FAILED, reason="merge conflict resolution failed")

    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    config = AutomationConfig()
    config.ENABLE_MERGEABILITY_REMEDIATION = True
    pr_data = {"number": 102, "head": {"ref": "feature-102", "sha": "head-102"}, "mergeable": False}

    actions = _handle_pr_merge(_client(), "owner/repo", pr_data, config, {})

    # Verify failure is handled
    assert any("Starting mergeability remediation" in action for action in actions)
    assert any("remediation failed" in action.lower() for action in actions)
    assert not any("remediation completed" in action.lower() for action in actions)
    mock_check_status.assert_not_called()


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_get_mergeable_state_uses_existing_data(mock_github_client, mock_get_ghapi_client):
    """Verify mergeable state uses existing data when available."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    config = AutomationConfig()
    pr_data = {"mergeable": True, "mergeStateStatus": "CLEAN"}

    result = _get_mergeable_state("owner/repo", pr_data, config)

    assert result["mergeable"] is True
    assert result["merge_state_status"] == "CLEAN"
    # Should not call GitHub API when mergeable is not None
    mock_api.pulls.get.assert_not_called()


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_get_mergeable_state_refreshes_when_unknown(mock_github_client, mock_get_ghapi_client):
    """Verify mergeable state is refreshed when current value is unknown."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    mock_api.pulls.get.return_value = {"mergeable": False, "mergeStateStatus": "DIRTY"}

    config = AutomationConfig()
    pr_data = {"mergeable": None, "mergeStateStatus": None}

    result = _get_mergeable_state("owner/repo", pr_data, config)

    assert result["mergeable"] is False
    assert result["merge_state_status"] == "DIRTY"
    # Verify GitHub API was called
    mock_api.pulls.get.assert_called_once()


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_get_mergeable_state_handles_api_failure(mock_github_client, mock_get_ghapi_client):
    """Verify mergeable state handles API failure gracefully."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    mock_api.pulls.get.side_effect = Exception("API error")

    config = AutomationConfig()
    pr_data = {"mergeable": None, "mergeStateStatus": None}

    # Should not raise exception
    result = _get_mergeable_state("owner/repo", pr_data, config)

    assert result["mergeable"] is None
    assert result["merge_state_status"] is None


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_success(mock_update, mock_checkout, mock_branch_manager, mock_github_client, mock_get_ghapi_client):
    """Verify successful mergeability remediation flow."""
    # Mock API
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock PR details retrieval
    mock_api.pulls.get.return_value = {"base": {"ref": "develop"}, "head": {"ref": "feature-branch"}}

    # Mock successful checkout and update
    mock_checkout.return_value = True
    mock_update.return_value = PRActionList(("display text does not authorize completion",))
    mock_update.return_value.repair_result = PRRepairResult(RepairDisposition.APPLIED)

    actions = _start_mergeability_remediation(200, "DIRTY", repo_name="owner/repo")

    assert any("Starting mergeability remediation for PR #200" in action for action in actions)
    assert any("Determined base branch: develop, head branch: feature-branch" in action for action in actions)
    assert any("Checked out PR #200 branch" in action for action in actions)
    assert any("Mergeability remediation completed for PR #200" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" in actions
    assert "Failed" not in str(actions)


@pytest.mark.parametrize(
    ("disposition", "expected_text"),
    [
        (RepairDisposition.NO_UPDATE_NEEDED, "found no base update needed"),
        (RepairDisposition.FAILED, "remediation failed"),
    ],
)
@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch", return_value=True)
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_remediation_uses_typed_result_despite_identical_misleading_actions(mock_update, _checkout, _branch_manager, mock_github_client, mock_get_ghapi_client, disposition, expected_text):
    """REQ-003: display flags cannot change the remediation transition."""
    mock_api = Mock()
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature"}}
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"
    produced = PRActionList(("Pushed updated branch", "up to date with main branch", "ACTION_FLAG:SKIP_ANALYSIS", "ACTION_FLAG:DEGRADING_MERGE_SKIP_MERGE"))
    produced.repair_result = PRRepairResult(disposition, reason="comparison failed")
    mock_update.return_value = produced

    actions = _start_mergeability_remediation(2040, "DIRTY", repo_name="owner/repo")

    assert mock_update.call_count == 1
    assert expected_text in " ".join(actions).lower()
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    assert ("remediation completed" in " ".join(actions).lower()) is False
    mock_github_client.get_instance.return_value.close_pr.assert_not_called()


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch", return_value=True)
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_remediation_closes_only_for_typed_quality_refusal(mock_update, _checkout, _branch, mock_github, mock_api_factory):
    """REQ-003: typed quality refusal, not its display flag, owns closure."""
    api = Mock()
    api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature"}}
    mock_api_factory.return_value = api
    client = mock_github.get_instance.return_value
    client.token = "token"
    produced = PRActionList(("ordinary diagnostic",))
    produced.repair_result = PRRepairResult(RepairDisposition.QUALITY_REFUSED, reason="quality degradation")
    mock_update.return_value = produced

    actions = _start_mergeability_remediation(2041, "DIRTY", repo_name="owner/repo")

    mock_update.assert_called_once()
    client.close_pr.assert_called_once()
    assert any("quality degradation risk" in action for action in actions)


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_start_mergeability_remediation_pr_details_fails(mock_github_client, mock_get_ghapi_client):
    """Verify remediation handles PR details retrieval failure."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock PR details retrieval failure
    mock_api.pulls.get.side_effect = Exception("PR not found")

    actions = _start_mergeability_remediation(201, "UNKNOWN", repo_name="owner/repo")

    assert any("Starting mergeability remediation for PR #201" in action for action in actions)
    assert any("Failed to get PR #201 details via GhApi" in action for action in actions)
    assert not any("remediation completed" in action.lower() for action in actions)


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_checkout_fails(mock_update, mock_checkout, mock_github_client, mock_get_ghapi_client):
    """Verify remediation handles checkout failure."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock PR details retrieval success
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    # Mock failed checkout
    mock_checkout.return_value = False
    mock_update.return_value = []

    actions = _start_mergeability_remediation(202, "CLEAN", repo_name="owner/repo")

    assert any("Starting mergeability remediation for PR #202" in action for action in actions)
    assert any("Failed to prepare PR #202 branch" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
    # Update should not be called when checkout fails
    mock_update.assert_not_called()


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_update_fails(mock_update, mock_checkout, mock_branch_manager, mock_github_client, mock_get_ghapi_client):
    """Verify remediation handles update failure."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock PR details retrieval success
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    # Mock successful checkout but failed update
    mock_checkout.return_value = True
    mock_update.return_value = PRActionList(("Pushed updated branch for PR #203", "ACTION_FLAG:SKIP_ANALYSIS"))
    mock_update.return_value.repair_result = PRRepairResult(RepairDisposition.FAILED, reason="merge conflict resolution failed")

    actions = _start_mergeability_remediation(203, "UNKNOWN", repo_name="owner/repo")

    assert any("Starting mergeability remediation for PR #203" in action for action in actions)
    assert any("Mergeability remediation failed for PR #203" in action for action in actions)
    assert not any("remediation completed" in action.lower() for action in actions)


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
@patch("src.auto_coder.pr_processor.BranchManager")
@patch("src.auto_coder.pr_processor._checkout_pr_branch")
@patch("src.auto_coder.pr_processor._update_with_base_branch")
def test_start_mergeability_remediation_handles_exception(mock_update, mock_checkout, mock_branch_manager, mock_github_client, mock_get_ghapi_client):
    """Verify remediation handles unexpected exceptions."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock PR details retrieval
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {"ref": "feature-branch"}}

    # Make checkout raise an exception
    mock_checkout.side_effect = RuntimeError("Checkout failed unexpectedly")

    actions = _start_mergeability_remediation(204, "UNKNOWN", repo_name="owner/repo")

    assert any("Starting mergeability remediation for PR #204" in action for action in actions)
    assert any("Error during mergeability remediation" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_start_mergeability_remediation_parses_base_branch_fallback(mock_github_client, mock_get_ghapi_client):
    """Verify remediation handles API failure gracefully."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock API failure for PR details
    mock_api.pulls.get.side_effect = Exception("API error")

    # Mock checkout failure (any failure is fine, we just want to check the fallback)
    with patch("src.auto_coder.pr_processor._checkout_pr_branch", return_value=False):
        actions = _start_mergeability_remediation(205, "UNKNOWN", repo_name="owner/repo")

        # The remediation loop should fail to get details
        assert any("Failed to get PR #205 details" in action for action in actions)


@patch("src.auto_coder.pr_processor.get_ghapi_client")
@patch("src.auto_coder.pr_processor.GitHubClient")
def test_start_mergeability_remediation_missing_head_ref(mock_github_client, mock_get_ghapi_client):
    """Verify remediation errors out if head ref is missing."""
    mock_api = Mock()
    mock_get_ghapi_client.return_value = mock_api
    mock_github_client.get_instance.return_value.token = "token"

    # Mock PR details with missing head ref
    mock_api.pulls.get.return_value = {"base": {"ref": "main"}, "head": {}}

    actions = _start_mergeability_remediation(206, "UNKNOWN", repo_name="owner/repo")

    assert any("Failed to determine head branch for PR #206" in action for action in actions)
    assert "ACTION_FLAG:SKIP_ANALYSIS" not in actions
