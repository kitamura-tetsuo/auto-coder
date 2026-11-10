"""Inspection-only quarantined management history, without replay or grants.

An operator-supplied manifest defines only an inspection scope, never complete
lineage. All original journal bytes are read through protected file descriptors
and validated in memory; SQLite never opens or mutates original store files.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from auto_coder.logger_config import get_logger
from auto_coder.operating_policy import MANAGEMENT_REPOSITORY

from .operation_store import ACTIVE_PHASES, ManagementOperationStore
from .operations import AcceptedAuthority, ManagedTarget, OperationIntent, OperationRecord, validate_authority
from .retained_journal import RetainedJournal

logger = get_logger(__name__)
MAX_JOURNALS = 32
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024


class QuarantineUnavailable(ValueError):
    """Historical evidence is unavailable, not empty or safe to replay."""


@dataclass(frozen=True)
class QuarantineJournal:
    instance_id: str
    database: Path
    fence: Path
    database_device: int
    database_inode: int
    database_sha256: str
    database_ctime_ns: int
    fence_device: int
    fence_inode: int
    fence_ctime_ns: int


@dataclass(frozen=True)
class InspectionReport:
    status: str
    launch_authority: bool
    lineage_completeness: str
    inspected_journals: int
    historical_keys: int
    unresolved_targets: int


@dataclass(frozen=True)
class InspectionDecision:
    status: str
    launch_authority: bool = False
    lineage_completeness: str = "unverified"


def _open_protected(path: Path, *, identity: tuple[int, int] | None = None, ctime_ns: int | None = None) -> int:
    if not path.is_absolute():
        raise QuarantineUnavailable("absolute protected path required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_mode & 0o077) != 0
            or info.st_uid != os.geteuid()
            or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
            or not stat.S_ISREG(named.st_mode)
            or (identity is not None and (info.st_dev, info.st_ino) != identity)
            or (ctime_ns is not None and info.st_ctime_ns != ctime_ns)
        ):
            raise QuarantineUnavailable("protected file identity or permissions differ")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_protected(path: Path, *, identity: tuple[int, int] | None = None, ctime_ns: int | None = None, limit: int) -> tuple[bytes, tuple[int, ...]]:
    descriptor = _open_protected(path, identity=identity, ctime_ns=ctime_ns)
    try:
        before = os.fstat(descriptor)
        if before.st_size > limit:
            raise QuarantineUnavailable("protected evidence exceeds inspection limit")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if remaining == 0:
            raise QuarantineUnavailable("protected evidence exceeds inspection limit")
        after = os.fstat(descriptor)
        fingerprint = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if fingerprint != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise QuarantineUnavailable("protected evidence changed while reading")
        return b"".join(chunks), fingerprint
    finally:
        os.close(descriptor)


def load_manifest(path: Path) -> tuple[QuarantineJournal, ...]:
    """Require a private, exact-schema operator manifest; not an authority."""
    raw, _ = _read_protected(path, limit=MAX_MANIFEST_BYTES)
    try:
        document = json.loads(raw)
        if type(document) is not dict or set(document) != {"version", "journals"} or (type(document["version"]) is not int or document["version"] != 1):
            raise QuarantineUnavailable("invalid quarantine manifest")
        entries = document["journals"]
        if type(entries) is not list or not 1 <= len(entries) <= MAX_JOURNALS:
            raise QuarantineUnavailable("invalid quarantine journal count")
        expected = {
            "instance_id",
            "database",
            "fence",
            "database_device",
            "database_inode",
            "database_sha256",
            "database_ctime_ns",
            "fence_device",
            "fence_inode",
            "fence_ctime_ns",
        }
        journals: list[QuarantineJournal] = []
        identities: set[tuple[int, int]] = set()
        paths: set[Path] = set()
        instances: set[str] = set()
        for entry in entries:
            if type(entry) is not dict or set(entry) != expected:
                raise QuarantineUnavailable("invalid quarantine journal binding")
            instance = entry["instance_id"]
            digest = entry["database_sha256"]
            if type(instance) is not str or not 1 <= len(instance) <= 128 or "\x00" in instance or instance in instances:
                raise QuarantineUnavailable("invalid original management instance")
            if type(digest) is not str or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise QuarantineUnavailable("invalid database digest")
            for name in ("database_device", "database_inode", "database_ctime_ns", "fence_device", "fence_inode", "fence_ctime_ns"):
                if type(entry[name]) is not int or entry[name] <= 0:
                    raise QuarantineUnavailable("invalid file identity")
            for name in ("database", "fence"):
                if type(entry[name]) is not str or not entry[name] or "\x00" in entry[name]:
                    raise QuarantineUnavailable("invalid evidence path")
            database, fence = Path(entry["database"]), Path(entry["fence"])
            if not database.is_absolute() or not fence.is_absolute():
                raise QuarantineUnavailable("absolute evidence paths required")
            for evidence_path, identity in (
                (database, (entry["database_device"], entry["database_inode"])),
                (fence, (entry["fence_device"], entry["fence_inode"])),
            ):
                if evidence_path in paths or identity in identities:
                    raise QuarantineUnavailable("duplicate or conflicting evidence binding")
                paths.add(evidence_path)
                identities.add(identity)
            instances.add(instance)
            journals.append(
                QuarantineJournal(
                    instance,
                    database,
                    fence,
                    entry["database_device"],
                    entry["database_inode"],
                    digest,
                    entry["database_ctime_ns"],
                    entry["fence_device"],
                    entry["fence_inode"],
                    entry["fence_ctime_ns"],
                )
            )
        return tuple(journals)
    except (TypeError, KeyError, json.JSONDecodeError) as error:
        raise QuarantineUnavailable("invalid quarantine manifest") from error


def _sidecars(database: Path) -> tuple[tuple[str, tuple[int, ...] | None], ...]:
    """Refuse every nonempty sidecar; immutable snapshots cannot include WAL."""
    results: list[tuple[str, tuple[int, ...] | None]] = []
    for suffix in ("-wal", "-journal", "-shm"):
        path = Path(str(database) + suffix)
        try:
            descriptor = _open_protected(path)
        except FileNotFoundError:
            results.append((suffix, None))
            continue
        try:
            info = os.fstat(descriptor)
            if info.st_size:
                raise QuarantineUnavailable("nonempty transaction sidecar")
            results.append((suffix, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)))
        finally:
            os.close(descriptor)
    return tuple(results)


class QuarantineHistory:
    """Hold shared kernel fences; every decision revalidates every journal."""

    def __init__(self, journals: tuple[QuarantineJournal, ...]) -> None:
        if not 1 <= len(journals) <= MAX_JOURNALS:
            raise QuarantineUnavailable("nonempty bounded journal set required")
        paths: set[Path] = set()
        identities: set[tuple[int, int]] = set()
        instances: set[str] = set()
        for entry in journals:
            if entry.instance_id in instances:
                raise QuarantineUnavailable("duplicate original management instance")
            instances.add(entry.instance_id)
            for path, identity in (
                (entry.database, (entry.database_device, entry.database_inode)),
                (entry.fence, (entry.fence_device, entry.fence_inode)),
            ):
                if path in paths or identity in identities:
                    raise QuarantineUnavailable("duplicate or conflicting evidence binding")
                paths.add(path)
                identities.add(identity)
        self._journals = journals
        self._locks: list[int] = []
        self._closed = False
        try:
            # Acquire ALL fences before reading any journal; later failure closes earlier holds.
            for journal in journals:
                descriptor = _open_protected(journal.fence, identity=(journal.fence_device, journal.fence_inode), ctime_ns=journal.fence_ctime_ns)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BaseException:
                    os.close(descriptor)
                    raise
                self._locks.append(descriptor)
            self._snapshot()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> QuarantineHistory:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        for descriptor in reversed(self._locks):
            os.close(descriptor)
        self._locks.clear()
        self._closed = True

    def _snapshot(self) -> tuple[tuple[QuarantineJournal, tuple[OperationRecord, ...]], ...]:
        if self._closed or len(self._locks) != len(self._journals):
            raise QuarantineUnavailable("quarantine fence no longer held")
        collected: list[tuple[QuarantineJournal, tuple[OperationRecord, ...]]] = []
        snapshots: list[tuple[QuarantineJournal, tuple[int, ...], tuple[tuple[str, tuple[int, ...] | None], ...]]] = []
        for journal, fence_fd in zip(self._journals, self._locks):
            original = os.fstat(fence_fd)
            if (original.st_dev, original.st_ino, original.st_ctime_ns) != (journal.fence_device, journal.fence_inode, journal.fence_ctime_ns):
                raise QuarantineUnavailable("held fence identity changed")
            check = _open_protected(journal.fence, identity=(journal.fence_device, journal.fence_inode), ctime_ns=journal.fence_ctime_ns)
            os.close(check)
            sidecars = _sidecars(journal.database)
            content, fingerprint = _read_protected(
                journal.database,
                identity=(journal.database_device, journal.database_inode),
                ctime_ns=journal.database_ctime_ns,
                limit=MAX_DATABASE_BYTES,
            )
            if hashlib.sha256(content).hexdigest() != journal.database_sha256:
                raise QuarantineUnavailable("database digest differs")
            # The original SQLite header can record WAL mode even after its
            # WAL has been checkpointed and removed. Only in the private
            # in-memory copy, translate its journal-mode header to rollback.
            # Without this, deserialize may try to open a WAL sidecar beside
            # the ephemeral :memory: database and fail before validation.
            if len(content) < 100 or content[:16] != b"SQLite format 3\x00":
                raise QuarantineUnavailable("invalid SQLite database header")
            image = bytearray(content)
            if image[18:20] == b"\x02\x02":
                image[18:20] = b"\x01\x01"
            elif image[18:20] != b"\x01\x01":
                raise QuarantineUnavailable("unsupported SQLite journal mode")
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
                connection.deserialize(bytes(image))
                validated = RetainedJournal._validated_records(connection)
                connection.rollback()
            # Validate ALL sources twice, including an earlier journal after later reads.
            collected.append((journal, tuple(record for record, _ in validated)))
            snapshots.append((journal, fingerprint, sidecars))
            if _sidecars(journal.database) != sidecars:
                raise QuarantineUnavailable("sidecar changed during inspection")
            fresh, later = _read_protected(
                journal.database,
                identity=(journal.database_device, journal.database_inode),
                ctime_ns=journal.database_ctime_ns,
                limit=MAX_DATABASE_BYTES,
            )
            if later != fingerprint or hashlib.sha256(fresh).hexdigest() != journal.database_sha256:
                raise QuarantineUnavailable("database changed during inspection")
        for journal, fingerprint, sidecars in snapshots:
            # Even if a later journal was read successfully, earlier evidence must remain fixed.
            fresh, observed = _read_protected(
                journal.database,
                identity=(journal.database_device, journal.database_inode),
                ctime_ns=journal.database_ctime_ns,
                limit=MAX_DATABASE_BYTES,
            )
            if observed != fingerprint or hashlib.sha256(fresh).hexdigest() != journal.database_sha256:
                raise QuarantineUnavailable("earlier journal changed during inspection")
            if _sidecars(journal.database) != sidecars:
                raise QuarantineUnavailable("earlier sidecar changed during inspection")
        return tuple(collected)

    def inspect(self) -> InspectionReport:
        records = self._snapshot()
        return InspectionReport(
            status="inspection_only",
            launch_authority=False,
            lineage_completeness="unverified",
            inspected_journals=len(records),
            historical_keys=sum(len(rows) for _, rows in records),
            unresolved_targets=sum(len({row.intent.target.key() for row in rows if row.phase in ACTIVE_PHASES}) for _, rows in records),
        )

    def decide(self, intent: OperationIntent, authority: AcceptedAuthority) -> InspectionDecision:
        """Return only historical exclusion evidence, never old rows or admission."""
        validate_authority(authority)
        original = self._snapshot()
        for journal, records in original:
            candidate = intent
            if intent.target.channel == "management":
                candidate = OperationIntent(
                    ManagedTarget(journal.instance_id, "management", MANAGEMENT_REPOSITORY),
                    intent.action,
                    intent.parameters,
                    intent.idempotency_key,
                    intent.expected_incarnation,
                    intent.expected_artifact,
                )
            namespace = ManagementOperationStore._namespace(candidate, authority)
            if any(ManagementOperationStore._namespace(row.intent, row.authority) == namespace for row in records):
                return InspectionDecision("key_excluded")
        for journal, records in original:
            if any(row.phase in ACTIVE_PHASES and (row.intent.target == intent.target or (intent.target.channel == "management" and row.intent.target == ManagedTarget(journal.instance_id, "management", MANAGEMENT_REPOSITORY))) for row in records):
                return InspectionDecision("target_blocked")
        return InspectionDecision("no_conflict_in_supplied_journals")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect supplied management histories without replay or launch authority")
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        with QuarantineHistory(load_manifest(arguments.manifest)) as reader:
            result = reader.inspect()
            print(json.dumps(asdict(result), sort_keys=True))
            return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        logger.error("quarantine inspection unavailable: {}", type(error).__name__)
        print(json.dumps({"status": "unavailable", "launch_authority": False, "lineage_completeness": "unverified"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
