"""Production-entry regressions for durable local PR repair handoff (#2560)."""

from __future__ import annotations

import asyncio
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, Candidate, ProcessedPRResult, PRProcessingOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobExecutionResult, LocalJobRunner
from auto_coder.pr_processor import DependencyBotAdmissionDecision, _offer_selected_local_pr_repair, local_pr_repair_context
from auto_coder.pr_repair_dispatch import RepairDisposition, RepairKind


def _pr(number: int) -> dict:
    return {
        "number": number,
        "title": f"repair {number}",
        "body": "<!-- auto-coder:local-llm -->",
        "state": "open",
        "merged": False,
        "labels": [],
        "user": {"login": "human"},
        "head": {"repo": {"full_name": "owner/repo"}, "ref": f"feature-{number}", "sha": f"H{number}"},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main", "sha": "B1"},
    }


def _production_scope(engine: AutomationEngine):
    stack = ExitStack()
    engine.refresh_review_adjudications = MagicMock()
    engine._is_pr_author_allowed = MagicMock(return_value=True)
    stack.enter_context(patch("auto_coder.pr_processor.evaluate_dependency_bot_admission", return_value=DependencyBotAdmissionDecision(True)))
    unsafe = stack.enter_context(patch("auto_coder.pr_processor._reject_unsafe_codex_cloud_pr"))
    unsafe.side_effect = lambda _client, _repo, candidate, _config: MagicMock(closed=False, metadata_error=None, actions=[], authoritative_pr_data=candidate)
    stack.enter_context(patch("auto_coder.pr_processor._close_empty_pr", return_value=MagicMock(closed=False, actions=[], issue_numbers=[])))
    stack.enter_context(patch("auto_coder.pr_processor._close_stale_jules_pr", return_value=MagicMock(closed=False, actions=[], issue_numbers=[])))
    label = stack.enter_context(patch("auto_coder.automation_engine.LabelManager"))
    label.return_value.__enter__.return_value = True
    stack.enter_context(patch("auto_coder.pr_processor._selected_local_backend_name", return_value="codex"))
    stack.enter_context(patch("auto_coder.pr_processor.check_pr_repair_exhaustion", return_value=None))
    return stack


def _install_real_offer(engine: AutomationEngine, github: MagicMock):
    def process(_github, _config, repository, data, **_kwargs):
        with local_pr_repair_context(_kwargs["local_repair_context"]):
            repair = _offer_selected_local_pr_repair(repository, data, RepairKind.MERGE_CONFLICT, github, conflict_eligible=True)
        assert repair is not None
        return ProcessedPRResult(data, [repair.reason], outcome=PRProcessingOutcome.DEFERRED, repair_result=repair)

    return patch("auto_coder.automation_engine.process_pull_request", side_effect=process)


def test_unified_pr_entry_retains_exact_execution_without_merge_or_slot_release(tmp_path: Path) -> None:
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = MagicMock()
    github.get_pull_request_metadata_strict.side_effect = lambda _repo, number: _pr(number)
    engine = AutomationEngine(github, AutomationConfig())
    engine.local_job_store = jobs
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    engine.implementation_slots.resolve_owner = MagicMock(side_effect=lambda _kind, data, _github: ImplementationOwner("pr", data["number"]))  # type: ignore[method-assign]
    merge = MagicMock()

    with _production_scope(engine), _install_real_offer(engine, github), patch("auto_coder.pr_processor._merge_pr", merge):
        result = engine._process_single_candidate_unified("owner/repo", Candidate("pr", _pr(17), 0), engine.config, origin="worker")

    assert result.repair_result is not None and result.repair_result.disposition is RepairDisposition.DEFERRED
    handoff = result.repair_result.local_handoff
    assert handoff is not None
    retained = jobs.pr_repair_for_execution("owner/repo", 17, handoff.implementation_execution_id)
    assert retained is not None and retained.job_id == handoff.job_id
    assert engine.implementation_slots.active_execution_ids(ImplementationOwner("pr", 17)) == (handoff.implementation_execution_id,)
    merge.assert_not_called()


