import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auto_coder.automation_config import AutomationConfig, ExplicitTargetOutcome
from auto_coder.automation_engine import CODEX_RETRY_HANDOFF_EFFECT, AutomationEngine
from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.codex_cloud_client import CodexSubmissionOutcome, CodexSubmissionResult
from auto_coder.codex_retry_reconciliation import CodexRetryReconciler
from auto_coder.github_pending_work import PendingWorkStore
from auto_coder.implementation_retirement import (
    ContinuingObligations,
    ImplementationPRObservation,
    ImplementationRetirementObservation,
    ProviderSessionObservation,
    PRTerminalState,
    RetirementStatus,
    SessionTerminalState,
)
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_processor import _acknowledge_retry_projection, _process_issue_codex_cloud_mode
from auto_coder.issue_stage_routing import ImplementationRetryRequest, IssueStageRoutingStore
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobRunner
from auto_coder.retry_dispatch import RetryDispatchRepository
from auto_coder.util.gh_cache import OpenGitHubEntities
from auto_coder.util.github_request_outcome import GitHubApiOutcome

from .test_pending_work_resumption import _github_error


def _reconciler(engine: AutomationEngine) -> CodexRetryReconciler:
    return CodexRetryReconciler(
        "owner/repo",
        engine._get_implementation_slots("owner/repo"),
        lambda request_id, issue_number: engine._schedule_codex_retry_handoff("owner/repo", request_id, issue_number),
    )


