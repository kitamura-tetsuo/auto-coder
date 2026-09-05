import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.auto_coder.automation_config import AutomationConfig, Candidate, CandidateProcessingResult
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.entity_invalidation import DurableInvalidationQueue, EntityIdentity, GitHubDeliveryMetadata
from src.auto_coder.util.gh_cache import GitHubClient, OpenGitHubEntities, OpenGitHubIssue
from src.auto_coder.webhook_server import SentryWebhookPayload, create_app, process_github_payload, process_sentry_payload


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
    assert engine.invalidations.pending_count("owner/repo") == 1
    assert 0 < engine.invalidations.seconds_until_next_ready("owner/repo") <= 60

    monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: (created + timedelta(seconds=60)).timestamp())
    asyncio.run(engine._enqueue_pending_invalidations("owner/repo"))
    queued = engine.queue.get_nowait()
    assert queued.data == {"number": 200}
    assert queued.invalidation_generation == 1


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
        lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="issue", number=1725, success=True),
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

    def process(repo, candidate):
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

    get = MagicMock(side_effect=[response([issue]), response([])])
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", get)
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.Client.get", MagicMock(return_value=response(issue)))
    engine = AutomationEngine(GitHubClient("token"), AutomationConfig())
    processed = []
    monkeypatch.setattr(engine, "_get_implementation_slots", lambda repo: MagicMock())
    monkeypatch.setattr(engine, "_producer_loop", lambda repo: asyncio.Event().wait())
    monkeypatch.setattr(
        engine,
        "_process_single_candidate",
        lambda repo, candidate: processed.append(candidate.data["number"]) or CandidateProcessingResult(type="issue", number=1725, success=True),
    )
    monkeypatch.setattr("src.auto_coder.automation_engine.install_asyncio_diagnostics", lambda loop: None)
    monkeypatch.setattr("src.auto_coder.automation_engine.get_health_monitor", MagicMock())

    async def startup():
        task = asyncio.create_task(engine.start_automation("owner/repo", concurrency=1))
        for _ in range(200):
            if engine.startup_reconciled:
                break
            await asyncio.sleep(0.01)
        assert processed == []
        assert engine.queue.qsize() == 0
        remaining = engine.invalidations.seconds_until_next_ready("owner/repo")
        assert remaining is not None and 55 < remaining <= 60

        monkeypatch.setattr("src.auto_coder.entity_invalidation.time.time", lambda: (now + timedelta(seconds=60)).timestamp())
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
        lambda repo, candidate: processed.append(candidate.data.copy()) or CandidateProcessingResult(type="pr", number=100, success=True),
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
        lambda repo, candidate: processed.append(candidate.data["state"]) or CandidateProcessingResult(type=entity_type, number=100, success=True),
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
        lambda repo, candidate: processed.append(candidate.data.copy()) or CandidateProcessingResult(type="issue", number=200, success=True),
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
        lambda repo, candidate: processed.append(candidate.data.copy()) or CandidateProcessingResult(type="issue", number=200, success=True),
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
