"""Fixed, Docker-independent PID-1 supervisor for one Auto-Coder worker."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .worker_supervisor_store import SupervisorStore


@dataclass(frozen=True)
class SupervisorConfig:
    instance_id: str
    channel: str
    repository: str
    runtime_root: Path
    workspace_root: Path
    interpreter: Path

    @classmethod
    def environment(cls) -> "SupervisorConfig":
        values = {name: os.environ.get(name, "").strip() for name in ("AUTO_CODER_SUPERVISOR_INSTANCE_ID", "AUTO_CODER_CHANNEL", "AUTO_CODER_REPOSITORY")}
        if not all(values.values()):
            raise ValueError("supervisor instance, channel, and repository are required")
        return cls(
            values["AUTO_CODER_SUPERVISOR_INSTANCE_ID"],
            values["AUTO_CODER_CHANNEL"],
            values["AUTO_CODER_REPOSITORY"],
            Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", "/runtime")),
            Path(os.environ.get("AUTO_CODER_WORKSPACE_ROOT", "/workspace")),
            Path(os.environ.get("AUTO_CODER_BOOTSTRAP_INTERPRETER", sys.executable)),
        )


class WorkerSupervisor:
    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.store = SupervisorStore(config.runtime_root / "supervisor" / "state.json")
        self.child: subprocess.Popen[bytes] | None = None
        self.incarnation = str(uuid.uuid4())

    @staticmethod
    def _start_token(pid: int) -> str:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]

    def _payload_digest(self, request: dict[str, Any]) -> str:
        relevant = {key: request.get(key) for key in ("operation", "expected_supervisor_incarnation", "expected_process_run_id", "expected_generation", "runtime_id")}
        return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def inspect(self) -> dict[str, Any]:
        state = self.store.read()
        worker = state.get("worker")
        alive = False
        if isinstance(worker, dict):
            try:
                alive = self._start_token(int(worker["pid"])) == worker["start_token"]
            except (FileNotFoundError, ProcessLookupError, KeyError, ValueError):
                pass
        return {"status": "RUNNING" if alive else ("STOPPED" if state.get("stopped") else "UNKNOWN"), "supervisor_incarnation": self.incarnation, "worker": worker, "alive": alive}

    def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        operation_id = request.get("operation_id")
        operation = request.get("operation")
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128 or operation not in {"INSPECT", "START", "REQUEST_GRACEFUL_STOP", "RESTART_WORKER"}:
            return {"status": "UNSUPPORTED"}
        state = self.store.read()
        digest = self._payload_digest(request)
        prior = state["operations"].get(operation_id)
        if prior:
            return prior["result"] if prior["digest"] == digest else {"status": "CONFLICT"}
        if operation == "INSPECT":
            return self.inspect()
        expected = request.get("expected_supervisor_incarnation")
        if expected is not None and expected != self.incarnation:
            return {"status": "CONFLICT"}
        state["operations"][operation_id] = {"digest": digest, "result": {"status": "IN_PROGRESS"}}
        self.store.write(state)  # acceptance is durable before any process effect
        if operation == "START":
            result = self._start(state)
        elif operation == "REQUEST_GRACEFUL_STOP":
            result = self._stop(state)
        else:
            stopped = self._stop(state)
            result = self._start(state) if stopped["status"] == "STOPPED" else stopped
        state["operations"][operation_id]["result"] = result
        self.store.write(state)
        return result

    def _start(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.inspect()["alive"]:
            return {"status": "CONFLICT"}
        generation = str(uuid.uuid4())
        environment = os.environ.copy()
        environment["AUTO_CODER_DEPLOYMENT_GENERATION"] = generation
        environment["AUTO_CODER_MANAGEMENT_SOCKET"] = str(self.config.runtime_root / "management.sock")
        command = [str(self.config.interpreter), "-m", "auto_coder.cli", "process-issues", "--repo", self.config.repository, "--log-file", str(self.config.runtime_root / "logs" / "auto-coder.log")]
        self.child = subprocess.Popen(command, cwd=self.config.workspace_root, env=environment, start_new_session=False)
        worker = {"pid": self.child.pid, "start_token": self._start_token(self.child.pid), "process_run_id": str(uuid.uuid4()), "deployment_generation": generation, "runtime_id": "bootstrap", "started_at": time.time()}
        state.update(worker=worker, stopped=False)
        return {"status": "RUNNING", "worker": worker}

    def _stop(self, state: dict[str, Any]) -> dict[str, Any]:
        observation = self.inspect()
        if not observation["alive"]:
            state["stopped"] = True
            state["worker"] = None
            return {"status": "STOPPED"}
        worker = observation["worker"]
        os.kill(int(worker["pid"]), signal.SIGTERM)
        return {"status": "DRAINING", "worker": worker}

    def run(self) -> None:
        for line in sys.stdin:
            try:
                response = self.apply(json.loads(line))
            except (ValueError, json.JSONDecodeError):
                response = {"status": "UNSUPPORTED"}
            print(json.dumps(response, separators=(",", ":")), flush=True)


def main() -> None:
    WorkerSupervisor(SupervisorConfig.environment()).run()


if __name__ == "__main__":
    main()