def _authority(request_id: str = "request-1", attempt_id: str = "logical-1") -> ImplementationRetryRequest:
    del attempt_id
    routing = IssueStageRoutingStore(Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3")
    existing = routing.retry_request(request_id)
    if existing is not None:
        return existing
    routing.accept_retry_request(request_id, "owner/repo", 2223, "generation-7")
    predecessor = CloudManager("owner/repo").read_bindings_strict().get("2223")
    routing.capture_retry_predecessor(
        request_id,
        predecessor.provider if predecessor else None,
        predecessor.task_id if predecessor else None,
        predecessor.backend_name if predecessor else None,
    )
    return routing.mark_retry_owned(request_id, f"execution-{request_id}")


def _dispatch(authority: ImplementationRetryRequest, backend: str = "codex-alias", branch: str = "main") -> list[str]:
    config = AutomationConfig()
    config.MAIN_BRANCH = branch
    with (
        patch("auto_coder.issue_processor.get_current_attempt", return_value=4),
        patch("auto_coder.issue_processor.increment_attempt", return_value=5),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
    ):
        return _process_issue_codex_cloud_mode(
            "owner/repo",
            {"number": 2223, "title": "Retry safely", "body": "", "labels": []},
            config,
            MagicMock(),
            backend,
            retry_authority=authority,
        )


def test_controller_confirms_accepted_receipt_only_after_slot_membership(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    handoff = store.allocate_numeric_attempt(authority.request_id, [4])
    assert handoff.numeric_attempt == 5
    store.record_outcome(authority.request_id, "accepted", external_id="task-joined", tracking_complete=True)
    store.mark_tracking_complete(authority.request_id)
    CloudRunRepository("owner/repo").save(CloudRun("owner/repo", 2223, 5, "codex-cloud", task_id="task-joined", backend_name="codex-alias"))
    assert CloudManager("owner/repo").add_session(2223, "task-joined", "codex-cloud", "codex-alias")
    slots = ImplementationSlotRepository("owner/repo", 2)
    assert slots.reserve(ImplementationOwner("issue", 2223))
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots

    outcome, reason = _reconciler(engine).reconcile(authority.request_id)
    replay_outcome, _ = _reconciler(engine).reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.SUCCESS
    assert replay_outcome is ExplicitTargetOutcome.SUCCESS
    assert "phase=accepted-current" in reason
    assert slots.has_provider_sessions(ImplementationOwner("issue", 2223))
    confirmed = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert confirmed is not None
    assert confirmed.handoff_complete is True


def test_controller_reconstructs_missing_owner_without_starting_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(authority.request_id, "accepted", external_id="task-unjoined", tracking_complete=True)
    store.mark_tracking_complete(authority.request_id)
    CloudRunRepository("owner/repo").save(CloudRun("owner/repo", 2223, 5, "codex-cloud", task_id="task-unjoined", backend_name="codex-alias"))
    assert CloudManager("owner/repo").add_session(2223, "task-unjoined", "codex-cloud", "codex-alias")
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 2)

    outcome, reason = _reconciler(engine).reconcile(authority.request_id)
    replay_outcome, _ = _reconciler(engine).reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.SUCCESS
    assert replay_outcome is ExplicitTargetOutcome.SUCCESS
    assert "phase=accepted-current" in reason
    owner = ImplementationOwner("issue", 2223)
    snapshot = engine.implementation_slots.snapshot()
    restored = next(item for item in snapshot.owners if item.owner == owner)
    assert restored.provider_sessions == ("task-unjoined",)
    assert restored.executions == ()
    assert snapshot.normal_usage == 1
    assert snapshot.normal_available == 1
    unfinished = RetryDispatchRepository("owner/repo").list_unfinished_accepted()
    assert unfinished == ()
    completed = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert completed is not None
    assert completed.tracking_complete is True
    assert completed.handoff_complete is True


def test_missing_owner_recovery_counts_over_limit_and_preserves_existing_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(authority.request_id, "accepted", external_id="task-over-limit", tracking_complete=True)
    store.mark_tracking_complete(authority.request_id)
    CloudRunRepository("owner/repo").save(CloudRun("owner/repo", 2223, 5, "codex-cloud", task_id="task-over-limit", backend_name="codex-alias"))
    assert CloudManager("owner/repo").add_session(2223, "task-over-limit", "codex-cloud", "codex-alias")
    slots = ImplementationSlotRepository("owner/repo", 1)
    unrelated = ImplementationOwner("issue", 99)
    assert slots.reserve(unrelated)
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots

    outcome, _ = _reconciler(engine).reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.SUCCESS
    snapshot = slots.snapshot()
    assert {item.owner for item in snapshot.owners} == {unrelated, ImplementationOwner("issue", 2223)}
    assert snapshot.normal_usage == 2
    assert snapshot.normal_available == 0
    assert slots.active_execution_ids(ImplementationOwner("issue", 2223)) == ()


@pytest.mark.parametrize(
    "retained_url",
    [
        "https://chatgpt.com/remote/task_e_Recovered8",
        "https://chatgpt.com/codex/tasks/task_e_Recovered8",
        "https://chatgpt.com/codex/cloud/tasks/task_e_Recovered8",
        "",
    ],
)
@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_controller_recovers_missing_receipt_from_owned_request_and_accepted_run(mock_client_type, retained_url, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    run = CloudRun(
        "owner/repo",
        2223,
        5,
        "codex-cloud",
        task_id="task_e_Recovered8",
        backend_name="codex-original",
        environment_id="environment-original",
        base_branch="original-base",
        submission_outcome="accepted",
        task_url=retained_url,
        launch_identity=authority.request_id,
    )
    CloudRunRepository("owner/repo").save(run)
    assert CloudManager("owner/repo").add_session(2223, run.task_id, run.provider, run.backend_name)
    slots = ImplementationSlotRepository("owner/repo", 2)
    assert slots.reserve(ImplementationOwner("issue", 2223))
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots

    outcome, reason = _reconciler(engine).reconcile(authority.request_id)

    mock_client_type.assert_not_called()
    assert outcome is ExplicitTargetOutcome.SUCCESS
    assert "R=request-1" in reason
    recovered = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert recovered is not None
    assert (
        recovered.request_id,
        recovered.attempt_id,
        recovered.generation,
        recovered.numeric_attempt,
        recovered.external_id,
        recovered.external_url,
        recovered.backend_name,
        recovered.environment_id,
        recovered.creation_id,
        recovered.handoff_complete,
    ) == (
        authority.request_id,
        authority.attempt_id,
        authority.generation,
        5,
        "task_e_Recovered8",
        "https://chatgpt.com/remote/task_e_Recovered8",
        run.backend_name,
        run.environment_id,
        authority.ownership_reference,
        True,
    )


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_run_mismatch_schedules_live_handoff_and_next_turn_repairs(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(authority.request_id, "accepted", external_id="task-live", tracking_complete=True)
    store.mark_tracking_complete(authority.request_id)
    slots = ImplementationSlotRepository("owner/repo", 2)
    assert slots.reserve(ImplementationOwner("issue", 2223))
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots
    pending = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: pending)

    outcome, reason = _reconciler(engine).reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.DEFERRED
    assert "boundary=cloud-run" in reason
    assert "retained environment/base provenance is incomplete" in reason
    obligations = pending.all_pending()
    assert len(obligations) == 1
    incomplete = store.get(authority.request_id)
    assert incomplete is not None
    assert (incomplete.tracking_complete, incomplete.projection_disposition, incomplete.handoff_complete) == (
        True,
        "accepted-tracking-incomplete",
        False,
    )
    assert obligations[0].unfinished_effects == (CODEX_RETRY_HANDOFF_EFFECT,)
    run = CloudRun(
        "owner/repo",
        2223,
        5,
        "codex-cloud",
        task_id="task-live",
        backend_name="codex-alias",
        environment_id="environment-original",
        base_branch="main",
        submission_outcome="accepted",
    )
    CloudRunRepository("owner/repo").save(run)
    assert CloudManager("owner/repo").add_session(2223, run.task_id, run.provider, run.backend_name)

    engine._bind_pending_work_scheduler("owner/repo")

    async def dispatch_pending():
        assert engine.pending_work_scheduler is not None
        await engine.pending_work_scheduler._dispatch_due()
        await asyncio.gather(*tuple(engine.pending_work_scheduler._tasks))

    asyncio.run(dispatch_pending())

    assert pending.get(obligations[0].identity) is None
    completed = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert completed is not None
    assert (completed.projection_disposition, completed.handoff_complete) == ("accepted-current", True)
    mock_client_type.return_value.submit_task.assert_not_called()


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_startup_rediscovers_unfinished_receipt_through_ordinary_scheduler(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    dispatch.allocate_numeric_attempt(authority.request_id, [4])
    dispatch.record_outcome(authority.request_id, "accepted", external_id="task-startup", tracking_complete=True)
    dispatch.mark_tracking_complete(authority.request_id)
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="task-startup",
            backend_name="codex-alias",
            environment_id="environment-startup",
            base_branch="main",
            submission_outcome="accepted",
            launch_identity=authority.request_id,
        )
    )
    assert CloudManager("owner/repo").add_session(2223, "task-startup", "codex-cloud", "codex-alias")

    # Reopen every durable authority behind a fresh production controller.
    pending = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: pending)
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities()
    engine = AutomationEngine(github, config=AutomationConfig())
    engine.pending_work_scheduler = None
    process_candidate = MagicMock()
    monkeypatch.setattr(engine, "_process_single_candidate", process_candidate)
    monkeypatch.setattr(engine, "_producer_loop", lambda repo: asyncio.Event().wait())
    monkeypatch.setattr("auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("auto_coder.automation_engine.get_health_monitor", MagicMock())

    async def run_until_reconciled():
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        for _ in range(300):
            current = RetryDispatchRepository("owner/repo").get(authority.request_id)
            if current is not None and current.handoff_complete and not pending.all_pending():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_until_reconciled())

    completed = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert completed is not None and completed.handoff_complete
    assert completed.projection_disposition == "accepted-current"
    owner = ImplementationOwner("issue", 2223)
    reopened_slots = ImplementationSlotRepository("owner/repo", 2)
    assert reopened_slots.active_execution_ids(owner) == ()
    assert reopened_slots.has_provider_sessions(owner)
    assert pending.all_pending() == []
    process_candidate.assert_not_called()
    mock_client_type.return_value.submit_task.assert_not_called()


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_composed_startup_holds_admission_across_failed_enumeration_and_reopens_retained_state(mock_client_type, tmp_path, monkeypatch):
    """REQ-006: exercise recovery ordering through the real engine entrypoint."""
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority("request-composed-startup")
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    dispatch.allocate_numeric_attempt(authority.request_id, [4])
    dispatch.record_outcome(authority.request_id, "accepted", external_id="task-composed", tracking_complete=True)
    dispatch.mark_tracking_complete(authority.request_id)
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="task-composed",
            backend_name="codex-alias",
            environment_id="environment-composed",
            base_branch="main",
            submission_outcome="accepted",
            launch_identity=authority.request_id,
        )
    )
    assert CloudManager("owner/repo").add_session(2223, "task-composed", "codex-cloud", "codex-alias")

    jobs = LocalJobStore()

    def retained_result(repository: str, issue: int, attempt: str) -> str:
        owner, repo = repository.split("/", 1)
        identity = IssueAttemptIdentity(owner, repo, issue, attempt)
        guard = IssueDispatchGuard(tmp_path / f"{attempt}.sqlite3")
        assert guard.reserve(identity, CandidateHandoff("codex", "local")).admitted
        offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, repository, issue, attempt, "codex", "retained input")
        accepted = jobs.offer_issue(offer, identity, guard)
        assert accepted is not None
        claim = jobs.claim(accepted.job_id, f"dead-owner-{attempt}")
        assert claim is not None and jobs.mark_provider_entered(claim)
        artifact = jobs.persist_result_artifact(claim, InvocationOutcome.COMPLETED, "ACTION_SUMMARY: retained")
        assert artifact is not None and jobs.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id)
        assert jobs.get(accepted.job_id).state is LocalJobState.RESULT_RECORDED
        return accepted.job_id

    own_job = retained_result("owner/repo", 61, "own-retained")
    foreign_job = retained_result("other/repo", 62, "foreign-retained")
    reopened = LocalJobStore(jobs.path)

    checkpoint_entered = Event()
    release_checkpoint = Event()
    enumeration_calls = 0

    class CheckpointGithub(MagicMock):
        def get_open_entities_strict(self, repository):
            nonlocal enumeration_calls
            enumeration_calls += 1
            if enumeration_calls == 1:
                raise _github_error(GitHubApiOutcome.PRIMARY_THROTTLED)
            checkpoint_entered.set()
            assert release_checkpoint.wait(5)
            return OpenGitHubEntities()

    github = CheckpointGithub()
    engine = AutomationEngine(github, config=AutomationConfig())
    engine.local_job_store = reopened
    scheduled_identities: list[tuple[str, int]] = []
    original_schedule = engine._schedule_codex_retry_handoff

    def record_schedule(repository: str, request_id: str, issue_number: int) -> None:
        scheduled_identities.append((request_id, issue_number))
        original_schedule(repository, request_id, issue_number)

    monkeypatch.setattr(engine, "_schedule_codex_retry_handoff", record_schedule)
    pending = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda _repository: pending)
    monkeypatch.setattr("auto_coder.automation_engine.install_asyncio_diagnostics", lambda _loop: None)
    monkeypatch.setattr("auto_coder.automation_engine.get_health_monitor", MagicMock())
    monkeypatch.setattr(engine, "_resume_local_issue_publications", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(engine, "_enqueue_pending_invalidations", AsyncMock())

    producer_entered = asyncio.Event()
    worker_entries = 0

    async def producer(_repository):
        producer_entered.set()
        await asyncio.Event().wait()

    async def worker(*_args):
        nonlocal worker_entries
        worker_entries += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(engine, "_producer_loop", producer)
    monkeypatch.setattr(engine, "_worker_loop", worker)

    issue_poll_count = 0
    original_poll = LocalJobRunner.poll

    def counted_poll(runner):
        nonlocal issue_poll_count
        if LocalJobKind.ISSUE_IMPLEMENTATION in runner.adapters:
            issue_poll_count += 1
        return original_poll(runner)

    monkeypatch.setattr(LocalJobRunner, "poll", counted_poll)

    async def scenario():
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        assert await asyncio.to_thread(checkpoint_entered.wait, 5)
        assert enumeration_calls == 2
        assert not producer_entered.is_set()
        assert worker_entries == 0
        assert issue_poll_count == 0
        assert reopened.get(own_job).state is LocalJobState.RESULT_RECORDED
        assert reopened.get(foreign_job).state is LocalJobState.RESULT_RECORDED
        assert engine.pending_work_scheduler is not None
        assert len(engine.pending_work_scheduler._handlers) == 6
        assert scheduled_identities == [(authority.request_id, 2223)]

        # Repeating the same recovery turn retains one durable identity rather
        # than manufacturing a second effect or executor registration.
        engine._schedule_codex_retry_handoff("owner/repo", authority.request_id, 2223)
        engine._schedule_codex_retry_handoff("owner/repo", authority.request_id, 2223)
        matching = [item for item in pending.all_pending() if item.identity.revision == authority.request_id]
        assert len(matching) <= 1
        assert scheduled_identities == [(authority.request_id, 2223)] * 3
        assert len(set(scheduled_identities)) == 1
        if matching:
            assert matching[0].unfinished_effects == (CODEX_RETRY_HANDOFF_EFFECT,)
        assert len(engine.pending_work_scheduler._handlers) == 6

        release_checkpoint.set()
        await asyncio.wait_for(producer_entered.wait(), 5)
        for _ in range(300):
            if issue_poll_count and RetryDispatchRepository("owner/repo").get(authority.request_id).handoff_complete:
                break
            await asyncio.sleep(0.01)
        assert worker_entries == 2
        assert issue_poll_count > 0
        assert reopened.get(foreign_job).state is LocalJobState.RESULT_RECORDED
        completed = RetryDispatchRepository("owner/repo").get(authority.request_id)
        assert completed is not None and completed.handoff_complete
        assert len([item for item in pending.all_pending() if item.identity.revision == authority.request_id]) == 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    mock_client_type.return_value.submit_task.assert_not_called()


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_pending_reconciliation_cannot_reclaim_pointer_after_newer_acceptance_at_confirmation(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = _authority("request-1")
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(first, "codex-cloud", "codex-alias", {"base_branch": "main"})
    dispatch.allocate_numeric_attempt(first.request_id, [4])
    dispatch.record_outcome(first.request_id, "accepted", external_id="task-first")
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="task-first",
            backend_name="codex-alias",
            environment_id="environment-first",
            base_branch="main",
            submission_outcome="accepted",
            launch_identity=first.request_id,
        )
    )
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "task-first", "codex-cloud", "codex-alias")
    pending = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: pending)
    slots = ImplementationSlotRepository("owner/repo", 2)
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots
    engine._schedule_codex_retry_handoff("owner/repo", first.request_id, 2223)
    engine._bind_pending_work_scheduler("owner/repo")

    original_restore = slots.restore_accepted_retry_session
    restore_calls = 0

    def accept_newer_at_confirmation(owner, **kwargs):
        nonlocal restore_calls
        restore_calls += 1
        if restore_calls == 2:
            newer = _authority("request-2")
            dispatch.claim(newer, "codex-cloud", "codex-alias", {"base_branch": "main"})
            dispatch.allocate_numeric_attempt(newer.request_id, [5])
            dispatch.record_outcome(newer.request_id, "accepted", external_id="task-newer")
            assert manager.add_session(2223, "task-newer", "codex-cloud", "codex-alias")
        return original_restore(owner, **kwargs)

    monkeypatch.setattr(slots, "restore_accepted_retry_session", accept_newer_at_confirmation)

    async def dispatch_pending():
        assert engine.pending_work_scheduler is not None
        await engine.pending_work_scheduler._dispatch_due()
        await asyncio.gather(*tuple(engine.pending_work_scheduler._tasks))

    asyncio.run(dispatch_pending())

    first_after = RetryDispatchRepository("owner/repo").get(first.request_id)
    assert first_after is not None
    assert (first_after.projection_disposition, first_after.handoff_complete) == (
        "accepted-historical",
        True,
    )
    assert manager.read_bindings_strict()["2223"] == CloudTaskBinding("codex-cloud", "task-newer", "codex-alias")
    owner = ImplementationOwner("issue", 2223)
    assert slots.active_execution_ids(owner) == ()
    assert slots.has_provider_sessions(owner)
    assert pending.all_pending() == []
    mock_client_type.return_value.submit_task.assert_not_called()


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_pending_reconciliation_retries_failed_handoff_persistence(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    dispatch.allocate_numeric_attempt(authority.request_id, [4])
    dispatch.record_outcome(authority.request_id, "accepted", external_id="task-persistence")
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="task-persistence",
            backend_name="codex-alias",
            environment_id="environment-persistence",
            base_branch="main",
            submission_outcome="accepted",
            launch_identity=authority.request_id,
        )
    )
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "task-persistence", "codex-cloud", "codex-alias")
    pending = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: pending)
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine._schedule_codex_retry_handoff("owner/repo", authority.request_id, 2223)
    engine._bind_pending_work_scheduler("owner/repo")

    original_complete = RetryDispatchRepository.mark_handoff_complete
    fail_once = True

    def transient_failure(self, request_id):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("handoff journal unavailable")
        return original_complete(self, request_id)

    monkeypatch.setattr(RetryDispatchRepository, "mark_handoff_complete", transient_failure)

    async def dispatch_pending():
        assert engine.pending_work_scheduler is not None
        await engine.pending_work_scheduler._dispatch_due()
        await asyncio.gather(*tuple(engine.pending_work_scheduler._tasks))

    asyncio.run(dispatch_pending())
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert (retained.tracking_complete, retained.projection_disposition, retained.handoff_complete) == (
        True,
        "accepted-tracking-incomplete",
        False,
    )
    obligations = pending.all_pending()
    assert len(obligations) == 1
    assert obligations[0].unfinished_effects == (CODEX_RETRY_HANDOFF_EFFECT,)

    asyncio.run(dispatch_pending())

    completed = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert completed is not None
    assert (completed.projection_disposition, completed.handoff_complete) == (
        "accepted-current",
        True,
    )
    assert manager.read_bindings_strict()["2223"] == CloudTaskBinding("codex-cloud", "task-persistence", "codex-alias")
    assert pending.all_pending() == []
    mock_client_type.return_value.submit_task.assert_not_called()


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_pending_reconciliation_preserves_exact_retired_session_history(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    dispatch.allocate_numeric_attempt(authority.request_id, [4])
    dispatch.record_outcome(authority.request_id, "accepted", external_id="task-retired")
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="task-retired",
            backend_name="codex-alias",
            environment_id="environment-retired",
            base_branch="main",
            submission_outcome="accepted",
            launch_identity=authority.request_id,
        )
    )
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "unrelated-current", "codex-cloud", "other-backend")
    owner = ImplementationOwner("issue", 2223)
    slots = ImplementationSlotRepository("owner/repo", 2)
    with slots.serialize(owner):
        assert (
            slots.restore_accepted_retry_session(
                owner,
                request_id=authority.request_id,
                attempt_id=authority.attempt_id,
                generation=authority.generation,
                acquisition_reference=authority.ownership_reference,
                session_id="task-retired",
            )
            == "restored"
        )
    assert slots.record_implementation_pr(owner, 91)
    incarnation = slots.owner_incarnation(owner)
    revision = slots.owner_activity_revision(owner)
    assert incarnation is not None and revision is not None
    retirement = slots.retire_owner(
        ImplementationRetirementObservation(
            repository="owner/repo",
            owner=owner,
            reservation_incarnation=incarnation,
            activity_revision=revision,
            implementation_prs=(ImplementationPRObservation(91, PRTerminalState.CLOSED),),
            provider_sessions=(
                ProviderSessionObservation(
                    "task-retired",
                    "codex-cloud",
                    SessionTerminalState.ENDED,
                ),
            ),
            continuing_obligations=ContinuingObligations(),
        )
    )
    assert retirement.status is RetirementStatus.RELEASED

    pending_path = tmp_path / "pending.db"
    pending = PendingWorkStore(pending_path)
    monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda repository: pending)
    restarted = AutomationEngine(MagicMock(), config=AutomationConfig())
    restarted.implementation_slots = ImplementationSlotRepository("owner/repo", 2)
    restarted._schedule_codex_retry_handoff("owner/repo", authority.request_id, 2223)
    restarted._bind_pending_work_scheduler("owner/repo")

    async def dispatch_pending():
        assert restarted.pending_work_scheduler is not None
        await restarted.pending_work_scheduler._dispatch_due()
        await asyncio.gather(*tuple(restarted.pending_work_scheduler._tasks))

    asyncio.run(dispatch_pending())

    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert (retained.projection_disposition, retained.handoff_complete) == (
        "accepted-historical",
        True,
    )
    assert manager.read_bindings_strict()["2223"] == CloudTaskBinding("codex-cloud", "unrelated-current", "other-backend")
    reopened_slots = ImplementationSlotRepository("owner/repo", 2)
    assert reopened_slots.active_owners() == ()
    assert reopened_slots.has_retired_session("task-retired")
    assert pending.all_pending() == []
    mock_client_type.return_value.submit_task.assert_not_called()


