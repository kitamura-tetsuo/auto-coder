"""Incarnation-bound, read-only worker observations over a private Unix socket."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .build_provenance import observe_controller_artifact
from .deployment_channel import DeploymentIdentity, validate_repository_ownership
from .health_monitor import get_health_monitor
from .logger_config import get_logger
from .public_api import init_public_api
from .security_utils import redact_string

logger = get_logger(__name__)

MAX_DOCUMENT_BYTES = 256 * 1024
MAX_UNSETTLED = 500
MAX_TEXT = 2000
_NONCE = re.compile(r"^[\x21-\x7e]{1,128}$")


class ManagementBridgeConfigurationError(RuntimeError):
    """The opt-in bridge cannot safely establish its private endpoint."""


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
    return redact_string(str(value))[:MAX_TEXT]


class WorkerManagementBridge:
    """Own one UDS listener and terminal receipt for one worker lifetime."""

    def __init__(self, identity: BridgeIdentity, engine: Any) -> None:
        self.identity = identity
        self.engine = engine
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.server: Any = None
        self.task: Optional[asyncio.Task[Any]] = None
        self._owned_endpoint: Optional[tuple[int, int]] = None
        self._register_routes()

    @property
    def receipt_path(self) -> Path:
        return self.identity.socket_path.with_name("last-terminal.json")

    def _headers(self) -> dict[str, str]:
        artifact = observe_controller_artifact()
        return {
            "Cache-Control": "no-store",
            "Auto-Coder-Process-Run-Id": artifact.process_run_id or "unavailable",
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

        # This registers only the existing inert projections.  The public
        # application's opt-in flag and route set are not consulted or changed.
        init_public_api(self.app, self.engine, self.identity.repository)

    def _observation(self, nonce: str) -> dict[str, object]:
        artifact = observe_controller_artifact()
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
            "process_run_id": artifact.process_run_id,
            "observation_time": datetime.now(timezone.utc).isoformat(),
            "nonce": nonce,
            "artifact": {
                "distribution_version": asdict(artifact.distribution_version),
                "source_revision": asdict(artifact.source_revision),
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
        if path.exists() or path.is_symlink():
            raise ManagementBridgeConfigurationError("management socket endpoint already exists")
        os.chmod(path.parent, 0o700)
        import uvicorn

        config = uvicorn.Config(self.app, uds=str(path), log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.server.capture_signals = lambda: _NullContext()
        self.task = asyncio.create_task(self.server.serve(), name="private-worker-management-bridge")
        while not self.server.started:
            if self.task.done():
                await self.task
                raise ManagementBridgeConfigurationError("management bridge failed to start")
            await asyncio.sleep(0.01)
        details = path.stat()
        if not stat.S_ISSOCK(details.st_mode):
            raise ManagementBridgeConfigurationError("management endpoint is not a Unix socket")
        os.chmod(path, 0o600)
        self._owned_endpoint = (details.st_dev, details.st_ino)
        logger.info("Private worker management bridge started")

    async def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            await self.task
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
            artifact = observe_controller_artifact()
            payload = {
                "schema_version": 1,
                "repository": self.identity.repository,
                "channel": self.identity.channel,
                "deployment_generation": self.identity.deployment_generation,
                "process_run_id": artifact.process_run_id,
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
