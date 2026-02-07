import hashlib
import hmac
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

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


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_github_webhook_security_valid_signature(mock_init_dashboard):
    engine = MockEngine()
    secret = "mysecret"
    app = create_app(engine, "owner/repo", github_secret=secret)

    with TestClient(app) as client:
        payload = {"action": "opened", "pull_request": {"number": 202, "title": "New Feature"}}
        payload_bytes = b'{"action": "opened", "pull_request": {"number": 202, "title": "New Feature"}}'

        # Calculate valid signature
        mac = hmac.new(secret.encode(), msg=payload_bytes, digestmod=hashlib.sha256)
        signature = f"sha256={mac.hexdigest()}"

        # Note: TestClient handles json encoding, but we need exact bytes for signature.
        # So we pass content=payload_bytes and set content-type header.
        response = client.post("/hooks/github", content=payload_bytes, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": signature, "Content-Type": "application/json"})
        assert response.status_code == 200
        assert response.json() == {"status": "received"}


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_github_webhook_security_malformed_signature_too_many_equals(mock_init_dashboard):
    engine = MockEngine()
    secret = "mysecret"
    app = create_app(engine, "owner/repo", github_secret=secret)

    with TestClient(app) as client:
        payload_bytes = b"{}"

        # Calculate valid signature part
        mac = hmac.new(secret.encode(), msg=payload_bytes, digestmod=hashlib.sha256)
        # Malformed signature: sha256=hash=extra
        signature = f"sha256={mac.hexdigest()}=extra"

        try:
            response = client.post("/hooks/github", content=payload_bytes, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": signature, "Content-Type": "application/json"})
            # Should fail gracefully (403), not crash (500)
            # Currently this crashes with 500 due to ValueError
            assert response.status_code == 403, f"Expected 403, got {response.status_code}. Response: {response.text}"
        except Exception as e:
            pytest.fail(f"Server crashed: {e}")


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_github_webhook_security_invalid_prefix(mock_init_dashboard):
    engine = MockEngine()
    secret = "mysecret"
    app = create_app(engine, "owner/repo", github_secret=secret)

    with TestClient(app) as client:
        payload_bytes = b"{}"
        mac = hmac.new(secret.encode(), msg=payload_bytes, digestmod=hashlib.sha256)
        signature = f"sha1={mac.hexdigest()}"

        response = client.post("/hooks/github", content=payload_bytes, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": signature, "Content-Type": "application/json"})
        assert response.status_code == 403
        assert "Invalid signature" in response.json()["detail"]


@patch("src.auto_coder.webhook_server.init_dashboard")
def test_github_webhook_security_missing_signature(mock_init_dashboard):
    engine = MockEngine()
    secret = "mysecret"
    app = create_app(engine, "owner/repo", github_secret=secret)

    with TestClient(app) as client:
        response = client.post("/hooks/github", json={}, headers={"X-GitHub-Event": "pull_request"})
        assert response.status_code == 403
