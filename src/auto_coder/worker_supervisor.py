"""Fixed, Docker-independent PID-1 supervisor for one Auto-Coder worker."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from loguru import logger

from .worker_supervisor_store import SupervisorStore
from .worker_versions import PrepareIntent, WorkerVersionConfig, WorkerVersionError, WorkerVersionPreparer

_FULL_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


@dataclass(frozen=True)
class SupervisorConfig:
    instance_id: str
    channel: str
    repository: str
    runtime_root: Path
    workspace_root: Path
    interpreter: Path
    version_root: Path | None = None
    child_command: tuple[str, ...] | None = None
    start_on_boot: bool = False
    source_repository: Path | None = None
    bridge_bind_address: str | None = None
    bridge_port: int | None = None
    bridge_secret_file: Path | None = None

    @classmethod
    def environment(cls) -> "SupervisorConfig":
        names = ("AUTO_CODER_SUPERVISOR_INSTANCE_ID", "AUTO_CODER_CHANNEL", "AUTO_CODER_REPOSITORY")
        values = {name: os.environ.get(name, "").strip() for name in names}
        if not all(values.values()):
            raise ValueError("supervisor instance, channel, and repository are required")
        runtime = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", "/runtime"))
        return cls(
            values[names[0]],
            values[names[1]],
            values[names[2]],
            runtime,
            Path(os.environ.get("AUTO_CODER_WORKSPACE_ROOT", "/workspace")),
            Path(os.environ.get("AUTO_CODER_BOOTSTRAP_INTERPRETER", sys.executable)),
            Path(os.environ.get("AUTO_CODER_VERSION_ROOT", runtime / "worker-versions")),
            start_on_boot=os.environ.get("AUTO_CODER_SUPERVISOR_AUTO_START", "1") == "1",
            source_repository=Path(os.environ["AUTO_CODER_SOURCE_REPOSITORY_ROOT"]) if "AUTO_CODER_SOURCE_REPOSITORY_ROOT" in os.environ else None,
            bridge_bind_address=os.environ.get("AUTO_CODER_BRIDGE_BIND_ADDRESS"),
            bridge_port=int(os.environ["AUTO_CODER_BRIDGE_PORT"]) if os.environ.get("AUTO_CODER_BRIDGE_PORT", "").isdigit() else None,
            bridge_secret_file=Path(os.environ["AUTO_CODER_BRIDGE_SECRET_FILE"]) if "AUTO_CODER_BRIDGE_SECRET_FILE" in os.environ else None,
        )


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path) -> None:
        super().__init__("localhost", timeout=2)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class WorkerSupervisor:
    """Durable single-child state machine and private typed-operation server."""

    _REQUEST_FIELDS = frozenset(
        {
            "operation_id",
            "operation",
            "expected_supervisor_incarnation",
            "expected_process_run_id",
            "expected_generation",
            "expected_runtime_id",
            "runtime_id",
        }
    )

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.store = SupervisorStore(config.runtime_root / "supervisor" / "state.json")
        self.child: subprocess.Popen[bytes] | None = None
        self.incarnation = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._shutdown_requested = False
        self._active_preparations: set[str] = set()

    @property
    def control_socket(self) -> Path:
        return self.config.runtime_root / "supervisor" / "control.sock"

    @property
    def worker_socket(self) -> Path:
        return self.config.runtime_root / "management.sock"

    @staticmethod
    def _start_token(pid: int) -> str:
        # The comm field is parenthesized and may contain spaces/parentheses.
        # Parse after its final ")" so the Linux starttime field stays stable.
        record = Path(f"/proc/{pid}/stat").read_text()
        closing = record.rfind(") ")
        if closing < 0:
            raise ValueError("invalid Linux process stat")
        fields = record[closing + 2 :].split()
        if len(fields) <= 19:
            raise ValueError("incomplete Linux process stat")
        if fields[0] in {"Z", "X"}:
            raise ProcessLookupError(pid)
        return fields[19]

    def _is_alive(self, worker: object) -> bool:
        if not isinstance(worker, dict):
            return False
        try:
            return self._start_token(int(worker["pid"])) == worker["start_token"]
        except (OSError, KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _digest(request: dict[str, Any]) -> str:
        keys = ("operation", "expected_supervisor_incarnation", "expected_process_run_id", "expected_generation", "expected_runtime_id", "runtime_id")
        return hashlib.sha256(json.dumps({key: request.get(key) for key in keys}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _receipt(self, worker: dict[str, Any]) -> bool:
        if not worker.get("stop_requested") or not worker.get("process_run_id"):
            return False
        try:
            value = json.loads(self.worker_socket.with_name("last-terminal.json").read_text())
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(value, dict):
            return False
        expected = {"schema_version": 1, "repository": self.config.repository, "channel": self.config.channel, "deployment_generation": worker.get("deployment_generation"), "process_run_id": worker.get("process_run_id"), "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}
        return all(value.get(key) == expected_value for key, expected_value in expected.items())

    def worker_http(self, worker: dict[str, Any], method: str, target: str, body: bytes = b"") -> tuple[int, dict[str, Any]] | None:
        """Read one bounded response from the actual, PID-verified worker socket."""
        connection = _UnixHTTPConnection(self.worker_socket)
        try:
            before = self._start_token(int(worker["pid"]))
            if before != worker["start_token"]:
                return None
            connection.connect()
            assert connection.sock is not None
            peer_pid = struct.unpack("3i", connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))[0]
            if peer_pid != worker["pid"]:
                return None
            headers = {"Content-Type": "application/json"} if method == "POST" else {}
            connection.request(method, target, body=body if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                return None
            value = json.loads(raw)
            producer_run = response.getheader("Auto-Coder-Process-Run-Id")
            producer_generation = response.getheader("Auto-Coder-Deployment-Generation")
            after = self._start_token(int(worker["pid"]))
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            return None
        finally:
            connection.close()
        if (
            before != after
            or not isinstance(value, dict)
            or producer_generation != worker["deployment_generation"]
            or not isinstance(producer_run, str)
            or not 1 <= len(producer_run) <= 128
            or producer_run == "unavailable"
            or worker.get("process_run_id") not in (None, producer_run)
            or (target.startswith("/v1/observation?") and value.get("process_run_id") != producer_run)
        ):
            return None
        return response.status, value

    def _observation(self, worker: dict[str, Any], nonce: str | None = None) -> dict[str, Any] | None:
        nonce = nonce or uuid.uuid4().hex
        result = self.worker_http(worker, "GET", "/v1/observation?" + urlencode({"nonce": nonce}))
        if result is None or result[0] != 200:
            return None
        body = result[1]
        valid = (
            body.get("schema_version") == 1
            and body.get("nonce") == nonce
            and body.get("repository") == self.config.repository
            and body.get("channel") == self.config.channel
            and body.get("deployment_generation") == worker["deployment_generation"]
            and isinstance(body.get("process_run_id"), str)
            and 1 <= len(body["process_run_id"]) <= 128
        )
        return body if valid else None

    @staticmethod
    def _artifact_matches_ready(artifact: object, ready: dict[str, Any]) -> bool:
        if not isinstance(artifact, dict):
            return False
        source, version = artifact.get("source_revision"), artifact.get("distribution_version")
        return (
            isinstance(source, dict)
            and isinstance(version, dict)
            and source.get("available") is True
            and version.get("available") is True
            and source.get("value") == ready["source_sha"] == ready["installed_provenance"]["source_revision"]
            and version.get("value") == ready["distribution_version"] == ready["installed_provenance"]["distribution_version"]
        )

    def _reconcile_worker(self, state: dict[str, Any]) -> None:
        worker = state.get("worker")
        if not isinstance(worker, dict):
            return
        alive = self._is_alive(worker)
        # A binding describes the currently observed OS lifetime, never merely
        # the last successful observation.  Invalidate it before any liveness
        # branch so a crashed/replaced child cannot retain VERIFIED state.
        worker.pop("runtime_binding", None)
        if not alive:
            worker["binding_status"] = "UNAVAILABLE"
            self.store.write(state)
        if alive:
            observation = self._observation(worker)
            if observation:
                if worker.get("process_run_id") not in (None, observation["process_run_id"]):
                    worker["binding_status"] = "CONFLICT"
                    self.store.write(state)
                    return
                worker["process_run_id"] = observation["process_run_id"]
                worker["binding_observed_at"] = time.time()
                worker["observed_artifact"] = observation.get("artifact")
                if worker["runtime_id"] == "bootstrap":
                    worker["binding_status"] = "UNAVAILABLE"
                elif state.get("selected_runtime_id") != worker["runtime_id"]:
                    worker["binding_status"] = "CONFLICT"
                else:
                    selected = self._verify_runtime({"runtime_id": worker["runtime_id"], "interpreter_path": worker["ready"].get("interpreter_path"), "ready": worker["ready"], "ready_path": worker.get("ready_path")}) if isinstance(worker.get("ready"), dict) else None
                    if selected is None:
                        worker["binding_status"] = "UNAVAILABLE"
                    elif not self._artifact_matches_ready(observation.get("artifact"), worker["ready"]):
                        worker["binding_status"] = "CONFLICT"
                    elif not self._is_alive(worker):
                        worker["binding_status"] = "UNAVAILABLE"
                    else:
                        worker["binding_status"] = "VERIFIED"
                        worker["runtime_binding"] = {
                            "runtime_id": worker["runtime_id"],
                            "content_identity": worker["ready"]["content_identity"],
                            "lock_sha256": worker["ready"]["lock_sha256"],
                            "source_sha": worker["ready"]["source_sha"],
                            "distribution_version": worker["ready"]["distribution_version"],
                            "process_run_id": worker["process_run_id"],
                            "deployment_generation": worker["deployment_generation"],
                            "start_token": worker["start_token"],
                            "observed_at": worker["binding_observed_at"],
                        }
            else:
                worker["binding_status"] = "UNAVAILABLE"
            self.store.write(state)
        if not alive and self.child is not None and self.child.pid == worker["pid"]:
            self.child.poll()
        if not alive and self._receipt(worker):
            worker["ended_at"] = time.time()
            state.update(last_worker=worker, worker=None, stopped=True, stop_verified=True)
            self.store.write(state)

    def inspect(self) -> dict[str, Any]:
        with self._lock:
            try:
                state = self.store.read()
            except (OSError, ValueError) as error:
                logger.error("Supervisor state unavailable during inspection: {}", type(error).__name__)
                return {"status": "NEEDS_ATTENTION", "supervisor_incarnation": self.incarnation, "worker": None, "alive": False}
            self._reconcile_worker(state)
            worker, alive = state.get("worker"), self._is_alive(state.get("worker"))
            status = "DRAINING" if alive and isinstance(worker, dict) and worker.get("stop_requested") else "RUNNING" if alive else "UNKNOWN" if worker is not None else "STOPPED" if state.get("stopped") else "UNKNOWN"
            return {"status": status, "supervisor_incarnation": self.incarnation, "selected_runtime_id": state.get("selected_runtime_id"), "worker": worker, "alive": alive}

    @staticmethod
    def project_worker(worker: object) -> dict[str, Any] | None:
        """Expose identity and genuine READY binding, never local installation paths."""
        if not isinstance(worker, dict):
            return None
        allowed = ("process_run_id", "deployment_generation", "runtime_id", "binding_status", "runtime_binding", "stop_requested")
        return {key: worker[key] for key in allowed if key in worker}

    @classmethod
    def project_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        projected: dict[str, Any] = {key: result[key] for key in ("status", "supervisor_incarnation", "selected_runtime_id", "alive", "reason") if key in result}
        if "worker" in result:
            projected["worker"] = cls.project_worker(result["worker"])
        return projected

    def observe_worker(self, nonce: str, *, expected_process_run_id: str, expected_deployment_generation: str) -> tuple[str, dict[str, Any] | None]:
        """Preserve the signed worker fence through one atomic UDS/READY join."""
        with self._lock:
            inspection = self.inspect()
            worker = inspection.get("worker")
            if not inspection["alive"] or not isinstance(worker, dict):
                return "UNAVAILABLE", None
            if worker.get("process_run_id") != expected_process_run_id or worker.get("deployment_generation") != expected_deployment_generation:
                return "CONFLICT", None
            observed = self._observation(worker, nonce)
            if observed is None:
                return "UNAVAILABLE", None
            if observed.get("process_run_id") != expected_process_run_id or observed.get("deployment_generation") != expected_deployment_generation:
                return "CONFLICT", None
            binding_status = "UNAVAILABLE"
            binding = None
            if worker["runtime_id"] != "bootstrap":
                state = self.store.read()
                selected = self._resolve_runtime(worker["runtime_id"])
                if state.get("selected_runtime_id") != worker["runtime_id"]:
                    binding_status = "CONFLICT"
                elif selected is not None and isinstance(worker.get("ready"), dict):
                    if selected.get("ready") == worker["ready"] and self._artifact_matches_ready(observed.get("artifact"), worker["ready"]):
                        if self._is_alive(worker):
                            ready = worker["ready"]
                            binding = {
                                "runtime_id": worker["runtime_id"],
                                "content_identity": ready["content_identity"],
                                "lock_sha256": ready["lock_sha256"],
                                "source_sha": ready["source_sha"],
                                "distribution_version": ready["distribution_version"],
                                "process_run_id": observed["process_run_id"],
                                "deployment_generation": observed["deployment_generation"],
                            }
                            binding_status = "VERIFIED"
                    else:
                        binding_status = "CONFLICT"
            return "OK", {"observation": observed, "binding_status": binding_status, "runtime_binding": binding}

    def read_operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                state = self.store.read()
            except (OSError, ValueError):
                return {"status": "UNKNOWN"}
            record = state["operations"].get(operation_id)
            if record is None:
                return {"status": "NOT_FOUND"}
            result = self.project_result(self._reconcile_operation(operation_id, state))
            result.update(operation_id=operation_id, intent_digest=record["digest"])
            return result

    def _version_preparer(self) -> WorkerVersionPreparer:
        if self.config.source_repository is None:
            raise ValueError("trusted source repository is not configured")
        return WorkerVersionPreparer(
            WorkerVersionConfig(
                source_repository=self.config.source_repository,
                version_root=self.config.version_root or self.config.runtime_root / "worker-versions",
                python_executable=self.config.interpreter,
                uv_executable=Path("/usr/local/bin/uv"),
                uv_version="0.7.22",
                command_timeout=600,
            )
        )

    @staticmethod
    def _ready_projection(receipt: Any) -> dict[str, Any]:
        return {
            "status": "READY",
            "installed_runtime_id": receipt.installed_runtime_id,
            "source_sha": receipt.source_sha,
            "lock_sha256": receipt.lock_sha256,
            "content_identity": receipt.content_identity,
            "distribution_version": receipt.distribution_version,
            "runtime_profile": receipt.runtime_profile,
        }

    def prepare_version(self, operation_id: str, source_sha: str) -> dict[str, Any]:
        """Durably admit a fixed-source stage, with asynchronous typed readback."""
        if not _OPERATION_ID.fullmatch(operation_id) or not _FULL_SHA.fullmatch(source_sha):
            return {"status": "UNSUPPORTED"}
        if self.config.source_repository is None:
            return {"status": "UNAVAILABLE"}
        digest = hashlib.sha256(f"{operation_id}\0{source_sha}".encode()).hexdigest()
        attempt_id = hashlib.sha256(f"worker-preparation\0{operation_id}".encode()).hexdigest()[:32]
        with self._lock:
            try:
                state = self.store.read()
            except (OSError, ValueError):
                return {"status": "UNKNOWN"}
            if operation_id in state["operations"]:
                return {"status": "CONFLICT"}
            preparations = state.setdefault("preparations", {})
            prior = preparations.get(operation_id)
            if prior is not None:
                return self.read_preparation(operation_id) if prior.get("digest") == digest else {"status": "CONFLICT"}
            if any(record.get("result", {}).get("status") == "IN_PROGRESS" for record in preparations.values()):
                return {"status": "CONFLICT"}
            preparations[operation_id] = {"digest": digest, "source_sha": source_sha, "attempt_id": attempt_id, "result": {"status": "IN_PROGRESS"}}
            self.store.write(state)
            self._active_preparations.add(operation_id)
            thread = threading.Thread(target=self._complete_preparation, args=(operation_id, source_sha, attempt_id), daemon=True, name="worker-version-preparation")
            thread.start()
            return {"status": "IN_PROGRESS", "operation_id": operation_id, "source_sha": source_sha}

    def _complete_preparation(self, operation_id: str, source_sha: str, attempt_id: str) -> None:
        try:
            prepared = self._version_preparer().prepare(PrepareIntent(source_sha, attempt_id))
            result = self._ready_projection(prepared.receipt)
        except WorkerVersionError as error:
            result = {"status": "FAILED", "reason": error.failure.value}
        except Exception as error:
            logger.error("Worker version preparation unavailable: {}", type(error).__name__)
            result = {"status": "UNKNOWN"}
        with self._lock:
            try:
                state = self.store.read()
                record = state.setdefault("preparations", {}).get(operation_id)
                if record is not None and record.get("source_sha") == source_sha:
                    record["result"] = result
                    self.store.write(state)
            except (OSError, ValueError) as error:
                logger.error("Worker preparation receipt storage unavailable: {}", type(error).__name__)
            finally:
                self._active_preparations.discard(operation_id)

    def read_preparation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                state = self.store.read()
            except (OSError, ValueError):
                return {"status": "UNKNOWN"}
            record = state.get("preparations", {}).get(operation_id)
            if record is None:
                return {"status": "NOT_FOUND"}
            result = record["result"]
            if result.get("status") != "IN_PROGRESS" or operation_id in self._active_preparations:
                return {**result, "operation_id": operation_id, "source_sha": record["source_sha"]}
            # A new Supervisor lifetime has no owner for the old effect. Only
            # an independently verified READY at the exact attempt can recover it.
            try:
                preparer = self._version_preparer()
                matches = list(preparer.root.glob(f"revisions/*/{record['attempt_id']}/READY.json"))
                if len(matches) == 1:
                    receipt = preparer.read_ready(matches[0])
                    if receipt.source_sha == record["source_sha"]:
                        result = self._ready_projection(receipt)
                    else:
                        result = {"status": "CONFLICT"}
                else:
                    result = {"status": "UNKNOWN"}
            except (OSError, ValueError, WorkerVersionError):
                result = {"status": "UNKNOWN"}
            record["result"] = result
            self.store.write(state)
            return {**result, "operation_id": operation_id, "source_sha": record["source_sha"]}

    def _fences_match(self, request: dict[str, Any], state: dict[str, Any]) -> bool:
        if "expected_supervisor_incarnation" in request and request["expected_supervisor_incarnation"] != self.incarnation:
            return False
        worker = state.get("worker")
        current = (
            worker.get("process_run_id") if isinstance(worker, dict) else None,
            worker.get("deployment_generation") if isinstance(worker, dict) else None,
            worker.get("runtime_id") if isinstance(worker, dict) else state.get("selected_runtime_id"),
        )
        return all(key not in request or request[key] == value for key, value in zip(("expected_process_run_id", "expected_generation", "expected_runtime_id"), current))

    @staticmethod
    def _unresolved(state: dict[str, Any], excluding: str) -> bool:
        statuses = {"IN_PROGRESS", "DRAINING", "UNKNOWN", "NEEDS_ATTENTION"}
        return any(key != excluding and record.get("result", {}).get("status") in statuses for key, record in state["operations"].items())

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        operation_id, operation = request.get("operation_id"), request.get("operation")
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128 or operation not in {"INSPECT", "START", "REQUEST_GRACEFUL_STOP", "RESTART_WORKER", "RECOVER_PREVIOUS"}:
            return {"status": "UNSUPPORTED"}
        if not request.keys() <= self._REQUEST_FIELDS:
            # A caller may supply identity fences and a runtime ID, but not
            # executable arguments, users, paths, networks, or new targets.
            return {"status": "UNSUPPORTED"}
        for name in ("expected_supervisor_incarnation", "expected_process_run_id", "expected_generation", "expected_runtime_id"):
            value = request.get(name)
            if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
                return {"status": "UNSUPPORTED"}

        digest = self._digest(request)
        with self._lock:
            if self._shutdown_requested and operation in {"START", "RESTART_WORKER"}:
                return {"status": "CONFLICT"}
            try:
                state = self.store.read()
            except (OSError, ValueError) as error:
                logger.error("Supervisor state unavailable for operation: {}", type(error).__name__)
                return {"status": "NEEDS_ATTENTION"}
            if operation_id in state.get("preparations", {}):
                return {"status": "CONFLICT"}
            prior = state["operations"].get(operation_id)
            if prior:
                return self._reconcile_operation(operation_id, state) if prior["digest"] == digest else {"status": "CONFLICT"}
            if operation == "INSPECT":
                return self.inspect()
            self._reconcile_worker(state)
            if not self._fences_match(request, state):
                return {"status": "CONFLICT"}
            if self._unresolved(state, operation_id):
                return {"status": "NEEDS_ATTENTION"}

        # READY verification can execute an interpreter from a prepared runtime.
        # Keep it outside the lifecycle lock so INSPECT and safe stop requests for
        # the current child remain available while that bounded probe runs.
        runtime = self._resolve_runtime(request.get("runtime_id", "bootstrap")) if operation in {"START", "RESTART_WORKER", "RECOVER_PREVIOUS"} else None
        if operation in {"START", "RESTART_WORKER", "RECOVER_PREVIOUS"} and runtime is None:
            return {"status": "UNAVAILABLE"}

        with self._lock:
            try:
                state = self.store.read()
            except (OSError, ValueError) as error:
                logger.error("Supervisor state unavailable for operation: {}", type(error).__name__)
                return {"status": "NEEDS_ATTENTION"}
            prior = state["operations"].get(operation_id)
            if prior:
                return self._reconcile_operation(operation_id, state) if prior["digest"] == digest else {"status": "CONFLICT"}
            self._reconcile_worker(state)
            if not self._fences_match(request, state):
                return {"status": "CONFLICT"}
            if self._unresolved(state, operation_id):
                return {"status": "NEEDS_ATTENTION"}
            state["operations"][operation_id] = {"digest": digest, "request": dict(request), "runtime": runtime, "effect_generation": uuid.uuid4().hex, "result": {"status": "IN_PROGRESS"}}
            self.store.write(state)
            if not self._fences_match(request, state):
                return self._finish(operation_id, state, {"status": "CONFLICT"})
            if operation == "RECOVER_PREVIOUS":
                result = self._recover_previous(state, runtime, operation_id)
            else:
                result = self._start(state, runtime, operation_id) if operation == "START" else self._stop(state)
            if operation == "RESTART_WORKER" and result["status"] == "STOPPED":
                result = self._start(state, runtime, operation_id)
            return self._finish(operation_id, state, result)

    def _recover_previous(self, state: dict[str, Any], runtime: dict[str, Any] | None, operation_id: str) -> dict[str, Any]:
        """One separately journaled prior-READY recovery after actual child exit."""
        failed, previous = state.get("worker"), state.get("last_worker")
        if (
            not isinstance(failed, dict)
            or not isinstance(previous, dict)
            or self._is_alive(failed)
            or runtime is None
            or runtime.get("runtime_id") == "bootstrap"
            or previous.get("runtime_id") != runtime.get("runtime_id")
            or previous.get("ready") != runtime.get("ready")
            or state.get("selected_runtime_id") != failed.get("runtime_id")
            or state.get("prior_recovery_generation") == failed.get("deployment_generation")
        ):
            return {"status": "CONFLICT"}
        if self._verify_runtime(runtime) is None:
            return {"status": "UNAVAILABLE"}
        # No SIGKILL or forced drain is permitted. Only the exact exited OS
        # lifetime may be replaced; the attempt is durable before Popen.
        state.update(prior_recovery_attempted=True, prior_recovery_generation=failed["deployment_generation"], failed_worker=failed, worker=None, stopped=True, stop_verified=False)
        self.store.write(state)
        return self._start(state, runtime, operation_id)

    def _finish(self, operation_id: str, state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        record = state["operations"][operation_id]
        record["result"] = result
        self.store.write(state)
        worker = state.get("worker") or state.get("last_worker")
        identity = worker if isinstance(worker, dict) else {}
        logger.info(
            "Supervisor operation={} id_hash={} status={} pid={} generation={}",
            record["request"]["operation"],
            hashlib.sha256(operation_id.encode()).hexdigest()[:12],
            result.get("status"),
            identity.get("pid"),
            identity.get("deployment_generation"),
        )
        return result

    def _reconcile_operation(self, operation_id: str, state: dict[str, Any]) -> dict[str, Any]:
        record, result = state["operations"][operation_id], state["operations"][operation_id]["result"]
        if result.get("status") not in {"IN_PROGRESS", "DRAINING", "UNKNOWN"}:
            return result
        worker = state.get("worker")
        successor_launch = result.get("status") == "DRAINING" and record["request"]["operation"] == "RESTART_WORKER" and isinstance(worker, dict) and worker.get("launch_operation_id") == operation_id
        if result.get("status") == "IN_PROGRESS" or successor_launch:
            if (
                record["request"]["operation"] in {"START", "RESTART_WORKER", "RECOVER_PREVIOUS"}
                and isinstance(worker, dict)
                and worker.get("launch_operation_id") == operation_id
                and isinstance(record.get("runtime"), dict)
                and record["runtime"].get("runtime_id") == worker.get("runtime_id")
                and record["runtime"].get("ready") == worker.get("ready")
                and record["runtime"].get("ready_path") == worker.get("ready_path")
                and state.get("selected_runtime_id") == worker.get("runtime_id")
                and self._is_alive(worker)
            ):
                observation = self._observation(worker)
                if observation is None or worker.get("process_run_id") not in (None, observation["process_run_id"]):
                    return result
                self._reconcile_worker(state)
                worker = state.get("worker")
                if isinstance(worker, dict) and worker.get("process_run_id") == observation["process_run_id"] and self._is_alive(worker):
                    return self._finish(operation_id, state, {"status": "RUNNING", "worker": worker})
                return result
            if successor_launch:
                return result
            return self._finish(operation_id, state, {"status": "NEEDS_ATTENTION"})
        self._reconcile_worker(state)
        if state.get("worker") is not None:
            return result
        if not state.get("stop_verified"):
            return self._finish(operation_id, state, {"status": "UNKNOWN"})
        if record["request"]["operation"] == "RESTART_WORKER":
            return self._finish(operation_id, state, self._start(state, record.get("runtime"), operation_id))
        return self._finish(operation_id, state, {"status": "STOPPED"})

    def _resolve_runtime(self, runtime_id: object) -> dict[str, Any] | None:
        if runtime_id == "bootstrap":
            return {"runtime_id": "bootstrap", "interpreter_path": str(self.config.interpreter), "ready": None}
        if not isinstance(runtime_id, str) or not runtime_id.isalnum() or len(runtime_id) > 128:
            return None
        root = self.config.version_root or self.config.runtime_root / "worker-versions"
        matches = []
        for path in root.glob("revisions/*/*/READY.json"):
            try:
                if json.loads(path.read_text()).get("installed_runtime_id") == runtime_id:
                    matches.append(path)
            except (OSError, ValueError):
                pass
        if len(matches) != 1:
            return None
        preparer = WorkerVersionPreparer(
            WorkerVersionConfig(
                self.config.workspace_root,
                root,
                self.config.interpreter,
                Path("/usr/local/bin/uv"),
                "0.7.22",
                command_timeout=2.0,
            )
        )
        try:
            receipt = preparer.read_ready(matches[0])
        except WorkerVersionError:
            return None
        if receipt.runtime_profile != preparer.config.runtime_profile or receipt.installed_runtime_id != runtime_id:
            return None
        return {"runtime_id": runtime_id, "interpreter_path": receipt.interpreter_path, "ready": asdict(receipt), "ready_path": str(matches[0].resolve())}

    def _verify_runtime(self, runtime: dict[str, Any] | None) -> dict[str, Any] | None:
        """Revalidate the originally accepted selection at the actual effect boundary."""
        if runtime is None:
            return None
        if runtime.get("runtime_id") == "bootstrap":
            return runtime if runtime == self._resolve_runtime("bootstrap") and self.config.interpreter.is_file() else None
        selected = self._resolve_runtime(runtime.get("runtime_id"))
        return selected if selected == runtime else None

    def _start(self, state: dict[str, Any], runtime: dict[str, Any] | None, operation_id: str) -> dict[str, Any]:
        if runtime is None or state.get("worker") is not None:
            return {"status": "CONFLICT"}
        if self._verify_runtime(runtime) is None:
            return {"status": "UNAVAILABLE"}
        generation = uuid.uuid4().hex
        environment = os.environ.copy()
        environment.update(
            AUTO_CODER_DEPLOYMENT_GENERATION=generation,
            AUTO_CODER_MANAGEMENT_SOCKET=str(self.worker_socket),
            AUTO_CODER_REPOSITORY=self.config.repository,
            AUTO_CODER_CHANNEL=self.config.channel,
            AUTO_CODER_RUNTIME_ROOT=str(self.config.runtime_root),
            AUTO_CODER_WORKSPACE_ROOT=str(self.config.workspace_root),
        )
        if runtime["runtime_id"] != "bootstrap":
            # Prevent an inherited Python override from importing bootstrap code
            # while a staged interpreter claims to execute an immutable install.
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            environment["VIRTUAL_ENV"] = runtime["ready"]["environment_path"]
            environment["PATH"] = str(Path(runtime["ready"]["environment_path"]) / "bin") + os.pathsep + environment.get("PATH", "")
        command = list(self.config.child_command) if self.config.child_command else [runtime["interpreter_path"], "-m", "auto_coder.cli", "process-issues", "--repo", self.config.repository, "--log-file", str(self.config.runtime_root / "logs" / "auto-coder.log")]
        state.update(selected_runtime_id=runtime["runtime_id"], stop_verified=False, launch_intent={"generation": generation, "runtime": runtime, "at": time.time()})
        self.store.write(state)
        try:
            self.child = subprocess.Popen(command, cwd=self.config.workspace_root, env=environment, start_new_session=False)
        except OSError:
            # The pre-spawn intent is deliberately retained for safe reconstruction.
            return {"status": "UNKNOWN", "reason": "child launch unavailable"}
        state["launch_intent"]["pid"] = self.child.pid
        self.store.write(state)
        try:
            start_token = self._start_token(self.child.pid)
        except OSError as error:
            # The child can exit between Popen and the reuse-safe /proc sample.  The
            # process effect is then ambiguous: retain its durable launch intent so
            # reconstruction cannot spawn a replacement, but keep PID 1 available.
            state["launch_intent"].update(
                lifetime_status="unavailable",
                observed_at=time.time(),
                error=type(error).__name__,
            )
            self.store.write(state)
            return {
                "status": "UNKNOWN",
                "reason": "child lifetime unavailable during startup",
            }
        worker = {"pid": self.child.pid, "start_token": start_token, "process_run_id": None, "deployment_generation": generation, "runtime_id": runtime["runtime_id"], "ready": runtime["ready"], "ready_path": runtime.get("ready_path"), "started_at": time.time(), "launch_operation_id": operation_id}
        state.update(worker=worker, stopped=False, launch_intent=None)
        self.store.write(state)
        return {"status": "RUNNING", "worker": worker}

    def _stop(self, state: dict[str, Any]) -> dict[str, Any]:
        worker = state.get("worker")
        if not isinstance(worker, dict):
            return {"status": "STOPPED"} if state.get("stop_verified") or state.get("stopped") else {"status": "UNKNOWN"}
        if not self._is_alive(worker):
            self._reconcile_worker(state)
            return {"status": "STOPPED"} if state.get("worker") is None else {"status": "UNKNOWN", "worker": worker}
        if not worker.get("stop_requested"):
            worker.update(stop_requested=True, stop_requested_at=time.time())
            self.store.write(state)
            # Use an fd for the actual process lifetime. Checking /proc and then
            # calling kill(pid) leaves a PID-reuse window even on a private host.
            try:
                pid_fd = os.pidfd_open(int(worker["pid"]))
                try:
                    if self._start_token(int(worker["pid"])) != worker["start_token"]:
                        return {"status": "UNKNOWN", "worker": worker}
                    signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
                finally:
                    os.close(pid_fd)
            except OSError:
                return {"status": "UNKNOWN", "worker": worker}
        return {"status": "DRAINING", "worker": worker}

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(2)
            stream = connection.makefile("rwb")
            try:
                request = json.loads(stream.readline(256 * 1024))
                response = self.apply(request) if isinstance(request, dict) else {"status": "UNSUPPORTED"}
            except (OSError, ValueError, json.JSONDecodeError):
                response = {"status": "UNSUPPORTED"}
            except Exception as error:
                logger.error("Supervisor request failed: {}", type(error).__name__)
                response = {"status": "UNKNOWN"}
            try:
                stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                stream.flush()
            except OSError:
                # A dropped client can raise EPIPE or ECONNRESET. Neither may
                # end the long-lived Supervisor request service.
                pass

    def run(self) -> None:
        """Hold one exclusive runtime owner for the lifetime of the server."""
        self.control_socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.control_socket.parent / "owner.lock"
        owner_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Supervisor runtime is already owned") from error
            self._run_exclusive()
        finally:
            os.close(owner_fd)

    def _run_exclusive(self) -> None:
        """Serve requests without letting a contender replace our socket."""
        from .worker_supervisor_bridge import SupervisorBridgeConfig, SupervisorBridgeServer

        bridge_config = SupervisorBridgeConfig.from_supervisor(self)
        try:
            details = self.control_socket.lstat()
            if stat.S_ISSOCK(details.st_mode):
                self.control_socket.unlink()
            else:
                raise RuntimeError("control endpoint is not a socket")
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.control_socket))
        os.chmod(self.control_socket, 0o600)
        listener.listen(8)
        listener.settimeout(0.25)
        previous_handler = None
        if threading.current_thread() is threading.main_thread():
            previous_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_shutdown)
        bridge = None
        try:
            if bridge_config is not None:
                bridge = SupervisorBridgeServer(bridge_config, self)
                bridge.start()
            if self.config.start_on_boot and not self.store.path.exists():
                # A fresh channel container retains its previous automatic worker
                # startup. A persisted stop/crash never triggers a resurrection.
                result = self.apply({"operation_id": "bootstrap-initial-start", "operation": "START"})
                logger.info("Initial worker start: {}", result.get("status"))
            shutdown_operation = {"operation_id": "container-shutdown-" + self.incarnation, "operation": "REQUEST_GRACEFUL_STOP"}
            while True:
                if self._shutdown_requested:
                    result = self.apply(shutdown_operation)
                    state = self.store.read()
                    if state.get("worker") is None or not self._is_alive(state.get("worker")):
                        logger.info("Supervisor container shutdown; worker status: {}", result.get("status"))
                        return
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                self._serve(connection)
        finally:
            if bridge is not None:
                bridge.close()
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
            listener.close()
            self.control_socket.unlink(missing_ok=True)

    def _handle_shutdown(self, _signum: int, _frame: object) -> None:
        self._shutdown_requested = True


def main() -> None:
    config = SupervisorConfig.environment()
    directory = config.runtime_root / "supervisor"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stdout, colorize=sys.stdout.isatty(), format=layout)
    logger.add(directory / "supervisor.log", rotation="10 MB", retention="5 days", format=layout)
    WorkerSupervisor(config).run()


if __name__ == "__main__":
    main()
