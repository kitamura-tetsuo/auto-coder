"""Fail-closed read-only evidence for a locally requested management recovery.

This module does not authorize or launch a process. Its caller must retain the
owner and durable admission fences while consuming these observations.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .bootstrap_store import ChildPhase, ChildRecord
from .bootstrap_supervisor import Action, BootstrapConfig, ManagementBootstrapSupervisor, ResultStatus
from .operation_store import ACTIVE_PHASES, ManagementOperationStore
from .operations import EffectState, HistoryEntry, OperationPhase, OperationRecord, validate_authority
from .self_update_handoff import HandoffPhase, _record_row
from .version_staging import ManagementReadyReceipt


class EvidenceUnavailable(ValueError):
    """Authoritative evidence was absent, inconsistent, or inaccessible."""


@dataclass(frozen=True)
class PreviousLifetime:
    pid: int
    start_token: str
    generation: int
    runtime: str
    incarnation: str
    launch_nonce: str
    runtime_receipt: str
    source_receipt: str


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise EvidenceUnavailable("required store unavailable")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2, isolation_level=None)
    connection.row_factory = sqlite3.Row
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        connection.close()
        raise EvidenceUnavailable("required store is corrupt")
    return connection


def _metadata(connection: sqlite3.Connection, expected: dict[str, str]) -> None:
    actual = dict(connection.execute("SELECT key,value FROM metadata"))
    if actual != expected:
        raise EvidenceUnavailable("store schema or instance mismatch")


def _schema(connection: sqlite3.Connection, tables: dict[str, tuple[str, ...]]) -> None:
    for name, expected in tables.items():
        columns = tuple(row["name"] for row in connection.execute(f"PRAGMA table_info({name})"))
        if columns != expected:
            raise EvidenceUnavailable("required store schema is incomplete")


def _validate_child_record(record: ChildRecord) -> None:
    text = (record.instance_id, record.selected_runtime, record.previous_runtime, record.start_token, record.supervisor_incarnation, record.readiness, record.exit_cause, record.effect_operation_id, record.effect_attempt_id, record.drain_nonce, record.runtime_receipt, record.source_receipt)
    if any(not isinstance(value, str) or len(value) > 512 or "\x00" in value for value in text):
        raise EvidenceUnavailable("child record text is invalid")
    if type(record.pid) is not int or record.pid < 0 or type(record.child_generation) is not int or record.child_generation < 0 or type(record.intended_stop) is not bool or type(record.recovery_used) is not bool or record.readiness not in {"not_observed", "application_ready"}:
        raise EvidenceUnavailable("child record types are invalid")


def read_child_evidence(config: BootstrapConfig) -> tuple[ChildRecord, PreviousLifetime]:
    """Recover the last generation's exact lifetime from retained effect rows."""
    try:
        with closing(_read_only(config.state_root / "management-child.sqlite3")) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            marker = metadata.get("recovery_mailbox_id")
            if metadata.get("schema_version") != "1" or set(metadata) - {"schema_version", "recovery_mailbox_id"} or (marker is not None and (not isinstance(marker, str) or str(uuid.UUID(marker)) != marker)):
                raise EvidenceUnavailable("child ledger schema or recovery marker mismatch")
            _schema(
                connection,
                {
                    "child_state": ("instance_id", "record_json", "updated_at"),
                    "effects": ("operation_id", "attempt_id", "action", "runtime_ref", "expected_identity", "phase", "result_json", "created_at", "updated_at"),
                    "supervisor_presence": ("instance_id", "incarnation", "pid", "start_token", "updated_at"),
                },
            )
            rows = connection.execute("SELECT instance_id,record_json FROM child_state").fetchall()
            if len(rows) != 1 or rows[0]["instance_id"] != config.instance_id:
                raise EvidenceUnavailable("child instance mismatch")
            raw = json.loads(rows[0]["record_json"])
            if not isinstance(raw, dict) or set(raw) != set(ChildRecord.__dataclass_fields__):
                raise EvidenceUnavailable("child record fields are incomplete")
            raw["phase"] = ChildPhase(raw["phase"])
            child = ChildRecord(**raw)
            _validate_child_record(child)
            if child.instance_id != config.instance_id or child.phase not in {ChildPhase.STOPPED, ChildPhase.NEEDS_ATTENTION} or child.pid != 0 or child.start_token:
                raise EvidenceUnavailable("child is not safely stopped")
            if child.child_generation <= 0 or not child.selected_runtime or not child.supervisor_incarnation:
                raise EvidenceUnavailable("selected child identity unavailable")
            effects = connection.execute("SELECT * FROM effects ORDER BY rowid").fetchall()
            found: set[PreviousLifetime] = set()
            for effect in effects:
                if effect["phase"] != "observed":
                    raise EvidenceUnavailable("unresolved child effect")
                result = json.loads(effect["result_json"])
                status = ResultStatus(result["status"])
                if status in {ResultStatus.UNKNOWN, ResultStatus.DRAINING}:
                    raise EvidenceUnavailable("child effect has an unresolved outcome")
                if set(result) != {"status", "detail", "identity", "record", "process_ready", "application_ready"} or effect["action"] not in {*(action.value for action in Action), "RECOVER_SELECTED", "OBSERVE_SAFETY_RESTORE"}:
                    raise EvidenceUnavailable("invalid child effect result")
                effect_text = (effect["operation_id"], effect["attempt_id"], effect["action"], effect["runtime_ref"], effect["expected_identity"], result["detail"], result["identity"])
                if any(not isinstance(value, str) or len(value) > 512 or "\x00" in value for value in effect_text) or not effect["operation_id"] or not effect["attempt_id"]:
                    raise EvidenceUnavailable("invalid child effect identity")
                if type(result["process_ready"]) is not bool or result["process_ready"] != (status in {ResultStatus.OBSERVED_RUNNING, ResultStatus.PROCESS_RESTORED}) or result["application_ready"] is not False:
                    raise EvidenceUnavailable("invalid child effect readiness")
                raw_record = result["record"]
                if not isinstance(raw_record, dict) or set(raw_record) != set(ChildRecord.__dataclass_fields__):
                    raise EvidenceUnavailable("invalid child effect record")
                raw_record["phase"] = ChildPhase(raw_record["phase"])
                record = ChildRecord(**raw_record)
                _validate_child_record(record)
                if record.instance_id != config.instance_id or result["identity"] != ManagementBootstrapSupervisor._identity(record):
                    raise EvidenceUnavailable("effect instance or lifetime mismatch")
                if record.child_generation != child.child_generation or record.selected_runtime != child.selected_runtime:
                    continue
                if type(record.pid) is int and record.pid > 0 and isinstance(record.start_token, str) and record.start_token:
                    if effect["action"] not in {"START_SELECTED", "RESTORE_PREVIOUS", "RECOVER_SELECTED", "OBSERVE_SAFETY_RESTORE"} or status not in {ResultStatus.OBSERVED_RUNNING, ResultStatus.PROCESS_RESTORED} or result["process_ready"] is not True:
                        raise EvidenceUnavailable("retained process lifetime lacks launch evidence")
                    if effect["action"] == "OBSERVE_SAFETY_RESTORE" and (
                        effect["operation_id"] != child.effect_operation_id + ":safety-observation" or effect["attempt_id"] != child.effect_attempt_id or effect["expected_identity"] != result["identity"] or status is not ResultStatus.PROCESS_RESTORED
                    ):
                        raise EvidenceUnavailable("safety return observation conflicts")
                    lifetime = PreviousLifetime(record.pid, record.start_token, record.child_generation, record.selected_runtime, record.supervisor_incarnation, record.drain_nonce, record.runtime_receipt, record.source_receipt)
                    if (
                        not lifetime.start_token.isdecimal()
                        or int(lifetime.start_token) <= 0
                        or str(int(lifetime.start_token)) != lifetime.start_token
                        or any(not isinstance(value, str) or not value or len(value) > 512 for value in (lifetime.start_token, lifetime.runtime, lifetime.incarnation, lifetime.launch_nonce, lifetime.runtime_receipt))
                    ):
                        raise EvidenceUnavailable("invalid retained lifetime")
                    found.add(lifetime)
            if len(found) != 1:
                raise EvidenceUnavailable("exact retained lifetime unavailable or conflicting")
            previous = next(iter(found))
            if (previous.incarnation, previous.launch_nonce, previous.runtime_receipt, previous.source_receipt) != (child.supervisor_incarnation, child.drain_nonce, child.runtime_receipt, child.source_receipt):
                raise EvidenceUnavailable("retained lifetime conflicts with selected child")
            return child, previous
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
        raise EvidenceUnavailable("child evidence unavailable") from exc


