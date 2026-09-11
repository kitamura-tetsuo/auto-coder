import asyncio
import io
import math
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from src.auto_coder.decomposition_validation_lifecycle import DecompositionValidationLifecycle
from src.auto_coder.entity_invalidation import CIWebhookDelivery, DurableInvalidationQueue, EntityIdentity, GitHubDeliveryMetadata
from src.auto_coder.github_pending_work import PendingWorkScheduler, PendingWorkStore
from src.auto_coder.github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from src.auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from src.auto_coder.specification_analyzer import SpecificationAnalysisResult
from src.auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from src.auto_coder.util.gh_cache import GitHubClient, OpenGitHubEntities, OpenGitHubIssue
from src.auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestOutcome,
    GitHubRequestRefused,
    GitHubResponseMetadata,
    RequestProvenance,
    configure_github_request_boundary,
)
from src.auto_coder.webhook_server import SentryWebhookPayload, create_app, process_github_payload, process_sentry_payload


def _github_deferral(reason: str, retry_at: float) -> GitHubRequestDeferred:
    context = GitHubRequestContext(
        operation_id="strict-refresh",
        attempt_id="attempt",
        subsystem="test",
        api_origin="https://api.github.com",
        method="GET",
        kind="read",
        endpoint_template="/repos/{owner}/{repo}/pulls/{number}",
        repository="owner/repo",
        item="pr#100",
        strict_read=True,
    )
    return GitHubRequestDeferred(context, reason, retry_at)


def _install_real_governor_cooldown(tmp_path: Path) -> tuple[GitHubRequestGovernor, float]:
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    admitted = _github_deferral("seed", 0).outcome.context
    assert governor.admit(admitted)
    retry_at = time.time() + 120.0
    governor.observe(
        GitHubRequestOutcome(
            admitted,
            429,
            GitHubApiOutcome.THROTTLED,
            RequestProvenance.NETWORK,
            DeliveryCertainty.HTTP_RESPONSE_RECEIVED,
            GitHubResponseMetadata(retry_after_seconds=120.0),
            1.0,
        )
    )
    configure_github_request_boundary(governor.admit_blocking, governor.observe)
    return governor, retry_at


def test_durable_refresh_deferral_uses_independent_latest_retry_guard(tmp_path: Path, monkeypatch):
    path = tmp_path / "invalidations.sqlite3"
    queue = DurableInvalidationQueue(path)
    identity = EntityIdentity("owner/repo", "issue", 42)
    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: 100.0)

    queue.invalidate(identity, not_before=130.0)
    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: 131.0)
    claim = queue.claim("owner/repo")
    assert claim is not None
    assert queue.begin_processing(claim)
    queue.invalidate(identity, not_before=120.0, urgent_admission=True)
    deferred = queue.defer(claim, "request_in_flight", math.nan, "https://api.github.com", now=131.0)
    assert deferred.retry_not_before == 132.0

    queue.invalidate(identity, not_before=120.0, urgent_admission=True)
    assert queue.claim("owner/repo") is None
    retained = queue.get_deferred(identity)
    assert retained is not None
    assert retained.reason == "request_in_flight"
    assert retained.api_origin == "https://api.github.com"
    assert retained.generation == 2

    restarted = DurableInvalidationQueue(path)
    restarted.recover("owner/repo")
    assert restarted.claim("owner/repo") is None
    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: 132.0)
    assert restarted.claim("owner/repo") is not None


@pytest.mark.parametrize("entity_type", ["pr", "issue"])
def test_worker_persists_real_strict_refresh_deferral_without_candidate_error(tmp_path: Path, monkeypatch, entity_type):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_process_single_candidate", lambda *args, **kwargs: processed.append(args) or CandidateProcessingResult(type="pr", number=100, success=True))
    _, retry_at = _install_real_governor_cooldown(tmp_path)
    output = io.StringIO()
    sink = loguru_logger.add(output, format="{level}|{message}")

    async def scenario():
        await engine.invalidate_entity("owner/repo", entity_type, 100)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        for _ in range(200):
            # Persistence happens in a thread before the worker resumes to
            # record its diagnostic. Wait for the worker's completion boundary
            # rather than cancelling between the database write and its log.
            if engine.invalidations.get_deferred(EntityIdentity("owner/repo", entity_type, 100)) and engine.active_workers.get(0) is None:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    try:
        asyncio.run(scenario())
    finally:
        configure_github_request_boundary()
        loguru_logger.remove(sink)
    retained = engine.invalidations.get_deferred(EntityIdentity("owner/repo", entity_type, 100))
    assert retained is not None
    assert retained.reason == "rate_limit_cooldown"
    assert retained.retry_not_before >= retry_at - 0.1
    assert processed == []
    logs = output.getvalue()
    assert "WARNING|Authoritative refresh safely deferred" in logs
    assert "Failed to create candidate" not in logs
    assert "ERROR|Worker 0 error processing candidate" not in logs


