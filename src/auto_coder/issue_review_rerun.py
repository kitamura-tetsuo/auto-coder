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
from typing import Callable, Iterable, Optional


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

    def active_subjects(self, repository: str) -> tuple[SubjectRerunStatus, ...]:
        """Return reconstructible current work for one repository."""
        normalized_repository = repository.strip().lower()
        try:
            with self._connect() as db:
                rows = db.execute(
                    """SELECT repository, kind, issue_number, request_id, authority,
                       state, reason, decision_reference, evaluation_source
                       FROM rerun_subjects
                       WHERE repository=? AND state IN ('pending', 'deferred')
                       ORDER BY authority, subject_key""",
                    (normalized_repository,),
                ).fetchall()
            return tuple(
                SubjectRerunStatus(
                    ReviewSubject(row[0], row[1], int(row[2])),
                    str(row[3]),
                    int(row[4]),
                    str(row[5]),
                    row[6],
                    row[7],
                    row[8],
                )
                for row in rows
            )
        except (OSError, sqlite3.Error) as exc:
            raise RerunAuthorityUnavailable(str(exc)) from exc

    def execution_key(self, subject: ReviewSubject, semantic_identity: str) -> str:
        """Key scheduler coalescing to both semantic and current rerun authority."""
        authority, _request_id, _state = self.authority(subject)
        return f"{semantic_identity}:rerun-authority:{authority}"


Admission = Callable[[ReviewSubject], Optional[str]]


class IssueReviewRerunOperation:
    """Accept and durably materialize review-only rerun work.

    ``admit`` returns ``None`` only after current authoritative admission has
    created reconstructible Review-lane work.  Otherwise it returns the
    concrete deferral reason persisted on the request.  Since acceptance is
    committed first, a crash before this pass is recovered by ``recover``.
    """

    def __init__(self, store: IssueReviewRerunStore) -> None:
        self.store = store

    def accept(self, request_id: str, subjects: Iterable[ReviewSubject], admit: Admission) -> tuple[SubjectRerunStatus, ...]:
        selected = tuple(subjects)
        if len({subject.repository.strip().lower() for subject in selected}) > 1:
            raise ValueError("one rerun request must target exactly one repository")
        accepted = self.store.accept(request_id, selected)
        self._admit_current(accepted, admit)
        return self.store.status(request_id)

    def recover(self, repository: str, admit: Admission) -> tuple[SubjectRerunStatus, ...]:
        active = self.store.active_subjects(repository)
        self._admit_current(active, admit)
        return self.store.active_subjects(repository)

    def _admit_current(self, statuses: Iterable[SubjectRerunStatus], admit: Admission) -> None:
        for status in statuses:
            authority, request_id, state = self.store.authority(status.subject)
            if authority != status.authority or request_id != status.request_id or state not in {"pending", "deferred"}:
                continue
            try:
                reason = admit(status.subject)
            except Exception as exc:
                reason = f"authoritative review admission unavailable: {type(exc).__name__}: {exc}"
            if reason:
                self.store.defer(status.subject, status.authority, reason)
