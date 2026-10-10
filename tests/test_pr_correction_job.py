import asyncio
import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.local_review_repair import LocalReviewRepairClaim, LocalReviewRepairOutcome, LocalReviewRepairRequest, LocalReviewRepairStore, admit_local_repair_allowance
from auto_coder.pr_correction_job import PRCorrectionJobAdapter, offer_pr_correction_job, resume_pr_correction_publication
from auto_coder.pr_processor import ReviewRepairRouteDecision, ReviewRepairRouteDisposition
from auto_coder.util.gh_cache import PullRequestRoutingMetadata, ReviewThread, ReviewThreadComment


def _request() -> LocalReviewRepairRequest:
    return LocalReviewRepairRequest("owner/repo", 42, "owner/repo", "repair", "head-1", ("root-1",), "bounded prompt")


def _git_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    return repository, head


def _wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


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


def test_authority_is_revalidated_after_worktree_preparation_before_model_entry(tmp_path: Path, monkeypatch) -> None:
    repository, head = _git_repository(tmp_path)
    monkeypatch.chdir(repository)
    request = replace(_request(), head_sha=head)
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    assert admit_local_repair_allowance(request, ledger)[0] is not None
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    current = True
    executor = MagicMock(return_value="ACTION_SUMMARY: changed")

    class ChangingAuthorityAdapter(PRCorrectionJobAdapter):
        def _authority_is_current(self, _job) -> bool:
            return current

    adapter = ChangingAuthorityAdapter(MagicMock(), MagicMock(return_value=executor), jobs)
    real_run = subprocess.run

    def change_after_preparation(command, **kwargs):
        nonlocal current
        result = real_run(command, **kwargs)
        if command[:3] == ["git", "worktree", "add"]:
            current = False
        return result

    runner = LocalJobRunner(jobs, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.PR_REVIEW_CORRECTION: adapter})
    with (
        patch("auto_coder.pr_correction_job.LocalReviewRepairStore", return_value=repairs),
        patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.local_review_repair.subprocess.run", side_effect=change_after_preparation),
    ):
        assert runner.poll() == 1
        _wait_for(lambda: runner.active_count() == 0)

    retained = jobs.get(job.job_id)
    assert retained is not None and retained.state is LocalJobState.PENDING
    assert retained.provider_entered is False
    repair = repairs.get(request)
    assert repair is not None and repair.phase == "not_started" and repair.invocation_entered is False
    generation = ledger.get_snapshot("https://api.github.com", request.repository, request.pr_number).get_outstanding_generation()
    assert generation is not None and generation.lifecycle_state.value == "RESERVED"
    assert generation.delivery_attempts == ()
    executor.assert_not_called()
    runner.close()


def test_worktree_preparation_failure_retries_same_job_and_invokes_model_once(tmp_path: Path, monkeypatch) -> None:
    repository, head = _git_repository(tmp_path)
    monkeypatch.chdir(repository)
    request = replace(_request(), head_sha=head)
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    assert admit_local_repair_allowance(request, ledger)[0] is not None
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    executor = MagicMock(return_value="ACTION_SUMMARY: no change")

    class CurrentAuthorityAdapter(PRCorrectionJobAdapter):
        def _authority_is_current(self, _job) -> bool:
            return True

    adapter = CurrentAuthorityAdapter(MagicMock(), MagicMock(return_value=executor), jobs)
    real_run = subprocess.run
    failed_once = False

    def fail_first_preparation(command, **kwargs):
        nonlocal failed_once
        if command[:3] == ["git", "worktree", "add"] and not failed_once:
            failed_once = True
            return subprocess.CompletedProcess(command, 1, "", "transient worktree failure")
        return real_run(command, **kwargs)

    runner = LocalJobRunner(jobs, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.PR_REVIEW_CORRECTION: adapter})
    with (
        patch("auto_coder.pr_correction_job.LocalReviewRepairStore", return_value=repairs),
        patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger),
        patch("auto_coder.local_review_repair.subprocess.run", side_effect=fail_first_preparation),
    ):
        assert runner.poll() == 1
        _wait_for(lambda: runner.active_count() == 0)
        first = jobs.get(job.job_id)
        assert first is not None and first.state is LocalJobState.PENDING and not first.provider_entered
        first_repair = repairs.get(request)
        assert first_repair is not None and first_repair.phase == "not_started"
        replay = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
        assert replay is not None and replay.job_id == job.job_id
        assert repairs.get(request).incarnation == first_repair.incarnation  # type: ignore[union-attr]
        assert runner.poll() == 1
        _wait_for(lambda: runner.active_count() == 0)

    completed = jobs.get(job.job_id)
    assert completed is not None and completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert completed.provider_entered is True
    executor.assert_called_once()
    assert completed.upstream_attempt == request.attempt_id
    assert completed.owner_generation == job.owner_generation
    runner.close()


