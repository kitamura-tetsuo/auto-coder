"""Regression tests for durable explicit-local review correction."""

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.git_commit import git_push
from auto_coder.llm_backend_config import get_active_repo_name
from auto_coder.local_review_repair import (
    LocalReviewRepairOutcome,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    _default_executor,
    execute_local_review_repair,
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


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_store_serializes_same_pr_and_retains_indeterminate_execution(tmp_path: Path) -> None:
    first_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    second_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = _request()

    assert first_store.admit(request) == (True, "executing")
    assert second_store.admit(request) == (False, "executing")
    assert first_store.transition(request, "indeterminate", reason="provider response lost") is True
    assert second_store.admit(request) == (False, "indeterminate")


def test_distinct_feedback_is_admitted_after_prior_completion(tmp_path: Path) -> None:
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    original = _request()
    later = _request(feedback=("comment-1", "comment-2"))

    assert store.admit(original) == (True, "executing")
    assert store.admit(later) == (False, "executing")
    assert store.transition(original, "awaiting_validation", result_sha="result-a") is True
    assert store.admit(later) == (True, "executing")
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
        selected = select_local_review_repair_candidates("target/repo")

    assert selected == ["local-b", "local-a"]
    rank.assert_called_once_with([["local-b"], ["local-a"]], config)


def test_candidate_selection_reads_target_repository_effective_config() -> None:
    config = MagicMock()
    config.get_ordinary_priority_groups.return_value = [["target-alias"]]
    config.resolve_backend_type.return_value = "codex"
    with (
        patch("auto_coder.local_review_repair.get_llm_config", return_value=config) as load_config,
        patch("auto_coder.quota_selector.rank_high_score_backends_by_quota", return_value=["target-alias"]),
    ):
        assert select_local_review_repair_candidates("owner/target") == ["target-alias"]

    load_config.assert_called_once_with(repo_name="owner/target")


def test_executor_builds_alias_models_and_options_inside_target_repository_context() -> None:
    config = MagicMock()
    config.get_ordinary_priority_groups.return_value = [["target-alias"]]
    config.resolve_backend_type.return_value = "codex"
    config.get_model_for_backend.return_value = "target-model"
    manager = MagicMock()
    manager.run_prompt.return_value = "ACTION_SUMMARY: fixed"
    observed_contexts: list[str | None] = []

    def build_manager(**_kwargs):
        observed_contexts.append(get_active_repo_name())
        return manager

    with (
        patch("auto_coder.local_review_repair.get_llm_config", return_value=config),
        patch("auto_coder.quota_selector.rank_high_score_backends_by_quota", return_value=["target-alias"]),
        patch("auto_coder.cli_helpers.build_backend_manager", side_effect=build_manager) as build,
    ):
        result = _default_executor(_request(), "/target/worktree")

    assert result == "ACTION_SUMMARY: fixed"
    assert observed_contexts == ["owner/repo"]
    assert build.call_args.kwargs["selected_backends"] == ["target-alias"]
    assert build.call_args.kwargs["models"] == {"target-alias": "target-model"}
    manager.run_prompt.assert_called_once_with("repair exact finding")


def test_production_boundary_retries_identical_definitely_not_started_request(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, _head = _repository(tmp_path)
    monkeypatch.chdir(repository)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = LocalReviewRepairRequest(
        repository="owner/repo",
        pr_number=42,
        head_repository="owner/repo",
        head_ref="repair-head",
        head_sha="future-head",
        feedback_identities=("comment-1",),
        prompt="repair",
    )
    executor = MagicMock(return_value="ACTION_SUMMARY: no change")

    first = execute_local_review_repair(request, store=store, executor=executor)
    assert first.phase == "not_started"
    executor.assert_not_called()

    _git(repository, "branch", "future-head", "HEAD")
    second = execute_local_review_repair(request, store=store, executor=executor)

    assert second.phase == "completed_no_change"
    assert second.executed is True
    executor.assert_called_once()


def test_production_boundary_admits_later_feedback_after_completed_attempt(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, head = _repository(tmp_path)
    monkeypatch.chdir(repository)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    first = _request(feedback=("comment-a",))
    second = _request(feedback=("comment-a", "comment-b"))
    first = replace(first, head_sha=head)
    second = replace(second, head_sha=head)
    executor = MagicMock(return_value="ACTION_SUMMARY: no change")

    first_outcome = execute_local_review_repair(first, store=store, executor=executor)
    second_outcome = execute_local_review_repair(second, store=store, executor=executor)

    assert first_outcome.phase == "completed_no_change"
    assert second_outcome.phase == "completed_no_change"
    assert executor.call_count == 2


def test_publication_pending_resumes_without_rerunning_executor(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, head = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", f"{head}:refs/heads/repair-head")
    monkeypatch.chdir(repository)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = replace(_request(), head_sha=head, head_ref="repair-head")

    def edit(_request, worktree: str) -> str:
        worktree_path = Path(worktree)
        (worktree_path / "tracked.txt").write_text("corrected\n", encoding="utf-8")
        _git(worktree_path, "add", "tracked.txt")
        _git(worktree_path, "commit", "-m", "correction")
        (worktree_path / "uncommitted.txt").write_text("retain status change\n", encoding="utf-8")
        return "ACTION_SUMMARY: corrected"

    executor = MagicMock(side_effect=edit)
    rejected = MagicMock(success=False, stderr="response lost", stdout="", returncode=1)
    accepted = MagicMock(success=True, stderr="", stdout="published", returncode=0)
    commit = MagicMock(success=True, stderr="", stdout="committed", returncode=0)
    with (
        patch("auto_coder.local_review_repair.git_commit_with_retry", return_value=commit),
        patch("auto_coder.local_review_repair.git_push", side_effect=(rejected, accepted)) as push,
    ):
        first = execute_local_review_repair(request, store=store, executor=executor)
        second = execute_local_review_repair(request, store=store, executor=executor)

    assert first.phase == "publication_pending"
    assert second.phase == "awaiting_validation"
    assert second.published is True
    executor.assert_called_once()
    assert push.call_count == 2
    assert push.call_args_list[1].kwargs["branch"].endswith(":repair-head")


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
