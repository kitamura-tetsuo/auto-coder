"""Expiry and request-count contracts for explicit Issue declaration discovery."""

from datetime import datetime, timedelta
from threading import RLock
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.util.gh_cache import GitHubClient


@pytest.mark.parametrize("age,repository,expected_calls", [(3599, "o/r", 0), (3600, "o/r", 1), (3601, "o/r", 1), (0, "o/other", 1)])
def test_discovery_honors_memory_expiry_and_repository(age, repository, expected_calls):
    github = object.__new__(GitHubClient)
    github.token = "test"
    github._open_issues_cache_lock = RLock()
    github._open_issues_cache = [{"number": 10, "body": "cached"}]
    github._open_issues_cache_repo = repository
    now = datetime(2026, 9, 13, 12)
    github._open_issues_cache_time = now - timedelta(seconds=age)
    api = MagicMock()
    api.issues.list_for_repo.return_value = [{"number": 20, "body": "refreshed"}]
    with patch("auto_coder.util.gh_cache.datetime") as clock, patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=api) as factory:
        clock.now.return_value = now
        result = github.get_open_issue_declarations("o/r")
    assert result == ([{"number": 20, "body": "refreshed"}] if expected_calls else [{"number": 10, "body": "cached"}])
    assert factory.call_count == expected_calls
    assert api.issues.list_for_repo.call_count == expected_calls


def test_discovery_paginates_without_enrichment_or_pr_listing():
    github = object.__new__(GitHubClient)
    github.token = "test"
    github._open_issues_cache_lock = RLock()
    github._open_issues_cache = None
    api = MagicMock()
    api.issues.list_for_repo.side_effect = [[{"number": 1, "pull_request": {}}, {"number": 2, "pull_request": {}}], [{"number": 3, "body": "Parent-Issue: #10"}]]
    with patch("auto_coder.util.gh_cache.get_ghapi_client", return_value=api):
        result = github.get_open_issue_declarations("o/r", limit=2)
    assert result == [{"number": 3, "body": "Parent-Issue: #10"}]
    assert len(api.mock_calls) == 2
    assert [call.kwargs for call in api.issues.list_for_repo.call_args_list] == [{"state": "open", "per_page": 2, "page": 1}, {"state": "open", "per_page": 2, "page": 2}]


@pytest.mark.parametrize("max_age,expected_requests", [(3600, 1), (0, 2)])
def test_discovery_reuses_only_fresh_persistent_http_responses(tmp_path, monkeypatch, max_age, expected_requests):
    import httpx

    from auto_coder.util.gh_cache import get_caching_client

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()
    sent = []

    def respond(request):
        sent.append(request.url.path)
        return httpx.Response(200, headers={"Cache-Control": f"private, max-age={max_age}"}, json=[{"number": 3, "body": "Parent-Issue: #10"}])

    for _ in range(2):
        github = object.__new__(GitHubClient)
        github.token = "test"
        github._open_issues_cache_lock = RLock()
        github._open_issues_cache = None
        with patch.object(httpx.HTTPTransport, "handle_request", lambda _transport, request: respond(request)), get_caching_client(admission_hook=lambda _context: True) as client:
            with patch("auto_coder.util.gh_cache.get_caching_client", return_value=client):
                assert github.get_open_issue_declarations("o/r") == [{"number": 3, "body": "Parent-Issue: #10"}]
    assert sent == ["/repos/o/r/issues"] * expected_requests


def test_private_http_cache_isolates_credentials_and_preserves_wire_extensions(tmp_path, monkeypatch):
    import httpx

    from auto_coder.util.gh_cache import _caching_request, get_caching_client

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".cache").mkdir()
    sent = []
    admitted = []

    def respond(request):
        assert request.extensions["timeout"]["read"] == 7
        assert request.extensions.get("auto_coder_operation_id")
        identity = request.headers.get("Authorization", "")
        sent.append(identity)
        return httpx.Response(200, headers={"Cache-Control": "private, max-age=3600"}, json={"identity": identity})

    with patch.object(httpx.HTTPTransport, "handle_request", lambda _transport, request: respond(request)):
        for credential in ("first-test-token", "second-test-token", "first-test-token"):
            with get_caching_client(admission_hook=lambda context: admitted.append(context) or True) as client:
                response = _caching_request(client, "GET", "https://api.github.com/repos/o/r/issues", headers={"Authorization": credential}, timeout=7, path_template="/repos/{owner}/{repo}/issues")
                assert response.json() == {"identity": credential}
    assert sent == ["first-test-token", "second-test-token"]
    assert len(admitted) == 2


@pytest.mark.parametrize("allowed", [True, False])
def test_cached_graphql_stream_is_classified_before_admission_without_losing_body(tmp_path, monkeypatch, allowed):
    import json

    import httpx

    from auto_coder.util.gh_cache import _caching_request, get_caching_client
    from auto_coder.util.github_request_outcome import GitHubRequestRefused

    monkeypatch.chdir(tmp_path)
    admitted = []
    sent = []
    payload = {"query": "mutation TestMutation { __typename }", "variables": {"value": "synthetic-private-value"}}

    def respond(request):
        sent.append(json.loads(request.read()))
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(200, json={"data": {"__typename": "Mutation"}})

    with patch.object(httpx.HTTPTransport, "handle_request", lambda _transport, request: respond(request)):
        with get_caching_client(admission_hook=lambda context: admitted.append(context) or allowed) as client:

            def send():
                return _caching_request(client, "POST", "https://api.github.com/graphql", json=payload, timeout=7, path_template="/graphql")

            if allowed:
                assert send().json() == {"data": {"__typename": "Mutation"}}
            else:
                with pytest.raises(GitHubRequestRefused):
                    send()
    assert len(admitted) == 1
    assert admitted[0].kind == "mutation"
    assert sent == ([payload] if allowed else [])
    assert "synthetic-private-value" not in repr(admitted)


def test_connected_pr_lookup_survives_cache_request_stream_conversion(tmp_path, monkeypatch):
    import json

    import httpx

    from auto_coder.util.gh_cache import get_caching_client

    monkeypatch.chdir(tmp_path)
    github = object.__new__(GitHubClient)
    github.token = "test"
    admitted = []
    sent = []
    response_body = {"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [{"number": 77, "repository": {"owner": {"login": "o"}, "name": "r"}}], "pageInfo": {"hasNextPage": False}}}}}}

    def respond(request):
        sent.append(json.loads(request.read()))
        return httpx.Response(200, json=response_body)

    with patch.object(httpx.HTTPTransport, "handle_request", lambda _transport, request: respond(request)):
        with get_caching_client(admission_hook=lambda context: admitted.append(context) or True) as client:
            with patch("auto_coder.util.gh_cache.get_caching_client", return_value=client):
                assert github.get_connected_prs("o/r", 2048) == [77]
    assert len(sent) == 1
    assert sent[0]["variables"] == {"owner": "o", "name": "r", "number": 2048, "cursor": None}
    assert [context.kind for context in admitted] == ["read"]
