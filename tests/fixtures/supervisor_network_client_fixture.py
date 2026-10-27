"""Network-side client executed in the isolated unprivileged manager container."""

from __future__ import annotations

import http.client
import json
import socket
import sys
import time
import uuid
from dataclasses import asdict, replace

from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient
from auto_coder.worker_network_protocol import request_mac


def _observation(client: WorkerNetworkClient) -> dict[str, object]:
    result = client.observe_worker()
    if result.result_code != "OK" or result.data is None:
        raise RuntimeError("actual worker observation is unavailable")
    observed = result.data["observation"]
    if not isinstance(observed, dict):
        raise RuntimeError("invalid real observation")
    return observed


def _drop_stop_response(client: WorkerNetworkClient, operation_id: str) -> None:
    observed = _observation(client)
    body = json.dumps(
        {
            "operation_id": operation_id,
            "operation": "REQUEST_GRACEFUL_STOP",
            "expected_process_run_id": observed["process_run_id"],
            "expected_deployment_generation": observed["deployment_generation"],
            "expected_runtime_id": "bootstrap",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    target, generation = "/v1/worker-operations", client.supervisor_generation
    assert generation is not None
    nonce, request_id, timestamp = uuid.uuid4().hex, uuid.uuid4().hex, str(int(time.time()))
    fields = {
        "X-AC-Version": "1",
        "X-AC-Instance": client.config.instance_id,
        "X-AC-Supervisor-Generation": generation,
        "X-AC-Timestamp": timestamp,
        "X-AC-Nonce": nonce,
        "X-AC-Request-Id": request_id,
        "X-AC-Signature": request_mac(client.config.secret, method="POST", target=target, instance=client.config.instance_id, generation=generation, timestamp=timestamp, nonce=nonce, request_id=request_id, body=body),
        "Content-Type": "application/json",
    }
    connection = http.client.HTTPConnection(client.config.address, client.config.port, timeout=5)
    connection.connect()
    try:
        connection.putrequest("POST", target)
        for key, value in fields.items():
            connection.putheader(key, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        assert connection.sock is not None
        connection.sock.shutdown(socket.SHUT_WR)
    finally:
        connection.close()


def main() -> None:
    command, channel = sys.argv[1:3]
    config = WorkerEndpointConfig.environment(channel)
    if command == "swapped-target":
        peer = WorkerEndpointConfig.environment("release" if channel == "beta" else "beta")
        config = replace(config, address=peer.address)
    client = WorkerNetworkClient(config)
    inspected = client.inspect_supervisor()
    if command == "inspect" or command == "swapped-target":
        result = inspected
    elif command == "observe":
        result = client.observe_worker("fixture-request-" + uuid.uuid4().hex)
    elif command == "drop-stop":
        _drop_stop_response(client, sys.argv[3])
        print(json.dumps({"dispatched_id": sys.argv[3]}))
        return
    elif command == "read-operation":
        result = client.read_worker_operation(sys.argv[3])
    elif command == "stale-stop":
        observed = _observation(client)
        client.supervisor_generation = sys.argv[3]
        result = client.dispatch_worker_operation(
            "stale-attempt-" + uuid.uuid4().hex,
            "REQUEST_GRACEFUL_STOP",
            process_run_id=str(observed["process_run_id"]),
            deployment_generation=str(observed["deployment_generation"]),
            selected_runtime_id="bootstrap",
        )
    else:
        raise ValueError("unsupported fixed integration command")
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
