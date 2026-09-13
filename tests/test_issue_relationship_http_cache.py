"""Issue admission reuses valid HTTP evidence without repeating wire reads."""

from collections import Counter
from unittest.mock import patch

import httpx
import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.util.gh_cache import GitHubClient, get_caching_client
from auto_coder.util.github_request_outcome import RequestProvenance, configure_github_request_boundary


@pytest.mark.parametrize("cache_control,expected", [("private, max-age=3600", 1), ("private, max-age=0", 2), ("no-store", 2)])
@pytest.mark.parametrize("relation", ["", "parent", "sub_issues", "dependencies/blocked_by", "dependencies/blocking"])
def test_relationship_reads_reuse_only_fresh_http_evidence(tmp_path, monkeypatch, cache_control, expected, relation):
    monkeypatch.chdir(tmp_path)
    sent = []
    payload = {"number": 10, "id": 100, "body": "current"}
    paginated = relation not in ("", "parent")

    def respond(_transport, request):
        sent.append(request)
        return httpx.Response(200, headers={"Cache-Control": cache_control}, json=[payload] if paginated else payload)

    with patch.object(httpx.HTTPTransport, "handle_request", respond), get_caching_client(admission_hook=lambda _: True) as cached:
        with patch("auto_coder.util.gh_cache.get_caching_client", return_value=cached), patch("auto_coder.util.gh_cache.boundary_hooks", return_value=(None, None)):
            github = GitHubClient("test-token")
            for _ in range(2):
                assert github._read_issue_resource("o/r", 10, relation, paginated=paginated) == ([payload] if paginated else payload)
    assert len(sent) == expected
    assert all("no-cache" not in request.headers.get("Cache-Control", "") for request in sent)


def test_explicit_preflight_and_repeated_family_checks_send_each_resource_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = Counter()
    snapshots = {
        10: {"number": 10, "id": 100, "state": "open", "body": ""},
        11: {"number": 11, "id": 110, "state": "open", "body": "Parent-Issue: #10"},
    }
    payloads = {
        "/repos/o/r/issues": list(snapshots.values()),
        "/repos/o/r/issues/10": snapshots[10],
        "/repos/o/r/issues/11": snapshots[11],
        "/repos/o/r/issues/10/sub_issues": [snapshots[11]],
        "/repos/o/r/issues/11/sub_issues": [],
        "/repos/o/r/issues/10/parent": None,
        "/repos/o/r/issues/11/parent": snapshots[10],
    }

    def respond(_transport, request):
        sent[request.url.path] += 1
        payload = payloads[request.url.path]
        return httpx.Response(404 if payload is None else 200, headers={"Cache-Control": "private, max-age=3600"}, json=payload)

    with patch.object(httpx.HTTPTransport, "handle_request", respond), get_caching_client(admission_hook=lambda _: True) as cached:
        with patch("auto_coder.util.gh_cache.get_caching_client", return_value=cached), patch("auto_coder.util.gh_cache.boundary_hooks", return_value=(None, None)):
            engine = AutomationEngine(GitHubClient("test-token"), AutomationConfig())
            assert engine._preflight_explicit_issue_relationships("o/r", 11, refresh_all=True) == snapshots[11]
            for _ in range(2):
                assert engine._fetch_authoritative_decomposition_set("o/r", 10) == (snapshots[10], [snapshots[11]])
    assert sent == Counter({path: 1 for path in payloads})


