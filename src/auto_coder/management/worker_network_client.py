"""Fixed-target, machine-authenticated private Supervisor network client.

This is a lower-level transport for the durable management dispatcher. OAuth
credentials are never accepted here or forwarded to the worker container.
"""

from __future__ import annotations

import hashlib
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
_TARGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REGISTRY_ENV = "AUTO_CODER_WORKER_REGISTRY"
_WORKER_FIELDS = frozenset({"target_id", "supervisor_instance_id", "channel", "repository", "bridge_address", "bridge_port", "bridge_secret_file"})
_LEGACY_FIELDS = frozenset(f"AUTO_CODER_{channel}_{field}" for channel in ("BETA", "RELEASE") for field in ("SUPERVISOR_INSTANCE_ID", "REPOSITORY", "BRIDGE_ADDRESS", "BRIDGE_PORT", "BRIDGE_SECRET_FILE"))


@dataclass(frozen=True)
class WorkerEndpointConfig:
    instance_id: str
    channel: str
    repository: str
    address: str
    port: int
    secret: bytes
    target_id: str = ""
    secret_file: Path | None = None

    @property
    def binding_digest(self) -> str:
        """Non-secret persistent identity of the exact configured bridge."""
        identity = (
            self.target_id or self.instance_id,
            self.instance_id,
            self.channel,
            self.repository,
            self.address,
            self.port,
            str(self.secret_file) if self.secret_file is not None else "",
            hashlib.sha256(self.secret).hexdigest(),
        )
        return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    @classmethod
    def environment(cls, target_id: str) -> "WorkerEndpointConfig":
        """Resolve one exact target from the trusted, validated registry."""
        return configured_worker_registry()[target_id]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate worker registry field")
        result[key] = value
    return result


def configured_worker_registry() -> dict[str, WorkerEndpointConfig]:
    """Validate the entire trusted target registry before enabling any bridge."""
    raw = os.environ.get(_REGISTRY_ENV)
    if raw is None:
        return {}
    if not raw or len(raw.encode("utf-8")) > 65536:
        raise ValueError("worker registry is empty or too large")
    try:
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
    except ValueError as exc:
        if str(exc) == "duplicate worker registry field":
            raise
        raise ValueError("invalid worker registry JSON") from exc
    except TypeError as exc:
        raise ValueError("invalid worker registry JSON") from exc
    if not isinstance(document, dict) or set(document) != {"workers"} or not isinstance(document["workers"], list) or not document["workers"]:
        raise ValueError("worker registry must contain a nonempty workers list")
    registry: dict[str, WorkerEndpointConfig] = {}
    instances: set[str] = set()
    endpoints: set[tuple[str, int]] = set()
    repositories: set[str] = set()
    secret_paths: set[Path] = set()
    secrets: set[bytes] = set()
    for entry in document["workers"]:
        if not isinstance(entry, dict) or set(entry) != _WORKER_FIELDS:
            raise ValueError("worker registry entry is incomplete or has unknown fields")
        target_id = entry["target_id"]
        instance = entry["supervisor_instance_id"]
        channel = entry["channel"]
        repository = entry["repository"]
        port = entry["bridge_port"]
        path_value = entry["bridge_secret_file"]
        if (
            not isinstance(target_id, str)
            or _TARGET_ID.fullmatch(target_id) is None
            or not isinstance(instance, str)
            or _BRIDGE_IDENTITY.fullmatch(instance) is None
            or not isinstance(channel, str)
            or channel not in {"beta", "release"}
            or not isinstance(repository, str)
            or repository.count("/") != 1
            or _BRIDGE_IDENTITY.fullmatch(repository) is None
            or type(port) is not int
            or not 1024 <= port <= 65535
            or not isinstance(path_value, str)
            or not Path(path_value).is_absolute()
        ):
            raise ValueError("worker registry identity, channel or port is invalid")
        try:
            address = ipaddress.ip_address(entry["bridge_address"])
        except (ValueError, TypeError) as exc:
            raise ValueError("worker network endpoint must be a fixed private Docker IPv4 address and port") from exc
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
            raise ValueError("worker network endpoint must be a fixed private Docker IPv4 address and port")
        endpoint = (str(address), port)
        secret_path = Path(path_value)
        if target_id in registry or instance in instances or endpoint in endpoints or repository in repositories or secret_path in secret_paths:
            raise ValueError("worker registry requires distinct targets, instances, endpoints, repositories and credentials")
        secret = load_machine_secret(secret_path)
        if secret in secrets:
            raise ValueError("worker registry requires distinct machine credentials")
        registry[target_id] = WorkerEndpointConfig(instance, channel, repository, str(address), port, secret, target_id, secret_path)
        instances.add(instance)
        endpoints.add(endpoint)
        repositories.add(repository)
        secret_paths.add(secret_path)
        secrets.add(secret)
    return registry


def configured_worker_bridge_environment() -> dict[str, str]:
    """Capture the complete registry for a restarted management child."""
    legacy = _LEGACY_FIELDS.intersection(os.environ)
    if legacy:
        raise ValueError("legacy channel bridge configuration must be migrated to the worker registry")
    registry = configured_worker_registry()
    return {_REGISTRY_ENV: os.environ[_REGISTRY_ENV]} if registry else {}


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
        request_run = self.worker_process_run_id or ""
        request_generation = self.worker_deployment_generation or ""
        result = self._call("GET", "/v1/worker-observation?" + urlencode({"nonce": expected}), worker_run=request_run, worker_generation=request_generation)
        if result.result_code == "OK":
            data = result.data
            observed = data.get("observation") if isinstance(data, dict) else None
            if (
                not isinstance(observed, dict)
                or observed.get("nonce") != expected
                or observed.get("repository") != self.config.repository
                or observed.get("channel") != self.config.channel
                or observed.get("process_run_id") != request_run
                or observed.get("deployment_generation") != request_generation
            ):
                return self._unknown(result)
            assert isinstance(data, dict)
            binding = data.get("runtime_binding")
            if binding is not None and (not isinstance(binding, dict) or binding.get("process_run_id") != observed.get("process_run_id") or binding.get("deployment_generation") != observed.get("deployment_generation") or data.get("binding_status") != "VERIFIED"):
                return self._unknown(result)
        return result

    def diagnostics(self, kind: str, params: dict[str, str] | None = None) -> WorkerNetworkResult:
        allowed = {
            "consumer-identity": set(),
            "status": {"limit"},
            "logs": {"limit", "item_type", "item_number", "execution_id", "process_run_id", "after_sequence", "cursor"},
            "validation-attempts": {"pr_number", "attempt_id", "attempt_sequence"},
            "pr-evidence": {"pr_number", "limit", "cursor", "expected_revision"},
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
