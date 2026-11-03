"""Production worker/scheduler regressions for failed authoritative refreshes."""

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from src.auto_coder.automation_config import AutomationConfig, CandidateProcessingResult, PRProcessingOutcome
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.dashboard_detail import evidence_rows
from src.auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity
from src.auto_coder.execution_trace import TraceCollector, get_trace_collector
from src.auto_coder.util.gh_cache import GitHubClient


@pytest.mark.parametrize("failure_kind", ["transport", "malformed", "returned-error"])
@pytest.mark.parametrize("later_stabilization", [False, True])
def test_failed_refresh_cannot_spin_on_generic_wakes_or_restart(tmp_path: Path, monkeypatch, failure_kind: str, later_stabilization: bool) -> None:
    monkeypatch.setattr(TraceCollector, "_instance", None)
    database = tmp_path / "invalidations.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(database))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    attempts = []
    identity = EntityIdentity("owner/repo", "pr", 100)
    stabilization = time.time() + 120 if later_stabilization else None

    def fail_refresh(*_args):
        attempts.append(time.monotonic())
        engine.invalidations.invalidate(identity, delivery_id="during-refresh", not_before=stabilization)
        if failure_kind == "transport":
            raise httpx.ConnectError("connection refused", request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/100"))
        if failure_kind == "returned-error":
            return {"number": 100, "title": "Current authority", "body": "", "state": "open", "user": {"login": "contributor", "id": 1}, "head": {"ref": "topic"}}
        raise ValueError("authoritative PR metadata is malformed")

    monkeypatch.setattr(github, "get_pull_request_metadata_strict", fail_refresh)
    monkeypatch.setattr(engine, "_is_pr_author_allowed", lambda _data: True)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda *_args, **_kwargs: CandidateProcessingResult(type="pr", number=100, error="processing responsibility is unfinished"))

    async def scenario():
        consumer = asyncio.create_task(engine._invalidation_loop("owner/repo"))
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            await engine.invalidate_entity("owner/repo", "pr", 100)
            for _ in range(200):
                if attempts and engine.invalidations.get_deferred(identity) is not None:
                    break
                await asyncio.sleep(0.005)
            assert attempts, "the production worker never attempted its strict refresh"
            for _ in range(5):
                assert engine._invalidation_wake_event is not None
                engine.invalidations.invalidate(identity, delivery_id="newer-notification")
                engine._invalidation_wake_event.set()
                await asyncio.sleep(0.02)
            assert len(attempts) == 1, "an unrelated wake bypassed the failed refresh backoff"
        finally:
            consumer.cancel()
            worker.cancel()
            await asyncio.gather(consumer, worker, return_exceptions=True)

    asyncio.run(scenario())
    recovered = DurableInvalidationQueue(database)
    recovered.recover("owner/repo")
    assert recovered.pending_count("owner/repo") == 1
    assert recovered.claim("owner/repo") is None, "process reconstruction erased the retry guard"
    retained = recovered.get_deferred(identity)
    assert retained is not None
    assert retained.generation == 2, "the notification received during refresh was lost"
    assert retained.retry_not_before > time.time() + 55
    if stabilization is not None:
        assert retained.retry_not_before >= stabilization
    events = [event for event in get_trace_collector().get_snapshot(repository="owner/repo", item_type="pr", item_number=100).events if event.stage_id == "worker.failure-backoff" and event.kind == "stage-result"]
    assert len(events) == 1 and events[0].outcome == "deferred"
    assert events[0].facts["retry_at"] == retained.retry_not_before
    rows = evidence_rows(events)
    assert len(rows) == 1 and rows[0]["outcome"] == "deferred"
    assert "operational_processing_failure" in rows[0]["facts"]
    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: retained.retry_not_before)
    restarted = AutomationEngine(github, AutomationConfig())
    restarted.invalidations.recover("owner/repo")
    processed = []

    def current_refresh(*_args):
        attempts.append(time.monotonic())
        return {"number": 100, "title": "Current authority", "body": "", "state": "open", "user": {"login": "contributor", "id": 1}, "head": {"ref": "topic"}}

    monkeypatch.setattr(github, "get_pull_request_metadata_strict", current_refresh)
    monkeypatch.setattr(restarted, "_is_pr_author_allowed", lambda _data: True)
    monkeypatch.setattr(restarted, "_process_single_candidate", lambda _repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))

    async def resume_due_work():
        consumer = asyncio.create_task(restarted._invalidation_loop("owner/repo"))
        worker = asyncio.create_task(restarted._worker_loop("owner/repo", 0))
        try:
            for _ in range(200):
                if processed and restarted.invalidations.pending_count("owner/repo") == 0:
                    break
                await asyncio.sleep(0.005)
            assert processed == [100]
            assert len(attempts) == 2
            assert restarted.invalidations.pending_count("owner/repo") == 0
        finally:
            consumer.cancel()
            worker.cancel()
            await asyncio.gather(consumer, worker, return_exceptions=True)

    asyncio.run(resume_due_work())


