"""
Execution-scoped structured diagnostic tracing.

This module provides a versioned, structured-event view of automation
activity that is scoped to individual controller *executions* (one
item-scoped evaluation of an Issue or PR), rather than to whatever text a
Queue/Worker log line happened to contain. It is purely observational:
recording, scope propagation, retention, and snapshot reads never authorize
work, change business return values or exceptions, or make GitHub/provider
requests.

Design notes
-------------
* An "execution" is one top-level controller evaluation of a single
  (repository, item_type, item_number). A resumed/detached job or a new
  top-level evaluation always gets a fresh opaque execution id, even for
  the same item/attempt/head. Nested work that explicitly participates in
  the same evaluation keeps that execution's identity by propagating the
  ``ExecutionScope`` explicitly (across asyncio tasks and worker threads).
* Scope propagation uses ``contextvars.ContextVar``, which already isolates
  concurrent asyncio tasks and top-level threads from one another. Explicit
  propagation into a worker thread or a detached task is done via
  ``current_scope()`` / ``bind_scope()``.
* Event sequence numbers are unique, monotonically increasing within a
  process run, and allocated atomically at publication time, under the same
  lock that appends the event into the bounded retention buffer.
* Retention (events and execution metadata) is bounded. Eviction is tracked
  so a snapshot can honestly report truncation instead of presenting a
  falsely-complete history.
* Trace-sink failures (e.g. an internal bug while building/publishing an
  event) are caught and logged best-effort; they never propagate out of the
  tracing API and never block or alter the business operation being traced.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import copy
import dataclasses
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Deque, Dict, Iterator, List, Literal, Optional

from loguru import logger

SCHEMA_VERSION = 1


class ItemType(str, Enum):
    ISSUE = "issue"
    PR = "pr"


class EventKind(str, Enum):
    EXECUTION_STARTED = "execution-started"
    STAGE_STARTED = "stage-started"
    STAGE_RESULT = "stage-result"
    EXECUTION_FINISHED = "execution-finished"


class Outcome(str, Enum):
    """Explicit, mutually exclusive completion outcomes for an execution or stage.

    UNKNOWN is a first-class outcome: a missing/unrecorded outcome must stay
    UNKNOWN rather than defaulting to a successful one.
    """

    UNKNOWN = "unknown"
    COMPLETED = "completed"
    ACCEPTED_HANDOFF = "accepted_handoff"
    DEFERRED = "deferred"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ExecutionScope:
    """An opaque, propagatable handle identifying one controller evaluation."""

    execution_id: str
    start_sequence: int
    repository: str
    item_type: str
    item_number: int
    process_run_id: str


@dataclass(frozen=True)
class StructuredEvent:
    """One immutable, versioned diagnostic observation."""

    schema_version: int
    repository: str
    item_type: str
    item_number: int
    process_run_id: str
    execution_id: Optional[str]
    execution_start_sequence: Optional[int]
    sequence: int
    origin: str
    stage_id: str
    label: str
    kind: str
    timestamp: float
    outcome: Optional[str] = None
    facts: Optional[Dict[str, Any]] = None
    legacy: bool = False
    supported: bool = True

    def copy_for_snapshot(self) -> "StructuredEvent":
        """Return a copy whose `facts` dict is independent of the stored one."""
        if self.facts is None:
            return self
        return dataclasses.replace(self, facts=copy.deepcopy(self.facts))


@dataclass(frozen=True)
class ExecutionSummary:
    """Companion metadata about one execution, independent of event retention."""

    execution_id: str
    start_sequence: int
    repository: str
    item_type: str
    item_number: int
    finished: bool
    outcome: Optional[str]


@dataclass(frozen=True)
class TraceSnapshot:
    """A read-only, caller-mutation-insulated view of retained trace evidence."""

    process_run_id: str
    schema_version: int
    events: List[StructuredEvent]
    events_truncated: bool
    execution_metadata_truncated: bool
    executions: Dict[str, ExecutionSummary]


_current_scope: "contextvars.ContextVar[Optional[ExecutionScope]]" = contextvars.ContextVar("auto_coder_execution_scope", default=None)


def current_scope() -> Optional[ExecutionScope]:
    """Return the ``ExecutionScope`` bound to the calling context, if any."""
    return _current_scope.get()


@contextlib.contextmanager
def bind_scope(scope: Optional[ExecutionScope]) -> Iterator[Optional[ExecutionScope]]:
    """Bind ``scope`` to the calling context for the duration of the `with` block.

    Use this to explicitly propagate an ``ExecutionScope`` captured via
    ``current_scope()`` into a worker thread or a detached task, since
    contextvars are not inherited by threads started without an explicit
    context copy.
    """
    token = _current_scope.set(scope)
    try:
        yield scope
    finally:
        _current_scope.reset(token)


def run_with_scope(scope: Optional[ExecutionScope], fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``fn(*args, **kwargs)`` with ``scope`` bound. Convenience for thread handoff."""
    with bind_scope(scope):
        return fn(*args, **kwargs)


