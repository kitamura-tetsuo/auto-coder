import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository, ImplementationSlotUnavailable
from auto_coder.util.gh_cache import OpenGitHubEntities, OpenGitHubIssue


def test_reclamation_propagates_provider_store_construction_failure(tmp_path):
    """REQ-002: daemon setup failure reaches the collector as unavailable evidence."""
    github = MagicMock()
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    due_checks = MagicMock(return_value=0)

    with (
        patch("auto_coder.cloud_manager.CloudManager", return_value=MagicMock()),
        patch("auto_coder.cloud_run.CloudRunRepository", side_effect=RuntimeError("store unavailable")),
        patch("auto_coder.automation_engine.run_due_reclamation_checks", due_checks),
    ):
        assert asyncio.run(engine._run_due_reclamation_checks("owner/repo", slots)) == 0

    assert due_checks.call_count == 1
    assert due_checks.call_args.kwargs["cloud_provider_stores_available"] is False


def test_external_capacity_release_refills_fresh_ranking_past_rejection(monkeypatch, tmp_path):
    """The daemon production watcher refetches, reranks, and does not stop at rejection."""
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    snapshots = {
        20: {"number": 20, "state": "open", "created_at": "2026-01-02T00:00:00Z", "labels": [{"name": "implementation-ready"}]},
        30: {"number": 30, "state": "open", "created_at": "2026-01-03T00:00:00Z", "labels": [{"name": "implementation-ready"}, {"name": "urgent"}]},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    occupying = ImplementationOwner("issue", 10)
    assert engine.implementation_slots.reserve_new(occupying)
    attempted = []

    def process(_repo, candidate, **_kwargs):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20:
            assert engine.implementation_slots.reserve_new(ImplementationOwner("issue", 20))
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, title="", success=False, actions=[])

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        # A distinct repository instance represents another auto-coder process.
        ImplementationSlotRepository("owner/repo", 1, path).release(occupying)
        for _ in range(300):
            if attempted == [30, 20]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert attempted == [30, 20]
    github.get_open_entities_strict.assert_called_once_with("owner/repo")
    assert engine.implementation_slots.available_normal_slots() == 0


