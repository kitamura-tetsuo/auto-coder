"""Fail-closed lifecycle fence for PRs owned by Jules competitions.

The observation adapter is the authority for membership and winner identity.  This
module turns that read-only decision into an ingress decision and keeps PR cleanup
as a durable, independently replayable effect.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, cast

from .jules_candidate_observation import ArtifactClassification, ClassificationResult


class SpeculativeClassifier(Protocol):
    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int) -> ClassificationResult: ...


class CleanupGitHubClient(Protocol):
    def get_pull_request_metadata_strict(self, repository: str, pr_number: int) -> Mapping[str, object]: ...

    def close_pr(self, repository: str, pr_number: int, comment: str) -> None: ...


@dataclass(frozen=True)
class CleanupObligation:
    repository: str
    issue_number: int
    generation_id: str
    candidate_id: str
    pr_number: int
    selected_pr: Optional[int]
    reason: str


@dataclass(frozen=True)
class LifecycleDecision:
    classification: ArtifactClassification = ArtifactClassification.LEGACY
    allow_ordinary_processing: bool = True
    cleanup_pending: bool = False
    reason: str = ""


class SpeculativeCleanupStore:
    """Durable close obligations; retirement is recorded before GitHub mutation."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS cleanup (
                repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
                generation_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                pr_number INTEGER NOT NULL, selected_pr INTEGER,
                status TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(repository, generation_id, candidate_id, pr_number))"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def retain(self, repository: str, issue: int, result: ClassificationResult, pr_number: int, selected_pr: Optional[int]) -> None:
        reason = f"retired Jules competitor in generation {result.generation_id}"
        if selected_pr is not None:
            reason += f"; selected PR is #{selected_pr}"
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO cleanup VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)",
                (repository, issue, result.generation_id or "", result.candidate_id or "", pr_number, selected_pr, reason),
            )

    def complete(self, repository: str, generation: str, candidate: str, pr_number: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE cleanup SET status='COMPLETE' WHERE repository=? AND generation_id=? AND candidate_id=? AND pr_number=?",
                (repository, generation, candidate, pr_number),
            )

    def pending(self) -> tuple[CleanupObligation, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT repository, issue_number, generation_id, candidate_id, pr_number, selected_pr, reason FROM cleanup WHERE status='PENDING'").fetchall()
        return tuple(CleanupObligation(str(row[0]), int(row[1]), str(row[2]), str(row[3]), int(row[4]), cast(Optional[int], row[5]), str(row[6])) for row in rows)


class SpeculativeJulesLifecycle:
    def __init__(self, classifier: SpeculativeClassifier, cleanup_store: SpeculativeCleanupStore):
        self.classifier = classifier
        self.cleanup_store = cleanup_store

    def evaluate_pr(self, repository: str, issue_number: int, pr_number: int) -> LifecycleDecision:
        result = self.classifier.classify(repository, issue_number, repository, pr_number)
        if result.classification is ArtifactClassification.LEGACY:
            return LifecycleDecision()
        if result.classification is ArtifactClassification.SELECTED:
            return LifecycleDecision(result.classification, True)
        if result.classification is ArtifactClassification.RETIRED:
            self.cleanup_store.retain(repository, issue_number, result, pr_number, None)
            return LifecycleDecision(result.classification, False, True, "verified retired Jules competitor")
        # Active candidates use the competition evaluator; suspected/conflicting
        # artifacts must be retried after authoritative evidence becomes available.
        return LifecycleDecision(result.classification, False, False, "; ".join(result.diagnostics) or "candidate-aware evaluation required")

    def consume_cleanup(self, github_client: CleanupGitHubClient) -> int:
        """Attempt every due close, rechecking authority at the outbound boundary."""
        completed = 0
        for obligation in self.cleanup_store.pending():
            current = self.classifier.classify(obligation.repository, obligation.issue_number, obligation.repository, obligation.pr_number)
            if current.classification is not ArtifactClassification.RETIRED or current.generation_id != obligation.generation_id or current.candidate_id != obligation.candidate_id:
                continue
            pr = github_client.get_pull_request_metadata_strict(obligation.repository, obligation.pr_number)
            state = pr.get("state")
            merged = bool(pr.get("merged"))
            if state == "closed" or merged:
                self.cleanup_store.complete(obligation.repository, obligation.generation_id, obligation.candidate_id, obligation.pr_number)
                completed += 1
                continue
            # A second classification is intentionally adjacent to the mutation.
            boundary = self.classifier.classify(obligation.repository, obligation.issue_number, obligation.repository, obligation.pr_number)
            if boundary.classification is not ArtifactClassification.RETIRED or boundary.generation_id != obligation.generation_id or boundary.candidate_id != obligation.candidate_id:
                continue
            github_client.close_pr(obligation.repository, obligation.pr_number, f"Auto-Coder: Closing verified losing Jules candidate PR. {obligation.reason}.")
            confirmed = github_client.get_pull_request_metadata_strict(obligation.repository, obligation.pr_number)
            if confirmed.get("state") == "closed" or confirmed.get("merged"):
                self.cleanup_store.complete(obligation.repository, obligation.generation_id, obligation.candidate_id, obligation.pr_number)
                completed += 1
        return completed


_lifecycle: Optional[SpeculativeJulesLifecycle] = None


def configure_speculative_jules_lifecycle(lifecycle: Optional[SpeculativeJulesLifecycle]) -> None:
    """Install the process lifecycle authority (also used by isolated tests)."""
    global _lifecycle
    _lifecycle = lifecycle


def get_speculative_jules_lifecycle() -> Optional[SpeculativeJulesLifecycle]:
    return _lifecycle


def default_cleanup_path() -> Path:
    root = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", Path.home() / ".auto-coder"))
    return root / "state" / "jules_speculative_cleanup.db"
