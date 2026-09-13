from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

import httpx
import pytest

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.github_app_reviewer import GitHubAppReviewer, ReviewerAppConfig
from auto_coder.logger_config import setup_logger
from auto_coder.util.gh_cache import ActionsSecretPublisher, GitHubClient, get_ghapi_client
from auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    DiagnosticTransport,
    GitHubApiOutcome,
    GitHubRequestError,
    GitHubRequestRefused,
    RequestProvenance,
    classify_response,
    configure_github_request_boundary,
    github_http_client,
    response_metadata,
)


class _RaisingStream(httpx.SyncByteStream):
    """A response body stream that fails mid-read, after headers arrive."""

    def __iter__(self):
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

    def close(self) -> None:
        pass


def test_process_boundary_refuses_distinct_production_credentials_without_a_send(tmp_path, monkeypatch):
    """Controller, reviewer-App, and secret publication all reach one seam."""
    sent = []

    def forbidden_send(self, request):
        sent.append(request)
        return httpx.Response(500, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden_send)
    monkeypatch.setattr("auto_coder.github_app_reviewer.jwt.encode", lambda *args, **kwargs: "reviewer-jwt")
    key = tmp_path / "reviewer.pem"
    key.write_text("private", encoding="utf-8")
    observations = []
    configure_github_request_boundary(lambda context: False, observations.append)
    try:
        with pytest.raises(GitHubRequestRefused):
            GitHubClient("ordinary-token").get_issue_dispatch_snapshot_strict("acme/widgets", 1)

        reviewer = GitHubAppReviewer(ReviewerAppConfig("123", private_key_path=key))
        assert reviewer.publish("acme/widgets", 2, "sha", AdversarialValidationResult(result="PASS")).success is False

        with pytest.raises(Exception):
            ActionsSecretPublisher("secret-token").set_repository_secret("acme/widgets", "VALUE", "secret")
    finally:
        configure_github_request_boundary()

    assert sent == []
    assert len(observations) == 3
    assert {item.context.subsystem for item in observations} == {
        "controller-issue-read",
        "reviewer-app",
        "actions-secrets",
    }
    assert all(item.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT for item in observations)


def _client(handler, **kwargs):
    transport = DiagnosticTransport(httpx.MockTransport(handler), **kwargs)
    return httpx.Client(transport=transport)


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (403, {"X-RateLimit-Remaining": "0"}, {"message": "forbidden"}, GitHubApiOutcome.PRIMARY_THROTTLED),
        (403, {"x-ratelimit-remaining": "2", "Retry-After": "3"}, {"message": "wait"}, GitHubApiOutcome.SECONDARY_THROTTLED),
        (403, {}, {"message": "ABUSE DETECTION mechanism"}, GitHubApiOutcome.SECONDARY_THROTTLED),
        (403, {}, {"message": "Resource forbidden"}, GitHubApiOutcome.FORBIDDEN),
        (401, {}, {}, GitHubApiOutcome.AUTHENTICATION_FAILURE),
        (429, {}, {}, GitHubApiOutcome.THROTTLED),
    ],
)
def test_production_ghapi_rest_classifies_response(status, headers, body, expected):
    observations = []

    def handler(request):
        return httpx.Response(status, headers=headers, json=body, request=request)

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=_client(handler)):
        api = get_ghapi_client("known-token", observation_hook=observations.append)
        with pytest.raises(GitHubRequestError) as caught:
            api("/repos/acme/widgets/issues/17/comments", verb="POST", data={"body": "secret"})

    assert caught.value.outcome.classification is expected
    assert caught.value.outcome.context.repository == "acme/widgets"
    assert observations[-1].classification is expected