def test_daemon_reconstructs_run_and_promotes_captured_legacy_predecessor(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "jules-predecessor", "jules", "")
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(
        authority,
        "codex-cloud",
        "codex-alias",
        {
            "base_branch": "original-base",
            "publication_head_repository": "owner/repo",
            "publication_head_ref": "issue-2223-attempt-5-codex-cloud",
        },
        predecessor=("jules", "jules-predecessor", ""),
    )
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(
        authority.request_id,
        "accepted",
        external_id="task-daemon-repaired",
        external_url="https://example.test/tasks/task-daemon-repaired",
        environment_id="original-environment",
    )
    slots = ImplementationSlotRepository("owner/repo", 2)
    assert slots.reserve(ImplementationOwner("issue", 2223))
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots

    outcome, reason = _reconciler(engine).reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.SUCCESS
    assert "phase=accepted-current" in reason
    assert manager.read_bindings_strict()["2223"] == CloudTaskBinding("codex-cloud", "task-daemon-repaired", "codex-alias")
    run = CloudRunRepository("owner/repo").get(2223, 5)
    assert run is not None
    assert (
        run.task_id,
        run.environment_id,
        run.base_branch,
        run.task_url,
        run.launch_identity,
        run.publication_head_repository,
        run.publication_head_ref,
    ) == (
        "task-daemon-repaired",
        "original-environment",
        "original-base",
        "https://example.test/tasks/task-daemon-repaired",
        authority.request_id,
        "owner/repo",
        "issue-2223-attempt-5-codex-cloud",
    )


