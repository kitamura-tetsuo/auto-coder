"""Regression coverage for PR scheduling behind a backlog of Issues."""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.candidate_queue import CandidateQueue


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