def test_failed_refill_enumeration_remains_pending(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.side_effect = [RuntimeError("temporary outage"), OpenGitHubEntities()]
    engine = AutomationEngine(github, AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    occupying = ImplementationOwner("issue", 10)
    assert engine.implementation_slots.reserve_new(occupying)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("auto_coder.automation_engine.REFILL_RETRY_INTERVAL_SECONDS", 0.01)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        ImplementationSlotRepository("owner/repo", 1, path).release(occupying)
        for _ in range(300):
            if github.get_open_entities_strict.call_count == 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert github.get_open_entities_strict.call_count == 2


def test_dispatch_authority_failure_keeps_refill_pending(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20)])
    snapshot = {"number": 20, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_dispatch_snapshot_strict.return_value = snapshot
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    attempts = 0

    def process(_repo, _candidate, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CandidateProcessingResult(type="issue", number=20, error="authoritative read failed", refill_retry_required=True)
        assert engine.implementation_slots.reserve_new(ImplementationOwner("issue", 20))
        return CandidateProcessingResult(type="issue", number=20, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is False
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempts == 2


def test_unexpected_candidate_fault_pauses_only_actual_issue_and_continues(monkeypatch, tmp_path):
    """REQ-001/002/004/006/008: isolate a fault without replay or global stop."""
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {
        "number": number,
        "state": "open",
        "labels": [{"name": "implementation-ready"}],
    }
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    attempted = []

    def process(_repo, candidate, **_kwargs):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20:
            raise RuntimeError("effect state is unknown")
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)

    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempted == [20, 30]
    fault = engine.get_status()["refill_faults"]
    assert fault == [
        {
            "repository": "owner/repo",
            "target": 20,
            "phase": "candidate_dispatch",
            "exception_class": "RuntimeError",
            "disposition": "intervention_required",
            "retry_not_before": None,
        }
    ]

    paused = engine._process_single_candidate_unified(
        "owner/repo",
        Candidate(type="issue", data={"number": 20}, priority=0),
        engine.config,
    )
    assert paused.target_outcome is not None
    assert paused.error and "paused" in paused.error


def test_capacity_fault_clears_only_after_valid_store_observation(monkeypatch, tmp_path):
    """REQ-003/008/009: missing evidence is unavailable, never synthetic capacity."""
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    observations = [RuntimeError("corrupt image"), (1, (4, 9))]

    def observe():
        value = observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(slots, "normal_capacity_snapshot", observe)

    assert asyncio.run(engine._observe_refill_capacity("owner/repo", slots, "capacity_observation")) is None
    fault = engine.get_status()["refill_faults"][0]
    assert fault["disposition"] == "capacity_unavailable"
    assert fault["retry_not_before"] is not None
    assert engine._refill_admission_paused("owner/repo", 99)

    assert asyncio.run(engine._observe_refill_capacity("owner/repo", slots, "capacity_observation")) == (1, (4, 9))
    assert engine.get_status()["refill_faults"] == []


def test_refill_initialization_fault_keeps_service_paused_until_cancel(monkeypatch):
    """REQ-001/002/009: unusable shared state pauses without ending the task."""
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    monkeypatch.setattr(
        engine,
        "_get_implementation_slots",
        MagicMock(side_effect=RuntimeError("store initialization failed")),
    )
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        for _ in range(100):
            if engine.get_status()["refill_faults"]:
                break
            await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(scenario())
    assert engine.get_status()["refill_faults"] == [
        {
            "repository": "owner/repo",
            "target": None,
            "phase": "service_initialization",
            "exception_class": "RuntimeError",
            "disposition": "intervention_required",
            "retry_not_before": None,
        }
    ]


def test_ownership_failure_does_not_abort_other_refill_candidates(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(20), OpenGitHubIssue(30)])
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: {"number": number, "state": "open", "labels": [{"name": "implementation-ready"}]}
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    attempted = []

    def implementation(_repo, candidate, *_args):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20 and attempted.count(20) == 1:
            raise ImplementationSlotUnavailable("Timed out acquiring runtime lock 'owner.lock'")
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate_unified_impl", implementation)
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is False
    assert attempted == [20, 30]
    assert asyncio.run(engine._refill_normal_implementation_slots("owner/repo")) is True
    assert attempted == [20, 30, 20, 30]


def test_release_during_refill_causes_second_fresh_enumeration(monkeypatch, tmp_path):
    github = MagicMock()
    github.get_open_entities_strict.side_effect = [
        OpenGitHubEntities(issues=[OpenGitHubIssue(20)]),
        OpenGitHubEntities(issues=[OpenGitHubIssue(30)]),
    ]
    snapshots = {
        20: {"number": 20, "state": "open", "labels": [{"name": "implementation-ready"}]},
        30: {"number": 30, "state": "open", "labels": [{"name": "implementation-ready"}]},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: snapshots[number]
    github.get_issue_details.side_effect = lambda issue: issue
    engine = AutomationEngine(github, AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    original = ImplementationOwner("issue", 10)
    assert engine.implementation_slots.reserve_new(original)
    dispatch_started = threading.Event()
    dispatch_can_finish = threading.Event()

    def process(_repo, candidate, **_kwargs):
        owner = ImplementationOwner("issue", candidate.issue_number)
        assert engine.implementation_slots.reserve_new(owner)
        if candidate.issue_number == 20:
            dispatch_started.set()
            assert dispatch_can_finish.wait(5)
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("auto_coder.automation_engine.REFILL_RETRY_INTERVAL_SECONDS", 0.01)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        external = ImplementationSlotRepository("owner/repo", 1, path)
        external.release(original)
        assert await asyncio.to_thread(dispatch_started.wait, 5)
        external.release(ImplementationOwner("issue", 20))
        dispatch_can_finish.set()
        for _ in range(300):
            if github.get_open_entities_strict.call_count >= 2 and engine.implementation_slots.available_normal_slots() == 0:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert github.get_open_entities_strict.call_count == 2
    assert ImplementationOwner("issue", 30) in engine.implementation_slots.active_owners()


def test_fill_and_release_between_ordinary_samples_triggers_refill(monkeypatch, tmp_path):
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    path = tmp_path / "slots.json"
    engine.implementation_slots = ImplementationSlotRepository("owner/repo", 1, path)
    refill = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "_refill_normal_implementation_slots", refill)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.05)

    started = threading.Event()
    original_snapshot = engine.implementation_slots.normal_capacity_snapshot

    def snapshot_wrapper():
        result = original_snapshot()
        started.set()
        return result

    monkeypatch.setattr(engine.implementation_slots, "normal_capacity_snapshot", snapshot_wrapper)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        assert await asyncio.to_thread(started.wait, 5)
        external = ImplementationSlotRepository("owner/repo", 1, path)
        transient = ImplementationOwner("issue", 40)
        assert external.reserve_new(transient)
        external.release(transient)
        for _ in range(300):
            if refill.await_count:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    refill.assert_awaited_once_with("owner/repo")


def test_available_normal_slots_excludes_emergency_capacity(tmp_path):
    slots = ImplementationSlotRepository("owner/repo", 2, tmp_path / "slots.json")
    assert slots.available_normal_slots() == 2
    assert slots.reserve_new(ImplementationOwner("issue", 1))
    assert slots.available_normal_slots() == 1
    execution = slots.start_execution(ImplementationOwner("issue", 2), allow_urgent_emergency=True)
    assert execution is not None
    assert slots.available_normal_slots() == 0
    emergency = slots.start_execution(ImplementationOwner("issue", 3), allow_urgent_emergency=True)
    assert emergency is not None
    assert slots.available_normal_slots() == 0