def assert_current_owner(config: BootstrapConfig, incarnation: str) -> None:
    """Verify that this process is the currently recorded live Supervisor."""
    try:
        with closing(_read_only(config.state_root / "management-child.sqlite3")) as connection:
            rows = connection.execute("SELECT instance_id,incarnation,pid,start_token,updated_at FROM supervisor_presence").fetchall()
        if len(rows) != 1:
            raise EvidenceUnavailable("Supervisor presence unavailable or conflicting")
        row = rows[0]
        now = time.time()
        updated = row["updated_at"]
        if not isinstance(updated, (int, float)) or isinstance(updated, bool) or not math.isfinite(updated) or not 0 <= now - updated <= max(30, config.graceful_timeout + 5):
            raise EvidenceUnavailable("Supervisor presence is stale")
        token = ManagementBootstrapSupervisor._start_token(os.getpid())
        if not token or (row["instance_id"], row["incarnation"], row["pid"], row["start_token"]) != (config.instance_id, incarnation, os.getpid(), token):
            raise EvidenceUnavailable("this process is not the owning Supervisor")
        lock_path = config.state_root / ".supervisor-owner.lock"
        descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                raise EvidenceUnavailable("Supervisor owner fence is not held")
        finally:
            os.close(descriptor)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
        raise EvidenceUnavailable("Supervisor ownership evidence unavailable") from exc


