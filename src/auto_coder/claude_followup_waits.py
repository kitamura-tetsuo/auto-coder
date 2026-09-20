"""Durable admission holds for quota-blocked Claude follow-up work."""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .exceptions import ClaudeFollowupUsageLimitError, DeliveryCertainty


@dataclass(frozen=True)
class ClaudeFollowupWait:
    repository: str
    pr_number: int
    backend_name: str
    credential_context: str
    task_id: str
    purpose: str
    work_identity: str
    reason: str
    observed_at: float
    retry_not_before: float
    certainty: DeliveryCertainty


@dataclass(frozen=True)
class ClaudeFollowupAdmission:
    token: str
    repository: str
    backend_name: str
    credential_context: str
    refusal_epoch: float


class ClaudeFollowupHoldActive(RuntimeError):
    """Raised before a usage read when a context hold or recheck owns admission."""

    def __init__(self, retry_not_before: float) -> None:
        self.retry_not_before = retry_not_before
        super().__init__(f"Claude quota hold is active until {retry_not_before}")


class ClaudeFollowupWaitStore:
    """SQLite store separating definitely-unsent work from uncertain delivery."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (
            path
            or Path(
                os.environ.get(
                    "AUTO_CODER_CLAUDE_FOLLOWUP_DB",
                    "~/.auto-coder/claude-followup-waits.sqlite3",
                )
            ).expanduser()
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS claude_followup_waits (
              repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
              backend_name TEXT NOT NULL, credential_context TEXT NOT NULL,
              task_id TEXT NOT NULL, purpose TEXT NOT NULL,
              work_identity TEXT NOT NULL, reason TEXT NOT NULL,
              observed_at REAL NOT NULL, retry_not_before REAL NOT NULL,
              certainty TEXT NOT NULL, details TEXT NOT NULL,
              PRIMARY KEY(repository, backend_name, credential_context,
                          task_id, purpose, work_identity)
            )
            """
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS claude_followup_admissions (
                 repository TEXT NOT NULL, backend_name TEXT NOT NULL,
                 credential_context TEXT NOT NULL, token TEXT NOT NULL,
                 refusal_epoch REAL NOT NULL, lease_until REAL NOT NULL,
                 PRIMARY KEY(repository, backend_name, credential_context))"""
        )
        self._connection.commit()

    def active_hold(
        self,
        repository: str,
        backend_name: str,
        credential_context: str,
        now: Optional[float] = None,
    ) -> Optional[float]:
        """Return the context deadline; uncertain deliveries also remain held."""
        at = time.time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                """SELECT MAX(retry_not_before) FROM claude_followup_waits
                   WHERE repository=? AND backend_name=? AND credential_context=?
                     AND (retry_not_before>? OR certainty=?)""",
                (repository, backend_name, credential_context, at, DeliveryCertainty.INDETERMINATE.value),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def get(self, repository: str, task_id: str, purpose: str, work_identity: str) -> Optional[ClaudeFollowupWait]:
        with self._lock:
            row = self._connection.execute(
                """SELECT repository,pr_number,backend_name,credential_context,
                          task_id,purpose,work_identity,reason,observed_at,
                          retry_not_before,certainty FROM claude_followup_waits
                   WHERE repository=? AND task_id=? AND purpose=? AND work_identity=?""",
                (repository, task_id, purpose, work_identity),
            ).fetchone()
        if not row:
            return None
        return ClaudeFollowupWait(
            repository=str(row[0]),
            pr_number=int(row[1]),
            backend_name=str(row[2]),
            credential_context=str(row[3]),
            task_id=str(row[4]),
            purpose=str(row[5]),
            work_identity=str(row[6]),
            reason=str(row[7]),
            observed_at=float(row[8]),
            retry_not_before=float(row[9]),
            certainty=DeliveryCertainty(row[10]),
        )

    def retain(self, wait: ClaudeFollowupWait, details: tuple[str, ...] = ()) -> None:
        """Coalesce the same work without shortening an established wait."""
        with self._lock:
            self._connection.execute(
                """INSERT INTO claude_followup_waits VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(repository,backend_name,credential_context,task_id,
                               purpose,work_identity) DO UPDATE SET
                     reason=excluded.reason,
                     observed_at=MAX(observed_at,excluded.observed_at),
                     retry_not_before=MAX(retry_not_before,excluded.retry_not_before),
                     certainty=CASE WHEN certainty=? THEN certainty
                                    ELSE excluded.certainty END,
                     details=excluded.details""",
                (wait.repository, wait.pr_number, wait.backend_name, wait.credential_context, wait.task_id, wait.purpose, wait.work_identity, wait.reason, wait.observed_at, wait.retry_not_before, wait.certainty.value, json.dumps(details), DeliveryCertainty.INDETERMINATE.value),
            )
            self._connection.commit()

    @contextmanager
    def admission(
        self,
        repository: str,
        backend_name: str,
        credential_context: str,
        now: Optional[float] = None,
    ):
        """Own one context recheck and fence it against newer refusals."""
        at = time.time() if now is None else now
        token = uuid.uuid4().hex
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            hold = self.active_hold(repository, backend_name, credential_context, at)
            row = self._connection.execute(
                """SELECT token,lease_until FROM claude_followup_admissions
                   WHERE repository=? AND backend_name=? AND credential_context=?""",
                (repository, backend_name, credential_context),
            ).fetchone()
            if hold is not None:
                self._connection.rollback()
                raise ClaudeFollowupHoldActive(hold)
            if row and float(row[1]) > at:
                self._connection.rollback()
                raise ClaudeFollowupHoldActive(float(row[1]))
            refusal = self._latest_refusal(repository, backend_name, credential_context)
            self._connection.execute(
                "INSERT OR REPLACE INTO claude_followup_admissions VALUES(?,?,?,?,?,?)",
                (repository, backend_name, credential_context, token, refusal, at + 300.0),
            )
            self._connection.commit()
        claim = ClaudeFollowupAdmission(token, repository, backend_name, credential_context, refusal)
        try:
            yield claim
        finally:
            with self._lock:
                self._connection.execute(
                    """DELETE FROM claude_followup_admissions WHERE repository=?
                       AND backend_name=? AND credential_context=? AND token=?""",
                    (repository, backend_name, credential_context, token),
                )
                self._connection.commit()

    def authorize_assignment(self, claim: ClaudeFollowupAdmission) -> bool:
        """Reject a stale positive observation after a newer refusal was retained."""
        with self._lock:
            row = self._connection.execute(
                """SELECT token FROM claude_followup_admissions WHERE repository=?
                   AND backend_name=? AND credential_context=?""",
                (claim.repository, claim.backend_name, claim.credential_context),
            ).fetchone()
            return bool(row and row[0] == claim.token and self._latest_refusal(claim.repository, claim.backend_name, claim.credential_context) <= claim.refusal_epoch)

    def _latest_refusal(self, repository: str, backend_name: str, credential_context: str) -> float:
        row = self._connection.execute(
            """SELECT MAX(observed_at) FROM claude_followup_waits
               WHERE repository=? AND backend_name=? AND credential_context=?""",
            (repository, backend_name, credential_context),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def due_prs(self, repository: str, now: Optional[float] = None) -> tuple[int, ...]:
        """Return PRs with definitely-unsent work whose retained deadline is due."""
        at = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                """SELECT DISTINCT pr_number FROM claude_followup_waits
                   WHERE repository=? AND certainty=? AND retry_not_before<=?
                   ORDER BY pr_number""",
                (repository, DeliveryCertainty.NOT_SENT.value, at),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def next_due_at(self, repository: str) -> Optional[float]:
        with self._lock:
            row = self._connection.execute(
                """SELECT MIN(retry_not_before) FROM claude_followup_waits
                   WHERE repository=? AND certainty=?""",
                (repository, DeliveryCertainty.NOT_SENT.value),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def retire_obsolete_pr_work(self, repository: str, pr_number: int, due_before: float) -> None:
        """Retire only a due unsent incarnation after authoritative evaluation."""
        with self._lock:
            self._connection.execute(
                """DELETE FROM claude_followup_waits WHERE repository=? AND
                   pr_number=? AND certainty=? AND retry_not_before<=?""",
                (repository, pr_number, DeliveryCertainty.NOT_SENT.value, due_before),
            )
            self._connection.commit()

    def retire(self, repository: str, task_id: str, purpose: str, work_identity: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM claude_followup_waits WHERE repository=? AND task_id=? AND purpose=? AND work_identity=? AND certainty=?",
                (repository, task_id, purpose, work_identity, DeliveryCertainty.NOT_SENT.value),
            )
            self._connection.commit()


_stores: dict[str, ClaudeFollowupWaitStore] = {}
_stores_lock = threading.Lock()


def get_claude_followup_wait_store() -> ClaudeFollowupWaitStore:
    path = str(Path(os.environ.get("AUTO_CODER_CLAUDE_FOLLOWUP_DB", "~/.auto-coder/claude-followup-waits.sqlite3")).expanduser())
    with _stores_lock:
        return _stores.setdefault(path, ClaudeFollowupWaitStore(Path(path)))


def wait_from_error(
    error: ClaudeFollowupUsageLimitError,
    pr_number: int,
    task_id: str,
    purpose: str,
    work_identity: str,
) -> ClaudeFollowupWait:
    if not error.repository:
        raise ValueError("Claude quota refusal lacks repository identity")
    return ClaudeFollowupWait(
        error.repository,
        pr_number,
        error.backend_name,
        error.credential_context,
        task_id,
        purpose,
        work_identity,
        error.reason.value,
        error.observed_at,
        error.retry_not_before,
        error.delivery_certainty,
    )