def test_failed_retry_transaction_stops_worker_without_success_observation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(TraceCollector, "_instance", None)
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    attempts = []

    def fail_refresh(*_args):
        attempts.append(100)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(github, "get_pull_request_metadata_strict", fail_refresh)
    engine.invalidations._connection.execute("CREATE TRIGGER reject_retry BEFORE UPDATE OF retry_not_before ON entity_invalidations WHEN NEW.retry_not_before IS NOT NULL BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END")

    async def scenario():
        await engine.invalidate_entity("owner/repo", "pr", 100)
        await asyncio.wait_for(engine._worker_loop("owner/repo", 0), timeout=2)

    asyncio.run(scenario())
    assert attempts == [100]
    assert engine.invalidations.pending_count("owner/repo") == 1
    assert engine.invalidations.claim("owner/repo") is None
    events = [event for event in get_trace_collector().get_snapshot(repository="owner/repo", item_type="pr", item_number=100).events if event.stage_id == "worker.failure-backoff" and event.kind == "stage-result"]
    assert len(events) == 1 and events[0].outcome == "failed"
    assert events[0].facts["reason"] == "retry_state_persistence_failed"
    assert evidence_rows(events)[0]["outcome"] == "failed"
    engine.invalidations.recover("owner/repo")
    assert engine.invalidations.claim("owner/repo") is not None


def test_diagnostic_failure_cannot_release_committed_retry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    attempts = []

    def fail_refresh(*_args):
        attempts.append(100)
        raise ValueError("malformed authority")

    def fail_diagnostic(*_args, **_kwargs):
        raise OSError("diagnostic unavailable")

    monkeypatch.setattr(github, "get_pull_request_metadata_strict", fail_refresh)
    monkeypatch.setattr(get_trace_collector(), "record_event", fail_diagnostic)

    async def scenario():
        await engine.invalidate_entity("owner/repo", "pr", 100)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            for _ in range(200):
                if engine.invalidations.get_deferred(EntityIdentity("owner/repo", "pr", 100)) is not None:
                    break
                await asyncio.sleep(0.005)
            assert engine.invalidations.claim("owner/repo") is None
            assert not worker.done()
            assert attempts == [100]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("newer_notification", [False, True])
@pytest.mark.parametrize("reject_transaction", [False, True])
def test_pending_review_retry_transaction_preserves_responsibility(tmp_path: Path, monkeypatch, newer_notification: bool, reject_transaction: bool) -> None:
    monkeypatch.setattr(TraceCollector, "_instance", None)
    database = tmp_path / "invalidations.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(database))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    identity = EntityIdentity("owner/repo", "pr", 100)
    deadline = time.time() + 90
    stabilization = deadline + 30
    attempts = []
    completions = []
    original_complete = engine.invalidations.complete_with_outcome

    def complete(claim):
        completions.append(claim)
        return original_complete(claim)

    def refresh(*_args):
        attempts.append(100)
        return {"number": 100, "title": "Pending review", "body": "", "state": "open", "user": {"login": "contributor", "id": 1}, "head": {"ref": "topic"}}

    def pending_review(*_args, **_kwargs):
        if newer_notification:
            engine.invalidations.invalidate(identity, delivery_id="during-pending-review", not_before=stabilization)
        return CandidateProcessingResult(type="pr", number=100, outcome=PRProcessingOutcome.DEFERRED, retry_not_before=deadline)

    monkeypatch.setattr(github, "get_pull_request_metadata_strict", refresh)
    monkeypatch.setattr(engine, "_is_pr_author_allowed", lambda _data: True)
    monkeypatch.setattr(engine, "_process_single_candidate", pending_review)
    monkeypatch.setattr(engine.invalidations, "complete_with_outcome", complete)
    if reject_transaction:
        engine.invalidations._connection.execute("CREATE TRIGGER reject_retry BEFORE UPDATE OF retry_not_before ON entity_invalidations WHEN NEW.retry_not_before IS NOT NULL BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END")

    async def scenario():
        await engine.invalidate_entity("owner/repo", "pr", 100)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            for _ in range(200):
                if worker.done() or engine.invalidations.get_deferred(identity) is not None:
                    break
                await asyncio.sleep(0.005)
            if reject_transaction:
                assert worker.done(), "failed retry persistence must stop the affected worker"
                await worker
            else:
                assert not worker.done()
                retained = engine.invalidations.get_deferred(identity)
                assert retained is not None
                assert retained.retry_not_before >= (stabilization if newer_notification else deadline)
                assert retained.generation == (2 if newer_notification else 1)
            assert attempts == [100]
            assert completions == [], "pending review must never acknowledge unfinished responsibility"
            assert engine.invalidations.pending_count("owner/repo") == 1
            assert engine.invalidations.claim("owner/repo") is None
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    events = [event for event in get_trace_collector().get_snapshot(repository="owner/repo", item_type="pr", item_number=100).events if event.stage_id == "worker.failure-backoff" and event.kind == "stage-result"]
    recovered = DurableInvalidationQueue(database)
    row = recovered._connection.execute("SELECT state, generation, claimed_generation FROM entity_invalidations WHERE repository='owner/repo' AND entity_type='pr' AND entity_number=100").fetchone()
    assert row is not None and row[1] == (2 if newer_notification else 1)
    if reject_transaction:
        assert tuple(row) == ("processing", 2 if newer_notification else 1, 1)
        assert len(events) == 1 and events[0].outcome == "failed"
        assert events[0].facts["reason"] == "retry_state_persistence_failed"
        assert evidence_rows(events)[0]["outcome"] == "failed"
        recovered.recover("owner/repo")
        monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: stabilization + 1)
        claim = recovered.claim("owner/repo")
        assert claim is not None and claim.generation == (2 if newer_notification else 1)
    else:
        assert row[0] == "dirty" and row[2] is None
        assert events == []
        recovered.recover("owner/repo")
        monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: deadline - 1)
        assert recovered.claim("owner/repo") is None
