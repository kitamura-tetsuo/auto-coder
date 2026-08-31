"""Tests for dedicated GitHub App adversarial review publication."""

from pathlib import Path

import httpx
import pytest

from auto_coder.adversarial_validator import AdversarialValidationFinding, AdversarialValidationResult
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig, load_reviewer_app_config


class RecordingClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def response(status: int, data: dict[str, object]) -> httpx.Response:
    return httpx.Response(status, json=data, request=httpx.Request("GET", "https://api.github.test"))


def configured_reviewer(tmp_path: Path, client: RecordingClient, monkeypatch: pytest.MonkeyPatch, now: float = 1_000.0) -> GitHubAppReviewer:
    key = tmp_path / "reviewer.pem"
    key.write_text("fake private key", encoding="utf-8")
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "fake-app-jwt")
    return GitHubAppReviewer(ReviewerAppConfig("123", "client", key), api_url="https://api.github.test", client=client, clock=lambda: now)


def auth_responses(head_sha: str = "sha-a") -> list[httpx.Response]:
    return [
        response(200, {"id": 77}),
        response(201, {"token": "fake-installation-token", "expires_at": "2099-01-01T00:00:00Z"}),
        response(200, {"head": {"sha": head_sha}}),
        response(200, {"id": 9}),
    ]


def test_loads_existing_user_facing_configuration_shape(tmp_path: Path) -> None:
    config_dir = tmp_path / ".auto-coder"
    config_dir.mkdir()
    config = config_dir / "config.toml"
    config.write_text('[github-app-auto-coder-reviewer]\napp_id = "4765828"\nclient_id = "client-id"\n', encoding="utf-8")

    loaded = load_reviewer_app_config(config, tmp_path)

    assert loaded.app_id == "4765828"
    assert loaded.client_id == "client-id"
    assert loaded.private_key_path == config_dir / "auto-coder-reviewer.pem"


@pytest.mark.parametrize(
    ("result", "event"),
    [
        (AdversarialValidationResult(result="PASS", summary="Verified"), "APPROVE"),
        (
            AdversarialValidationResult(
                result="NEEDS_FIX",
                summary="Violation",
                findings=[AdversarialValidationFinding("Requirement", "Counterexample", "Missing test", "Regression")],
            ),
            "REQUEST_CHANGES",
        ),
        (AdversarialValidationResult(result="BLOCKED", summary="Could not validate"), "COMMENT"),
    ],
)
def test_publishes_native_review_with_installation_token_and_exact_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: AdversarialValidationResult, event: str) -> None:
    client = RecordingClient(auth_responses())
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", result)

    assert publication.success is True
    assert publication.event == event
    review_call = client.calls[-1]
    assert review_call[0] == "POST"
    assert review_call[1].endswith("/repos/owner/repo/pulls/42/reviews")
    assert review_call[2]["json"] == {"body": reviewer._body(result), "event": event, "commit_id": "sha-a"}
    assert review_call[2]["headers"]["Authorization"] == "Bearer fake-installation-token"  # type: ignore[index]


def test_head_race_fails_closed_without_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient(auth_responses("sha-b")[:-1])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS"))

    assert publication.success is False
    assert publication.event == "APPROVE"
    assert len(client.calls) == 3
    assert all(not call[1].endswith("/reviews") for call in client.calls)


def test_auth_failure_does_not_submit_a_review_or_expose_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    client = RecordingClient([response(401, {"message": "rejected"})])
    reviewer = configured_reviewer(tmp_path, client, monkeypatch)

    publication = reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS"))

    assert publication.success is False
    assert len(client.calls) == 1
    assert "fake-app-jwt" not in caplog.text
    assert "fake-installation-token" not in caplog.text


def test_cached_expired_token_is_refreshed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = RecordingClient(auth_responses() + auth_responses())
    now = [1_000.0]
    reviewer = configured_reviewer(tmp_path, client, monkeypatch, now[0])
    reviewer._clock = lambda: now[0]

    assert reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS")).success
    reviewer._tokens["owner/repo"].expires_at = 1_030.0
    now[0] = 1_001.0
    assert reviewer.publish("owner/repo", 42, "sha-a", AdversarialValidationResult(result="PASS")).success
    assert sum(call[1].endswith("/installation") for call in client.calls) == 2
