"""Private durable ledger for the management child bootstrap supervisor."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

SCHEMA_VERSION = "1"


class ChildPhase(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    UNKNOWN = "unknown"
    NEEDS_ATTENTION = "needs_attention"


@dataclass(frozen=True)
class ChildRecord:
    instance_id: str
    phase: ChildPhase = ChildPhase.STOPPED
    selected_runtime: str = ""
    previous_runtime: str = ""
    pid: int = 0
    start_token: str = ""
    child_generation: int = 0
    supervisor_incarnation: str = ""
    intended_stop: bool = False
    readiness: str = "not_observed"
    recovery_used: bool = False
    exit_cause: str = ""
    effect_operation_id: str = ""
    effect_attempt_id: str = ""


@dataclass(frozen=True)
class EffectRecord:
    operation_id: str
    attempt_id: str
    action: str
    runtime_ref: str
    expected_identity: str
    phase: str
    result_json: str
    created_at: float
    updated_at: float


class BootstrapStore:
    """Versioned state isolated from OAuth and management-operation databases."""

    def __init__(self, root: Path, instance_id: str) -> None:
        if not root.is_absolute() or not instance_id or len(instance_id) > 128:
            raise ValueError("an absolute state root and bounded instance id are required")
        self.root = root
        self.instance_id = instance_id
        self.path = root / "management-child.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if version is not None and version[0] != SCHEMA_VERSION:
            connection.close()
            raise sqlite3.DatabaseError("unsupported management child schema")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version',?)", (SCHEMA_VERSION,))
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS child_state(
              instance_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS effects(
              operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, action TEXT NOT NULL,
              runtime_ref TEXT NOT NULL, expected_identity TEXT NOT NULL, phase TEXT NOT NULL,
              result_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """
        )
        return connection

    def load(self) -> ChildRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT record_json FROM child_state WHERE instance_id=?", (self.instance_id,)).fetchone()
        if row is None:
            return ChildRecord(self.instance_id)
        try:
            value = json.loads(row[0])
            value["phase"] = ChildPhase(value["phase"])
            record = ChildRecord(**value)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise sqlite3.DatabaseError("corrupt management child record") from exc
        if record.instance_id != self.instance_id:
            raise sqlite3.DatabaseError("management child instance mismatch")
        return record

    def save(self, record: ChildRecord) -> None:
        if record.instance_id != self.instance_id:
            raise ValueError("management child instance mismatch")
        encoded = asdict(record)
        encoded["phase"] = record.phase.value
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO child_state VALUES(?,?,?) ON CONFLICT(instance_id) DO UPDATE SET record_json=excluded.record_json,updated_at=excluded.updated_at",
                (self.instance_id, json.dumps(encoded, sort_keys=True), time.time()),
            )

    def begin_effect(self, operation_id: str, attempt_id: str, action: str, runtime_ref: str, expected_identity: str) -> EffectRecord:
        for value in (operation_id, attempt_id, action, runtime_ref, expected_identity):
            if len(value) > 256 or "\x00" in value:
                raise ValueError("effect identity is not bounded")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM effects WHERE operation_id=?", (operation_id,)).fetchone()
            if old is not None:
                connection.rollback()
                effect = self._effect(old)
                if (effect.attempt_id, effect.action, effect.runtime_ref, effect.expected_identity) != (attempt_id, action, runtime_ref, expected_identity):
                    raise ValueError("operation replay conflicts with its immutable effect")
                return effect
            connection.execute("INSERT INTO effects VALUES(?,?,?,?,?,?,?,?,?)", (operation_id, attempt_id, action, runtime_ref, expected_identity, "prepared", "", now, now))
            connection.commit()
        return EffectRecord(operation_id, attempt_id, action, runtime_ref, expected_identity, "prepared", "", now, now)

    def finish_effect(self, operation_id: str, phase: str, result: dict[str, object]) -> EffectRecord:
        encoded = json.dumps(result, sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM effects WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise ValueError("effect was not prepared")
            connection.execute("UPDATE effects SET phase=?,result_json=?,updated_at=? WHERE operation_id=?", (phase, encoded, time.time(), operation_id))
            connection.commit()
            updated = connection.execute("SELECT * FROM effects WHERE operation_id=?", (operation_id,)).fetchone()
        assert updated is not None
        return self._effect(updated)

    @staticmethod
    def _effect(row: sqlite3.Row) -> EffectRecord:
        return EffectRecord(*(row[key] for key in ("operation_id", "attempt_id", "action", "runtime_ref", "expected_identity", "phase", "result_json", "created_at", "updated_at")))