def test_restart_recovers_publication_checkpoint_before_runner_artifact(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    request = _request()
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    assert admit_local_repair_allowance(request, ledger)[0] is not None
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    claim = jobs.claim(job.job_id, "process:999999:1")
    assert claim is not None and claim.acquired
    assert jobs.mark_provider_entered(claim)
    incarnation = int(job.upstream_incarnation.split(":", 1)[0])
    repair_claim = LocalReviewRepairClaim(True, "executing", request.attempt_id, incarnation)
    assert repairs.transition(request, repair_claim, "publication_pending", result_sha="commit-2", workspace_path="/retained")
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    def wake(completed) -> None:
        future = asyncio.run_coroutine_threadsafe(engine.invalidate_entity(completed.repository, "pr", completed.target_number), loop_holder["loop"])
        assert future.result(2)

    adapter = PRCorrectionJobAdapter(MagicMock(), MagicMock(), jobs)
    runner = LocalJobRunner(
        jobs,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.PR_REVIEW_CORRECTION: adapter},
        completion_wake=wake,
    )
    recovered = LocalReviewRepairOutcome("awaiting_validation", "published retained commit", executed=True, published=True)

    async def scenario() -> None:
        loop_holder["loop"] = asyncio.get_running_loop()
        with (
            patch("auto_coder.pr_correction_job.LocalReviewRepairStore", return_value=repairs),
            patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger),
            patch("auto_coder.pr_correction_job.execute_local_review_repair", return_value=recovered) as resume,
            patch("auto_coder.local_job_runner.runner_owner_alive", return_value=False),
        ):
            assert runner.poll() == 0
            for _ in range(200):
                if engine.invalidations.pending_count("owner/repo") == 1:
                    break
                await asyncio.sleep(0.01)
            queued = await asyncio.wait_for(engine.queue.get_for_type("pr"), 1)
            assert queued.data["number"] == request.pr_number
            engine.queue.task_done()
        resume.assert_called_once()
        assert "executor" not in resume.call_args.kwargs

    asyncio.run(scenario())

    retained = jobs.get(job.job_id)
    assert retained is not None and retained.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = jobs.get_result_artifact(retained.result_reference)
    assert artifact is not None and json.loads(artifact.output)["phase"] == "awaiting_validation"
    assert engine.invalidations.pending_count("owner/repo") == 1
    runner.close()


