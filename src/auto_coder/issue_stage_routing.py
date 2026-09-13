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
) -> LaneClassification:
    """Classify implementation independently using exact-current READY evidence."""
    all_ready = all(item.verdict == "READY" for item in requirements)
    return LaneClassification(repository, IMPLEMENTATION_STAGE, target_number, generation, priority, admitted and all_ready)


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
                """
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
                    "UPDATE issue_lane_arrivals SET priority=?, remaining_json=? WHERE repository=? AND stage=? AND target_number=?",
                    (classification.priority, remaining, classification.repository, classification.stage, classification.target_number),
                )
            else:
                self._connection.execute(
                    "DELETE FROM issue_lane_arrivals WHERE repository=? AND stage=? AND target_number=?",
                    (classification.repository, classification.stage, classification.target_number),
                )
                self._connection.execute(
                    "INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,created_at) VALUES(?,?,?,?,?,'pending',?,?)",
                    (classification.repository, classification.stage, classification.target_number, classification.generation, classification.priority, remaining, timestamp),
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