def test_malformed_strict_pr_metadata_remains_pending_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    response = httpx.Response(200, json={}, request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/100"))
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", lambda *args, **kwargs: response)
    processed = []
    monkeypatch.setattr(engine, "_process_single_candidate", lambda *args, **kwargs: processed.append(args))
    output = io.StringIO()
    sink = loguru_logger.add(output, format="{level}|{message}")

    async def scenario():
        await engine.invalidate_entity("owner/repo", "pr", 100)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        for _ in range(200):
            if "did not return PR metadata" in output.getvalue():
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    try:
        asyncio.run(scenario())
    finally:
        loguru_logger.remove(sink)
    assert processed == []
    assert engine.invalidations.pending_count("owner/repo") == 1
    assert "ERROR|Failed to create candidate for pr #100" in output.getvalue()
    assert "did not return PR metadata" in output.getvalue()


def test_invalidation_loop_automatically_retries_at_durable_deadline(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    attempts = []
    processed = []
    deferred = _github_deferral("request_in_flight", time.time())
    raw_pr = {"number": 100, "title": "Current", "body": "", "state": "open", "user": {"login": "contributor", "id": 1}, "head": {"ref": "topic"}}

    def strict_refresh(*args):
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            raise deferred
        return raw_pr

    monkeypatch.setattr(github, "get_pull_request_metadata_strict", strict_refresh)
    monkeypatch.setattr(engine, "_is_pr_author_allowed", lambda data: True)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda *args, **kwargs: processed.append(args[1].data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))

    async def scenario():
        consumer = asyncio.create_task(engine._invalidation_loop("owner/repo"))
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await engine.invalidate_entity("owner/repo", "pr", 100)
        for _ in range(200):
            if len(attempts) == 1:
                break
            await asyncio.sleep(0.01)
        assert len(attempts) == 1
        await asyncio.sleep(0.2)
        assert len(attempts) == 1
        for _ in range(250):
            if processed == [100]:
                break
            await asyncio.sleep(0.01)
        consumer.cancel()
        worker.cancel()
        await asyncio.gather(consumer, worker, return_exceptions=True)

    asyncio.run(scenario())
    assert processed == [100]
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= 0.9


def test_failed_deferral_transaction_stops_worker_and_preserves_recovery(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(GitHubClient("test-token"), AutomationConfig())
    deferred = _github_deferral("rate_limit_cooldown", time.time() + 120.0)
    monkeypatch.setattr(engine.github, "get_pull_request_metadata_strict", MagicMock(side_effect=deferred))
    engine.invalidations._connection.execute(
        """CREATE TRIGGER fail_deferral BEFORE UPDATE OF deferral_reason ON entity_invalidations
           WHEN NEW.deferral_reason IS NOT NULL BEGIN SELECT RAISE(ABORT, 'disk unavailable'); END"""
    )
    output = io.StringIO()
    sink = loguru_logger.add(output, format="{level}|{message}")

    async def scenario():
        await engine.invalidate_entity("owner/repo", "pr", 100)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await asyncio.wait_for(worker, timeout=2.0)

    try:
        asyncio.run(scenario())
    finally:
        loguru_logger.remove(sink)
    assert engine.github.get_pull_request_metadata_strict.call_count == 1
    assert engine.invalidations.pending_count("owner/repo") == 1
    assert engine.invalidations.claim("owner/repo") is None
    assert "Failed to persist authoritative-refresh deferral" in output.getvalue()
    assert "Authoritative refresh safely deferred" not in output.getvalue()
    engine.invalidations.recover("owner/repo")
    assert engine.invalidations.claim("owner/repo") is not None


def _candidate(repo_name, entity_type, number, propagate_errors=False):
    return Candidate(type=entity_type, data={"number": number, "state": "open"}, priority=0)


async def _run_worker_until(engine, expected_count, processed):
    worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
    for _ in range(200):
        if len(processed) == expected_count and engine.invalidations.pending_count("owner/repo") == 0:
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


def test_durable_queue_coalesces_queued_events_and_requeues_active_event(tmp_path: Path):
    identity = EntityIdentity("owner/repo", "pr", 100)
    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    for _ in range(5):
        assert queue.invalidate(identity)

    first = queue.claim("owner/repo")
    assert first is not None and first.generation == 1
    queue.invalidate(identity)
    assert queue.pending_count("owner/repo") == 1
    assert queue.begin_processing(first)
    queue.invalidate(identity)
    assert queue.complete(first)
    second = queue.claim("owner/repo")
    assert second is not None and second.generation == 2

    restarted = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    restarted.recover("owner/repo")
    recovered = restarted.claim("owner/repo")
    assert recovered == second


def test_duplicate_delivery_does_not_advance_generation(tmp_path: Path):
    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    identity = EntityIdentity("owner/repo", "issue", 42)
    assert queue.invalidate(identity, "delivery-1")
    assert not queue.invalidate(identity, "delivery-1")
    claim = queue.claim("owner/repo")
    assert claim is not None and claim.generation == 1


def test_new_issue_webhooks_preserve_creation_anchored_stabilization(tmp_path: Path, monkeypatch):
    """The HTTP-originated state reaches durable scheduling without snapshot dispatch."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    created = datetime.now(timezone.utc)

    async def receive_mutations():
        for index, action in enumerate(("opened", "edited", "labeled")):
            await process_github_payload(
                "issues",
                {
                    "action": action,
                    "issue": {"number": 200, "created_at": created.isoformat()},
                },
                engine,
                "owner/repo",
                f"issue-{index}",
            )

    asyncio.run(receive_mutations())
    assert engine.queue.qsize() == 0
    assert engine.invalidations.pending_count("owner/repo") == 2
    assert 0 < engine.invalidations.seconds_until_next_ready("owner/repo") <= 60

    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: (created + timedelta(seconds=60)).timestamp())
    asyncio.run(engine._enqueue_pending_invalidations("owner/repo"))
    queued = [engine.queue.get_nowait(), engine.queue.get_nowait()]
    assert [(candidate.type, candidate.data) for candidate in queued] == [
        ("dependency", {"number": 1}),
        ("issue", {"number": 200}),
    ]
    assert all(candidate.invalidation_generation == 1 for candidate in queued)


def test_mutation_deadlines_cannot_extend_existing_issue_window(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: 900.0)
    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    identity = EntityIdentity("owner/repo", "issue", 200)
    assert queue.invalidate(identity, "opened", not_before=1000.0)
    assert queue.invalidate(identity, "edited", not_before=1055.0)
    assert queue.seconds_until_next_ready("owner/repo") == 100.0


def test_steady_state_maintenance_does_not_enumerate_candidates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    engine = AutomationEngine(github, AutomationConfig())
    monkeypatch.setattr(engine, "_check_and_handle_closed_branch", lambda repo: True)
    monkeypatch.setattr(engine, "_get_candidates", MagicMock(side_effect=AssertionError("candidate polling is forbidden")))
    monkeypatch.setattr("src.auto_coder.automation_engine.check_for_updates_and_restart", lambda: None)
    monkeypatch.setattr("src.auto_coder.automation_engine.git_pull", lambda: MagicMock(success=True))

    waits = 0

    async def finish_after_intervals(seconds):
        nonlocal waits
        waits += 1
        if waits == 3:
            raise asyncio.CancelledError
        return False

    monkeypatch.setattr(engine, "_sleep_or_wake", finish_after_intervals)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(engine._producer_loop("owner/repo"))

    engine._get_candidates.assert_not_called()
    github.get_open_issues.assert_not_called()
    github.get_open_pull_requests.assert_not_called()


def test_one_delivery_can_invalidate_multiple_entities_with_preserved_metadata(tmp_path: Path):
    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    first = EntityIdentity("owner/repo", "pr", 41)
    second = EntityIdentity("owner/repo", "pr", 42)

    assert queue.invalidate(first, "delivery-1", "check_run", "completed")
    assert queue.invalidate(second, "delivery-1", "check_run", "completed")
    assert not queue.invalidate(first, "delivery-1", "check_run", "completed")

    assert queue.get_delivery_metadata("owner/repo", "delivery-1") == [
        GitHubDeliveryMetadata("delivery-1", first, "check_run", "completed"),
        GitHubDeliveryMetadata("delivery-1", second, "check_run", "completed"),
    ]


def test_http_redelivery_after_migration_recognizes_former_adapter_suffix(tmp_path: Path, monkeypatch):
    path = tmp_path / "invalidations.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE entity_invalidations (
            repository TEXT NOT NULL, entity_type TEXT NOT NULL, entity_number INTEGER NOT NULL,
            generation INTEGER NOT NULL, claimed_generation INTEGER, state TEXT NOT NULL,
            PRIMARY KEY(repository, entity_type, entity_number)
        );
        CREATE TABLE github_deliveries (
            repository TEXT NOT NULL, delivery_id TEXT NOT NULL,
            PRIMARY KEY(repository, delivery_id)
        );
        INSERT INTO github_deliveries VALUES ('owner/repo', 'same-delivery:0');
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(path))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json={"action": "opened", "pull_request": {"number": 77}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "same-delivery"},
        )

    assert response.status_code == 200
    assert engine.invalidations.pending_count("owner/repo") == 0
    assert engine.queue.qsize() == 0


@pytest.mark.parametrize("phase", ["refresh", "parent_validation", "dependency"])
def test_worker_status_owns_candidate_during_pre_dispatch(tmp_path: Path, monkeypatch, phase):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    entered = threading.Event()
    release = threading.Event()
    entity_type = "dependency" if phase == "dependency" else "issue"
    processed = []

    def pause():
        entered.set()
        assert release.wait(10), "Test did not release the pre-dispatch operation"

    def fetch(*args):
        if phase == "refresh":
            pause()
        return Candidate(type="issue", data={"number": 100, "title": "Fetched title", "state": "open"}, priority=0)

    def validate(*args):
        if phase == "parent_validation":
            pause()

    async def expand(*args):
        await asyncio.to_thread(pause)

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(engine, "_validate_submitted_parent_generation_for_child", validate)
    monkeypatch.setattr(engine, "_expand_dependency_obligation", expand)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda *args, **kwargs: processed.append(100) or CandidateProcessingResult(type="issue", number=100, success=True))

    async def scenario():
        await engine.invalidate_entity("owner/repo", entity_type, 100)
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            status = engine.get_status()
            assert status["active_workers"] == {0: {"type": entity_type, "number": 100, "title": "Fetched title" if phase == "parent_validation" else None}}
            assert status["queue_items"] == []
            release.set()
            await asyncio.wait_for(engine.queue.join(), 5)
            assert engine.get_status()["active_workers"] == {0: None}
            assert engine.invalidations.pending_count("owner/repo") == 0
        finally:
            release.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert processed == ([] if phase == "dependency" else [100])


def test_five_webhooks_before_worker_cause_one_fetch_and_decision(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    fetches = []
    processed = []

    def fetch(*args):
        fetches.append(args[2])
        return _candidate(*args)

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)

    async def scenario():
        payload = {"action": "opened", "pull_request": {"number": 100}}
        for index in range(5):
            await process_github_payload("pull_request", payload, engine, "owner/repo", f"delivery-{index}")
        assert engine.queue.qsize() == 1
        await _run_worker_until(engine, 1, processed)

    asyncio.run(scenario())
    assert fetches == [100]
    assert processed == [100]


def test_fetch_failure_remains_durable_and_restart_retries(tmp_path: Path, monkeypatch):
    path = tmp_path / "invalidations.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(path))
    github = GitHubClient("test-token")
    failing = AutomationEngine(github, AutomationConfig())
    transport = MagicMock(side_effect=RuntimeError("GitHub unavailable"))
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", transport)

    async def fail_once():
        await process_github_payload("pull_request", {"action": "opened", "pull_request": {"number": 100}}, failing, "owner/repo", "outage")
        worker = asyncio.create_task(failing._worker_loop("owner/repo", 0))
        for _ in range(200):
            if transport.call_count == 1:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    asyncio.run(fail_once())
    assert failing.invalidations.pending_count("owner/repo") == 1

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "number": 100,
        "title": "Recovered PR",
        "body": "",
        "state": "open",
        "user": {"login": "contributor", "id": 123},
        "labels": [],
        "assignees": [],
        "head": {"ref": "feature", "sha": "abc"},
        "base": {"ref": "main", "sha": "def"},
    }
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", MagicMock(return_value=response))
    restarted = AutomationEngine(GitHubClient("test-token"), AutomationConfig())
    processed = []
    monkeypatch.setattr(restarted, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)

    async def retry():
        restarted.invalidations.recover("owner/repo")
        await restarted._enqueue_pending_invalidations("owner/repo")
        await _run_worker_until(restarted, 1, processed)

    asyncio.run(retry())
    assert processed == [100]


def test_start_automation_recovers_before_steady_state(tmp_path: Path, monkeypatch):
    path = tmp_path / "invalidations.sqlite3"
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(path))
    first = AutomationEngine(MagicMock(), AutomationConfig())
    first.invalidations.invalidate(EntityIdentity("owner/repo", "pr", 100))
    interrupted = first.invalidations.claim("owner/repo")
    assert interrupted is not None and first.invalidations.begin_processing(interrupted)

    restarted = AutomationEngine(MagicMock(), AutomationConfig())
    processed = []
    monkeypatch.setattr(restarted, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(restarted, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
    monkeypatch.setattr(restarted, "_get_implementation_slots", lambda repo: MagicMock())
    monkeypatch.setattr(restarted, "_producer_loop", lambda repo: asyncio.Event().wait())
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)
    monkeypatch.setattr("src.auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("src.auto_coder.automation_engine.get_health_monitor", MagicMock())

    async def startup():
        task = asyncio.create_task(restarted.start_automation("owner/repo", concurrency=1))
        for _ in range(200):
            if processed == [100] and restarted.invalidations.pending_count("owner/repo") == 0:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(startup())
    assert processed == [100]


def test_startup_reconciliation_recovers_missed_issue_through_worker_path(tmp_path: Path, monkeypatch):
    """AS-001/AS-005: production startup turns live GitHub state into normal work."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(issues=[OpenGitHubIssue(1725)])
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="issue", number=1725, success=True),
    )
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())
    monkeypatch.setattr(engine, "_producer_loop", lambda repo: asyncio.Event().wait())
    monkeypatch.setattr("src.auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("src.auto_coder.automation_engine.get_health_monitor", MagicMock())

    async def startup():
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        for _ in range(200):
            if processed == [1725] and engine.invalidations.pending_count("owner/repo") == 0:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(startup())
    assert processed == [1725]
    assert engine.get_status()["startup_reconciliation"] == {"complete": True, "error": None}
    github.get_open_entities_strict.assert_called_once_with("owner/repo")


def _deferred_admission_error(retry_after=0):
    outcome = GitHubRequestOutcome(
        GitHubRequestContext("op", "attempt", "startup", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}/issues"),
        None,
        GitHubApiOutcome.REFUSED,
        RequestProvenance.NETWORK,
        DeliveryCertainty.DEFINITELY_NOT_SENT,
        GitHubResponseMetadata(retry_after_seconds=retry_after),
        1,
    )
    return GitHubRequestRefused(outcome)


def test_startup_admission_deferral_retries_durably_without_terminating_daemon(tmp_path: Path, monkeypatch):
    """Issue #1921 AS-001/REQ-001/REQ-002/REQ-006/REQ-007: a deferred startup scan
    retains recoverable work and retries through the pending-work scheduler instead
    of terminating the daemon; ordinary workers only start once it succeeds."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    store = PendingWorkStore(tmp_path / "pending.db")
    monkeypatch.setattr("src.auto_coder.automation_engine.get_pending_work_store", lambda: store)

    attempts = []
    allow_retry = threading.Event()

    def enumerate_entities(repo_name):
        attempts.append(1)
        if len(attempts) == 1:
            raise _deferred_admission_error()
        allow_retry.wait(timeout=5)
        return OpenGitHubEntities(pull_requests=[100])

    github = MagicMock()
    github.get_open_entities_strict.side_effect = enumerate_entities
    engine = AutomationEngine(github, AutomationConfig())
    engine.pending_work_scheduler = PendingWorkScheduler(store, poll_interval=0.02)
    processed = []
    monkeypatch.setattr(engine, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())
    monkeypatch.setattr(engine, "_producer_loop", lambda repo: asyncio.Event().wait())
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
    monkeypatch.setattr("src.auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("src.auto_coder.automation_engine.get_health_monitor", MagicMock())

    async def scenario():
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        for _ in range(200):
            if attempts:
                break
            await asyncio.sleep(0.01)
        assert attempts, "the first startup attempt never ran"

        # The daemon stays alive with visible, incomplete recovery rather than crashing.
        for _ in range(200):
            if len(attempts) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(attempts) >= 2, "the pending-work scheduler never retried the deferred scan"
        assert not task.done()
        assert engine.startup_reconciled is False
        pending = engine.get_status()["pending_work"]
        assert any(item["stage"] == "startup-reconciliation" and item["repository"] == "owner/repo" for item in pending)
        # Ordinary work never starts before recovery succeeds.
        assert processed == []

        allow_retry.set()
        for _ in range(300):
            if engine.startup_reconciled:
                break
            await asyncio.sleep(0.01)
        assert engine.startup_reconciled is True
        assert len(attempts) >= 2, "the pending-work scheduler never retried the deferred scan"

        for _ in range(200):
            if processed == [100]:
                break
            await asyncio.sleep(0.01)
        assert processed == [100]
        # A completed obligation must not linger for a later timer-driven repeat.
        assert engine.get_status()["pending_work"] == []

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_startup_reconciliation_failure_never_starts_steady_state(tmp_path: Path, monkeypatch):
    """AS-004/REQ-006: a failed one-shot scan is visible and fail-closed."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    github.get_open_entities_strict.side_effect = RuntimeError("second page unavailable")
    engine = AutomationEngine(github, AutomationConfig())
    producer = MagicMock()
    monkeypatch.setattr(engine, "_producer_loop", producer)
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())

    with pytest.raises(RuntimeError, match="second page unavailable"):
        asyncio.run(engine.start_automation("owner/repo", concurrency=1))

    producer.assert_not_called()
    assert engine.get_status()["startup_reconciliation"] == {
        "complete": False,
        "error": "RuntimeError: second page unavailable",
    }
    github.get_open_entities_strict.assert_called_once_with("owner/repo")


