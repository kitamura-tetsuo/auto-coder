"""Instance-local admission control for adversarial PR validation."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class AdversarialValidationLease:
    """The result of requesting one PR's validation admission."""

    acquired: bool


class AdversarialValidationScheduler:
    """Bound active validations while coalescing duplicate local PR triggers."""

    def __init__(self, concurrency: int = 2) -> None:
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("adversarial validation concurrency must be a positive integer")
        self.concurrency = concurrency
        self._capacity = threading.BoundedSemaphore(concurrency)
        self._identity_lock = threading.Lock()
        self._active_prs: set[tuple[str, int]] = set()

    @contextmanager
    def admit(self, repo_name: str, pr_number: int) -> Iterator[AdversarialValidationLease]:
        """Hold capacity through every resource, transition, and cleanup boundary.

        A duplicate trigger from this scheduler instance is rejected rather than
        queued: queuing it could deliberately launch a second attempt as soon as
        the first releases its slot.
        """
        identity = (repo_name, pr_number)
        with self._identity_lock:
            duplicate = identity in self._active_prs
            if not duplicate:
                self._active_prs.add(identity)

        if duplicate:
            yield AdversarialValidationLease(False)
            return

        acquired = False
        try:
            self._capacity.acquire()
            acquired = True
            yield AdversarialValidationLease(True)
        finally:
            if acquired:
                self._capacity.release()
            with self._identity_lock:
                self._active_prs.discard(identity)
