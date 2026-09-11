"""Advisory Issue observations and negative admission results; never authorize work."""

import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .automation_config import CandidateProcessingResult, ExplicitTargetOutcome

# Bound refusal reuse on later evaluations when notifications have been missed.
MAX_OBSERVATION_AGE_SECONDS = 300


@dataclass
class IssueObservation:
    snapshot: dict[str, object] = field(default_factory=dict)
    epoch: int = 0
    observed_at: float = 0.0
    ambiguous: bool = False


@dataclass
class CachedIssueBlock:
    result: CandidateProcessingResult = field(default_factory=lambda: CandidateProcessingResult(type="issue"))
    epoch: int = 0
    policy: str = ""
    observed_at: float = 0.0


class IssueAdmissionCache:
    """Process-local cache fenced by repository changes and bounded by age.

    Repository-wide invalidation deliberately includes siblings and reverse
    dependencies. Restart discards all observations; durable startup recovery
    remains responsible for discovering changes received while offline.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._epochs: dict[str, int] = {}
        self._observations: dict[tuple[str, int], IssueObservation] = {}
        self._blocks: dict[tuple[str, int], CachedIssueBlock] = {}

    def epoch(self, repository: str) -> int:
        with self._lock:
            return self._epochs.get(repository, 0)

    def invalidate(self, repository: str) -> None:
        with self._lock:
            self._epochs[repository] = self.epoch(repository) + 1
            self._blocks = {key: value for key, value in self._blocks.items() if key[0] != repository}

    def observe(self, repository: str, snapshot: Mapping[str, object], *, authoritative: bool = False) -> bool:
        number = snapshot.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or "pull_request" in snapshot:
            return False
        if not all(isinstance(snapshot.get(key), str) for key in ("title", "body", "state", "updated_at")):
            return False
        # Raw REST, webhook, and normalized candidates have different auxiliary
        # fields. Only Issue admission inputs belong to this observation.
        snapshot = {key: snapshot.get(key) for key in ("number", "title", "body", "state", "updated_at", "labels", "parent_issue_number", "parent_issue_url")}
        labels = snapshot.get("labels")
        if isinstance(labels, list):
            snapshot["labels"] = sorted(str(label.get("name", "")) if isinstance(label, dict) else str(label) for label in labels)
        with self._lock:
            key = (repository, number)
            previous = self._observations.get(key)
            ambiguous = False
            if previous is not None:
                # Replaying a collected candidate cannot acknowledge a later
                # relationship notification whose payload omitted this Issue.
                if not authoritative and previous.epoch != self.epoch(repository) and previous.snapshot == dict(snapshot):
                    return False
                # An out-of-order delivery is a reason to refresh, never to
                # restore a negative decision from an older representation.
                if str(snapshot["updated_at"]) < str(previous.snapshot["updated_at"]):
                    self.invalidate(repository)
                    return False
                if previous.snapshot != dict(snapshot):
                    self.invalidate(repository)
                ambiguous = not authoritative and snapshot["updated_at"] == previous.snapshot["updated_at"] and (previous.ambiguous or previous.snapshot != dict(snapshot))
            observed_at = time.monotonic()
            if previous is not None and previous.snapshot == dict(snapshot) and not authoritative:
                observed_at = previous.observed_at
            self._observations[key] = IssueObservation(deepcopy(dict(snapshot)), self.epoch(repository), observed_at, ambiguous)
            return not ambiguous

    def snapshot(self, repository: str, number: int) -> Optional[dict[str, object]]:
        with self._lock:
            observed = self._observations.get((repository, number))
            if observed is None or observed.ambiguous or observed.epoch != self.epoch(repository) or time.monotonic() - observed.observed_at >= MAX_OBSERVATION_AGE_SECONDS:
                return None
            return deepcopy(observed.snapshot)

    def get(self, repository: str, number: int, policy: str) -> Optional[CandidateProcessingResult]:
        with self._lock:
            block = self._blocks.get((repository, number))
            if block is None or block.epoch != self.epoch(repository) or block.policy != policy or time.monotonic() - block.observed_at >= MAX_OBSERVATION_AGE_SECONDS:
                return None
            return deepcopy(block.result)

    def remember(self, repository: str, number: int, policy: str, epoch: int, result: CandidateProcessingResult) -> None:
        # Callers explicitly mark only completed refusals, never an unfinished
        # publication, retry, remediation, or a successful admission.
        if result.number != number or result.target_outcome is not ExplicitTargetOutcome.BLOCKED or not result.blocked_cacheable or result.refill_retry_required or result.success:
            return
        with self._lock:
            if epoch == self.epoch(repository):
                self._blocks[(repository, number)] = CachedIssueBlock(deepcopy(result), epoch, policy, time.monotonic())
