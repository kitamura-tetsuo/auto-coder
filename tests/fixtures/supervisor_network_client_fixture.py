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


def _auth_attack_receipts(client: WorkerNetworkClient) -> dict[str, object]:
    nonce, request_id, timestamp = uuid.uuid4().hex, uuid.uuid4().hex, str(int(time.time()))
    generation = client.supervisor_generation
    assert generation is not None
    path = "/v1/supervisor"
    valid = request_mac(client.config.secret, method="GET", target=path, instance=client.config.instance_id, generation=generation, timestamp=timestamp, nonce=nonce, request_id=request_id, body=b"")
    headers = {
        "X-AC-Version": "1",
        "X-AC-Instance": client.config.instance_id,
        "X-AC-Supervisor-Generation": generation,
        "X-AC-Timestamp": timestamp,
        "X-AC-Nonce": nonce,
        "X-AC-Request-Id": request_id,
        "X-AC-Signature": valid,
    }

    def request(selected: dict[str, str]) -> int:
        connection = http.client.HTTPConnection(client.config.address, client.config.port, timeout=5)
        try:
            connection.request("GET", path, headers=selected)
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    result = [request(headers), request(headers)]
    invalid_key = dict(headers, **{"X-AC-Nonce": uuid.uuid4().hex, "X-AC-Signature": "0" * 64})
    result.append(request(invalid_key))
    expired = dict(headers, **{"X-AC-Nonce": uuid.uuid4().hex, "X-AC-Timestamp": str(int(time.time()) - 1000)})
    expired["X-AC-Signature"] = request_mac(client.config.secret, method="GET", target=path, instance=client.config.instance_id, generation=generation, timestamp=expired["X-AC-Timestamp"], nonce=expired["X-AC-Nonce"], request_id=request_id, body=b"")
    result.append(request(expired))
    return {"statuses": result}


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
    elif command == "auth-attacks":
        print(json.dumps(_auth_attack_receipts(client), sort_keys=True))
        return
    elif command == "observe":
        result = client.observe_worker("fixture-request-" + uuid.uuid4().hex)
    elif command == "drop-stop":
        _drop_stop_response(client, sys.argv[3])
        print(json.dumps({"dispatched_id": sys.argv[3]}))
        return
    elif command == "read-operation":
        result = client.read_worker_operation(sys.argv[3])
    elif command == "prepare-version":
        result = client.prepare_version(sys.argv[3], sys.argv[4])
    elif command == "read-preparation":
        result = client.read_preparation(sys.argv[3])
    elif command == "start-staged":
        if inspected.result_code != "OK" or inspected.data is None:
            raise RuntimeError("Supervisor inspection is not authenticated")
        result = client.dispatch_worker_operation(
            sys.argv[3],
            "START",
            process_run_id=None,
            deployment_generation=None,
            selected_runtime_id=inspected.data.get("selected_runtime_id"),
            runtime_id=sys.argv[4],
        )
    elif command == "stale-observation":
        client.worker_process_run_id, client.worker_deployment_generation = sys.argv[3:5]
        result = client.observe_worker()
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
