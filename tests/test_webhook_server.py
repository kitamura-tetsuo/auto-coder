from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.github_ci_observer import accept_and_fence_ci_delivery as real_accept_and_fence_ci_delivery
from src.auto_coder.github_ci_observer import ci_observation_merge_authority, ci_read_phase, observe_ci
from src.auto_coder.util.gh_cache import GitHubClient
from src.auto_coder.webhook_server import create_app


class MockInvalidations(list):
    def accept_ci_delivery(self, delivery):
        self.append(delivery)
        return True


# Minimal mock to avoid importing everything
class MockGitHubClient:
    def __init__(self):
        self.commit_pull_requests = []

    def create_issue(self, *args, **kwargs):
        return MagicMock(number=101)

    def get_issue_details(self, *args, **kwargs):
        return {"number": 101, "title": "Test Issue", "state": "open"}

    def get_pull_request(self, *args, **kwargs):
        return MagicMock(number=202)

    def get_pr_details(self, *args, **kwargs):
        return {"number": 202, "title": "Test PR", "state": "open"}

    def get_pull_request_numbers_for_commit(self, repo_name, sha):
        return self.commit_pull_requests


class MockQueue:
    def __init__(self):
        self.put_calls = []

    async def put(self, item):
        self.put_calls.append(item)


