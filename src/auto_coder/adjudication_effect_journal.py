import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass(frozen=True)
class AdjudicationEffectRecord:
    repository: str
    pr_number: int
    context_id: str
    decision_id: str
    finding_identity: str
    effect_kind: str  # 'resolve', 'unresolve', 'codex_task', 'cached_verdict', 'test_oracle_gap'
    generation: str
    status: str  # 'pending', 'confirmed', 'unknown', 'definitely-not-sent'


class AdjudicationEffectJournal:
    """Durably tracks the delivery and state of side-effects caused by adjudications (REQ-008)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS adjudication_effects (
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                context_id TEXT NOT NULL,
                decision_id TEXT NOT NULL,
                finding_identity TEXT NOT NULL,
                effect_kind TEXT NOT NULL,
                generation TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY(repository, pr_number, context_id, decision_id, finding_identity, effect_kind)
            )"""
        )
        self._db.commit()

    def record_effect(self, repository: str, pr_number: int, context_id: str, decision_id: str, finding_identity: str, effect_kind: str, generation: str, status: str) -> None:
        if status not in {"pending", "confirmed", "unknown", "definitely-not-sent"}:
            raise ValueError(f"invalid effect status: {status}")

        with self._lock:
            self._db.execute(
                """INSERT OR REPLACE INTO adjudication_effects
                   (repository, pr_number, context_id, decision_id, finding_identity, effect_kind, generation, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (repository, pr_number, context_id, decision_id, finding_identity, effect_kind, generation, status),
            )
            self._db.commit()

    def get_effects_for_pr(self, repository: str, pr_number: int) -> list[AdjudicationEffectRecord]:
        with self._lock:
            rows = self._db.execute("SELECT repository, pr_number, context_id, decision_id, finding_identity, effect_kind, generation, status " "FROM adjudication_effects WHERE repository = ? AND pr_number = ?", (repository, pr_number)).fetchall()
            return [AdjudicationEffectRecord(*row) for row in rows]

    def get_effect(self, repository: str, pr_number: int, context_id: str, decision_id: str, finding_identity: str, effect_kind: str) -> Optional[AdjudicationEffectRecord]:
        with self._lock:
            row = self._db.execute(
                "SELECT repository, pr_number, context_id, decision_id, finding_identity, effect_kind, generation, status " "FROM adjudication_effects WHERE repository = ? AND pr_number = ? AND context_id = ? AND decision_id = ? AND finding_identity = ? AND effect_kind = ?",
                (repository, pr_number, context_id, decision_id, finding_identity, effect_kind),
            ).fetchone()
            if row:
                return AdjudicationEffectRecord(*row)
            return None
