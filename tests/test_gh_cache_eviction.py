"""Tests for GitHub HTTP cache eviction and Cache-Control handling."""

import hashlib
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from hishel import CacheOptions, Headers
from hishel import Request as CacheRequest
from hishel import Response as CacheResponse
from hishel import SpecificationPolicy, SyncSqliteStorage
from hishel.httpx import SyncCacheClient

from src.auto_coder.github_ci_observer import observe_ci
from src.auto_coder.util.gh_cache import (
    _GitHubCacheTransport,
    evict_github_cache_by_pattern,
    evict_github_ci_cache,
    evict_github_entity_cache,
    get_ghapi_client,
)
from src.auto_coder.webhook_server import create_app


def test_evict_github_cache_by_pattern_empty_and_missing(tmp_path: Path) -> None:
    non_existent = str(tmp_path / "non_existent.db")
    assert evict_github_cache_by_pattern("", db_path=non_existent) == 0
    assert evict_github_cache_by_pattern("some-sha", db_path=non_existent) == 0


def test_evict_github_cache_by_pattern_matches_and_removes(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cache.db")
    storage = SyncSqliteStorage(database_path=db_path)

    req1 = CacheRequest(method=b"GET", url=b"https://api.github.com/repos/owner/repo/pulls/42", headers=Headers({"host": "api.github.com"}))
    res1 = CacheResponse(status_code=200, headers=Headers({"cache-control": "public, max-age=3600"}), stream=[b"ok"])
    storage.create_entry(req1, res1, key="key1")

    req2 = CacheRequest(method=b"GET", url=b"https://api.github.com/repos/owner/repo/pulls/99", headers=Headers({"host": "api.github.com"}))
    res2 = CacheResponse(status_code=200, headers=Headers({"cache-control": "public, max-age=3600"}), stream=[b"ok"])
    storage.create_entry(req2, res2, key="key2")

    assert evict_github_cache_by_pattern("owner/repo/pulls/42", db_path=db_path) == 1
    assert evict_github_cache_by_pattern("owner/repo/pulls/42", db_path=db_path) == 0


def test_evict_github_ci_cache(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cache.db")
    storage = SyncSqliteStorage(database_path=db_path)

    req1 = CacheRequest(method=b"GET", url=b"https://api.github.com/repos/owner/repo/commits/abc1234def/check-runs", headers=Headers({"host": "api.github.com"}))
    res1 = CacheResponse(status_code=200, headers=Headers({"cache-control": "public, max-age=3600"}), stream=[b"ok"])
    storage.create_entry(req1, res1, key="key1")

    assert evict_github_ci_cache("", "owner/repo", db_path=db_path) == 0
    assert evict_github_ci_cache("abc1234def", "owner/repo", db_path=db_path) == 1
    assert evict_github_ci_cache("abc1234def", "owner/repo", db_path=db_path) == 0


def test_evict_github_entity_cache_for_pr_and_issue(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cache.db")
    storage = SyncSqliteStorage(database_path=db_path)

    # A PR has both /pulls/10 and /issues/10 cached in various operations
    req_pull = CacheRequest(method=b"GET", url=b"https://api.github.com/repos/owner/repo/pulls/10", headers=Headers({"host": "api.github.com"}))
    res_pull = CacheResponse(status_code=200, headers=Headers({"cache-control": "public, max-age=3600"}), stream=[b"ok"])
    storage.create_entry(req_pull, res_pull, key="key_pull")

    req_issue = CacheRequest(method=b"GET", url=b"https://api.github.com/repos/owner/repo/issues/10", headers=Headers({"host": "api.github.com"}))
    res_issue = CacheResponse(status_code=200, headers=Headers({"cache-control": "public, max-age=3600"}), stream=[b"ok"])
    storage.create_entry(req_issue, res_issue, key="key_issue")

    req_other = CacheRequest(method=b"GET", url=b"https://api.github.com/repos/owner/repo/pulls/20", headers=Headers({"host": "api.github.com"}))
    res_other = CacheResponse(status_code=200, headers=Headers({"cache-control": "public, max-age=3600"}), stream=[b"ok"])
    storage.create_entry(req_other, res_other, key="key_other")

    # Empty inputs return 0
    assert evict_github_entity_cache("", "pr", 10, db_path=db_path) == 0
    assert evict_github_entity_cache("owner/repo", "pr", 0, db_path=db_path) == 0

    # Evicting PR 10 should evict both /pulls/10 and /issues/10
    evicted = evict_github_entity_cache("owner/repo", "pr", 10, db_path=db_path)
    assert evicted == 2

    # PR 20 should still be untouched
    assert evict_github_cache_by_pattern("owner/repo/pulls/20", db_path=db_path) == 1


def test_github_cache_transport_no_cache_header_evicts_entry(tmp_path: Path) -> None:
    db_path = str(tmp_path / "cache.db")
    storage = SyncSqliteStorage(database_path=db_path)

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, headers={"cache-control": "public, max-age=3600"}, text=f'{{"call": {call_count}}}')

    mock_transport = httpx.MockTransport(handler)
    gh_transport = _GitHubCacheTransport(
        next_transport=mock_transport,
        storage=storage,
        policy=SpecificationPolicy(cache_options=CacheOptions(shared=False)),
    )
    client = SyncCacheClient(storage=storage, transport=gh_transport)

    url = "https://api.github.com/repos/owner/repo/pulls/1"
    # First call - populates cache
    res1 = client.get(url)
    assert res1.status_code == 200
    assert call_count == 1

    # Second call without no-cache - served from cache
    res2 = client.get(url)
    assert res2.status_code == 200
    assert call_count == 1

    # Third call with Cache-Control: no-cache - evicts old entry and hits transport
    res3 = client.get(url, headers={"Cache-Control": "no-cache"})
    assert res3.status_code == 200
    assert call_count == 2


def test_get_ghapi_client_extra_headers() -> None:
    api = get_ghapi_client("fake_token", extra_headers={"Cache-Control": "no-cache"})
    assert hasattr(api, "headers")
    assert api.headers.get("Cache-Control") == "no-cache"


def test_observe_ci_sets_no_cache_header() -> None:
    mock_api = MagicMock()
    mock_api.headers = {}
    mock_api.checks.list_for_ref.return_value = {"check_runs": []}
    mock_api.actions.list_workflow_runs_for_repo.return_value = {"workflow_runs": []}

    observe_ci(mock_api, "fake_token", "owner/repo", 1, "head_sha")
    assert mock_api.headers.get("Cache-Control") == "no-cache"


def test_webhook_server_ci_webhook_evicts_cache(tmp_path: Path) -> None:
    from src.auto_coder.entity_invalidation import DurableInvalidationQueue

    class DummyEngine:
        def __init__(self, path: Path) -> None:
            self.invalidations = DurableInvalidationQueue(path)
            self._invalidation_wake_event = None

    engine = DummyEngine(tmp_path / "events.sqlite3")
    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")

    with patch("src.auto_coder.webhook_server.evict_github_ci_cache") as mock_ci_evict, patch("src.auto_coder.webhook_server.evict_github_entity_cache") as mock_entity_evict:
        with TestClient(app) as client:
            response = client.post(
                "/hooks/github",
                json={
                    "action": "completed",
                    "workflow_run": {"head_sha": "abc1234", "id": 8, "workflow_id": 4, "run_attempt": 1, "pull_requests": [{"number": 42}]},
                    "repository": {"full_name": "owner/repo"},
                },
                headers={"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": "uuid-test-ci-evict"},
            )
            assert response.status_code == 200

        mock_ci_evict.assert_called_once_with("abc1234", "owner/repo")
        mock_entity_evict.assert_called_once_with("owner/repo", "pr", 42)


def test_webhook_server_entity_webhook_evicts_cache(tmp_path: Path) -> None:
    class DummyEngine:
        async def invalidate_entity(self, *args, **kwargs) -> bool:
            return True

    engine = DummyEngine()
    with patch("src.auto_coder.webhook_server.init_dashboard"):
        app = create_app(engine, "owner/repo")

    with patch("src.auto_coder.webhook_server.evict_github_entity_cache") as mock_entity_evict:
        with TestClient(app) as client:
            response = client.post(
                "/hooks/github",
                json={
                    "action": "labeled",
                    "pull_request": {"number": 105},
                    "repository": {"full_name": "owner/repo"},
                },
                headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "uuid-test-pr-evict"},
            )
            assert response.status_code == 200

        mock_entity_evict.assert_called_once_with("owner/repo", "pr", 105)
