from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.webhook_server import create_app


# Minimal mock to avoid importing everything
class MockGitHubClient:
    def create_issue(self, *args, **kwargs):
        return MagicMock(number=101)

    def get_issue_details(self, *args, **kwargs):
        return {"number": 101, "title": "Test Issue", "state": "open"}

    def get_pull_request(self, *args, **kwargs):
        return MagicMock(number=202)

    def get_pr_details(self, *args, **kwargs):
        return {"number": 202, "title": "Test PR", "state": "open"}


class MockQueue:
    def __init__(self):
        self.put_calls = []

    async def put(self, item):
        self.put_calls.append(item)


class MockEngine:
    def __init__(self):
        self.github = MockGitHubClient()
        self.queue = MockQueue()


def test_sentry_webhook():
    engine = MockEngine()
    app = create_app(engine, "owner/repo")

    with TestClient(app) as client:
        payload = {"message": "Something went wrong", "project_name": "MyProject", "level": "error", "url": "http://sentry.io/error/123"}

        response = client.post("/hooks/sentry", json=payload)
        assert response.status_code == 200
        assert response.json() == {"status": "received"}


def test_github_pr_webhook():
    engine = MockEngine()
    app = create_app(engine, "owner/repo")

    with TestClient(app) as client:
        payload = {"action": "opened", "pull_request": {"number": 202, "title": "New Feature"}}

        response = client.post("/hooks/github", json=payload, headers={"X-GitHub-Event": "pull_request"})
        assert response.status_code == 200
