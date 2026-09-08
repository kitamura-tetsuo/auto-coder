"""Durable application obligations interrupted by GitHub availability.

The request governor controls wire admission.  This store controls the distinct
application question: whether unfinished semantic work may be forgotten.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from .logger_config import get_logger
from .util.github_request_outcome import GitHubApiOutcome, GitHubRequestError

logger = get_logger(__name__)
_LOCK = threading.Lock()
_DEFAULT_STORE: PendingWorkStore | None = None
MAX_THROTTLED_RETRIES = 3


class PendingReason(str, Enum):
    THROTTLED = "throttled"
    ADMISSION_DEFERRED = "admission_deferred"
    AUTHENTICATION = "authentication_failure"
    FORBIDDEN = "forbidden"
    INDETERMINATE = "indeterminate_delivery"
    RETRIES_EXHAUSTED = "throttle_retries_exhausted"


@dataclass(frozen=True)
class WorkIdentity:
    repository: str
    entity: str
    stage: str
    revision: str = ""

    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PendingObligation:
    identity: WorkIdentity
    reason: PendingReason
    not_before: float
    unfinished_effects: tuple[str, ...] = field(default_factory=tuple)
    throttle_attempts: int = 0
    last_error: str = ""

    @property
    def automatically_retryable(self) -> bool:
        return self.reason in {PendingReason.THROTTLED, PendingReason.ADMISSION_DEFERRED}


def default_pending_work_path() -> Path:
    return Path.home() / ".auto-coder" / "github_pending_work.db"


class PendingWorkPersistenceError(RuntimeError):
    """Persistence uncertainty; dependent effects must stop (fail closed)."""


class PendingWorkStore:
    """SQLite-backed semantic retry queue, shared across workers and restarts."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_pending_work_path()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS github_pending_work (
            work_key TEXT PRIMARY KEY, repository TEXT NOT NULL, entity TEXT NOT NULL,
            stage TEXT NOT NULL, revision TEXT NOT NULL, reason TEXT NOT NULL,
            not_before REAL NOT NULL, unfinished_effects TEXT NOT NULL,
            throttle_attempts INTEGER NOT NULL, last_error TEXT NOT NULL,
            updated_at REAL NOT NULL)"""
        )
        return connection

    def defer(
        self,
        identity: WorkIdentity,
        error: GitHubRequestError,
        unfinished_effects: tuple[str, ...],
        *,
        governor_deadline: float | None = None,
        now: float | None = None,
    ) -> PendingObligation:
        """Atomically retain work; only actual throttle responses spend retries."""
        current_time = time.time() if now is None else now
        classification = error.outcome.classification
        if classification is GitHubApiOutcome.REFUSED:
            reason = PendingReason.ADMISSION_DEFERRED
        elif classification is GitHubApiOutcome.AUTHENTICATION_FAILURE:
            reason = PendingReason.AUTHENTICATION
        elif classification is GitHubApiOutcome.FORBIDDEN:
            reason = PendingReason.FORBIDDEN
        elif error.outcome.delivery.value == "indeterminate_after_possible_send":
            reason = PendingReason.INDETERMINATE
        else:
            reason = PendingReason.THROTTLED
        retry_after = error.outcome.metadata.retry_after_seconds or 0.0
        due = max(current_time + retry_after, governor_deadline or current_time)
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute(
                    "SELECT throttle_attempts, not_before, unfinished_effects FROM github_pending_work WHERE work_key=?",
                    (identity.key(),),
                ).fetchone()
                attempts = int(row[0]) if row else 0
                if reason is PendingReason.THROTTLED:
                    attempts += 1
                    if attempts > MAX_THROTTLED_RETRIES:
                        reason = PendingReason.RETRIES_EXHAUSTED
                prior_due = float(row[1]) if row else 0.0
                prior_effects = tuple(json.loads(row[2])) if row else ()
                effects = tuple(dict.fromkeys((*prior_effects, *unfinished_effects)))
                # Authentication, forbidden, ambiguity and exhausted retries are
                # operational blocks: they are never timer-driven.
                if reason not in {PendingReason.THROTTLED, PendingReason.ADMISSION_DEFERRED}:
                    due = 0.0
                scheduled_for = max(due, prior_due) if reason in {PendingReason.THROTTLED, PendingReason.ADMISSION_DEFERRED} else 0.0
                obligation = PendingObligation(identity, reason, scheduled_for, effects, attempts, str(error))
                connection.execute(
                    "INSERT OR REPLACE INTO github_pending_work VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identity.key(),
                        identity.repository,
                        identity.entity,
                        identity.stage,
                        identity.revision,
                        reason.value,
                        obligation.not_before,
                        json.dumps(effects),
                        attempts,
                        obligation.last_error,
                        current_time,
                    ),
                )
                return obligation
        except Exception as exc:
            logger.error("Could not persist GitHub pending obligation {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be persisted") from exc

    def due(self, now: float | None = None) -> list[PendingObligation]:
        current_time = time.time() if now is None else now
        try:
            with _LOCK, self._connect() as connection:
                rows = connection.execute(
                    "SELECT repository,entity,stage,revision,reason,not_before,unfinished_effects,throttle_attempts,last_error " "FROM github_pending_work WHERE not_before > 0 AND not_before <= ? ORDER BY not_before",
                    (current_time,),
                ).fetchall()
            return [PendingObligation(WorkIdentity(*row[:4]), PendingReason(row[4]), row[5], tuple(json.loads(row[6])), row[7], row[8]) for row in rows]
        except Exception as exc:
            logger.error("Could not read GitHub pending obligations: {}", exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be read") from exc

    def complete_effect(self, identity: WorkIdentity, effect: str) -> bool:
        """Confirm one durable effect without completing its independent siblings."""
        try:
            with _LOCK, self._connect() as connection:
                row = connection.execute("SELECT unfinished_effects FROM github_pending_work WHERE work_key=?", (identity.key(),)).fetchone()
                if row is None:
                    return False
                remaining = tuple(item for item in json.loads(row[0]) if item != effect)
                if remaining:
                    connection.execute(
                        "UPDATE github_pending_work SET unfinished_effects=?, updated_at=? WHERE work_key=?",
                        (json.dumps(remaining), time.time(), identity.key()),
                    )
                else:
                    connection.execute("DELETE FROM github_pending_work WHERE work_key=?", (identity.key(),))
                return True
        except Exception as exc:
            logger.error("Could not complete GitHub pending effect {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending effect could not be persisted") from exc

    def supersede(self, identity: WorkIdentity) -> None:
        """Discard effects after an authoritative refresh proves the revision stale."""
        try:
            with _LOCK, self._connect() as connection:
                connection.execute("DELETE FROM github_pending_work WHERE work_key=?", (identity.key(),))
        except Exception as exc:
            logger.error("Could not supersede GitHub pending obligation {}: {}", identity.key(), exc)
            raise PendingWorkPersistenceError("GitHub pending work could not be updated") from exc


def get_pending_work_store() -> PendingWorkStore:
    """Return the process-wide durable obligation store."""
    global _DEFAULT_STORE
    with _LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = PendingWorkStore()
        return _DEFAULT_STORE
