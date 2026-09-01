"""Tests for assigning unresolved PR review work to Codex Cloud."""

from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import (
    ClaimedReviewThreadGateState,
    _delegate_codex_cloud_review_thread_repair,
    _handle_pr_merge,
)
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult


def _pr_data(head_sha: str = "head-1") -> dict:
    return {
        "number": 5262,
        "body": "Fixes #42\n\nhttps://chatgpt.com/codex/tasks/task_review_5262",
        "head": {"ref": "codex/review-5262", "sha": head_sha},
        "base": {"ref": "main", "sha": "base-1"},
        "labels": [],
    }


def _thread(reply: str = "Please cover the edge case") -> ReviewThread:
    return ReviewThread(
        id="PRRT_thread_1",
        is_resolved=False,
        comments=[
            ReviewThreadComment(
                database_id=101,
                body="This misses the empty-input case",
                author_login="reviewer",
            ),
            ReviewThreadComment(database_id=102, body=reply, author_login="maintainer"),
        ],
    )


def test_review_repair_uses_existing_task_and_deduplicates_unchanged_state(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        first = _delegate_codex_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            unresolved_threads=(_thread(),),
        )
        second = _delegate_codex_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            unresolved_threads=(_thread(),),
        )

    assert first == ["Requested Codex Cloud task 'task_review_5262' to address unresolved review threads for PR #5262"]
    assert second == ["Codex Cloud review repair was already requested for PR #5262 at the current state"]
    send_followup.assert_called_once()
    task_id, prompt = send_followup.call_args.args
    assert task_id == "task_review_5262"
    assert "existing pull request #5262" in prompt
    assert "codex/review-5262" in prompt
    assert "Do not create a new pull request." in prompt
    assert "Review every currently unresolved GitHub review thread" in prompt
    assert "<!-- auto-coder-review-addressed:v1 -->" in prompt
    assert state_path.exists()


def test_changed_review_discussion_can_be_delegated_again(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_codex_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            unresolved_threads=(_thread("First clarification"),),
        )
        _delegate_codex_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            unresolved_threads=(_thread("Updated clarification"),),
        )

    assert send_followup.call_count == 2


def test_non_codex_pr_preserves_generic_review_gate() -> None:
    pr_data = _pr_data()
    pr_data["body"] = "Fixes #42"

    with patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send_followup:
        actions = _delegate_codex_cloud_review_thread_repair("owner/repo", pr_data)

    assert actions == []
    send_followup.assert_not_called()


@patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True)
@patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"})
@patch("auto_coder.pr_processor._check_github_actions_status")
@patch("auto_coder.pr_processor._get_claimed_review_thread_state")
@patch("auto_coder.pr_processor._delegate_codex_cloud_review_thread_repair")
def test_passing_codex_pr_delegates_blocking_review_threads(
    delegate_review_repair,
    claimed_state,
    checks,
    _mergeable,
    _continue,
) -> None:
    thread = _thread()
    checks.return_value = GitHubActionsStatusResult(success=True, ids=[1])
    claimed_state.return_value = ClaimedReviewThreadGateState(
        unresolved=(thread,),
        has_blocking_unresolved=True,
    )
    delegate_review_repair.return_value = ["Requested Codex Cloud task 'task_review_5262' to address unresolved review threads for PR #5262"]
    config = AutomationConfig()
    config.AUTO_MERGE = True
    client = MagicMock()

    actions = _handle_pr_merge(client, "owner/repo", _pr_data(), config, {})

    assert actions == [
        "All GitHub Actions checks passed for PR #5262",
        "Skipping merge for PR #5262 due to unresolved review threads",
        "Requested Codex Cloud task 'task_review_5262' to address unresolved review threads for PR #5262",
    ]
    delegate_review_repair.assert_called_once_with(
        "owner/repo",
        _pr_data(),
        github_client=client,
        unresolved_threads=(thread,),
    )