class ExecutionHandle:
    """Context manager returned by ``TraceCollector.start_execution``.

    Binds the new ``ExecutionScope`` to the current context on enter and
    restores the enclosing scope on exit, even after an exception or
    cancellation. Emits an ``execution-finished`` event on exit unless one
    was already emitted via ``finish()``.
    """

    def __init__(self, collector: "TraceCollector", scope: ExecutionScope, origin: str) -> None:
        self._collector = collector
        self.scope = scope
        self._origin = origin
        self._token: Optional[contextvars.Token[Optional[ExecutionScope]]] = None
        self._pending_outcome: Optional[Outcome] = None
        self._finished = False

    def set_outcome(self, outcome: Outcome) -> None:
        """Record the intended outcome to use when the scope exits normally."""
        self._pending_outcome = outcome

    def finish(self, outcome: Outcome, facts: Optional[Dict[str, Any]] = None) -> None:
        """Explicitly emit the execution-finished event now (idempotent)."""
        if self._finished:
            return
        self._finished = True
        self._collector._finish_execution(self.scope, self._origin, outcome, facts)

    def __enter__(self) -> "ExecutionHandle":
        self._token = _current_scope.set(self.scope)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> "Literal[False]":
        try:
            if not self._finished:
                outcome = self._pending_outcome
                if outcome is None:
                    if exc_type is not None:
                        if issubclass(exc_type, asyncio.CancelledError):
                            outcome = Outcome.CANCELLED
                        else:
                            outcome = Outcome.FAILED
                    else:
                        outcome = Outcome.UNKNOWN
                self._collector._finish_execution(self.scope, self._origin, outcome, None)
                self._finished = True
        finally:
            if self._token is not None:
                _current_scope.reset(self._token)
        return False