class MockEngine:
    def __init__(self):
        self.github = MockGitHubClient()
        self.queue = MockQueue()
        self.invalidations = MockInvalidations()

    async def invalidate_entity(self, repo_name, entity_type, number, delivery_id=None, event_type=None, action=None, not_before=None):
        self.invalidations.append((repo_name, entity_type, number, delivery_id, event_type, action))
        return True


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_real_ci_webhook_route_waits_for_merge_authority_boundary(mock_init_dashboard):
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    actions = MagicMock()
    actions.list_workflow_runs_for_repo.return_value = {
        "workflow_runs": [
            {
                "id": 10,
                "workflow_id": 1,
                "run_attempt": 1,
                "head_sha": "head",
                "status": "completed",
                "conclusion": "success",
                "path": ".github/workflows/pr-tests.yml",
            }
        ]
    }
    checks = MagicMock()
    checks.list_for_ref.return_value = {"check_runs": []}
    api = SimpleNamespace(actions=actions, checks=checks)
    route_entered_barrier = Event()
    response_finished = Event()
    response_holder = []

    def observed_accept_and_fence(accept, reason):
        route_entered_barrier.set()
        return real_accept_and_fence_ci_delivery(accept, reason)

    with TestClient(app) as client, ci_read_phase("merge-boundary"):
        snapshot = observe_ci(api, "credential", "owner/repo", 7, "head")

        def post_delivery():
            response_holder.append(
                client.post(
                    "/hooks/github",
                    json={
                        "action": "in_progress",
                        "workflow_run": {"id": 11, "workflow_id": 1, "run_attempt": 1, "head_sha": "head", "pull_requests": [{"number": 7}]},
                        "repository": {"full_name": "owner/repo"},
                    },
                    headers={"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": "race-delivery"},
                )
            )
            response_finished.set()

        with patch("src.auto_coder.webhook_server.accept_and_fence_ci_delivery", side_effect=observed_accept_and_fence):
            with ci_observation_merge_authority(snapshot) as current:
                assert current is True
                thread = Thread(target=post_delivery)
                thread.start()
                assert route_entered_barrier.wait(2)
                assert engine.invalidations == []
                assert not response_finished.wait(0.1)
            thread.join(2)

    assert response_finished.is_set()
    assert response_holder[0].status_code == 200
    assert len(engine.invalidations) == 1


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_sentry_webhook(mock_init_dashboard):
    engine = MockEngine()
    app = create_app(engine, "owner/repo")

    with TestClient(app) as client:
        payload = {"message": "Something went wrong", "project_name": "MyProject", "level": "error", "url": "http://sentry.io/error/123"}

        response = client.post("/hooks/sentry", json=payload)
        assert response.status_code == 200
        assert response.json() == {"status": "received"}


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_github_pr_webhook(mock_init_dashboard):
    engine = MockEngine()
    app = create_app(engine, "owner/repo")

    with TestClient(app) as client:
        payload = {"action": "opened", "pull_request": {"number": 202, "title": "New Feature"}, "repository": {"full_name": "owner/repo"}}

        response = client.post("/hooks/github", json=payload, headers={"X-GitHub-Event": "pull_request"})
        assert response.status_code == 200
        assert engine.invalidations == [("owner/repo", "pr", 202, None, "pull_request", "opened")]


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_type"),
    [
        ("pull_request", {"action": "synchronize", "pull_request": {"number": 7}}, "pr"),
        ("pull_request_review_thread", {"action": "resolved", "pull_request": {"number": 8}}, "pr"),
        ("pull_request_review", {"action": "submitted", "pull_request": {"number": 9}}, "pr"),
        ("pull_request_review_comment", {"action": "created", "pull_request": {"number": 10}}, "pr"),
        ("issue_comment", {"action": "created", "issue": {"number": 11}}, "issue"),
        ("issue_comment", {"action": "edited", "issue": {"number": 12, "pull_request": {}}}, "pr"),
        ("issues", {"action": "labeled", "issue": {"number": 13}}, "issue"),
    ],
)
@patch("src.auto_coder.webhook_server.init_dashboard")
def test_material_webhooks_are_normalized_at_http_boundary(mock_init_dashboard, event_type, payload, expected_type):
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    payload["repository"] = {"full_name": "owner/repo"}
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json=payload,
            headers={"X-GitHub-Event": event_type, "X-GitHub-Delivery": "delivery-uuid"},
        )
    assert response.status_code == 200
    number = payload.get("pull_request", payload.get("issue"))["number"]
    assert engine.invalidations == [("owner/repo", expected_type, number, "delivery-uuid", event_type, payload["action"])]


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_completed_check_without_embedded_pr_is_persisted_without_lookup(mock_init_dashboard):
    engine = MockEngine()
    engine.github.commit_pull_requests = [21, 22]
    app = create_app(engine, "owner/repo")
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json={"action": "completed", "check_run": {"head_sha": "abc", "pull_requests": []}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "check_run", "X-GitHub-Delivery": "check-delivery"},
        )
    assert response.status_code == 200
    assert engine.github.commit_pull_requests == [21, 22]
    assert len(engine.invalidations) == 1
    delivery = engine.invalidations[0]
    assert delivery.delivery_id == "check-delivery"
    assert delivery.head_sha == "abc"
    assert delivery.pull_request_numbers == ()


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_completed_check_defers_paginated_commit_lookup(mock_init_dashboard, monkeypatch):
    engine = MockEngine()
    engine.github = GitHubClient("token")
    app = create_app(engine, "owner/repo")
    first_url = "https://api.github.com/repos/owner/repo/commits/abc/pulls?per_page=100"
    second_url = "https://api.github.com/repositories/1/commits/abc/pulls?per_page=100&page=2"
    first = httpx.Response(
        200,
        json=[{"number": number} for number in range(1, 31)],
        headers={"Link": f'<{second_url}>; rel="next"'},
        request=httpx.Request("GET", first_url),
    )
    second = httpx.Response(200, json=[{"number": 31}], request=httpx.Request("GET", second_url))
    get = MagicMock(side_effect=[first, second])
    monkeypatch.setattr("src.auto_coder.util.gh_cache.httpx.get", get)

    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json={"action": "completed", "check_run": {"head_sha": "abc", "pull_requests": []}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "check_run", "X-GitHub-Delivery": "paginated-check"},
        )

    assert response.status_code == 200
    assert len(engine.invalidations) == 1
    assert engine.invalidations[0].head_sha == "abc"
    assert get.call_args_list == []


