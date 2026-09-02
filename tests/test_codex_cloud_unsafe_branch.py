"""Regression tests for rejecting Codex Cloud's shared remote branch."""

from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.pr_processor import process_pull_request


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
        "body": "Closes #161\n\nhttps://chatgpt.com/codex/tasks/task_reissue_161",
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
    task_id, prompt = followup.call_args.args
    assert task_id == "task_reissue_161"
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


def test_automation_engine_routes_empty_work_pr_to_existing_cloud_task(
    config: AutomationConfig,
    github_client: MagicMock,
) -> None:
    """Candidate collection and worker dispatch must not consume the PR as generically empty."""
    config.CHECK_LABELS = False
    pr_data = codex_pr(changed_files=0)
    pr_data.update({"created_at": "2026-01-01T00:00:00Z", "labels": [], "mergeable": True})
    github_client.get_open_prs_json.return_value = [pr_data]
    github_client.get_open_issues_json.return_value = []
    engine = AutomationEngine(github_client, config=config)

    with (
        patch("auto_coder.util.github_action.preload_github_actions_status"),
        patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.util.github_action._check_github_actions_status", return_value=MagicMock(success=True)),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as followup,
        patch("auto_coder.pr_processor.increment_attempt") as increment,
        patch("auto_coder.pr_processor._release_issue_processing_label") as release_label,
    ):
        candidates = engine._get_candidates("owner/repo")
        assert len(candidates) == 1
        assert candidates[0].type == "pr"

        result = engine._process_single_candidate("owner/repo", candidates[0])

    assert result.success is True
    assert any("in-flight Codex Cloud reissue flow" in action for action in result.actions)
    github_client.close_pr.assert_called_once()
    followup.assert_called_once()
    increment.assert_not_called()
    release_label.assert_not_called()