def test_graphql_partial_data_never_erases_structured_error():
    def handler(request):
        return httpx.Response(
            200,
            headers={"x-ratelimit-remaining": "0", "x-github-request-id": "REQ-1"},
            json={"data": {"addComment": {"id": "partial"}}, "errors": [{"type": "RATE_LIMITED", "message": "limit"}]},
            request=request,
        )

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=_client(handler)):
        with pytest.raises(GitHubRequestError) as caught:
            get_ghapi_client("token")("/graphql", verb="POST", data={"query": "mutation X { x }"})

    outcome = caught.value.outcome
    assert outcome.classification is GitHubApiOutcome.PRIMARY_THROTTLED
    assert outcome.delivery is DeliveryCertainty.HTTP_RESPONSE_RECEIVED
    assert outcome.metadata.github_request_id == "REQ-1"


def test_header_parsing_rejects_invalid_values_and_accepts_http_date():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    metadata = response_metadata(
        {
            "Retry-After": future,
            "X-RateLimit-Limit": "nan",
            "X-RateLimit-Remaining": "-1",
            "X-RateLimit-Used": "2.5",
            "X-RateLimit-Reset": "inf",
        }
    )
    assert metadata.retry_after_seconds is not None and 0 <= metadata.retry_after_seconds <= 60
    assert metadata.rate_limit_limit is None
    assert metadata.rate_limit_remaining is None
    assert metadata.rate_limit_used is None
    assert metadata.rate_limit_reset is None
    assert classify_response(403, metadata) is GitHubApiOutcome.SECONDARY_THROTTLED


def test_admission_refusal_is_definitely_not_sent():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(204, request=request)

    client = _client(handler, admission_hook=lambda context: False)
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=client):
        with pytest.raises(GitHubRequestRefused) as caught:
            get_ghapi_client("token", admission_hook=lambda context: False)("/repos/acme/widgets/issues/1/comments", verb="POST", data={"body": "x"})
    assert sent == []
    assert caught.value.outcome.delivery is DeliveryCertainty.DEFINITELY_NOT_SENT


def test_timeout_is_indeterminate_and_secret_safe(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "github.log"
    setup_logger(stream=console, log_file=str(log_file))

    def handler(request):
        raise httpx.ReadTimeout(f"token=SENSITIVE request={request.url} Authorization: Bearer SENSITIVE")

    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=_client(handler)):
        with pytest.raises(GitHubRequestError) as caught:
            get_ghapi_client("SENSITIVE")("/repos/acme/widgets/issues?signature=SIGNED", verb="POST", data={"secret": "BODYSECRET"})

    assert caught.value.outcome.delivery is DeliveryCertainty.INDETERMINATE
    rendered = console.getvalue() + log_file.read_text()
    assert "SENSITIVE" not in rendered
    assert "SIGNED" not in rendered
    assert "BODYSECRET" not in rendered


def test_body_read_failure_finalizes_instead_of_stranding_occupancy():
    """AS-003/REQ-004: a body-read failure inside the response hook must still
    deliver a terminal, non-fabricated observation and must not automatically
    resend the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-ratelimit-remaining": "5"}, stream=_RaisingStream(), request=request)

    observations = []
    sent = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return handler(request)

    with github_http_client(
        subsystem="controller-strict",
        transport=httpx.MockTransport(counting_handler),
        observation_hook=observations.append,
    ) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            client.get("https://api.github.com/repos/acme/widgets")

    assert len(sent) == 1
    assert len(observations) == 1
    outcome = observations[0]
    assert outcome.status == 200
    assert outcome.delivery is DeliveryCertainty.HTTP_RESPONSE_RECEIVED
    assert outcome.provenance is RequestProvenance.NETWORK
    assert outcome.metadata.rate_limit_remaining == 5
    assert outcome.classification is GitHubApiOutcome.SUCCESS


def test_success_payload_is_preserved_and_cache_result_is_separate():
    request = httpx.Request("GET", "https://api.github.com/repos/acme/widgets")
    response = httpx.Response(200, json={"private": "application payload"}, request=request)
    observations = []
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=httpx.Client(transport=httpx.MockTransport(lambda _: response))):
        result = get_ghapi_client("token", observation_hook=observations.append)("/repos/acme/widgets")
    assert result == {"private": "application payload"}
    assert observations[-1].provenance is RequestProvenance.LOCAL_CACHE