def test_webhook_during_startup_reconciliation_is_not_cleared(tmp_path: Path, monkeypatch):
    """AS-003: a newer invalidation survives an older recovery observation."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    engine.github.get_open_entities_strict.return_value = OpenGitHubEntities(pull_requests=[100])
    observed = []
    processing_started = asyncio.Event()
    allow_completion = asyncio.Event()
    event_loop = None

    def process(repo, candidate, **_kwargs):
        observed.append(candidate.data["number"])
        if len(observed) == 1:
            event_loop.call_soon_threadsafe(processing_started.set)
            asyncio.run_coroutine_threadsafe(allow_completion.wait(), event_loop).result()
        return CandidateProcessingResult(type="pr", number=100, success=True)

    monkeypatch.setattr(engine, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(engine, "_process_single_candidate", process)

    async def scenario():
        nonlocal event_loop
        event_loop = asyncio.get_running_loop()
        await engine._reconcile_open_github_entities("owner/repo")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await processing_started.wait()
        await process_github_payload("pull_request", {"action": "synchronize", "pull_request": {"number": 100}}, engine, "owner/repo", "newer")
        allow_completion.set()
        for _ in range(200):
            if len(observed) == 2 and engine.invalidations.pending_count("owner/repo") == 0:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert observed == [100, 100]


def test_strict_startup_enumeration_reads_all_pages_and_current_open_state(monkeypatch):
    """AS-002: REST collections, not missed event history, define recovery input."""
    responses = []
    for payload, next_url in (
        ([{"number": 1}, {"number": 90, "pull_request": {}}], "https://api.github.com/issues-page-2"),
        ([{"number": 2}], None),
        ([{"number": 100}], None),
    ):
        response = MagicMock()
        response.json.return_value = payload
        response.links = {"next": {"url": next_url}} if next_url else {}
        responses.append(response)
    get = MagicMock(side_effect=responses)
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", get)

    entities = GitHubClient("token").get_open_entities_strict("owner/repo")

    assert entities == OpenGitHubEntities(issues=[OpenGitHubIssue(1), OpenGitHubIssue(2)], pull_requests=[100])
    assert get.call_count == 3
    assert "issues-page-2" in get.call_args_list[1].args[0]
    assert "/pulls?state=open" in get.call_args_list[2].args[0]


def test_real_startup_scan_preserves_recent_issue_stabilization(tmp_path: Path, monkeypatch):
    """REQ-002/REQ-007: missed opening webhooks retain normal Issue delay."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    now = datetime.now(timezone.utc)
    issue = {
        "number": 1725,
        "title": "Recent work",
        "body": "Ready",
        "state": "open",
        "created_at": now.isoformat(),
        "user": {"login": "contributor", "id": 123},
        "labels": [{"name": "implementation-ready"}],
        "assignees": [],
        "comments": 0,
    }

    def response(payload, next_url=None):
        result = MagicMock()
        result.json.return_value = payload
        result.links = {"next": {"url": next_url}} if next_url else {}
        return result

    get = MagicMock(side_effect=[response([issue]), response([])] * 5)
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", get)
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.Client.get", MagicMock(return_value=response(issue)))
    github = GitHubClient("token")
    github.get_parent_issue_details_strict = MagicMock(return_value=None)
    github.get_direct_sub_issues_strict = MagicMock(return_value=[])
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())
    monkeypatch.setattr(engine, "_producer_loop", lambda repo: asyncio.Event().wait())
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="issue", number=1725, success=True),
    )
    monkeypatch.setattr("src.auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("src.auto_coder.automation_engine.get_health_monitor", MagicMock())

    async def startup():
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        for _ in range(200):
            if engine.startup_reconciled and engine._invalidation_wake_event is not None:
                break
            await asyncio.sleep(0.01)
        assert processed == []
        assert engine.queue.qsize() == 0
        remaining = engine.invalidations.seconds_until_next_ready("owner/repo")
        assert remaining is not None and 55 < remaining <= 60

        github.get_open_entities_strict = MagicMock(
            return_value=OpenGitHubEntities(
                issues=[OpenGitHubIssue(number=1725, created_at=issue["created_at"])],
                pull_requests=[],
            )
        )
        monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: (now + timedelta(seconds=60)).timestamp())
        monkeypatch.setattr("src.auto_coder.automation_engine.time.time", lambda: (now + timedelta(seconds=60)).timestamp())
        assert engine._invalidation_wake_event is not None
        engine._invalidation_wake_event.set()
        for _ in range(200):
            if processed == [1725]:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(startup())
    assert processed == [1725]
    assert get.call_count == 2


