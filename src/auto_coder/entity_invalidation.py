"""Durable, coalescing invalidations for authoritative GitHub reevaluation."""

import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)

ISSUE_STABILIZATION_SECONDS = 60
CI_RECONCILIATION_SECONDS = 300


def issue_stabilization_deadline(created_at: str) -> Optional[float]:
    """Return the shared creation-anchored eligibility deadline for an Issue."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (created + timedelta(seconds=ISSUE_STABILIZATION_SECONDS)).timestamp()


@dataclass(frozen=True)
class EntityIdentity:
    repository: str
    entity_type: str
    number: int

    def __post_init__(self) -> None:
        if self.entity_type not in {"issue", "pr", "dependency"}:
            raise ValueError("entity_type must be 'issue', 'pr', or 'dependency'")
        if not self.repository or self.number <= 0:
            raise ValueError("repository and a positive entity number are required")


@dataclass(frozen=True)
class ClaimedInvalidation:
    identity: EntityIdentity
    generation: int
    urgent_admission: bool = False


class InvalidationDisposition(str, Enum):
    """The actual committed-transition classification of one `invalidate()` call.

    Observed at the same state-owning boundary that performs the durable
    transition (never guessed from the legacy Boolean, timestamps, or queue
    length): `NEW_PENDING` means no row existed for this identity before this
    transition; `COALESCED` means an existing dirty/queued row absorbed this
    invalidation without requiring a processing follow-up; `FOLLOWUP_REQUIRED`
    means the identity was already `processing` and this call advanced its
    generation so the in-flight worker's `complete()` will re-queue it.
    """

    NEW_PENDING = "new_pending"
    COALESCED = "coalesced"
    FOLLOWUP_REQUIRED = "followup_required"


@dataclass(frozen=True)
class InvalidationTransition:
    """The richer result of one `invalidate()` attempt.

    `accepted` is exactly the legacy Boolean (`False` only for a duplicate
    delivery). `disposition` is `None` when `accepted` is `False`; otherwise
    it is the actual observed transition, never inferred after the fact.
    """

    accepted: bool
    disposition: Optional[InvalidationDisposition] = None


class CompletionOutcome(str, Enum):
    """The actual committed-transition classification of one `complete()` call.

    `CLEARED` means the claimed generation was the latest and the identity's
    row was removed: no further work is pending. `FOLLOWUP_PENDING` means a
    newer generation had already been recorded while this claim was
    processing, so the identity was returned to `dirty` for a follow-up
    attempt (the legacy `True` case). `STALE_NO_OP` means the claim no longer
    matched the current row (already completed, released, or claimed by a
    different generation) -- this call committed nothing and must never be
    reported as a successful completion.
    """

    CLEARED = "cleared"
    FOLLOWUP_PENDING = "followup_pending"
    STALE_NO_OP = "stale_no_op"


@dataclass(frozen=True)
class CompletionTransition:
    """The richer result of one `complete()` attempt.

    `has_followup` is exactly the legacy Boolean. `latest_generation` is the
    generation observed to remain pending when `outcome` is
    `FOLLOWUP_PENDING`; it is `None` otherwise.
    """

    outcome: CompletionOutcome
    has_followup: bool
    latest_generation: Optional[int] = None


@dataclass(frozen=True)
class DeferredInvalidation:
    """Persisted admission wait for one authoritative reevaluation."""

    identity: EntityIdentity
    generation: int
    retry_not_before: float
    reason: str
    api_origin: Optional[str] = None


@dataclass(frozen=True)
class GitHubDeliveryMetadata:
    delivery_id: str
    identity: EntityIdentity
    event_type: Optional[str] = None
    action: Optional[str] = None


@dataclass(frozen=True)
class CIWebhookDelivery:
    repository: str
    delivery_id: str
    event_type: str
    action: Optional[str]
    pull_request_numbers: tuple[int, ...]
    head_sha: Optional[str]
    workflow_id: Optional[str] = None
    run_id: Optional[str] = None
    run_attempt: Optional[int] = None


class DurableInvalidationQueue:
    """SQLite-backed dirty-entity set with generation-based in-flight coalescing."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS entity_invalidations (
                repository TEXT NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr', 'dependency')),
                entity_number INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                claimed_generation INTEGER,
                state TEXT NOT NULL CHECK(state IN ('dirty', 'queued', 'processing')),
                not_before REAL,
                retry_not_before REAL,
                deferral_reason TEXT,
                deferral_api_origin TEXT,
                urgent_admission INTEGER NOT NULL DEFAULT 0 CHECK(urgent_admission IN (0, 1)),
                PRIMARY KEY(repository, entity_type, entity_number)
            );
            CREATE TABLE IF NOT EXISTS github_deliveries (
                repository TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr', 'dependency')),
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
            CREATE TABLE IF NOT EXISTS ci_webhook_deliveries (
                repository TEXT NOT NULL, delivery_id TEXT NOT NULL,
                event_type TEXT NOT NULL, action TEXT, head_sha TEXT,
                workflow_id TEXT, run_id TEXT, run_attempt INTEGER,
                received_at REAL NOT NULL,
                PRIMARY KEY(repository, delivery_id)
            );
            CREATE TABLE IF NOT EXISTS ci_delivery_targets (
                repository TEXT NOT NULL, delivery_id TEXT NOT NULL,
                pr_number INTEGER NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(repository, delivery_id, pr_number),
                FOREIGN KEY(repository, delivery_id) REFERENCES ci_webhook_deliveries(repository, delivery_id)
            );
            CREATE TABLE IF NOT EXISTS ci_pending_prs (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                observation_epoch INTEGER NOT NULL, first_seen REAL NOT NULL,
                latest_seen REAL NOT NULL, eligible_at REAL NOT NULL,
                PRIMARY KEY(repository, pr_number)
            );
            CREATE TABLE IF NOT EXISTS ci_correlations (
                repository TEXT NOT NULL, head_sha TEXT NOT NULL,
                observation_epoch INTEGER NOT NULL, first_seen REAL NOT NULL,
                latest_seen REAL NOT NULL, eligible_at REAL NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'processing')),
                PRIMARY KEY(repository, head_sha)
            );
            CREATE TABLE IF NOT EXISTS ci_watches (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL, workflow_id TEXT NOT NULL DEFAULT '',
                run_id TEXT, run_number INTEGER, run_attempt INTEGER,
                observation_availability TEXT NOT NULL DEFAULT 'unavailable',
                observation_epoch INTEGER NOT NULL DEFAULT 0,
                last_attempt_at REAL, next_reconcile_at REAL NOT NULL,
                publication_state TEXT NOT NULL DEFAULT 'none',
                throttle_attempts INTEGER NOT NULL DEFAULT 0,
                operational_block TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(repository, pr_number, head_sha, workflow_id)
            );
            """)
        schema = self._connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entity_invalidations'").fetchone()[0]
        if "'queued'" not in schema:
            # Migrate databases created by the first durable-queue release.
            self._connection.executescript("""
                ALTER TABLE entity_invalidations RENAME TO entity_invalidations_v1;
                CREATE TABLE entity_invalidations (
                    repository TEXT NOT NULL,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr')),
                    entity_number INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    claimed_generation INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('dirty', 'queued', 'processing')),
                    not_before REAL,
                    PRIMARY KEY(repository, entity_type, entity_number)
                );
                INSERT INTO entity_invalidations(repository, entity_type, entity_number,
                    generation, claimed_generation, state)
                    SELECT * FROM entity_invalidations_v1;
                DROP TABLE entity_invalidations_v1;
                """)

        # Dependency reevaluation is represented by one coalescing repository
        # obligation.  Rebuild older CHECK-constrained databases before that
        # identity can be persisted.
        schema = self._connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'entity_invalidations'").fetchone()[0]
        if "'dependency'" not in schema:
            self._connection.executescript("""
                ALTER TABLE entity_invalidations RENAME TO entity_invalidations_v2;
                CREATE TABLE entity_invalidations (
                    repository TEXT NOT NULL,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr', 'dependency')),
                    entity_number INTEGER NOT NULL, generation INTEGER NOT NULL,
                    claimed_generation INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('dirty', 'queued', 'processing')),
                    not_before REAL, urgent_admission INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(repository, entity_type, entity_number)
                );
                INSERT INTO entity_invalidations(
                    repository, entity_type, entity_number, generation,
                    claimed_generation, state, not_before, urgent_admission
                )
                SELECT repository, entity_type, entity_number, generation,
                    claimed_generation, state, not_before, 0
                FROM entity_invalidations_v2;
                DROP TABLE entity_invalidations_v2;
                """)

        invalidation_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(entity_invalidations)")}
        if "not_before" not in invalidation_columns:
            self._connection.execute("ALTER TABLE entity_invalidations ADD COLUMN not_before REAL")
        if "urgent_admission" not in invalidation_columns:
            self._connection.execute("ALTER TABLE entity_invalidations ADD COLUMN urgent_admission INTEGER NOT NULL DEFAULT 0")
        if "retry_not_before" not in invalidation_columns:
            self._connection.execute("ALTER TABLE entity_invalidations ADD COLUMN retry_not_before REAL")
        if "deferral_reason" not in invalidation_columns:
            self._connection.execute("ALTER TABLE entity_invalidations ADD COLUMN deferral_reason TEXT")
        if "deferral_api_origin" not in invalidation_columns:
            self._connection.execute("ALTER TABLE entity_invalidations ADD COLUMN deferral_api_origin TEXT")

        delivery_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(github_deliveries)")}
        if "entity_type" not in delivery_columns:
            # Preserve old delivery IDs as repository-wide deduplication
            # tombstones because the previous schema did not record entities.
            self._connection.executescript("""
                INSERT OR IGNORE INTO legacy_github_deliveries(repository, delivery_id)
                    SELECT repository, delivery_id FROM github_deliveries;
                DROP TABLE github_deliveries;
                CREATE TABLE github_deliveries (
                    repository TEXT NOT NULL, delivery_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr')),
                    entity_number INTEGER NOT NULL, event_type TEXT, action TEXT,
                    PRIMARY KEY(repository, delivery_id, entity_type, entity_number)
                );
                """)
            legacy_rows = self._connection.execute("SELECT repository, delivery_id FROM legacy_github_deliveries").fetchall()
            with self._connection:
                self._connection.executemany(
                    "INSERT OR IGNORE INTO legacy_github_deliveries(repository, delivery_id) VALUES (?, ?)",
                    ((repository, self._raw_legacy_delivery_id(delivery_id)) for repository, delivery_id in legacy_rows),
                )
        delivery_schema = self._connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'github_deliveries'").fetchone()[0]
        if "'dependency'" not in delivery_schema:
            self._connection.executescript("""
                ALTER TABLE github_deliveries RENAME TO github_deliveries_v2;
                CREATE TABLE github_deliveries (
                    repository TEXT NOT NULL, delivery_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL CHECK(entity_type IN ('issue', 'pr', 'dependency')),
                    entity_number INTEGER NOT NULL, event_type TEXT, action TEXT,
                    PRIMARY KEY(repository, delivery_id, entity_type, entity_number)
                );
                INSERT INTO github_deliveries SELECT * FROM github_deliveries_v2;
                DROP TABLE github_deliveries_v2;
                """)

    def accept_ci_delivery(self, delivery: CIWebhookDelivery, now: Optional[float] = None) -> bool:
        """Atomically retain a CI delivery and advance its durable observation scopes."""
        received = time.time() if now is None else now
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO ci_webhook_deliveries
                   (repository, delivery_id, event_type, action, head_sha,
                    workflow_id, run_id, run_attempt, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (delivery.repository, delivery.delivery_id, delivery.event_type, delivery.action, delivery.head_sha, delivery.workflow_id, delivery.run_id, delivery.run_attempt, received),
            )
            if cursor.rowcount == 0:
                return False
            for number in delivery.pull_request_numbers:
                self._connection.execute(
                    "INSERT INTO ci_delivery_targets(repository, delivery_id, pr_number) VALUES (?, ?, ?)",
                    (delivery.repository, delivery.delivery_id, number),
                )
                self._advance_ci_pr(delivery.repository, number, received)
                # A webhook is invalidation evidence, not a result.  Move the
                # matching durable observation obligation to the same quiet
                # window; the consumer will still perform an authoritative read.
                self._connection.execute(
                    """UPDATE ci_watches SET observation_epoch = observation_epoch + 1,
                           next_reconcile_at = MIN(next_reconcile_at, ?)
                       WHERE repository = ? AND pr_number = ? AND active = 1""",
                    (received + 2, delivery.repository, number),
                )
            if not delivery.pull_request_numbers and delivery.head_sha:
                self._connection.execute(
                    """INSERT INTO ci_correlations(repository, head_sha, observation_epoch,
                           first_seen, latest_seen, eligible_at, state)
                       VALUES (?, ?, 1, ?, ?, ?, 'pending')
                       ON CONFLICT(repository, head_sha) DO UPDATE SET
                           observation_epoch = observation_epoch + 1,
                           latest_seen = excluded.latest_seen,
                           eligible_at = MIN(first_seen + 10, excluded.latest_seen + 2),
                           state = 'pending'""",
                    (delivery.repository, delivery.head_sha, received, received, received + 2),
                )
            return True

    def ensure_ci_watch(self, repository: str, pr_number: int, head_sha: str, workflow_id: str = "", *, now: Optional[float] = None) -> bool:
        """Durably create a targeted CI obligation without weakening an existing one."""
        if not repository or pr_number <= 0 or not head_sha:
            return False
        created = time.time() if now is None else now
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO ci_watches(repository, pr_number, head_sha, workflow_id,
                       next_reconcile_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(repository, pr_number, head_sha, workflow_id)
                   DO UPDATE SET active = 1""",
                (repository, pr_number, head_sha, workflow_id, created),
            )
        return True

    def promote_due_ci_watches(self, repository: str, now: Optional[float] = None) -> int:
        """Promote only due targeted watches and retain their 300-second clock."""
        attempted = time.time() if now is None else now
        with self._lock, self._connection:
            rows = self._connection.execute(
                """SELECT pr_number, head_sha, workflow_id FROM ci_watches
                   WHERE repository = ? AND active = 1 AND operational_block IS NULL
                     AND next_reconcile_at <= ?""",
                (repository, attempted),
            ).fetchall()
            for number, head_sha, workflow_id in rows:
                self._connection.execute(
                    """UPDATE ci_watches SET last_attempt_at = ?, next_reconcile_at = ?
                       WHERE repository = ? AND pr_number = ? AND head_sha = ? AND workflow_id = ?""",
                    (attempted, attempted + CI_RECONCILIATION_SECONDS, repository, number, head_sha, workflow_id),
                )
                self._connection.execute(
                    """INSERT INTO entity_invalidations(repository, entity_type, entity_number, generation, state)
                       VALUES (?, 'pr', ?, 1, 'dirty')
                       ON CONFLICT(repository, entity_type, entity_number) DO UPDATE SET
                         generation = CASE WHEN state = 'processing' THEN generation + 1 ELSE generation END""",
                    (repository, number),
                )
        return len(rows)

    def retire_ci_watches(self, repository: str, pr_number: int, current_head: Optional[str] = None) -> None:
        """Stop obsolete periodic work while retaining watch and claim history."""
        with self._lock, self._connection:
            if current_head is None:
                self._connection.execute(
                    "UPDATE ci_watches SET active = 0 WHERE repository = ? AND pr_number = ?",
                    (repository, pr_number),
                )
            else:
                self._connection.execute(
                    "UPDATE ci_watches SET active = 0 WHERE repository = ? AND pr_number = ? AND head_sha <> ?",
                    (repository, pr_number, current_head),
                )

    def restore_ci_watches_for_open_lifecycle(self, repository: str, pr_number: int, current_head: str) -> bool:
        """Restore current-head recovery only when this PR has watch history.

        The strict authoritative refresh calls this before processing admission.
        Consequently a reopened lifecycle can recover a retired obligation even
        when implementation policy now excludes the PR, without creating watch
        obligations for open PRs that have never entered the supported watch
        path.
        """
        if not repository or pr_number <= 0 or not current_head:
            return False
        with self._lock, self._connection:
            history = self._connection.execute(
                "SELECT 1 FROM ci_watches WHERE repository = ? AND pr_number = ? LIMIT 1",
                (repository, pr_number),
            ).fetchone()
            if history is None:
                return False
            self._connection.execute(
                "UPDATE ci_watches SET active = 0 WHERE repository = ? AND pr_number = ? AND head_sha <> ?",
                (repository, pr_number, current_head),
            )
            current = self._connection.execute(
                "SELECT 1 FROM ci_watches WHERE repository = ? AND pr_number = ? AND head_sha = ? LIMIT 1",
                (repository, pr_number, current_head),
            ).fetchone()
            if current is None:
                self._connection.execute(
                    "INSERT INTO ci_watches(repository, pr_number, head_sha, workflow_id, next_reconcile_at) VALUES (?, ?, ?, '', ?)",
                    (repository, pr_number, current_head, time.time()),
                )
            else:
                self._connection.execute(
                    "UPDATE ci_watches SET active = 1 WHERE repository = ? AND pr_number = ? AND head_sha = ?",
                    (repository, pr_number, current_head),
                )
        return True

    def schedule_ci_watch_recheck(self, repository: str, pr_number: int, head_sha: str, delay_seconds: float = 30.0) -> bool:
        """Advance next_reconcile_at for an active watch when checks remain in progress."""
        if not repository or pr_number <= 0 or not head_sha:
            return False
        target = time.time() + delay_seconds
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """UPDATE ci_watches SET next_reconcile_at = MIN(next_reconcile_at, ?)
                   WHERE repository = ? AND pr_number = ? AND head_sha = ? AND active = 1""",
                (target, repository, pr_number, head_sha),
            )
            return cursor.rowcount > 0

    def _advance_ci_pr(self, repository: str, number: int, received: float) -> None:
        self._connection.execute(
            """INSERT INTO ci_pending_prs(repository, pr_number, observation_epoch,
                   first_seen, latest_seen, eligible_at)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(repository, pr_number) DO UPDATE SET
                   observation_epoch = observation_epoch + 1,
                   latest_seen = excluded.latest_seen,
                   eligible_at = MIN(first_seen + 10, excluded.latest_seen + 2)""",
            (repository, number, received, received, received + 2),
        )

    def claim_ci_correlation(self, repository: str) -> Optional[str]:
        """Claim one due SHA lookup; an interrupted claim is recovered on startup."""
        with self._lock, self._connection:
            row = self._connection.execute(
                """UPDATE ci_correlations SET state = 'processing'
                   WHERE rowid = (SELECT rowid FROM ci_correlations
                     WHERE repository = ? AND state = 'pending' AND eligible_at <= ?
                     ORDER BY eligible_at LIMIT 1)
                   RETURNING head_sha""",
                (repository, time.time()),
            ).fetchone()
            return str(row[0]) if row else None

    def finish_ci_correlation(self, repository: str, sha: str, numbers: list[int]) -> bool:
        """Fan a completely resolved correlation into recoverable delivery targets."""
        with self._lock, self._connection:
            scope = self._connection.execute(
                """SELECT observation_epoch, first_seen, latest_seen, eligible_at, state
                   FROM ci_correlations WHERE repository = ? AND head_sha = ?""",
                (repository, sha),
            ).fetchone()
            if scope is None:
                raise RuntimeError("CI correlation claim is no longer current")
            if scope[4] == "pending":
                # A delivery received during the lookup supersedes its result.
                # Retain that delivery's quiet window and durable retry instead
                # of terminating the controller or applying stale PR membership.
                return False
            deliveries = self._connection.execute(
                """SELECT delivery_id FROM ci_webhook_deliveries
                   WHERE repository = ? AND head_sha = ? AND NOT EXISTS
                     (SELECT 1 FROM ci_delivery_targets t WHERE t.repository = ci_webhook_deliveries.repository
                      AND t.delivery_id = ci_webhook_deliveries.delivery_id)""",
                (repository, sha),
            ).fetchall()
            for (delivery_id,) in deliveries:
                for number in sorted(set(numbers)):
                    self._connection.execute(
                        "INSERT OR IGNORE INTO ci_delivery_targets(repository, delivery_id, pr_number) VALUES (?, ?, ?)",
                        (repository, delivery_id, number),
                    )
            epoch, first_seen, latest_seen, eligible_at, _ = scope
            for number in sorted(set(numbers)):
                self._connection.execute(
                    """INSERT INTO ci_pending_prs(repository, pr_number, observation_epoch,
                           first_seen, latest_seen, eligible_at) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(repository, pr_number) DO UPDATE SET
                           observation_epoch = observation_epoch + excluded.observation_epoch,
                           latest_seen = MAX(latest_seen, excluded.latest_seen),
                           eligible_at = MIN(first_seen + 10, MAX(eligible_at, excluded.eligible_at))""",
                    (repository, number, epoch, first_seen, latest_seen, eligible_at),
                )
                self._connection.execute(
                    """UPDATE ci_watches SET observation_epoch = observation_epoch + ?,
                           next_reconcile_at = MIN(next_reconcile_at, ?)
                       WHERE repository = ? AND pr_number = ? AND active = 1""",
                    (epoch, eligible_at, repository, number),
                )
            self._connection.execute("DELETE FROM ci_correlations WHERE repository = ? AND head_sha = ?", (repository, sha))
            try:
                from .util.gh_cache import evict_github_entity_cache

                for number in sorted(set(numbers)):
                    evict_github_entity_cache(repository, "pr", number)
            except Exception as exc:
                logger.debug(f"Failed to evict PR cache in finish_ci_correlation: {exc}")
            return True

    def release_ci_correlation(self, repository: str, sha: str, retry_after: float = 60) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE ci_correlations SET state = 'pending', eligible_at = MAX(eligible_at, ?) WHERE repository = ? AND head_sha = ?",
                (time.time() + retry_after, repository, sha),
            )

    def promote_due_ci(self, repository: str) -> int:
        """Atomically turn due PR batches into ordinary authoritative reevaluations."""
        promoted = 0
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT pr_number, observation_epoch FROM ci_pending_prs WHERE repository = ? AND eligible_at <= ?",
                (repository, time.time()),
            ).fetchall()
            for number, epoch in rows:
                target_rows = self._connection.execute(
                    """SELECT t.delivery_id, d.event_type, d.action FROM ci_delivery_targets t
                       JOIN ci_webhook_deliveries d USING(repository, delivery_id)
                       WHERE t.repository = ? AND t.pr_number = ? AND t.applied = 0""",
                    (repository, number),
                ).fetchall()
                for delivery_id, event_type, action in target_rows:
                    self._connection.execute(
                        """INSERT OR IGNORE INTO github_deliveries
                           (repository, delivery_id, entity_type, entity_number, event_type, action)
                           VALUES (?, ?, 'pr', ?, ?, ?)""",
                        (repository, delivery_id, number, event_type, action),
                    )
                    self._connection.execute(
                        "UPDATE ci_delivery_targets SET applied = 1 WHERE repository = ? AND delivery_id = ? AND pr_number = ?",
                        (repository, delivery_id, number),
                    )
                self._connection.execute(
                    """INSERT INTO entity_invalidations(repository, entity_type, entity_number, generation, state)
                       VALUES (?, 'pr', ?, 1, 'dirty')
                       ON CONFLICT(repository, entity_type, entity_number) DO UPDATE SET
                         generation = CASE WHEN state = 'processing' THEN generation + 1 ELSE generation END""",
                    (repository, number),
                )
                self._connection.execute("DELETE FROM ci_pending_prs WHERE repository = ? AND pr_number = ? AND observation_epoch = ?", (repository, number, epoch))
                promoted += 1
        return promoted

    def seconds_until_next_ci(self, repository: str) -> Optional[float]:
        with self._lock:
            row = self._connection.execute(
                """SELECT MIN(eligible_at) FROM (
                     SELECT eligible_at FROM ci_pending_prs WHERE repository = ?
                     UNION ALL SELECT eligible_at FROM ci_correlations WHERE repository = ? AND state = 'pending'
                     UNION ALL SELECT next_reconcile_at FROM ci_watches
                       WHERE repository = ? AND active = 1 AND operational_block IS NULL)""",
                (repository, repository, repository),
            ).fetchone()
        return None if not row or row[0] is None else max(0.0, float(row[0]) - time.time())

    @staticmethod
    def _raw_legacy_delivery_id(delivery_id: str) -> str:
        """Undo the former adapter's ``:<entity index>`` delivery suffix."""
        raw_delivery_id, separator, index = delivery_id.rpartition(":")
        if separator and raw_delivery_id and index.isdigit():
            return raw_delivery_id
        return delivery_id

    def recover(self, repository: str) -> list[EntityIdentity]:
        """Make work interrupted by process termination claimable again.

        Returns every identity that was actually `queued`/`processing` and
        got reset to `dirty` by this call, for diagnostic recovery evidence
        (Issue #2001). No existing caller consumed the previous `None`
        return, so this is purely additive.
        """
        with self._lock, self._connection:
            rows = self._connection.execute(
                """UPDATE entity_invalidations SET state = 'dirty', claimed_generation = NULL
                   WHERE repository = ? AND state IN ('queued', 'processing')
                   RETURNING entity_type, entity_number""",
                (repository,),
            ).fetchall()
            self._connection.execute(
                "UPDATE ci_correlations SET state = 'pending' WHERE repository = ? AND state = 'processing'",
                (repository,),
            )
        return [EntityIdentity(repository, entity_type, number) for entity_type, number in rows]

    def invalidate(
        self,
        identity: EntityIdentity,
        delivery_id: Optional[str] = None,
        event_type: Optional[str] = None,
        action: Optional[str] = None,
        not_before: Optional[float] = None,
        urgent_admission: bool = False,
    ) -> bool:
        """Persist an invalidation; return False only for a duplicate delivery."""
        return self.invalidate_with_transition(identity, delivery_id, event_type, action, not_before, urgent_admission).accepted

    def invalidate_with_transition(
        self,
        identity: EntityIdentity,
        delivery_id: Optional[str] = None,
        event_type: Optional[str] = None,
        action: Optional[str] = None,
        not_before: Optional[float] = None,
        urgent_admission: bool = False,
    ) -> InvalidationTransition:
        """`invalidate()`, additionally reporting the actual committed transition.

        The prior row's state is observed under the same lock/transaction
        that performs the upsert, so the returned disposition describes the
        real transition this call committed, never a guess from a later
        unlocked read.
        """
        with self._lock, self._connection:
            if delivery_id:
                legacy = self._connection.execute(
                    "SELECT 1 FROM legacy_github_deliveries WHERE repository = ? AND delivery_id = ?",
                    (identity.repository, delivery_id),
                ).fetchone()
                if legacy is not None:
                    return InvalidationTransition(False, None)
                cursor = self._connection.execute(
                    """INSERT OR IGNORE INTO github_deliveries
                       (repository, delivery_id, entity_type, entity_number, event_type, action)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (identity.repository, delivery_id, identity.entity_type, identity.number, event_type, action),
                )
                if cursor.rowcount == 0:
                    return InvalidationTransition(False, None)
            existing = self._connection.execute(
                "SELECT state FROM entity_invalidations WHERE repository = ? AND entity_type = ? AND entity_number = ?",
                (identity.repository, identity.entity_type, identity.number),
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO entity_invalidations(repository, entity_type, entity_number, generation, state, not_before, urgent_admission)
                VALUES (?, ?, ?, 1, 'dirty', ?, ?)
                ON CONFLICT(repository, entity_type, entity_number) DO UPDATE SET
                    generation = CASE
                        WHEN state = 'processing' THEN generation + 1
                        ELSE generation
                    END,
                    not_before = CASE
                        WHEN entity_invalidations.not_before IS NULL THEN excluded.not_before
                        WHEN excluded.not_before IS NULL THEN entity_invalidations.not_before
                        ELSE MIN(entity_invalidations.not_before, excluded.not_before)
                    END,
                    urgent_admission = MAX(entity_invalidations.urgent_admission, excluded.urgent_admission)
                """,
                (identity.repository, identity.entity_type, identity.number, not_before, int(urgent_admission)),
            )
            if existing is None:
                disposition = InvalidationDisposition.NEW_PENDING
            elif existing[0] == "processing":
                disposition = InvalidationDisposition.FOLLOWUP_REQUIRED
            else:
                disposition = InvalidationDisposition.COALESCED
            return InvalidationTransition(True, disposition)

    def claim(self, repository: str) -> Optional[ClaimedInvalidation]:
        """Atomically reserve one dirty identity for the in-memory queue."""
        with self._lock, self._connection:
            row = self._connection.execute(
                """UPDATE entity_invalidations
                   SET state = 'queued', claimed_generation = generation
                   WHERE rowid = (
                       SELECT rowid FROM entity_invalidations
                       WHERE repository = ? AND state = 'dirty'
                         AND (not_before IS NULL OR not_before <= ?)
                         AND (retry_not_before IS NULL OR retry_not_before <= ?)
                       ORDER BY rowid LIMIT 1
                   ) AND state = 'dirty'
                   RETURNING entity_type, entity_number, generation, urgent_admission""",
                (repository, time.time(), time.time()),
            ).fetchone()
            if row is None:
                return None
            entity_type, number, generation, urgent_admission = row
            return ClaimedInvalidation(EntityIdentity(repository, entity_type, number), generation, bool(urgent_admission))

    def seconds_until_next_ready(self, repository: str) -> Optional[float]:
        """Return the delay until the earliest dirty invalidation is eligible."""
        with self._lock:
            row = self._connection.execute(
                """SELECT MIN(MAX(COALESCE(not_before, 0), COALESCE(retry_not_before, 0)))
                   FROM entity_invalidations WHERE repository = ? AND state = 'dirty'
                     AND (not_before IS NOT NULL OR retry_not_before IS NOT NULL)""",
                (repository,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return max(0.0, float(row[0]) - time.time())

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
        return self.complete_with_outcome(claim).has_followup

    def complete_with_outcome(self, claim: ClaimedInvalidation) -> CompletionTransition:
        """`complete()`, additionally reporting the actual committed transition.

        `STALE_NO_OP` distinguishes a claim that no longer matched the
        current row (already completed/released/reclaimed) from a genuine
        `CLEARED` completion; the legacy Boolean alone conflates both as
        `False`.
        """
        identity = claim.identity
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT generation, claimed_generation, state FROM entity_invalidations
                   WHERE repository = ? AND entity_type = ? AND entity_number = ?""",
                (identity.repository, identity.entity_type, identity.number),
            ).fetchone()
            if row is None or row[1] != claim.generation or row[2] != "processing":
                return CompletionTransition(CompletionOutcome.STALE_NO_OP, False, None)
            if row[0] > claim.generation:
                self._connection.execute(
                    """UPDATE entity_invalidations SET state = 'dirty', claimed_generation = NULL,
                           retry_not_before = NULL, deferral_reason = NULL, deferral_api_origin = NULL
                       WHERE repository = ? AND entity_type = ? AND entity_number = ?""",
                    (identity.repository, identity.entity_type, identity.number),
                )
                return CompletionTransition(CompletionOutcome.FOLLOWUP_PENDING, True, int(row[0]))
            self._connection.execute(
                "DELETE FROM entity_invalidations WHERE repository = ? AND entity_type = ? AND entity_number = ?",
                (identity.repository, identity.entity_type, identity.number),
            )
            return CompletionTransition(CompletionOutcome.CLEARED, False, None)

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

    def defer(
        self,
        claim: ClaimedInvalidation,
        reason: str,
        retry_at: Optional[float],
        api_origin: Optional[str] = None,
        now: Optional[float] = None,
    ) -> DeferredInvalidation:
        """Atomically retain a claimed reevaluation behind a durable retry guard."""
        handled_at = time.time() if now is None else now
        supplied = retry_at if retry_at is not None and math.isfinite(retry_at) and retry_at >= 0 else 0.0
        identity = claim.identity
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT generation, not_before, retry_not_before FROM entity_invalidations
                   WHERE repository = ? AND entity_type = ? AND entity_number = ?
                     AND state = 'processing' AND claimed_generation = ?""",
                (identity.repository, identity.entity_type, identity.number, claim.generation),
            ).fetchone()
            if row is None:
                raise RuntimeError("invalidation deferral claim is no longer current")
            generation, stabilization, retained_retry = row
            deadline = max(
                handled_at + 1.0,
                supplied,
                float(stabilization or 0.0),
                float(retained_retry or 0.0),
            )
            cursor = self._connection.execute(
                """UPDATE entity_invalidations
                   SET state = 'dirty', claimed_generation = NULL, retry_not_before = ?,
                       deferral_reason = ?, deferral_api_origin = ?
                   WHERE repository = ? AND entity_type = ? AND entity_number = ?
                     AND state = 'processing' AND claimed_generation = ?""",
                (deadline, reason, api_origin, identity.repository, identity.entity_type, identity.number, claim.generation),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("invalidation deferral transition was not committed")
        return DeferredInvalidation(identity, int(generation), deadline, reason, api_origin)

    def wake_dependency_waiters(self, repository: str) -> None:
        """Issue notifications may release advisory waits, never GitHub cooldowns."""
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE entity_invalidations
                   SET retry_not_before = NULL, deferral_reason = NULL
                   WHERE repository = ? AND state = 'dirty'
                     AND deferral_reason = 'cached_dependency_wait'""",
                (repository,),
            )

    def get_deferred(self, identity: EntityIdentity) -> Optional[DeferredInvalidation]:
        """Return persisted retry metadata for diagnostics and tests."""
        with self._lock:
            row = self._connection.execute(
                """SELECT generation, retry_not_before, deferral_reason, deferral_api_origin
                   FROM entity_invalidations WHERE repository = ? AND entity_type = ? AND entity_number = ?
                     AND retry_not_before IS NOT NULL""",
                (identity.repository, identity.entity_type, identity.number),
            ).fetchone()
        if row is None:
            return None
        return DeferredInvalidation(identity, int(row[0]), float(row[1]), str(row[2]), row[3])

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
