"""Durable, coalescing invalidations for authoritative GitHub reevaluation."""

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class EntityIdentity:
    repository: str
    entity_type: str
    number: int

    def __post_init__(self) -> None:
        if self.entity_type not in {"issue", "pr"}:
            raise ValueError("entity_type must be 'issue' or 'pr'")
        if not self.repository or self.number <= 0:
            raise ValueError("repository and a positive entity number are required")


@dataclass(frozen=True)
class ClaimedInvalidation:
    identity: EntityIdentity
    generation: int


@dataclass(frozen=True)
class GitHubDeliveryMetadata:
    delivery_id: str
    identity: EntityIdentity
    event_type: Optional[str] = None
    action: Optional[str] = None


class DurableInvalidationQueue:
    """SQLite-backed dirty-entity set with generation-based in-flight coalescing."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity_invalidations (
                repository TEXT NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr')),
                entity_number INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                claimed_generation INTEGER,
                state TEXT NOT NULL CHECK(state IN ('dirty', 'queued', 'processing')),
                PRIMARY KEY(repository, entity_type, entity_number)
            );
            CREATE TABLE IF NOT EXISTS github_deliveries (
                repository TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr')),
                entity_number INTEGER NOT NULL,
                event_type TEXT,
                action TEXT,
                PRIMARY KEY(repository, delivery_id, entity_type, entity_number)
            );
            CREATE TABLE IF NOT EXISTS legacy_github_deliveries (
                repository TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                PRIMARY KEY(repository, delivery_id)
            );
            """
        )
        schema = self._connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entity_invalidations'").fetchone()[0]
        if "'queued'" not in schema:
            # Migrate databases created by the first durable-queue release.
            self._connection.executescript(
                """
                ALTER TABLE entity_invalidations RENAME TO entity_invalidations_v1;
                CREATE TABLE entity_invalidations (
                    repository TEXT NOT NULL,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr')),
                    entity_number INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    claimed_generation INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('dirty', 'queued', 'processing')),
                    PRIMARY KEY(repository, entity_type, entity_number)
                );
                INSERT INTO entity_invalidations
                    SELECT * FROM entity_invalidations_v1;
                DROP TABLE entity_invalidations_v1;
                """
            )

        delivery_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(github_deliveries)")}
        if "entity_type" not in delivery_columns:
            # Preserve old delivery IDs as repository-wide deduplication
            # tombstones because the previous schema did not record entities.
            self._connection.executescript(
                """
                INSERT OR IGNORE INTO legacy_github_deliveries(repository, delivery_id)
                    SELECT repository, delivery_id FROM github_deliveries;
                DROP TABLE github_deliveries;
                CREATE TABLE github_deliveries (
                    repository TEXT NOT NULL, delivery_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr')),
                    entity_number INTEGER NOT NULL, event_type TEXT, action TEXT,
                    PRIMARY KEY(repository, delivery_id, entity_type, entity_number)
                );
                """
            )
            legacy_rows = self._connection.execute("SELECT repository, delivery_id FROM legacy_github_deliveries").fetchall()
            with self._connection:
                self._connection.executemany(
                    "INSERT OR IGNORE INTO legacy_github_deliveries(repository, delivery_id) VALUES (?, ?)",
                    ((repository, self._raw_legacy_delivery_id(delivery_id)) for repository, delivery_id in legacy_rows),
                )

    @staticmethod
    def _raw_legacy_delivery_id(delivery_id: str) -> str:
        """Undo the former adapter's ``:<entity index>`` delivery suffix."""
        raw_delivery_id, separator, index = delivery_id.rpartition(":")
        if separator and raw_delivery_id and index.isdigit():
            return raw_delivery_id
        return delivery_id

    def recover(self, repository: str) -> None:
        """Make work interrupted by process termination claimable again."""
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE entity_invalidations SET state = 'dirty', claimed_generation = NULL
                   WHERE repository = ? AND state IN ('queued', 'processing')""",
                (repository,),
            )

    def invalidate(
        self,
        identity: EntityIdentity,
        delivery_id: Optional[str] = None,
        event_type: Optional[str] = None,
        action: Optional[str] = None,
    ) -> bool:
        """Persist an invalidation; return False only for a duplicate delivery."""
        with self._lock, self._connection:
            if delivery_id:
                legacy = self._connection.execute(
                    "SELECT 1 FROM legacy_github_deliveries WHERE repository = ? AND delivery_id = ?",
                    (identity.repository, delivery_id),
                ).fetchone()
                if legacy is not None:
                    return False
                cursor = self._connection.execute(
                    """INSERT OR IGNORE INTO github_deliveries
                       (repository, delivery_id, entity_type, entity_number, event_type, action)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (identity.repository, delivery_id, identity.entity_type, identity.number, event_type, action),
                )
                if cursor.rowcount == 0:
                    return False
            self._connection.execute(
                """
                INSERT INTO entity_invalidations(repository, entity_type, entity_number, generation, state)
                VALUES (?, ?, ?, 1, 'dirty')
                ON CONFLICT(repository, entity_type, entity_number) DO UPDATE SET
                    generation = CASE
                        WHEN state = 'processing' THEN generation + 1
                        ELSE generation
                    END
                """,
                (identity.repository, identity.entity_type, identity.number),
            )
            return True

    def claim(self, repository: str) -> Optional[ClaimedInvalidation]:
        """Atomically reserve one dirty identity for the in-memory queue."""
        with self._lock, self._connection:
            row = self._connection.execute(
                """UPDATE entity_invalidations
                   SET state = 'queued', claimed_generation = generation
                   WHERE rowid = (
                       SELECT rowid FROM entity_invalidations
                       WHERE repository = ? AND state = 'dirty' ORDER BY rowid LIMIT 1
                   ) AND state = 'dirty'
                   RETURNING entity_type, entity_number, generation""",
                (repository,),
            ).fetchone()
            if row is None:
                return None
            entity_type, number, generation = row
            return ClaimedInvalidation(EntityIdentity(repository, entity_type, number), generation)

    def begin_processing(self, claim: ClaimedInvalidation) -> bool:
        """Mark queued work active at the point a worker actually receives it."""
        identity = claim.identity
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """UPDATE entity_invalidations SET state = 'processing'
                   WHERE repository = ? AND entity_type = ? AND entity_number = ?
                     AND state = 'queued' AND claimed_generation = ?""",
                (identity.repository, identity.entity_type, identity.number, claim.generation),
            )
            return cursor.rowcount == 1

    def complete(self, claim: ClaimedInvalidation) -> bool:
        """Complete evaluated generation; return True when a later generation remains."""
        identity = claim.identity
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT generation, claimed_generation, state FROM entity_invalidations
                   WHERE repository = ? AND entity_type = ? AND entity_number = ?""",
                (identity.repository, identity.entity_type, identity.number),
            ).fetchone()
            if row is None or row[1] != claim.generation or row[2] != "processing":
                return False
            if row[0] > claim.generation:
                self._connection.execute(
                    """UPDATE entity_invalidations SET state = 'dirty', claimed_generation = NULL
                       WHERE repository = ? AND entity_type = ? AND entity_number = ?""",
                    (identity.repository, identity.entity_type, identity.number),
                )
                return True
            self._connection.execute(
                "DELETE FROM entity_invalidations WHERE repository = ? AND entity_type = ? AND entity_number = ?",
                (identity.repository, identity.entity_type, identity.number),
            )
            return False

    def release(self, claim: ClaimedInvalidation) -> None:
        """Return an interrupted or failed reevaluation to the dirty set."""
        identity = claim.identity
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE entity_invalidations SET state = 'dirty', claimed_generation = NULL
                   WHERE repository = ? AND entity_type = ? AND entity_number = ?
                     AND state = 'processing' AND claimed_generation = ?""",
                (identity.repository, identity.entity_type, identity.number, claim.generation),
            )

    def pending_count(self, repository: str) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) FROM entity_invalidations WHERE repository = ?", (repository,)).fetchone()
            return int(row[0])

    def get_delivery_metadata(self, repository: str, delivery_id: str) -> list[GitHubDeliveryMetadata]:
        """Expose preserved provider metadata for diagnostics and deduplication audits."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT entity_type, entity_number, event_type, action
                   FROM github_deliveries WHERE repository = ? AND delivery_id = ?
                   ORDER BY entity_type, entity_number""",
                (repository, delivery_id),
            ).fetchall()
        return [GitHubDeliveryMetadata(delivery_id, EntityIdentity(repository, entity_type, number), event_type, action) for entity_type, number, event_type, action in rows]