def test_real_paginated_scan_failure_blocks_startup(tmp_path: Path, monkeypatch):
    """REQ-005: an HTTP failure after page one cannot become partial success."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    first = httpx.Response(
        200,
        json=[{"number": 1, "created_at": "2026-09-05T00:00:00Z"}],
        headers={"Link": '<https://api.github.com/repos/owner/repo/issues?state=open&per_page=100&page=2>; rel="next"'},
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues?state=open&per_page=100"),
    )
    failure = httpx.Response(
        503,
        request=httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues?state=open&per_page=100&page=2"),
    )
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", MagicMock(side_effect=[first, failure]))
    engine = AutomationEngine(GitHubClient("token"), AutomationConfig())
    producer = MagicMock()
    monkeypatch.setattr(engine, "_producer_loop", producer)
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(engine.start_automation("owner/repo", concurrency=1))

    producer.assert_not_called()
    assert engine.startup_reconciled is False
    assert engine.startup_reconciliation_error is not None
    assert "HTTPStatusError" in engine.startup_reconciliation_error


def test_http_duplicate_delivery_causes_one_execution(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)

    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")
    headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "same-delivery"}
    with TestClient(app) as client:
        payload = {"action": "opened", "pull_request": {"number": 100}, "repository": {"full_name": "owner/repo"}}
        assert client.post("/hooks/github", json=payload, headers=headers).status_code == 200
        assert client.post("/hooks/github", json=payload, headers=headers).status_code == 200

    asyncio.run(_run_worker_until(engine, 1, processed))
    assert processed == [100]


def test_out_of_order_http_webhooks_reconcile_one_authoritative_pr_state(tmp_path: Path, monkeypatch):
    """AS-002: live closed state survives normalization and blocks dispatch."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig(pr_allowlist=[123]))
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/100")
    response = httpx.Response(
        200,
        request=request,
        json={
            "number": 100,
            "title": "Current GitHub title",
            "body": "",
            "state": "closed",
            "user": {"login": "allowed-contributor", "id": 123},
            "labels": [],
            "assignees": [],
            "head": {"ref": "feature", "sha": "abc"},
            "base": {"ref": "main", "sha": "def"},
        },
    )
    get = MagicMock(return_value=response)
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", get)
    processed = []
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data.copy()) or CandidateProcessingResult(type="pr", number=100, success=True),
    )

    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")
    with TestClient(app) as client:
        for delivery, action, stale_title in (
            ("newer-delivery", "edited", "Newer payload snapshot"),
            ("older-delivery", "opened", "Older payload snapshot"),
        ):
            response = client.post(
                "/hooks/github",
                json={
                    "action": action,
                    "pull_request": {
                        "number": 100,
                        "state": "open",
                        "title": stale_title,
                        "user": {"login": "allowed-contributor", "id": 123},
                    },
                    "repository": {"full_name": "owner/repo"},
                },
                headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": delivery},
            )
            assert response.status_code == 200

    asyncio.run(_run_worker_until(engine, 0, processed))

    assert get.call_count == 1
    assert get.call_args.args[0] == "https://api.github.com/repos/owner/repo/pulls/100"
    assert processed == []
    assert engine.invalidations.pending_count("owner/repo") == 0


