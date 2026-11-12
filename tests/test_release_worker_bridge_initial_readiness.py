"""Unit regressions for the initial signed release bridge observation."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, WorkerNetworkResult
from tests import test_release_worker_acceptance_container as acceptance


def _response(code: str, *, alive: bool | None = None, worker: object = None) -> WorkerNetworkResult:
    return WorkerNetworkResult(
        http_status=200 if code == "OK" else 503,
        result_code=code,
        data={"alive": alive, "worker": worker} if code == "OK" else None,
        supervisor_generation="fixed-generation",
        request_id="fixed-request",
    )


def _poll(predicate: Callable[[], WorkerNetworkResult | None], *, timeout: float) -> WorkerNetworkResult:
    assert timeout > 0
    for _ in range(3):
        value = predicate()
        if value is not None:
            return value
    raise AssertionError("bounded observation expired")


def _client(monkeypatch: pytest.MonkeyPatch, responses: list[WorkerNetworkResult]) -> WorkerNetworkClient:
    client = WorkerNetworkClient(WorkerEndpointConfig("release-worker", "release", "owner/repo", "10.1.2.3", 8843, b"x" * 48))
    result = iter(responses)
    monkeypatch.setattr(client, "inspect_supervisor", lambda: next(result))
    monkeypatch.setattr(acceptance, "_wait_for", _poll)
    return client


def test_initial_release_observation_retries_unknown_before_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    accepted = _response("OK", alive=False)
    client = _client(monkeypatch, [_response("UNKNOWN"), accepted])
    assert acceptance._await_initial_stopped_supervisor(client) is accepted


@pytest.mark.parametrize(
    "responses",
    [
        [_response("UNKNOWN")] * 3,
        [_response("OK", alive=True)] * 3,
        [_response("OK", alive=False, worker={"process_run_id": "live"})] * 3,
    ],
)
def test_initial_release_observation_never_converts_unproven_state_to_stopped(monkeypatch: pytest.MonkeyPatch, responses: list[WorkerNetworkResult]) -> None:
    client = _client(monkeypatch, responses)
    with pytest.raises(AssertionError, match="initial release Supervisor did not prove stopped"):
        acceptance._await_initial_stopped_supervisor(client)
