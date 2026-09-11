import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.entity_invalidation import CIWebhookDelivery, DurableInvalidationQueue, EntityIdentity
from src.auto_coder.webhook_server import create_app


class IntakeEngine:
    def __init__(self, path: Path) -> None:
        self.invalidations = DurableInvalidationQueue(path)
        self.github = MagicMock()
        self._invalidation_wake_event = None


def test_real_http_ci_intake_is_durable_and_emits_no_lookup(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    engine = IntakeEngine(path)
    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json={"action": "completed", "workflow_run": {"head_sha": "abc", "id": 8, "workflow_id": 4, "run_attempt": 2}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": "uuid-1"},
        )
    assert response.status_code == 200
    engine.github.get_pull_request_numbers_for_commit.assert_not_called()

    reopened = DurableInvalidationQueue(path)
    assert reopened.claim_ci_correlation("owner/repo") is None
    # The deferred lookup remains represented and becomes due without a webhook.
    assert reopened.seconds_until_next_ci("owner/repo") is not None


def test_ci_delivery_deduplication_and_multi_target_fanout_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    delivery = CIWebhookDelivery("owner/repo", "uuid-2", "check_suite", "completed", (11, 12), "abc")
    queue = DurableInvalidationQueue(path)
    assert queue.accept_ci_delivery(delivery, now=time.time() - 3)
    assert not queue.accept_ci_delivery(delivery, now=time.time())
    assert queue.promote_due_ci("owner/repo") == 2
    assert queue.claim("owner/repo").identity == EntityIdentity("owner/repo", "pr", 11)

    reopened = DurableInvalidationQueue(path)
    reopened.recover("owner/repo")
    identities = []
    while claim := reopened.claim("owner/repo"):
        identities.append(claim.identity)
    assert identities == [EntityIdentity("owner/repo", "pr", 11), EntityIdentity("owner/repo", "pr", 12)]
    assert not reopened.accept_ci_delivery(delivery)


def test_supported_ci_delivery_requires_uuid_and_target(tmp_path: Path) -> None:
    engine = IntakeEngine(tmp_path / "events.sqlite3")
    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")
    with TestClient(app) as client:
        missing_uuid = client.post(
            "/hooks/github",
            json={"action": "completed", "check_run": {"head_sha": "abc"}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "check_run"},
        )
        missing_target = client.post(
            "/hooks/github",
            json={"action": "completed", "check_run": {}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "check_run", "X-GitHub-Delivery": "uuid-3"},
        )
    assert missing_uuid.status_code == 422
    assert missing_target.status_code == 422
    engine.github.get_pull_request_numbers_for_commit.assert_not_called()


def test_superseded_correlation_preserves_delivery_and_retry_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite3"
    queue = DurableInvalidationQueue(path)
    receiver = DurableInvalidationQueue(path)
    first = CIWebhookDelivery("owner/repo", "first", "check_run", "created", (), "abc")
    later = CIWebhookDelivery("owner/repo", "later", "check_run", "completed", (), "abc")
    assert queue.accept_ci_delivery(first, now=100)
    with patch("src.auto_coder.entity_invalidation.time.time", return_value=103):
        assert queue.claim_ci_correlation("owner/repo") == "abc"
        assert receiver.accept_ci_delivery(later, now=103)
        assert queue.finish_ci_correlation("owner/repo", "abc", [11]) is False
        assert queue.promote_due_ci("owner/repo") == 0
        assert queue.seconds_until_next_ci("owner/repo") == 2

    reopened = DurableInvalidationQueue(path)
    reopened.recover("owner/repo")
    with patch("src.auto_coder.entity_invalidation.time.time", return_value=104):
        assert reopened.claim_ci_correlation("owner/repo") is None
    with patch("src.auto_coder.entity_invalidation.time.time", return_value=105):
        assert reopened.claim_ci_correlation("owner/repo") == "abc"
        assert reopened.finish_ci_correlation("owner/repo", "abc", [12]) is True
        assert reopened.promote_due_ci("owner/repo") == 1
        assert reopened.claim("owner/repo").identity == EntityIdentity("owner/repo", "pr", 12)
        assert reopened.claim("owner/repo") is None
        assert reopened.seconds_until_next_ci("owner/repo") is None
        assert not reopened.accept_ci_delivery(first)
        assert not reopened.accept_ci_delivery(later)


def test_ci_drain_survives_delivery_during_lookup_and_promotes_other_prs(tmp_path: Path) -> None:
    engine = IntakeEngine(tmp_path / "events.sqlite3")
    receiver = DurableInvalidationQueue(tmp_path / "events.sqlite3")
    assert engine.invalidations.accept_ci_delivery(CIWebhookDelivery("owner/repo", "initial", "check_run", "created", (), "abc"), now=100)
    assert engine.invalidations.accept_ci_delivery(CIWebhookDelivery("owner/repo", "known-pr", "check_run", "completed", (2011,), "def"), now=100)

    def lookup(repository: str, sha: str) -> list[int]:
        assert (repository, sha) == ("owner/repo", "abc")
        assert receiver.accept_ci_delivery(CIWebhookDelivery(repository, "during-lookup", "check_run", "completed", (), sha), now=103)
        return [99]

    engine.github.get_pull_request_numbers_for_commit.side_effect = lookup
    with patch("src.auto_coder.entity_invalidation.time.time", return_value=103):
        asyncio.run(AutomationEngine._drain_ci_webhooks(engine, "owner/repo"))
        assert engine.invalidations.claim("owner/repo").identity == EntityIdentity("owner/repo", "pr", 2011)
        assert engine.invalidations.claim("owner/repo") is None
        assert engine.invalidations.seconds_until_next_ci("owner/repo") == 2

    engine.github.get_pull_request_numbers_for_commit.side_effect = None
    engine.github.get_pull_request_numbers_for_commit.return_value = [2012]
    with patch("src.auto_coder.entity_invalidation.time.time", return_value=105):
        asyncio.run(AutomationEngine._drain_ci_webhooks(engine, "owner/repo"))
        assert engine.invalidations.claim("owner/repo").identity == EntityIdentity("owner/repo", "pr", 2012)
        assert engine.invalidations.claim("owner/repo") is None
        assert engine.invalidations.seconds_until_next_ci("owner/repo") is None
    assert engine.github.get_pull_request_numbers_for_commit.call_count == 2
