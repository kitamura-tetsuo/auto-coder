"""Regression tests for durable explicit-local review correction."""

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.git_commit import git_push
from auto_coder.llm_backend_config import get_active_repo_name
from auto_coder.local_review_repair import (
    LocalBackendUnavailableError,
    LocalReviewRepairOutcome,
    LocalReviewRepairRequest,
    LocalReviewRepairStore,
    _default_executor,
    admit_local_repair_allowance,
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

    claim = first_store.admit(request)
    assert (claim.admitted, claim.phase) == (True, "executing")
    duplicate = second_store.admit(request)
    assert (duplicate.admitted, duplicate.phase) == (False, "executing")
    assert first_store.transition(request, claim, "indeterminate", reason="provider response lost") is True
    retained = second_store.admit(request)
    assert (retained.admitted, retained.phase) == (False, "indeterminate")


def test_distinct_feedback_is_admitted_after_prior_completion(tmp_path: Path) -> None:
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    original = _request()
    later = _request(feedback=("comment-1", "comment-2"))

    original_claim = store.admit(original)
    assert original_claim.admitted is True
    assert store.admit(later).admitted is False
    assert store.transition(original, original_claim, "awaiting_validation", result_sha="result-a") is True
    assert store.admit(later).admitted is True
    assert later.attempt_id != original.attempt_id


def test_not_started_readmission_respects_other_active_claim_and_fences_old_incarnation(tmp_path: Path) -> None:
    first_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    second_store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request_a = _request(feedback=("comment-a",))
    request_b = _request(feedback=("comment-b",))
    first_claim = first_store.admit(request_a)
    assert first_store.transition(request_a, first_claim, "not_started") is True
    claim_b = second_store.admit(request_b)
    assert claim_b.admitted is True

    blocked_a = first_store.admit(request_a)
    assert blocked_a.admitted is False
    assert blocked_a.attempt_id == request_b.attempt_id

    assert second_store.transition(request_b, claim_b, "awaiting_validation") is True
    new_claim_a = first_store.admit(request_a)
    assert new_claim_a.admitted is True
    assert new_claim_a.incarnation != first_claim.incarnation
    assert first_store.transition(request_a, first_claim, "completed_no_change") is False
    assert first_store.get(request_a).phase == "executing"


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


def test_production_allowance_admission_is_attributable_and_idempotent(tmp_path: Path) -> None:
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    first, reason = admit_local_repair_allowance(_request(), ledger)
    assert reason == ""
    assert first is not None
    second, reason = admit_local_repair_allowance(_request(), ledger)
    assert reason == ""
    assert second is not None
    assert second.generation_id == first.generation_id
    snapshot = ledger.get_snapshot("https://api.github.com", "owner/repo", 42)
    assert len(snapshot.generations) == 1
    assert snapshot.generations[0].owning_identity == "local-review-repair"
    assert snapshot.generations[0].covered_blocker_ids == ("comment-1",)


def test_unreadable_production_allowance_prevents_local_route_execution() -> None:
    evidence = PullRequestRoutingMetadata(
        api_origin="https://api.github.com",
        repository="owner/repo",
        number=42,
        state="open",
        body="<!-- auto-coder:local-llm -->\nCloses #7",
        head_repository="owner/repo",
        head_ref="repair-head",
        head_sha="abc123",
    )
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", evidence)
    thread = ReviewThread(id="thread", comments=[ReviewThreadComment(database_id=1, body="fix it", author_login="reviewer")])
    pr_data = {"number": 42, "body": evidence.body, "head": {"ref": "repair-head", "sha": "abc123"}, "base": {"ref": "main"}}
    with (
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-004"),
        patch("auto_coder.local_review_repair.admit_local_repair_allowance", side_effect=OSError("allowance unreadable")),
        patch("auto_coder.local_review_repair.execute_local_review_repair") as execute,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", pr_data, MagicMock(), (thread,), config=MagicMock())

    assert result.local_phase == "not_admitted"
    assert "allowance unreadable" in result[0]
    execute.assert_not_called()


def test_backend_unavailable_is_definitely_not_started_and_retryable(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, head = _repository(tmp_path)
    monkeypatch.chdir(repository)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = replace(_request(), head_sha=head)
    with patch("auto_coder.local_review_repair._prepare_default_executor", side_effect=LocalBackendUnavailableError("temporarily unavailable")):
        first = execute_local_review_repair(request, store=store)
    assert first.phase == "backend_unavailable"
    assert first.executed is False
    assert store.get(request) is None

    invocation = MagicMock(return_value="ACTION_SUMMARY: complete")
    with patch("auto_coder.local_review_repair._prepare_default_executor", return_value=invocation):
        second = execute_local_review_repair(request, store=store)
    assert second.phase == "completed_no_change"
    invocation.assert_called_once()


def test_cannot_fix_is_terminal_failure_not_no_change(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, head = _repository(tmp_path)
    monkeypatch.chdir(repository)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    outcome = execute_local_review_repair(replace(_request(), head_sha=head), store=store, executor=MagicMock(return_value="CANNOT_FIX"))

    assert outcome.phase == "terminal_failure"
    assert outcome.executed is True
    assert store.get(replace(_request(), head_sha=head)).phase == "terminal_failure"


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
        (worktree_path / "test_regression.py").write_text("def test_fixed():\n    assert True\n", encoding="utf-8")
        return "ACTION_SUMMARY: corrected"

    executor = MagicMock(side_effect=edit)
    rejected = MagicMock(success=False, stderr="response lost", stdout="", returncode=1)
    with patch("auto_coder.local_review_repair.git_push", return_value=rejected) as push:
        first = execute_local_review_repair(request, store=store, executor=executor)
    retained = store.get(request)
    assert retained is not None
    _git(repository, "push", "origin", f"{retained.result_sha}:refs/heads/repair-head")
    reconstructed = replace(request, head_sha=retained.result_sha, feedback_identities=("comment-1", "comment-2"))
    with patch("auto_coder.local_review_repair.git_push") as recovery_push:
        second = execute_local_review_repair(reconstructed, store=store, executor=executor)

    assert first.phase == "publication_pending"
    assert second.phase == "awaiting_validation"
    assert second.published is True
    executor.assert_called_once()
    push.assert_called_once()
    recovery_push.assert_not_called()


def test_unstaged_output_is_committed_and_published_to_existing_branch(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, head = _repository(tmp_path)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(repository, "remote", "add", "origin", str(remote))
    _git(repository, "push", "origin", f"{head}:refs/heads/repair-head")
    monkeypatch.chdir(repository)

    def edit(_request, worktree: str) -> str:
        path = Path(worktree)
        (path / "tracked.txt").write_text("corrected\n", encoding="utf-8")
        (path / "test_regression.py").write_text("def test_fixed():\n    assert True\n", encoding="utf-8")
        return "ACTION_SUMMARY: corrected"

    request = replace(_request(), head_sha=head, head_ref="repair-head")
    outcome = execute_local_review_repair(request, store=LocalReviewRepairStore(tmp_path / "repairs.sqlite3"), executor=edit)

    assert outcome.phase == "awaiting_validation"
    published = _git(repository, "show", f"{outcome.reason.split()[1]}:tracked.txt")
    added_test = _git(repository, "show", f"{outcome.reason.split()[1]}:test_regression.py")
    assert published == "corrected"
    assert "assert True" in added_test
    assert _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/repair-head") == outcome.reason.split()[1]


def test_commit_failure_preserves_corrective_workspace(tmp_path: Path, monkeypatch, _use_custom_subprocess_mock) -> None:
    repository, head = _repository(tmp_path)
    monkeypatch.chdir(repository)
    store = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    request = replace(_request(), head_sha=head)

    def edit(_request, worktree: str) -> str:
        Path(worktree, "tracked.txt").write_text("recover me\n", encoding="utf-8")
        Path(worktree, "new_test.py").write_text("def test_recovery(): pass\n", encoding="utf-8")
        return "ACTION_SUMMARY: corrected"

    failed = MagicMock(success=False, stderr="hook failure", stdout="", returncode=1)
    with patch("auto_coder.local_review_repair.git_commit_with_retry", return_value=failed):
        outcome = execute_local_review_repair(request, store=store, executor=edit)

    record = store.get(request)
    assert outcome.phase == "indeterminate"
    assert record is not None
    workspace = Path(record.workspace_path)
    assert workspace.is_dir()
    assert (workspace / "tracked.txt").read_text(encoding="utf-8") == "recover me\n"
    assert (workspace / "new_test.py").exists()


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
        patch("auto_coder.local_review_repair.admit_local_repair_allowance", return_value=(MagicMock(), "")),
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
    assert execute.call_args.kwargs["allowance_authority"] is not None
    assert result.route_disposition == "LOCAL_EXECUTION"
    assert result.deferred is True
    assert "awaiting_validation" in result[0]


def test_local_route_propagates_cannot_fix_as_terminal_failure() -> None:
    evidence = PullRequestRoutingMetadata(
        api_origin="https://api.github.com",
        repository="owner/repo",
        number=42,
        state="open",
        body="<!-- auto-coder:local-llm -->\nCloses #7",
        head_repository="owner/repo",
        head_ref="repair-head",
        head_sha="abc123",
    )
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", evidence)
    thread = ReviewThread(id="thread", comments=[ReviewThreadComment(database_id=1, body="fix it")])
    pr_data = {"number": 42, "body": evidence.body, "head": {"ref": "repair-head", "sha": "abc123"}, "base": {"ref": "main"}}
    with (
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-010"),
        patch("auto_coder.local_review_repair.admit_local_repair_allowance", return_value=(MagicMock(), "")),
        patch(
            "auto_coder.local_review_repair.execute_local_review_repair",
            return_value=LocalReviewRepairOutcome("terminal_failure", "local backend could not correct the finding", True, False),
        ),
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", pr_data, MagicMock(), (thread,), config=MagicMock())

    assert result.local_phase == "terminal_failure"
    assert result.deferred is False
    assert "terminal_failure" in result[0]


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


@pytest.mark.parametrize("heading", ["### Auto-Coder adversarial finding", "### Auto-Coder material test-oracle gap"])
@pytest.mark.parametrize("replay", [False, True])
def test_validated_local_feedback_overrides_addressed_claim_and_excludes_unrelated_threads(heading: str, replay: bool) -> None:
    from auto_coder.pr_processor import _send_adversarial_validation_feedback_to_cloud_task

    evidence = PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", "<!-- auto-coder:local-llm -->", "owner/repo", "repair-head", "abc123")
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", evidence)
    finding = f"{heading}\n\nCorrect the broken invariant."
    thread = ReviewThread(id="validated", comments=[ReviewThreadComment(database_id=1, body=finding, author_login="reviewer"), ReviewThreadComment(database_id=2, body="<!-- auto-coder-review-addressed:v1 -->", author_login="implementer")])
    unrelated = ReviewThread(id="unrelated", comments=[ReviewThreadComment(database_id=3, body="Unrelated request", author_login="reviewer")])
    github = MagicMock()
    github.get_pr_review_threads_strict.return_value = [thread, unrelated]
    pr_data = {"number": 42, "body": evidence.body, "head": {"ref": "repair-head", "sha": "abc123"}, "base": {"ref": "main"}}
    with (
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-001: preserve invariant"),
        patch("auto_coder.local_review_repair.admit_local_repair_allowance", return_value=(object(), "")),
        patch("auto_coder.local_review_repair.execute_local_review_repair", return_value=LocalReviewRepairOutcome("awaiting_validation", "published", True, True)) as execute,
        patch("auto_coder.pr_processor._resolve_cloud_task_origin") as cloud,
    ):
        result = _send_adversarial_validation_feedback_to_cloud_task("owner/repo", pr_data, "abc123", finding, github, () if replay else [finding], config=MagicMock())
    execute.assert_called_once()
    request = execute.call_args.args[0]
    assert finding in request.prompt
    assert "Unrelated request" not in request.prompt
    assert len(request.feedback_identities) == 1
    assert request.head_sha == "abc123"
    assert result.route_disposition == "LOCAL_EXECUTION"
    assert result.local_phase == "awaiting_validation"
    cloud.assert_not_called()


def test_local_adversarial_repair_rejects_changed_validated_head() -> None:
    evidence = PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", "<!-- auto-coder:local-llm -->", "owner/repo", "repair-head", "new-head")
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", evidence)
    with (
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
        patch("auto_coder.local_review_repair.execute_local_review_repair") as execute,
    ):
        result = _delegate_cloud_review_thread_repair("owner/repo", {"number": 42}, MagicMock(), config=MagicMock(), validated_feedback=("finding",), validated_head_sha="old-head")
    assert result.route_disposition == "CONFLICT"
    assert result == ["Local review repair was not admitted for PR #42: validated head has changed"]
    execute.assert_not_called()
