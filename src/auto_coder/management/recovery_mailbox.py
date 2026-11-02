"""Fixed local mailbox for one operator-requested management recovery."""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecoveryIntent:
    request_id: str
    instance_id: str
    supervisor_incarnation: str
    child_generation: int
    runtime: str
    runtime_receipt: str
    source_receipt: str
    management_config: str
    management_config_sha256: str

    def validate(self) -> None:
        if not isinstance(self.request_id, str) or str(uuid.UUID(self.request_id)) != self.request_id or type(self.child_generation) is not int or self.child_generation <= 0:
            raise ValueError("invalid recovery request identity")
        if any(not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value for value in (self.instance_id, self.supervisor_incarnation, self.runtime, self.runtime_receipt, self.management_config, self.management_config_sha256)):
            raise ValueError("invalid expected recovery identity")
        if not isinstance(self.source_receipt, str) or len(self.source_receipt) > 256 or "\x00" in self.source_receipt:
            raise ValueError("invalid expected source receipt")
        if not Path(self.management_config).is_absolute() or len(self.management_config_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.management_config_sha256):
            raise ValueError("invalid management configuration identity")


@dataclass(frozen=True)
class RecoveryRecord:
    intent: RecoveryIntent
    status: str
    detail: str
    observation_id: str
    child_identity: str
    observation_started: float
    observation_deadline: float