def test_webhook_during_active_processing_forces_later_reevaluation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    states = iter(["first", "later"])
    processing_started = asyncio.Event()
    allow_completion = asyncio.Event()
    observed = []
    event_loop = None

    def fetch(repo_name, entity_type, number, propagate_errors=False):
        return Candidate(type=entity_type, data={"number": number, "state": next(states)}, priority=0)

    def process(repo_name, candidate, **_kwargs):
        observed.append(candidate.data["state"])
        if candidate.data["state"] == "first":
            event_loop.call_soon_threadsafe(processing_started.set)
            asyncio.run_coroutine_threadsafe(allow_completion.wait(), event_loop).result()
        return CandidateProcessingResult(type="pr", number=100, success=True)

    async def scenario():
        nonlocal event_loop
        event_loop = asyncio.get_running_loop()
        await process_github_payload("pull_request", {"action": "opened", "pull_request": {"number": 100}}, engine, "owner/repo", "first")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await processing_started.wait()
        await process_github_payload("pull_request", {"action": "synchronize", "pull_request": {"number": 100}}, engine, "owner/repo", "later")
        allow_completion.set()
        for _ in range(200):
            if observed == ["first", "later"] and engine.invalidations.pending_count("owner/repo") == 0:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(engine, "_process_single_candidate", process)
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)
    asyncio.run(scenario())
    assert observed == ["first", "later"]


