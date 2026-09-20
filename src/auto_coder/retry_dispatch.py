"""Durable, request-scoped ownership of explicit retry dispatch effects.

The retry authorization store decides *whether* an operator request may own an
implementation attempt.  This module starts at the next boundary: it makes the
one external creation allowed by that owned attempt recoverable and
idempotent.  In particular, an absent provider receipt is never treated as
proof that a claimed creation did not happen.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .issue_stage_routing import ImplementationRetryRequest


class RetryDispatchConflict(RuntimeError):
    """Raised when durable dispatch identity contradicts the requested use."""


@dataclass(frozen=True)
class RetryHandoff:
    repository: str
    issue_number: int
    request_id: str
    attempt_id: str
    generation: str
    route: str
    backend_name: str
    creation_id: str
    outcome: str
    route_config: str
    numeric_attempt: Optional[int] = None
    external_id: Optional[str] = None
    external_url: Optional[str] = None
    diagnostic: Optional[str] = None
    tracking_complete: bool = False

    @property
    def suppresses_creation(self) -> bool:
        return self.outcome in {"claimed", "accepted", "indeterminate", "completed"}


class RetryDispatchRepository:
    """SQLite journal for one creation effect per owned retry attempt."""

    def __init__(self, repository: str, path: Optional[Path] = None) -> None:
        if not repository:
            raise ValueError("repository must be non-empty")
        self.repository = repository
        self.path = path or Path.home() / ".auto-coder" / repository / "retry_dispatch.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # WAL setup and schema migration need serialization of their own:
        # SQLite's busy handler does not reliably wait when two fresh
        # connections race to change journal mode.  The advisory lock is only
        # for construction; normal claims continue to serialize in SQLite.
        initialization_lock = self.path.with_suffix(f"{self.path.suffix}.init.lock")
        with open(initialization_lock, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            with self._connection:
                self._connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS retry_handoffs (
                    request_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    attempt_id TEXT NOT NULL UNIQUE,
                    generation TEXT NOT NULL,
                    route TEXT NOT NULL,
                    backend_name TEXT NOT NULL,
                    creation_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN
                        ('claimed','definitely-not-started','accepted','indeterminate','completed')),
                    route_config TEXT NOT NULL,
                    numeric_attempt INTEGER,
                    external_id TEXT,
                    external_url TEXT,
                    diagnostic TEXT,
                    tracking_complete INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS retry_handoff_issue_attempt
                    ON retry_handoffs(repository, issue_number, numeric_attempt)
                    WHERE numeric_attempt IS NOT NULL;
                """
                )

    @staticmethod
    def _validate_authority(authority: ImplementationRetryRequest, repository: str, issue_number: int) -> None:
        if authority.status != "owned" or not authority.ownership_reference:
            raise RetryDispatchConflict("retry authority has not acquired real implementation ownership")
        if authority.repository != repository or authority.target_number != issue_number:
            raise RetryDispatchConflict("retry authority does not match the dispatch target")
        if not authority.request_id or not authority.attempt_id or not authority.generation:
            raise RetryDispatchConflict("retry authority identity is incomplete")

    @staticmethod
    def _safe_config(config: Mapping[str, str]) -> str:
        forbidden = ("token", "secret", "password", "credential", "authorization", "api_key")
        clean: dict[str, str] = {}
        for key, value in config.items():
            if any(part in key.lower() for part in forbidden):
                raise ValueError(f"route configuration must not persist credentials ({key})")
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("route configuration keys and values must be strings")
            clean[key] = value
        return json.dumps(clean, sort_keys=True, separators=(",", ":"))

    def claim(
        self,
        authority: ImplementationRetryRequest,
        route: str,
        backend_name: str,
        route_config: Mapping[str, str],
    ) -> tuple[RetryHandoff, bool]:
        """Claim the external creation before calling a process/provider.

        ``claimed`` deliberately means indeterminate after a crash.  Only an
        explicit ``definitely-not-started`` result permits the same R/A to
        reacquire; it retains the original creation identity.
        """
        self._validate_authority(authority, self.repository, authority.target_number)
        if not route or not backend_name:
            raise ValueError("route and backend_name must be non-empty")
        encoded = self._safe_config(route_config)
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._get_locked(authority.request_id)
            if existing is not None:
                expected = (self.repository, authority.target_number, authority.attempt_id, authority.generation, route, backend_name, encoded)
                actual = (existing.repository, existing.issue_number, existing.attempt_id, existing.generation, existing.route, existing.backend_name, existing.route_config)
                if actual != expected:
                    raise RetryDispatchConflict("retry handoff is already bound to different dispatch inputs")
                if existing.outcome == "definitely-not-started":
                    self._connection.execute(
                        "UPDATE retry_handoffs SET outcome='claimed', diagnostic=NULL, updated_at=? WHERE request_id=? AND outcome='definitely-not-started'",
                        (time.time(), authority.request_id),
                    )
                    refreshed = self._get_locked(authority.request_id)
                    assert refreshed is not None
                    return refreshed, True
                return existing, False
            self._connection.execute(
                "INSERT INTO retry_handoffs(repository,issue_number,request_id,attempt_id,generation,route,backend_name,creation_id,outcome,route_config,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.repository, authority.target_number, authority.request_id, authority.attempt_id, authority.generation, route, backend_name, uuid.uuid4().hex, "claimed", encoded, time.time()),
            )
            created = self._get_locked(authority.request_id)
            assert created is not None
            return created, True

    def allocate_numeric_attempt(self, request_id: str, observed_attempts: list[int]) -> RetryHandoff:
        """Bind a number once, strictly above all available authoritative evidence."""
        if not observed_attempts or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in observed_attempts):
            raise ValueError("authoritative numeric attempt evidence is required")
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            handoff = self._require_locked(request_id)
            if handoff.numeric_attempt is not None:
                return handoff
            rows = self._connection.execute(
                "SELECT numeric_attempt FROM retry_handoffs WHERE repository=? AND issue_number=? AND numeric_attempt IS NOT NULL",
                (self.repository, handoff.issue_number),
            ).fetchall()
            retained = [int(row[0]) for row in rows]
            allocated = max(observed_attempts + retained) + 1
            self._connection.execute("UPDATE retry_handoffs SET numeric_attempt=?,updated_at=? WHERE request_id=?", (allocated, time.time(), request_id))
            return self._require_locked(request_id)

    def record_outcome(
        self,
        request_id: str,
        outcome: str,
        *,
        external_id: Optional[str] = None,
        external_url: Optional[str] = None,
        diagnostic: Optional[str] = None,
        tracking_complete: bool = False,
    ) -> RetryHandoff:
        if outcome not in {"definitely-not-started", "accepted", "indeterminate", "completed"}:
            raise ValueError("invalid retry dispatch outcome")
        if outcome in {"accepted", "completed"} and not external_id:
            raise ValueError("accepted work requires an external identity")
        with self._lock, self._connection:
            current = self._require_locked(request_id)
            if current.outcome in {"accepted", "completed"}:
                if (current.external_id, current.external_url) != (external_id or current.external_id, external_url or current.external_url):
                    raise RetryDispatchConflict("accepted retry receipt is immutable")
                if outcome == "definitely-not-started":
                    raise RetryDispatchConflict("accepted work cannot become unsent")
            self._connection.execute(
                "UPDATE retry_handoffs SET outcome=?,external_id=COALESCE(?,external_id),external_url=COALESCE(?,external_url),diagnostic=?,tracking_complete=?,updated_at=? WHERE request_id=?",
                (outcome, external_id, external_url, diagnostic, int(tracking_complete), time.time(), request_id),
            )
            return self._require_locked(request_id)

    def mark_tracking_complete(self, request_id: str) -> RetryHandoff:
        with self._lock, self._connection:
            current = self._require_locked(request_id)
            if current.outcome not in {"accepted", "completed"} or not current.external_id:
                raise RetryDispatchConflict("only accepted work can complete tracking")
            self._connection.execute("UPDATE retry_handoffs SET tracking_complete=1,updated_at=? WHERE request_id=?", (time.time(), request_id))
            return self._require_locked(request_id)

    def get(self, request_id: str) -> Optional[RetryHandoff]:
        with self._lock:
            return self._get_locked(request_id)

    def list_for_issue(self, issue_number: int) -> tuple[RetryHandoff, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT repository,issue_number,request_id,attempt_id,generation,route,backend_name,creation_id,outcome,route_config,numeric_attempt,external_id,external_url,diagnostic,tracking_complete FROM retry_handoffs WHERE repository=? AND issue_number=? ORDER BY updated_at,request_id",
                (self.repository, issue_number),
            ).fetchall()
        return tuple(self._decode(row) for row in rows)

    def _require_locked(self, request_id: str) -> RetryHandoff:
        result = self._get_locked(request_id)
        if result is None:
            raise ValueError(f"unknown retry handoff {request_id!r}")
        return result

    def _get_locked(self, request_id: str) -> Optional[RetryHandoff]:
        row = self._connection.execute(
            "SELECT repository,issue_number,request_id,attempt_id,generation,route,backend_name,creation_id,outcome,route_config,numeric_attempt,external_id,external_url,diagnostic,tracking_complete FROM retry_handoffs WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row: tuple[object, ...]) -> RetryHandoff:
        issue_number = row[1]
        numeric_attempt = row[10]
        if isinstance(issue_number, bool) or not isinstance(issue_number, int):
            raise ValueError("stored retry handoff Issue number is invalid")
        if numeric_attempt is not None and (isinstance(numeric_attempt, bool) or not isinstance(numeric_attempt, int)):
            raise ValueError("stored retry handoff numeric attempt is invalid")
        return RetryHandoff(
            repository=str(row[0]),
            issue_number=issue_number,
            request_id=str(row[2]),
            attempt_id=str(row[3]),
            generation=str(row[4]),
            route=str(row[5]),
            backend_name=str(row[6]),
            creation_id=str(row[7]),
            outcome=str(row[8]),
            route_config=str(row[9]),
            numeric_attempt=numeric_attempt,
            external_id=str(row[11]) if row[11] is not None else None,
            external_url=str(row[12]) if row[12] is not None else None,
            diagnostic=str(row[13]) if row[13] is not None else None,
            tracking_complete=bool(row[14]),
        )