@pytest.mark.parametrize("retained_url", ["https://chatgpt.com/codex/tasks/task_e_Retained7", None])
def test_daemon_reconstructs_canonical_url_from_retained_task_identity(tmp_path, monkeypatch, retained_url):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(
        authority,
        "codex-cloud",
        "codex-alias",
        {
            "base_branch": "main",
            "publication_head_repository": "owner/repo",
            "publication_head_ref": "issue-2223-attempt-5-codex-cloud",
        },
    )
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(
        authority.request_id,
        "accepted",
        external_id="task_e_Retained7",
        external_url=retained_url,
        environment_id="retained-environment",
    )
    slots = ImplementationSlotRepository("owner/repo", 2)
    assert slots.reserve(ImplementationOwner("issue", 2223))
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())
    engine.implementation_slots = slots

    outcome, _ = _reconciler(engine).reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.SUCCESS
    run = CloudRunRepository("owner/repo").get(2223, 5)
    assert run is not None
    assert run.task_id == "task_e_Retained7"
    assert run.task_url == "https://chatgpt.com/remote/task_e_Retained7"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_owned_retry_promotes_attributed_predecessor_and_replay_preserves_run(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "jules-old", provider="jules", backend_name="jules")
    client = mock_client_type.return_value
    client.environment_id = "environment-original"
    client.submit_task.return_value = CodexSubmissionResult(
        CodexSubmissionOutcome.ACCEPTED,
        "task-new",
        "https://example.test/tasks/task-new",
    )

    first = _dispatch(_authority())
    run_repo = CloudRunRepository("owner/repo")
    run_repo.add_pull_request(2223, 5, 91)
    replay = _dispatch(_authority(), backend="codex-alias", branch="changed-after-acceptance")

    client.submit_task.assert_called_once()
    assert first == ["Started Codex Cloud task 'task-new' for issue #2223"]
    assert "already accepted" in replay[0]
    assert manager.read_bindings_strict()["2223"] == CloudTaskBinding("codex-cloud", "task-new", "codex-alias")
    handoff = RetryDispatchRepository("owner/repo").get("request-1")
    assert handoff is not None
    assert (handoff.predecessor_provider, handoff.predecessor_task_id) == ("jules", "jules-old")
    assert handoff.projection_disposition == "accepted-current"
    run = run_repo.get(2223, 5)
    assert run is not None
    assert (run.environment_id, run.base_branch, run.task_url, run.pull_request_numbers) == (
        "environment-original",
        "main",
        "https://example.test/tasks/task-new",
        [91],
    )


