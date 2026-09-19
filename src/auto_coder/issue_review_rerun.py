"""Durable authority for explicit Issue-review rerun requests.

The journal is deliberately separate from semantic decision stores.  A rerun
changes the authority of an occurrence, not the review policy identity, and a
single SQLite transaction makes a multi-subject request effective before the
caller is told that it was accepted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


class RerunAuthorityUnavailable(RuntimeError):
    """The revocation journal could not be read or durably updated."""


@dataclass(frozen=True, order=True)
class ReviewSubject:
    repository: str
    kind: str
    issue_number: int

    def __post_init__(self) -> None:
        if not self.repository.strip() or self.kind not in {"individual", "decomposition"}:
            raise ValueError("invalid review subject")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("review subject Issue number must be positive")

    @property
    def key(self) -> str:
        return f"{self.repository.strip().lower()}:{self.kind}:{self.issue_number}"


@dataclass(frozen=True)
class SubjectRerunStatus:
    subject: ReviewSubject
    request_id: str
    authority: int
    state: str
    reason: Optional[str] = None
    decision_reference: Optional[str] = None
    evaluation_source: Optional[str] = None


class IssueReviewRerunStore:
    """Transactional rerun request and per-stable-subject authority journal."""

    def __init__(self, path: Optional[Path] = None) -> None:
        root = Path(os.environ.get("AUTO_CODER_SPECIFICATION_VALIDATION_ROOT", Path.home() / ".auto-coder"))
        self.path = path or root / "issue_review_reruns.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS rerun_requests (
                        request_id TEXT PRIMARY KEY, normalized_subjects TEXT NOT NULL,
                        accepted_sequence INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS rerun_subjects (
                        subject_key TEXT PRIMARY KEY, repository TEXT NOT NULL,
                        kind TEXT NOT NULL, issue_number INTEGER NOT NULL,
                        request_id TEXT NOT NULL, authority INTEGER NOT NULL,
                        state TEXT NOT NULL, reason TEXT, decision_reference TEXT,
                        evaluation_source TEXT
                    );
                    CREATE TABLE IF NOT EXISTS rerun_request_subjects (
                        request_id TEXT NOT NULL, subject_key TEXT NOT NULL,
                        authority INTEGER NOT NULL, PRIMARY KEY(request_id, subject_key)
                    );
                    """
                )
        except (OSError, sqlite3.Error) as exc:
            raise RerunAuthorityUnavailable(str(exc)) from exc

    @staticmethod
    def _normalized(subjects: Iterable[ReviewSubject]) -> tuple[ReviewSubject, ...]:
        normalized = tuple(sorted(set(subjects), key=lambda item: item.key))
        if not normalized:
            raise ValueError("at least one review subject is required")
        return normalized

    def accept(self, request_id: str, subjects: Iterable[ReviewSubject]) -> tuple[SubjectRerunStatus, ...]:
        """Atomically accept, replay, or reject one exact finite subject set."""
        if not request_id.strip():
            raise ValueError("request identifier must be nonempty")
        normalized = self._normalized(subjects)
        encoded = json.dumps([item.key for item in normalized], separators=(",", ":"))
        try:
            with self._lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT normalized_subjects FROM rerun_requests WHERE request_id=?", (request_id,)).fetchone()
                if existing is not None:
                    if existing[0] != encoded:
                        db.rollback()
                        raise ValueError("rerun request identifier is already bound to a different subject set")
                    db.commit()
                    return self.status(request_id)
                sequence = int(db.execute("SELECT COALESCE(MAX(accepted_sequence), 0) + 1 FROM rerun_requests").fetchone()[0])
                db.execute("INSERT INTO rerun_requests VALUES(?,?,?)", (request_id, encoded, sequence))
                for subject in normalized:
                    previous = db.execute("SELECT request_id FROM rerun_subjects WHERE subject_key=?", (subject.key,)).fetchone()
                    if previous is not None:
                        db.execute("UPDATE rerun_subjects SET state='superseded' WHERE subject_key=?", (subject.key,))
                    db.execute(
                        "INSERT OR REPLACE INTO rerun_subjects VALUES(?,?,?,?,?,?, 'pending', NULL, NULL, NULL)",
                        (subject.key, subject.repository.strip().lower(), subject.kind, subject.issue_number, request_id, sequence),
                    )
                    db.execute("INSERT INTO rerun_request_subjects VALUES(?,?,?)", (request_id, subject.key, sequence))
                db.commit()
            return self.status(request_id)
        except ValueError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise RerunAuthorityUnavailable(str(exc)) from exc

    def authority(self, subject: ReviewSubject) -> tuple[int, Optional[str], str]:
        """Return ``(authority, request_id, state)``; failures never mean no reset."""
        try:
            with self._connect() as db:
                row = db.execute("SELECT authority, request_id, state FROM rerun_subjects WHERE subject_key=?", (subject.key,)).fetchone()
            return (0, None, "none") if row is None else (int(row[0]), str(row[1]), str(row[2]))
        except (OSError, sqlite3.Error) as exc:
            raise RerunAuthorityUnavailable(str(exc)) from exc

    def defer(self, subject: ReviewSubject, authority: int, reason: str) -> bool:
        return self._transition(subject, authority, "deferred", reason, None, None)

    def satisfy(self, subject: ReviewSubject, authority: int, decision_reference: str, evaluation_source: str) -> bool:
        if evaluation_source not in {"model", "local-only"}:
            raise ValueError("a rerun may only be satisfied by a fresh model or local-only evaluation")
        return self._transition(subject, authority, "satisfied", None, decision_reference, evaluation_source)

    def _transition(self, subject: ReviewSubject, authority: int, state: str, reason: Optional[str], reference: Optional[str], source: Optional[str]) -> bool:
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "UPDATE rerun_subjects SET state=?, reason=?, decision_reference=?, evaluation_source=? WHERE subject_key=? AND authority=?",
                    (state, reason, reference, source, subject.key, authority),
                )
                return cursor.rowcount == 1
        except (OSError, sqlite3.Error) as exc:
            raise RerunAuthorityUnavailable(str(exc)) from exc

    def status(self, request_id: str) -> tuple[SubjectRerunStatus, ...]:
        try:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT rs.repository, rs.kind, rs.issue_number, rqs.authority,
                       CASE WHEN rs.request_id=rqs.request_id THEN rs.state ELSE 'superseded' END,
                       rs.reason, rs.decision_reference, rs.evaluation_source
                       FROM rerun_request_subjects rqs JOIN rerun_subjects rs USING(subject_key)
                       WHERE rqs.request_id=? ORDER BY rqs.subject_key""",
                    (request_id,),
                ).fetchall()
            return tuple(SubjectRerunStatus(ReviewSubject(row[0], row[1], int(row[2])), request_id, int(row[3]), row[4], row[5], row[6], row[7]) for row in rows)
        except (OSError, sqlite3.Error) as exc:
            raise RerunAuthorityUnavailable(str(exc)) from exc

    def execution_key(self, subject: ReviewSubject, semantic_identity: str) -> str:
        """Key scheduler coalescing to both semantic and current rerun authority."""
        authority, _request_id, _state = self.authority(subject)
        return f"{semantic_identity}:rerun-authority:{authority}"
