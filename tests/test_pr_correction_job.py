from pathlib import Path
from unittest.mock import MagicMock, patch

from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, admit_local_repair_allowance
from auto_coder.pr_correction_job import PRCorrectionJobAdapter, offer_pr_correction_job
from auto_coder.util.gh_cache import PullRequestRoutingMetadata


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
    request = _request()
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    assert admit_local_repair_allowance(request, ledger)[0] is not None
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    github = MagicMock()
    github.get_pull_request_routing_metadata_strict.return_value = PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", "<!-- auto-coder:local-llm -->", "owner/repo", "repair", "head-1")
    adapter = PRCorrectionJobAdapter(github, MagicMock())

    with patch("auto_coder.pr_correction_job.local_review_repair_db_path", return_value=repairs.path), patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger):
        assert adapter.authorize_provider_entry(job) is True
        github.get_pull_request_routing_metadata_strict.return_value = PullRequestRoutingMetadata("https://api.github.com", "owner/repo", 42, "open", "<!-- auto-coder:local-llm -->", "owner/repo", "repair", "head-2")
        assert adapter.authorize_provider_entry(job) is False
