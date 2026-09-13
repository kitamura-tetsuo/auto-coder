"""Regression coverage for PR scheduling behind a backlog of Issues."""

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.candidate_queue import CandidateQueue


@pytest.mark.parametrize("blocked_type", ["issue", "pr"])
def test_dedicated_workers_progress_while_other_type_is_busy(tmp_path, monkeypatch, blocked_type):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    other_type = "pr" if blocked_type == "issue" else "issue"

    def fetch(repo, kind, number, propagate_errors=False):
        return Candidate(type=kind, data={"number": number, "state": "open"}, priority=0)

    def process(repo, candidate, **kwargs):
        if candidate.type == blocked_type:
            entered.set()
            assert release.wait(5)
        else:
            completed.set()
        return CandidateProcessingResult(type=candidate.type, number=candidate.data["number"], success=True)

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", lambda *args: None)
    monkeypatch.setattr(engine, "_refresh_issue_stage_routing", lambda *args: None)

    async def scenario():
        workers = [asyncio.create_task(engine._worker_loop("owner/repo", i, kind)) for i, kind in enumerate(("issue", "pr"))]
        try:
            await engine.invalidate_entity("owner/repo", blocked_type, 10)
            assert await asyncio.to_thread(entered.wait, 2)
            await engine.invalidate_entity("owner/repo", other_type, 20)
            assert await asyncio.to_thread(completed.wait, 2)
            blocked_id = 0 if blocked_type == "issue" else 1
            assert engine.active_workers[blocked_id].type == blocked_type
            release.set()
            await asyncio.wait_for(engine.queue.join(), 5)
            assert engine.invalidations.pending_count("owner/repo") == 0
        finally:
            release.set()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    asyncio.run(scenario())


def test_typed_queue_waiters_cancel_without_consuming_other_lane():
    async def scenario():
        queue = CandidateQueue()
        waiter = asyncio.create_task(queue.get_for_type("pr"))
        await queue.put(Candidate(type="issue", data={"number": 1}, priority=0))
        await asyncio.sleep(0)
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await queue.get_for_type("issue")).data["number"] == 1
        queue.task_done()
        await queue.put(Candidate(type="dependency", data={"number": 3}, priority=0))
        await queue.put(Candidate(type="pr", data={"number": 2}, priority=1))
        assert (await queue.get_for_type("pr")).data["number"] == 2
        queue.task_done()
        assert (await queue.get_for_type("issue")).type == "dependency"
        queue.task_done()
        await asyncio.wait_for(queue.join(), 1)

    asyncio.run(scenario())


def test_equal_priority_is_stable_and_join_tracks_every_candidate():
    async def scenario():
        queue = CandidateQueue()
        for number, priority in [(10, 0), (11, 0), (20, 1), (21, 1)]:
            await queue.put(Candidate(type="pr" if priority else "issue", data={"number": number}, priority=priority))
        assert [queue.get_nowait().data["number"] for _ in range(4)] == [20, 21, 10, 11]
        for _ in range(4):
            queue.task_done()
        await asyncio.wait_for(queue.join(), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("restart", [False, True])
def test_durable_prs_overtake_issue_backlog_without_losing_generations(tmp_path, monkeypatch, restart):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    processed = []
    fetched = []

    async def scenario():
        nonlocal engine
        for kind, number in [("issue", 1980), ("issue", 1981), ("pr", 2004), ("pr", 2005)]:
            await engine.invalidate_entity("owner/repo", kind, number)
        await engine.invalidate_entity("owner/repo", "pr", 2004)
        if restart:
            engine = AutomationEngine(MagicMock(), AutomationConfig())
            engine.invalidations.recover("owner/repo")
            await engine._enqueue_pending_invalidations("owner/repo")

        def fetch(repo, kind, number, propagate_errors=False):
            fetched.append((kind, number))
            return Candidate(type=kind, data={"number": number, "state": "open"}, priority=0)

        def process(repo, candidate, **kwargs):
            processed.append((candidate.type, candidate.data["number"], candidate.invalidation_generation))
            return CandidateProcessingResult(type=candidate.type, number=candidate.data["number"], success=True)

        monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
        monkeypatch.setattr(engine, "_process_single_candidate", process)
        monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", lambda *args: None)
        monkeypatch.setattr(engine, "_refresh_issue_stage_routing", lambda *args: None)
        status = engine.get_status()
        assert [item["number"] for item in status["queue_items"]] == [2004, 2005, 1980, 1981]
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            await asyncio.wait_for(engine.queue.join(), 5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert engine.invalidations.pending_count("owner/repo") == 0

    asyncio.run(scenario())
    assert fetched == [("pr", 2004), ("pr", 2005), ("issue", 1980), ("issue", 1981)]
    assert processed == [("pr", 2004, 1), ("pr", 2005, 1), ("issue", 1980, 1), ("issue", 1981, 1)]
