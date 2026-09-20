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

    def classify_pr(self, repository: str, pr_repository: str, pr_number: int, hinted_issue_numbers: tuple[int, ...] = ()) -> tuple[Optional[int], ClassificationResult]: ...

    def selected_pr_number(self, repository: str, issue_number: int, generation_id: str) -> Optional[int]: ...


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

    def evaluate_pr(self, repository: str, pr_number: int, hinted_issue_numbers: tuple[int, ...] = ()) -> LifecycleDecision:
        issue_number, result = self.classifier.classify_pr(repository, repository, pr_number, hinted_issue_numbers)
        if result.classification is ArtifactClassification.LEGACY:
            return LifecycleDecision()
        if result.classification is ArtifactClassification.SELECTED:
            return LifecycleDecision(result.classification, True)
        if result.classification is ArtifactClassification.RETIRED:
            if issue_number is None or result.generation_id is None:
                return LifecycleDecision(ArtifactClassification.BLOCKED, False, False, "retired artifact ownership is unavailable")
            selected_pr = self.classifier.selected_pr_number(repository, issue_number, result.generation_id)
            self.cleanup_store.retain(repository, issue_number, result, pr_number, selected_pr)
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


class RefreshingSpeculativeClassifier:
    """Refresh candidate sessions before making an ingress authority decision."""

    def __init__(self, adapter: object, ledger: object):
        self.adapter = adapter
        self.ledger = ledger

    def classify(self, repository: str, issue_number: int, pr_repository: str, pr_number: int) -> ClassificationResult:
        snapshot = self.ledger.get_namespace_snapshot(repository, issue_number)  # type: ignore[attr-defined]
        for generation in snapshot.generations:
            for candidate in generation.candidates:
                if candidate.session_id:
                    self.adapter.observe_candidate(repository, issue_number, generation.generation_id, candidate.candidate_id)  # type: ignore[attr-defined]
        return self.adapter.classify(repository, issue_number, pr_repository, pr_number)  # type: ignore[no-any-return,attr-defined]

    def classify_pr(self, repository: str, pr_repository: str, pr_number: int, hinted_issue_numbers: tuple[int, ...] = ()) -> tuple[Optional[int], ClassificationResult]:
        issue_numbers = tuple(dict.fromkeys((*hinted_issue_numbers, *self.ledger.list_issue_numbers(repository))))  # type: ignore[attr-defined]
        if not issue_numbers:
            return None, ClassificationResult(ArtifactClassification.LEGACY, mutation_allowed=True)
        classified = tuple((issue, self.classify(repository, issue, pr_repository, pr_number)) for issue in issue_numbers)
        relevant = tuple(item for item in classified if item[1].classification is not ArtifactClassification.LEGACY)
        if not relevant:
            return None, ClassificationResult(ArtifactClassification.LEGACY, mutation_allowed=True)
        definitive = tuple(item for item in relevant if item[1].classification in (ArtifactClassification.ACTIVE_UNSELECTED, ArtifactClassification.SELECTED, ArtifactClassification.RETIRED))
        if len(definitive) == 1:
            return definitive[0]
        if len(definitive) > 1 or len(relevant) > 1:
            return None, ClassificationResult(ArtifactClassification.BLOCKED, diagnostics=("PR has multiple speculative Issue associations",))
        return relevant[0]

    def selected_pr_number(self, repository: str, issue_number: int, generation_id: str) -> Optional[int]:
        generation = self.ledger.get_namespace_snapshot(repository, issue_number).get_generation(generation_id)  # type: ignore[attr-defined]
        return generation.winner_pr_number if generation is not None else None


_lifecycle: Optional[SpeculativeJulesLifecycle] = None


def configure_speculative_jules_lifecycle(lifecycle: Optional[SpeculativeJulesLifecycle]) -> None:
    """Install the process lifecycle authority (also used by isolated tests)."""
    global _lifecycle
    _lifecycle = lifecycle


def get_speculative_jules_lifecycle(github_client: Optional[object] = None) -> Optional[SpeculativeJulesLifecycle]:
    """Return configured authority, lazily creating the production adapter."""
    global _lifecycle
    if _lifecycle is None and github_client is not None:
        from .jules_candidate_observation import CandidateObservationStore, JulesCandidateObservationAdapter
        from .jules_client import JulesClient
        from .jules_competition_ledger import JulesCompetitionLedger

        root = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", Path.home() / ".auto-coder")) / "state"
        ledger = JulesCompetitionLedger()
        adapter = JulesCandidateObservationAdapter(
            ledger,
            CandidateObservationStore(root / "jules_candidate_observations.db"),
            JulesClient(),
            github_client,  # type: ignore[arg-type]
        )
        _lifecycle = SpeculativeJulesLifecycle(
            RefreshingSpeculativeClassifier(adapter, ledger),
            SpeculativeCleanupStore(default_cleanup_path()),
        )
    return _lifecycle


def default_cleanup_path() -> Path:
    root = Path(os.environ.get("AUTO_CODER_RUNTIME_ROOT", Path.home() / ".auto-coder"))
    return root / "state" / "jules_speculative_cleanup.db"


def consume_due_speculative_cleanup(github_client: object) -> int:
    """Run restart-recovered cleanup from a production maintenance origin."""
    lifecycle = get_speculative_jules_lifecycle(github_client)
    if lifecycle is None:
        return 0
    return lifecycle.consume_cleanup(github_client)  # type: ignore[arg-type]
