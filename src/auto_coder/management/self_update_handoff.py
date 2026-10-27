"""Private, durable self-update handoff consumed by the immutable Supervisor.

The management dispatcher owns operation admission.  This mailbox owns only the
one already-authorized, exact process replacement and its bounded safety return.
No bearer credential or caller-selected executable crosses this boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from loguru import logger

from .bootstrap_store import ChildPhase, ChildRecord
from .version_staging import ManagementReadyReceipt, ManagementRuntimeConfig, ManagementVersionStager


class HandoffPhase(str, Enum):
    SUBMITTED = "submitted"
    STOPPING = "stopping"
    DRAINING = "draining"
    STOPPED = "stopped"
    STARTING = "starting"
    AWAITING_PROOF = "awaiting_authenticated_proof"
    RESTORING = "restoring"
    RESTORE_DRAINING = "restore_draining"
    RESTORE_STARTING = "restore_starting"
    SUCCEEDED = "succeeded"
    PROCESS_RESTORED = "process_restored"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    NEEDS_ATTENTION = "needs_attention"


_TERMINAL = {HandoffPhase.SUCCEEDED, HandoffPhase.PROCESS_RESTORED, HandoffPhase.DENIED, HandoffPhase.UNAVAILABLE}


@dataclass(frozen=True)
class SelfUpdateHandoff:
    operation_id: str
    attempt_id: str
    correlation_identity: str
    effect_generation: int
    expected_supervisor: str
    expected_child: ChildRecord
    candidate: ManagementReadyReceipt
    previous: ManagementReadyReceipt | None
    policy_revision: str
    authorized_at: float
    preflight_identity: str = ""


@dataclass(frozen=True)
class ApplicationProof:
    child_identity: str
    runtime_id: str
    receipt_identity: str
    source_sha: str
    uid: str
    client_id: str
    grant_id: str
    observed_at: float


@dataclass(frozen=True)
class HandoffRecord:
    handoff: SelfUpdateHandoff
    phase: HandoffPhase = HandoffPhase.SUBMITTED
    detail: str = ""
    selected_identity: str = ""
    selected_generation: int = 0
    deadline: float = 0
    challenge_observed: bool = False
    proof: ApplicationProof | None = None
    restored_identity: str = ""


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _handoff(raw: dict[str, object]) -> SelfUpdateHandoff:
    value = dict(raw)
    child_raw = value["expected_child"]
    if not isinstance(child_raw, dict):
        raise ValueError("invalid expected management child")
    child = dict(child_raw)
    child["phase"] = ChildPhase(child["phase"])
    value["expected_child"] = ChildRecord(**child)
    value["candidate"] = ManagementReadyReceipt(**value["candidate"])  # type: ignore[arg-type]
    value["previous"] = ManagementReadyReceipt(**value["previous"]) if value["previous"] else None  # type: ignore[arg-type]
    return SelfUpdateHandoff(**value)  # type: ignore[arg-type]


def _record(raw: str) -> HandoffRecord:
    try:
        value = json.loads(raw)
        value["handoff"] = _handoff(value["handoff"])
        value["phase"] = HandoffPhase(value["phase"])
        value["proof"] = ApplicationProof(**value["proof"]) if value["proof"] else None
        return HandoffRecord(**value)
    except (ValueError, TypeError, KeyError) as exc:
        raise sqlite3.DatabaseError("corrupt management self-update handoff") from exc


def _record_row(row: sqlite3.Row) -> HandoffRecord:
    record = _record(row["record_json"])
    if _encode(asdict(record.handoff)) != row["intent_json"]:
        raise sqlite3.DatabaseError("management handoff differs from its immutable original intent")
    return record


def valid_application_proof(record: HandoffRecord, *, uid: str, client_id: str) -> bool:
    """Require exact installed-process, original-actor and bounded MCP evidence."""
    proof = record.proof
    if proof is None or record.challenge_observed is not True or record.phase not in {HandoffPhase.AWAITING_PROOF, HandoffPhase.SUCCEEDED}:
        return False
    candidate = record.handoff.candidate
    if any(not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value for value in (proof.child_identity, proof.runtime_id, proof.receipt_identity, proof.source_sha, proof.uid, proof.client_id, proof.grant_id)):
        return False
    times = (record.handoff.authorized_at, proof.observed_at, record.deadline)
    if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) for value in times):
        return False
    return (
        bool(record.selected_identity)
        and type(record.selected_generation) is int
        and record.selected_generation == record.handoff.expected_child.child_generation + 1
        and (proof.uid, proof.client_id) == (uid, client_id)
        and bool(proof.grant_id)
        and (proof.child_identity, proof.runtime_id, proof.receipt_identity, proof.source_sha) == (record.selected_identity, candidate.management_runtime_id, candidate.receipt_identity, candidate.source_sha)
        and record.handoff.authorized_at <= proof.observed_at <= record.deadline
    )


class HandoffStore:
    """A single-instance mailbox; rows retain immutable original operation facts."""

    def __init__(self, root: Path, instance_id: str) -> None:
        if not root.is_absolute() or not instance_id or len(instance_id) > 128:
            raise ValueError("invalid management handoff root or instance")
        self.root = root
        self.instance_id = instance_id
        self.path = root / "management-self-update.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES('instance_id',?)", (self.instance_id,))
        metadata = dict(connection.execute("SELECT key,value FROM metadata").fetchall())
        if metadata != {"schema_version": "1", "instance_id": self.instance_id}:
            connection.close()
            raise sqlite3.DatabaseError("management handoff schema or instance mismatch")
        connection.executescript("CREATE TABLE IF NOT EXISTS handoffs(operation_id TEXT PRIMARY KEY,intent_json TEXT NOT NULL,record_json TEXT NOT NULL,phase TEXT NOT NULL);" "CREATE TABLE IF NOT EXISTS runtimes(reference TEXT PRIMARY KEY,receipt_json TEXT NOT NULL);")
        return connection

    def _after_submit_commit(self) -> None:
        """Persistence boundary for transport-loss and crash regression tests."""

    def _after_phase_commit(self, record: HandoffRecord) -> None:
        """Persistence boundary for physical-effect failure regression tests."""

    def submit(self, handoff: SelfUpdateHandoff) -> HandoffRecord:
        for value in (handoff.operation_id, handoff.attempt_id, handoff.correlation_identity, handoff.expected_supervisor, handoff.policy_revision):
            if not value or len(value) > 512 or "\x00" in value:
                raise ValueError("invalid self-update handoff identity")
        if handoff.effect_generation <= 0 or handoff.expected_child.instance_id != self.instance_id or handoff.candidate.instance_id != self.instance_id:
            raise ValueError("self-update handoff instance or effect mismatch")
        if handoff.previous is None or handoff.previous.instance_id != self.instance_id or handoff.expected_child.selected_runtime != handoff.previous.management_runtime_id:
            raise ValueError("self-update requires the exact previous verified management runtime")
        encoded = _encode(asdict(handoff))
        record = HandoffRecord(handoff)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT intent_json,record_json FROM handoffs WHERE operation_id=?", (handoff.operation_id,)).fetchone()
            if old is not None:
                connection.rollback()
                if old["intent_json"] != encoded:
                    raise ValueError("self-update handoff replay conflicts with original intent")
                return _record_row(old)
            rows = connection.execute("SELECT intent_json,record_json FROM handoffs").fetchall()
            if any(_record_row(row).phase not in _TERMINAL for row in rows):
                connection.rollback()
                raise ValueError("another management handoff retains the process lane")
            for receipt in (handoff.candidate, handoff.previous):
                existing = connection.execute("SELECT receipt_json FROM runtimes WHERE reference=?", (receipt.management_runtime_id,)).fetchone()
                receipt_json = _encode(asdict(receipt))
                if existing is not None and existing[0] != receipt_json:
                    connection.rollback()
                    raise ValueError("management runtime identity conflicts with retained receipt")
                connection.execute("INSERT OR IGNORE INTO runtimes VALUES(?,?)", (receipt.management_runtime_id, receipt_json))
            connection.execute("INSERT INTO handoffs VALUES(?,?,?,?)", (handoff.operation_id, encoded, _encode(asdict(record)), record.phase.value))
            connection.commit()
        self._after_submit_commit()
        return record

    def read(self, operation_id: str) -> HandoffRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT intent_json,record_json FROM handoffs WHERE operation_id=?", (operation_id,)).fetchone()
        return _record_row(row) if row else None

    def active(self) -> tuple[HandoffRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT intent_json,record_json FROM handoffs ORDER BY rowid").fetchall()
        return tuple(record for row in rows if (record := _record_row(row)).phase not in _TERMINAL)

    def runtime(self, reference: str) -> ManagementReadyReceipt | None:
        with self._connect() as connection:
            row = connection.execute("SELECT receipt_json FROM runtimes WHERE reference=?", (reference,)).fetchone()
            handoffs = connection.execute("SELECT intent_json,record_json FROM handoffs").fetchall()
        if row is None:
            return None
        try:
            receipt = ManagementReadyReceipt(**json.loads(row[0]))
            bindings = tuple(value for raw in handoffs for value in (_record_row(raw).handoff.candidate, _record_row(raw).handoff.previous) if value is not None and value.management_runtime_id == reference)
            if not bindings or any(binding != receipt for binding in bindings):
                raise sqlite3.DatabaseError("retained management runtime differs from original immutable handoff")
            return receipt
        except (ValueError, TypeError, KeyError) as exc:
            raise sqlite3.DatabaseError("corrupt retained management runtime") from exc

    def change(
        self,
        expected: HandoffRecord,
        *,
        phase: HandoffPhase | None = None,
        detail: str | None = None,
        selected_identity: str | None = None,
        selected_generation: int | None = None,
        deadline: float | None = None,
        challenge_observed: bool | None = None,
        proof: ApplicationProof | None = None,
        restored_identity: str | None = None,
    ) -> HandoffRecord | None:
        updated = replace(
            expected,
            phase=phase if phase is not None else expected.phase,
            detail=detail if detail is not None else expected.detail,
            selected_identity=selected_identity if selected_identity is not None else expected.selected_identity,
            selected_generation=selected_generation if selected_generation is not None else expected.selected_generation,
            deadline=deadline if deadline is not None else expected.deadline,
            challenge_observed=challenge_observed if challenge_observed is not None else expected.challenge_observed,
            proof=proof if proof is not None else expected.proof,
            restored_identity=restored_identity if restored_identity is not None else expected.restored_identity,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT intent_json,record_json FROM handoffs WHERE operation_id=?", (expected.handoff.operation_id,)).fetchone()
            if row is None or _record_row(row) != expected:
                connection.rollback()
                return None
            count = connection.execute(
                "UPDATE handoffs SET record_json=?,phase=? WHERE operation_id=? AND record_json=?",
                (_encode(asdict(updated)), updated.phase.value, expected.handoff.operation_id, _encode(asdict(expected))),
            ).rowcount
            connection.commit()
        if count != 1:
            return None
        self._after_phase_commit(updated)
        return updated

    def record_proof(self, operation_id: str, proof: ApplicationProof) -> bool:
        record = self.read(operation_id)
        if record is None or record.phase is not HandoffPhase.AWAITING_PROOF:
            return False
        candidate = record.handoff.candidate
        if (proof.child_identity, proof.runtime_id, proof.receipt_identity, proof.source_sha) != (record.selected_identity, candidate.management_runtime_id, candidate.receipt_identity, candidate.source_sha):
            return False
        if not record.handoff.authorized_at <= proof.observed_at <= record.deadline or not proof.uid or not proof.client_id or not proof.grant_id:
            return False
        if record.proof is not None and (record.proof.uid, record.proof.client_id) != (proof.uid, proof.client_id):
            return False
        return self.change(record, proof=proof) is not None


def observe_supervisor(config: "BootstrapConfig") -> tuple[str, ChildRecord] | None:
    """Read existing Supervisor evidence without creating or changing its state."""
    from .bootstrap_supervisor import ManagementBootstrapSupervisor

    path = config.state_root / "management-child.sqlite3"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute("SELECT incarnation,pid,start_token,updated_at FROM supervisor_presence WHERE instance_id=?", (config.instance_id,)).fetchone()
            child = connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (config.instance_id,)).fetchone()
        if row is None or child is None or time.time() - row[3] > max(30, config.graceful_timeout + 5) or ManagementBootstrapSupervisor._start_token(row[1]) != row[2]:
            return None
        values = json.loads(child[0])
        values["phase"] = ChildPhase(values["phase"])
        record = ChildRecord(**values)
        if record.instance_id != config.instance_id or not record.pid or ManagementBootstrapSupervisor._start_token(record.pid) != record.start_token:
            return None
        return row[0], record
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return None


def runtime_for_receipt(config: "BootstrapConfig", receipt: ManagementReadyReceipt) -> "RuntimeSpec":
    from .bootstrap_supervisor import RuntimeSpec

    if config.management_version_root is None or config.management_config is None or receipt.instance_id != config.instance_id:
        raise ValueError("management installed-runtime profile is not configured")
    environment = Path(receipt.environment_path)
    environment.resolve().relative_to(config.management_version_root.resolve())
    initial = next((item for item in config.runtimes if item.reference == config.initial_runtime), None)
    child_environment = dict(initial.environment) if initial is not None else {}
    # These values come from immutable operator configuration, never the MCP
    # request or the mailbox. In particular, retain configured ADC file paths.
    child_environment.pop("PYTHONPATH", None)
    child_environment.pop("PYTHONHOME", None)
    child_environment.setdefault("PATH", os.defpath)
    child_environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
    return RuntimeSpec(
        receipt.management_runtime_id,
        f"sha256:{receipt.python_executable_sha256}",
        Path(receipt.interpreter_path),
        ("-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config", str(config.management_config)),
        tuple(sorted(child_environment.items())),
        config.state_root / f"drain-{receipt.management_runtime_id}.json",
        environment,
        receipt.receipt_identity,
        environment.parent / "MANAGEMENT_READY.json",
        "management-installed",
    )


def verify_installed_runtime(runtime: "RuntimeSpec") -> bool:
    """Authenticate local installed bytes against the original READY receipt."""
    try:
        if runtime.source_root is None or runtime.ready_receipt is None or runtime.ready_receipt.is_symlink() or not runtime.ready_receipt.is_absolute():
            return False
        receipt = ManagementReadyReceipt(**json.loads(runtime.ready_receipt.read_text(encoding="utf-8")))
        environment = Path(receipt.environment_path)
        if runtime.ready_receipt != environment.parent / "MANAGEMENT_READY.json" or runtime.source_root != environment or runtime.executable != environment / "bin" / "python":
            return False
        if runtime.reference != receipt.management_runtime_id or runtime.source_receipt != receipt.receipt_identity or ManagementVersionStager._receipt_identity(receipt) != receipt.receipt_identity:
            return False
        if runtime.receipt != f"sha256:{receipt.python_executable_sha256}" or hashlib.sha256(runtime.executable.read_bytes()).hexdigest() != receipt.python_executable_sha256:
            return False
        if len(runtime.arguments) != 8 or runtime.arguments[:7] != ("-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config"):
            return False
        management_config = Path(runtime.arguments[7])
        if not management_config.is_absolute() or hashlib.sha256(management_config.read_bytes()).hexdigest() != receipt.configuration_sha256:
            return False
        config = ManagementRuntimeConfig(receipt.instance_id, "kitamura-tetsuo/auto-coder", environment.parent / "source", environment.parent.parent, runtime.executable, Path("/usr/bin/uv"), "unused", management_config=management_config)
        stager = ManagementVersionStager(config)
        stager._root = environment.parent.parent
        return stager._content_identity(environment, probe=False) == receipt.installed_content_sha256
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


class SelfUpdateHandoffConsumer:
    """Advance exact physical effects only from the existing Supervisor loop."""

    def __init__(self, supervisor: "ManagementBootstrapSupervisor") -> None:
        self.supervisor = supervisor
        self.config = supervisor.config
        self.store = HandoffStore(self.config.state_root, self.config.instance_id)

    def tick(self) -> None:
        try:
            records = self.store.active()
        except (OSError, ValueError, sqlite3.Error):
            logger.error("Management self-update handoff evidence is unavailable; preserving the current child")
            return
        for record in records:
            if record.phase is HandoffPhase.NEEDS_ATTENTION:
                continue
            try:
                self._advance(record)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error):
                # Receipt failure never retries a potentially delivered effect.
                logger.error("Management self-update reconciliation unavailable operation={}", record.handoff.operation_id)

    def _advance(self, record: HandoffRecord) -> None:
        from .bootstrap_supervisor import ResultStatus

        handoff = record.handoff
        operation = handoff.operation_id
        if record.phase is HandoffPhase.SUBMITTED:
            if handoff.expected_supervisor != self.supervisor.incarnation:
                self._attention(record, "Supervisor incarnation changed before the first effect; inspect the original operation without resubmission")
                return
            observed = self.supervisor.inspect()
            if observed.status is not ResultStatus.OBSERVED_RUNNING or observed.record != handoff.expected_child:
                self._attention(record, "expected management child lifetime changed before handoff")
                return
            if handoff.previous is None or not runtime_for_receipt(self.config, handoff.candidate).verify() or not runtime_for_receipt(self.config, handoff.previous).verify():
                self._attention(record, "selected or previous installed READY identity is unavailable; no child was stopped")
                return
            refusal = self._authority_refusal(handoff)
            if refusal is not None:
                self.store.change(record, phase=refusal[0], detail=refusal[1])
                return
            claimed = self.store.change(record, phase=HandoffPhase.STOPPING)
            if claimed is None:
                return
            stopped = self.supervisor.request_graceful_stop(operation + ":stop", handoff.attempt_id, self.supervisor._identity(handoff.expected_child))
            self._after_stop(claimed, stopped)
            return
        if record.phase in {HandoffPhase.STOPPING, HandoffPhase.DRAINING}:
            observed = self.supervisor.reconcile()
            if observed.record.effect_operation_id != operation + ":stop":
                self._attention(record, "stop handoff has no matching physical-effect owner; retain the lane and inspect receipts")
                return
            self._after_stop(record, observed)
            return
        if record.phase is HandoffPhase.STOPPED:
            stopped = self.supervisor.reconcile()
            if stopped.status is not ResultStatus.STOPPED or stopped.record.effect_operation_id != operation + ":stop" or stopped.record.selected_runtime != handoff.expected_child.selected_runtime:
                self._attention(record, "the original child has not been authoritatively drained and stopped")
                return
            refusal = self._authority_refusal(handoff)
            if refusal is not None:
                failed = self.store.change(record, phase=HandoffPhase.RESTORE_STARTING, detail=refusal[1].replace("; no stop was sent", "") + "; the stopped original runtime will be restored once")
                if failed is None:
                    return
                previous = handoff.previous
                assert previous is not None
                restored = self.supervisor.restore_stopped_original(operation + ":restore", handoff.attempt_id, self.supervisor._identity(stopped.record), previous.management_runtime_id)
                if self._is_restored(failed, restored):
                    self.store.change(failed, phase=HandoffPhase.PROCESS_RESTORED, restored_identity=self.supervisor._identity(restored.record), detail="forward authority changed after drain; exact previous executable restored once")
                else:
                    self._attention(failed, "forward authority changed after drain and the one previous-runtime return is unconfirmed")
                return
            claimed = self.store.change(record, phase=HandoffPhase.STARTING)
            if claimed is None:
                return
            result = self.supervisor.start_selected(operation + ":start", handoff.attempt_id, handoff.candidate.management_runtime_id, self.supervisor._identity(stopped.record))
            self._after_start(claimed, result)
            return
        if record.phase is HandoffPhase.STARTING:
            observed = self.supervisor.reconcile()
            if observed.record.effect_operation_id != operation + ":start":
                self._attention(record, "candidate launch is ambiguous; do not repeat start")
                return
            self._after_start(record, observed)
            return
        if record.phase is HandoffPhase.AWAITING_PROOF:
            self._verify_application(record)
            return
        if record.phase in {HandoffPhase.RESTORING, HandoffPhase.RESTORE_DRAINING}:
            observed = self.supervisor.reconcile()
            if observed.record.effect_operation_id != operation + ":restore-stop":
                self._attention(record, "safety-stop delivery is ambiguous; do not resend")
                return
            self._after_restore_stop(record, observed)
            return
        if record.phase is HandoffPhase.RESTORE_STARTING:
            observed = self.supervisor.reconcile()
            if self._is_restored(record, observed):
                self.store.change(record, phase=HandoffPhase.PROCESS_RESTORED, restored_identity=self.supervisor._identity(observed.record), detail="one exact previous executable restored; operator state was retained")
            else:
                self._attention(record, "previous-runtime launch is unresolved; no automatic retry is allowed")

    def _after_stop(self, record: HandoffRecord, observed: "Observation") -> None:
        from .bootstrap_supervisor import ResultStatus

        if observed.status is ResultStatus.STOPPED:
            self.store.change(record, phase=HandoffPhase.STOPPED)
        elif observed.status is ResultStatus.DRAINING:
            self.store.change(record, phase=HandoffPhase.DRAINING, detail="waiting for active requests to drain and exact OS child exit")
        else:
            self._attention(record, "graceful stop is unconfirmed; do not signal again or start another listener")

    def _after_start(self, record: HandoffRecord, observed: "Observation") -> None:
        from .bootstrap_supervisor import ResultStatus

        if self._is_restored(record, observed):
            self.store.change(record, phase=HandoffPhase.PROCESS_RESTORED, restored_identity=self.supervisor._identity(observed.record), detail="candidate startup failed; one exact previous executable is running")
        elif observed.status is ResultStatus.OBSERVED_RUNNING and observed.record.selected_runtime == record.handoff.candidate.management_runtime_id and observed.record.source_receipt == record.handoff.candidate.receipt_identity:
            self.store.change(
                record,
                phase=HandoffPhase.AWAITING_PROOF,
                selected_identity=self.supervisor._identity(observed.record),
                selected_generation=observed.record.child_generation,
                deadline=time.time() + self.config.verification_timeout,
                detail="reconnect to the unchanged MCP URL and call whoami or operation_get with a current credential",
            )
        else:
            self._attention(record, "candidate process did not produce an exact observed installed-runtime lifetime")

    def _verify_application(self, record: HandoffRecord) -> None:
        from .bootstrap_supervisor import ResultStatus

        observed = self.supervisor.reconcile()
        if self._is_restored(record, observed):
            self.store.change(record, phase=HandoffPhase.PROCESS_RESTORED, restored_identity=self.supervisor._identity(observed.record), detail="candidate exited before verification; exact previous process restored once")
            return
        if observed.status is not ResultStatus.OBSERVED_RUNNING or self.supervisor._identity(observed.record) != record.selected_identity:
            self._attention(record, "candidate lifetime is absent or stale; no authenticated success can be established")
            return
        if not record.challenge_observed and self._protected_challenge():
            updated = self.store.change(record, challenge_observed=True)
            if updated is None:
                return
            record = updated
        if record.challenge_observed and record.proof is not None and self._proof_matches_authority(record):
            runtime = self.supervisor._runtime(record.handoff.candidate.management_runtime_id)
            if runtime is None or not runtime.verify() or runtime.source_receipt != record.handoff.candidate.receipt_identity:
                self._begin_restore(record, "selected installed runtime changed before authenticated verification completed")
                return
            current = self.supervisor.inspect()
            if current.status is ResultStatus.OBSERVED_RUNNING and self.supervisor._identity(current.record) == record.selected_identity:
                if self.supervisor.store.reserve(current.record, replace(current.record, readiness="application_ready")):
                    self.store.change(record, phase=HandoffPhase.SUCCEEDED, detail="exact running installed runtime and a fresh protected authenticated MCP readback were verified")
            return
        if time.time() < record.deadline:
            return
        self._begin_restore(record, "bounded protected application verification failed")

    def _begin_restore(self, record: HandoffRecord, reason: str) -> None:
        failed = self.store.change(record, phase=HandoffPhase.RESTORING, detail=reason + "; attempt one exact previous-runtime restore")
        if failed is None:
            return
        stopped = self.supervisor.request_graceful_stop(record.handoff.operation_id + ":restore-stop", record.handoff.attempt_id, record.selected_identity)
        self._after_restore_stop(failed, stopped)

    def _after_restore_stop(self, record: HandoffRecord, observed: "Observation") -> None:
        from .bootstrap_supervisor import ResultStatus

        if observed.status is ResultStatus.DRAINING:
            self.store.change(record, phase=HandoffPhase.RESTORE_DRAINING, detail="candidate verification failed; awaiting cooperative candidate drain before one restore")
            return
        if observed.status is not ResultStatus.STOPPED:
            self._attention(record, "failed candidate did not confirm drain and exit; exact previous runtime was not started")
            return
        claimed = self.store.change(record, phase=HandoffPhase.RESTORE_STARTING)
        if claimed is None:
            return
        previous = record.handoff.previous
        assert previous is not None
        restored = self.supervisor.restore_previous(record.handoff.operation_id + ":restore", record.handoff.attempt_id, self.supervisor._identity(observed.record), previous.management_runtime_id)
        if self._is_restored(claimed, restored):
            self.store.change(claimed, phase=HandoffPhase.PROCESS_RESTORED, restored_identity=self.supervisor._identity(restored.record), detail="one exact previous executable restored after failed application verification; operator state was retained")
        else:
            self._attention(claimed, "the one previous-runtime attempt failed or is unobservable; manual recovery is required")

    def _is_restored(self, record: HandoffRecord, observed: "Observation") -> bool:
        from .bootstrap_supervisor import ResultStatus

        previous = record.handoff.previous
        return (
            previous is not None
            and observed.status in {ResultStatus.PROCESS_RESTORED, ResultStatus.OBSERVED_RUNNING}
            and observed.record.recovery_used
            and observed.record.selected_runtime == previous.management_runtime_id
            and observed.record.source_receipt == previous.receipt_identity
            and observed.record.child_generation >= record.handoff.expected_child.child_generation + 2
            and observed.record.effect_operation_id in {record.handoff.operation_id + ":start", record.handoff.operation_id + ":restore-stop", record.handoff.operation_id + ":restore"}
            and self.supervisor._start_token(observed.record.pid) == observed.record.start_token
            and bool(observed.record.start_token)
        )

    def _authority_refusal(self, handoff: SelfUpdateHandoff) -> tuple[HandoffPhase, str] | None:
        from .action_scopes import PendingAuthorityStatus
        from .identity import FirebaseOperatorIdentity, IdentityConfig
        from .oauth_config import load_management_settings
        from .oauth_grants import OAuthGrantService
        from .operation_store import ManagementOperationStore
        from .operations import EffectState, ReadStatus
        from .self_update_policy import REQUIRED_SCOPES, ManagementUpdateOutcome, _current_policy, _recorded_update

        try:
            assert self.config.management_config is not None
            settings = load_management_settings(self.config.management_config)
            bootstrap_path = settings.bootstrap_config_path
            if bootstrap_path is None or settings.operating_policy_path is None:
                return HandoffPhase.UNAVAILABLE, "configured original authority paths are unavailable; no stop was sent"
            journal_root = settings.operation_state_root or settings.state_root
            if journal_root != self.config.journal_root or settings.state_root != self.config.oauth_root:
                return HandoffPhase.UNAVAILABLE, "configured retained operator-state roots conflict; no stop was sent"
            journal = ManagementOperationStore(journal_root)
            lookup = journal.get(handoff.operation_id)
            if lookup.status is not ReadStatus.FOUND or lookup.record is None:
                return HandoffPhase.UNAVAILABLE, "original operation journal unavailable; no stop was sent"
            record = lookup.record
            accepted = _recorded_update(record)
            if accepted is None or record.effect.state is not EffectState.SENT_UNKNOWN or (record.effect.attempt_id, record.effect.correlation_identity, record.effect.owner_generation) != (handoff.attempt_id, handoff.correlation_identity, handoff.effect_generation):
                return HandoffPhase.DENIED, "original operation or effect generation mismatches the handoff; no stop was sent"
            intent, original, revision = accepted
            if (original.issuer, original.resource, original.project_id) != (settings.oauth.canonical_issuer, settings.oauth.resource, settings.oauth.firebase_project_id):
                return HandoffPhase.DENIED, "original authority issuer or resource conflicts with fixed configuration; no stop was sent"
            if revision != handoff.policy_revision or {p.name: p.value for p in record.intent.parameters}.get("source_sha") != handoff.candidate.source_sha:
                return HandoffPhase.DENIED, "handoff changed immutable original intent; no stop was sent"
            policy = _current_policy(settings.operating_policy_path, bootstrap_path, intent)
            if policy.outcome is ManagementUpdateOutcome.UNAVAILABLE:
                return HandoffPhase.UNAVAILABLE, "current management policy is unavailable; no stop was sent"
            if policy.outcome is not ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED or policy.policy_revision != revision:
                return HandoffPhase.DENIED, "original management policy was revoked or changed; no stop was sent"
            grants = OAuthGrantService(settings.oauth, FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json")))
            pending = grants.check_pending_authority(original, REQUIRED_SCOPES)
            if pending.status is PendingAuthorityStatus.UNAVAILABLE:
                return HandoffPhase.UNAVAILABLE, "original grant authority is unavailable; no stop was sent"
            if pending.status is not PendingAuthorityStatus.PERMITTED:
                return HandoffPhase.DENIED, "original grant or deploy consent was revoked; no stop was sent"
            if journal.get(handoff.operation_id).record != record:
                return HandoffPhase.UNAVAILABLE, "original journal revision changed during authorization; no stop was sent"
            return None
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, AssertionError):
            return HandoffPhase.UNAVAILABLE, "original authority configuration or retained state is unreadable; no stop was sent"

    def _proof_matches_authority(self, record: HandoffRecord) -> bool:
        from .operation_store import ManagementOperationStore
        from .operations import EffectState, ReadStatus
        from .self_update_policy import _recorded_update

        proof = record.proof
        if proof is None:
            return False
        lookup = ManagementOperationStore(self.config.journal_root).get(record.handoff.operation_id)
        if lookup.status is not ReadStatus.FOUND or lookup.record is None:
            return False
        operation = lookup.record
        accepted = _recorded_update(operation)
        if accepted is None or operation.effect.state not in {EffectState.SENT_UNKNOWN, EffectState.CONFIRMED}:
            return False
        if (operation.effect.attempt_id, operation.effect.correlation_identity, operation.effect.owner_generation) != (record.handoff.attempt_id, record.handoff.correlation_identity, record.handoff.effect_generation):
            return False
        if operation.intent.target.instance_id != self.config.instance_id or accepted[2] != record.handoff.policy_revision or {item.name: item.value for item in operation.intent.parameters}.get("source_sha") != record.handoff.candidate.source_sha:
            return False
        authority = lookup.record.authority
        return valid_application_proof(record, uid=authority.uid, client_id=authority.oauth_client_id)

    def _protected_challenge(self) -> bool:
        import httpx

        from .oauth_config import load_management_settings

        try:
            assert self.config.management_config is not None
            settings = load_management_settings(self.config.management_config)
            authority = urlsplit(settings.oauth.canonical_issuer).netloc
            url = self.config.listener_url.rstrip("/")
            if not url.endswith("/mcp"):
                url += "/mcp"
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=0.5) as client:
                result = client.post(url, headers={"Host": authority, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}, json={"jsonrpc": "2.0", "id": "challenge", "method": "tools/list", "params": {}})
            header = result.headers.get("www-authenticate", "")
            return result.status_code == 401 and "Bearer" in header and settings.oauth.canonical_issuer in header
        except (httpx.HTTPError, OSError, ValueError, AssertionError):
            return False

    def _attention(self, record: HandoffRecord, detail: str) -> None:
        self.store.change(record, phase=HandoffPhase.NEEDS_ATTENTION, detail=detail)


if TYPE_CHECKING:
    from .bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor, Observation, RuntimeSpec
