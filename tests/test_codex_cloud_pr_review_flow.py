"""Tests for assigning unresolved PR review work to Codex Cloud."""

import json
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig, ProcessedPRResult, PRProcessingOutcome
from auto_coder.cloud_manager import CloudTaskBinding
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.pr_processor import (
    AdversarialValidationEligibility,
    ClaimedReviewThreadGateState,
    CloudReviewRepairResult,
    CodexReviewState,
    _cloud_review_feedback_markers,
    _delegate_cloud_review_thread_repair,
    _handle_pr_merge,
    _process_pr_for_merge,
    _review_feedback_identity,
    _take_pr_actions,
)
from auto_coder.review_thread_validation import ClaimedReviewThread
from auto_coder.util.gh_cache import PullRequestRepairMetadata, ReviewThread, ReviewThreadComment
from auto_coder.util.github_action import GitHubActionsStatusResult


@pytest.fixture(autouse=True)
def durable_codex_cloud_origin():
    """Create the durable ownership origin required by review repair."""
    with patch(
        "auto_coder.pr_processor.CloudManager.get_binding",
        return_value=CloudTaskBinding(provider="codex-cloud", task_id="task_e_review5262"),
    ):
        yield


def _github_client(head_sha: str = "head-1", head_ref: str = "codex/review-5262") -> MagicMock:
    client = MagicMock()
    client.get_pr_comments.return_value = []
    client.get_pull_request_repair_metadata_strict.return_value = PullRequestRepairMetadata(head_ref=head_ref, head_sha=head_sha, base_ref="main")
    return client


def _pr_data(head_sha: str = "head-1") -> dict:
    return {
        "number": 5262,
        "body": "Fixes #42\n\nhttps://chatgpt.com/codex/tasks/task_e_review5262",
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
        first = _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            github_client=_github_client(),
            unresolved_threads=(_thread(),),
        )
        second = _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            github_client=_github_client(),
            unresolved_threads=(_thread(),),
        )

    assert first == ["Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262"]
    assert second == ["Codex Cloud review repair was already requested for PR #5262 for all current actionable feedback"]
    send_followup.assert_called_once()
    task_id, prompt = send_followup.call_args.args[:2]
    assert task_id == "task_e_review5262"
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
        _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data(),
            github_client=_github_client(),
            unresolved_threads=(_thread("First clarification"),),
        )
        _delegate_cloud_review_thread_repair(
            "owner/repo",
            _pr_data("head-2"),
            github_client=_github_client("head-2"),
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
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(_thread(),))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(_thread(), second))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(_thread(), second))

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
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(original,))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(with_new_review,))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data("head-2"), github_client=_github_client(), unresolved_threads=(with_new_review,))

    assert send_followup.call_count == 2
    second_prompt = send_followup.call_args.args[1]
    assert "Whitespace-only input is still broken" in second_prompt
    assert "This misses the empty-input case" not in second_prompt
    assert "Implemented and tested" not in second_prompt


def test_distinct_findings_with_identical_text_are_each_delivered_once(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    first = ReviewThread(
        id="PRRT_thread_1",
        comments=[ReviewThreadComment(database_id=101, body="Same actionable text", author_login="reviewer")],
    )
    second = ReviewThread(
        id="PRRT_thread_2",
        comments=[ReviewThreadComment(database_id=201, body="Same actionable text", author_login="reviewer")],
    )

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(first,))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(first, second))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(first, second))

    assert send_followup.call_count == 2
    assert "Thread `PRRT_thread_2`:\nSame actionable text" in send_followup.call_args.args[1]
    assert "Thread `PRRT_thread_1`" not in send_followup.call_args.args[1]


def test_failed_review_feedback_delivery_remains_retryable(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", side_effect=[False, True]) as send_followup,
    ):
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(_thread(),))
        _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=_github_client(), unresolved_threads=(_thread(),))

    assert send_followup.call_count == 2
    assert state_path.exists()


