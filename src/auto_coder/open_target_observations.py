"""Advisory dashboard observations; never an execution authority."""

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenTarget:
    kind: str
    number: int
    title: str = ""
    created_at: str = ""


class OpenTargetObservations:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._targets: dict[tuple[str, int], OpenTarget] = {}
        self._changes: dict[tuple[str, int], int] = {}
        self._revision = 0
        self._complete_at: float | None = None
        self._error: str | None = None

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def complete(self, targets: list[OpenTarget], started_revision: int) -> None:
        with self._lock:
            incoming = {(item.kind, item.number): item for item in targets}
            # A strict target observation newer than enumeration wins, including
            # a closed/404 tombstone. Enumeration is not a webhook event log.
            for key, revision in self._changes.items():
                if revision > started_revision:
                    incoming.pop(key, None)
                    if key in self._targets:
                        incoming[key] = self._targets[key]
                elif key in incoming and key in self._targets:
                    incoming[key] = self._targets[key]
            self._targets = incoming
            self._complete_at = time.time()
            self._error = None

    def fail(self, reason: str) -> None:
        with self._lock:
            self._error = reason

    def observe(self, target: OpenTarget, state: str) -> None:
        if state not in {"open", "closed", "absent"}:
            return
        with self._lock:
            key = (target.kind, target.number)
            self._revision += 1
            self._changes[key] = self._revision
            if state == "open":
                self._targets[key] = target
            else:
                self._targets.pop(key, None)

    def snapshot(self) -> tuple[list[OpenTarget], dict[str, object]]:
        with self._lock:
            return list(self._targets.values()), {
                "availability": "stale" if self._error and self._complete_at is not None else "available" if self._complete_at is not None else "unavailable",
                "complete_observed_at": self._complete_at,
                "reason": self._error,
                "source": "startup enumeration and strict target observations; advisory only",
            }


def open_targets_notice(observation: dict[str, object], *, empty: bool) -> str:
    availability = observation.get("availability", "unavailable")
    if availability == "unavailable":
        return "Open-target enumeration is unavailable; an empty repository has not been confirmed."
    if availability == "stale":
        return "Open-target enumeration failed; retained observations may be stale."
    if empty:
        return "Complete enumeration observed; no open issues or PRs remain in the recorded observations."
    return "Recorded open targets; updated by startup enumeration and strict target observations, not a live repository listing."
