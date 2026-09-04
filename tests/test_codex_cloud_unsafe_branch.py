"""Regression tests for rejecting Codex Cloud's shared remote branch."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.pr_processor import _handle_pr_merge, _process_pr_for_fixes, _process_pr_for_merge, process_pull_request
from auto_coder.util.gh_cache import GitHubClient


@pytest.fixture
def config() -> AutomationConfig:
    return AutomationConfig()


@pytest.fixture
def github_client() -> MagicMock:
    client = MagicMock()
    client.get_issue.return_value = {"state": "open"}
    return client


def codex_pr(*, branch: str = "work", changed_files: int = 3) -> dict:
    return {
        "number": 162,
        "title": "Fix issue #161",
        "body": "Closes #161\n\nhttps://chatgpt.com/codex/tasks/task_e_reissue161",
        "user": {"login": "chatgpt-codex-connector[bot]"},
        "head": {"ref": branch, "sha": "abc123"},
        "base": {"ref": "main", "sha": "base123"},
        "changed_files": changed_files,
        "state": "open",
    }


@pytest.mark.parametrize("changed_files", [0, 22])
def test_work_pr_is_closed_and_existing_task_reissues_without_local_processing(
    config: AutomationConfig,
    github_client: MagicMock,
    changed_files: int,
) -> None:
    pr_data = codex_pr(changed_files=changed_files)

    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as followup,
        patch("auto_coder.pr_processor._close_empty_pr") as empty_check,
        patch("auto_coder.pr_processor._process_pr_for_fixes") as normal_processing,
        patch("auto_coder.pr_processor.increment_attempt") as increment,
        patch("auto_coder.pr_processor._release_issue_processing_label") as release_label,
    ):
        result = process_pull_request(github_client, config, "owner/repo", pr_data)

    github_client.close_pr.assert_called_once()
    followup.assert_called_once()
    task_id, prompt = followup.call_args.args[:2]
    assert task_id == "task_e_reissue161"
    assert "unique to this task or its linked issue" in prompt
    assert "Do not reuse, update, push to, or force-push the remote `work` branch" in prompt
    assert "Do not reopen, retarget, or reuse the closed pull request" in prompt
    empty_check.assert_not_called()
    normal_processing.assert_not_called()
    increment.assert_not_called()
    release_label.assert_not_called()
    assert result.priority == "close"
    assert any("in-flight Codex Cloud reissue flow" in action for action in result.actions_taken)


def test_failed_reissue_delivery_releases_issue_to_retry_policy(
    config: AutomationConfig,
    github_client: MagicMock,
) -> None:
    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=False),
        patch("auto_coder.pr_processor.increment_attempt", return_value=4) as increment,
        patch("auto_coder.pr_processor._release_issue_processing_label", return_value=True) as release_label,
    ):
        result = process_pull_request(github_client, config, "owner/repo", codex_pr())

    increment.assert_called_once_with("owner/repo", 161)
    release_label.assert_called_once_with(github_client, "owner/repo", 161, config)
    assert "Incremented attempt for issue #161 to 4" in result.actions_taken
    assert "Removed @auto-coder label from issue #161" in result.actions_taken


def test_task_specific_codex_branch_and_non_codex_work_branch_are_not_rejected(
    config: AutomationConfig,
    github_client: MagicMock,
) -> None:
    local_pr = codex_pr()
    local_pr["body"] = "Closes #161"
    local_pr["user"] = {"login": "developer"}

    for pr_data in (codex_pr(branch="issue-161-parser-fix"), local_pr):
        with patch("auto_coder.pr_processor._close_empty_pr") as empty_check:
            empty_check.return_value.closed = True
            empty_check.return_value.actions = ["stopped after safety gate"]
            process_pull_request(github_client, config, "owner/repo", pr_data)

        empty_check.assert_called_once()

    github_client.close_pr.assert_not_called()


@pytest.mark.parametrize("changed_files", [0, 22])
def test_automation_engine_rejects_work_pr_before_label_and_in_progress_ci_gates(
    config: AutomationConfig,
    github_client: MagicMock,
    changed_files: int,
) -> None:
    """Candidate collection and worker dispatch must not consume the PR as generically empty."""
    pr_data = codex_pr(changed_files=changed_files)
    pr_data.update({"created_at": "2026-01-01T00:00:00Z", "labels": [], "mergeable": True})
    github_client.get_open_prs_json.return_value = [pr_data]
    github_client.get_open_issues_json.return_value = []
    engine = AutomationEngine(github_client, config=config)

    with (
        patch("auto_coder.util.github_action.preload_github_actions_status") as preload_ci,
        patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress", return_value=False) as ci_gate,
        patch("auto_coder.util.github_action._check_github_actions_status", return_value=MagicMock(success=True)),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as followup,
        patch("auto_coder.automation_engine.LabelManager") as label_gate,
        patch("auto_coder.pr_processor.increment_attempt") as increment,
        patch("auto_coder.pr_processor._release_issue_processing_label") as release_label,
    ):
        candidates = engine._get_candidates("owner/repo")

    assert candidates == []
    github_client.close_pr.assert_called_once()
    followup.assert_called_once()
    preload_ci.assert_called_once_with("owner/repo", [])
    label_gate.assert_not_called()
    ci_gate.assert_not_called()
    increment.assert_not_called()
    release_label.assert_not_called()


def test_lower_merge_boundary_rejects_unsafe_work_before_all_processing_side_effects(
    config: AutomationConfig,
    github_client: MagicMock,
) -> None:
    """The production merge/review choke point must enforce the invariant itself."""
    pr_data = codex_pr()

    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as followup,
        patch("auto_coder.pr_processor.run_adversarial_validation") as adversarial,
        patch("auto_coder.pr_processor.retry_pending_stale_review_thread_rollbacks") as review_gate,
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress") as ci_gate,
        patch("auto_coder.pr_processor._get_mergeable_state") as mergeability,
        patch("auto_coder.pr_processor._merge_pr") as merge,
        patch("auto_coder.pr_processor.git_checkout_branch") as checkout,
        patch("auto_coder.pr_processor._send_codex_cloud_error_feedback") as ci_repair,
        patch("auto_coder.pr_processor._delegate_cloud_merge_conflict_repair") as conflict_repair,
    ):
        actions = _handle_pr_merge(github_client, "owner/repo", pr_data, config, {})

    assert actions == [
        "Closed unsafe Codex Cloud PR #162 on shared remote branch 'work'",
        "Requested Codex Cloud task 'task_e_reissue161' to publish a replacement PR from a task-specific branch",
        "Preserved issue #161 in the in-flight Codex Cloud reissue flow",
    ]
    github_client.close_pr.assert_called_once()
    followup.assert_called_once()
    for forbidden in (adversarial, review_gate, ci_gate, mergeability, merge, checkout, ci_repair, conflict_repair):
        forbidden.assert_not_called()


def test_reduced_metadata_is_strictly_refreshed_before_processing(
    config: AutomationConfig,
) -> None:
    """A production-origin reduced PR object cannot hide Codex origin or head."""

    class ReducedMetadataClient:
        def __init__(self) -> None:
            self.close_pr = MagicMock()
            self.get_issue = MagicMock(return_value={"state": "open"})
            self.strict_calls = 0

        def get_pull_request_metadata_strict(self, repo_name: str, pr_number: int) -> dict:
            assert (repo_name, pr_number) == ("owner/repo", 162)
            self.strict_calls += 1
            return codex_pr()

    client = ReducedMetadataClient()
    reduced_pr = {"number": 162, "title": "issue-like cached representation"}

    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as followup,
        patch("auto_coder.pr_processor._close_empty_pr") as empty_check,
        patch("auto_coder.pr_processor.run_adversarial_validation") as adversarial,
    ):
        result = process_pull_request(client, config, "owner/repo", reduced_pr)

    assert client.strict_calls == 1
    client.close_pr.assert_called_once()
    followup.assert_called_once()
    empty_check.assert_not_called()
    adversarial.assert_not_called()
    assert result.priority == "close"


def test_reduced_metadata_lookup_failure_fails_closed(
    config: AutomationConfig,
) -> None:
    class FailingMetadataClient:
        def get_pull_request_metadata_strict(self, repo_name: str, pr_number: int) -> dict:
            raise RuntimeError("metadata unavailable")

    with (
        patch("auto_coder.pr_processor._close_empty_pr") as empty_check,
        patch("auto_coder.pr_processor.run_adversarial_validation") as adversarial,
    ):
        result = process_pull_request(FailingMetadataClient(), config, "owner/repo", {"number": 162})

    assert result.outcome.value == "deferred"
    assert result.actions_taken == ["Skipping PR #162: authoritative branch safety could not be established (metadata unavailable)"]
    empty_check.assert_not_called()
    adversarial.assert_not_called()


def test_cached_task_branch_is_rejected_when_live_head_is_work(
    config: AutomationConfig,
) -> None:
    client = GitHubClient(token="test")
    cached = codex_pr(branch="issue-161-stale")
    client.get_pull_request_metadata_strict = MagicMock(return_value=codex_pr(branch="work"))
    client.close_pr = MagicMock()
    client.get_issue = MagicMock(return_value={"state": "open"})

    with (
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True),
        patch("auto_coder.pr_processor.retry_pending_stale_review_thread_rollbacks") as review_gate,
        patch("auto_coder.pr_processor.run_adversarial_validation") as adversarial,
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress") as ci_gate,
    ):
        actions = _handle_pr_merge(client, "owner/repo", cached, config, {})

    client.get_pull_request_metadata_strict.assert_called_once_with("owner/repo", 162)
    client.close_pr.assert_called_once()
    assert actions[0] == "Closed unsafe Codex Cloud PR #162 on shared remote branch 'work'"
    review_gate.assert_not_called()
    adversarial.assert_not_called()
    ci_gate.assert_not_called()


def test_connector_authored_work_pr_without_task_url_stops_at_lower_boundary(
    config: AutomationConfig,
) -> None:
    """Authoritative connector identity alone establishes Codex Cloud origin."""
    client = GitHubClient(token="test")
    authoritative = codex_pr()
    authoritative["body"] = "Closes #161"
    client.get_pull_request_metadata_strict = MagicMock(return_value=authoritative)
    client.close_pr = MagicMock()
    client.get_issue = MagicMock(return_value={"state": "open"})

    with (
        patch("auto_coder.pr_processor._resolve_codex_cloud_task_id", return_value="task_e_reissue161"),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as followup,
        patch("auto_coder.pr_processor.retry_pending_stale_review_thread_rollbacks") as review_gate,
        patch("auto_coder.pr_processor.run_adversarial_validation") as adversarial,
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress") as ci_gate,
        patch("auto_coder.pr_processor._get_mergeable_state") as mergeability,
        patch("auto_coder.pr_processor.git_checkout_branch") as checkout,
        patch("auto_coder.pr_processor._merge_pr") as merge,
        patch("auto_coder.pr_processor._send_codex_cloud_error_feedback") as ci_repair,
        patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair") as review_repair,
        patch("auto_coder.pr_processor._delegate_cloud_merge_conflict_repair") as conflict_repair,
    ):
        actions = _handle_pr_merge(client, "owner/repo", codex_pr(branch="cached-safe"), config, {})

    client.get_pull_request_metadata_strict.assert_called_once_with("owner/repo", 162)
    client.close_pr.assert_called_once()
    followup.assert_called_once()
    assert actions[0] == "Closed unsafe Codex Cloud PR #162 on shared remote branch 'work'"
    for forbidden in (review_gate, adversarial, ci_gate, mergeability, checkout, merge, ci_repair, review_repair, conflict_repair):
        forbidden.assert_not_called()


def test_candidate_collection_preserves_live_task_branch_when_cache_says_work(
    config: AutomationConfig,
) -> None:
    client = GitHubClient(token="test")
    cached = codex_pr(branch="work")
    cached.update({"created_at": "2026-01-01T00:00:00Z", "labels": [], "mergeable": True})
    live = {**cached, "head": {**cached["head"], "ref": "issue-161-fixed"}}
    client.get_open_prs_json = MagicMock(return_value=[cached])
    client.get_open_issues_json = MagicMock(return_value=[])
    client.get_pull_request_metadata_strict = MagicMock(return_value=live)
    client.close_pr = MagicMock()
    engine = AutomationEngine(client, config=config)

    with (
        patch("auto_coder.util.github_action.preload_github_actions_status") as preload,
        patch("auto_coder.util.github_action._check_github_actions_status", return_value=MagicMock(success=False)),
        patch("auto_coder.pr_processor._close_empty_pr", return_value=MagicMock(closed=False)),
    ):
        candidates = engine._get_candidates("owner/repo")

    client.get_pull_request_metadata_strict.assert_called_once_with("owner/repo", 162)
    assert not client.close_pr.called
    preload.assert_called_once()
    assert preload.call_args.args[1][0]["head"]["ref"] == "issue-161-fixed"


@pytest.mark.parametrize("wrapper", ["merge", "fix"])
def test_production_wrappers_reject_before_label_or_progress(
    config: AutomationConfig,
    wrapper: str,
) -> None:
    client = GitHubClient(token="test")
    client.get_pull_request_metadata_strict = MagicMock(return_value=codex_pr())
    client.close_pr = MagicMock()
    client.get_issue = MagicMock(return_value={"state": "open"})

    with (
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True),
        patch("auto_coder.pr_processor.LabelManager") as label_manager,
        patch("auto_coder.pr_processor.ProgressStage") as progress_stage,
        patch("auto_coder.pr_processor._handle_pr_merge") as merge_handler,
        patch("auto_coder.pr_processor._take_pr_actions") as fix_handler,
    ):
        if wrapper == "merge":
            result = _process_pr_for_merge("owner/repo", codex_pr(branch="cached-branch"), config)
        else:
            result = _process_pr_for_fixes(client, "owner/repo", codex_pr(branch="cached-branch"), config)

    assert result.priority == "close"
    client.close_pr.assert_called_once()
    label_manager.assert_not_called()
    progress_stage.assert_not_called()
    merge_handler.assert_not_called()
    fix_handler.assert_not_called()
