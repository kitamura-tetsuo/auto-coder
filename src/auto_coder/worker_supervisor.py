"""Fixed, Docker-independent PID-1 supervisor for one Auto-Coder worker."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
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

from loguru import logger

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
    start_on_boot: bool = False

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

    def _observation(self, worker: dict[str, Any]) -> dict[str, Any] | None:
        nonce = uuid.uuid4().hex
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
            connection.request("GET", f"/v1/observation?nonce={nonce}")
            response = connection.getresponse()
            body = json.loads(response.read()) if response.status == 200 else None
            after = self._start_token(int(worker["pid"]))
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            connection.close()
        valid = (
            isinstance(body, dict)
            and body.get("schema_version") == 1
            and before == after == worker["start_token"]
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
        if alive:
            worker.pop("runtime_binding", None)
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
            if not request.keys() <= self._REQUEST_FIELDS:
                # A caller may supply identity fences and a runtime ID, but not
                # executable arguments, users, paths, networks, or new targets.
                return {"status": "UNSUPPORTED"}
            for name in ("expected_supervisor_incarnation", "expected_process_run_id", "expected_generation", "expected_runtime_id"):
                value = request.get(name)
                if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 128):
                    return {"status": "UNSUPPORTED"}
            if self._shutdown_requested and operation in {"START", "RESTART_WORKER"}:
                return {"status": "CONFLICT"}
            try:
                state = self.store.read()
            except (OSError, ValueError) as error:
                logger.error("Supervisor state unavailable for operation: {}", type(error).__name__)
                return {"status": "NEEDS_ATTENTION"}
            digest = self._digest(request)
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

    def _start(self, state: dict[str, Any], runtime: dict[str, Any] | None) -> dict[str, Any]:
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
        worker = {"pid": self.child.pid, "start_token": start_token, "process_run_id": None, "deployment_generation": generation, "runtime_id": runtime["runtime_id"], "ready": runtime["ready"], "ready_path": runtime.get("ready_path"), "started_at": time.time()}
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
            except BrokenPipeError:
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
        try:
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
