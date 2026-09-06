"""Identity-keyed, globally bounded semantic specification validation."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ValidationJob(Generic[T]):
    """A shared result for one exact semantic-validation identity."""

    identity_key: str
    future: Future[T]

    def result(self) -> T:
        return self.future.result()


class ValidationScheduler:
    """One capacity boundary shared by every validation category.

    Jobs are keyed by the full lifecycle identity. Completed jobs are removed
    from memory because durable lifecycle stores are the restart/reuse source of
    truth; overlapping observations share the same Future.
    """

    def __init__(self, concurrency: int) -> None:
        if type(concurrency) is not int or concurrency <= 0:
            raise ValueError("validation concurrency must be a positive integer")
        self.concurrency = concurrency
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="spec-validation")
        self._lock = threading.Lock()
        self._in_flight: dict[str, Future[object]] = {}

    def submit(self, identity_key: str, operation: Callable[[], T]) -> ValidationJob[T]:
        with self._lock:
            existing = self._in_flight.get(identity_key)
            if existing is not None:
                return ValidationJob(identity_key, existing)  # type: ignore[arg-type]
            # Repository-specific backend selection is held in a ContextVar.
            # ThreadPoolExecutor workers start with an empty context unless the
            # submitting context is explicitly propagated, which would make the
            # analyzer execute a different provider/model from its policy key.
            context = copy_context()
            future: Future[T] = self._executor.submit(context.run, operation)
            self._in_flight[identity_key] = future  # type: ignore[assignment]

        def retire(completed: Future[T]) -> None:
            with self._lock:
                if self._in_flight.get(identity_key) is completed:
                    self._in_flight.pop(identity_key, None)

        future.add_done_callback(retire)
        return ValidationJob(identity_key, future)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
