"""Immutable PID-1-style owner for one replaceable management service child."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from .bootstrap_store import BootstrapStore, ChildPhase, ChildRecord, EffectRecord


class Action(str, Enum):
    SUPERVISE = "SUPERVISE"
    INSPECT = "INSPECT"
    REQUEST_GRACEFUL_STOP = "REQUEST_GRACEFUL_STOP"
    START_SELECTED = "START_SELECTED"
    RECONCILE = "RECONCILE"


class ResultStatus(str, Enum):
    OBSERVED_RUNNING = "observed_running"
    STOPPED = "stopped"
    DRAINING = "draining"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"
    PROCESS_RESTORED = "process_restored"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class RuntimeSpec:
    reference: str
    receipt: str
    executable: Path
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...] = ()
    drain_ack: Path | None = None
    source_root: Path | None = None
    source_receipt: str = ""
    ready_receipt: Path | None = None

    def verify(self, *, initial: bool = False) -> bool:
        if not self.executable.is_absolute() or not self.executable.is_file() or self.executable.is_symlink():
            return False
        digest = hashlib.sha256(self.executable.read_bytes()).hexdigest()
        if self.receipt != f"sha256:{digest}":
            return False
        if initial:
            return True
        if self.source_root is None or self.ready_receipt is None or not self.source_root.is_absolute() or not self.source_root.is_dir() or self.source_root.is_symlink() or not self.ready_receipt.is_file() or self.ready_receipt.is_symlink():
            return False
        actual = self._source_digest(self.source_root)
        try:
            ready = json.loads(self.ready_receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return self.source_receipt == actual and ready == {"ready": True, "reference": self.reference, "source_receipt": actual}

    @staticmethod
    def _source_digest(root: Path) -> str:
        digest = hashlib.sha256()
        try:
            members = list(root.rglob("*"))
            # Source closures containing links are unsupported: neither the
            # link target nor its containment is immutable under this receipt.
            if any(path.is_symlink() for path in members):
                return ""
            paths = sorted((path for path in members if path.is_file()), key=lambda path: path.relative_to(root).as_posix())
            for path in paths:
                relative = path.relative_to(root).as_posix().encode()
                digest.update(len(relative).to_bytes(4, "big"))
                digest.update(relative)
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            return ""
        return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True)
class BootstrapConfig:
    instance_id: str
    state_root: Path
    oauth_root: Path
    journal_root: Path
    listener_url: str
    initial_runtime: str
    runtimes: tuple[RuntimeSpec, ...]
    graceful_timeout: float = 30.0
    crash_budget: int = 1

    @classmethod
    def read(cls, path: Path) -> "BootstrapConfig":
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("bootstrap configuration must be an absolute regular file")
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {"instance_id", "state_root", "oauth_root", "journal_root", "listener_url", "initial_runtime", "runtimes", "graceful_timeout", "crash_budget"}
        if set(raw) - allowed:
            raise ValueError("unknown bootstrap configuration field")
        runtimes = tuple(
            RuntimeSpec(
                reference=item["reference"],
                receipt=item["receipt"],
                executable=Path(item["executable"]),
                arguments=tuple(item.get("arguments", ())),
                environment=tuple(sorted(item.get("environment", {}).items())),
                drain_ack=Path(item["drain_ack"]) if item.get("drain_ack") else None,
                source_root=Path(item["source_root"]) if item.get("source_root") else None,
                source_receipt=item.get("source_receipt", ""),
                ready_receipt=Path(item["ready_receipt"]) if item.get("ready_receipt") else None,
            )
            for item in raw["runtimes"]
        )
        config = cls(raw["instance_id"], Path(raw["state_root"]), Path(raw["oauth_root"]), Path(raw["journal_root"]), raw["listener_url"], raw["initial_runtime"], runtimes, float(raw.get("graceful_timeout", 30)), int(raw.get("crash_budget", 1)))
        config.validate()
        return config

    def validate(self) -> None:
        if not self.instance_id or len(self.instance_id) > 128 or not self.listener_url.startswith(("http://127.0.0.1:", "http://[::1]:")):
            raise ValueError("invalid fixed management identity or loopback listener")
        if not all(path.is_absolute() for path in (self.state_root, self.oauth_root, self.journal_root)) or len({self.state_root, self.oauth_root, self.journal_root}) != 3:
            raise ValueError("state, OAuth, and journal roots must be distinct absolute paths")
        refs = {runtime.reference for runtime in self.runtimes}
        if len(refs) != len(self.runtimes) or self.initial_runtime not in refs or self.crash_budget < 0 or not 0 < self.graceful_timeout <= 3600:
            raise ValueError("invalid runtime profile")
        for runtime in self.runtimes:
            if not runtime.reference or len(runtime.reference) > 128 or len(runtime.arguments) > 64 or any(len(arg) > 4096 or "\x00" in arg for arg in runtime.arguments):
                raise ValueError("unbounded runtime profile")
            if any(not key or "=" in key or len(key) > 128 or len(value) > 4096 for key, value in runtime.environment):
                raise ValueError("invalid runtime environment")
            if runtime.reference != self.initial_runtime and (runtime.source_root is None or runtime.ready_receipt is None or not runtime.source_receipt):
                raise ValueError("non-bootstrap runtimes require READY-bound source identity")


@dataclass(frozen=True)
class Observation:
    status: ResultStatus
    record: ChildRecord
    operation_id: str = ""
    process_ready: bool = False
    application_ready: bool = False
    detail: str = ""


class ManagementBootstrapSupervisor:
    """Own one child; all executable data comes from immutable local config."""

    def __init__(self, config: BootstrapConfig, incarnation: str | None = None) -> None:
        config.validate()
        self.config = config
        self.store = BootstrapStore(config.state_root, config.instance_id)
        self.incarnation = incarnation or str(uuid.uuid4())
        self._lock = threading.RLock()
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def inspect(self) -> Observation:
        with self._lock:
            try:
                record = self.store.load()
            except sqlite3.DatabaseError as exc:
                return Observation(ResultStatus.UNKNOWN, ChildRecord(self.config.instance_id, ChildPhase.UNKNOWN), detail=str(exc))
            if record.pid == 0 and record.phase in {ChildPhase.STARTING, ChildPhase.DRAINING, ChildPhase.UNKNOWN, ChildPhase.NEEDS_ATTENTION}:
                return Observation(ResultStatus.UNKNOWN, record, detail="unresolved process effect has no authoritative OS lifetime")
            if record.pid == 0:
                return Observation(ResultStatus.STOPPED, record)
            owned = self._children.get(record.pid)
            if owned is not None and owned.poll() is not None:
                if record.intended_stop:
                    exited = replace(record, phase=ChildPhase.UNKNOWN, exit_cause="intended_exit_observed")
                    self.store.save(exited)
                    return Observation(ResultStatus.UNKNOWN, exited, detail="intended child exit awaits drain evidence")
                failed = replace(record, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause=f"startup_exit_{owned.returncode}")
                self.store.save(failed)
                if failed.previous_runtime and not failed.recovery_used:
                    return self._restore_once(failed.effect_operation_id, failed)
                return Observation(ResultStatus.NEEDS_ATTENTION, failed, detail="owned child exited before application readiness")
            token = self._start_token(record.pid)
            if token != record.start_token:
                uncertain = replace(record, phase=ChildPhase.UNKNOWN, readiness="not_observed")
                self.store.save(uncertain)
                return Observation(ResultStatus.UNKNOWN, uncertain, detail="recorded OS lifetime is absent or stale")
            return Observation(ResultStatus.DRAINING if record.phase is ChildPhase.DRAINING else ResultStatus.OBSERVED_RUNNING, record, process_ready=True, application_ready=record.readiness == "application_ready")

    def start_selected(self, operation_id: str, attempt_id: str, runtime_ref: str, expected_identity: str = "") -> Observation:
        with self._lock:
            effect = self.store.begin_effect(operation_id, attempt_id, Action.START_SELECTED.value, runtime_ref, expected_identity)
            replay = self._replay(effect)
            if replay is not None:
                return replay
            current = self.inspect()
            if current.status is ResultStatus.OBSERVED_RUNNING and current.record.effect_operation_id == operation_id and current.record.effect_attempt_id == attempt_id and current.record.selected_runtime == runtime_ref:
                return self._finish(operation_id, ResultStatus.OBSERVED_RUNNING, current.record)
            if current.status in {ResultStatus.OBSERVED_RUNNING, ResultStatus.DRAINING} or (expected_identity and expected_identity != self._identity(current.record)):
                return self._finish(operation_id, ResultStatus.CONFLICT, current.record, "a child lifetime or expectation conflicts")
            if current.status is ResultStatus.UNKNOWN:
                return self._finish(operation_id, ResultStatus.UNKNOWN, current.record, current.detail)
            runtime = self._runtime(runtime_ref)
            if runtime is None or not runtime.verify(initial=runtime_ref == self.config.initial_runtime):
                return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, replace(current.record, phase=ChildPhase.NEEDS_ATTENTION), "runtime receipt is not verified")
            prepared = replace(
                current.record,
                phase=ChildPhase.STARTING,
                previous_runtime=current.record.selected_runtime,
                selected_runtime=runtime_ref,
                child_generation=current.record.child_generation + 1,
                supervisor_incarnation=self.incarnation,
                intended_stop=False,
                readiness="not_observed",
                exit_cause="",
                effect_operation_id=operation_id,
                effect_attempt_id=attempt_id,
                drain_nonce=str(uuid.uuid4()),
                runtime_receipt=runtime.receipt,
                source_receipt=runtime.source_receipt,
            )
            if not self.store.reserve(current.record, prepared):
                winner = self.inspect()
                return self._finish(operation_id, ResultStatus.CONFLICT if winner.status is not ResultStatus.UNKNOWN else ResultStatus.UNKNOWN, winner.record, "another Supervisor reserved the child generation", persist_state=False)
            try:
                environment = dict(runtime.environment)
                environment.update({"AUTO_CODER_CHILD_GENERATION": str(prepared.child_generation), "AUTO_CODER_DRAIN_NONCE": prepared.drain_nonce})
                if runtime.drain_ack is not None:
                    environment["AUTO_CODER_DRAIN_RECEIPT"] = str(runtime.drain_ack)
                child = subprocess.Popen([str(runtime.executable), *runtime.arguments], env=environment, start_new_session=False)
            except OSError:
                failed = replace(prepared, phase=ChildPhase.NEEDS_ATTENTION, exit_cause="launch_failed")
                self.store.save(failed)
                return self._restore_once(operation_id, failed)
            token = self._start_token(child.pid)
            if not token:
                failed = replace(prepared, phase=ChildPhase.NEEDS_ATTENTION, exit_cause="unobservable_launch")
                self.store.save(failed)
                return self._finish(operation_id, ResultStatus.UNKNOWN, failed, "new child OS lifetime could not be observed")
            self._children[child.pid] = child
            running = replace(prepared, phase=ChildPhase.RUNNING, pid=child.pid, start_token=token)
            self.store.save(running)
            # A process which exits during the bounded startup observation is a
            # confirmed failed start, not stale external PID evidence.
            deadline = time.monotonic() + 0.1
            while time.monotonic() < deadline and child.poll() is None:
                time.sleep(0.01)
            if child.poll() is not None:
                failed = replace(running, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause=f"startup_exit_{child.returncode}")
                self.store.save(failed)
                return self._restore_once(operation_id, failed)
            return self._finish(operation_id, ResultStatus.OBSERVED_RUNNING, running)

    def request_graceful_stop(self, operation_id: str, attempt_id: str, expected_identity: str) -> Observation:
        with self._lock:
            effect = self.store.begin_effect(operation_id, attempt_id, Action.REQUEST_GRACEFUL_STOP.value, "", expected_identity)
            replay = self._replay(effect)
            if replay is not None:
                return replay
            observed = self.inspect()
            if observed.status is ResultStatus.STOPPED:
                return self._finish(operation_id, ResultStatus.STOPPED, observed.record)
            if observed.status is not ResultStatus.OBSERVED_RUNNING or expected_identity != self._identity(observed.record):
                # This is only an observation.  Another Supervisor may have
                # claimed DRAINING after our read, so persisting this earlier
                # snapshot could roll the durable stop owner back to RUNNING.
                return self._finish(
                    operation_id,
                    ResultStatus.CONFLICT if observed.status is not ResultStatus.UNKNOWN else ResultStatus.UNKNOWN,
                    observed.record,
                    "child identity changed",
                    persist_state=False,
                )
            draining = replace(
                observed.record,
                phase=ChildPhase.DRAINING,
                intended_stop=True,
                effect_operation_id=operation_id,
                effect_attempt_id=attempt_id,
            )
            # The state transition is the durable signal claim.  An object lock
            # cannot fence another Supervisor process, while an unconditional
            # save would allow both participants to send the cooperative signal.
            if not self.store.reserve(observed.record, draining):
                winner = self.inspect()
                if winner.record.phase is ChildPhase.DRAINING and winner.record.effect_operation_id == operation_id and winner.record.effect_attempt_id == attempt_id:
                    # A prepared effect plus the matching draining owner means
                    # the other participant may have signalled and lost its
                    # response.  Never signal again or terminalize that ambiguity.
                    return Observation(
                        ResultStatus.DRAINING if winner.status is ResultStatus.DRAINING else ResultStatus.UNKNOWN,
                        winner.record,
                        operation_id,
                        winner.process_ready,
                        winner.application_ready,
                        "graceful-stop effect is already owned by another Supervisor",
                    )
                return self._finish(
                    operation_id,
                    ResultStatus.UNKNOWN if winner.status is ResultStatus.UNKNOWN else ResultStatus.CONFLICT,
                    winner.record,
                    "another Supervisor reserved a child transition",
                    persist_state=False,
                )
            os.kill(draining.pid, signal.SIGTERM)  # exactly one cooperative signal
        deadline = time.monotonic() + self.config.graceful_timeout
        while time.monotonic() < deadline:
            if self._start_token(draining.pid) != draining.start_token:
                runtime = self._runtime(draining.selected_runtime)
                acknowledged = runtime is not None and self._drain_acknowledged(runtime, draining)
                if not acknowledged:
                    unknown = replace(draining, phase=ChildPhase.UNKNOWN, exit_cause="exit_without_drain_ack")
                    self.store.save(unknown)
                    return self._finish(operation_id, ResultStatus.UNKNOWN, unknown, "child exited without a drain acknowledgement")
                stopped = replace(draining, phase=ChildPhase.STOPPED, pid=0, start_token="", exit_cause="intended_stop")
                self.store.save(stopped)
                return self._finish(operation_id, ResultStatus.STOPPED, stopped)
            time.sleep(0.02)
        return self._finish(operation_id, ResultStatus.DRAINING, draining, "observation deadline expired; no force signal was sent")

    def reconcile(self) -> Observation:
        observed = self.inspect()
        record = observed.record
        if observed.status is ResultStatus.UNKNOWN and record.phase is ChildPhase.UNKNOWN and record.intended_stop:
            runtime = self._runtime(record.selected_runtime)
            if runtime is not None and self._drain_acknowledged(runtime, record) and self._start_token(record.pid) != record.start_token:
                stopped = replace(record, phase=ChildPhase.STOPPED, pid=0, start_token="", exit_cause="intended_stop")
                self.store.save(stopped)
                return Observation(ResultStatus.STOPPED, stopped)
        return observed

    def run(self) -> int:
        """Remain alive as PID 1 and reap only the configured child."""
        record = self.store.load()
        if record.pid == 0 and record.phase is ChildPhase.STOPPED and not record.intended_stop and record.child_generation == 0 and self.config.crash_budget > 0:
            self.start_selected(f"bootstrap-{uuid.uuid4()}", str(uuid.uuid4()), self.config.initial_runtime)
        while True:
            observed = self.inspect()
            if observed.status is ResultStatus.OBSERVED_RUNNING:
                child = self._children.get(observed.record.pid)
                if child is not None and child.poll() is not None:
                    self.store.save(replace(observed.record, phase=ChildPhase.STOPPED, pid=0, start_token="", exit_cause="unexpected_exit"))
            time.sleep(0.2)

    def _restore_once(self, operation_id: str, failed: ChildRecord) -> Observation:
        if failed.recovery_used or not failed.previous_runtime:
            return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, failed, "no verified previous runtime is available")
        runtime = self._runtime(failed.previous_runtime)
        recovery = replace(failed, recovery_used=True)
        self.store.save(recovery)  # recovery intent precedes launch
        if runtime is None or not runtime.verify(initial=runtime.reference == self.config.initial_runtime):
            return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, recovery, "previous runtime receipt is not verified")
        try:
            environment = dict(runtime.environment)
            recovery_nonce = str(uuid.uuid4())
            environment.update({"AUTO_CODER_CHILD_GENERATION": str(recovery.child_generation + 1), "AUTO_CODER_DRAIN_NONCE": recovery_nonce})
            if runtime.drain_ack is not None:
                environment["AUTO_CODER_DRAIN_RECEIPT"] = str(runtime.drain_ack)
            child = subprocess.Popen([str(runtime.executable), *runtime.arguments], env=environment, start_new_session=False)
        except OSError:
            return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, recovery, "previous runtime launch failed")
        token = self._start_token(child.pid)
        if not token:
            return self._finish(operation_id, ResultStatus.UNKNOWN, recovery, "recovery OS lifetime is unknown")
        self._children[child.pid] = child
        restored = replace(
            recovery,
            phase=ChildPhase.RUNNING,
            selected_runtime=runtime.reference,
            pid=child.pid,
            start_token=token,
            child_generation=recovery.child_generation + 1,
            drain_nonce=recovery_nonce,
            runtime_receipt=runtime.receipt,
            source_receipt=runtime.source_receipt,
            exit_cause="candidate_launch_failed",
        )
        self.store.save(restored)
        return self._finish(operation_id, ResultStatus.PROCESS_RESTORED, restored)

    def _replay(self, effect: EffectRecord) -> Observation | None:
        if effect.phase == "prepared":
            return None
        try:
            result = json.loads(effect.result_json)
            status = ResultStatus(result["status"])
            record_value = result["record"]
            record_value["phase"] = ChildPhase(record_value["phase"])
            record = ChildRecord(**record_value)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return Observation(ResultStatus.UNKNOWN, self.store.load(), effect.operation_id, detail="corrupt effect receipt")
        return Observation(status, record, effect.operation_id, bool(result.get("process_ready")), bool(result.get("application_ready")), result.get("detail", ""))

    def _finish(self, operation_id: str, status: ResultStatus, record: ChildRecord, detail: str = "", *, persist_state: bool = True) -> Observation:
        if persist_state:
            self.store.save(record)
        process_ready = status in {ResultStatus.OBSERVED_RUNNING, ResultStatus.PROCESS_RESTORED}
        encoded_record = asdict(record)
        encoded_record["phase"] = record.phase.value
        self.store.finish_effect(operation_id, "observed", {"status": status.value, "detail": detail, "identity": self._identity(record), "record": encoded_record, "process_ready": process_ready, "application_ready": False})
        return Observation(status, record, operation_id, process_ready, False, detail)

    @staticmethod
    def _drain_acknowledged(runtime: RuntimeSpec, record: ChildRecord) -> bool:
        if runtime.drain_ack is None:
            return False
        try:
            receipt = json.loads(runtime.drain_ack.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return receipt == {"generation": record.child_generation, "nonce": record.drain_nonce, "pid": record.pid, "start_token": record.start_token}

    def _runtime(self, reference: str) -> RuntimeSpec | None:
        return next((item for item in self.config.runtimes if item.reference == reference), None)

    @staticmethod
    def _start_token(pid: int) -> str:
        if pid <= 0:
            return ""
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return "" if fields[2] == "Z" else fields[21]
        except (OSError, IndexError):
            return ""

    @staticmethod
    def _identity(record: ChildRecord) -> str:
        return f"{record.pid}:{record.start_token}:{record.child_generation}:{record.selected_runtime}:{record.supervisor_incarnation}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-Coder immutable management child supervisor")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(ManagementBootstrapSupervisor(BootstrapConfig.read(args.config)).run())


if __name__ == "__main__":
    main()
