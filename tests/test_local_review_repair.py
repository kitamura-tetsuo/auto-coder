"""Regression tests for durable explicit-local review correction."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.git_commit import git_push
from auto_coder.local_review_repair import (
    LocalReviewRepairOutcome,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    select_local_review_repair_candidates,
)
from auto_coder.pr_processor import (
    ReviewRepairRouteDecision,
    ReviewRepairRouteDisposition,
    _delegate_cloud_review_thread_repair,
)
from auto_coder.util.gh_cache import PullRequestRoutingMetadata, ReviewThread, ReviewThreadComment


def _request(*, feedback: tuple[str, ...] = ("comment-1",)) -> LocalReviewRepairRequest:
    return LocalReviewRepairRequest(
        repository="owner/repo",
        pr_number=42,
        head_repository="owner/repo",
        head_ref="issue-7_attempt-1",
        head_sha="abc123",
        feedback_identities=feedback,
        prompt="repair exact finding",
    )


def test_store_serializes_same_pr_and_retains_indeterminate_execution(tmp_path: Path) -> None:
    first_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    second_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = _request()

    assert first_store.admit(request) == (True, "executing")
    assert second_store.admit(request) == (False, "executing")
    assert first_store.transition(request, "indeterminate", reason="provider response lost") is True
    assert second_store.admit(request) == (False, "indeterminate")


def test_feedback_discovered_during_attempt_is_not_absorbed(tmp_path: Path) -> None:
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    original = _request()
    later = _request(feedback=("comment-1", "comment-2"))

    assert store.admit(original) == (True, "executing")
    assert store.admit(later) == (False, "executing")
    assert later.attempt_id != original.attempt_id


def test_candidate_selection_excludes_task_only_types_and_preserves_ranked_groups() -> None:
    config = MagicMock()
    config.get_ordinary_priority_groups.return_value = [["jules-task", "local-b"], ["cloud-task", "local-a"]]
    config.resolve_backend_type.side_effect = {
        "jules-task": "jules",
        "local-b": "codex",
        "cloud-task": "codex-cloud",
        "local-a": "claude",
    }.get

    with (
        patch("auto_coder.local_review_repair.get_llm_config", return_value=config),
        patch("auto_coder.quota_selector.rank_high_score_backends_by_quota", return_value=["local-b", "local-a"]) as rank,
    ):
        selected = select_local_review_repair_candidates()

    assert selected == ["local-b", "local-a"]
    rank.assert_called_once_with([["local-b"], ["local-a"]], config)


def test_local_route_invokes_real_execution_boundary_with_two_tier_feedback() -> None:
    evidence = PullRequestRoutingMetadata(
        api_origin="https://api.github.com",
        repository="owner/repo",
        number=42,
        state="open",
        body="<!-- auto-coder:local-llm -->\nCloses #7",
        head_repository="owner/repo",
        head_ref="issue-7_attempt-1",
        head_sha="abc123",
    )
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", evidence)
    thread = ReviewThread(
        id="thread-strong",
        comments=[
            ReviewThreadComment(
                database_id=91,
                author_login="auto-coder-reviewer[bot]",
                body="<!-- auto-coder-two-tier-finding:v1 -->\nFix the reachable race.",
            )
        ],
    )
    pr_data = {
        "number": 42,
        "body": evidence.body,
        "head": {"ref": evidence.head_ref, "sha": evidence.head_sha},
        "base": {"ref": "main"},
        "user": {"login": "implementer"},
    }

    with (
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-001: preserve the race invariant"),
        patch("auto_coder.local_review_repair.execute_local_review_repair", return_value=LocalReviewRepairOutcome("awaiting_validation", "published", True, True)) as execute,
    ):
        result = _delegate_cloud_review_thread_repair(
            "owner/repo",
            pr_data,
            github_client=MagicMock(),
            unresolved_threads=(thread,),
            config=MagicMock(),
        )

    request = execute.call_args.args[0]
    assert "auto-coder-two-tier-finding:v1" in request.prompt
    assert "REQ-001: preserve the race invariant" in request.prompt
    assert request.head_ref == "issue-7_attempt-1"
    assert request.feedback_identities
    assert result.route_disposition == "LOCAL_EXECUTION"
    assert result.deferred is True
    assert "awaiting_validation" in result[0]


def test_exact_head_push_uses_lease_and_never_enters_recovery_fallback() -> None:
    rejected = MagicMock(success=False, stderr="stale info", stdout="", returncode=1)
    with (
        patch("auto_coder.git_commit.CommandExecutor") as executor_type,
        patch("auto_coder.git_commit.try_llm_commit_push", create=True) as fallback,
    ):
        executor_type.return_value.run_command.return_value = rejected
        result = git_push(
            cwd="/checkout",
            remote="origin",
            branch="HEAD:issue-7_attempt-1",
            expected_remote_sha="abc123",
        )

    assert result is rejected
    executor_type.return_value.run_command.assert_called_once_with(
        [
            "git",
            "push",
            "--force-with-lease=refs/heads/issue-7_attempt-1:abc123",
            "origin",
            "HEAD:issue-7_attempt-1",
        ],
        cwd="/checkout",
    )
    fallback.assert_not_called()
