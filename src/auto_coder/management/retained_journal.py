"""Read-only validation of a prior journal while its owner is fenced.

This is an evidence reader, not an admission or migration mechanism. Callers
must keep this object open for the entire period in which they rely on it.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from auto_coder.operating_policy import MANAGEMENT_REPOSITORY

from .operation_store import ACTIVE_PHASES, SCHEMA_VERSION, ManagementOperationStore
from .operations import AcceptedAuthority, AdmissionResult, AdmissionStatus, ManagedTarget, OperationIntent, OperationRecord, validate_authority


class RetainedJournal:
    """Pin a previous owner lock and database without modifying either."""

    def __init__(self, database: Path, fence: Path, *, database_identity: tuple[int, int], fence_identity: tuple[int, int], prior_management_instance: str | None = None) -> None:
        self.database = database
        self.fence = fence
        self.database_identity = database_identity
        self.fence_identity = fence_identity
        if prior_management_instance is not None and (not prior_management_instance or len(prior_management_instance) > 128 or "\x00" in prior_management_instance):
            raise ValueError("invalid retained management instance")
        self.prior_management_instance = prior_management_instance
        if not database.is_absolute() or not fence.is_absolute():
            raise ValueError("absolute prior evidence paths required")
        self._fd = os.open(fence, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self._check_file(fence, fence_identity, self._fd)
            fcntl.flock(self._fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self._check_file(database, database_identity)
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise

    @staticmethod
    def _check_file(path: Path, identity: tuple[int, int], descriptor: int | None = None) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
            raise ValueError("retained evidence identity differs")
        if descriptor is not None:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
                raise ValueError("retained fence descriptor differs")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> RetainedJournal:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def read(self) -> tuple[OperationRecord, ...]:
        return tuple(record for record, _ in self.read_evidence())

    def _empty_wal_identity(self) -> tuple[int, int, int, int] | None:
        wal = self.database.with_name(self.database.name + "-wal")
        try:
            descriptor = os.open(wal, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != 0:
                raise ValueError("retained write-ahead log is not proven empty")
            return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns
        finally:
            os.close(descriptor)

    def read_evidence(self) -> tuple[tuple[OperationRecord, dict[str, object] | None], ...]:
        """Validate all records and their last history; errors never mean empty."""
        if self._fd < 0:
            raise ValueError("prior owner fence is closed")
        self._check_file(self.fence, self.fence_identity, self._fd)
        self._check_file(self.database, self.database_identity)
        readonly_mount = bool(os.statvfs(self.database).f_flag & os.ST_RDONLY)
        wal_before = self._empty_wal_identity() if readonly_mount else None
        # SQLite cannot create its shared-memory sidecar on an RO bind. Only
        # use immutable mode when a real RO mount and an absent/empty WAL are
        # verified; immutable mode must never hide unapplied WAL transactions.
        query = "?mode=ro&immutable=1" if readonly_mount else "?mode=ro"
        with closing(sqlite3.connect(self.database.as_uri() + query, uri=True, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            check = connection.execute("PRAGMA quick_check").fetchall()
            if len(check) != 1 or check[0][0] != "ok":
                raise ValueError("retained journal integrity check failed")
            version = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version is None or version[0] != SCHEMA_VERSION or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("retained journal schema or references differ")
            records: list[tuple[OperationRecord, dict[str, object] | None]] = []
            active: set[str] = set()
            for row in connection.execute("SELECT * FROM operations ORDER BY sequence"):
                record = ManagementOperationStore._record(row)
                validate_authority(record.authority)
                if record.intent.fingerprint() != row["fingerprint"] or record.intent.target.key() != row["target_key"] or ManagementOperationStore._namespace(record.intent, record.authority) != row["namespace_key"] or type(record.revision) is not int or record.revision < 0:
                    raise ValueError("retained operation binding differs")
                if record.phase in ACTIVE_PHASES:
                    if row["target_key"] in active:
                        raise ValueError("duplicate retained active target")
                    active.add(row["target_key"])
                history = connection.execute("SELECT * FROM operation_history WHERE operation_id=? ORDER BY sequence", (record.operation_id,)).fetchall()
                parsed = [ManagementOperationStore._history(entry) for entry in history]
                if (
                    not parsed
                    or len(parsed) != record.revision + 1
                    or any(item.revision != index for index, item in enumerate(parsed))
                    or parsed[-1].phase != record.phase
                    or parsed[-1].owner_attempt_id != record.owner_attempt_id
                    or parsed[-1].effect_state != record.effect.state
                    or parsed[-1].effect_owner_generation != record.effect.owner_generation
                    or parsed[-1].effect_attempt_id != record.effect.attempt_id
                    or parsed[-1].correlation_identity != record.effect.correlation_identity
                    or parsed[-1].safe_category != record.safe_error_category
                    or parsed[-1].receipt != record.effect.receipt
                ):
                    raise ValueError("retained operation history incomplete")
                snapshot = connection.execute("SELECT revision,snapshot_json FROM worker_lifecycle WHERE operation_id=?", (record.operation_id,)).fetchone()
                if record.intent.action in {"start_worker", "stop_worker", "restart_worker", "deploy_beta_source"} and snapshot is None:
                    raise ValueError("retained lifecycle evidence missing")
                value: dict[str, object] | None = None
                if snapshot is not None:
                    value = json.loads(snapshot["snapshot_json"])
                    if type(snapshot["revision"]) is not int or snapshot["revision"] < 0 or not isinstance(value, dict):
                        raise ValueError("retained lifecycle evidence invalid")
                    ManagementOperationStore._encode_lifecycle(value)
                records.append((record, value))
            self._check_file(self.fence, self.fence_identity, self._fd)
            self._check_file(self.database, self.database_identity)
            if readonly_mount and self._empty_wal_identity() != wal_before:
                raise ValueError("retained write-ahead log changed while reading")
            connection.rollback()
            return tuple(records)

    def admission(self, intent: OperationIntent, authority: AcceptedAuthority) -> AdmissionResult | None:
        """Return a prior result or block an active target; never send an effect.

        A None result only says that this retained journal has no conflicting
        row. It does not authorize an operation in any current journal.
        """
        validate_authority(authority)
        namespace = ManagementOperationStore._namespace(intent, authority)
        fingerprint = intent.fingerprint()
        records = self.read()
        for record in records:
            if ManagementOperationStore._namespace(record.intent, record.authority) == namespace:
                status = AdmissionStatus.REPLAY if record.intent_fingerprint == fingerprint else AdmissionStatus.CONFLICT
                return AdmissionResult(status, record)
        for record in records:
            if record.intent.target == intent.target and record.phase in ACTIVE_PHASES:
                return AdmissionResult(AdmissionStatus.BUSY, record)
        return None

    def owned(self, operation_id: str, target: ManagedTarget, target_id: str, authority: AcceptedAuthority) -> OperationRecord | None:
        """Select a record only for the original actor, client and target.

        The public endpoint must separately authenticate the current grant,
        check its read scope and resolve the target under current policy.
        """
        evidence = self.owned_evidence(operation_id, target, target_id, authority)
        return None if evidence is None else evidence[0]

    def owned_evidence(self, operation_id: str, target: ManagedTarget, target_id: str, authority: AcceptedAuthority) -> tuple[OperationRecord, dict[str, object] | None] | None:
        validate_authority(authority)
        for record, snapshot in self.read_evidence():
            original = record.authority
            parameters = {item.name: item.value for item in record.intent.parameters}
            if (
                record.operation_id == operation_id
                and record.intent.target == target
                and parameters.get("target_id") == target_id
                and (original.issuer, original.resource, original.project_id, original.uid, original.oauth_client_id) == (authority.issuer, authority.resource, authority.project_id, authority.uid, authority.oauth_client_id)
            ):
                return record, snapshot
        return None

    def management_target(self) -> ManagedTarget:
        if self.prior_management_instance is None:
            raise ValueError("prior management identity unavailable")
        return ManagedTarget(self.prior_management_instance, "management", MANAGEMENT_REPOSITORY)

    def owned_management(self, operation_id: str, authority: AcceptedAuthority) -> OperationRecord | None:
        validate_authority(authority)
        target = self.management_target()
        for record in self.read():
            original = record.authority
            if (
                record.operation_id == operation_id
                and record.intent.target == target
                and record.intent.action == "management-service-self-update"
                and (original.issuer, original.resource, original.project_id, original.uid, original.oauth_client_id) == (authority.issuer, authority.resource, authority.project_id, authority.uid, authority.oauth_client_id)
            ):
                return record
        return None

    def management_lane(self) -> OperationRecord | None:
        target = self.management_target()
        return next((record for record in self.read() if record.intent.target == target and record.phase in ACTIVE_PHASES), None)

    def management_request(self, key: str, source_sha: str, authority: AcceptedAuthority) -> AdmissionResult | None:
        validate_authority(authority)
        target = self.management_target()
        probe = OperationIntent(target, "management-service-self-update", (), key)
        namespace = ManagementOperationStore._namespace(probe, authority)
        records = self.read()
        for record in records:
            if ManagementOperationStore._namespace(record.intent, record.authority) == namespace:
                params = {part.name: part.value for part in record.intent.parameters}
                status = AdmissionStatus.REPLAY if record.intent.action == probe.action and params.get("source_sha") == source_sha else AdmissionStatus.CONFLICT
                return AdmissionResult(status, record)
        busy = next((record for record in records if record.intent.target == target and record.phase in ACTIVE_PHASES), None)
        return AdmissionResult(AdmissionStatus.BUSY, busy) if busy is not None else None


class RetainedGuardedOperationStore(ManagementOperationStore):
    """Gate current-journal admissions and effect claims on frozen old lanes.

    The caller owns the retained reader for at least this store's entire
    lifetime. Startup must establish its provenance and continuously held
    owner fence before constructing this store.
    """

    def __init__(self, state_root: Path, retained: RetainedJournal) -> None:
        super().__init__(state_root)
        self.retained = retained

    def admit(self, intent: OperationIntent, authority: AcceptedAuthority, *, lifecycle_snapshot: dict[str, object] | None = None) -> AdmissionResult:
        try:
            if intent.target.channel == "management":
                previous_management = self.retained.management_lane()
                if previous_management is not None:
                    return AdmissionResult(AdmissionStatus.BUSY, previous_management, "retained management operation")
            previous = self.retained.admission(intent, authority)
            if previous is not None:
                # Never return a retained replay as an admitted current row:
                # the normal dispatcher would try to execute that old ID.
                status = AdmissionStatus.CONFLICT if previous.status is AdmissionStatus.CONFLICT else AdmissionStatus.BUSY
                return AdmissionResult(status, previous.record, "retained operation")
        except Exception:
            return AdmissionResult(AdmissionStatus.UNAVAILABLE, detail="retained journal unavailable")
        return super().admit(intent, authority, lifecycle_snapshot=lifecycle_snapshot)

    def effect_permitted(self, record: OperationRecord) -> bool:
        """Revalidate at dispatch; no prior row may be executed by this store."""
        try:
            if record.intent.target.channel == "management" and self.retained.management_lane() is not None:
                return False
            return self.retained.admission(record.intent, record.authority) is None
        except Exception:
            return False
