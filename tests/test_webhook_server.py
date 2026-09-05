from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.util.gh_cache import GitHubClient
from src.auto_coder.webhook_server import create_app


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
        self.invalidations = []

    async def invalidate_entity(self, repo_name, entity_type, number, delivery_id=None, event_type=None, action=None):
        self.invalidations.append((repo_name, entity_type, number, delivery_id, event_type, action))
        return True


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
def test_completed_check_without_embedded_pr_resolves_commit(mock_init_dashboard):
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
    assert [item[2] for item in engine.invalidations] == [21, 22]
    assert all(item[3:] == ("check-delivery", "check_run", "completed") for item in engine.invalidations)


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_completed_check_paginates_commit_lookup_before_invalidating(mock_init_dashboard, monkeypatch):
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
    assert [item[2] for item in engine.invalidations] == list(range(1, 32))
    assert [call.args[0] for call in get.call_args_list] == [first_url, second_url]


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