def test_authoritative_pr_not_found_completes_invalidation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    engine = AutomationEngine(github, AutomationConfig())
    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/pulls/100")
    not_found = httpx.Response(404, request=request)
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", lambda *args, **kwargs: not_found)
    processed = []
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))

    async def scenario():
        await process_github_payload("pull_request", {"action": "opened", "pull_request": {"number": 100}}, engine, "owner/repo", "not-found")
        await _run_worker_until(engine, 0, processed)

    asyncio.run(scenario())
    assert processed == []
    assert engine.invalidations.pending_count("owner/repo") == 0


def test_issue_invalidation_uses_single_strict_snapshot_for_decision(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = GitHubClient("test-token")
    strict_snapshot = {
        "number": 42,
        "title": "Authoritative issue",
        "body": "Current body",
        "state": "open",
        "user": {"login": "contributor", "id": 123},
        "labels": [],
        "assignees": [],
        "comments": 0,
    }
    github.get_issue_dispatch_snapshot_strict = MagicMock(return_value=strict_snapshot)
    github.get_parent_issue_details_strict = MagicMock(return_value=None)
    github.get_direct_sub_issues_strict = MagicMock(return_value=[])
    github.get_issue = MagicMock(side_effect=RuntimeError("second request unavailable"))
    github.get_open_entities_strict = MagicMock(return_value=OpenGitHubEntities(issues=[], pull_requests=[]))
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate, **_kwargs: processed.append(candidate.data) or CandidateProcessingResult(type="issue", number=42, success=True))
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)

    async def scenario():
        await process_github_payload("issues", {"action": "opened", "issue": {"number": 42, "title": "stale"}}, engine, "owner/repo", "issue-delivery")
        await _run_worker_until(engine, 1, processed)

    asyncio.run(scenario())
    assert github.get_issue_dispatch_snapshot_strict.call_count >= 2
    assert all(call.args == ("owner/repo", 42) for call in github.get_issue_dispatch_snapshot_strict.call_args_list)
    github.get_issue.assert_not_called()
    assert processed[0]["body"] == "Current body"
    assert engine.invalidations.pending_count("owner/repo") == 0


def test_dependency_close_http_delivery_discovers_dependent_through_worker(tmp_path: Path, monkeypatch):
    """A native close endpoint reaches normal processing without a dependent event."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(
        issues=[OpenGitHubIssue(number=205, created_at="2020-01-01T00:00:00Z")],
        pull_requests=[],
    )
    snapshots = {
        101: {"number": 101, "state": "closed", "labels": []},
        205: {"number": 205, "state": "open", "labels": [{"name": "implementation-ready"}]},
    }
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue_details.side_effect = lambda issue: dict(issue)
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda _repo, candidate, **_kwargs: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="issue", number=candidate.data["number"], success=True),
    )

    payload = {
        "action": "blocked_by_removed",
        "blocked_issue": {"number": 205, "repository_url": "https://api.github.com/repos/owner/repo"},
        "blocking_issue": {"number": 101, "repository_url": "https://api.github.com/repos/owner/repo"},
        "repository": {"full_name": "owner/repo"},
    }

    async def scenario():
        with patch("src.auto_coder.webhook_server.init_dashboard"):
            app = create_app(engine, "owner/repo")
        with TestClient(app) as client:
            assert (
                client.post(
                    "/hooks/github",
                    json=payload,
                    headers={"X-GitHub-Event": "issue_dependencies", "X-GitHub-Delivery": "close-edge"},
                ).status_code
                == 200
            )
        await _run_worker_until(engine, 1, processed)

    asyncio.run(scenario())
    assert processed == [205]
    assert engine.invalidations.pending_count("owner/repo") == 0


@pytest.mark.parametrize("child_state", ["open", "closed"])
def test_child_edit_webhook_validates_submitted_generation_before_eligibility_filter(tmp_path: Path, monkeypatch, child_state: str):
    """A child-only webhook reaches set validation despite ownership or closure."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    body = "## Requirements\n- REQ-001: Preserve the edited behavior."
    parent = {"id": 100, "number": 10, "title": "Parent", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}], "user": {"id": 1}}
    child = {
        "id": 110,
        "number": 11,
        "title": "Edited child",
        "body": body + "\nEdited.",
        "state": child_state,
        "labels": [],
        "user": {"id": 1},
        "parent_issue_url": "https://api.github.com/repos/owner/repo/issues/10",
    }
    github = MagicMock()
    snapshots = {10: parent, 11: child}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue_details.side_effect = lambda value: dict(value)
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_parent_issue_details_strict.side_effect = lambda _repo, number: dict(parent) if number == 11 else None
    events = []
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", lambda *_args: events.append("set") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "children.json", lambda manifest, _body: events.append(f"child:{manifest.issue_number}") or SpecificationAnalysisResult("READY"))
    owner = ImplementationOwner("issue", 11)
    if child_state == "open":
        execution_id = slots.start_execution(owner)
        assert execution_id is not None
        assert slots.record_provider_session(owner, "retained-session")
        slots.finish_execution(owner, execution_id)

    async def scenario() -> None:
        await process_github_payload("issues", {"action": "edited", "issue": {"number": 11}}, engine, "owner/repo", f"edited-{child_state}")
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        for _ in range(300):
            if engine.invalidations.pending_count("owner/repo") == 0:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        asyncio.run(scenario())

    assert set(events) == {"set", "child:11"}
    dispatch.assert_not_called()
    assert slots.active_execution_ids(owner) == ()
    assert slots.has_provider_sessions(owner) is (child_state == "open")
    assert engine.invalidations.pending_count("owner/repo") == 0


