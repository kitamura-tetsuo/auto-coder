"""Private authenticated HTTP transport for a fixed in-container Supervisor.

The worker remains the sole diagnostic and reevaluation producer. This server is
owned by PID 1; it cannot start a scheduler or access the Docker Engine.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from loguru import logger

from .worker_network_protocol import (
    MAX_CLOCK_SKEW,
    MAX_NONCES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_TARGET_LENGTH,
    PROTOCOL_VERSION,
    load_machine_secret,
    request_mac,
    response_mac,
    valid_token,
)

if TYPE_CHECKING:
    from .worker_supervisor import WorkerSupervisor

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HEX_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
_NONCE = re.compile(r"[\x21-\x7e]{1,128}\Z")
_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
_DIAGNOSTIC_PARAMS = {
    "status": {"limit"},
    "logs": {"limit", "item_type", "item_number", "execution_id", "process_run_id", "after_sequence", "cursor"},
    "validation-attempts": {"pr_number", "attempt_id", "attempt_sequence"},
}


@dataclass(frozen=True)
class SupervisorBridgeConfig:
    instance_id: str
    channel: str
    repository: str
    address: str
    port: int
    secret: bytes

    @classmethod
    def from_supervisor(cls, supervisor: "WorkerSupervisor") -> "SupervisorBridgeConfig | None":
        config = supervisor.config
        values = (config.bridge_bind_address, config.bridge_port, config.bridge_secret_file)
        if all(value is None for value in values):
            return None
        if any(value is None for value in values) or config.source_repository is None:
            raise ValueError("enabled supervisor bridge requires address, port, machine credential and trusted source root")
        assert config.bridge_bind_address is not None and config.bridge_port is not None and config.bridge_secret_file is not None
        address = ipaddress.ip_address(config.bridge_bind_address)
        if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified:
            raise ValueError("supervisor bridge requires an exact private Docker-network IPv4 address")
        if not 1024 <= config.bridge_port <= 65535:
            raise ValueError("supervisor bridge port is invalid")
        if config.channel not in {"beta", "release"} or not all(_IDENTITY.fullmatch(value) for value in (config.instance_id, config.repository)):
            raise ValueError("supervisor bridge identity is invalid")
        source = config.source_repository
        if not source.is_absolute():
            raise ValueError("trusted source repository must be an absolute path")
        return cls(config.instance_id, config.channel, config.repository, str(address), config.bridge_port, load_machine_secret(config.bridge_secret_file))


class _ReplayGuard:
    """Fixed-space nonce admission within one Supervisor incarnation."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def accept(self, nonce: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}
            if nonce in self._seen or len(self._seen) >= MAX_NONCES:
                return False
            self._seen[nonce] = now + 2 * MAX_CLOCK_SKEW
            return True


class _BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8
    allow_reuse_address = False

    def __init__(self, config: SupervisorBridgeConfig, supervisor: "WorkerSupervisor") -> None:
        self.bridge_config = config
        self.supervisor = supervisor
        self.replay = _ReplayGuard()
        self._slots = threading.BoundedSemaphore(4)
        super().__init__((config.address, config.port), _BridgeHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class SupervisorBridgeServer:
    """Optional network listener started only after exclusive PID-1 ownership."""

    def __init__(self, config: SupervisorBridgeConfig, supervisor: "WorkerSupervisor") -> None:
        self._http = _BoundedServer(config, supervisor)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._http.serve_forever, kwargs={"poll_interval": 0.1}, name="supervisor-private-http", daemon=True)
        self._thread.start()
        logger.info("Private Supervisor transport started instance={} channel={}", self._http.bridge_config.instance_id, self._http.bridge_config.channel)

    def close(self) -> None:
        if self._thread is not None:
            self._http.shutdown()
            self._thread.join(timeout=5)
        self._http.server_close()