def test_implementer_cannot_forge_a_cloud_delivery_receipt(tmp_path) -> None:
    state_path = tmp_path / "review-repairs.json"
    thread = _thread()
    prefix = "owner/repo#5262:task_e_review5262:"
    identity = _review_feedback_identity(prefix, thread, 0)
    client = MagicMock()
    client.get_authenticated_user_login.return_value = "auto-coder-bot"
    client.get_pr_comments.return_value = [
        {
            "body": _cloud_review_feedback_markers([identity]),
            "user": {"login": "maintainer"},
        }
    ]

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=state_path),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup", return_value=True) as send_followup,
    ):
        actions = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), github_client=client, unresolved_threads=(thread,))

    send_followup.assert_called_once()
    assert actions == ["Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262"]


def test_non_codex_pr_preserves_generic_review_gate() -> None:
    pr_data = _pr_data()
    pr_data["body"] = "Fixes #42"

    with (
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None),
        patch("auto_coder.codex_cloud_client.CodexCloudClient.send_followup") as send_followup,
    ):
        actions = _delegate_cloud_review_thread_repair("owner/repo", pr_data)

    assert actions == ["Review repair was not delivered for PR #5262: no provider-owned cloud task association was found"]
    assert actions.delivered is False
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
        patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair") as delegate_review_repair,
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
@patch("auto_coder.pr_processor._delegate_cloud_review_thread_repair")
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
    delegate_review_repair.return_value = CloudReviewRepairResult(
        ["Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262"],
        delivered=True,
    )
    config = AutomationConfig()
    config.AUTO_MERGE = True
    client = MagicMock()

    actions = _handle_pr_merge(client, "owner/repo", _pr_data(), config, {})

    assert actions == [
        "All GitHub Actions checks passed for PR #5262",
        "Skipping merge for PR #5262 due to unresolved review threads",
        "Requested Codex Cloud task 'task_e_review5262' to address unresolved review threads for PR #5262",
    ]
    delegate_review_repair.assert_called_once_with(
        "owner/repo",
        _pr_data(),
        github_client=client,
        unresolved_threads=(thread,),
    )


def test_passing_jules_pr_routes_review_repair_from_durable_origin(tmp_path) -> None:
    """Exercise the production merge gate through ownership and transport."""
    repair = _thread()
    provenance = ReviewThread(
        id="PRRT_provenance",
        comments=[ReviewThreadComment(database_id=301, body="<!-- auto-coder-change-provenance-clarification:v1 -->\nWhich commit introduced this?", author_login="reviewer")],
    )
    client = _github_client(head_sha="head-2", head_ref="jules/live-repair-5262")
    jules = MagicMock()
    jules.send_followup.return_value = True
    config = AutomationConfig()
    config.AUTO_MERGE = True
    pr_data = _pr_data()
    pr_data["user"] = {"login": "google-labs-jules[bot]"}

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(unresolved=(repair, provenance), blocking_unresolved=(repair, provenance), has_blocking_unresolved=True),
        ),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="jules", task_id="existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=jules),
    ):
        actions = _handle_pr_merge(client, "owner/repo", pr_data, config, {})

    jules.send_followup.assert_called_once()
    task_id, prompt = jules.send_followup.call_args.args
    assert task_id == "existing-jules-session"
    assert "existing pull request #5262" in prompt
    assert "jules/live-repair-5262" in prompt
    assert "head-2" in prompt
    assert "head-1" not in prompt
    assert "codex/review-5262" not in prompt
    assert "This misses the empty-input case" in prompt
    assert "Which commit introduced this?" not in prompt
    assert any("Requested Jules task 'existing-jules-session'" in action for action in actions)


def test_unresolved_repair_without_durable_owner_fails_processing_visibly() -> None:
    thread = _thread()
    client = MagicMock()
    config = AutomationConfig()
    config.AUTO_MERGE = True
    status = ProcessedPRResult(pr_data=_pr_data())

    with (
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True),
        ),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=None),
    ):
        actions = _take_pr_actions(client, "owner/repo", _pr_data(), config, status)

    assert status.outcome is PRProcessingOutcome.FAILED
    assert status.error == "Review repair was not delivered for PR #5262: no provider-owned cloud task association was found"
    assert status.error in actions
    assert not any("processing deferred" in action for action in actions)


