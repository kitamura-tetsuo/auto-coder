"""SQLite producer for durable, idempotent management operations.

This module only records decisions and consumer-supplied evidence.  Admission
is deliberately not authorization and no executor or provider is imported.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

from ..logger_config import get_logger
from .operations import (
    AcceptedAuthority,
    ActionParameter,
    AdmissionResult,
    AdmissionStatus,
    EffectAttempt,
    EffectReceipt,
    EffectState,
    HistoryEntry,
    HistoryPage,
    ManagedTarget,
    MutationResult,
    MutationStatus,
    OperationIntent,
    OperationLookup,
    OperationPage,
    OperationPhase,
    OperationRecord,
    QueryStatus,
    ReadStatus,
    validate_authority,
)

logger = get_logger(__name__)
SCHEMA_VERSION = "2"
ACTIVE_PHASES = {OperationPhase.ACCEPTED, OperationPhase.RUNNING, OperationPhase.RECONCILING, OperationPhase.NEEDS_ATTENTION}
TERMINAL_PHASES = {OperationPhase.SUCCEEDED, OperationPhase.ROLLED_BACK, OperationPhase.FAILED}
MAX_QUERY_LIMIT = 200


class ManagementOperationStore:
    """Journal rooted in an explicitly supplied management-state directory."""

    def __init__(self, state_root: Path) -> None:
        if not state_root.is_absolute():
            raise ValueError("management state root must be absolute")
        self._root = state_root
        self._path = state_root / "management-operations.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self._root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        existing = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if existing is not None and existing[0] != SCHEMA_VERSION:
            connection.close()
            raise sqlite3.DatabaseError("unsupported management operation schema")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        connection.executescript(
            """
        CREATE TABLE IF NOT EXISTS operations (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE NOT NULL,
          namespace_key TEXT UNIQUE NOT NULL, target_key TEXT NOT NULL, intent_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL, authority_json TEXT NOT NULL, phase TEXT NOT NULL,
          revision INTEGER NOT NULL, owner_attempt_id TEXT NOT NULL, safe_error_category TEXT NOT NULL,
          effect_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_target ON operations(target_key)
          WHERE phase IN ('accepted','running','reconciling','needs_attention');
        CREATE TABLE IF NOT EXISTS operation_history (
          sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL,
          phase TEXT NOT NULL, revision INTEGER NOT NULL, owner_attempt_id TEXT NOT NULL,
          effect_state TEXT NOT NULL, effect_owner_generation INTEGER NOT NULL,
          effect_attempt_id TEXT NOT NULL, correlation_identity TEXT NOT NULL,
          safe_category TEXT NOT NULL, receipt_json TEXT NOT NULL,
          recorded_at REAL NOT NULL, FOREIGN KEY(operation_id) REFERENCES operations(operation_id));
        CREATE TABLE IF NOT EXISTS worker_lifecycle (
          operation_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
          snapshot_json TEXT NOT NULL, updated_at REAL NOT NULL,
          FOREIGN KEY(operation_id) REFERENCES operations(operation_id));
        """
        )
        return connection

    def lifecycle_snapshot(self, operation_id: str) -> tuple[int, dict[str, object]] | None:
        """Read the original source evidence and each pre-effect fence atomically."""
        connection = self._connect()
        try:
            row = connection.execute("SELECT revision,snapshot_json FROM worker_lifecycle WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return None
            value = json.loads(row["snapshot_json"])
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("invalid worker lifecycle snapshot")
            return int(row["revision"]), value
        finally:
            connection.close()

    def lifecycle_save(self, operation_id: str, expected_revision: int, snapshot: dict[str, object]) -> bool:
        """CAS-write one bounded snapshot to the same durable SQLite journal."""
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 32768 or snapshot.get("schema_version") != 1:
            raise ValueError("invalid or unbounded worker lifecycle evidence")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM operations WHERE operation_id=?", (operation_id,)).fetchone() is None:
                connection.rollback()
                return False
            current = connection.execute("SELECT revision FROM worker_lifecycle WHERE operation_id=?", (operation_id,)).fetchone()
            if current is None and expected_revision == -1:
                connection.execute("INSERT INTO worker_lifecycle VALUES (?,?,?,?)", (operation_id, 0, encoded, time.time()))
            elif current is not None and current["revision"] == expected_revision:
                connection.execute("UPDATE worker_lifecycle SET revision=?,snapshot_json=?,updated_at=? WHERE operation_id=?", (expected_revision + 1, encoded, time.time(), operation_id))
            else:
                connection.rollback()
                return False
            connection.commit()
            return True
        finally:
            connection.close()

    def _before_admission_transaction(self) -> None:
        """Test seam immediately before the real admission transaction."""

    def _after_admission_commit(self) -> None:
        """Delivery seam after commit, before the acknowledgement is read."""

    def admit(self, intent: OperationIntent, authority: AcceptedAuthority) -> AdmissionResult:
        try:
            parameters = intent.normalized_parameters()
            validate_authority(authority)
            fingerprint = intent.fingerprint()
            namespace = self._namespace(intent, authority)
            connection = self._connect()
            try:
                self._before_admission_transaction()
                connection.execute("BEGIN IMMEDIATE")
                old = connection.execute("SELECT * FROM operations WHERE namespace_key=?", (namespace,)).fetchone()
                if old is not None:
                    connection.rollback()
                    record = self._record(old)
                    status = AdmissionStatus.REPLAY if old["fingerprint"] == fingerprint else AdmissionStatus.CONFLICT
                    return AdmissionResult(status, record)
                target_rows = connection.execute(
                    "SELECT * FROM operations WHERE target_key=?",
                    (intent.target.key(),),
                ).fetchall()
                target_records = tuple(self._record(row) for row in target_rows)
                busy = next((record for record in target_records if record.phase in ACTIVE_PHASES), None)
                if busy is not None:
                    connection.rollback()
                    return AdmissionResult(AdmissionStatus.BUSY, busy)
                operation_id = str(uuid.uuid4())
                normalized = OperationIntent(intent.target, intent.action, parameters, intent.idempotency_key, intent.expected_incarnation, intent.expected_artifact)
                now = time.time()
                connection.execute(
                    "INSERT INTO operations(operation_id,namespace_key,target_key,intent_json,fingerprint,authority_json,phase,revision,owner_attempt_id,safe_error_category,effect_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (operation_id, namespace, intent.target.key(), self._intent_json(normalized), fingerprint, self._authority_json(authority), OperationPhase.ACCEPTED.value, 0, "", "", self._effect_json(EffectAttempt()), now, now),
                )
                self._append_history(connection, operation_id, OperationPhase.ACCEPTED, 0, "", EffectAttempt(), "", now)
                connection.commit()
                self._after_admission_commit()
                row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None:
                    return self._confirm_ambiguous(namespace, fingerprint)
                record = self._record(row)
                logger.info("Management operation admitted id={} phase={} target={}", operation_id, record.phase.value, intent.target.key())
                return AdmissionResult(AdmissionStatus.CREATED, record)
            except (sqlite3.Error, OSError):
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                return self._confirm_ambiguous(namespace, fingerprint)
            finally:
                connection.close()
        except (ValueError, KeyError, TypeError, IndexError, sqlite3.Error, OSError) as exc:
            return AdmissionResult(AdmissionStatus.UNAVAILABLE, detail=self._safe_error(exc))

    def get(self, operation_id: str) -> OperationLookup:
        try:
            connection = self._connect()
            try:
                row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None:
                    return OperationLookup(ReadStatus.NOT_FOUND)
                return OperationLookup(ReadStatus.FOUND, self._record(row))
            finally:
                connection.close()
        except (ValueError, KeyError, TypeError, IndexError, sqlite3.Error, OSError) as exc:
            return OperationLookup(ReadStatus.UNAVAILABLE, detail=self._safe_error(exc))

    def find_by_key(self, intent: OperationIntent, authority: AcceptedAuthority) -> OperationLookup:
        try:
            connection = self._connect()
            try:
                row = connection.execute("SELECT * FROM operations WHERE namespace_key=?", (self._namespace(intent, authority),)).fetchone()
                if row is None:
                    return OperationLookup(ReadStatus.NOT_FOUND)
                return OperationLookup(ReadStatus.FOUND, self._record(row))
            finally:
                connection.close()
        except (ValueError, KeyError, TypeError, IndexError, sqlite3.Error, OSError) as exc:
            return OperationLookup(ReadStatus.UNAVAILABLE, detail=self._safe_error(exc))

    def transition(self, operation_id: str, expected_revision: int, phase: OperationPhase, *, owner_attempt_id: str = "", safe_error_category: str = "", evidence: EffectReceipt | None = None) -> MutationResult:
        if phase in TERMINAL_PHASES:
            error = self._receipt_error(evidence)
            if error:
                return MutationResult(MutationStatus.INVALID, detail=error)
        return self._mutate(operation_id, expected_revision, phase, owner_attempt_id, safe_error_category, None, evidence)

    def begin_effect(self, operation_id: str, expected_revision: int, *, owner_attempt_id: str, attempt_id: str, correlation_identity: str) -> MutationResult:
        try:
            self._bounded_evidence(owner_attempt_id, attempt_id, correlation_identity)
        except ValueError as exc:
            return MutationResult(MutationStatus.INVALID, detail=str(exc))
        effect = EffectAttempt(EffectState.SENT_UNKNOWN, expected_revision + 1, attempt_id, correlation_identity)
        return self._mutate(operation_id, expected_revision, OperationPhase.RECONCILING, owner_attempt_id, "", effect, None)

    def record_effect(self, operation_id: str, expected_revision: int, *, attempt_id: str, state: EffectState, receipt: EffectReceipt) -> MutationResult:
        if state not in {EffectState.CONFIRMED, EffectState.REJECTED}:
            return MutationResult(MutationStatus.INVALID, detail="observation must confirm or reject")
        error = self._receipt_error(receipt)
        if error:
            return MutationResult(MutationStatus.INVALID, detail=error)
        lookup = self.get(operation_id)
        if lookup.status is ReadStatus.UNAVAILABLE:
            return MutationResult(MutationStatus.UNAVAILABLE, detail=lookup.detail)
        current = lookup.record
        if current is None or current.effect.attempt_id != attempt_id or current.effect.correlation_identity != receipt.correlation_identity:
            return MutationResult(MutationStatus.STALE, current, "effect correlation mismatch")
        effect = EffectAttempt(state, current.effect.owner_generation, attempt_id, receipt.correlation_identity, receipt)
        return self._mutate(operation_id, expected_revision, current.phase, current.owner_attempt_id, current.safe_error_category, effect, receipt)

    def resolve_needs_attention(self, operation_id: str, expected_revision: int, phase: OperationPhase, *, evidence: EffectReceipt) -> MutationResult:
        error = self._receipt_error(evidence)
        if error:
            return MutationResult(MutationStatus.INVALID, detail=error)
        lookup = self.get(operation_id)
        if lookup.status is ReadStatus.UNAVAILABLE:
            return MutationResult(MutationStatus.UNAVAILABLE, detail=lookup.detail)
        current = lookup.record
        if current is None or current.phase is not OperationPhase.NEEDS_ATTENTION:
            return MutationResult(MutationStatus.INVALID, current, "operation is not awaiting attention")
        if phase in TERMINAL_PHASES:
            return self._mutate(
                operation_id,
                expected_revision,
                phase,
                current.owner_attempt_id,
                current.safe_error_category,
                None,
                evidence,
                allow_attention_resolution=True,
            )
        if current.effect.state is not EffectState.NOT_SENT and current.effect.correlation_identity != evidence.correlation_identity:
            return MutationResult(MutationStatus.INVALID, current, "resolution evidence correlation mismatch")
        effect = EffectAttempt(
            EffectState.CONFIRMED,
            current.effect.owner_generation or expected_revision + 1,
            current.effect.attempt_id,
            evidence.correlation_identity,
            evidence,
        )
        return self._mutate(
            operation_id,
            expected_revision,
            phase,
            current.owner_attempt_id,
            current.safe_error_category,
            effect,
            evidence,
            allow_attention_resolution=True,
        )

    def list_operations(self, *, cursor: int = 0, limit: int = 50, outstanding_only: bool = False, target: ManagedTarget | None = None) -> OperationPage:
        limit = self._limit(limit)
        clauses = ["sequence > ?"]
        values: list[int | str] = [cursor]
        if outstanding_only:
            clauses.append("phase IN ('accepted','running','reconciling','needs_attention')")
        if target is not None:
            clauses.append("target_key=?")
            values.append(target.key())
        try:
            connection = self._connect()
            try:
                rows = connection.execute(f"SELECT * FROM operations WHERE {' AND '.join(clauses)} ORDER BY sequence LIMIT ?", (*values, limit + 1)).fetchall()
                more = len(rows) > limit
                rows = rows[:limit]
                return OperationPage(tuple(self._record(row) for row in rows), int(rows[-1]["sequence"]) if more and rows else None)
            finally:
                connection.close()
        except (ValueError, KeyError, TypeError, IndexError, sqlite3.Error, OSError) as exc:
            return OperationPage((), None, QueryStatus.UNAVAILABLE, self._safe_error(exc))

    def history(self, operation_id: str, *, cursor: int = 0, limit: int = 50) -> HistoryPage:
        limit = self._limit(limit)
        try:
            connection = self._connect()
            try:
                rows = connection.execute("SELECT * FROM operation_history WHERE operation_id=? AND sequence>? ORDER BY sequence LIMIT ?", (operation_id, cursor, limit + 1)).fetchall()
                more = len(rows) > limit
                rows = rows[:limit]
                entries = tuple(self._history(row) for row in rows)
                return HistoryPage(entries, int(rows[-1]["sequence"]) if more and rows else None)
            finally:
                connection.close()
        except (ValueError, KeyError, TypeError, IndexError, sqlite3.Error, OSError) as exc:
            return HistoryPage((), None, QueryStatus.UNAVAILABLE, self._safe_error(exc))

    def _mutate(
        self,
        operation_id: str,
        expected_revision: int,
        phase: OperationPhase,
        owner: str,
        category: str,
        effect: EffectAttempt | None,
        evidence: EffectReceipt | None,
        *,
        allow_attention_resolution: bool = False,
    ) -> MutationResult:
        try:
            self._bounded_evidence(owner, category)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None:
                    connection.rollback()
                    return MutationResult(MutationStatus.INVALID, detail="unknown operation")
                current = self._record(row)
                if current.revision != expected_revision or current.phase in TERMINAL_PHASES:
                    connection.rollback()
                    return MutationResult(MutationStatus.STALE, current)
                if current.phase is OperationPhase.NEEDS_ATTENTION and phase is not OperationPhase.NEEDS_ATTENTION and not allow_attention_resolution:
                    connection.rollback()
                    return MutationResult(MutationStatus.INVALID, current, "NEEDS_ATTENTION requires explicit evidence-bearing resolution")
                if effect is not None and effect.state is EffectState.SENT_UNKNOWN and current.effect.state is EffectState.SENT_UNKNOWN:
                    connection.rollback()
                    return MutationResult(MutationStatus.INVALID, current, "unresolved effect cannot be replaced")
                next_effect = effect or current.effect
                if phase in TERMINAL_PHASES:
                    error = self._receipt_error(evidence)
                    if error:
                        connection.rollback()
                        return MutationResult(MutationStatus.INVALID, current, error)
                    assert evidence is not None
                    if next_effect.state is not EffectState.NOT_SENT and next_effect.correlation_identity != evidence.correlation_identity:
                        connection.rollback()
                        return MutationResult(MutationStatus.INVALID, current, "terminal evidence correlation mismatch")
                    if next_effect.state is not EffectState.REJECTED or next_effect.receipt != evidence:
                        next_effect = EffectAttempt(
                            EffectState.CONFIRMED,
                            next_effect.owner_generation or expected_revision + 1,
                            next_effect.attempt_id,
                            evidence.correlation_identity,
                            evidence,
                        )
                revision = expected_revision + 1
                connection.execute(
                    "UPDATE operations SET phase=?,revision=?,owner_attempt_id=?,safe_error_category=?,effect_json=?,updated_at=? WHERE operation_id=? AND revision=?", (phase.value, revision, owner, category, self._effect_json(next_effect), time.time(), operation_id, expected_revision)
                )
                self._append_history(connection, operation_id, phase, revision, owner, next_effect, category, time.time())
                connection.commit()
                updated = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                return MutationResult(MutationStatus.UPDATED, self._record(updated))
            finally:
                connection.close()
        except (ValueError, KeyError, TypeError, IndexError, sqlite3.Error, OSError) as exc:
            return MutationResult(MutationStatus.UNAVAILABLE, detail=self._safe_error(exc))

    def _confirm_ambiguous(self, namespace: str, fingerprint: str) -> AdmissionResult:
        try:
            connection = self._connect()
            try:
                row = connection.execute("SELECT * FROM operations WHERE namespace_key=?", (namespace,)).fetchone()
                if row is not None and row["fingerprint"] == fingerprint:
                    return AdmissionResult(AdmissionStatus.REPLAY, self._record(row), "confirmed after uncertain commit")
            finally:
                connection.close()
        except (sqlite3.Error, OSError):
            pass
        return AdmissionResult(AdmissionStatus.UNAVAILABLE, detail="persistence outcome unavailable")

    @staticmethod
    def _namespace(intent: OperationIntent, authority: AcceptedAuthority) -> str:
        return json.dumps((authority.issuer, authority.resource, authority.project_id, authority.uid, authority.oauth_client_id, intent.target.key(), intent.idempotency_key), separators=(",", ":"))

    @staticmethod
    def _intent_json(intent: OperationIntent) -> str:
        return json.dumps(
            {
                "target": [intent.target.instance_id, intent.target.channel, intent.target.repository],
                "action": intent.action,
                "parameters": [[p.name, p.value] for p in intent.parameters],
                "idempotency_key": intent.idempotency_key,
                "expected_incarnation": intent.expected_incarnation,
                "expected_artifact": intent.expected_artifact,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _authority_json(value: AcceptedAuthority) -> str:
        return json.dumps(
            {
                "issuer": value.issuer,
                "resource": value.resource,
                "project_id": value.project_id,
                "uid": value.uid,
                "oauth_client_id": value.oauth_client_id,
                "trusted_grant_id": value.trusted_grant_id,
                "accepted_scopes": list(value.accepted_scopes),
                "authentication_time": value.authentication_time,
                "accepted_at": value.accepted_at,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _effect_json(value: EffectAttempt) -> str:
        receipt = None if value.receipt is None else [value.receipt.correlation_identity, value.receipt.observation_source, value.receipt.observed_at, value.receipt.safe_result]
        return json.dumps([value.state.value, value.owner_generation, value.attempt_id, value.correlation_identity, receipt], separators=(",", ":"))

    @classmethod
    def _record(cls, row: sqlite3.Row) -> OperationRecord:
        intent_data = json.loads(row["intent_json"])
        target = ManagedTarget(*intent_data["target"])
        intent = OperationIntent(target, intent_data["action"], tuple(ActionParameter(*p) for p in intent_data["parameters"]), intent_data["idempotency_key"], intent_data["expected_incarnation"], intent_data["expected_artifact"])
        auth = json.loads(row["authority_json"])
        authority = AcceptedAuthority(auth["issuer"], auth["resource"], auth["project_id"], auth["uid"], auth["oauth_client_id"], auth["trusted_grant_id"], tuple(auth["accepted_scopes"]), auth["authentication_time"], auth["accepted_at"])
        effect = cls._parse_effect(row["effect_json"])
        return OperationRecord(row["operation_id"], intent, row["fingerprint"], authority, OperationPhase(row["phase"]), row["revision"], row["owner_attempt_id"], row["safe_error_category"], effect)

    @staticmethod
    def _parse_effect(raw: str) -> EffectAttempt:
        data = json.loads(raw)
        receipt = None if data[4] is None else EffectReceipt(*data[4])
        return EffectAttempt(EffectState(data[0]), data[1], data[2], data[3], receipt)

    @classmethod
    def _history(cls, row: sqlite3.Row) -> HistoryEntry:
        receipt_data = json.loads(row["receipt_json"])
        receipt = None if receipt_data is None else EffectReceipt(*receipt_data)
        return HistoryEntry(
            row["sequence"],
            row["operation_id"],
            OperationPhase(row["phase"]),
            row["revision"],
            row["owner_attempt_id"],
            EffectState(row["effect_state"]),
            row["effect_owner_generation"],
            row["effect_attempt_id"],
            row["correlation_identity"],
            row["safe_category"],
            receipt,
            row["recorded_at"],
        )

    @classmethod
    def _append_history(cls, connection: sqlite3.Connection, operation_id: str, phase: OperationPhase, revision: int, owner: str, effect: EffectAttempt, category: str, now: float) -> None:
        receipt = None if effect.receipt is None else [effect.receipt.correlation_identity, effect.receipt.observation_source, effect.receipt.observed_at, effect.receipt.safe_result]
        connection.execute(
            "INSERT INTO operation_history(operation_id,phase,revision,owner_attempt_id,effect_state,effect_owner_generation,effect_attempt_id,correlation_identity,safe_category,receipt_json,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, phase.value, revision, owner, effect.state.value, effect.owner_generation, effect.attempt_id, effect.correlation_identity, category, json.dumps(receipt), now),
        )

    @staticmethod
    def _limit(limit: int) -> int:
        if limit < 1 or limit > MAX_QUERY_LIMIT:
            raise ValueError("query limit out of bounds")
        return limit

    @staticmethod
    def _bounded_evidence(*values: str) -> None:
        if any(len(value) > 512 or "\x00" in value for value in values):
            raise ValueError("evidence field out of bounds")

    @classmethod
    def _receipt_error(cls, receipt: EffectReceipt | None) -> str:
        if receipt is None:
            return "terminal transition requires evidence"
        if not receipt.correlation_identity or not receipt.observation_source or not receipt.safe_result:
            return "terminal evidence fields must be nonempty"
        if not math.isfinite(receipt.observed_at) or receipt.observed_at <= 0:
            return "terminal evidence observation time must be positive"
        try:
            cls._bounded_evidence(receipt.correlation_identity, receipt.observation_source, receipt.safe_result)
        except ValueError as exc:
            return str(exc)
        return ""

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        return type(exc).__name__