class RecoveryMailbox:
    """A local-only durable mailbox; possession of OAuth authority grants no write access."""

    def __init__(self, root: Path, instance_id: str) -> None:
        if not root.is_absolute() or not instance_id or len(instance_id) > 128:
            raise ValueError("invalid recovery mailbox location")
        self.root = root
        self.instance_id = instance_id
        self.path = root / "management-recovery.sqlite3"

    def _directory_authorized(self) -> None:
        info = self.root.stat()
        if self.root.is_symlink() or info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
            raise PermissionError("recovery mailbox directory is not owned and protected")

    def _child_marker(self) -> str | None:
        path = self.root / "management-child.sqlite3"
        if path.is_symlink() or not path.is_file():
            raise sqlite3.DatabaseError("management child ledger unavailable")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            if metadata.get("schema_version") != "1" or set(metadata) - {"schema_version", "recovery_mailbox_id"}:
                raise sqlite3.DatabaseError("management child ledger schema mismatch")
            marker = metadata.get("recovery_mailbox_id")
            if marker is not None and (not isinstance(marker, str) or str(uuid.UUID(marker)) != marker):
                raise sqlite3.DatabaseError("invalid recovery mailbox marker")
            return marker
        finally:
            connection.close()

    def initialize(self) -> None:
        """Only the running Supervisor initializes or joins this mailbox."""
        self._directory_authorized()
        existing_marker = self._child_marker()
        marker = existing_marker
        if marker is not None and not self.path.exists():
            raise sqlite3.DatabaseError("retained recovery mailbox is missing")
        if marker is None and (self.path.exists() or self.path.is_symlink()):
            raise sqlite3.DatabaseError("recovery mailbox has no retained ledger identity")
        if not self.path.exists() and not self.path.is_symlink():
            marker = str(uuid.uuid4())
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            connection = sqlite3.connect(self.path, timeout=2, isolation_level=None)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                connection.execute("INSERT INTO metadata VALUES('schema_version','1')")
                connection.execute("INSERT INTO metadata VALUES('instance_id',?)", (self.instance_id,))
                connection.execute("INSERT INTO metadata VALUES('mailbox_id',?)", (marker,))
                connection.execute(
                    "CREATE TABLE recovery_requests(request_id TEXT PRIMARY KEY,intent_json TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,"
                    "observation_id TEXT NOT NULL,child_identity TEXT NOT NULL,observation_started REAL NOT NULL,observation_deadline REAL NOT NULL,"
                    "created_at REAL NOT NULL,updated_at REAL NOT NULL)"
                )
                self._validate(connection)
            finally:
                connection.close()
        connection = self._open_existing(readonly=True)
        try:
            identity = self._validate(connection)
        finally:
            connection.close()
        if marker is not None and marker != identity:
            raise sqlite3.DatabaseError("recovery mailbox identity changed")
        if existing_marker is None:
            ledger = self.root / "management-child.sqlite3"
            connection = sqlite3.connect(ledger.as_uri() + "?mode=rw", uri=True, timeout=2, isolation_level=None)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT value FROM metadata WHERE key='recovery_mailbox_id'").fetchone()
                if row is None:
                    connection.execute("INSERT INTO metadata VALUES('recovery_mailbox_id',?)", (identity,))
                elif row[0] != identity:
                    raise sqlite3.DatabaseError("recovery mailbox identity changed")
                connection.commit()
            finally:
                connection.close()

    def _open_existing(self, *, readonly: bool) -> sqlite3.Connection:
        self._directory_authorized()
        info = self.path.stat()
        if self.path.is_symlink() or info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise PermissionError("recovery mailbox is not owned and protected")
        connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True, timeout=2, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        connection = self._open_existing(readonly=readonly)
        try:
            identity = self._validate(connection)
            if self._child_marker() != identity:
                raise sqlite3.DatabaseError("recovery mailbox does not match retained ledger")
        except Exception:
            connection.close()
            raise
        return connection

    def _validate(self, connection: sqlite3.Connection) -> str:
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        identity = metadata.get("mailbox_id", "")
        if not isinstance(identity, str) or metadata != {"schema_version": "1", "instance_id": self.instance_id, "mailbox_id": identity} or str(uuid.UUID(identity)) != identity:
            raise sqlite3.DatabaseError("recovery mailbox schema or instance mismatch")
        columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(recovery_requests)"))
        if columns != ("request_id", "intent_json", "status", "detail", "observation_id", "child_identity", "observation_started", "observation_deadline", "created_at", "updated_at"):
            raise sqlite3.DatabaseError("recovery mailbox schema is incomplete")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("recovery mailbox integrity check failed")
        return identity

    @staticmethod
    def _record(row: sqlite3.Row) -> RecoveryRecord:
        value = RecoveryIntent(**json.loads(row["intent_json"]))
        value.validate()
        canonical = json.dumps(asdict(value), sort_keys=True, separators=(",", ":"))
        if row["intent_json"] != canonical or row["request_id"] != value.request_id or row["status"] not in {"pending", "claimed", "launched", "succeeded", "refused", "unknown"}:
            raise sqlite3.DatabaseError("corrupt recovery request")
        if not isinstance(row["detail"], str) or len(row["detail"]) > 512 or "\x00" in row["detail"]:
            raise sqlite3.DatabaseError("invalid recovery detail")
        times = (row["observation_started"], row["observation_deadline"], row["created_at"], row["updated_at"])
        if any(not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item) or item < 0 for item in times):
            raise sqlite3.DatabaseError("invalid recovery timestamps")
        observation_id, identity = row["observation_id"], row["child_identity"]
        if not isinstance(observation_id, str) or not isinstance(identity, str) or len(identity) > 512 or "\x00" in identity:
            raise sqlite3.DatabaseError("invalid recovery observation identity")
        has_observation = bool(observation_id or identity or times[0] or times[1])
        if row["status"] in {"launched", "succeeded"} or (row["status"] == "unknown" and has_observation):
            if len(observation_id) != 32 or any(c not in "0123456789abcdef" for c in observation_id) or not identity or not 0 < times[0] < times[1] <= times[0] + 3600:
                raise sqlite3.DatabaseError("invalid launched recovery evidence")
        elif has_observation:
            raise sqlite3.DatabaseError("unexpected recovery observation evidence")
        return RecoveryRecord(value, row["status"], row["detail"], observation_id, identity, times[0], times[1])

    def submit(self, intent: RecoveryIntent) -> RecoveryRecord:
        intent.validate()
        if intent.instance_id != self.instance_id:
            raise ValueError("recovery instance mismatch")
        encoded = json.dumps(asdict(intent), sort_keys=True, separators=(",", ":"))
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT * FROM recovery_requests WHERE request_id=?", (intent.request_id,)).fetchone()
            if old is not None:
                if old["intent_json"] != encoded:
                    raise ValueError("request identity conflicts with original intent")
            else:
                now = time.time()
                connection.execute("INSERT INTO recovery_requests VALUES(?,?, 'pending','', '', '',0,0,?,?)", (intent.request_id, encoded, now, now))
            connection.commit()
            row = connection.execute("SELECT * FROM recovery_requests WHERE request_id=?", (intent.request_id,)).fetchone()
            assert row is not None
            return self._record(row)
        finally:
            connection.close()

    def get(self, request_id: str) -> RecoveryRecord | None:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError("invalid recovery request identity")
        connection = self.connect(readonly=True)
        try:
            row = connection.execute("SELECT * FROM recovery_requests WHERE request_id=?", (request_id,)).fetchone()
            return self._record(row) if row is not None else None
        finally:
            connection.close()