@pytest.mark.asyncio
async def test_pr_worker_processes_second_candidate_while_independent_runner_is_model_blocked(tmp_path: Path, monkeypatch) -> None:
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = MagicMock()
    github.get_pull_request_metadata_strict.side_effect = lambda _repo, number: _pr(number)
    engine = AutomationEngine(github, AutomationConfig())
    engine.local_job_store = jobs
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots.resolve_owner = MagicMock(side_effect=lambda _kind, data, _github: ImplementationOwner("pr", data["number"]))  # type: ignore[method-assign]
    entered = threading.Event()
    release = threading.Event()

    class BarrierAdapter:
        def authorize_provider_entry(self, _job):
            return True

        def invoke(self, _job):
            entered.set()
            assert release.wait(5)
            return LocalJobExecutionResult(InvocationOutcome.COMPLETED, "ACTION_SUMMARY: repaired")

    runner = LocalJobRunner(
        jobs,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.PR_MERGE_CONFLICT: BarrierAdapter()},
    )
    monkeypatch.setattr("auto_coder.automation_engine.is_item_closed_on_github", lambda *_args: False)

    with _production_scope(engine), _install_real_offer(engine, github):
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0, "pr"))
        try:
            await engine.queue.put(Candidate("pr", _pr(17), 0))

            async def first_job_ready() -> None:
                while not any(job.target_number == 17 for job in jobs.discover_unsettled()):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(first_job_ready(), 2)
            assert runner.poll() == 1
            assert await asyncio.to_thread(entered.wait, 2)
            await engine.queue.put(Candidate("pr", _pr(18), 0))
            await asyncio.wait_for(engine.queue.join(), 2)
            first = next(job for job in jobs.discover_unsettled() if job.target_number == 17)
            second = next(job for job in jobs.discover_unsettled() if job.target_number == 18)
            assert first.state is LocalJobState.RUNNING
            assert second.state is LocalJobState.PENDING
            assert engine.implementation_slots.active_execution_ids(ImplementationOwner("pr", 17)) == (first.implementation_execution_id,)
            assert engine.implementation_slots.active_execution_ids(ImplementationOwner("pr", 18)) == (second.implementation_execution_id,)
        finally:
            release.set()
            runner.close(wait=True)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


def test_explicit_pr_entry_retains_one_job_and_target_scoped_runner_ignores_unrelated_work(tmp_path: Path) -> None:
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    github = MagicMock()
    github.get_pull_request_metadata_strict.side_effect = lambda _repo, number: _pr(number)
    engine = AutomationEngine(github, AutomationConfig())
    engine.local_job_store = jobs
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots.resolve_owner = MagicMock(side_effect=lambda _kind, data, _github: ImplementationOwner("pr", data["number"]))  # type: ignore[method-assign]
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._create_candidate_from_single = MagicMock(side_effect=lambda _repo, _kind, number, **_kwargs: Candidate("pr", _pr(number), 0))

    with _production_scope(engine), _install_real_offer(engine, github):
        explicit = engine.process_single("owner/repo", "pr", 17, explicit_only=True)
        unrelated = engine._process_single_candidate_unified("owner/repo", Candidate("pr", _pr(18), 0), engine.config, origin="worker")
        repeated = engine.process_single("owner/repo", "pr", 17, explicit_only=True)

    assert explicit["errors"] == []
    target_jobs = [job for job in jobs.discover_unsettled() if job.target_number == 17]
    assert len(target_jobs) == 1
    target_id = target_jobs[0].job_id
    assert unrelated.repair_result is not None and unrelated.repair_result.local_handoff is not None
    unrelated_id = unrelated.repair_result.local_handoff.job_id
    assert repeated["errors"] == []
    assert [job.job_id for job in jobs.discover_unsettled() if job.target_number == 17] == [target_id]

    entered = threading.Event()
    release = threading.Event()

    class TargetAdapter:
        def authorize_provider_entry(self, _job):
            return True

        def invoke(self, _job):
            entered.set()
            assert release.wait(5)
            return LocalJobExecutionResult(InvocationOutcome.COMPLETED, "ACTION_SUMMARY: target repaired")

    runner = LocalJobRunner(
        jobs,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.PR_MERGE_CONFLICT: TargetAdapter()},
        job_ids=frozenset({target_id}),
    )
    try:
        assert runner.poll() == 1
        assert entered.wait(2)
        assert jobs.get(target_id).state is LocalJobState.RUNNING  # type: ignore[union-attr]
        assert jobs.get(unrelated_id).state is LocalJobState.PENDING  # type: ignore[union-attr]
        assert runner.poll() == 0
    finally:
        release.set()
        runner.close(wait=True)
