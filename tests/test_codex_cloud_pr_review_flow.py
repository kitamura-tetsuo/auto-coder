"""Tests for assigning unresolved PR review work to Codex Cloud."""

from unittest.mock import MagicMock, patch

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import (
    AdversarialValidationEligibility,
    ClaimedReviewThreadGateState,
    CodexReviewState,
    _delegate_codex_cloud_review_thread_repair,
    _handle_pr_merge,
    _process_pr_for_merge,
)
from auto_coder.review_thread_validation import ClaimedReviewThread
from auto_coder.util.gh_cache import ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult


def _pr_data(head_sha: str = "head-1") -> dict:
    return {
        "number": 5262,
        "body": "Fixes #42\n\nhttps://chatgpt.com/codex/tasks/task_review_5262",
        "head": {"ref": "codex/review-5262", "sha": head_sha},
        "base": {"ref": "main", "sha": "base-1"},
        "labels": [],
        "user": {"login": "maintainer"},
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
    assert second == ["Codex Cloud review repair was already requested for PR #5262 for all current actionable feedback"]
    send_followup.assert_called_once()
    task_id, prompt = send_followup.call_args.args
    assert task_id == "task_review_5262"
    assert "existing pull request #5262" in prompt
    assert "codex/review-5262" in prompt
    assert "Do not create a new pull request." in prompt
    assert "Address only the newly delivered actionable reviewer feedback" in prompt
    assert "This misses the empty-input case" in prompt
    assert "<!-- auto-coder-review-addressed:v1 -->" in prompt
    assert state_path.exists()


def test_implementer_reply_and_head_change_do_not_create_new_repair_work(tmp_path) -> None:
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
            _pr_data("head-2"),
            unresolved_threads=(_thread("Updated clarification"),),
        )

    send_followup.assert_called_once()


def test_new_review_finding_is_delivered_without_redelivering_old_finding(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    second = ReviewThread(
        id="PRRT_thread_2",
        is_resolved=False,
        comments=[ReviewThreadComment(database_id=201, body="Handle whitespace-only input", author_login="reviewer")],
    )

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data(), unresolved_threads=(_thread(),))
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), unresolved_threads=(_thread(), second))
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), unresolved_threads=(_thread(), second))

    assert send_followup.call_count == 2
    second_prompt = send_followup.call_args.args[1]
    assert "Handle whitespace-only input" in second_prompt
    assert "This misses the empty-input case" not in second_prompt


def test_new_reviewer_reply_is_delivered_once_without_redelivering_thread_root(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    original = _thread("Implemented and tested")
    with_new_review = _thread("Implemented and tested")
    with_new_review.comments.append(ReviewThreadComment(database_id=103, body="Whitespace-only input is still broken", author_login="reviewer"))

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data(), unresolved_threads=(original,))
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), unresolved_threads=(with_new_review,))
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), unresolved_threads=(with_new_review,))

    assert send_followup.call_count == 2
    second_prompt = send_followup.call_args.args[1]
    assert "Whitespace-only input is still broken" in second_prompt
    assert "This misses the empty-input case" not in second_prompt
    assert "Implemented and tested" not in second_prompt


def test_failed_review_feedback_delivery_remains_retryable(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", side_effect=[False, True]) as send_followup,
    ):
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data(), unresolved_threads=(_thread(),))
        _delegate_codex_cloud_review_thread_repair("owner/repo", _pr_data(), unresolved_threads=(_thread(),))

    assert send_followup.call_count == 2
    assert state_path.exists()


def test_non_codex_pr_preserves_generic_review_gate() -> None:
    pr_data = _pr_data()
    pr_data["body"] = "Fixes #42"

    with patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send_followup:
        actions = _delegate_codex_cloud_review_thread_repair("owner/repo", pr_data)

    assert actions == []
    send_followup.assert_not_called()


def test_single_pr_merge_entry_validates_claimed_thread_without_redelegating() -> None:
    client = MagicMock()
    client.get_pull_request.return_value = {"head": {"sha": "head-1"}}
    claimed = ClaimedReviewThread(
        thread_id="PRRT_thread_1",
        root_comment_database_id=101,
        root_author_login="chatgpt-codex-connector[bot]",
        original_finding="This misses the empty-input case",
        discussion="agent: fixed and tested",
    )
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.MAX_ADVERSARIAL_VALIDATIONS = -1

    with (
        patch("auto_coder.pr_processor.GitHubClient.get_instance", return_value=client),
        patch("auto_coder.pr_processor.LabelManager") as label_manager,
        patch("auto_coder.pr_processor.retry_pending_stale_review_thread_rollbacks", return_value=[]),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(claimed=(claimed,)),
        ),
        patch(
            "auto_coder.pr_processor._get_adversarial_validation_eligibility",
            return_value=AdversarialValidationEligibility(issue_numbers=(42,)),
        ),
        patch("auto_coder.pr_processor._get_codex_review_state", return_value=CodexReviewState()),
        patch("auto_coder.pr_processor._get_published_adversarial_validation_status", return_value=(None, None)),
        patch("auto_coder.pr_processor.isolated_pr_head_worktree") as worktree,
        patch(
            "auto_coder.pr_processor.run_adversarial_validation",
            return_value=AdversarialValidationResult(result="PASS", summary="Pass", findings=[]),
        ) as run_validation,
        patch(
            "auto_coder.pr_processor.publish_adversarial_review",
            return_value=ReviewPublicationResult(True, "APPROVE", ""),
        ),
        patch("auto_coder.pr_processor._merge_pr", return_value=False),
        patch("auto_coder.pr_processor._delegate_codex_cloud_review_thread_repair") as delegate_review_repair,
    ):
        label_manager.return_value.__enter__.return_value = MagicMock()
        worktree.return_value.__enter__.return_value = "/tmp/worktree"
        result = _process_pr_for_merge("owner/repo", _pr_data(), config)

    assert any("claimed-addressed review thread" in action for action in result.actions_taken)
    assert any("Adversarial validation passed" in action for action in result.actions_taken)
    run_validation.assert_called_once()
    assert "PRRT_thread_1" in run_validation.call_args.kwargs["claimed_review_threads_section"]
    delegate_review_repair.assert_not_called()


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
        blocking_unresolved=(thread,),
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
