"""Bounded GhApi wire waits and shared governor recovery after a timeout."""

import httpx
import pytest

from auto_coder.github_request_governor import GitHubRequestGovernor
from auto_coder.util.gh_cache import get_caching_client, get_ghapi_client
from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, GitHubRequestError


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}),
        ({"timeout": None}, {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}),
        ({"timeout": 7.0}, {"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}),
        ({"timeout": httpx.Timeout(8.0, read=2.0)}, {"connect": 8.0, "read": 2.0, "write": 8.0, "pool": 8.0}),
    ],
)
def test_ghapi_timeout_reaches_wire_through_cache(tmp_path, monkeypatch, kwargs, expected):
    monkeypatch.chdir(tmp_path)
    sent = []

    def respond(_transport, request):
        sent.append(request.extensions["timeout"])
        return httpx.Response(200, json={"check_runs": []})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with get_caching_client(admission_hook=lambda _context: True) as client:
        monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: client)
        api = get_ghapi_client("test-token")
        assert api("/repos/owner/repo/commits/head/check-runs", **kwargs) == {"check_runs": []}
    assert sent == [expected]


def test_ghapi_read_timeout_releases_governor_for_next_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    sent = []

    def respond(_transport, request):
        sent.append(request.url.path)
        assert request.extensions["timeout"]["read"] == 30.0
        if len(sent) == 1:
            raise httpx.ReadTimeout("response headers timed out", request=request)
        return httpx.Response(200, json={"state": "closed"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    try:
        with get_caching_client(admission_hook=governor.admit, observation_hook=governor.observe) as client:
            monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args: client)
            api = get_ghapi_client("test-token", admission_hook=governor.admit, observation_hook=governor.observe)
            with pytest.raises(GitHubRequestError) as failure:
                api.checks.list_for_ref("owner", "repo", "head")
            assert isinstance(failure.value.__cause__, httpx.ReadTimeout)
            assert failure.value.outcome.classification is GitHubApiOutcome.TRANSPORT_FAILURE
            assert failure.value.outcome.delivery is DeliveryCertainty.INDETERMINATE
            assert api.pulls.get("owner", "repo", 5506) == {"state": "closed"}
        assert sent == ["/repos/owner/repo/commits/head/check-runs", "/repos/owner/repo/pulls/5506"]
    finally:
        governor.close()
