"""Durable dispatch-claim store for idempotent manual CI workflow_dispatch.

Implements the correctness boundary described in GitHub Issue #1791: a
manual ``workflow_dispatch`` must be admitted at most once per dispatch
identity (repository, PR number, authoritative head SHA, workflow
identifier), the admission decision must survive controller restart, and
any uncertainty about the claim state must fail closed (deny dispatch)
rather than risk a duplicate external call.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)

# Serializes access to a given store instance from this process. SQLite's own
# "BEGIN IMMEDIATE" transaction locking is what makes cross-process /
# cross-worker admission atomic; this lock only avoids redundant contention
# between threads of this process.
_DB_LOCK = threading.Lock()


class DispatchOutcome(str, Enum):
    """Observable outcome classes for an external workflow_dispatch call.

    PENDING means a claim has been durably recorded but the external call's
    outcome is not yet known or not yet recorded (e.g. a crash between
    publishing the claim and observing the result). PENDING is suppressing,
    identically to ACCEPTED and INDETERMINATE: only REJECTED unblocks reuse
    of the same dispatch identity.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class DispatchIdentity:
    """A manual CI dispatch identity per REQ-001: repo + PR + head SHA + workflow."""

    repo_name: str
    pr_number: int
    head_sha: str
    workflow_id: str

    def key(self) -> str:
        return f"{self.repo_name}:{self.pr_number}:{self.head_sha}:{self.workflow_id}"


@dataclass
class ClaimResult:
    """Result of attempting to acquire a dispatch-suppressing claim."""

    acquired: bool
    reason: str = ""


def default_dispatch_claim_db_path() -> Path:
    return Path.home() / ".auto-coder" / "dispatch_claims.db"


class DispatchClaimStore:
    """Fail-closed, restart-durable store of manual workflow_dispatch claims.

    Backed by SQLite so a suppressing claim is visible to every worker of the
    single authoritative controller and survives controller restart. Every
    operation fails closed: any exception while connecting, locking, reading,
    or writing is treated as "claim state unknown" and therefore denies
    dispatch admission (REQ-003).
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = Path(db_path) if db_path is not None else default_dispatch_claim_db_path()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatch_claims (
                claim_key TEXT PRIMARY KEY,
                repo_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    def try_acquire_claim(self, identity: DispatchIdentity) -> ClaimResult:
        """Attempt to durably acquire the dispatch-suppressing claim for identity.

        Returns ``acquired=True`` only when this call is the one permitted to
        invoke the external ``workflow_dispatch`` operation for this identity
        (REQ-002). A prior claim in any state other than REJECTED keeps the
        identity suppressing (REQ-004, REQ-006); a prior REJECTED claim is
        reclaimed so the identity becomes dispatchable again (REQ-005/AS-005).
        Any storage failure denies acquisition (REQ-003).
        """
        key = identity.key()
        try:
            with _DB_LOCK:
                conn = self._connect()
                try:
                    self._ensure_schema(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        row = conn.execute(
                            "SELECT state FROM dispatch_claims WHERE claim_key = ?",
                            (key,),
                        ).fetchone()
                        now = time.time()

                        if row is None:
                            conn.execute(
                                "INSERT INTO dispatch_claims " "(claim_key, repo_name, pr_number, head_sha, workflow_id, state, created_at, updated_at) " "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    key,
                                    identity.repo_name,
                                    identity.pr_number,
                                    identity.head_sha,
                                    identity.workflow_id,
                                    DispatchOutcome.PENDING.value,
                                    now,
                                    now,
                                ),
                            )
                            conn.execute("COMMIT")
                            return ClaimResult(acquired=True)

                        state = row[0]
                        if state == DispatchOutcome.REJECTED.value:
                            conn.execute(
                                "UPDATE dispatch_claims SET state = ?, updated_at = ? WHERE claim_key = ?",
                                (DispatchOutcome.PENDING.value, now, key),
                            )
                            conn.execute("COMMIT")
                            return ClaimResult(acquired=True)

                        conn.execute("COMMIT")
                        return ClaimResult(acquired=False, reason=f"existing claim state={state}")
                    except Exception:
                        try:
                            conn.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                finally:
                    conn.close()
        except Exception as e:
            logger.error(f"Dispatch claim store error while acquiring claim for {key}: {e}")
            return ClaimResult(acquired=False, reason=f"store error: {e}")

    def record_outcome(self, identity: DispatchIdentity, outcome: DispatchOutcome) -> bool:
        """Durably record the observable outcome of a dispatch attempt.

        Returns True only if the write is confirmed. On storage failure the
        caller must treat the identity as still suppressing (it already is:
        the claim row remains in its previously-recorded, suppressing state).
        """
        key = identity.key()
        try:
            with _DB_LOCK:
                conn = self._connect()
                try:
                    self._ensure_schema(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        now = time.time()
                        conn.execute(
                            "UPDATE dispatch_claims SET state = ?, updated_at = ? WHERE claim_key = ?",
                            (outcome.value, now, key),
                        )
                        conn.execute("COMMIT")
                        return True
                    except Exception:
                        try:
                            conn.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                finally:
                    conn.close()
        except Exception as e:
            logger.error(f"Dispatch claim store error while recording outcome for {key}: {e}")
            return False


_default_store: Optional[DispatchClaimStore] = None
_default_store_lock = threading.Lock()


def get_dispatch_claim_store() -> DispatchClaimStore:
    """Return the process-wide default DispatchClaimStore instance."""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = DispatchClaimStore()
        return _default_store