def test_older_accepted_retry_is_retained_as_historical_when_later_receipt_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    old = CloudTaskBinding("jules", "jules-old", "jules")
    assert manager.add_session(2223, old.task_id, provider=old.provider, backend_name=old.backend_name)
    store = RetryDispatchRepository("owner/repo")
    for authority, task in ((_authority("request-1", "logical-1"), "task-1"), (_authority("request-2", "logical-2"), "task-2")):
        store.claim(authority, "codex-cloud", "codex", {"base_branch": "main"}, predecessor=(old.provider, old.task_id, old.backend_name))
        store.record_outcome(authority.request_id, "accepted", external_id=task)

    disposition = manager.promote_retry_binding(
        2223,
        CloudTaskBinding("codex-cloud", "task-1", "codex"),
        old,
        lambda: store.is_latest_accepted("request-1"),
    )
    store.mark_historical("request-1", "later receipt")

    assert disposition == "historical"
    assert manager.read_bindings_strict()["2223"] == old
    assert store.get("request-1").projection_disposition == "accepted-historical"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_unrecognized_conflict_retains_accepted_receipt_as_incomplete(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = mock_client_type.return_value
    client.environment_id = "env"
    client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-new")
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "known-old", provider="jules", backend_name="jules")

    original_promote = CloudManager.promote_retry_binding

    def replace_before_promotion(self, issue_number, binding, predecessor, may_promote, *args):
        self.add_session(issue_number, "unknown-racer", provider="claude-routine", backend_name="claude")
        return original_promote(self, issue_number, binding, predecessor, may_promote, *args)

    with patch.object(CloudManager, "promote_retry_binding", replace_before_promotion):
        result = _dispatch(_authority())

    assert "tracking is incomplete" in result[0]
    handoff = RetryDispatchRepository("owner/repo").get("request-1")
    assert handoff is not None
    assert handoff.outcome == "accepted"
    assert handoff.external_id == "task-new"
    assert handoff.projection_disposition == "accepted-tracking-incomplete"
    assert manager.read_bindings_strict()["2223"].task_id == "unknown-racer"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_absent_run_without_retained_environment_stays_incomplete(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    predecessor = CloudTaskBinding("jules", "jules-old", "jules")
    assert manager.add_session(
        2223,
        predecessor.task_id,
        provider=predecessor.provider,
        backend_name=predecessor.backend_name,
    )
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(
        authority,
        "codex-cloud",
        "codex-alias",
        {"base_branch": "main"},
        predecessor=(predecessor.provider, predecessor.task_id, predecessor.backend_name),
    )
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(
        authority.request_id,
        "accepted",
        external_id="task-retained",
        external_url="https://example.test/tasks/task-retained",
    )

    result = _dispatch(authority)

    mock_client_type.assert_not_called()
    assert result == ["Accepted Codex Cloud task 'task-retained' for issue #2223, but tracking is incomplete: " "Accepted cloud run provenance is incomplete"]
    assert CloudRunRepository("owner/repo").get(2223, 5) is None
    assert manager.read_bindings_strict()["2223"] == predecessor
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert retained.external_id == "task-retained"
    assert retained.projection_disposition == "accepted-tracking-incomplete"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_claimed_retry_recovers_receipt_from_matching_accepted_run(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = mock_client_type.return_value
    client.environment_id = "environment-retained"
    client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-recovered")
    authority = _authority()
    original = RetryDispatchRepository.record_outcome
    failed = False

    def fail_first_receipt(self, request_id, outcome, **kwargs):
        nonlocal failed
        if outcome == "accepted" and not failed:
            failed = True
            raise OSError("receipt journal unavailable")
        return original(self, request_id, outcome, **kwargs)

    with patch.object(RetryDispatchRepository, "record_outcome", fail_first_receipt):
        first = _dispatch(authority)
    replay = _dispatch(authority)

    assert "receipt could not be persisted" in first[0]
    assert "already accepted" in replay[0]
    client.submit_task.assert_called_once()
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert (retained.outcome, retained.external_id, retained.projection_disposition) == (
        "accepted",
        "task-recovered",
        "accepted-current",
    )


@pytest.mark.parametrize("retained_url", ["https://chatgpt.com/codex/tasks/task_e_Recovered8", ""])
@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_claimed_retry_canonicalizes_retained_run_url_without_resubmission(mock_client_type, retained_url, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(
        authority,
        "codex-cloud",
        "codex-alias",
        {
            "base_branch": "main",
            "publication_head_repository": "owner/repo",
            "publication_head_ref": "issue-2223-attempt-5-codex-cloud",
        },
    )
    store.allocate_numeric_attempt(authority.request_id, [4])
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="task_e_Recovered8",
            backend_name="codex-alias",
            environment_id="retained-environment",
            base_branch="main",
            submission_outcome="accepted",
            task_url=retained_url,
            launch_identity=authority.request_id,
            publication_head_repository="owner/repo",
            publication_head_ref="issue-2223-attempt-5-codex-cloud",
        )
    )

    result = _dispatch(authority)

    mock_client_type.return_value.submit_task.assert_not_called()
    assert "already accepted" in result[0]
    canonical = "https://chatgpt.com/remote/task_e_Recovered8"
    handoff = RetryDispatchRepository("owner/repo").get(authority.request_id)
    run = CloudRunRepository("owner/repo").get(2223, 5)
    assert handoff is not None and handoff.external_url == canonical
    assert run is not None and run.task_url == canonical


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_run_write_failure_retains_receipt_and_repairs_without_resubmission(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    client = mock_client_type.return_value
    client.environment_id = "environment-retained"
    client.submit_task.return_value = CodexSubmissionResult(
        CodexSubmissionOutcome.ACCEPTED,
        "task-after-run-failure",
        "https://example.test/task-after-run-failure",
    )
    original = CloudRunRepository.update_claim
    failed = False

    def fail_once(self, run):
        nonlocal failed
        if run.submission_outcome == "accepted" and not failed:
            failed = True
            raise OSError("run store unavailable")
        return original(self, run)

    with patch.object(CloudRunRepository, "update_claim", fail_once):
        first = _dispatch(authority)
    replay = _dispatch(authority)

    assert "tracking is incomplete" in first[0]
    assert "already accepted" in replay[0]
    client.submit_task.assert_called_once()
    run = CloudRunRepository("owner/repo").get(2223, 5)
    assert run is not None
    assert (run.task_id, run.environment_id, run.submission_outcome) == (
        "task-after-run-failure",
        "environment-retained",
        "accepted",
    )
    handoff = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert handoff is not None
    assert (handoff.external_id, handoff.environment_id, handoff.projection_disposition) == (
        "task-after-run-failure",
        "environment-retained",
        "accepted-current",
    )


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_reconciler_repairs_indeterminate_run_claim_without_resubmission(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    store = RetryDispatchRepository("owner/repo")
    store.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    store.allocate_numeric_attempt(authority.request_id, [4])
    store.record_outcome(
        authority.request_id,
        "accepted",
        external_id="task-after-run-failure",
        environment_id="environment-retained",
    )
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            backend_name="codex-alias",
            environment_id="environment-retained",
            base_branch="main",
            submission_outcome="indeterminate",
            launch_identity=authority.request_id,
        )
    )
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())

    outcome, _ = engine._codex_retry_reconciler("owner/repo").reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.SUCCESS
    mock_client_type.return_value.submit_task.assert_not_called()
    run = CloudRunRepository("owner/repo").get(2223, 5)
    assert run is not None
    assert (run.task_id, run.submission_outcome, run.launch_identity) == (
        "task-after-run-failure",
        "accepted",
        authority.request_id,
    )


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_missing_receipt_rejects_accepted_run_from_another_request(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    run = CloudRun(
        "owner/repo",
        2223,
        5,
        "codex-cloud",
        task_id="task-earlier",
        backend_name="codex-alias",
        environment_id="environment-earlier",
        base_branch="main",
        submission_outcome="accepted",
        launch_identity="different-request",
    )
    CloudRunRepository("owner/repo").save(run)
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, run.task_id, run.provider, run.backend_name)
    engine = AutomationEngine(MagicMock(), config=AutomationConfig())

    outcome, _ = engine._codex_retry_reconciler("owner/repo").reconcile(authority.request_id)

    assert outcome is ExplicitTargetOutcome.DEFERRED
    mock_client_type.return_value.submit_task.assert_not_called()
    assert RetryDispatchRepository("owner/repo").get(authority.request_id) is None
    assert CloudRunRepository("owner/repo").get(2223, 5) == run
    assert manager.read_bindings_strict()["2223"].task_id == "task-earlier"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_legacy_accepted_retry_confirms_matching_current_projection(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    routing_path = Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3"
    with sqlite3.connect(routing_path) as connection:
        connection.execute(
            "UPDATE implementation_retry_requests SET predecessor_captured=0,predecessor_provider=NULL,predecessor_task_id=NULL,predecessor_backend_name=NULL WHERE request_id=?",
            (authority.request_id,),
        )
    authority = IssueStageRoutingStore(routing_path).retry_request(authority.request_id)
    assert authority is not None
    dispatch = RetryDispatchRepository("owner/repo")
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    handoff = dispatch.allocate_numeric_attempt(authority.request_id, [4])
    assert handoff.numeric_attempt == 5
    dispatch.record_outcome(
        authority.request_id,
        "accepted",
        external_id="legacy-task",
        external_url="https://example.test/legacy-task",
    )
    CloudRunRepository("owner/repo").save(
        CloudRun(
            "owner/repo",
            2223,
            5,
            "codex-cloud",
            task_id="legacy-task",
            backend_name="codex-alias",
            environment_id="legacy-environment",
            base_branch="main",
            task_url="https://example.test/legacy-task",
        )
    )
    assert CloudManager("owner/repo").add_session(2223, "legacy-task", "codex-cloud", "codex-alias")

    result = _dispatch(authority)

    mock_client_type.assert_not_called()
    assert "already accepted" in result[0]
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert retained.projection_disposition == "accepted-current"


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_unreadable_or_mismatched_durable_authority_prevents_creation(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = _authority()
    with patch.object(IssueStageRoutingStore, "retry_request", side_effect=OSError("authority store unreadable")):
        unreadable = _dispatch(authority)

    with (
        patch("auto_coder.issue_processor.get_current_attempt", return_value=4),
        patch("auto_coder.issue_processor.get_commit_log", return_value=""),
    ):
        mismatched = _process_issue_codex_cloud_mode(
            "owner/repo",
            {"number": 2224, "title": "Wrong target", "body": "", "labels": []},
            AutomationConfig(),
            MagicMock(),
            "codex-alias",
            retry_authority=authority,
        )

    mock_client_type.assert_not_called()
    assert "authority store unreadable" in unreadable[0]
    assert "does not match owned dispatch authority" in mismatched[0]


@patch("auto_coder.codex_cloud_client.CodexCloudClient")
def test_binding_installed_after_admission_is_not_treated_as_predecessor(mock_client_type, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    assert manager.add_session(2223, "predecessor", provider="jules", backend_name="jules")
    authority = _authority()
    assert manager.add_session(2223, "unrelated", provider="claude-routine", backend_name="claude")
    client = mock_client_type.return_value
    client.environment_id = "environment"
    client.submit_task.return_value = CodexSubmissionResult(CodexSubmissionOutcome.ACCEPTED, "task-new")

    result = _dispatch(authority)

    assert "tracking is incomplete" in result[0]
    assert manager.read_bindings_strict()["2223"].task_id == "unrelated"
    retained = RetryDispatchRepository("owner/repo").get(authority.request_id)
    assert retained is not None
    assert retained.projection_disposition == "accepted-tracking-incomplete"


def test_acceptance_and_cross_provider_promotion_share_one_stale_writer_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    manager = CloudManager("owner/repo")
    predecessor = CloudTaskBinding("jules", "predecessor", "jules")
    assert manager.add_session(2223, predecessor.task_id, predecessor.provider, predecessor.backend_name)
    first = _authority("request-1")
    second = _authority("request-2")
    store = RetryDispatchRepository("owner/repo")
    for authority, route in (
        (first, "claude-routine"),
        (second, "codex-cloud"),
    ):
        store.claim(
            authority,
            route,
            route,
            {"base_branch": "main"},
            predecessor=(predecessor.provider, predecessor.task_id, predecessor.backend_name),
        )
    store.record_outcome(first.request_id, "accepted", external_id="claude-task")

    reached_check = Event()
    release_check = Event()

    def older_promotion() -> str:
        def checked() -> bool:
            reached_check.set()
            release_check.wait(timeout=5)
            return store.is_latest_accepted(first.request_id)

        return manager.promote_retry_binding(
            2223,
            CloudTaskBinding("claude-routine", "claude-task", "claude-routine"),
            predecessor,
            checked,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(older_promotion)
        assert reached_check.wait(timeout=5)

        def accept_and_promote_newer() -> str:
            store.record_outcome(second.request_id, "accepted", external_id="codex-task")
            disposition = manager.promote_retry_binding(
                2223,
                CloudTaskBinding("codex-cloud", "codex-task", "codex-cloud"),
                predecessor,
                lambda: store.is_latest_accepted(second.request_id),
                (CloudTaskBinding("claude-routine", "claude-task", "claude-routine"),),
            )
            store.mark_prior_accepted_historical(second.request_id)
            return disposition

        newer = pool.submit(accept_and_promote_newer)
        release_check.set()
        assert older.result(timeout=5) == "current"
        assert newer.result(timeout=5) == "current"

    assert manager.read_bindings_strict()["2223"].task_id == "codex-task"
    assert store.get(first.request_id).projection_disposition == "accepted-historical"

    assert (
        _acknowledge_retry_projection(
            manager,
            store,
            first.request_id,
            2223,
            CloudTaskBinding("claude-routine", "claude-task", "claude-routine"),
        )
        == "historical"
    )
    assert store.get(first.request_id).projection_disposition == "accepted-historical"


import sqlite3