def test_child_edit_validation_error_retains_invalidation_for_identity_retry(tmp_path: Path, monkeypatch):
    """A child ERROR is not evidence and cannot acknowledge its durable trigger."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    body = "## Requirements\n- REQ-001: Preserve the edited behavior."
    parent = {"id": 100, "number": 10, "title": "Parent", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}], "user": {"id": 1}}
    child = {
        "id": 110,
        "number": 11,
        "title": "Edited child",
        "body": body + "\nEdited.",
        "state": "closed",
        "labels": [],
        "user": {"id": 1},
        "parent_issue_url": "https://api.github.com/repos/owner/repo/issues/10",
    }
    github = MagicMock()
    snapshots = {10: parent, 11: child}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue_details.side_effect = lambda value: dict(value)
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_parent_issue_details_strict.side_effect = lambda _repo, number: dict(parent) if number == 11 else None
    set_calls = []
    child_verdicts = iter(("ERROR", "READY"))
    child_calls = []
    engine = AutomationEngine(github, AutomationConfig())
    decomposition = DecompositionValidationLifecycle(
        "owner/repo",
        "provider/model",
        tmp_path / "sets.json",
        lambda *_args: set_calls.append("set") or DecompositionAnalysisResult("READY"),
    )

    def analyze_child(manifest, _body):
        child_calls.append(manifest.issue_number)
        return SpecificationAnalysisResult(next(child_verdicts))

    individual = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "children.json", analyze_child)
    engine._decomposition_validators["owner/repo"] = decomposition
    engine._specification_validators["owner/repo"] = individual

    async def run_attempt(expect_pending: bool) -> None:
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await asyncio.wait_for(engine.queue.join(), timeout=3)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert engine.invalidations.pending_count("owner/repo") == int(expect_pending)

    async def scenario() -> None:
        await process_github_payload("issues", {"action": "edited", "issue": {"number": 11}}, engine, "owner/repo", "edited-error")
        await run_attempt(expect_pending=True)

        relationship = engine._child_review_context(parent, [child], 11)
        child_identity = individual.identity(11, child["title"], child["body"], relationship)
        assert individual.store.get(child_identity) is None
        assert set_calls == ["set"]
        assert child_calls == [11]
        assert parent["labels"] == [{"name": "implementation-ready"}]
        github.remove_labels.assert_not_called()
        github.add_comment_to_issue.assert_not_called()

        await engine._enqueue_pending_invalidations("owner/repo")
        await run_attempt(expect_pending=False)

    asyncio.run(scenario())

    assert set_calls == ["set"]
    assert child_calls == [11, 11]
    relationship = engine._child_review_context(parent, [child], 11)
    child_identity = individual.identity(11, child["title"], child["body"], relationship)
    assert individual.store.get(child_identity) is not None
    assert individual.store.get(child_identity).verdict == "READY"
    assert parent["labels"] == [{"name": "implementation-ready"}]


@pytest.mark.parametrize("child_state", ["open", "closed"])
def test_explicit_child_processing_validates_submitted_generation_before_eligibility_filter(tmp_path: Path, child_state: str):
    """The operator entry point validates a child's set before implementation filters."""
    body = "## Requirements\n- REQ-001: Preserve the edited behavior."
    parent = {"id": 100, "number": 10, "title": "Parent", "body": body, "state": "open", "labels": [{"name": "implementation-ready"}], "user": {"id": 1}}
    child = {
        "id": 110,
        "number": 11,
        "title": "Edited child",
        "body": body + "\nEdited.",
        "state": child_state,
        "labels": [],
        "user": {"id": 1},
        "parent_issue_url": "https://api.github.com/repos/owner/repo/issues/10",
    }
    github = MagicMock()
    github.get_open_entities_strict.return_value = OpenGitHubEntities(
        issues=[OpenGitHubIssue(number=10)] + ([OpenGitHubIssue(number=11)] if child_state == "open" else []),
        pull_requests=[],
    )
    snapshots = {10: parent, 11: child}
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, number: dict(snapshots[number])
    github.get_issue.return_value = child
    github.get_issue_details.side_effect = lambda value: dict(value)
    github.get_direct_sub_issues_strict.side_effect = lambda _repo, number: [dict(child)] if number == 10 else []
    github.get_parent_issue_details_strict.side_effect = lambda _repo, number: dict(parent) if number == 11 else None
    events = []
    engine = AutomationEngine(github, AutomationConfig())
    engine._check_and_handle_closed_branch = MagicMock(return_value=True)
    engine._get_authoritative_item_type = MagicMock(return_value="issue")
    slots = ImplementationSlotRepository("owner/repo", 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._decomposition_validators["owner/repo"] = DecompositionValidationLifecycle("owner/repo", "provider/model", tmp_path / "sets.json", lambda *_args: events.append("set") or DecompositionAnalysisResult("READY"))
    engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "provider/model", tmp_path / "children.json", lambda manifest, _body: events.append(f"child:{manifest.issue_number}") or SpecificationAnalysisResult("READY"))
    owner = ImplementationOwner("issue", 11)
    if child_state == "open":
        execution_id = slots.start_execution(owner)
        assert execution_id is not None
        assert slots.record_provider_session(owner, "retained-session")
        slots.finish_execution(owner, execution_id)

    with patch.object(engine, "_process_single_candidate_reserved") as dispatch:
        result = engine.process_single("owner/repo", "issue", 11, explicit_only=True)

    assert set(events) == {"set", "child:11"}
    dispatch.assert_not_called()
    assert result["errors"] == []
    assert slots.active_execution_ids(owner) == ()
    assert slots.has_provider_sessions(owner) is (child_state == "open")