def test_engine_completion_wake_invalidates_pr_before_settling_job(tmp_path: Path, monkeypatch) -> None:
    """The engine's production completion callback durably wakes validation first."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    request = _request()
    assert admit_local_repair_allowance(request, ledger)[0] is not None
    job = offer_pr_correction_job(request, "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
    assert job is not None
    claim = jobs.claim(job.job_id, "test-runner")
    assert claim is not None and claim.acquired
    assert jobs.mark_provider_entered(claim)
    artifact = jobs.persist_result_artifact(
        claim,
        InvocationOutcome.COMPLETED,
        json.dumps({"phase": "awaiting_validation", "published": True, "result_sha": "head-2"}),
    )
    assert artifact is not None
    assert jobs.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id)
    assert jobs.mark_downstream_pending(claim)

    captured: dict[str, object] = {}

    class CapturingRunner:
        def __init__(self, store, gate, **kwargs):
            assert store is jobs
            captured["wake"] = kwargs["completion_wake"]

        def poll(self) -> int:
            return 0

    engine = AutomationEngine(MagicMock(), AutomationConfig())
    monkeypatch.setattr("auto_coder.automation_engine.LocalJobStore", lambda: jobs)
    monkeypatch.setattr("auto_coder.automation_engine.LocalJobRunner", CapturingRunner)
    monkeypatch.setattr(engine.issue_stage_routing, "recover", MagicMock(side_effect=RuntimeError("captured")))
    monkeypatch.setattr("auto_coder.automation_engine.resume_pr_correction_publication", MagicMock(), raising=False)

    async def scenario() -> None:
        before = set(asyncio.all_tasks())
        with patch("auto_coder.pr_correction_job.resume_pr_correction_publication", return_value="awaiting_validation"):
            try:
                await engine.start_automation("owner/repo", concurrency=1)
            except RuntimeError as exc:
                assert str(exc) == "captured"
            wake = captured["wake"]
            assert callable(wake)
            assert jobs.get(job.job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
            assert engine.invalidations.pending_count("owner/repo") == 0
            await asyncio.to_thread(wake, jobs.get(job.job_id))
            queued = await asyncio.wait_for(engine.queue.get_for_type("pr"), 1)
            assert queued.data["number"] == 42
            assert queued.invalidation_generation is not None
            engine.queue.task_done()
        spawned = set(asyncio.all_tasks()) - before
        for task in spawned:
            task.cancel()
        await asyncio.gather(*spawned, return_exceptions=True)

    asyncio.run(scenario())

    settled = jobs.get(job.job_id)
    assert settled is not None and settled.state is LocalJobState.SETTLED
    assert engine.invalidations.pending_count("owner/repo") == 1
    effect = jobs.get_effect(job.job_id, claim.record.execution_incarnation, "pr-validation-wake")
    assert effect is not None and effect.state == "completed"


def test_single_pr_worker_hands_off_second_correction_while_first_backend_is_blocked(tmp_path: Path, monkeypatch) -> None:
    """One production PR worker reaches the real delegate twice without waiting for model completion."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    jobs = LocalJobStore(tmp_path / "jobs.sqlite3")
    repairs = LocalReviewRepairStore(tmp_path / "repairs.sqlite3")
    ledger = RepairAllowanceLedger(tmp_path / "allowance.sqlite3")
    entered = threading.Event()
    release = threading.Event()
    github = MagicMock()

    def request(number: int) -> LocalReviewRepairRequest:
        return LocalReviewRepairRequest("owner/repo", number, "owner/repo", f"repair-{number}", f"head-{number}", (f"root-{number}",), f"prompt-{number}")

    offered: dict[int, object] = {}

    def delegate_for(candidate: Candidate) -> CandidateProcessingResult:
        from auto_coder.pr_processor import _delegate_cloud_review_thread_repair

        number = int(candidate.data["number"])
        req = request(number)
        route = ReviewRepairRouteDecision(
            ReviewRepairRouteDisposition.LOCAL_REQUIRED,
            "explicit local",
            PullRequestRoutingMetadata("https://api.github.com", "owner/repo", number, "open", "<!-- auto-coder:local-llm -->", "owner/repo", req.head_ref, req.head_sha),
        )
        thread = ReviewThread(id=f"thread-{number}", comments=[ReviewThreadComment(database_id=number, body=f"fix-{number}")])

        def offer(actual: LocalReviewRepairRequest, backend: str):
            accepted = offer_pr_correction_job(actual, backend, store=jobs, repair_store=repairs, allowance_ledger=ledger)
            offered[number] = accepted
            return accepted

        with (
            patch("auto_coder.pr_processor._select_review_repair_route", return_value=route),
            patch("auto_coder.pr_processor._revalidate_local_review_repair_route", return_value=route),
            patch("auto_coder.pr_processor.resolve_existing_pr_repair_target", return_value=SimpleNamespace(head_ref=req.head_ref, head_sha=req.head_sha)),
            patch("auto_coder.pr_processor.retain_external_review_feedback", return_value=()),
            patch("auto_coder.pr_processor.get_linked_issues_context", return_value="REQ-010: retain worker capacity"),
            patch("auto_coder.pr_processor.render_prompt", return_value=req.prompt),
            patch("auto_coder.pr_processor.build_existing_pr_repair_prompt", return_value=req.prompt),
            patch("auto_coder.local_review_repair.admit_local_repair_allowance", side_effect=lambda actual: admit_local_repair_allowance(actual, ledger)),
            patch("auto_coder.local_review_repair.select_local_review_repair_candidates", return_value=["codex"]),
            patch("auto_coder.pr_correction_job.offer_pr_correction_job", side_effect=offer),
        ):
            result = _delegate_cloud_review_thread_repair(
                "owner/repo",
                candidate.data,
                github,
                (thread,),
                AutomationConfig(),
            )
        assert result.deferred and result.local_phase == "pending"
        return CandidateProcessingResult(type="pr", number=number, success=True)

    engine = AutomationEngine(github, AutomationConfig())
    monkeypatch.setattr(engine, "_process_single_candidate", lambda _repo, candidate, **_kwargs: delegate_for(candidate))
    monkeypatch.setattr("auto_coder.automation_engine.is_item_closed_on_github", lambda *_args: False)

    def barrier_executor(_request: LocalReviewRepairRequest, _worktree: str) -> str:
        entered.set()
        assert release.wait(5)
        return "ACTION_SUMMARY: corrected"

    class BarrierAdapter(PRCorrectionJobAdapter):
        def authorize_provider_entry(self, job) -> bool:
            with self._executor_lock:
                self._executors[job.job_id] = barrier_executor
            return True

    def execute_at_provider_entry(actual, *, executor, **_kwargs):
        executor(actual, "/tmp/held-local-backend")
        return LocalReviewRepairOutcome("awaiting_validation", "published", executed=True, published=True)

    runner = LocalJobRunner(
        jobs,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.PR_REVIEW_CORRECTION: BarrierAdapter(github, lambda *_args: barrier_executor)},
    )

    async def scenario() -> None:
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0, "pr"))
        try:
            execution_patch = patch("auto_coder.pr_correction_job.execute_local_review_repair", side_effect=execute_at_provider_entry)
            repair_store_patch = patch("auto_coder.pr_correction_job.LocalReviewRepairStore", return_value=repairs)
            ledger_patch = patch("auto_coder.pr_correction_job.RepairAllowanceLedger", return_value=ledger)
            execution_patch.start()
            repair_store_patch.start()
            ledger_patch.start()
            for number in (41, 42):
                await engine.queue.put(
                    Candidate(
                        type="pr",
                        data={
                            "number": number,
                            "state": "open",
                            "body": "<!-- auto-coder:local-llm -->",
                            "head": {"ref": f"repair-{number}", "sha": f"head-{number}"},
                            "base": {"ref": "main"},
                        },
                        priority=0,
                    )
                )
                if number == 41:
                    while number not in offered:
                        await asyncio.sleep(0.01)
                    assert runner.poll() == 1
                    assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(engine.queue.join(), 2)
            assert set(offered) == {41, 42}
            first = offered[41]
            second = offered[42]
            assert first is not None and second is not None
            assert jobs.get(first.job_id).state is LocalJobState.RUNNING  # type: ignore[union-attr]
            assert jobs.get(second.job_id).state is LocalJobState.PENDING  # type: ignore[union-attr]
            first_request = PRCorrectionJobAdapter._request(first)  # type: ignore[arg-type]
            first_repair = repairs.get(first_request)
            assert first_repair is not None and first_repair.local_job_id == first.job_id  # type: ignore[union-attr]
            assert first.owner_generation is not None  # type: ignore[union-attr]
            replay = offer_pr_correction_job(request(41), "codex", store=jobs, repair_store=repairs, allowance_ledger=ledger)
            assert replay is not None and replay.job_id == first.job_id  # type: ignore[union-attr]
            assert len([record for record in jobs.discover_unsettled() if record.target_number == 41]) == 1
        finally:
            release.set()
            execution_patch.stop()
            repair_store_patch.stop()
            ledger_patch.stop()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            runner.close(wait=True)

    asyncio.run(scenario())
