import asyncio
import threading
from unittest.mock import MagicMock

from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult
from auto_coder.automation_engine import AutomationEngine
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.util.gh_cache import OpenGitHubEntities, OpenGitHubIssue


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

    def process(_repo, candidate):
        attempted.append(candidate.issue_number)
        if candidate.issue_number == 20:
            assert engine.implementation_slots.reserve_new(ImplementationOwner("issue", 20))
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, title="", success=False, actions=[])

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        await asyncio.sleep(0.03)
        # A distinct repository instance represents another auto-coder process.
        ImplementationSlotRepository("owner/repo", 1, path).release(occupying)
        for _ in range(100):
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

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        await asyncio.sleep(0.03)
        ImplementationSlotRepository("owner/repo", 1, path).release(occupying)
        for _ in range(100):
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

    def process(_repo, _candidate):
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

    def process(_repo, candidate):
        owner = ImplementationOwner("issue", candidate.issue_number)
        assert engine.implementation_slots.reserve_new(owner)
        if candidate.issue_number == 20:
            dispatch_started.set()
            assert dispatch_can_finish.wait(2)
        return CandidateProcessingResult(type="issue", number=candidate.issue_number, success=True)

    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("auto_coder.automation_engine.CAPACITY_STATE_CHECK_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("auto_coder.automation_engine.REFILL_RETRY_INTERVAL_SECONDS", 0.01)

    async def scenario():
        task = asyncio.create_task(engine._capacity_refill_loop("owner/repo"))
        await asyncio.sleep(0.03)
        external = ImplementationSlotRepository("owner/repo", 1, path)
        external.release(original)
        assert await asyncio.to_thread(dispatch_started.wait, 2)
        external.release(ImplementationOwner("issue", 20))
        dispatch_can_finish.set()
        for _ in range(100):
            if github.get_open_entities_strict.call_count >= 2 and engine.implementation_slots.available_normal_slots() == 0:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert github.get_open_entities_strict.call_count == 2
    assert ImplementationOwner("issue", 30) in engine.implementation_slots.active_owners()


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
