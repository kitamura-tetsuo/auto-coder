"""Fixed, Docker-independent PID-1 supervisor for one Auto-Coder worker."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .worker_supervisor_store import SupervisorStore
from .worker_versions import WorkerVersionConfig, WorkerVersionError, WorkerVersionPreparer


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

    @classmethod
    def environment(cls) -> "SupervisorConfig":
        names = ("AUTO_CODER_SUPERVISOR_INSTANCE_ID", "AUTO_CODER_CHANNEL", "AUTO_CODER_REPOSITORY")
        values = {name: os.environ.get(name, "").strip() for name in names}
        if not all(values.values()):
            raise ValueError("supervisor instance, channel, and repository are required")
        runtime = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", "/runtime"))
        return cls(values[names[0]], values[names[1]], values[names[2]], runtime, Path(os.environ.get("AUTO_CODER_WORKSPACE_ROOT", "/workspace")), Path(os.environ.get("AUTO_CODER_BOOTSTRAP_INTERPRETER", sys.executable)), Path(os.environ.get("AUTO_CODER_VERSION_ROOT", runtime / "worker-versions")))


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

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.store = SupervisorStore(config.runtime_root / "supervisor" / "state.json")
        self.child: subprocess.Popen[bytes] | None = None
        self.incarnation = uuid.uuid4().hex
        self._lock = threading.RLock()

    @property
    def control_socket(self) -> Path:
        return self.config.runtime_root / "supervisor" / "control.sock"

    @property
    def worker_socket(self) -> Path:
        return self.config.runtime_root / "management.sock"

    @staticmethod
    def _start_token(pid: int) -> str:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        if fields[2] == "Z":
            raise ProcessLookupError(pid)
        return fields[21]

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
        try:
            value = json.loads(self.worker_socket.with_name("last-terminal.json").read_text())
        except (OSError, ValueError, TypeError):
            return False
        expected = {"schema_version": 1, "repository": self.config.repository, "channel": self.config.channel, "deployment_generation": worker.get("deployment_generation"), "process_run_id": worker.get("process_run_id"), "lifecycle_state": "stopped", "is_graceful_ready": True, "unsettled_count": 0}
        return all(value.get(key) == expected_value for key, expected_value in expected.items())

    def _observation(self, worker: dict[str, Any]) -> dict[str, Any] | None:
        nonce = uuid.uuid4().hex
        try:
            before = self._start_token(int(worker["pid"]))
            connection = _UnixHTTPConnection(self.worker_socket)
            connection.request("GET", f"/v1/observation?nonce={nonce}")
            response = connection.getresponse()
            body = json.loads(response.read()) if response.status == 200 else None
            connection.close()
            after = self._start_token(int(worker["pid"]))
        except (OSError, ValueError, http.client.HTTPException):
            return None
        valid = (
            isinstance(body, dict)
            and before == after == worker["start_token"]
            and body.get("nonce") == nonce
            and body.get("repository") == self.config.repository
            and body.get("channel") == self.config.channel
            and body.get("deployment_generation") == worker["deployment_generation"]
            and isinstance(body.get("process_run_id"), str)
        )
        return body if valid else None

    def _reconcile_worker(self, state: dict[str, Any]) -> None:
        worker = state.get("worker")
        if not isinstance(worker, dict):
            return
        alive = self._is_alive(worker)
        if alive and not worker.get("process_run_id"):
            observation = self._observation(worker)
            if observation:
                worker["process_run_id"] = observation["process_run_id"]
                worker["binding_observed_at"] = time.time()
                worker["runtime_binding"] = observation.get("artifact")
                self.store.write(state)
        if not alive and self._receipt(worker):
            state.update(last_worker=worker, worker=None, stopped=True, stop_verified=True)
            self.store.write(state)

    def inspect(self) -> dict[str, Any]:
        with self._lock:
            state = self.store.read()
            self._reconcile_worker(state)
            worker, alive = state.get("worker"), self._is_alive(state.get("worker"))
            status = "DRAINING" if alive and isinstance(worker, dict) and worker.get("stop_requested") else "RUNNING" if alive else "UNKNOWN" if worker is not None else "STOPPED" if state.get("stopped") else "UNKNOWN"
            return {"status": status, "supervisor_incarnation": self.incarnation, "worker": worker, "alive": alive}

    def _fences_match(self, request: dict[str, Any], state: dict[str, Any]) -> bool:
        if request.get("expected_supervisor_incarnation") not in (None, self.incarnation):
            return False
        worker = state.get("worker")
        current = (
            worker.get("process_run_id") if isinstance(worker, dict) else None,
            worker.get("deployment_generation") if isinstance(worker, dict) else None,
            worker.get("runtime_id") if isinstance(worker, dict) else state.get("selected_runtime_id"),
        )
        return all(request.get(key) in (None, value) for key, value in zip(("expected_process_run_id", "expected_generation", "expected_runtime_id"), current))

    @staticmethod
    def _unresolved(state: dict[str, Any], excluding: str) -> bool:
        statuses = {"IN_PROGRESS", "DRAINING", "UNKNOWN", "NEEDS_ATTENTION"}
        return any(key != excluding and record.get("result", {}).get("status") in statuses for key, record in state["operations"].items())

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            operation_id, operation = request.get("operation_id"), request.get("operation")
            if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128 or operation not in {"INSPECT", "START", "REQUEST_GRACEFUL_STOP", "RESTART_WORKER"}:
                return {"status": "UNSUPPORTED"}
            state, digest = self.store.read(), self._digest(request)
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
            runtime = self._resolve_runtime(request.get("runtime_id", "bootstrap")) if operation in {"START", "RESTART_WORKER"} else None
            if operation in {"START", "RESTART_WORKER"} and runtime is None:
                return {"status": "UNAVAILABLE"}
            state["operations"][operation_id] = {"digest": digest, "request": dict(request), "runtime": runtime, "effect_generation": uuid.uuid4().hex, "result": {"status": "IN_PROGRESS"}}
            self.store.write(state)
            if not self._fences_match(request, state):
                return self._finish(operation_id, state, {"status": "CONFLICT"})
            result = self._start(state, runtime) if operation == "START" else self._stop(state)
            if operation == "RESTART_WORKER" and result["status"] == "STOPPED":
                result = self._start(state, runtime)
            return self._finish(operation_id, state, result)

    def _finish(self, operation_id: str, state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        state["operations"][operation_id]["result"] = result
        self.store.write(state)
        return result

    def _reconcile_operation(self, operation_id: str, state: dict[str, Any]) -> dict[str, Any]:
        record, result = state["operations"][operation_id], state["operations"][operation_id]["result"]
        if result.get("status") not in {"IN_PROGRESS", "DRAINING", "UNKNOWN"}:
            return result
        if result.get("status") == "IN_PROGRESS":
            return self._finish(operation_id, state, {"status": "NEEDS_ATTENTION"})
        self._reconcile_worker(state)
        if state.get("worker") is not None:
            return result
        if not state.get("stop_verified"):
            return self._finish(operation_id, state, {"status": "UNKNOWN"})
        if record["request"]["operation"] == "RESTART_WORKER":
            return self._finish(operation_id, state, self._start(state, record.get("runtime")))
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
        preparer = WorkerVersionPreparer(WorkerVersionConfig(self.config.workspace_root, root, self.config.interpreter, Path("/usr/local/bin/uv"), "0.7.22"))
        try:
            receipt = preparer.read_ready(matches[0])
        except WorkerVersionError:
            return None
        return {"runtime_id": runtime_id, "interpreter_path": receipt.interpreter_path, "ready": asdict(receipt)}

    def _start(self, state: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
        if runtime is None or state.get("worker") is not None:
            return {"status": "CONFLICT"}
        generation = uuid.uuid4().hex
        environment = os.environ.copy()
        environment.update(AUTO_CODER_DEPLOYMENT_GENERATION=generation, AUTO_CODER_MANAGEMENT_SOCKET=str(self.worker_socket))
        command = list(self.config.child_command) if self.config.child_command else [runtime["interpreter_path"], "-m", "auto_coder.cli", "process-issues", "--repo", self.config.repository, "--log-file", str(self.config.runtime_root / "logs" / "auto-coder.log")]
        state.update(selected_runtime_id=runtime["runtime_id"], stop_verified=False, launch_intent={"generation": generation, "runtime": runtime, "at": time.time()})
        self.store.write(state)
        self.child = subprocess.Popen(command, cwd=self.config.workspace_root, env=environment, start_new_session=False)
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
        worker = {"pid": self.child.pid, "start_token": start_token, "process_run_id": None, "deployment_generation": generation, "runtime_id": runtime["runtime_id"], "ready": runtime["ready"], "started_at": time.time()}
        state.update(worker=worker, stopped=False, launch_intent=None)
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
            if not self._is_alive(worker):
                return {"status": "UNKNOWN", "worker": worker}
            os.kill(int(worker["pid"]), signal.SIGTERM)
        return {"status": "DRAINING", "worker": worker}

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            stream = connection.makefile("rwb")
            try:
                request = json.loads(stream.readline(256 * 1024))
                response = self.apply(request) if isinstance(request, dict) else {"status": "UNSUPPORTED"}
            except (ValueError, json.JSONDecodeError):
                response = {"status": "UNSUPPORTED"}
            try:
                stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                stream.flush()
            except BrokenPipeError:
                pass

    def run(self) -> None:
        """Serve one-request connections forever; EOF/disconnect is not shutdown."""
        self.control_socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        try:
            while True:
                connection, _ = listener.accept()
                self._serve(connection)
        finally:
            listener.close()
            self.control_socket.unlink(missing_ok=True)


def main() -> None:
    WorkerSupervisor(SupervisorConfig.environment()).run()


if __name__ == "__main__":
    main()
