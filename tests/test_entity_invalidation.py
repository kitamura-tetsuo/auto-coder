import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity, GitHubDeliveryMetadata
from src.auto_coder.util.gh_cache import GitHubClient
from src.auto_coder.webhook_server import create_app, process_github_payload


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


def test_five_webhooks_before_worker_cause_one_fetch_and_decision(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    fetches = []
    processed = []

    def fetch(*args):
        fetches.append(args[2])
        return _candidate(*args)

    monkeypatch.setattr(engine, "_create_candidate_from_single", fetch)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
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
    monkeypatch.setattr(restarted, "_process_single_candidate", lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
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
    monkeypatch.setattr(restarted, "_process_single_candidate", lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
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


def test_http_duplicate_delivery_causes_one_execution(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(tmp_path / "invalidations.sqlite3"))
    engine = AutomationEngine(MagicMock(), AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_create_candidate_from_single", _candidate)
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))
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

    def process(repo_name, candidate):
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
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="pr", number=100, success=True))

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
    github.get_issue = MagicMock(side_effect=RuntimeError("second request unavailable"))
    engine = AutomationEngine(github, AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_process_single_candidate", lambda repo, candidate: processed.append(candidate.data) or CandidateProcessingResult(type="issue", number=42, success=True))
    monkeypatch.setattr("src.auto_coder.automation_engine.is_item_closed_on_github", lambda *args: False)

    async def scenario():
        await process_github_payload("issues", {"action": "opened", "issue": {"number": 42, "title": "stale"}}, engine, "owner/repo", "issue-delivery")
        await _run_worker_until(engine, 1, processed)

    asyncio.run(scenario())
    github.get_issue_dispatch_snapshot_strict.assert_called_once_with("owner/repo", 42)
    github.get_issue.assert_not_called()
    assert processed[0]["body"] == "Current body"
    assert engine.invalidations.pending_count("owner/repo") == 0