def assert_journal_clear(config: BootstrapConfig) -> None:
    """Read every existing operation and handoff without creating either store."""
    try:
        with closing(_read_only(config.journal_root / "management-operations.sqlite3")) as connection:
            _metadata(connection, {"schema_version": "2"})
            _schema(
                connection,
                {
                    "operations": ("sequence", "operation_id", "namespace_key", "target_key", "intent_json", "fingerprint", "authority_json", "phase", "revision", "owner_attempt_id", "safe_error_category", "effect_json", "created_at", "updated_at"),
                    "operation_history": ("sequence", "operation_id", "phase", "revision", "owner_attempt_id", "effect_state", "effect_owner_generation", "effect_attempt_id", "correlation_identity", "safe_category", "receipt_json", "recorded_at"),
                    "worker_lifecycle": ("operation_id", "revision", "snapshot_json", "updated_at"),
                    "beta_runtime_smoke": ("operation_id", "revision", "snapshot_json", "updated_at"),
                },
            )
            if "one_active_target" not in {row["name"] for row in connection.execute("PRAGMA index_list(operations)")}:
                raise EvidenceUnavailable("operation lane index unavailable")
            operations: dict[str, OperationRecord] = {}
            for row in connection.execute("SELECT * FROM operations ORDER BY sequence"):
                record = ManagementOperationStore._record(row)
                validate_authority(record.authority)
                record.intent.normalized_parameters()
                if row["fingerprint"] != record.intent.fingerprint() or row["namespace_key"] != ManagementOperationStore._namespace(record.intent, record.authority) or row["target_key"] != record.intent.target.key() or row["phase"] != record.phase.value:
                    raise EvidenceUnavailable("operation target or phase mismatch")
                if record.operation_id in operations:
                    raise EvidenceUnavailable("duplicate operation identity")
                operations[record.operation_id] = record
                if record.intent.target.instance_id == config.instance_id and record.intent.target.channel == "management" and (record.phase in ACTIVE_PHASES or record.effect.state is EffectState.SENT_UNKNOWN):
                    raise EvidenceUnavailable("management operation retains the lane")
            last_history: dict[str, HistoryEntry] = {}
            for row in connection.execute("SELECT * FROM operation_history ORDER BY sequence"):
                entry = ManagementOperationStore._history(row)
                operation = operations.get(entry.operation_id)
                previous = last_history.get(entry.operation_id)
                if operation is None or type(entry.revision) is not int or entry.revision != (0 if previous is None else previous.revision + 1) or not isinstance(entry.recorded_at, (int, float)) or isinstance(entry.recorded_at, bool) or not math.isfinite(entry.recorded_at):
                    raise EvidenceUnavailable("operation history is incomplete or inconsistent")
                last_history[entry.operation_id] = entry
            for operation_id, operation in operations.items():
                last_entry = last_history.get(operation_id)
                if last_entry is None or (last_entry.revision, last_entry.phase, last_entry.owner_attempt_id, last_entry.effect_state, last_entry.effect_owner_generation, last_entry.effect_attempt_id, last_entry.correlation_identity, last_entry.receipt, last_entry.safe_category) != (
                    operation.revision,
                    operation.phase,
                    operation.owner_attempt_id,
                    operation.effect.state,
                    operation.effect.owner_generation,
                    operation.effect.attempt_id,
                    operation.effect.correlation_identity,
                    operation.effect.receipt,
                    operation.safe_error_category,
                ):
                    raise EvidenceUnavailable("operation history differs from retained state")
        with closing(_read_only(config.state_root / "management-self-update.sqlite3")) as connection:
            _metadata(connection, {"schema_version": "1", "instance_id": config.instance_id})
            _schema(connection, {"handoffs": ("operation_id", "intent_json", "record_json", "phase"), "runtimes": ("reference", "receipt_json")})
            terminal = {HandoffPhase.SUCCEEDED, HandoffPhase.PROCESS_RESTORED, HandoffPhase.DENIED, HandoffPhase.UNAVAILABLE}
            bindings: dict[str, ManagementReadyReceipt] = {}
            for row in connection.execute("SELECT intent_json,record_json,phase FROM handoffs ORDER BY rowid"):
                handoff_record = _record_row(row)
                handoff = handoff_record.handoff
                if handoff.expected_child.instance_id != config.instance_id or handoff.candidate.instance_id != config.instance_id or handoff.previous is None or handoff.previous.instance_id != config.instance_id or row["phase"] != handoff_record.phase.value or handoff_record.phase not in terminal:
                    raise EvidenceUnavailable("handoff retains or conflicts with the lane")
                for receipt in (handoff.candidate, handoff.previous):
                    if receipt.management_runtime_id in bindings and bindings[receipt.management_runtime_id] != receipt:
                        raise EvidenceUnavailable("handoff runtime identities conflict")
                    bindings[receipt.management_runtime_id] = receipt
            retained: dict[str, ManagementReadyReceipt] = {}
            for row in connection.execute("SELECT reference,receipt_json FROM runtimes"):
                receipt = ManagementReadyReceipt(**json.loads(row["receipt_json"]))
                if row["reference"] != receipt.management_runtime_id or receipt.instance_id != config.instance_id:
                    raise EvidenceUnavailable("retained runtime identity mismatch")
                retained[row["reference"]] = receipt
            if retained != bindings:
                raise EvidenceUnavailable("retained runtime evidence differs from handoffs")
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
        raise EvidenceUnavailable("operation or handoff evidence unavailable") from exc


