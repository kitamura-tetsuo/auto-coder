"""Fixed-target, machine-authenticated private Supervisor network client.

This is a lower-level transport for the durable management dispatcher. OAuth
credentials are never accepted here or forwarded to the worker container.
"""

from __future__ import annotations

import hmac
import http.client
import ipaddress
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from auto_coder.worker_network_protocol import (
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    load_machine_secret,
    request_mac,
    response_mac,
)

_BRIDGE_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")


@dataclass(frozen=True)
class WorkerEndpointConfig:
    instance_id: str
    channel: str
    repository: str
    address: str
    port: int
    secret: bytes

    @classmethod
    def environment(cls, channel: str) -> "WorkerEndpointConfig":
        """Use only trusted manager deployment variables, never MCP arguments."""
        if channel not in {"beta", "release"}:
            raise ValueError("unsupported worker channel")
        prefix = f"AUTO_CODER_{channel.upper()}_"
        required = ("SUPERVISOR_INSTANCE_ID", "REPOSITORY", "BRIDGE_ADDRESS", "BRIDGE_PORT", "BRIDGE_SECRET_FILE")
        values = {name: os.environ.get(prefix + name, "") for name in required}
        if not all(values.values()) or not values["BRIDGE_PORT"].isdecimal():
            raise ValueError("worker network endpoint is not fully configured")
        address = ipaddress.ip_address(values["BRIDGE_ADDRESS"])
        port = int(values["BRIDGE_PORT"])
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or not 1024 <= port <= 65535:
            raise ValueError("worker network endpoint must be a fixed private Docker IPv4 address and port")
        if "/" not in values["REPOSITORY"] or not _BRIDGE_IDENTITY.fullmatch(values["REPOSITORY"]) or not _BRIDGE_IDENTITY.fullmatch(values["SUPERVISOR_INSTANCE_ID"]):
            raise ValueError("worker network identity is invalid")
        return cls(values["SUPERVISOR_INSTANCE_ID"], channel, values["REPOSITORY"], str(address), port, load_machine_secret(Path(values["BRIDGE_SECRET_FILE"])))


_ENDPOINT_FIELDS = ("SUPERVISOR_INSTANCE_ID", "REPOSITORY", "BRIDGE_ADDRESS", "BRIDGE_PORT", "BRIDGE_SECRET_FILE")


def configured_worker_bridge_environment() -> dict[str, str]:
    """Capture one complete, operator-provisioned two-channel configuration."""
    names = (f"AUTO_CODER_{channel}_{field}" for channel in ("RELEASE", "BETA") for field in _ENDPOINT_FIELDS)
    present = {name: os.environ[name] for name in names if name in os.environ}
    if not present:
        return {}
    if len(present) != 2 * len(_ENDPOINT_FIELDS) or not all(present.values()):
        raise ValueError("both worker network channels require complete configuration")
    release, beta = (WorkerEndpointConfig.environment(channel) for channel in ("release", "beta"))
    if release.secret == beta.secret or (release.address, release.port) == (beta.address, beta.port) or release.instance_id == beta.instance_id:
        raise ValueError("worker network channels must have distinct credentials, endpoints and instances")
    return present


@dataclass(frozen=True)
class WorkerNetworkResult:
    http_status: int
    result_code: str
    data: dict[str, Any] | None
    supervisor_generation: str
    request_id: str


