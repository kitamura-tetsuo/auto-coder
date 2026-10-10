import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.local_review_repair import LocalReviewRepairOutcome, LocalReviewRepairRequest, LocalReviewRepairStore, admit_local_repair_allowance
from auto_coder.pr_correction_job import PRCorrectionJobAdapter, offer_pr_correction_job, resume_pr_correction_publication
from auto_coder.pr_processor import ReviewRepairRouteDecision, ReviewRepairRouteDisposition
from auto_coder.util.gh_cache import PullRequestRoutingMetadata, ReviewThread, ReviewThreadComment


def _request() -> LocalReviewRepairRequest:
    return LocalReviewRepairRequest("owner/repo", 42, "owner/repo", "repair", "head-1", ("root-1",), "bounded prompt")


def test_offer_is_durable_and_replay_does_not_consume_another_generation(tmp_path: Path) -> None:
    request = _request()
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, reason = admit_local_repair_allowance(request, ledger)
    assert authority is not None, reason

    first = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    second = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)

    assert first is not None and second is not None
    assert first.job_id == second.job_id
    assert first.state is LocalJobState.PENDING
    assert first.kind is LocalJobKind.PR_REVIEW_CORRECTION
    assert first.owner_generation == authority.generation_id
    assert repairs.get(request).local_job_id == first.job_id  # type: ignore[union-attr]


def test_adapter_revalidates_exact_head_route_and_allowance_before_entry(tmp_path: Path) -> None:
    github = MagicMock()
    github.get_pull_request_routing_metadata_strict.return_value = PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", "<!-- auto-coder:local-llm -->", "owner/repo", "repair", "head-1")
    thread = ReviewThread(id="thread-1", comments=[ReviewThreadComment(database_id=1, body="fix")])
    github.get_pr_review_threads_strict.return_value = [thread]
    from auto_coder.pr_processor import _review_feedback_identity

    request = LocalReviewRepairRequest("owner/repo", 42, "owner/repo", "repair", "head-1", (_review_feedback_identity("owner/repo#42:local:", thread, 0),), "bounded prompt")
    jobs = LocalJobStore(tmp_path / "jobs-current.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs-current.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance-current.sqlite3")
    assert admit_local_repair_allowance(request, ledger)[0] is not None
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    executor_factory = MagicMock(return_value=MagicMock())
    adapter = PRCorrectionJobAdapter(github, executor_factory)
    route = ReviewRepairRouteDecision(ReviewRepairRouteDisposition.LOCAL_REQUIRED, "local", github.get_pull_request_routing_metadata_strict.return_value)

    with (
        patch("auto_coder.pr_correction_job.local_review_repair_db_path", return_value=repairs.path),
        patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
        patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
    ):
        assert adapter.authorize_provider_entry(job) is True
        executor_factory.assert_called_once_with("codex", request)
        github.get_pull_request_routing_metadata_strict.return_value = PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", "<!-- auto-coder:local-llm -->", "owner/repo", "repair", "head-2")
        assert adapter.authorize_provider_entry(job) is False
        github.get_pull_request_routing_metadata_strict.return_value = route.evidence
        thread.comments.append(ReviewThreadComment(database_id=2, body="<!-- auto-coder-review-addressed:v1 -->"))
        assert adapter.authorize_provider_entry(job) is False


def test_publication_pending_result_resumes_without_another_model_invocation(tmp_path: Path) -> None:
    request = _request()
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    authority, reason = admit_local_repair_allowance(request, ledger)
    assert authority is not None, reason
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    retained = replace(job, result_reference="artifact-1", owner_generation=authority.generation_id)
    artifact = SimpleNamespace(output=json.dumps({"phase": "publication_pending"}))

    with (
        patch.object(jobs, "get_result_artifact", return_value=artifact),
        patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.pr_correction_job.local_review_repair_db_path", return_value=repairs.path),
        patch(
            "auto_coder.pr_correction_job.execute_local_review_repair",
            return_value=LocalReviewRepairOutcome("awaiting_validation", "published", executed=True, published=True),
        ) as resume,
    ):
        assert resume_pr_correction_publication(retained, jobs) == "awaiting_validation"

    assert "executor" not in resume.call_args.kwargs
