import asyncio
from pathlib import Path
from unittest.mock import MagicMock

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity
from src.auto_coder.webhook_server import process_github_payload


def test_durable_queue_coalesces_and_recovers_in_flight_work(tmp_path: Path):
    path = tmp_path / "invalidations.sqlite3"
    identity = EntityIdentity("owner/repo", "pr", 100)
    queue = DurableInvalidationQueue(path)

    for _ in range(5):
        assert queue.invalidate(identity)
    assert queue.pending_count("owner/repo") == 1

    first = queue.claim("owner/repo")
    assert first is not None
    assert first.generation == 5
    queue.invalidate(identity)
    assert queue.complete(first)
    second = queue.claim("owner/repo")
    assert second is not None
    assert second.generation == 6

    restarted = DurableInvalidationQueue(path)
    restarted.recover("owner/repo")
    recovered = restarted.claim("owner/repo")
    assert recovered is not None
    assert recovered.identity == identity
    assert recovered.generation == 6


def test_duplicate_delivery_does_not_advance_generation(tmp_path: Path):
    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    identity = EntityIdentity("owner/repo", "issue", 42)
    assert queue.invalidate(identity, "delivery-1")
    assert not queue.invalidate(identity, "delivery-1")
    claim = queue.claim("owner/repo")
    assert claim is not None
    assert claim.generation == 1


def test_webhook_origin_coalesces_and_worker_observes_inflight_invalidation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    engine = AutomationEngine(github, AutomationConfig())
    fetched_states = iter(["first", "later"])

    def create_candidate(repo_name, entity_type, number):
        state = next(fetched_states)
        return Candidate(type=entity_type, data={"number": number, "state": state}, priority=0)

    processing_started = asyncio.Event()
    allow_completion = asyncio.Event()
    observed = []

    def process_candidate(repo_name, candidate):
        observed.append(candidate.data["state"])
        if candidate.data["state"] == "first":
            processing_started_loop.call_soon_threadsafe(processing_started.set)
            asyncio.run_coroutine_threadsafe(allow_completion.wait(), processing_started_loop).result()
        return CandidateProcessingResult(type="pr", number=100, success=True)

    async def scenario():
        nonlocal processing_started_loop
        processing_started_loop = asyncio.get_running_loop()
        monkeypatch.setattr(engine, "_create_candidate_from_single", create_candidate)
        monkeypatch.setattr(engine, "_process_single_candidate", process_candidate)
        monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)

        payload = {"action": "opened", "pull_request": {"number": 100, "title": "stale snapshot"}}
        for index in range(5):
            await process_github_payload("pull_request", payload, engine, "owner/repo", f"delivery-{index}")
        assert engine.invalidations.pending_count("owner/repo") == 1
        assert engine.queue.qsize() == 1

        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await processing_started.wait()
        await process_github_payload("pull_request", {"action": "synchronize", "pull_request": {"number": 100}}, engine, "owner/repo", "delivery-later")
        allow_completion.set()
        for _ in range(100):
            if observed == ["first", "later"] and engine.invalidations.pending_count("owner/repo") == 0:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    processing_started_loop = None
    asyncio.run(scenario())
    assert observed == ["first", "later"]
    assert engine.invalidations.pending_count("owner/repo") == 0
