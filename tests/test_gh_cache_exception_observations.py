import asyncio
import sqlite3

import httpx
import pytest

from auto_coder.github_request_governor import GitHubRequestDeferred, GitHubRequestGovernor
from auto_coder.util.gh_cache import _caching_request, get_caching_client, get_ghapi_client
from auto_coder.util.github_request_outcome import DeliveryCertainty, GitHubApiOutcome, RequestProvenance, configure_github_request_boundary, take_wire_outcomes


@pytest.mark.parametrize("adapter", ["ghapi", "direct"])
def test_real_cache_write_lock_does_not_strand_wire_reservation(tmp_path, monkeypatch, adapter):
    monkeypatch.chdir(tmp_path)
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    sent = []
    locks = []

    def respond(_transport, request):
        sent.append(request.url.path)
        if len(sent) == 1:
            lock = sqlite3.connect(tmp_path / ".cache/gh_cache.db", timeout=0)
            lock.execute("BEGIN IMMEDIATE")
            locks.append(lock)
        return httpx.Response(200, headers={"cache-control": "max-age=60", "x-ratelimit-remaining": "10"}, json={"state": "open"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    configure_github_request_boundary(governor.admit, governor.observe)
    try:
        with get_caching_client(admission_hook=governor.admit, observation_hook=governor.observe) as client:
            monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args: client)
            api = get_ghapi_client("fixture-token", admission_hook=governor.admit, observation_hook=governor.observe)
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                if adapter == "ghapi":
                    api("/repos/owner/repo/issues/1")
                else:
                    _caching_request(client, "GET", "https://api.github.com/repos/owner/repo/issues/1", path_template="/repos/owner/repo/issues/{id}")
            for lock in locks:
                lock.rollback()
                lock.close()
            locks.clear()
            assert sent == ["/repos/owner/repo/issues/1"]
            with sqlite3.connect(tmp_path / "governor.sqlite3") as connection:
                assert connection.execute("SELECT resolved FROM reservations").fetchall() == [(1,)]
            assert api("/repos/owner/repo/issues/2") == {"state": "open"}
            assert sent == ["/repos/owner/repo/issues/1", "/repos/owner/repo/issues/2"]
    finally:
        for lock in locks:
            lock.rollback()
            lock.close()
        configure_github_request_boundary()
        governor.close()


@pytest.mark.parametrize("adapter", ["ghapi", "direct"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [200, 429])
def test_cached_body_failure_retains_headers_without_replay(tmp_path, monkeypatch, adapter, method, status):
    monkeypatch.chdir(tmp_path)
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    sent = []
    observations = []
    failure = httpx.RemoteProtocolError("body unavailable; token=DO_NOT_LOG")

    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise failure
            yield b""  # pragma: no cover

    def observe(outcome):
        observations.append(outcome)
        governor.observe(outcome)

    def respond(_transport, request):
        sent.append((request.method, request.url.path))
        if len(sent) == 1:
            headers = {"x-ratelimit-remaining": "5", "x-github-request-id": "received-header"}
            if status == 429:
                headers["retry-after"] = "120"
            return httpx.Response(status, headers=headers, stream=BrokenBody())
        return httpx.Response(200, headers={"cache-control": "max-age=60"}, json={"state": "open"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    configure_github_request_boundary(governor.admit, observe)
    try:
        with get_caching_client(admission_hook=governor.admit, observation_hook=observe) as client:
            monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args: client)
            api = get_ghapi_client("fixture-token", admission_hook=governor.admit, observation_hook=observe)
            with pytest.raises(httpx.RemoteProtocolError) as caught:
                if adapter == "ghapi":
                    api("/repos/owner/repo/issues/1", verb=method)
                else:
                    _caching_request(client, method, "https://api.github.com/repos/owner/repo/issues/1", path_template="/repos/owner/repo/issues/{id}")
            assert caught.value is failure
            assert sent == [(method, "/repos/owner/repo/issues/1")]
            assert len(observations) == 1
            outcome = observations[0]
            assert outcome.status == status
            assert outcome.delivery is DeliveryCertainty.HTTP_RESPONSE_RECEIVED
            assert outcome.provenance is RequestProvenance.NETWORK
            assert outcome.metadata.github_request_id == "received-header"
            assert outcome.metadata.rate_limit_remaining == 5
            assert outcome.message is None
            assert take_wire_outcomes() == []
            with sqlite3.connect(tmp_path / "governor.sqlite3") as connection:
                assert connection.execute("SELECT resolved FROM reservations").fetchall() == [(1,)]
            if status == 429:
                assert outcome.classification is GitHubApiOutcome.SECONDARY_THROTTLED
                assert outcome.metadata.retry_after_seconds == 120
                with pytest.raises(GitHubRequestDeferred) as deferred:
                    api("/repos/owner/repo/issues/2")
                assert deferred.value.reason == "rate_limit_cooldown"
                assert len(sent) == 1
            else:
                assert api("/repos/owner/repo/issues/2") == {"state": "open"}
                assert api("/repos/owner/repo/issues/2") == {"state": "open"}
                assert len(sent) == 2
                assert [item.provenance for item in observations] == [RequestProvenance.NETWORK, RequestProvenance.NETWORK, RequestProvenance.LOCAL_CACHE]
                assert sum(item.context.attempt_id == outcome.context.attempt_id for item in observations) == 1
    finally:
        configure_github_request_boundary()
        governor.close()


@pytest.mark.parametrize("adapter", ["ghapi", "direct"])
def test_cache_failure_before_wire_creates_no_receipt(tmp_path, monkeypatch, adapter):
    monkeypatch.chdir(tmp_path)
    governor = GitHubRequestGovernor(store_path=tmp_path / "governor.sqlite3")
    observations = []
    failure = sqlite3.OperationalError("local pre-send storage failure")

    def fail_before_transport(_self, _request):
        raise failure

    monkeypatch.setattr("auto_coder.util.gh_cache._GitHubCacheTransport.handle_request", fail_before_transport)
    configure_github_request_boundary(governor.admit, observations.append)
    try:
        with get_caching_client(admission_hook=governor.admit, observation_hook=observations.append) as client:
            monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args: client)
            api = get_ghapi_client("fixture-token", admission_hook=governor.admit, observation_hook=observations.append)
            with pytest.raises(sqlite3.OperationalError) as caught:
                if adapter == "ghapi":
                    api("/repos/owner/repo/issues/1")
                else:
                    _caching_request(client, "GET", "https://api.github.com/repos/owner/repo/issues/1", path_template="/repos/owner/repo/issues/{id}")
            assert caught.value is failure
            assert observations == []
            assert take_wire_outcomes() == []
            with sqlite3.connect(tmp_path / "governor.sqlite3") as connection:
                assert connection.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    finally:
        configure_github_request_boundary()
        governor.close()


@pytest.mark.parametrize("adapter", ["ghapi", "direct"])
def test_cancellation_after_recorded_response_preserves_original_failure(tmp_path, monkeypatch, adapter):
    monkeypatch.chdir(tmp_path)
    observations = []
    sent = []
    failure = asyncio.CancelledError("cancelled after wire response")

    def respond(_transport, request):
        sent.append(request.url.path)
        return httpx.Response(200, json={"state": "open"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    configure_github_request_boundary(lambda _context: True, observations.append)
    try:
        with get_caching_client(admission_hook=lambda _context: True, observation_hook=observations.append) as client:
            original_request = client.request

            def cancel_after_response(*args, **kwargs):
                original_request(*args, **kwargs).close()
                raise failure

            monkeypatch.setattr(client, "request", cancel_after_response)
            monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda *args: client)
            api = get_ghapi_client("fixture-token", admission_hook=lambda _context: True, observation_hook=observations.append)
            with pytest.raises(asyncio.CancelledError) as caught:
                if adapter == "ghapi":
                    api("/repos/owner/repo/issues/1")
                else:
                    _caching_request(client, "GET", "https://api.github.com/repos/owner/repo/issues/1", path_template="/repos/owner/repo/issues/{id}")
            assert caught.value is failure
            assert sent == ["/repos/owner/repo/issues/1"]
            assert len(observations) == 1
            assert observations[0].delivery is DeliveryCertainty.HTTP_RESPONSE_RECEIVED
            assert take_wire_outcomes() == []
    finally:
        configure_github_request_boundary()