class _BridgeHandler(BaseHTTPRequestHandler):
    server: _BoundedServer
    protocol_version = "HTTP/1.1"
    timeout = 5

    def log_message(self, _format: str, *_args: object) -> None:
        # Neither signed headers, request bodies, nor query values are logged.
        return

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _header(self, key: str) -> str | None:
        values = self.headers.get_all(key, [])
        return values[0] if len(values) == 1 else None

    def _handle(self, method: str) -> None:
        self.close_connection = True
        if len(self.path) > MAX_TARGET_LENGTH or "#" in self.path or not self.path.startswith("/"):
            self._send(400, "UNSUPPORTED")
            return
        size = self._header("Content-Length")
        if self.headers.get("Transfer-Encoding") is not None or (method == "POST" and (size is None or not size.isascii() or not size.isdecimal())):
            self._send(400, "UNSUPPORTED")
            return
        if method == "GET" and size not in (None, "0"):
            self._send(400, "UNSUPPORTED")
            return
        length = int(size) if size is not None else 0
        if length > MAX_REQUEST_BYTES or (method == "POST" and self._header("Content-Type") != "application/json"):
            self._send(413 if length > MAX_REQUEST_BYTES else 400, "UNSUPPORTED")
            return
        try:
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                self._send(400, "UNSUPPORTED")
                return
            request_id, generation = self._authenticate(method, body)
            if request_id is None:
                return
            status, code, data = self._dispatch(method, body)
            self._send(status, code, data, request_id=request_id, generation=generation)
        except (OSError, ValueError, TypeError) as error:
            logger.warning("Private Supervisor request refused category={}", type(error).__name__)
            self._send(422, "UNSUPPORTED")
        except Exception as error:
            logger.error("Private Supervisor transport unavailable category={}", type(error).__name__)
            self._send(503, "UNKNOWN")

    def _authenticate(self, method: str, body: bytes) -> tuple[str | None, str]:
        config = self.server.bridge_config
        generation = self._header("X-AC-Supervisor-Generation") or ""
        instance = self._header("X-AC-Instance") or ""
        timestamp = self._header("X-AC-Timestamp") or ""
        nonce = self._header("X-AC-Nonce") or ""
        request_id = self._header("X-AC-Request-Id") or ""
        signature = self._header("X-AC-Signature") or ""
        version = self._header("X-AC-Version")
        worker_run = self._header("X-AC-Worker-Process-Run-Id") or ""
        worker_generation = self._header("X-AC-Worker-Deployment-Generation") or ""
        expected = request_mac(
            config.secret,
            method=method,
            target=self.path,
            instance=instance,
            generation=generation,
            timestamp=timestamp,
            nonce=nonce,
            request_id=request_id,
            body=body,
            worker_run=worker_run,
            worker_generation=worker_generation,
        )
        verified = hmac.compare_digest(signature if _HEX_SIGNATURE.fullmatch(signature) else "0" * 64, expected)
        if version != PROTOCOL_VERSION or not verified or instance != config.instance_id or not valid_token(nonce) or not valid_token(request_id):
            self._send(401, "UNAUTHORIZED")
            return None, ""
        if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) != 10 or abs(time.time() - int(timestamp)) > MAX_CLOCK_SKEW:
            self._send(401, "UNAUTHORIZED")
            return None, ""
        if generation != self.server.supervisor.incarnation:
            if not (method == "GET" and self.path == "/v1/supervisor" and generation == "-"):
                self._send(409, "CONFLICT")
                return None, ""
        if not self.server.replay.accept(nonce):
            self._send(401, "UNAUTHORIZED")
            return None, ""
        path = urlsplit(self.path).path
        worker_request = path == "/v1/worker-observation" or path.startswith("/v1/diagnostics/") or path == "/v1/reevaluations" or path.startswith("/v1/reevaluations/")
        if worker_request and (not worker_run or not worker_generation or len(worker_run) > 128 or len(worker_generation) > 128 or not self._current_worker_matches(worker_run, worker_generation)):
            self._send(409, "CONFLICT", request_id=request_id, generation=self.server.supervisor.incarnation)
            return None, ""
        if not worker_request and (worker_run or worker_generation):
            self._send(422, "UNSUPPORTED", request_id=request_id, generation=self.server.supervisor.incarnation)
            return None, ""
        self._expected_worker_run = worker_run
        self._expected_worker_generation = worker_generation
        return request_id, self.server.supervisor.incarnation

    def _send(self, status: int, code: str, data: object = None, *, request_id: str | None = None, generation: str = "") -> None:
        config = self.server.bridge_config
        payload: dict[str, Any] = {
            "schema_version": 1,
            "instance_id": config.instance_id,
            "channel": config.channel,
            "repository": config.repository,
            "supervisor_generation": generation or self.server.supervisor.incarnation,
            "request_id": request_id,
            "result_code": code,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        raw = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            status = 503
            payload.update(result_code="UNAVAILABLE", data=None)
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if request_id is not None:
            self.send_header("X-AC-Response-Signature", response_mac(config.secret, request_id=request_id, generation=payload["supervisor_generation"], body=raw))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method: str, body: bytes) -> tuple[int, str, object]:
        supervisor = self.server.supervisor
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return 422, "UNSUPPORTED", None
        path = parsed.path
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if len(pairs) > 10 or len({key for key, _ in pairs}) != len(pairs) or len(parsed.query) > 1024:
            return 422, "UNSUPPORTED", None

        if method == "GET" and path == "/v1/supervisor" and not pairs:
            return 200, "OK", supervisor.project_result(supervisor.inspect())

        if method == "GET" and path == "/v1/worker-observation" and len(pairs) == 1 and pairs[0][0] == "nonce" and _NONCE.fullmatch(pairs[0][1]):
            result = supervisor.observe_worker(pairs[0][1])
            return (200, "OK", result) if result is not None else (503, "UNAVAILABLE", None)

        if method == "GET" and path.startswith("/v1/diagnostics/"):
            kind = path.removeprefix("/v1/diagnostics/")
            if kind not in _DIAGNOSTIC_PARAMS or not all(key in _DIAGNOSTIC_PARAMS[kind] for key, _ in pairs):
                return 422, "UNSUPPORTED", None
            upstream = self._worker_request("GET", "/api/" + kind + ("?" + parsed.query if parsed.query else ""))
            return self._translate_upstream(upstream)

        if method == "POST" and path == "/v1/reevaluations" and not pairs:
            value = self._parse_json(body, {"operation_id", "repository", "channel", "expected_process_run_id", "expected_deployment_generation", "target"})
            if value is None or not _OPERATION.fullmatch(str(value.get("operation_id", ""))):
                return 422, "UNSUPPORTED", None
            if value["repository"] != self.server.bridge_config.repository or value["channel"] != self.server.bridge_config.channel:
                return 409, "CONFLICT", None
            if value["expected_process_run_id"] != self._expected_worker_run or value["expected_deployment_generation"] != self._expected_worker_generation:
                return 409, "CONFLICT", None
            if not self._current_worker_matches(value["expected_process_run_id"], value["expected_deployment_generation"]):
                return 409, "CONFLICT", None
            return self._translate_upstream(self._worker_request("POST", "/v1/reevaluations", body))

        if method == "GET" and path.startswith("/v1/reevaluations/") and not pairs:
            operation_id = path.removeprefix("/v1/reevaluations/")
            if not _OPERATION.fullmatch(operation_id):
                return 422, "UNSUPPORTED", None
            return self._translate_upstream(self._worker_request("GET", f"/v1/reevaluations/{operation_id}"))

        if method == "POST" and path == "/v1/worker-version-preparations" and not pairs:
            value = self._parse_json(body, {"operation_id", "source_sha"})
            if value is None or not isinstance(value["operation_id"], str) or not _OPERATION.fullmatch(value["operation_id"]) or not isinstance(value["source_sha"], str) or not _SHA.fullmatch(value["source_sha"]):
                return 422, "UNSUPPORTED", None
            result = supervisor.prepare_version(value["operation_id"], value["source_sha"])
            return self._typed_result(result)

        if method == "GET" and path.startswith("/v1/worker-version-preparations/") and not pairs:
            operation_id = path.removeprefix("/v1/worker-version-preparations/")
            if not _OPERATION.fullmatch(operation_id):
                return 422, "UNSUPPORTED", None
            return self._typed_result(supervisor.read_preparation(operation_id))

        if method == "POST" and path == "/v1/worker-operations" and not pairs:
            value = self._parse_json(body, {"operation_id", "operation", "expected_process_run_id", "expected_deployment_generation", "expected_runtime_id"}, {"runtime_id"})
            if value is None or not isinstance(value["operation_id"], str) or not _OPERATION.fullmatch(value["operation_id"]) or value["operation"] not in {"START", "REQUEST_GRACEFUL_STOP", "RESTART_WORKER"}:
                return 422, "UNSUPPORTED", None
            run, generation, runtime = (value[key] for key in ("expected_process_run_id", "expected_deployment_generation", "expected_runtime_id"))
            if not self._current_worker_matches(run, generation, allow_stopped=True, expected_runtime_id=runtime):
                return 409, "CONFLICT", None
            if value["operation"] == "REQUEST_GRACEFUL_STOP" and "runtime_id" in value:
                return 422, "UNSUPPORTED", None
            if value["operation"] == "REQUEST_GRACEFUL_STOP" and run is None:
                return 409, "CONFLICT", None
            operation = {
                "operation_id": value["operation_id"],
                "operation": value["operation"],
                "expected_supervisor_incarnation": supervisor.incarnation,
                "expected_process_run_id": run,
                "expected_generation": generation,
                "expected_runtime_id": runtime,
            }
            if "runtime_id" in value:
                operation["runtime_id"] = value["runtime_id"]
            return self._typed_result(supervisor.project_result(supervisor.apply(operation)))

        if method == "GET" and path.startswith("/v1/worker-operations/") and not pairs:
            operation_id = path.removeprefix("/v1/worker-operations/")
            if not _OPERATION.fullmatch(operation_id):
                return 422, "UNSUPPORTED", None
            return self._typed_result(supervisor.read_operation(operation_id))
        return 404, "NOT_FOUND", None

    @staticmethod
    def _parse_json(body: bytes, required: set[str], optional: set[str] | None = None) -> dict[str, Any] | None:
        try:
            value = json.loads(body)
            if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= required | (optional or set()):
                return None
            return value
        except (ValueError, UnicodeDecodeError):
            return None

    def _current_worker_matches(self, run: object, generation: object, *, allow_stopped: bool = False, expected_runtime_id: object = None) -> bool:
        supervisor = self.server.supervisor
        inspection = supervisor.inspect()
        worker = inspection.get("worker")
        if not isinstance(worker, dict):
            if not allow_stopped or inspection["status"] != "STOPPED" or run is not None or generation is not None:
                return False
        elif not inspection["alive"] or not isinstance(run, str) or not isinstance(generation, str) or not run or not generation:
            return False
        elif worker.get("process_run_id") != run or worker.get("deployment_generation") != generation:
            return False
        if allow_stopped:
            try:
                selected = supervisor.store.read().get("selected_runtime_id")
            except (OSError, ValueError):
                return False
            return expected_runtime_id == selected
        return True

    def _worker_request(self, method: str, target: str, body: bytes = b"") -> tuple[int, dict[str, Any]] | None:
        snapshot = self.server.supervisor.inspect()
        worker = snapshot.get("worker")
        if not snapshot.get("alive") or not isinstance(worker, dict) or worker.get("process_run_id") != self._expected_worker_run or worker.get("deployment_generation") != self._expected_worker_generation:
            return None
        return self.server.supervisor.worker_http(worker, method, target, body)

    @staticmethod
    def _translate_upstream(upstream: tuple[int, dict[str, Any]] | None) -> tuple[int, str, object]:
        if upstream is None:
            return 503, "UNAVAILABLE", None
        status, data = upstream
        codes = {200: "OK", 202: "ACCEPTED", 400: "UNSUPPORTED", 404: "NOT_FOUND", 409: "CONFLICT", 422: "UNSUPPORTED", 503: "UNAVAILABLE"}
        if status not in codes:
            return 503, "UNKNOWN", None
        return status, codes[status], data

    @staticmethod
    def _typed_result(result: dict[str, Any]) -> tuple[int, str, object]:
        state = result.get("status")
        status = {"UNSUPPORTED": 422, "CONFLICT": 409, "NOT_FOUND": 404, "UNAVAILABLE": 503, "NEEDS_ATTENTION": 409, "UNKNOWN": 503}.get(state if isinstance(state, str) else "UNKNOWN", 200)
        return status, str(state) if isinstance(state, str) else "UNKNOWN", result
