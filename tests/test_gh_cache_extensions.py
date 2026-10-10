"""Request-extension preservation across the real HTTPX/Hishel cache bridge."""

from copy import deepcopy

import httpx
import pytest
from hishel import CacheOptions, SpecificationPolicy, SyncSqliteStorage

from auto_coder.util.gh_cache import _cache_request_extensions, _GitHubCacheTransport


@pytest.mark.parametrize(
    "selected_extensions",
    [
        {"timeout": {"connect": 1.0, "read": 2.0, "write": 3.0, "pool": 4.0}, "auto_coder_operation_id": "both-entries"},
        {"timeout": {"connect": 5.0, "read": None, "write": 7.0, "pool": 8.0}},
        {"auto_coder_operation_id": "identity-only"},
        {},
        {"timeout": None, "auto_coder_operation_id": ""},
    ],
    ids=["both", "timeout-only", "identity-only", "neither", "present-falsey-values"],
)
def test_cache_bridge_preserves_only_present_selected_extensions(tmp_path, selected_extensions):
    incoming = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues", extensions={**deepcopy(selected_extensions), "unrelated_extension": {"keep": ["original"]}})
    original_extensions = deepcopy(incoming.extensions)
    previous_context = deepcopy(_cache_request_extensions.get())
    wire_extensions = []
    delegated_contexts = []

    def respond(request):
        assert request is not incoming
        wire_extensions.append(dict(request.extensions))
        delegated_contexts.append(dict(_cache_request_extensions.get()))
        return httpx.Response(200, headers={"Cache-Control": "no-store"}, content=b"ok")

    storage = SyncSqliteStorage(database_path=str(tmp_path / "cache.db"))
    with _GitHubCacheTransport(next_transport=httpx.MockTransport(respond), storage=storage, policy=SpecificationPolicy(cache_options=CacheOptions(shared=False))) as transport:
        # Enter the production transport directly: HTTPX Client would add its own
        # default timeout before the bridge, hiding the absent-timeout scenario.
        response = transport.handle_request(incoming)
        assert response.read() == b"ok"
        response.close()

    assert delegated_contexts == [selected_extensions]
    assert len(wire_extensions) == 1
    forwarded = wire_extensions[0]
    for key, value in selected_extensions.items():
        assert forwarded[key] == value
    for absent_key in {"timeout", "auto_coder_operation_id"} - selected_extensions.keys():
        assert absent_key not in forwarded
    assert "unrelated_extension" not in forwarded
    assert incoming.extensions == original_extensions
    assert _cache_request_extensions.get() == previous_context


@pytest.mark.parametrize("first_request_fails", [False, True], ids=["success", "wire-error"])
def test_cache_bridge_restores_outer_context_and_isolates_next_request(tmp_path, first_request_fails):
    outer_context = {"timeout": {"read": 91.0}, "auto_coder_operation_id": "outer-operation", "outer_only": "preserved"}
    original_outer_context = deepcopy(outer_context)
    first_extensions = {"timeout": {"connect": 1.0, "read": 2.0, "write": 3.0, "pool": 4.0}, "auto_coder_operation_id": "first-operation"}
    first_request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/1", extensions=deepcopy(first_extensions))
    second_request = httpx.Request("GET", "https://api.github.com/repos/owner/repo/issues/2", extensions={"auto_coder_operation_id": "second-operation"})
    sent = []
    delegated_contexts = []

    def respond(request):
        sent.append(dict(request.extensions))
        delegated_contexts.append(dict(_cache_request_extensions.get()))
        if first_request_fails and len(sent) == 1:
            raise httpx.ReadTimeout("controlled wire timeout", request=request)
        return httpx.Response(200, headers={"Cache-Control": "no-store"}, content=b"ok")

    storage = SyncSqliteStorage(database_path=str(tmp_path / "cache.db"))
    outer_token = _cache_request_extensions.set(outer_context)
    try:
        with _GitHubCacheTransport(next_transport=httpx.MockTransport(respond), storage=storage, policy=SpecificationPolicy(cache_options=CacheOptions(shared=False))) as transport:
            if first_request_fails:
                with pytest.raises(httpx.ReadTimeout, match="controlled wire timeout"):
                    transport.handle_request(first_request)
            else:
                response = transport.handle_request(first_request)
                assert response.read() == b"ok"
                response.close()
            assert _cache_request_extensions.get() == original_outer_context
            response = transport.handle_request(second_request)
            assert response.read() == b"ok"
            response.close()
            assert _cache_request_extensions.get() == original_outer_context
    finally:
        _cache_request_extensions.reset(outer_token)

    assert delegated_contexts == [first_extensions, {"auto_coder_operation_id": "second-operation"}]
    assert len(sent) == 2
    assert sent[0]["timeout"] == first_extensions["timeout"]
    assert sent[0]["auto_coder_operation_id"] == "first-operation"
    assert sent[1]["auto_coder_operation_id"] == "second-operation"
    assert "timeout" not in sent[1]
    assert all("outer_only" not in metadata for metadata in sent)
    assert first_request.extensions == first_extensions
    assert second_request.extensions == {"auto_coder_operation_id": "second-operation"}
    assert outer_context == original_outer_context
