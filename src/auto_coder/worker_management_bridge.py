"""Incarnation-bound, read-only worker observations over a private Unix socket."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .build_provenance import observe_controller_artifact
from .deployment_channel import validate_repository_ownership
from .entity_invalidation import ReevaluationOperationConflict, ReevaluationReceipt
from .health_monitor import get_health_monitor
from .logger_config import get_logger
from .public_api import init_public_api, sanitize_text

logger = get_logger(__name__)

MAX_DOCUMENT_BYTES = 256 * 1024
MAX_UNSETTLED = 500
MAX_TEXT = 2000
_NONCE = re.compile(r"^[\x20-\x7e]{1,128}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IDENTITY = re.compile(r"^[\x21-\x7e]{1,2000}$")


class ManagementBridgeConfigurationError(RuntimeError):
    """The opt-in bridge cannot safely establish its private endpoint."""


class WrongWorkerTarget(ValueError):
    """A valid command names a different configured worker identity."""


@dataclass(frozen=True)
class BridgeIdentity:
    repository: str
    channel: str
    deployment_generation: str
    socket_path: Path
    runtime_root: Path
    declared_artifact_digest: str


def configured_identity(repository: str) -> Optional[BridgeIdentity]:
    """Capture and validate all lifetime identity before a listener is opened."""
    raw_socket = os.environ.get("AUTO_CODER_MANAGEMENT_SOCKET")
    if raw_socket is None:
        return None
    socket_path = Path(raw_socket)
    if not socket_path.is_absolute():
        raise ManagementBridgeConfigurationError("AUTO_CODER_MANAGEMENT_SOCKET must be absolute")
    generation = os.environ.get("AUTO_CODER_DEPLOYMENT_GENERATION", "").strip()
    if not generation or len(generation) > MAX_TEXT or not generation.isascii() or any(ord(char) < 0x21 or ord(char) > 0x7E for char in generation):
        raise ManagementBridgeConfigurationError("AUTO_CODER_DEPLOYMENT_GENERATION must be nonempty bounded ASCII")
    deployment = validate_repository_ownership(repository)
    if deployment is None:
        raise ManagementBridgeConfigurationError("management observation requires a release/beta deployment identity")
    root = deployment.runtime_root.resolve()
    resolved_parent = socket_path.parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise ManagementBridgeConfigurationError("management socket must be inside AUTO_CODER_RUNTIME_ROOT")
    if not resolved_parent.is_dir() or resolved_parent.stat().st_uid != os.getuid():
        raise ManagementBridgeConfigurationError("management socket directory must exist and be owned by this operator")
    return BridgeIdentity(repository, deployment.channel, generation, socket_path, root, deployment.artifact)


def _safe_text(value: object) -> str:
    # Reuse the diagnostic projection's full free-text policy.  In particular,
    # sanitize URLs, authorization values, credentials, and paths before the
    # shared 2,000-character clipping bound is applied.
    return sanitize_text(str(value), paths=True)[0]


class WorkerManagementBridge:
    """Own one UDS listener and terminal receipt for one worker lifetime."""

    def __init__(self, identity: BridgeIdentity, engine: Any) -> None:
        self.identity = identity
        self.engine = engine
        # Installed-artifact and process-run identity are immutable facts of
        # this bridge incarnation.  Capture them once before serving so every
        # response/header is correlatable and middleware never performs
        # metadata/filesystem I/O on the event loop.
        self.artifact = observe_controller_artifact()
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.server: Any = None
        self.task: Optional[asyncio.Task[Any]] = None
        self._listener: Optional[socket.socket] = None
        self._owned_endpoint: Optional[tuple[int, int]] = None
        self._register_routes()

    @property
    def receipt_path(self) -> Path:
        return self.identity.socket_path.with_name("last-terminal.json")

    def _headers(self) -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Auto-Coder-Process-Run-Id": self.artifact.process_run_id or "unavailable",
            "Auto-Coder-Deployment-Generation": self.identity.deployment_generation,
        }

    def _register_routes(self) -> None:
        @self.app.middleware("http")
        async def incarnation_headers(request: Request, call_next: Any) -> Response:
            response = await call_next(request)
            response.headers.update(self._headers())
            return response

        @self.app.get("/v1/observation", include_in_schema=False)
        async def observation(request: Request) -> Response:
            pairs = list(request.query_params.multi_items())
            if len(pairs) != 1 or pairs[0][0] != "nonce" or not _NONCE.fullmatch(pairs[0][1]):
                return JSONResponse({"error": {"code": "invalid_request", "message": "Exactly one valid nonce is required."}}, status_code=422)
            try:
                payload = await asyncio.to_thread(self._observation, pairs[0][1])
                encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
                if len(encoded) > MAX_DOCUMENT_BYTES:
                    raise ValueError("observation exceeds size limit")
                return Response(encoded, media_type="application/json")
            except Exception as exc:
                logger.error(f"Private worker observation unavailable: {type(exc).__name__}")
                return JSONResponse({"error": {"code": "observation_unavailable", "message": "Worker observation is unavailable."}}, status_code=503)

        @self.app.post("/v1/reevaluations", include_in_schema=False)
        async def reevaluate(request: Request) -> Response:
            try:
                raw = await request.body()
                if len(raw) > 16 * 1024:
                    return self._error("invalid_request", "Request is invalid.", 422)
                body = json.loads(raw)
                parsed = self._parse_reevaluation(body)
            except WrongWorkerTarget:
                return self._error("wrong_target", "Command targets a different worker.", 409)
            except (ValueError, TypeError, json.JSONDecodeError):
                return self._error("invalid_request", "Request is invalid.", 422)
            try:
                receipt, replay = await self.engine.accept_private_reevaluation(
                    **parsed,
                    current_process_run_id=self.artifact.process_run_id or "",
                    current_deployment_generation=self.identity.deployment_generation,
                )
            except ReevaluationOperationConflict:
                logger.warning("Private reevaluation refused code=operation_payload_conflict operation_id={}", parsed["operation_id"])
                return self._error("operation_payload_conflict", "Operation identity is already bound.", 409)
            except RuntimeError as exc:
                code = str(exc)
                if code == "wrong_incarnation":
                    self._record_reevaluation_refusal(parsed, code)
                    return self._error(code, "Worker identity does not match.", 409)
                if code == "worker_not_ready":
                    self._record_reevaluation_refusal(parsed, "not_ready_or_draining")
                    return self._error("not_ready_or_draining", "Worker is not accepting commands.", 409)
                logger.error("Private reevaluation storage unavailable operation_id={}", parsed["operation_id"])
                return self._error("storage_unavailable", "Authoritative storage is unavailable.", 503)
            except Exception as exc:
                logger.error("Private reevaluation storage unavailable operation_id={} error={}", parsed["operation_id"], type(exc).__name__)
                return self._error("storage_unavailable", "Authoritative storage is unavailable.", 503)
            logger.info("Private reevaluation response operation_id={} replay={}", parsed["operation_id"], replay)
            return JSONResponse(self._receipt_document(receipt), status_code=200 if replay else 202)

        @self.app.get("/v1/reevaluations/{operation_id}", include_in_schema=False)
        async def reevaluation_receipt(operation_id: str, request: Request) -> Response:
            if request.query_params or not _OPERATION_ID.fullmatch(operation_id):
                return self._error("invalid_identity", "Operation identity is invalid.", 422)
            try:
                receipt = await asyncio.to_thread(self.engine.invalidations.get_reevaluation_receipt, operation_id)
            except Exception as exc:
                logger.error("Private reevaluation receipt unavailable operation_id={} error={}", operation_id, type(exc).__name__)
                return self._error("storage_unavailable", "Authoritative storage is unavailable.", 503)
            if receipt is None:
                return self._error("not_found", "No committed acceptance receipt exists.", 404)
            return JSONResponse(self._receipt_document(receipt))

        # This registers only the existing inert projections.  The public
        # application's opt-in flag and route set are not consulted or changed.
        init_public_api(self.app, self.engine, self.identity.repository, safe_failure_logging=True)

    @staticmethod
    def _error(code: str, message: str, status: int) -> JSONResponse:
        return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)

    def _parse_reevaluation(self, body: object) -> dict[str, object]:
        required = {"operation_id", "repository", "channel", "expected_process_run_id", "expected_deployment_generation", "target"}
        if not isinstance(body, dict) or set(body) != required:
            raise ValueError("closed schema")
        target = body["target"]
        if not isinstance(target, dict) or set(target) != {"type", "number"}:
            raise ValueError("closed target schema")
        operation_id = body["operation_id"]
        strings = [body[key] for key in ("repository", "channel", "expected_process_run_id", "expected_deployment_generation")]
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise ValueError("invalid operation identity")
        if any(not isinstance(value, str) or not _IDENTITY.fullmatch(value) for value in strings):
            raise ValueError("invalid identity")
        number = target["number"]
        if target["type"] not in {"issue", "pr"} or isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("invalid target")
        if body["repository"] != self.identity.repository or body["channel"] != self.identity.channel:
            raise WrongWorkerTarget("foreign worker")
        return {
            "operation_id": operation_id,
            "repository": body["repository"],
            "channel": body["channel"],
            "target_type": target["type"],
            "target_number": number,
            "expected_process_run_id": body["expected_process_run_id"],
            "expected_deployment_generation": body["expected_deployment_generation"],
        }

    def _record_reevaluation_refusal(self, parsed: dict[str, object], code: str) -> None:
        """Emit bounded command correlation without becoming refusal authority."""
        try:
            logger.warning(
                "Private reevaluation refused code={} operation_id={} target={}#{} process_run_id={} deployment_generation={}",
                code,
                parsed["operation_id"],
                parsed["target_type"],
                parsed["target_number"],
                self.artifact.process_run_id or "unavailable",
                self.identity.deployment_generation,
            )
        except Exception:
            # Diagnostic sinks are explicitly non-authoritative. A broken sink
            # cannot turn a refusal into acceptance or change its typed result.
            pass

    @staticmethod
    def _receipt_document(receipt: ReevaluationReceipt) -> dict[str, object]:
        return {
            "schema_version": receipt.schema_version,
            "operation_id": receipt.operation_id,
            "repository": receipt.repository,
            "channel": receipt.channel,
            "target": {"type": receipt.target_type, "number": receipt.target_number},
            "requested_process_run_id": receipt.requested_process_run_id,
            "requested_deployment_generation": receipt.requested_deployment_generation,
            "accepting_process_run_id": receipt.accepting_process_run_id,
            "accepting_deployment_generation": receipt.accepting_deployment_generation,
            "accepted_at": receipt.accepted_at,
            "invalidation": {"disposition": receipt.disposition.value, "generation": receipt.generation},
            "outcome": receipt.outcome,
        }

    def _observation(self, nonce: str) -> dict[str, object]:
        gate = self.engine.invocation_admission_snapshot()
        unsettled = gate.unsettled
        identities = [
            {
                "invocation_id": item.invocation_id,
                "repository": _safe_text(item.repository),
                "target": _safe_text(item.target),
                "stage": _safe_text(item.stage),
                "state": item.state.value,
            }
            for item in unsettled[:MAX_UNSETTLED]
        ]
        return {
            "schema_version": 1,
            "repository": self.identity.repository,
            "channel": self.identity.channel,
            "deployment_generation": self.identity.deployment_generation,
            "process_run_id": self.artifact.process_run_id,
            "observation_time": datetime.now(timezone.utc).isoformat(),
            "nonce": nonce,
            "artifact": {
                "distribution_version": asdict(self.artifact.distribution_version),
                "source_revision": asdict(self.artifact.source_revision),
                "declared_artifact_digest": {"value": self.identity.declared_artifact_digest, "origin": "declared_configuration"},
            },
            "lifecycle": {
                "state": self.engine.lifecycle.value,
                "startup_reconciled": bool(self.engine.startup_reconciled),
                "startup_reconciliation_error": _safe_text(self.engine.startup_reconciliation_error) if self.engine.startup_reconciliation_error else None,
                "daemon_serving": bool(self.engine.daemon_serving),
            },
            "heartbeat": get_health_monitor().heartbeat_observation(),
            "invocations": {
                "state": gate.state.value,
                "is_graceful_ready": gate.is_graceful_ready,
                "unsettled_count": len(unsettled),
                "unsettled": identities,
                "clipped": len(unsettled) > MAX_UNSETTLED,
            },
        }

    async def start(self) -> None:
        """Start Uvicorn and prove this lifetime owns the resulting inode."""
        path = self.identity.socket_path
        os.chmod(path.parent, 0o700)
        import uvicorn

        # Bind ourselves rather than asking Uvicorn to bind a pathname.  A
        # Unix bind is the atomic ownership decision: unlike Uvicorn's uds
        # helper it never unlinks a socket won by a concurrent worker.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen()
            listener.setblocking(False)
        except OSError as exc:
            listener.close()
            raise ManagementBridgeConfigurationError("management socket endpoint already exists or cannot be bound") from exc
        self._listener = listener
        details = path.stat()
        self._owned_endpoint = (details.st_dev, details.st_ino)
        os.chmod(path, 0o600)

        config = uvicorn.Config(self.app, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.server.capture_signals = lambda: _NullContext()
        self.task = asyncio.create_task(self.server.serve(sockets=[listener]), name="private-worker-management-bridge")
        while not self.server.started:
            if self.task.done():
                await self.task
                raise ManagementBridgeConfigurationError("management bridge failed to start")
            await asyncio.sleep(0.01)
        logger.info("Private worker management bridge started")

    async def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            result = await asyncio.gather(self.task, return_exceptions=True)
            if result and isinstance(result[0], BaseException):
                logger.error(f"Private worker management bridge stopped with an error: {type(result[0]).__name__}")
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        try:
            details = self.identity.socket_path.lstat()
            if self._owned_endpoint == (details.st_dev, details.st_ino) and stat.S_ISSOCK(details.st_mode):
                self.identity.socket_path.unlink()
        except FileNotFoundError:
            pass

    def persist_graceful_terminal(self, reason: str) -> None:
        """Atomically record only the engine's successful graceful STOPPED branch."""
        try:
            gate = self.engine.invocation_admission_snapshot()
            if self.engine.lifecycle.value != "stopped" or gate.state.value == "forced" or not gate.is_graceful_ready or gate.unsettled:
                raise ValueError("terminal state is not a successful graceful drain")
            if self.artifact.process_run_id is None:
                raise ValueError("process-run identity is unavailable")
            payload = {
                "schema_version": 1,
                "repository": self.identity.repository,
                "channel": self.identity.channel,
                "deployment_generation": self.identity.deployment_generation,
                "process_run_id": self.artifact.process_run_id,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "terminal_reason": _safe_text(reason),
                "lifecycle_state": self.engine.lifecycle.value,
                "invocation_gate_state": gate.state.value,
                "is_graceful_ready": gate.is_graceful_ready,
                "unsettled_count": len(gate.unsettled),
            }
            encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise ValueError("terminal receipt exceeds size limit")
            fd, temporary = tempfile.mkstemp(prefix=".last-terminal-", dir=self.receipt_path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.receipt_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except Exception as exc:
            logger.error(f"Graceful terminal receipt unavailable: {type(exc).__name__}")


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None