@pytest.mark.parametrize(
    ("entity_type", "event_type", "entity_key"),
    [("pr", "pull_request", "pull_request"), ("issue", "issues", "issue")],
)
def test_reopened_invalidation_does_not_consult_cached_closed_state(tmp_path: Path, monkeypatch, entity_type, event_type, entity_key):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    github = MagicMock()
    github.get_pull_request.return_value = {"number": 100, "state": "closed"}
    github.get_issue.return_value = {"number": 100, "state": "closed"}
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(
        engine,
        "_create_candidate_from_single",
        lambda *args: Candidate(type=entity_type, data={"number": 100, "state": "open"}, priority=0),
    )
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data["state"]) or CandidateProcessingResult(type=entity_type, number=100, success=True),
    )

    async def scenario():
        await process_github_payload(event_type, {"action": "reopened", entity_key: {"number": 100}}, engine, "owner/repo", "reopened")
        await _run_worker_until(engine, 1, processed)

    asyncio.run(scenario())
    assert processed == ["open"]
    github.get_pull_request.assert_not_called()
    github.get_issue.assert_not_called()


def test_delayed_webhook_expires_through_consumer_and_fetches_final_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    final_state = {"number": 200, "state": "open", "labels": ["implementation-ready"]}
    fetched = []
    processed = []

    def fetch(*args):
        fetched.append(args[2])
        return Candidate(type="issue", data=final_state, priority=0)

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data.copy()) or CandidateProcessingResult(type="issue", number=200, success=True),
    )

    async def scenario():
        consumer = asyncio.create_task(engine._invalidation_loop("owner/repo"))
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        created_at = (datetime.now(timezone.utc) - timedelta(seconds=59.9)).isoformat()
        await process_github_payload(
            "issues",
            {"action": "opened", "issue": {"number": 200, "created_at": created_at, "labels": []}},
            engine,
            "owner/repo",
            "opened",
        )
        for _ in range(100):
            if processed:
                break
            await asyncio.sleep(0.01)
        consumer.cancel()
        worker.cancel()
        await asyncio.gather(consumer, worker, return_exceptions=True)

    asyncio.run(scenario())
    assert fetched == [200]
    assert processed == [final_state]


def test_urgent_label_transition_preserves_retryable_admission_obligation(tmp_path: Path, monkeypatch):
    """The webhook origin must survive the durable queue and a capacity defer."""
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    fetched = []
    processed = []

    def fetch(_repo, _entity_type, number, bypass_cache):
        fetched.append((number, bypass_cache))
        return Candidate(
            type="issue",
            data={"number": number, "state": "open", "labels": ["implementation-ready", "urgent"]},
            priority=0,
        )

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda _repo, candidate, **_kwargs: processed.append(candidate.urgent_admission)
        or CandidateProcessingResult(
            type="issue",
            number=1767,
            actions=["Deferred - logical implementation limit is occupied"],
            capacity_deferred=True,
        ),
    )

    async def scenario():
        await process_github_payload(
            "issues",
            {
                "action": "labeled",
                "issue": {"number": 1767},
                "label": {"name": "urgent"},
            },
            engine,
            "owner/repo",
            "urgent-delivery",
        )
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        for _ in range(100):
            if processed:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())

    assert fetched == [(1767, True)]
    assert processed == [True]
    assert engine.invalidations.pending_count("owner/repo") == 1


def test_sentry_created_issue_waits_then_fetches_current_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    created_at = (datetime.now(timezone.utc) - timedelta(seconds=59.5)).isoformat()
    github = MagicMock()
    github.create_issue.return_value = object()
    github.get_issue_details.return_value = {"number": 200, "created_at": created_at, "state": "open"}
    engine = AutomationEngine(github, AutomationConfig())
    final_state = {"number": 200, "state": "open", "labels": ["implementation-ready"]}
    processed = []
    monkeypatch.setattr(engine, "_create_candidate_from_single", lambda *args: Candidate(type="issue", data=final_state, priority=0))
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate, **_kwargs: processed.append(candidate.data.copy()) or CandidateProcessingResult(type="issue", number=200, success=True),
    )

    async def scenario():
        consumer = asyncio.create_task(engine._invalidation_loop("owner/repo"))
        worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
        await process_sentry_payload(SentryWebhookPayload(message="failure"), engine, "owner/repo")
        await asyncio.sleep(0.05)
        assert processed == []
        for _ in range(100):
            if processed:
                break
            await asyncio.sleep(0.01)
        consumer.cancel()
        worker.cancel()
        await asyncio.gather(consumer, worker, return_exceptions=True)

    asyncio.run(scenario())
    assert processed == [final_state]


def test_ci_watch_reconciliation_is_targeted_and_restart_durable(tmp_path: Path):
    path = tmp_path / "invalidations.sqlite3"
    queue = DurableInvalidationQueue(path)
    assert queue.ensure_ci_watch("owner/repo", 42, "head", "ci.yml", now=100)
    assert queue.promote_due_ci_watches("owner/repo", now=99) == 0
    assert queue.promote_due_ci_watches("owner/repo", now=100) == 1
    assert queue.promote_due_ci_watches("owner/repo", now=399) == 0
    reopened = DurableInvalidationQueue(path)
    assert reopened.promote_due_ci_watches("owner/repo", now=400) == 1


def test_ci_webhook_advances_watch_without_consuming_periodic_deadline(tmp_path: Path):
    queue = DurableInvalidationQueue(tmp_path / "invalidations.sqlite3")
    queue.ensure_ci_watch("owner/repo", 42, "head", "ci.yml", now=100)
    assert queue.promote_due_ci_watches("owner/repo", now=100) == 1
    delivery = CIWebhookDelivery("owner/repo", "delivery", "workflow_run", "completed", (42,), "head", "ci.yml", "9", 2)
    assert queue.accept_ci_delivery(delivery, now=110)
    assert queue.promote_due_ci_watches("owner/repo", now=111.9) == 0
    assert queue.promote_due_ci_watches("owner/repo", now=112) == 1