@pytest.mark.parametrize("event_type,entity_key", [("issues", "issue"), ("pull_request", "pull_request")])
@pytest.mark.parametrize("action", ["labeled", "unlabeled"])
@patch("src.auto_coder.webhook_server.init_dashboard")
def test_exact_legacy_auto_coder_label_change_does_not_invalidate(mock_init_dashboard, event_type, entity_key, action):
    """FTR-1792 AS-001: a labeled/unlabeled webhook for the exact retired
    '@auto-coder' label must not create a durable invalidation."""
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    payload = {
        "action": action,
        entity_key: {"number": 42},
        "label": {"name": "@auto-coder"},
        "repository": {"full_name": "owner/repo"},
    }
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json=payload,
            headers={"X-GitHub-Event": event_type, "X-GitHub-Delivery": "legacy-label-delivery"},
        )
    assert response.status_code == 200
    assert engine.invalidations == []


@pytest.mark.parametrize("label_name", ["auto-coder", "@auto-coder-old", "@Auto-Coder"])
@patch("src.auto_coder.webhook_server.init_dashboard")
def test_near_miss_label_changes_still_invalidate(mock_init_dashboard, label_name):
    """FTR-1792 AS-002: only the exact '@auto-coder' text is suppressed; every
    other label (including near-misses) continues through normal invalidation."""
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    payload = {
        "action": "labeled",
        "issue": {"number": 43},
        "label": {"name": label_name},
        "repository": {"full_name": "owner/repo"},
    }
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json=payload,
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": f"delivery-{label_name}"},
        )
    assert response.status_code == 200
    assert engine.invalidations == [("owner/repo", "issue", 43, f"delivery-{label_name}", "issues", "labeled")]


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_native_dependency_payload_records_local_numbers_and_scoped_obligation(mock_init_dashboard):
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    payload = {
        "action": "blocked_by_added",
        "blocked_issue": {
            "id": 900000205,
            "number": 205,
            "repository_url": "https://api.github.com/repos/owner/repo",
        },
        "blocking_issue": {
            "id": 900000101,
            "number": 101,
            "repository_url": "https://api.github.com/repos/foreign/repo",
        },
        "repository": {"full_name": "owner/repo"},
    }
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json=payload,
            headers={"X-GitHub-Event": "issue_dependencies", "X-GitHub-Delivery": "native-edge"},
        )

    assert response.status_code == 200
    assert engine.invalidations == [
        ("owner/repo", "dependency", 1, "native-edge", "issue_dependencies", "blocked_by_added"),
        ("owner/repo", "issue", 205, "native-edge", "issue_dependencies", "blocked_by_added"),
    ]


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_legacy_label_removal_also_suppressed(mock_init_dashboard):
    """AS-001 covers both labeled and unlabeled actions for the exact retired label."""
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    payload = {
        "action": "unlabeled",
        "pull_request": {"number": 44},
        "label": {"name": "@auto-coder"},
        "repository": {"full_name": "owner/repo"},
    }
    with TestClient(app) as client:
        response = client.post(
            "/hooks/github",
            json=payload,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "legacy-unlabel-delivery"},
        )
    assert response.status_code == 200
    assert engine.invalidations == []


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_irrelevant_action_and_wrong_repository_do_not_invalidate(mock_init_dashboard):
    engine = MockEngine()
    app = create_app(engine, "owner/repo")
    with TestClient(app) as client:
        ignored = client.post(
            "/hooks/github",
            json={"action": "milestoned", "issue": {"number": 1}, "repository": {"full_name": "owner/repo"}},
            headers={"X-GitHub-Event": "issues"},
        )
        rejected = client.post(
            "/hooks/github",
            json={"action": "opened", "issue": {"number": 2}, "repository": {"full_name": "other/repo"}},
            headers={"X-GitHub-Event": "issues"},
        )
    assert ignored.status_code == 200
    assert rejected.status_code == 403
    assert engine.invalidations == []