def test_relationship_mutation_revalidates_cached_absence_and_all_pages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    linked = False
    sent = []
    children = [{"number": number, "id": number * 10} for number in range(11, 112)]

    def respond(_transport, request):
        nonlocal linked
        sent.append((request.method, request.url.path, request.url.params.get("page"), request.headers.get("Cache-Control")))
        if request.method == "POST":
            linked = True
            return httpx.Response(201, json={})
        if request.url.path.endswith("/parent"):
            return httpx.Response(200 if linked else 404, headers={"Cache-Control": "private, max-age=3600"}, json={"number": 10} if linked else None)
        page = int(request.url.params.get("page", "1"))
        payload = children[(page - 1) * 100 : page * 100] if linked else []
        return httpx.Response(200, headers={"Cache-Control": "private, max-age=3600"}, json=payload)

    with patch.object(httpx.HTTPTransport, "handle_request", respond), get_caching_client(admission_hook=lambda _: True) as cached:
        with patch("auto_coder.util.gh_cache.get_caching_client", return_value=cached), patch("auto_coder.util.gh_cache.boundary_hooks", return_value=(None, None)):
            github = GitHubClient("test-token")
            assert github.get_parent_issue_details_strict("o/r", 11) is None
            assert github.get_direct_sub_issues_strict("o/r", 10) == []
            github.add_sub_issue_strict("o/r", 10, 11, 110)
            for _ in range(2):
                assert github.get_parent_issue_details_strict("o/r", 11) == {"number": 10}, sent
                assert github.get_direct_sub_issues_strict("o/r", 10) == children
    assert sent == [
        ("GET", "/repos/o/r/issues/11/parent", None, None),
        ("GET", "/repos/o/r/issues/10/sub_issues", "1", None),
        ("POST", "/repos/o/r/issues/10/sub_issues", None, None),
        ("GET", "/repos/o/r/issues/11/parent", None, "no-cache"),
        ("GET", "/repos/o/r/issues/10/sub_issues", "1", "no-cache"),
        ("GET", "/repos/o/r/issues/10/sub_issues", "2", "no-cache"),
    ]


def test_expired_issue_uses_etag_revalidation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = []

    def respond(_transport, request):
        sent.append(request.headers.get("If-None-Match"))
        if len(sent) == 1:
            return httpx.Response(200, headers={"Cache-Control": "private, max-age=0", "ETag": '"revision-1"'}, json={"number": 10})
        return httpx.Response(304, headers={"Cache-Control": "private, max-age=3600", "ETag": '"revision-1"'})

    with patch.object(httpx.HTTPTransport, "handle_request", respond), get_caching_client(admission_hook=lambda _: True) as cached:
        with patch("auto_coder.util.gh_cache.get_caching_client", return_value=cached), patch("auto_coder.util.gh_cache.boundary_hooks", return_value=(None, None)):
            github = GitHubClient("test-token")
            for _ in range(3):
                assert github.get_issue_dispatch_snapshot_strict("o/r", 10) == {"number": 10}
    assert sent == [None, '"revision-1"']


def test_issue_cache_hit_records_local_provenance_without_admission(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    admitted = []
    observed = []
    configure_github_request_boundary(lambda context: admitted.append(context) or True, observed.append)
    try:
        with patch.object(httpx.HTTPTransport, "handle_request", lambda _transport, request: httpx.Response(200, headers={"Cache-Control": "private, max-age=3600"}, json={"number": 10})):
            github = GitHubClient("test-token")
            for _ in range(2):
                assert github.get_issue_dispatch_snapshot_strict("o/r", 10) == {"number": 10}
        assert len(admitted) == 1
        assert observed[-1].provenance is RequestProvenance.LOCAL_CACHE
    finally:
        configure_github_request_boundary(None, None)


def test_expired_relationship_failure_never_uses_stale_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = []

    def respond(_transport, request):
        sent.append(request.url.path)
        return httpx.Response(200 if len(sent) == 1 else 503, headers={"Cache-Control": "private, max-age=0"}, json=[{"number": 11}])

    with patch.object(httpx.HTTPTransport, "handle_request", respond), get_caching_client(admission_hook=lambda _: True) as cached:
        with patch("auto_coder.util.gh_cache.get_caching_client", return_value=cached), patch("auto_coder.util.gh_cache.boundary_hooks", return_value=(None, None)):
            github = GitHubClient("test-token")
            assert github.get_direct_sub_issues_strict("o/r", 10) == [{"number": 11}]
            with pytest.raises(httpx.HTTPStatusError):
                github.get_direct_sub_issues_strict("o/r", 10)
    assert sent == ["/repos/o/r/issues/10/sub_issues"] * 2