def test_accepted_repair_without_any_durable_receipt_is_not_redelivered(tmp_path) -> None:
    """A transport success with indeterminate persistence fails closed across runs."""
    thread = _thread()
    client = _github_client()
    provider = MagicMock()
    provider.send_followup.return_value = True
    config = AutomationConfig()
    config.AUTO_MERGE = True

    common_patches = (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor._get_mergeable_state", return_value={"mergeable": True, "merge_state_status": "clean"}),
        patch("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", return_value=True),
        patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, ids=[1])),
        patch(
            "auto_coder.pr_processor._get_claimed_review_thread_state",
            return_value=ClaimedReviewThreadGateState(unresolved=(thread,), blocking_unresolved=(thread,), has_blocking_unresolved=True),
        ),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="jules", task_id="existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=provider),
        patch("auto_coder.pr_processor._record_delivered_review_feedback", side_effect=OSError("local disk unavailable")),
        patch("auto_coder.pr_processor._record_pr_delivered_review_feedback", side_effect=RuntimeError("GitHub receipt unavailable")),
    )

    with common_patches[0], common_patches[1], common_patches[2], common_patches[3], common_patches[4], common_patches[5], common_patches[6], common_patches[7], common_patches[8]:
        first_status = ProcessedPRResult(pr_data=_pr_data())
        first_actions = _take_pr_actions(client, "owner/repo", _pr_data(), config, first_status)
        second_status = ProcessedPRResult(pr_data=_pr_data())
        second_actions = _take_pr_actions(client, "owner/repo", _pr_data(), config, second_status)

    provider.send_followup.assert_called_once()
    assert first_status.outcome is PRProcessingOutcome.FAILED
    assert any("durable delivery confirmation failed" in action for action in first_actions)
    assert second_status.outcome is PRProcessingOutcome.FAILED
    assert any("duplicate delivery was suppressed" in action for action in second_actions)
    persisted = json.loads((tmp_path / "repairs.json").read_text(encoding="utf-8"))
    assert persisted["delivered_feedback"] == []
    assert len(persisted["pending_feedback"]) == 1


def test_new_finding_remains_deliverable_beside_indeterminate_old_finding(tmp_path) -> None:
    old_finding = _thread()
    new_finding = ReviewThread(
        id="PRRT_thread_2",
        comments=[ReviewThreadComment(database_id=201, body="A distinct new defect", author_login="reviewer")],
    )
    client = _github_client()
    provider = MagicMock()
    provider.send_followup.return_value = True

    with (
        patch("auto_coder.pr_processor._cloud_review_repair_state_path", return_value=tmp_path / "repairs.json"),
        patch("auto_coder.pr_processor.CloudManager.get_binding", return_value=CloudTaskBinding(provider="jules", task_id="existing-jules-session")),
        patch("auto_coder.cloud_task_engine.CloudTaskEngine.get_client_for_provider", return_value=provider),
        patch("auto_coder.pr_processor._record_delivered_review_feedback", side_effect=OSError("local disk unavailable")),
        patch("auto_coder.pr_processor._record_pr_delivered_review_feedback", side_effect=RuntimeError("GitHub receipt unavailable")),
    ):
        first = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (old_finding,))
        second = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (old_finding,))
        third = _delegate_cloud_review_thread_repair("owner/repo", _pr_data(), client, (old_finding, new_finding))

    assert first.delivered is False
    assert second.delivered is False
    assert third.delivered is False
    assert provider.send_followup.call_count == 2
    latest_prompt = provider.send_followup.call_args.args[1]
    assert "A distinct new defect" in latest_prompt
    assert "This misses the empty-input case" not in latest_prompt
