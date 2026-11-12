"""Release fixture readiness against actual signed Supervisor HTTP transports."""

from __future__ import annotations

import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, WorkerNetworkResult
from tests.test_release_worker_acceptance_container import _await_initial_stopped_supervisor
from tests.test_worker_supervisor_bridge import _bridge, _supervisor


class _ObservedClient(WorkerNetworkClient):
    """Record real responses without replacing transport or authentication."""

    def __init__(self, configuration: WorkerEndpointConfig) -> None:
        super().__init__(configuration)
        self.unavailable = threading.Event()
        self.responses: list[WorkerNetworkResult] = []

    def inspect_supervisor(self) -> WorkerNetworkResult:
        response = super().inspect_supervisor()
        self.responses.append(response)
        if response.result_code != "OK":
            self.unavailable.set()
        return response


def test_release_fixture_waits_for_actual_signed_bridge_start(tmp_path: Path, _use_real_sleep: None) -> None:
    supervisor = _supervisor(tmp_path, "release")
    key = os.urandom(48)
    # A bound but non-listening TCP socket keeps the port reserved while actual
    # connection attempts fail. No successful response exists before release.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        configuration = supervisor.config
        client = _ObservedClient(WorkerEndpointConfig(configuration.instance_id, configuration.channel, configuration.repository, "127.0.0.1", port, key))
        with ThreadPoolExecutor(max_workers=1) as executor:
            waiting = executor.submit(_await_initial_stopped_supervisor, client, timeout=3)
            assert client.unavailable.wait(2), "the fixture must observe actual unavailability before bridge startup"
            assert not waiting.done(), "a transient unavailable inspection must not complete fixture readiness"
            assert client.responses[0].http_status == 0 and client.responses[0].result_code == "UNKNOWN"
            reserved.close()
            bridge, _ = _bridge(supervisor, key, port=port)
            try:
                result = waiting.result(timeout=4)
                assert result.result_code == "OK" and result.data is not None and result.data["alive"] is False
                assert result.supervisor_generation == supervisor.incarnation == client.supervisor_generation
                assert client.responses[-1] == result
                assert not supervisor.store.path.exists(), "readiness must not create a worker or operation journal"
            finally:
                bridge.close()


@pytest.mark.parametrize("invalid_signature", [False, True], ids=["unavailable", "invalid-signature"])
def test_release_fixture_refuses_a_transport_without_authenticated_readiness(tmp_path: Path, _use_real_sleep: None, invalid_signature: bool) -> None:
    supervisor = _supervisor(tmp_path, "release")
    key = os.urandom(48)
    bridge = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        if invalid_signature:
            reserved.close()
            bridge, _ = _bridge(supervisor, os.urandom(48), port=port)
        configuration = supervisor.config
        client = _ObservedClient(WorkerEndpointConfig(configuration.instance_id, configuration.channel, configuration.repository, "127.0.0.1", port, key))
        try:
            with pytest.raises(AssertionError, match="initial release Supervisor did not prove stopped: .*UNKNOWN"):
                _await_initial_stopped_supervisor(client, timeout=0.25)
            assert client.responses and all(response.result_code == "UNKNOWN" for response in client.responses)
            assert {response.http_status for response in client.responses} == ({401} if invalid_signature else {0})
            assert client.supervisor_generation is None
            assert not supervisor.store.path.exists()
        finally:
            if bridge is not None:
                bridge.close()