def is_completed_safety_restore(config: BootstrapConfig, child: ChildRecord, previous: PreviousLifetime) -> bool:
    """Join an exact restored lifetime to its terminal handoff and journal."""
    from .self_update_operation import ManagementSelfUpdateHandler

    suffix = next((value for value in (":start", ":restore-stop", ":restore") if child.effect_operation_id.endswith(value)), None)
    if suffix is None:
        return False
    operation_id = child.effect_operation_id[: -len(suffix)]
    identity = f"{previous.pid}:{previous.start_token}:{previous.generation}:{previous.runtime}:{previous.incarnation}"
    try:
        with closing(_read_only(config.state_root / "management-self-update.sqlite3")) as connection:
            row = connection.execute("SELECT intent_json,record_json,phase FROM handoffs WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return False
            handoff = _record_row(row)
        with closing(_read_only(config.journal_root / "management-operations.sqlite3")) as connection:
            row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return False
            operation = ManagementOperationStore._record(row)
        receipt = handoff.handoff.previous
        evidence = operation.effect.receipt
        result = json.loads(evidence.safe_result) if evidence is not None else None
        return (
            handoff.phase is HandoffPhase.PROCESS_RESTORED
            and handoff.restored_identity == identity
            and receipt is not None
            and (receipt.instance_id, receipt.management_runtime_id, receipt.receipt_identity, "sha256:" + receipt.python_executable_sha256) == (config.instance_id, child.selected_runtime, child.source_receipt, child.runtime_receipt)
            and previous.generation >= handoff.handoff.expected_child.child_generation + 2
            and operation.intent.target.instance_id == config.instance_id
            and operation.intent.target.channel == "management"
            and operation.phase is OperationPhase.ROLLED_BACK
            and operation.effect.state is EffectState.CONFIRMED
            and evidence is not None
            and evidence.correlation_identity == operation.effect.correlation_identity
            and evidence.observation_source == "management-supervisor-authenticated-receipt"
            and result == {"status": handoff.phase.value, "source_sha": handoff.handoff.candidate.source_sha, "runtime_id": handoff.handoff.candidate.management_runtime_id, "generation": handoff.selected_generation}
            and ManagementSelfUpdateHandler._joined(operation, handoff)
        )
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, IndexError):
        return False


