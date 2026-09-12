"""Process-local Issue evidence for negative dependency admission only."""

import threading
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional

from .parent_issue_reconciliation import ParentDeclarationStatus, parse_parent_declaration
from .sibling_dependencies import BlockedByDeclarationStatus, parse_blocked_by_declaration

DEPENDENCY_OBSERVATION_TTL = 300


@dataclass(frozen=True)
class DependencyObservation:
    number: int = 0
    body: str = ""
    state: str = ""
    updated_at: str = ""
    observed_at: float = field(default_factory=time.monotonic)


class DependencyObservationCache:
    """Never persist trust or use a positive observation as execution authority."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._versions: dict[tuple[str, int], int] = {}
        self._observations: dict[tuple[str, int], DependencyObservation] = {}
        self._uncertain: set[tuple[str, int]] = set()

    def version(self, repository: str, number: int) -> int:
        with self._lock:
            return self._versions.get((repository, number), 0)

    def invalidate(self, repository: str, number: int) -> None:
        with self._lock:
            key = (repository, number)
            self._versions[key] = self._versions.get(key, 0) + 1
            self._uncertain.add(key)

    def observe(self, repository: str, snapshot: Mapping[str, object], *, version: Optional[int] = None) -> bool:
        """Publish a webhook, or a REST read fenced by its starting version."""
        number = snapshot.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            return False
        body, state, updated = snapshot.get("body"), snapshot.get("state"), snapshot.get("updated_at")
        if "pull_request" in snapshot or not isinstance(body, str) or state not in ("open", "closed") or not isinstance(updated, str) or not updated:
            self.invalidate(repository, number)
            return False
        observation = DependencyObservation(number, body, str(state), updated)
        with self._lock:
            key = (repository, number)
            if version is not None and self._versions.get(key, 0) != version:
                return False
            previous = self._observations.get(key)
            if previous is not None:
                if updated < previous.updated_at or (version is None and updated == previous.updated_at and (body != previous.body or state != previous.state)):
                    self.invalidate(repository, number)
                    return False
                if version is None and updated == previous.updated_at:
                    return key not in self._uncertain
            self._versions[key] = self._versions.get(key, 0) + 1
            self._observations[key] = observation
            self._uncertain.discard(key)
            return True

    def get(self, repository: str, number: int) -> Optional[DependencyObservation]:
        with self._lock:
            key = (repository, number)
            observation = self._observations.get(key)
            if key in self._uncertain or observation is None or time.monotonic() - observation.observed_at >= DEPENDENCY_OBSERVATION_TTL:
                return None
            return observation

    def dependencies(self, repository: str, number: int) -> frozenset[int]:
        with self._lock:
            observation = self.get(repository, number)
            if observation is None or observation.state != "open":
                return frozenset()
            parent = parse_parent_declaration(observation.body)
            if parent.status is not ParentDeclarationStatus.SUPPORTED:
                return frozenset()
            declaration = parse_blocked_by_declaration(observation.body, parent.status)
            if declaration.status is not BlockedByDeclarationStatus.SUPPORTED:
                return frozenset()
            return frozenset(n for n in declaration.dependencies or () if n != number)

    def waiting_on(self, repository: str, number: int) -> tuple[int, ...]:
        """Read target and blockers atomically; unknown evidence never refuses."""
        with self._lock:
            return tuple(sorted(n for n in self.dependencies(repository, number) if (observed := self.get(repository, n)) is not None and observed.state == "open"))
