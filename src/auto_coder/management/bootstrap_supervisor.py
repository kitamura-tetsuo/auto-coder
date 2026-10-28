"""Immutable PID-1-style owner for one replaceable management service child."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from loguru import logger

from .bootstrap_store import BootstrapStore, ChildPhase, ChildRecord, EffectRecord


class Action(str, Enum):
    SUPERVISE = "SUPERVISE"
    INSPECT = "INSPECT"
    REQUEST_GRACEFUL_STOP = "REQUEST_GRACEFUL_STOP"
    START_SELECTED = "START_SELECTED"
    RECONCILE = "RECONCILE"
    RESTORE_PREVIOUS = "RESTORE_PREVIOUS"


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
    execution_kind: str = "source-script"

    def verify(self, *, initial: bool = False) -> bool:
        if self.execution_kind == "management-installed":
            from .self_update_handoff import verify_installed_runtime

            return verify_installed_runtime(self)
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
        return self.source_receipt == actual and ready == {"ready": True, "reference": self.reference, "source_receipt": actual} and self._execution_is_source_closed()

    def _execution_is_source_closed(self) -> bool:
        """Reject configured Python inputs outside the READY source closure."""
        assert self.source_root is not None
        try:
            source_root = self.source_root.resolve(strict=True)
        except OSError:
            return False

        environment = dict(self.environment)
        for name in ("PYTHONPATH", "PYTHONHOME"):
            value = environment.get(name)
            if value is None:
                continue
            for entry in value.split(os.pathsep):
                if not entry or not self._inside_source(Path(entry), source_root):
                    return False

        # The execution contract is explicit, not inferred from the executable
        # basename: a Python interpreter can be copied under an arbitrary name.
        if self.execution_kind == "source-script":
            try:
                with self.executable.open("rb") as stream:
                    has_shebang = stream.read(2) == b"#!"
            except OSError:
                return False
            return has_shebang and self._inside_source(self.executable, source_root)
        if self.execution_kind == "python-file":
            # Only a direct absolute script entrypoint is supported here.  -c,
            # -m, and opaque binary profiles need a separately verified mapping
            # from their invocation to the actual executed source closure.
            return bool(self.arguments) and self._inside_source(Path(self.arguments[0]), source_root)
        return False

    @staticmethod
    def _inside_source(path: Path, source_root: Path) -> bool:
        if not path.is_absolute():
            return False
        try:
            path.resolve(strict=True).relative_to(source_root)
        except (OSError, ValueError):
            return False
        return True

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
    management_version_root: Path | None = None
    management_config: Path | None = None
    verification_timeout: float = 120.0

    @classmethod
    def read(cls, path: Path) -> "BootstrapConfig":
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("bootstrap configuration must be an absolute regular file")
        raw = json.loads(path.read_text(encoding="utf-8"))
        allowed = {"instance_id", "state_root", "oauth_root", "journal_root", "listener_url", "initial_runtime", "runtimes", "graceful_timeout", "crash_budget", "management_version_root", "management_config", "verification_timeout"}
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
                execution_kind=item.get("execution_kind", "source-script"),
            )
            for item in raw["runtimes"]
        )
        config = cls(
            raw["instance_id"],
            Path(raw["state_root"]),
            Path(raw["oauth_root"]),
            Path(raw["journal_root"]),
            raw["listener_url"],
            raw["initial_runtime"],
            runtimes,
            float(raw.get("graceful_timeout", 30)),
            int(raw.get("crash_budget", 1)),
            Path(raw["management_version_root"]) if raw.get("management_version_root") else None,
            Path(raw["management_config"]) if raw.get("management_config") else None,
            float(raw.get("verification_timeout", 120)),
        )
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
        if (self.management_version_root is None) != (self.management_config is None) or not 0 < self.verification_timeout <= 3600:
            raise ValueError("invalid management self-update bootstrap profile")
        if self.management_version_root is not None:
            assert self.management_config is not None
            if not self.management_version_root.is_absolute() or not self.management_config.is_absolute():
                raise ValueError("management self-update paths must be absolute")
            root = self.management_version_root.resolve()
            if any(root == path.resolve() or root in path.resolve().parents or path.resolve() in root.parents for path in (self.state_root, self.oauth_root, self.journal_root)):
                raise ValueError("management runtime root overlaps retained bootstrap or operator state")
        for runtime in self.runtimes:
            if not runtime.reference or len(runtime.reference) > 128 or len(runtime.arguments) > 64 or any(len(arg) > 4096 or "\x00" in arg for arg in runtime.arguments):
                raise ValueError("unbounded runtime profile")
            if any(not key or "=" in key or len(key) > 128 or len(value) > 4096 for key, value in runtime.environment):
                raise ValueError("invalid runtime environment")
            if runtime.reference != self.initial_runtime and (runtime.source_root is None or runtime.ready_receipt is None or not runtime.source_receipt):
                raise ValueError("non-bootstrap runtimes require READY-bound source identity")
            if runtime.reference != self.initial_runtime and runtime.execution_kind not in {"source-script", "python-file", "management-installed"}:
                raise ValueError("unsupported non-bootstrap execution profile")
            if runtime.execution_kind == "management-installed":
                if self.management_config is None or self.management_version_root is None or runtime.source_root is None:
                    raise ValueError("management installed runtime requires the fixed bootstrap profile")
                runtime.source_root.resolve().relative_to(self.management_version_root.resolve())
                if runtime.arguments != ("-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config", str(self.management_config)):
                    raise ValueError("management installed runtime must use the fixed serving entrypoint")


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

    def __init__(self, config: BootstrapConfig, incarnation: str | None = None, bootstrap_config_path: Path | None = None) -> None:
        config.validate()
        from .worker_network_client import configured_worker_bridge_environment

        # Validate the complete trusted bootstrap environment before recording
        # an effect or launching a management child.
        self._worker_bridge_environment = configured_worker_bridge_environment()
        self.config = config
        self.bootstrap_config_path = bootstrap_config_path
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
                return self._observe_owned_exit(record, owned)
            token = self._start_token(record.pid)
            if token != record.start_token:
                # The owned process can exit between poll() and /proc reading.
                # Reap it before classifying its known lifetime as a stale PID.
                if owned is not None and owned.poll() is not None:
                    return self._observe_owned_exit(record, owned)
                uncertain = replace(record, phase=ChildPhase.UNKNOWN, readiness="not_observed")
                if not self.store.reserve(record, uncertain):
                    return Observation(ResultStatus.UNKNOWN, self.store.load(), detail="child owner changed during OS observation")
                return Observation(ResultStatus.UNKNOWN, uncertain, detail="recorded OS lifetime is absent or stale")
            return Observation(ResultStatus.DRAINING if record.phase is ChildPhase.DRAINING else ResultStatus.OBSERVED_RUNNING, record, process_ready=True, application_ready=record.readiness == "application_ready")

    def _observe_owned_exit(self, record: ChildRecord, owned: subprocess.Popen[bytes]) -> Observation:
        if record.intended_stop:
            exited = replace(record, phase=ChildPhase.UNKNOWN, exit_cause="intended_exit_observed")
            if not self.store.reserve(record, exited):
                return Observation(ResultStatus.UNKNOWN, self.store.load(), detail="child owner changed during exit observation")
            return Observation(ResultStatus.UNKNOWN, exited, detail="intended child exit awaits drain evidence")
        failed = replace(record, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause=f"startup_exit_{owned.returncode}")
        if not self.store.reserve(record, failed):
            return Observation(ResultStatus.UNKNOWN, self.store.load(), detail="child owner changed during exit observation")
        if failed.previous_runtime and not failed.recovery_used and record.readiness != "application_ready":
            return self._restore_once(failed.effect_operation_id, failed)
        return Observation(ResultStatus.NEEDS_ATTENTION, failed, detail="owned child exited before application readiness")

    def start_selected(self, operation_id: str, attempt_id: str, runtime_ref: str, expected_identity: str = "") -> Observation:
        with self._lock:
            effect = self.store.begin_effect(operation_id, attempt_id, Action.START_SELECTED.value, runtime_ref, expected_identity)
            replay = self._replay(effect)
            if replay is not None:
                return replay
            current = self.inspect()
            if current.record.effect_operation_id == operation_id and current.record.effect_attempt_id == attempt_id:
                # The same action may be in flight in another Supervisor.
                # A current PID does not make its unfinished effect receipt
                # safe to complete on that participant's behalf.
                return Observation(ResultStatus.UNKNOWN, current.record, operation_id, detail="operation effect is still owned and unresolved")
            if current.status in {ResultStatus.OBSERVED_RUNNING, ResultStatus.DRAINING} or (expected_identity and expected_identity != self._identity(current.record)):
                return self._finish(operation_id, ResultStatus.CONFLICT, current.record, "a child lifetime or expectation conflicts", persist_state=False)
            if current.status is ResultStatus.UNKNOWN:
                return self._finish(operation_id, ResultStatus.UNKNOWN, current.record, current.detail, persist_state=False)
            runtime = self._runtime(runtime_ref)
            if runtime is None or not runtime.verify(initial=runtime_ref == self.config.initial_runtime):
                rejected = replace(current.record, phase=ChildPhase.NEEDS_ATTENTION)
                returning = current.record.phase is ChildPhase.STOPPED and bool(current.record.selected_runtime) and current.record.intended_stop
                if returning:
                    rejected = replace(
                        rejected,
                        selected_runtime=runtime_ref,
                        previous_runtime=current.record.selected_runtime,
                        child_generation=current.record.child_generation + 1,
                        supervisor_incarnation=self.incarnation,
                        intended_stop=False,
                        readiness="not_observed",
                        recovery_used=False,
                        exit_cause="candidate_verification_failed",
                        effect_operation_id=operation_id,
                        effect_attempt_id=attempt_id,
                        runtime_receipt=runtime.receipt if runtime is not None else "",
                        source_receipt=runtime.source_receipt if runtime is not None else "",
                    )
                if not self.store.reserve(current.record, rejected):
                    winner = self.inspect()
                    return self._finish(operation_id, ResultStatus.UNKNOWN if winner.status is ResultStatus.UNKNOWN else ResultStatus.CONFLICT, winner.record, "another Supervisor changed the child while the runtime was verified")
                if returning:
                    return self._restore_once(operation_id, rejected)
                return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, rejected, "runtime receipt is not verified")
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
                recovery_used=False,
            )
            if not self.store.reserve(current.record, prepared):
                winner = self.inspect()
                if winner.record.effect_operation_id == operation_id and winner.record.effect_attempt_id == attempt_id:
                    return Observation(ResultStatus.UNKNOWN, winner.record, operation_id, detail="operation effect is owned by another Supervisor")
                return self._finish(operation_id, ResultStatus.CONFLICT if winner.status is not ResultStatus.UNKNOWN else ResultStatus.UNKNOWN, winner.record, "another Supervisor reserved the child generation", persist_state=False)
            try:
                environment = dict(runtime.environment)
                # Fixed bootstrap machine-transport endpoints survive child
                # replacement; arbitrary inherited environment does not.
                environment.update(self._worker_bridge_environment)
                environment.update({"AUTO_CODER_CHILD_GENERATION": str(prepared.child_generation), "AUTO_CODER_DRAIN_NONCE": prepared.drain_nonce})
                if runtime.execution_kind == "management-installed":
                    environment.update({"AUTO_CODER_MANAGEMENT_RUNTIME_ID": runtime.reference, "AUTO_CODER_MANAGEMENT_RECEIPT_ID": runtime.source_receipt})
                    if operation_id.endswith(":start"):
                        environment["AUTO_CODER_SELF_UPDATE_OPERATION_ID"] = operation_id.removesuffix(":start")
                if runtime.drain_ack is not None:
                    environment["AUTO_CODER_DRAIN_RECEIPT"] = str(runtime.drain_ack)
                child = subprocess.Popen([str(runtime.executable), *runtime.arguments], env=environment, start_new_session=False)
            except OSError:
                failed = replace(prepared, phase=ChildPhase.NEEDS_ATTENTION, exit_cause="launch_failed")
                self.store.save(failed)
                return self._restore_once(operation_id, failed)
            token = self._start_token(child.pid)
            if not token:
                if child.poll() is not None:
                    failed = replace(prepared, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause=f"startup_exit_{child.returncode}")
                    if not self.store.reserve(prepared, failed):
                        return Observation(ResultStatus.UNKNOWN, self.store.load(), operation_id, detail="child owner changed during startup exit")
                    return self._restore_once(operation_id, failed)
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
            durable = self.store.load()
            if durable != running:
                status = ResultStatus.DRAINING if durable.phase is ChildPhase.DRAINING else ResultStatus.UNKNOWN
                return self._finish(
                    operation_id,
                    status,
                    durable,
                    "another Supervisor changed the launched child transition",
                    persist_state=False,
                )
            if child.poll() is not None:
                failed = replace(running, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause=f"startup_exit_{child.returncode}")
                if not self.store.reserve(running, failed):
                    return Observation(ResultStatus.UNKNOWN, self.store.load(), operation_id, detail="child owner changed during startup exit")
                return self._restore_once(operation_id, failed)
            return self._finish(operation_id, ResultStatus.OBSERVED_RUNNING, running, persist_state=False)

    def request_graceful_stop(self, operation_id: str, attempt_id: str, expected_identity: str) -> Observation:
        with self._lock:
            effect = self.store.begin_effect(operation_id, attempt_id, Action.REQUEST_GRACEFUL_STOP.value, "", expected_identity)
            replay = self._replay(effect)
            if replay is not None:
                return replay
            observed = self.inspect()
            if observed.status is ResultStatus.STOPPED:
                return self._finish(operation_id, ResultStatus.STOPPED, observed.record)
            if observed.record.effect_operation_id == operation_id and observed.record.effect_attempt_id == attempt_id:
                return Observation(ResultStatus.UNKNOWN, observed.record, operation_id, detail="graceful-stop effect is still owned and unresolved")
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
            pidfd_open = getattr(os, "pidfd_open", None)
            pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
            if pidfd_open is None or pidfd_send_signal is None:
                return self._finish(operation_id, ResultStatus.UNKNOWN, observed.record, "lifetime-bound process signalling is unavailable", persist_state=False)
            try:
                pidfd = pidfd_open(observed.record.pid, 0)
            except OSError:
                return self._finish(operation_id, ResultStatus.UNKNOWN, observed.record, "child lifetime disappeared before signalling", persist_state=False)
            if self._start_token(observed.record.pid) != observed.record.start_token:
                os.close(pidfd)
                return self._finish(operation_id, ResultStatus.UNKNOWN, observed.record, "child lifetime changed before signalling", persist_state=False)
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
                os.close(pidfd)
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
            try:
                try:
                    pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
                except OSError:
                    return self._finish(operation_id, ResultStatus.UNKNOWN, draining, "child lifetime disappeared during signalling", persist_state=False)
            finally:
                os.close(pidfd)
        deadline = time.monotonic() + self.config.graceful_timeout
        while time.monotonic() < deadline:
            if self._start_token(draining.pid) != draining.start_token:
                runtime = self._runtime(draining.selected_runtime)
                acknowledged = runtime is not None and self._drain_acknowledged(runtime, draining)
                if not acknowledged:
                    unknown = replace(draining, phase=ChildPhase.UNKNOWN, exit_cause="exit_without_drain_ack")
                    if not self.store.reserve(draining, unknown):
                        return self._finish(operation_id, ResultStatus.UNKNOWN, self.store.load(), "another Supervisor observed the exit")
                    return self._finish(operation_id, ResultStatus.UNKNOWN, unknown, "child exited without a drain acknowledgement")
                stopped = replace(draining, phase=ChildPhase.STOPPED, pid=0, start_token="", exit_cause="intended_stop")
                if not self.store.reserve(draining, stopped):
                    return self._finish(operation_id, ResultStatus.UNKNOWN, self.store.load(), "another Supervisor observed the exit")
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
                if self.store.reserve(record, stopped):
                    return Observation(ResultStatus.STOPPED, stopped)
                return Observation(ResultStatus.UNKNOWN, self.store.load(), detail="child owner changed during reconciliation")
        return observed

    def _pristine_initial_state(self) -> bool:
        """Never infer a first boot from an empty record over retained history."""
        try:
            with closing(sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                metadata = dict(connection.execute("SELECT key,value FROM metadata"))
                rows = connection.execute("SELECT instance_id,record_json FROM child_state").fetchall()
                if metadata != {"schema_version": "1"} or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("SELECT COUNT(*) FROM effects").fetchone()[0]:
                    return False
                if len(rows) > 1 or (rows and (rows[0][0] != self.config.instance_id or rows[0][1] != self.store._record_json(ChildRecord(self.config.instance_id)))):
                    return False
            recovery = self.config.state_root / "management-recovery.sqlite3"
            oauth = self.config.oauth_root / "oauth.sqlite3"
            if any(path.exists() or path.is_symlink() for path in (recovery, oauth)):
                return False
            stores = (
                (self.config.state_root / "management-self-update.sqlite3", {"schema_version": "1", "instance_id": self.config.instance_id}, ("handoffs", "runtimes")),
                (self.config.journal_root / "management-operations.sqlite3", {"schema_version": "2"}, ("operations", "operation_history")),
            )
            for path, expected_metadata, tables in stores:
                if path.is_symlink():
                    return False
                if path.exists():
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
                        if dict(connection.execute("SELECT key,value FROM metadata")) != expected_metadata or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            return False
                        if any(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables):
                            return False
            return True
        except (OSError, ValueError, sqlite3.Error):
            return False

    def run(self) -> int:
        """Remain alive as PID 1 and reap only the configured child."""
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        with (self.config.state_root / ".supervisor-owner.lock").open("a+b") as ownership:
            fcntl.flock(ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._run_owned()

    def _run_owned(self) -> int:
        from .self_update_handoff import SelfUpdateHandoffConsumer

        recovery_path = self.config.state_root / "management-recovery.sqlite3"
        if self.store.path.is_symlink():
            logger.error("Management child ledger cannot be a symlink")
            return 1
        ledger_exists = self.store.path.is_file()
        retained = (
            recovery_path,
            self.config.state_root / "management-self-update.sqlite3",
            self.config.journal_root / "management-operations.sqlite3",
            self.config.oauth_root / "oauth.sqlite3",
        )
        if not ledger_exists and any(path.exists() or path.is_symlink() for path in retained):
            logger.error("Management child ledger is missing; refusing bootstrap reconstruction")
            return 1
        self.store.require_existing = ledger_exists
        self.store.publish_supervisor(self.incarnation, os.getpid(), self._start_token(os.getpid()))
        self.store.require_existing = True
        consumer = SelfUpdateHandoffConsumer(self) if self.config.management_config is not None else None
        record = self.store.load()
        initial_state_pristine = self._pristine_initial_state() if record.child_generation == 0 else False
        if consumer is not None and record.child_generation == 0 and not consumer.store.path.exists() and not consumer.store.path.is_symlink():
            try:
                consumer.store.active()
            except (OSError, ValueError, sqlite3.Error):
                logger.warning("Management handoff mailbox unavailable")
        recovery_consumer = None
        recovery_mailbox_unavailable = False
        if self.bootstrap_config_path is not None:
            try:
                from .recovery_supervisor import RecoveryConsumer

                recovery_consumer = RecoveryConsumer(self, self.bootstrap_config_path)
            except (OSError, ValueError, sqlite3.Error):
                from .recovery_mailbox import RecoveryMailbox

                try:
                    mailbox = RecoveryMailbox(self.config.state_root, self.config.instance_id)
                    recovery_mailbox_unavailable = mailbox._child_marker() is not None or mailbox.path.exists() or mailbox.path.is_symlink()
                except (OSError, ValueError, sqlite3.Error):
                    recovery_mailbox_unavailable = True
                logger.warning("Management local recovery mailbox unavailable")
        if record.pid == 0 and record.phase is ChildPhase.STOPPED and not record.intended_stop and record.child_generation == 0 and self.config.crash_budget > 0:
            if not initial_state_pristine or self.store.has_unresolved_effects():
                self.store.reserve(record, replace(record, phase=ChildPhase.UNKNOWN, exit_cause="unresolved_effect"))
            else:
                self.start_selected(f"bootstrap-{uuid.uuid4()}", str(uuid.uuid4()), self.config.initial_runtime)
        while True:
            if not self.store.path.is_file() or self.store.path.is_symlink():
                logger.error("Management child ledger is unavailable; preserving process state")
                time.sleep(0.2)
                continue
            try:
                self.store.publish_supervisor(self.incarnation, os.getpid(), self._start_token(os.getpid()))
            except (OSError, sqlite3.Error):
                logger.error("Management child ledger is unavailable; preserving process state")
                time.sleep(0.2)
                continue
            # inspect() owns exit classification and the one-time safety return.
            # A second poll/save here can lose an exit between observations and
            # bypass that durable recovery transition.
            self.inspect()
            if consumer is not None and not recovery_mailbox_unavailable and (recovery_consumer is None or not recovery_consumer.blocks_self_update()):
                consumer.tick()
            if recovery_consumer is not None:
                recovery_consumer.tick()
            time.sleep(0.2)

    def restore_previous(self, operation_id: str, attempt_id: str, expected_identity: str, previous_runtime: str) -> Observation:
        """Make the single pre-authorized safety return after proven child exit."""
        with self._lock:
            effect = self.store.begin_effect(operation_id, attempt_id, Action.RESTORE_PREVIOUS.value, previous_runtime, expected_identity)
            replay = self._replay(effect)
            if replay is not None:
                return replay
            observed = self.reconcile()
            if observed.status is not ResultStatus.STOPPED or self._identity(observed.record) != expected_identity or observed.record.previous_runtime != previous_runtime:
                return self._finish(operation_id, ResultStatus.UNKNOWN, observed.record, "previous-runtime restore requires the exact stopped candidate lifetime")
            return self._restore_once(operation_id, observed.record)

    def restore_stopped_original(self, operation_id: str, attempt_id: str, expected_identity: str, previous_runtime: str) -> Observation:
        """Return the drained original when forward authority is no longer live."""
        with self._lock:
            effect = self.store.begin_effect(operation_id, attempt_id, Action.RESTORE_PREVIOUS.value, previous_runtime, expected_identity)
            replay = self._replay(effect)
            if replay is not None:
                return replay
            observed = self.reconcile()
            if observed.status is not ResultStatus.STOPPED or self._identity(observed.record) != expected_identity or observed.record.selected_runtime != previous_runtime or not observed.record.intended_stop:
                return self._finish(operation_id, ResultStatus.UNKNOWN, observed.record, "original-runtime return requires the exact drained original lifetime")
            refused = replace(
                observed.record,
                phase=ChildPhase.NEEDS_ATTENTION,
                previous_runtime=previous_runtime,
                child_generation=observed.record.child_generation + 1,
                recovery_used=False,
                intended_stop=False,
                exit_cause="forward_authority_refused_after_drain",
                effect_operation_id=operation_id,
                effect_attempt_id=attempt_id,
            )
            if not self.store.reserve(observed.record, refused):
                return Observation(ResultStatus.UNKNOWN, self.store.load(), operation_id, detail="original-runtime return owner changed")
            return self._restore_once(operation_id, refused)

    def _restore_once(self, operation_id: str, failed: ChildRecord) -> Observation:
        if failed.recovery_used or not failed.previous_runtime:
            return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, failed, "no verified previous runtime is available")
        runtime = self._runtime(failed.previous_runtime)
        recovery = replace(failed, recovery_used=True)
        # This durable claim precedes launch and fences another participant.
        if not self.store.reserve(failed, recovery):
            return Observation(ResultStatus.UNKNOWN, self.store.load(), operation_id, detail="another Supervisor owns the safety return")
        if runtime is None or not runtime.verify(initial=runtime.reference == self.config.initial_runtime):
            return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, recovery, "previous runtime receipt is not verified")
        try:
            environment = dict(runtime.environment)
            environment.update(self._worker_bridge_environment)
            recovery_nonce = str(uuid.uuid4())
            environment.update({"AUTO_CODER_CHILD_GENERATION": str(recovery.child_generation + 1), "AUTO_CODER_DRAIN_NONCE": recovery_nonce})
            if runtime.execution_kind == "management-installed":
                environment.update({"AUTO_CODER_MANAGEMENT_RUNTIME_ID": runtime.reference, "AUTO_CODER_MANAGEMENT_RECEIPT_ID": runtime.source_receipt})
            if runtime.drain_ack is not None:
                environment["AUTO_CODER_DRAIN_RECEIPT"] = str(runtime.drain_ack)
            child = subprocess.Popen([str(runtime.executable), *runtime.arguments], env=environment, start_new_session=False)
        except OSError:
            return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, recovery, "previous runtime launch failed")
        token = self._start_token(child.pid)
        if not token:
            if child.poll() is not None:
                failed_restore = replace(recovery, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause="previous_runtime_start_failed")
                if self.store.reserve(recovery, failed_restore):
                    return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, failed_restore, "the one previous-runtime process exited during startup")
                return self._finish(operation_id, ResultStatus.UNKNOWN, self.store.load(), "restore lifetime changed during startup observation")
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
            intended_stop=False,
            supervisor_incarnation=self.incarnation,
            readiness="not_observed",
        )
        self.store.save(restored)
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.01)
        if child.poll() is not None:
            failed_restore = replace(restored, phase=ChildPhase.NEEDS_ATTENTION, pid=0, start_token="", exit_cause="previous_runtime_start_failed")
            if self.store.reserve(restored, failed_restore):
                return self._finish(operation_id, ResultStatus.NEEDS_ATTENTION, failed_restore, "the one previous-runtime process exited during startup")
            return Observation(ResultStatus.UNKNOWN, self.store.load(), operation_id, detail="restore lifetime changed during startup observation")
        # The original start effect may already contain the candidate's
        # immutable observation. Retain the distinct restored lifetime under a
        # separate observation identity before declaring the safety return.
        observation_id = operation_id + ":safety-observation"
        evidence = self.store.begin_effect(observation_id, restored.effect_attempt_id, "OBSERVE_SAFETY_RESTORE", restored.selected_runtime, self._identity(restored))
        if evidence.phase != "prepared":
            return Observation(ResultStatus.UNKNOWN, restored, operation_id, detail="safety return observation already exists")
        self._finish(observation_id, ResultStatus.PROCESS_RESTORED, restored)
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

    def _finish(self, operation_id: str, status: ResultStatus, record: ChildRecord, detail: str = "", *, persist_state: bool = False) -> Observation:
        # Effect receipts are observations, not authority to overwrite a newer
        # child generation or another Supervisor's durable transition claim.
        # Callers must reserve or save an authoritative transition beforehand.
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
        configured = next((item for item in self.config.runtimes if item.reference == reference), None)
        if configured is not None or self.config.management_version_root is None:
            return configured
        from .self_update_handoff import HandoffStore, runtime_for_receipt

        receipt = HandoffStore(self.config.state_root, self.config.instance_id).runtime(reference)
        return runtime_for_receipt(self.config, receipt) if receipt is not None else None

    @staticmethod
    def _start_token(pid: int) -> str:
        if pid <= 0:
            return ""
        try:
            # Field 2 (comm) is parenthesized and may contain whitespace or
            # parentheses.  Splitting the entire line shifts field 22.
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            return "" if fields[0] == "Z" else fields[19]
        except (OSError, IndexError):
            return ""

    @staticmethod
    def _identity(record: ChildRecord) -> str:
        return f"{record.pid}:{record.start_token}:{record.child_generation}:{record.selected_runtime}:{record.supervisor_incarnation}"


def main() -> None:
    from ..logger_config import setup_logger

    parser = argparse.ArgumentParser(description="Auto-Coder immutable management child supervisor")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = BootstrapConfig.read(args.config)
    setup_logger(stream=sys.stdout, log_file=str(config.state_root / "logs" / "management-bootstrap.log"))
    raise SystemExit(ManagementBootstrapSupervisor(config, bootstrap_config_path=args.config).run())


if __name__ == "__main__":
    # Keep one canonical set of enum/dataclass identities when the supported
    # module CLI composes the separately imported durable handoff consumer.
    from auto_coder.management.bootstrap_supervisor import main as canonical_main

    canonical_main()