class WorkerNetworkClient:
    """Bounded typed client with signed response and incarnation correlation."""

    def __init__(self, config: WorkerEndpointConfig):
        self.config = config
        self.supervisor_generation: str | None = None
        self.worker_process_run_id: str | None = None
        self.worker_deployment_generation: str | None = None

    def inspect_supervisor(self) -> WorkerNetworkResult:
        result = self._call("GET", "/v1/supervisor", bootstrap=True)
        if result.result_code == "OK" and isinstance(result.data, dict) and result.data.get("supervisor_incarnation") == result.supervisor_generation:
            self.supervisor_generation = result.supervisor_generation
            worker = result.data.get("worker")
            if result.data.get("alive") is True and isinstance(worker, dict) and isinstance(worker.get("process_run_id"), str) and isinstance(worker.get("deployment_generation"), str):
                self.worker_process_run_id = worker["process_run_id"]
                self.worker_deployment_generation = worker["deployment_generation"]
            else:
                self.worker_process_run_id = self.worker_deployment_generation = None
        return result

    def observe_worker(self, nonce: str | None = None) -> WorkerNetworkResult:
        if not self._has_worker_identity():
            return self._unavailable_without_worker()
        expected = nonce or uuid.uuid4().hex
        result = self._call("GET", "/v1/worker-observation?" + urlencode({"nonce": expected}), worker_run=self.worker_process_run_id or "", worker_generation=self.worker_deployment_generation or "")
        if result.result_code == "OK":
            data = result.data
            observed = data.get("observation") if isinstance(data, dict) else None
            if not isinstance(observed, dict) or observed.get("nonce") != expected or observed.get("repository") != self.config.repository or observed.get("channel") != self.config.channel:
                return self._unknown(result)
            assert isinstance(data, dict)
            binding = data.get("runtime_binding")
            if binding is not None and (not isinstance(binding, dict) or binding.get("process_run_id") != observed.get("process_run_id") or binding.get("deployment_generation") != observed.get("deployment_generation") or data.get("binding_status") != "VERIFIED"):
                return self._unknown(result)
        return result

    def diagnostics(self, kind: str, params: dict[str, str] | None = None) -> WorkerNetworkResult:
        allowed = {
            "status": {"limit"},
            "logs": {"limit", "item_type", "item_number", "execution_id", "process_run_id", "after_sequence", "cursor"},
            "validation-attempts": {"pr_number", "attempt_id", "attempt_sequence"},
        }
        if kind not in allowed or (params and (not params.keys() <= allowed[kind] or any(not isinstance(value, str) for value in params.values()))):
            raise ValueError("unsupported diagnostic projection")
        if not self._has_worker_identity():
            return self._unavailable_without_worker()
        suffix = "?" + urlencode(params) if params else ""
        return self._call("GET", "/v1/diagnostics/" + kind + suffix, worker_run=self.worker_process_run_id or "", worker_generation=self.worker_deployment_generation or "")

    def accept_reevaluation(self, operation_id: str, *, process_run_id: str, deployment_generation: str, target_type: str, number: int) -> WorkerNetworkResult:
        body: dict[str, object] = {
            "operation_id": operation_id,
            "repository": self.config.repository,
            "channel": self.config.channel,
            "expected_process_run_id": process_run_id,
            "expected_deployment_generation": deployment_generation,
            "target": {"type": target_type, "number": number},
        }
        return self._call("POST", "/v1/reevaluations", body, worker_run=process_run_id, worker_generation=deployment_generation)

    def read_reevaluation(self, operation_id: str) -> WorkerNetworkResult:
        self._validate_reevaluation_id(operation_id)
        if not self._has_worker_identity():
            return self._unavailable_without_worker()
        return self._call("GET", "/v1/reevaluations/" + operation_id, worker_run=self.worker_process_run_id or "", worker_generation=self.worker_deployment_generation or "")

    def _has_worker_identity(self) -> bool:
        if self.worker_process_run_id is None or self.worker_deployment_generation is None:
            self.inspect_supervisor()
        return bool(self.worker_process_run_id and self.worker_deployment_generation)

    def _unavailable_without_worker(self) -> WorkerNetworkResult:
        return WorkerNetworkResult(503, "UNAVAILABLE", None, self.supervisor_generation or "", uuid.uuid4().hex)

    def prepare_version(self, operation_id: str, source_sha: str) -> WorkerNetworkResult:
        return self._call("POST", "/v1/worker-version-preparations", {"operation_id": operation_id, "source_sha": source_sha})

    def read_preparation(self, operation_id: str) -> WorkerNetworkResult:
        self._validate_operation_id(operation_id)
        return self._call("GET", "/v1/worker-version-preparations/" + operation_id)

    def dispatch_worker_operation(
        self,
        operation_id: str,
        operation: str,
        *,
        process_run_id: str | None,
        deployment_generation: str | None,
        selected_runtime_id: str | None,
        runtime_id: str | None = None,
    ) -> WorkerNetworkResult:
        body: dict[str, object] = {
            "operation_id": operation_id,
            "operation": operation,
            "expected_process_run_id": process_run_id,
            "expected_deployment_generation": deployment_generation,
            "expected_runtime_id": selected_runtime_id,
        }
        if runtime_id is not None:
            body["runtime_id"] = runtime_id
        return self._call("POST", "/v1/worker-operations", body)

    def read_worker_operation(self, operation_id: str) -> WorkerNetworkResult:
        self._validate_operation_id(operation_id)
        return self._call("GET", "/v1/worker-operations/" + operation_id)

    @staticmethod
    def _validate_operation_id(value: object) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value[0].isalnum() or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in value):
            raise ValueError("invalid operation ID")

    @staticmethod
    def _validate_reevaluation_id(value: object) -> None:
        # The existing UDS producer permits namespace separators and leading
        # punctuation; do not narrow its durable operation identity contract.
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value) is None:
            raise ValueError("invalid reevaluation operation ID")

    @staticmethod
    def _unknown(result: WorkerNetworkResult) -> WorkerNetworkResult:
        return WorkerNetworkResult(result.http_status, "UNKNOWN", None, result.supervisor_generation, result.request_id)

    def _call(self, method: str, target: str, document: dict[str, object] | None = None, *, bootstrap: bool = False, worker_run: str = "", worker_generation: str = "") -> WorkerNetworkResult:
        generation = "-" if bootstrap else self.supervisor_generation
        if generation is None:
            raise ValueError("read the signed Supervisor identity before making worker requests")
        if document is not None:
            if target == "/v1/reevaluations":
                self._validate_reevaluation_id(document.get("operation_id"))
            else:
                self._validate_operation_id(document.get("operation_id"))
        body = json.dumps(document, separators=(",", ":"), sort_keys=True, allow_nan=False).encode() if document is not None else b""
        if len(body) > 16 * 1024:
            raise ValueError("request exceeds size limit")
        request_id = uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time()))
        signature = request_mac(
            self.config.secret,
            method=method,
            target=target,
            instance=self.config.instance_id,
            generation=generation,
            timestamp=timestamp,
            nonce=nonce,
            request_id=request_id,
            body=body,
            worker_run=worker_run,
            worker_generation=worker_generation,
        )
        headers = {
            "X-AC-Version": PROTOCOL_VERSION,
            "X-AC-Instance": self.config.instance_id,
            "X-AC-Supervisor-Generation": generation,
            "X-AC-Timestamp": timestamp,
            "X-AC-Nonce": nonce,
            "X-AC-Request-Id": request_id,
            "X-AC-Signature": signature,
        }
        if document is not None:
            headers["Content-Type"] = "application/json"
        if worker_run or worker_generation:
            headers["X-AC-Worker-Process-Run-Id"] = worker_run
            headers["X-AC-Worker-Deployment-Generation"] = worker_generation
        connection = http.client.HTTPConnection(self.config.address, self.config.port, timeout=5)
        try:
            connection.request(method, target, body=body if document is not None else None, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES or response.status not in {200, 202, 400, 401, 404, 409, 413, 422, 503}:
                return WorkerNetworkResult(0, "UNKNOWN", None, generation, request_id)
            signature_response = response.getheader("X-AC-Response-Signature", "")
            expected = response_mac(self.config.secret, request_id=request_id, generation=generation if generation != "-" else self._parse_generation(raw), body=raw)
            if not hmac.compare_digest(signature_response, expected):
                return WorkerNetworkResult(response.status, "UNKNOWN", None, generation, request_id)
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or data.get("request_id") != request_id
                or data.get("instance_id") != self.config.instance_id
                or data.get("channel") != self.config.channel
                or data.get("repository") != self.config.repository
                or not isinstance(data.get("supervisor_generation"), str)
                or (generation != "-" and data["supervisor_generation"] != generation)
                or not isinstance(data.get("result_code"), str)
            ):
                return WorkerNetworkResult(response.status, "UNKNOWN", None, generation, request_id)
            payload = data.get("data")
            if payload is not None and not isinstance(payload, dict):
                return WorkerNetworkResult(response.status, "UNKNOWN", None, generation, request_id)
            return WorkerNetworkResult(response.status, data["result_code"], payload, data["supervisor_generation"], request_id)
        except (OSError, http.client.HTTPException, ValueError, UnicodeDecodeError):
            # The request may have reached the authoritative operation journal.
            # The caller must read back this SAME operation ID before retrying.
            return WorkerNetworkResult(0, "UNKNOWN", None, generation, request_id)
        finally:
            connection.close()

    @staticmethod
    def _parse_generation(raw: bytes) -> str:
        try:
            parsed = json.loads(raw)
            value = parsed.get("supervisor_generation") if isinstance(parsed, dict) else None
            return value if isinstance(value, str) and 1 <= len(value) <= 128 else ""
        except (ValueError, UnicodeDecodeError):
            return ""
