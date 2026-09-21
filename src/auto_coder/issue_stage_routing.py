"""Durable, generation-aware routing between Issue review and implementation.

This module deliberately does not execute either stage.  It turns an already
authoritative classification into durable lane items that future workers can
claim without relying on the webhook or startup payload which caused the
classification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Sequence

REVIEW_STAGE = "review"
IMPLEMENTATION_STAGE = "implementation"
_STAGES = (REVIEW_STAGE, IMPLEMENTATION_STAGE)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def issue_priority(labels: Sequence[str]) -> int:
    """Return the Issue lane priority without making labels part of identity."""
    names = set(labels)
    if names.intersection({"breaking-change", "breaking", "api-change", "deprecation", "version-major"}):
        return 7
    return 3 if "urgent" in names else 0


@dataclass(frozen=True)
class ContractIdentity:
    """Exact semantic identity of one authoritative GitHub Issue contract."""

    repository: str
    issue_number: int
    issue_id: int
    title: str
    body: str
    role: str

    @property
    def key(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True)
class ReviewRequirement:
    """One enabled, exact validation identity and its current durable status."""

    category: str
    subject_number: int
    identity_key: str
    verdict: Optional[str] = None
    in_flight: bool = False

    def __post_init__(self) -> None:
        if self.category not in {"individual", "decomposition"}:
            raise ValueError("unknown review category")
        if self.verdict not in {None, "READY", "BLOCKED", "ERROR"}:
            raise ValueError("unknown review verdict")

    @property
    def review_needed(self) -> bool:
        return self.verdict not in {"READY", "BLOCKED"}


@dataclass(frozen=True)
class LaneClassification:
    """Authoritative desired state for one target in one lane."""

    repository: str
    stage: str
    target_number: int
    generation: str
    priority: int
    eligible: bool
    remaining_identity_keys: tuple[str, ...] = ()
    family_parent_number: Optional[int] = None

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ValueError("unknown Issue stage")
        if self.target_number <= 0 or self.priority not in {0, 3, 7}:
            raise ValueError("invalid lane classification")


@dataclass(frozen=True)
class PendingLaneItem:
    repository: str
    stage: str
    target_number: int
    generation: str
    priority: int
    arrival: int
    state: str
    remaining_identity_keys: tuple[str, ...]


@dataclass(frozen=True)
class ImplementationRetryRequest:
    """Durable operator authority for one distinct implementation attempt."""

    request_id: str
    repository: str
    target_number: int
    generation: str
    attempt_id: str
    status: str
    ownership_reference: Optional[str] = None
    refusal: Optional[str] = None
    predecessor_captured: bool = False
    predecessor_provider: Optional[str] = None
    predecessor_task_id: Optional[str] = None
    predecessor_backend_name: Optional[str] = None


class RetryRequestConflict(ValueError):
    """Raised when a request identity is reused with different inputs."""


def standalone_review_generation(contract: ContractIdentity, requirements: Sequence[ReviewRequirement]) -> str:
    """Build a standalone generation from its enabled validation identity set."""
    enabled = sorted((item.category, item.subject_number, item.identity_key) for item in requirements)
    return _digest({"kind": "standalone-review", "contract": contract.key, "enabled": enabled})


def family_review_generation(repository: str, parent: ContractIdentity, children: Sequence[ContractIdentity], requirements: Sequence[ReviewRequirement]) -> str:
    """Build a family Review generation; verdicts and member state are excluded."""
    enabled = sorted((item.category, item.subject_number, item.identity_key) for item in requirements)
    members = sorted((child.issue_id, child.issue_number, child.key) for child in children)
    return _digest({"kind": "family-review", "repository": repository, "parent": (parent.issue_id, parent.issue_number, parent.key), "children": members, "enabled": enabled})


def implementation_generation(target: ContractIdentity, family_contract_keys: Sequence[str] = ()) -> str:
    """Build an Implementation generation independent of review and operations."""
    return _digest({"kind": "implementation", "target": target.key, "family": sorted(family_contract_keys)})


def review_classification(
    repository: str,
    target_number: int,
    generation: str,
    priority: int,
    admitted: bool,
    requirements: Sequence[ReviewRequirement],
) -> LaneClassification:
    """Classify review-needed work; terminal and in-flight identities are omitted."""
    remaining = tuple(sorted(item.identity_key for item in requirements if item.review_needed and not item.in_flight))
    return LaneClassification(repository, REVIEW_STAGE, target_number, generation, priority, admitted and bool(remaining), remaining)


def implementation_classification(
    repository: str,
    target_number: int,
    generation: str,
    priority: int,
    admitted: bool,
    requirements: Sequence[ReviewRequirement],
    family_parent_number: Optional[int] = None,
) -> LaneClassification:
    """Classify implementation independently using exact-current READY evidence."""
    all_ready = all(item.verdict == "READY" for item in requirements)
    return LaneClassification(
        repository,
        IMPLEMENTATION_STAGE,
        target_number,
        generation,
        priority,
        admitted and all_ready,
        family_parent_number=family_parent_number,
    )


class IssueStageRoutingStore:
    """SQLite-backed lane arrivals and implementation-start ownership tombstones."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS issue_lane_arrivals (
                    arrival INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository TEXT NOT NULL,
                    stage TEXT NOT NULL CHECK(stage IN ('review', 'implementation')),
                    target_number INTEGER NOT NULL,
                    generation TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority IN (0, 3, 7)),
                    state TEXT NOT NULL CHECK(state IN ('pending', 'deferred', 'starting')),
                    remaining_json TEXT NOT NULL,
                    family_parent_number INTEGER,
                    created_at REAL NOT NULL,
                    UNIQUE(repository, stage, target_number)
                );
                CREATE TABLE IF NOT EXISTS implementation_owned_starts (
                    repository TEXT NOT NULL,
                    target_number INTEGER NOT NULL,
                    generation TEXT NOT NULL,
                    owned_at REAL NOT NULL,
                    PRIMARY KEY(repository, target_number, generation)
                );
                CREATE TABLE IF NOT EXISTS implementation_retry_requests (
                    request_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    target_number INTEGER NOT NULL CHECK(target_number > 0),
                    generation TEXT NOT NULL,
                    attempt_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'owned', 'invalidated')),
                    ownership_reference TEXT,
                    refusal TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS implementation_retry_target
                    ON implementation_retry_requests(repository, target_number);
                """
            )
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(issue_lane_arrivals)")}
            if "family_parent_number" not in columns:
                self._connection.execute("ALTER TABLE issue_lane_arrivals ADD COLUMN family_parent_number INTEGER")
                # The earlier schema cannot prove whether an Implementation
                # row was standalone or belonged to a now-changed family.
                # Fail closed and let startup authority reconstruct it.
                self._connection.execute("DELETE FROM issue_lane_arrivals WHERE stage='implementation'")
            retry_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(implementation_retry_requests)")}
            for name, declaration in (
                ("predecessor_captured", "INTEGER NOT NULL DEFAULT 0"),
                ("predecessor_provider", "TEXT"),
                ("predecessor_task_id", "TEXT"),
                ("predecessor_backend_name", "TEXT"),
            ):
                if name not in retry_columns:
                    self._connection.execute(f"ALTER TABLE implementation_retry_requests ADD COLUMN {name} {declaration}")

    def accept_retry_request(
        self,
        request_id: str,
        repository: str,
        target_number: int,
        generation: str,
        now: Optional[float] = None,
    ) -> ImplementationRetryRequest:
        """Create, or idempotently replay, explicit retry authority.

        The insert and generated attempt identity are one SQLite transaction.
        Consequently an accepted result is never returned before it is durable.
        """
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("repository must be a non-empty string")
        if isinstance(target_number, bool) or not isinstance(target_number, int) or target_number <= 0:
            raise ValueError("target_number must be a positive integer")
        if not isinstance(generation, str) or not generation:
            raise ValueError("generation must be a non-empty exact identity")
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            existing = self._retry_locked(request_id)
            if existing is not None:
                if (existing.repository, existing.target_number, existing.generation) != (repository, target_number, generation):
                    raise RetryRequestConflict(f"retry request {request_id!r} is already bound to different inputs")
                return existing
            self._connection.execute(
                "INSERT INTO implementation_retry_requests(request_id,repository,target_number,generation,attempt_id,status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                (request_id, repository, target_number, generation, uuid.uuid4().hex, timestamp, timestamp),
            )
            result = self._retry_locked(request_id)
            assert result is not None
            return result

    def retry_request(self, request_id: str) -> Optional[ImplementationRetryRequest]:
        """Return one durable retry authorization without changing it."""
        with self._lock:
            return self._retry_locked(request_id)

    def capture_retry_predecessor(
        self,
        request_id: str,
        provider: Optional[str],
        task_id: Optional[str],
        backend_name: Optional[str],
    ) -> ImplementationRetryRequest:
        """Bind the current provider owner to retry admission exactly once."""
        if (provider is None) != (task_id is None):
            raise ValueError("predecessor provider and task must be supplied together")
        with self._lock, self._connection:
            current = self._retry_locked(request_id)
            if current is None:
                raise ValueError(f"unknown retry request {request_id!r}")
            candidate = (provider, task_id, backend_name)
            retained = (current.predecessor_provider, current.predecessor_task_id, current.predecessor_backend_name)
            if current.predecessor_captured:
                if retained != candidate:
                    raise RetryRequestConflict("retry predecessor is immutable")
                return current
            if current.status != "pending":
                raise RetryRequestConflict("retry predecessor was not captured before ownership")
            self._connection.execute(
                "UPDATE implementation_retry_requests SET predecessor_captured=1,predecessor_provider=?,predecessor_task_id=?,predecessor_backend_name=?,updated_at=? WHERE request_id=?",
                (provider, task_id, backend_name, time.time(), request_id),
            )
            return self._require_retry_locked(request_id, current.repository, current.target_number, current.generation)

    def retry_requests(self, repository: Optional[str] = None, target_number: Optional[int] = None) -> tuple[ImplementationRetryRequest, ...]:
        """Enumerate durable retry authority for recovery and diagnostics."""
        clauses: list[str] = []
        values: list[object] = []
        if repository is not None:
            clauses.append("repository=?")
            values.append(repository)
        if target_number is not None:
            clauses.append("target_number=?")
            values.append(target_number)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT request_id,repository,target_number,generation,attempt_id,status,ownership_reference,refusal,predecessor_captured,predecessor_provider,predecessor_task_id,predecessor_backend_name FROM implementation_retry_requests" + where + " ORDER BY created_at,request_id",
                values,
            ).fetchall()
        return tuple(self._decode_retry(row) for row in rows)

    def claim_retry_acquisition(self, request_id: str, repository: str, target_number: int, generation: str, now: Optional[float] = None) -> ImplementationRetryRequest:
        """Linearize authorization before crossing the ownership boundary."""
        del now
        with self._lock:
            return self._require_retry_locked(request_id, repository, target_number, generation)

    def defer_retry_acquisition(self, request_id: str, reason: str, now: Optional[float] = None) -> ImplementationRetryRequest:
        """Return an unperformed claim to pending after operational contention."""
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE implementation_retry_requests SET status='pending',refusal=?,updated_at=? WHERE request_id=? AND status='pending'",
                (reason, timestamp, request_id),
            )
            result = self._retry_locked(request_id)
            if result is None:
                raise ValueError(f"unknown retry request {request_id!r}")
            return result

    def mark_retry_owned(self, request_id: str, ownership_reference: str, now: Optional[float] = None) -> ImplementationRetryRequest:
        """Project a captured slot acquisition into the request record."""
        if not ownership_reference:
            raise ValueError("ownership_reference must be non-empty")
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            record = self._retry_locked(request_id)
            if record is None:
                raise ValueError(f"unknown retry request {request_id!r}")
            if record.status == "owned":
                if record.ownership_reference != ownership_reference:
                    raise RetryRequestConflict("retry request has conflicting ownership references")
                return record
            if record.status == "invalidated":
                return record
            if record.status != "pending":
                raise RetryRequestConflict("retry request is not pending acquisition")
            self._connection.execute(
                "UPDATE implementation_retry_requests SET status='owned',ownership_reference=?,refusal=NULL,updated_at=? WHERE request_id=? AND status='pending'",
                (ownership_reference, timestamp, request_id),
            )
            result = self._retry_locked(request_id)
            assert result is not None
            return result

    def invalidate_retry_request(self, request_id: str, current_generation: str, reason: Optional[str] = None, now: Optional[float] = None) -> ImplementationRetryRequest:
        """Invalidate unconsumed authority on an authoritative generation change."""
        if not current_generation:
            raise ValueError("current_generation must be known and non-empty")
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            record = self._retry_locked(request_id)
            if record is None:
                raise ValueError(f"unknown retry request {request_id!r}")
            if record.generation == current_generation or record.status == "owned":
                return record
            detail = reason or f"current generation changed to {current_generation}"
            self._connection.execute(
                "UPDATE implementation_retry_requests SET status='invalidated',refusal=?,updated_at=? WHERE request_id=? AND status='pending'",
                (detail, timestamp, request_id),
            )
            result = self._retry_locked(request_id)
            assert result is not None
            return result

    def _require_retry_locked(self, request_id: str, repository: str, target_number: int, generation: str) -> ImplementationRetryRequest:
        record = self._retry_locked(request_id)
        if record is None:
            raise ValueError(f"unknown retry request {request_id!r}")
        if (record.repository, record.target_number, record.generation) != (repository, target_number, generation):
            raise RetryRequestConflict("retry request does not authorize these inputs")
        return record

    def _retry_locked(self, request_id: str) -> Optional[ImplementationRetryRequest]:
        row = self._connection.execute(
            "SELECT request_id,repository,target_number,generation,attempt_id,status,ownership_reference,refusal,predecessor_captured,predecessor_provider,predecessor_task_id,predecessor_backend_name FROM implementation_retry_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        return self._decode_retry(row) if row is not None else None

    @staticmethod
    def _decode_retry(row: tuple[object, ...]) -> ImplementationRetryRequest:
        request_id, repository, target_number, generation, attempt_id, status, reference, refusal, predecessor_captured, predecessor_provider, predecessor_task_id, predecessor_backend_name = row
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(repository, str)
            or not repository
            or isinstance(target_number, bool)
            or not isinstance(target_number, int)
            or target_number <= 0
            or not isinstance(generation, str)
            or not generation
            or not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(status, str)
            or status not in {"pending", "owned", "invalidated"}
            or (reference is not None and (not isinstance(reference, str) or not reference))
            or (refusal is not None and not isinstance(refusal, str))
        ):
            raise ValueError("invalid durable implementation retry request")
        return ImplementationRetryRequest(
            request_id,
            repository,
            target_number,
            generation,
            attempt_id,
            status,
            reference,
            refusal,
            bool(predecessor_captured),
            str(predecessor_provider) if predecessor_provider is not None else None,
            str(predecessor_task_id) if predecessor_task_id is not None else None,
            str(predecessor_backend_name) if predecessor_backend_name is not None else None,
        )

    def reconcile(self, classification: LaneClassification, now: Optional[float] = None) -> Optional[PendingLaneItem]:
        """Atomically replace superseded work or update priority/work in place."""
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT generation FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=?",
                (classification.repository, classification.stage, classification.target_number),
            ).fetchone()
            owned = classification.stage == IMPLEMENTATION_STAGE and self._is_owned_locked(classification.repository, classification.target_number, classification.generation)
            if not classification.eligible or owned:
                self._connection.execute(
                    "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=?",
                    (classification.repository, classification.stage, classification.target_number),
                )
                return None
            remaining = json.dumps(list(classification.remaining_identity_keys), separators=(",", ":"))
            if row is not None and row[0] == classification.generation:
                self._connection.execute(
                    "UPDATE issue_lane_arrivals SET priority=?, remaining_json=?, family_parent_number=? WHERE repository=? AND stage=? AND target_number=?",
                    (
                        classification.priority,
                        remaining,
                        classification.family_parent_number,
                        classification.repository,
                        classification.stage,
                        classification.target_number,
                    ),
                )
            else:
                self._connection.execute(
                    "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=?",
                    (classification.repository, classification.stage, classification.target_number),
                )
                self._connection.execute(
                    "INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'pending',?,?,?)",
                    (
                        classification.repository,
                        classification.stage,
                        classification.target_number,
                        classification.generation,
                        classification.priority,
                        remaining,
                        classification.family_parent_number,
                        timestamp,
                    ),
                )
            return self._get_locked(classification.repository, classification.stage, classification.target_number)

    def pending(self, repository: str, stage: str) -> tuple[PendingLaneItem, ...]:
        """Return runnable work by descending priority and durable FIFO arrival."""
        if stage not in _STAGES:
            raise ValueError("unknown Issue stage")
        with self._lock:
            rows = self._connection.execute(
                "SELECT repository,stage,target_number,generation,priority,arrival,state,remaining_json FROM issue_lane_arrivals WHERE repository=? AND stage=? AND state='pending' ORDER BY priority DESC, arrival ASC",
                (repository, stage),
            ).fetchall()
            return tuple(self._decode(row) for row in rows)

    def remove(self, repository: str, stage: str, target_number: int) -> None:
        """Remove semantically ineligible or role-superseded pending work."""
        if stage not in _STAGES:
            raise ValueError("unknown Issue stage")
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=?",
                (repository, stage, target_number),
            )

    def remove_generation(self, repository: str, stage: str, target_number: int, generation: str) -> bool:
        """Remove one pending lane item only while its generation is still current."""
        if stage not in _STAGES:
            raise ValueError("unknown Issue stage")
        with self._lock, self._connection:
            changed = self._connection.execute(
                "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=? AND generation=?",
                (repository, stage, target_number, generation),
            ).rowcount
            return changed > 0

    def get(self, repository: str, stage: str, target_number: int) -> Optional[PendingLaneItem]:
        """Return the current pending lane item for one target, if any."""
        if stage not in _STAGES:
            raise ValueError("unknown Issue stage")
        with self._lock:
            return self._get_locked(repository, stage, target_number)

    def remove_target(self, repository: str, target_number: int) -> None:
        """Remove every pending lane role for an authoritatively absent target."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM issue_lane_arrivals WHERE repository=? AND target_number=?",
                (repository, target_number),
            )

    def targets(self, repository: str) -> tuple[int, ...]:
        """Return durable target identities which startup must re-authorize."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT DISTINCT target_number FROM issue_lane_arrivals WHERE repository=? ORDER BY target_number",
                (repository,),
            ).fetchall()
            return tuple(int(row[0]) for row in rows)

    def remove_departed_family_children(self, repository: str, parent_number: int, current_children: Sequence[int]) -> None:
        """Supersede Implementation work owned by an older family membership."""
        retained = tuple(current_children)
        with self._lock, self._connection:
            if retained:
                placeholders = ",".join("?" for _value in retained)
                self._connection.execute(
                    f"DELETE FROM issue_lane_arrivals WHERE repository=? AND stage='implementation' AND family_parent_number=? AND target_number NOT IN ({placeholders})",
                    (repository, parent_number, *retained),
                )
            else:
                self._connection.execute(
                    "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage='implementation' AND family_parent_number=?",
                    (repository, parent_number),
                )

    def begin(self, item: PendingLaneItem) -> bool:
        """Coalesce a start attempt for the exact still-current lane generation."""
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE issue_lane_arrivals SET state='starting' WHERE repository=? AND stage=? AND target_number=? AND generation=? AND state='pending'",
                (item.repository, item.stage, item.target_number, item.generation),
            ).rowcount
            return changed == 1

    def defer(self, item: PendingLaneItem) -> bool:
        """Return an operationally deferred start to the same arrival."""
        with self._lock, self._connection:
            changed = self._connection.execute(
                "UPDATE issue_lane_arrivals SET state='pending' WHERE repository=? AND stage=? AND target_number=? AND generation=? AND state IN ('starting','deferred')",
                (item.repository, item.stage, item.target_number, item.generation),
            ).rowcount
            return changed == 1

    def mark_implementation_owned(self, item: PendingLaneItem, now: Optional[float] = None) -> bool:
        """Persist the no-repeat boundary before removing an Implementation item."""
        if item.stage != IMPLEMENTATION_STAGE:
            raise ValueError("only Implementation work can acquire start ownership")
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            current = self._connection.execute(
                "SELECT state,generation FROM issue_lane_arrivals WHERE repository=? AND stage='implementation' AND target_number=?",
                (item.repository, item.target_number),
            ).fetchone()
            if current != ("starting", item.generation):
                return False
            self._connection.execute(
                "INSERT OR IGNORE INTO implementation_owned_starts(repository,target_number,generation,owned_at) VALUES(?,?,?,?)",
                (item.repository, item.target_number, item.generation, timestamp),
            )
            self._connection.execute(
                "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage='implementation' AND target_number=? AND generation=?",
                (item.repository, item.target_number, item.generation),
            )
            return True

    def record_implementation_owned(self, repository: str, target_number: int, generation: str, now: Optional[float] = None) -> None:
        """Durably tombstone a generation whose production start was independently proven.

        Unlike :meth:`mark_implementation_owned`, this does not require a
        durable ``starting`` arrival for *generation*: it is the entry point
        used by the production ownership adapter (``implementation_ownership.py``,
        #2061) that binds this tombstone to real ``ImplementationSlotRepository``
        acquisition on the current dispatch path, ahead of the dedicated
        Implementation worker's own ``begin()``/``starting`` lifecycle (#2055).
        Idempotent: recording an already-owned generation is a no-op beyond
        clearing any pending/starting arrival still tracking it.
        """
        timestamp = time.time() if now is None else now
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO implementation_owned_starts(repository,target_number,generation,owned_at) VALUES(?,?,?,?)",
                (repository, target_number, generation, timestamp),
            )
            self._connection.execute(
                "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage='implementation' AND target_number=? AND generation=?",
                (repository, target_number, generation),
            )

    def recover(self, repository: str) -> None:
        """Make pre-ownership attempts retryable while retaining owned tombstones."""
        with self._lock, self._connection:
            self._connection.execute("UPDATE issue_lane_arrivals SET state='pending' WHERE repository=? AND state IN ('starting','deferred')", (repository,))

    def is_implementation_owned(self, repository: str, target_number: int, generation: str) -> bool:
        with self._lock:
            return self._is_owned_locked(repository, target_number, generation)

    def _is_owned_locked(self, repository: str, target_number: int, generation: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM implementation_owned_starts WHERE repository=? AND target_number=? AND generation=?",
                (repository, target_number, generation),
            ).fetchone()
            is not None
        )

    def _get_locked(self, repository: str, stage: str, target_number: int) -> Optional[PendingLaneItem]:
        row = self._connection.execute(
            "SELECT repository,stage,target_number,generation,priority,arrival,state,remaining_json FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=?",
            (repository, stage, target_number),
        ).fetchone()
        return self._decode(row) if row is not None else None

    @staticmethod
    def _decode(row: tuple[object, ...]) -> PendingLaneItem:
        remaining = json.loads(str(row[7]))
        if not isinstance(remaining, list) or any(not isinstance(value, str) for value in remaining):
            raise ValueError("invalid durable lane identity set")
        return PendingLaneItem(
            str(row[0]),
            str(row[1]),
            int(str(row[2])),
            str(row[3]),
            int(str(row[4])),
            int(str(row[5])),
            str(row[6]),
            tuple(remaining),
        )