class TraceCollector:
    """
    Process-local singleton collecting execution-scoped structured events.

    Retention of both events and execution companion metadata is bounded;
    eviction is tracked so snapshots can honestly report truncation.
    """

    _instance: Optional["TraceCollector"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, max_events: int = 4000, max_executions: int = 1000) -> "TraceCollector":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._init(max_events, max_executions)
                    cls._instance = inst
        return cls._instance

    def _init(self, max_events: int, max_executions: int) -> None:
        self._events: Deque[StructuredEvent] = collections.deque(maxlen=max_events)
        self._executions: "collections.OrderedDict[str, ExecutionSummary]" = collections.OrderedDict()
        self._max_executions = max_executions
        self._seq_counter = 0
        self._publish_lock = threading.RLock()
        self.process_run_id = uuid.uuid4().hex
        self.events_dropped = 0
        self.executions_dropped = 0

    # -- internal plumbing -------------------------------------------------

    def _publish(self, build: Any) -> Optional[StructuredEvent]:
        try:
            with self._publish_lock:
                self._seq_counter += 1
                seq = self._seq_counter
                event = build(seq)
                if len(self._events) == (self._events.maxlen or 0):
                    self.events_dropped += 1
                self._events.append(event)
                return event
        except Exception:
            logger.exception("TraceCollector: failed to publish structured event; continuing without it")
            return None

    def _register_execution(self, summary: ExecutionSummary) -> None:
        try:
            with self._publish_lock:
                if len(self._executions) >= self._max_executions and summary.execution_id not in self._executions:
                    self._executions.popitem(last=False)
                    self.executions_dropped += 1
                self._executions[summary.execution_id] = summary
        except Exception:
            logger.exception("TraceCollector: failed to register execution metadata; continuing")

    # -- recording API -------------------------------------------------

    def start_execution(
        self,
        repository: str,
        item_type: str,
        item_number: int,
        origin: str,
        stage_id: str = "execution",
        label: Optional[str] = None,
        facts: Optional[Dict[str, Any]] = None,
    ) -> ExecutionHandle:
        """Open a new execution scope and emit its `execution-started` event.

        Always mints a fresh opaque execution id: callers that want nested
        work to keep participating in the same execution must propagate the
        returned handle's `.scope` explicitly (see `bind_scope`), never by
        reusing repository/item/attempt/head as an identity.
        """
        execution_id = uuid.uuid4().hex
        resolved_label = label or f"{item_type}#{item_number} execution"

        def build(seq: int) -> StructuredEvent:
            return StructuredEvent(
                schema_version=SCHEMA_VERSION,
                repository=repository,
                item_type=item_type,
                item_number=item_number,
                process_run_id=self.process_run_id,
                execution_id=execution_id,
                execution_start_sequence=seq,
                sequence=seq,
                origin=origin,
                stage_id=stage_id,
                label=resolved_label,
                kind=EventKind.EXECUTION_STARTED.value,
                timestamp=time.time(),
                outcome=None,
                facts=copy.deepcopy(facts) if facts else None,
            )

        event = self._publish(build)
        start_sequence = event.sequence if event is not None else -1
        scope = ExecutionScope(
            execution_id=execution_id,
            start_sequence=start_sequence,
            repository=repository,
            item_type=item_type,
            item_number=item_number,
            process_run_id=self.process_run_id,
        )
        self._register_execution(
            ExecutionSummary(
                execution_id=execution_id,
                start_sequence=start_sequence,
                repository=repository,
                item_type=item_type,
                item_number=item_number,
                finished=False,
                outcome=None,
            )
        )
        return ExecutionHandle(self, scope, origin)

    def record_event(
        self,
        kind: EventKind,
        stage_id: str,
        origin: str,
        label: Optional[str] = None,
        outcome: Optional[Outcome] = None,
        facts: Optional[Dict[str, Any]] = None,
        scope: Optional[ExecutionScope] = None,
    ) -> Optional[StructuredEvent]:
        """Record a `stage-started` or `stage-result` event.

        Uses `scope` when given (for explicit propagation of an earlier or
        remote scope); otherwise uses whatever `ExecutionScope` is bound to
        the calling context. With neither, the event is recorded as
        legacy/unscoped rather than inventing an execution.
        """
        active_scope = scope if scope is not None else current_scope()
        resolved_label = label or stage_id

        def build(seq: int) -> StructuredEvent:
            return StructuredEvent(
                schema_version=SCHEMA_VERSION,
                repository=active_scope.repository if active_scope else "",
                item_type=active_scope.item_type if active_scope else "",
                item_number=active_scope.item_number if active_scope else -1,
                process_run_id=(active_scope.process_run_id if active_scope else self.process_run_id),
                execution_id=active_scope.execution_id if active_scope else None,
                execution_start_sequence=(active_scope.start_sequence if active_scope else None),
                sequence=seq,
                origin=origin,
                stage_id=stage_id,
                label=resolved_label,
                kind=kind.value,
                timestamp=time.time(),
                outcome=outcome.value if outcome is not None else None,
                facts=copy.deepcopy(facts) if facts else None,
                legacy=active_scope is None,
            )

        return self._publish(build)

    def _finish_execution(
        self,
        scope: ExecutionScope,
        origin: str,
        outcome: Outcome,
        facts: Optional[Dict[str, Any]],
    ) -> Optional[StructuredEvent]:
        def build(seq: int) -> StructuredEvent:
            return StructuredEvent(
                schema_version=SCHEMA_VERSION,
                repository=scope.repository,
                item_type=scope.item_type,
                item_number=scope.item_number,
                process_run_id=scope.process_run_id,
                execution_id=scope.execution_id,
                execution_start_sequence=scope.start_sequence,
                sequence=seq,
                origin=origin,
                stage_id="execution",
                label=f"{scope.item_type}#{scope.item_number} execution",
                kind=EventKind.EXECUTION_FINISHED.value,
                timestamp=time.time(),
                outcome=outcome.value,
                facts=copy.deepcopy(facts) if facts else None,
            )

        event = self._publish(build)
        existing = self._executions.get(scope.execution_id)
        if existing is not None:
            self._register_execution(
                ExecutionSummary(
                    execution_id=existing.execution_id,
                    start_sequence=existing.start_sequence,
                    repository=existing.repository,
                    item_type=existing.item_type,
                    item_number=existing.item_number,
                    finished=True,
                    outcome=outcome.value,
                )
            )
        return event

    def record_legacy_or_raw(self, payload: Dict[str, Any]) -> StructuredEvent:
        """Ingest an externally-shaped record (e.g. an old TraceLogger entry).

        Records missing/mismatched `schema_version`, or missing required
        identity fields, are marked `supported=False`/`legacy=True` rather
        than silently promoted into a valid, successful execution event.
        """
        version = payload.get("schema_version")
        supported = version == SCHEMA_VERSION
        has_execution_identity = bool(payload.get("execution_id")) and payload.get("execution_start_sequence") is not None

        def build(seq: int) -> StructuredEvent:
            return StructuredEvent(
                schema_version=version if isinstance(version, int) else 0,
                repository=str(payload.get("repository", "")),
                item_type=str(payload.get("item_type", "")),
                item_number=int(payload.get("item_number") or -1),
                process_run_id=str(payload.get("process_run_id") or self.process_run_id),
                execution_id=(payload.get("execution_id") if has_execution_identity else None),
                execution_start_sequence=(payload.get("execution_start_sequence") if has_execution_identity else None),
                sequence=seq,
                origin=str(payload.get("origin", "legacy")),
                stage_id=str(payload.get("stage_id") or payload.get("category") or "legacy"),
                label=str(payload.get("label") or payload.get("message") or "legacy event"),
                kind=str(payload.get("kind", "")) or "legacy",
                timestamp=float(payload.get("timestamp") or time.time()),
                outcome=None,
                facts=copy.deepcopy(payload.get("facts") or payload.get("details")) or None,
                legacy=not has_execution_identity,
                supported=supported,
            )

        event = self._publish(build)
        if event is None:
            # Publication itself failed (e.g. buffer/lock error); surface a
            # best-effort unsupported/legacy record without raising, since
            # trace-sink failures must never propagate to callers.
            event = StructuredEvent(
                schema_version=0,
                repository="",
                item_type="",
                item_number=-1,
                process_run_id=self.process_run_id,
                execution_id=None,
                execution_start_sequence=None,
                sequence=-1,
                origin="legacy",
                stage_id="legacy",
                label="legacy event",
                kind="legacy",
                timestamp=time.time(),
                legacy=True,
                supported=False,
            )
        return event

    # -- read API -------------------------------------------------

    def get_snapshot(
        self,
        item_type: Optional[str] = None,
        item_number: Optional[int] = None,
        repository: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> TraceSnapshot:
        """Return an insulated snapshot of retained events and execution metadata."""
        with self._publish_lock:
            events = list(self._events)
            executions = dict(self._executions)
            events_truncated = self.events_dropped > 0
            executions_truncated = self.executions_dropped > 0

        if item_type is not None:
            events = [e for e in events if e.item_type == item_type]
        if item_number is not None:
            events = [e for e in events if e.item_number == item_number]
        if repository is not None:
            events = [e for e in events if e.repository == repository]
        if limit is not None:
            events = events[-limit:]

        snapshot_events = [e.copy_for_snapshot() for e in events]
        snapshot_executions = {k: v for k, v in executions.items()}

        return TraceSnapshot(
            process_run_id=self.process_run_id,
            schema_version=SCHEMA_VERSION,
            events=snapshot_events,
            events_truncated=events_truncated,
            execution_metadata_truncated=executions_truncated,
            executions=snapshot_executions,
        )

    def clear(self) -> None:
        self._events.clear()
        self._executions.clear()
        self.events_dropped = 0
        self.executions_dropped = 0


def get_trace_collector() -> TraceCollector:
    """Get the singleton `TraceCollector` instance."""
    return TraceCollector()