def assert_kernel_absence(config: BootstrapConfig, previous: PreviousLifetime) -> None:
    """Require a conclusive prior lifetime and configured-namespace listener check."""
    try:
        if not hasattr(os, "pidfd_open"):
            raise EvidenceUnavailable("pidfd unavailable")
        namespace = os.readlink("/proc/self/ns/net")
        pid_namespace = os.readlink("/proc/self/ns/pid")
        self_stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        if int(self_stat.split(" ", 1)[0]) != os.getpid() or os.readlink(f"/proc/{os.getpid()}/ns/pid") != pid_namespace:
            raise EvidenceUnavailable("procfs does not describe the owning PID namespace")
        try:
            pidfd = os.pidfd_open(previous.pid, 0)
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                raise
        else:
            try:
                stat = Path(f"/proc/{previous.pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
                if len(stat) < 20 or not stat[19].isdigit():
                    raise EvidenceUnavailable("process lifetime is unreadable")
                if stat[19] == previous.start_token:
                    raise EvidenceUnavailable("previous process lifetime remains present")
            finally:
                os.close(pidfd)
        endpoint = urlsplit(config.listener_url)
        if endpoint.hostname not in {"127.0.0.1", "::1"} or endpoint.port is None or endpoint.path != "/mcp" or endpoint.query or endpoint.fragment:
            raise EvidenceUnavailable("invalid configured endpoint")
        for table in ("/proc/self/net/tcp", "/proc/self/net/tcp6"):
            lines = Path(table).read_text(encoding="ascii").splitlines()
            if not lines or "local_address" not in lines[0] or "st" not in lines[0]:
                raise EvidenceUnavailable("listener evidence malformed")
            for line in lines[1:]:
                parts = line.split()
                if len(parts) < 4:
                    raise EvidenceUnavailable("listener evidence incomplete")
                address = parts[1].rsplit(":", 1)
                if len(address) != 2:
                    raise EvidenceUnavailable("listener evidence malformed")
                if int(address[1], 16) == endpoint.port and parts[3] == "0A":
                    raise EvidenceUnavailable("configured port has a listener")
        if os.readlink("/proc/self/ns/net") != namespace or os.readlink("/proc/self/ns/pid") != pid_namespace:
            raise EvidenceUnavailable("kernel namespace changed")
    except (OSError, ValueError, IndexError, UnicodeError) as exc:
        raise EvidenceUnavailable("kernel absence unavailable") from exc
