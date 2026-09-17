"""
Isolated, bounded diagnostic scopes for repository-wide maintenance jobs.

This module gives repository-scoped internal jobs (currently, the
dependency-rescan fan-out that turns one dependency-affecting webhook into a
repository-wide Issue reevaluation sweep) their own producer/snapshot-consumer
diagnostic interface, distinct from the schema-version-1 Issue/PR interface
in ``execution_trace.py``.

Why a separate interface
-------------------------
A repository job has no GitHub Issue/PR number: it targets a whole
repository under a job kind (e.g. ``dependency-rescan``). Reusing the
Issue/PR item interface would force callers to invent a synthetic item
number (the historical bug this replaces: the dashboard displaying
"Dependency #1", indistinguishable from GitHub Issue #1). Instead, a
``RepoJobTarget`` identifies a job by ``(repository, job_kind)`` in a
namespace that can never collide with an Issue/PR target, and this module's
recorder/collector/snapshot types are structurally distinct from
``StructuredEvent``/``TraceCollector`` so a job observation can never be
misclassified as -- or silently promoted into -- Issue/PR evidence. This is
purely additive: it does not read, write, or reinterpret anything recorded
through ``execution_trace.py``.

Design notes
------------
* An "execution" here is one actual scan attempt (including a retry or a
  recovered attempt). Every attempt gets a fresh opaque execution id, never
  reused because the repository, durable queue token, invalidation
  generation, source webhook delivery, or inputs happen to match an earlier
  attempt. Nested observations that explicitly participate in the same
  attempt propagate its ``RepoJobExecutionScope`` (contextvars, with
  explicit thread/task hand-off helpers, mirroring ``execution_trace``).
* Recording an intake, a queued state, or a recovered-pending observation
  never manufactures a scan execution: those observation kinds are
  deliberately unscoped (``execution_id is None``) until an actual attempt
  starts.
* Correlation between an unscoped observation and a later execution is
  always by an explicit, producer-supplied ``observation_id`` reference
  (``RepoJobFacts.source_observation_refs``) -- never by proximity in time,
  a matching generation/number, or "the most recently active worker".
* Facts are a typed dataclass, not a free-form mapping, so the fact set is
  structurally limited to references/counts/phases/availability -- there is
  no field that could carry raw request/response bodies, credentials, or
  raw Issue content. Reference collections (trigger deliveries, source
  Issues, target Issues) are bounded per record; a caller-supplied exact
  total count can remain exact even when the retained list is clipped.
* Retention (observations and execution metadata) is bounded and eviction is
  tracked, exactly like ``execution_trace.TraceCollector``, so a snapshot can
  honestly report truncation instead of a falsely-complete history.
* Recording, propagation, retention, and snapshot reads are purely
  observational: failures are caught and logged best-effort and never change
  a business return value/exception or perform a GitHub/provider/queue call.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, Iterator, List, Literal, Optional, Sequence, Tuple

from loguru import logger

from .execution_trace import Outcome

SCHEMA_VERSION = 1

_DEFAULT_MAX_OBSERVATIONS = 2000
_DEFAULT_MAX_EXECUTIONS = 500
_DEFAULT_MAX_REFS_PER_RECORD = 20


class RepoJobKind(str, Enum):
    """Supported repository-job namespaces, distinct from Issue/PR item types."""

    DEPENDENCY_RESCAN = "dependency-rescan"


class RepoJobObservationKind(str, Enum):
    """What a given `RepoJobObservation` reports.

    INTAKE/QUEUED/RECOVERED never carry an execution identity: they describe
    repository-scoped evidence that exists before (or independent of) any
    actual scan attempt. EXECUTION_STARTED/STAGE_REACHED/EXECUTION_FINISHED
    always belong to one execution attempt.
    """

    INTAKE = "intake"
    QUEUED = "queued"
    RECOVERED = "recovered"
    EXECUTION_STARTED = "execution-started"
    STAGE_REACHED = "stage-reached"
    EXECUTION_FINISHED = "execution-finished"


_UNSCOPED_KINDS = frozenset(
    {
        RepoJobObservationKind.INTAKE.value,
        RepoJobObservationKind.QUEUED.value,
        RepoJobObservationKind.RECOVERED.value,
    }
)


@dataclass(frozen=True)
class RepoJobTarget:
    """A validated `(repository, job_kind)` diagnostic target.

    Never carries an item/GitHub number: a repository job is identified by
    repository plus job kind alone. Construct via `resolve_repo_job_target`,
    which is the only supported way to turn a raw string into a target and
    rejects an unsupported/malformed kind instead of guessing one.
    """

    repository: str
    job_kind: str


def resolve_repo_job_target(repository: Optional[str], job_kind: Optional[str]) -> Optional[RepoJobTarget]:
    """Validate and construct a `RepoJobTarget`, or return None.

    Returns None for an empty repository or an unsupported/malformed
    `job_kind`. Callers must handle None explicitly (e.g. reject the
    request) rather than falling back to another target or to "the
    repository's most recent record".
    """
    if not repository or not isinstance(repository, str):
        return None
    if not isinstance(job_kind, str) or job_kind not in {member.value for member in RepoJobKind}:
        return None
    return RepoJobTarget(repository=repository, job_kind=job_kind)


@dataclass(frozen=True)
class ClippedNumberRefs:
    """A possibly-clipped, immutable tuple of reference numbers (e.g. Issue numbers).

    `total_count` is an explicit, separately-supplied exact aggregate; it is
    never derived from `len(numbers)` and stays None (unknown) unless a
    producer actually supplied it. `truncated` is set whenever the retained
    tuple is known to be an incomplete view of what the producer described,
    even when `total_count` itself is unknown.
    """

    numbers: Tuple[int, ...] = ()
    truncated: bool = False
    total_count: Optional[int] = None


@dataclass(frozen=True)
class ClippedTextRefs:
    """The `ClippedNumberRefs` counterpart for opaque text references (e.g. webhook delivery ids)."""

    values: Tuple[str, ...] = ()
    truncated: bool = False
    total_count: Optional[int] = None


def _clip_numbers(numbers: Optional[Sequence[int]], limit: int, total_count: Optional[int], already_truncated: bool) -> ClippedNumberRefs:
    values = tuple(numbers or ())
    clipped = values[:limit]
    return ClippedNumberRefs(numbers=clipped, truncated=already_truncated or len(values) > limit, total_count=total_count)


def _clip_text(values: Optional[Sequence[str]], limit: int, total_count: Optional[int], already_truncated: bool) -> ClippedTextRefs:
    resolved = tuple(values or ())
    clipped = resolved[:limit]
    return ClippedTextRefs(values=clipped, truncated=already_truncated or len(resolved) > limit, total_count=total_count)


@dataclass(frozen=True)
class RepoJobFacts:
    """Typed, structurally-limited facts attached to one `RepoJobObservation`.

    Every field is optional and independently absent-vs-known: `None` (or an
    empty/default collection with `total_count=None`) means "not recorded",
    never a guessed zero/false/empty. A target/source Issue reference
    records a handoff target, not proof of that Issue's execution,
    eligibility, or implementation.
    """

    source_observation_refs: Tuple[str, ...] = ()
    observed_invalidation_generation: Optional[int] = None
    trigger_event: Optional[str] = None
    trigger_action: Optional[str] = None
    trigger_delivery_refs: ClippedTextRefs = field(default_factory=ClippedTextRefs)
    source_issue_refs: ClippedNumberRefs = field(default_factory=ClippedNumberRefs)
    queue_phase: Optional[str] = None
    worker_phase: Optional[str] = None
    scan_available: Optional[bool] = None
    handoff_count: Optional[int] = None
    handoff_disposition: Optional[str] = None
    target_issue_refs: ClippedNumberRefs = field(default_factory=ClippedNumberRefs)
    failure_reason: Optional[str] = None
    scheduled_retry_not_before: Optional[float] = None
    # Per-attempt discovery/handoff aggregate counts (Issue #2001). Each is
    # independently optional/absent-vs-known, exactly like every other field
    # here: a producer that never reached that phase leaves it None rather
    # than reporting a guessed zero.
    discovered_issue_count: Optional[int] = None
    attempted_handoff_count: Optional[int] = None
    confirmed_handoff_count: Optional[int] = None
    failed_or_unconfirmed_handoff_count: Optional[int] = None
    new_pending_handoff_count: Optional[int] = None
    coalesced_handoff_count: Optional[int] = None
    followup_required_handoff_count: Optional[int] = None


@dataclass(frozen=True)
class RepoJobExecutionScope:
    """An opaque, propagatable handle identifying one repository-job execution attempt."""

    execution_id: str
    start_sequence: int
    repository: str
    job_kind: str
    process_run_id: str


@dataclass(frozen=True)
class RepoJobObservation:
    """One immutable, versioned repository-job diagnostic observation."""

    schema_version: int
    observation_id: str
    repository: str
    job_kind: str
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
    facts: Optional[RepoJobFacts] = None

    def copy_for_snapshot(self) -> "RepoJobObservation":
        """Return self: `RepoJobFacts` is itself frozen/tuple-based and immutable."""
        return self


@dataclass(frozen=True)
class RepoJobExecutionSummary:
    """Companion metadata about one repository-job execution attempt."""

    execution_id: str
    start_sequence: int
    repository: str
    job_kind: str
    finished: bool
    outcome: Optional[str]


@dataclass(frozen=True)
class RepoJobSnapshot:
    """A read-only, caller/producer-mutation-insulated view of retained repo-job evidence."""

    process_run_id: str
    schema_version: int
    observations: List[RepoJobObservation]
    observations_truncated: bool
    execution_metadata_truncated: bool
    executions: Dict[str, RepoJobExecutionSummary]


_current_repo_job_scope: "contextvars.ContextVar[Optional[RepoJobExecutionScope]]" = contextvars.ContextVar("auto_coder_repo_job_scope", default=None)


def current_repo_job_scope() -> Optional[RepoJobExecutionScope]:
    """Return the `RepoJobExecutionScope` bound to the calling context, if any."""
    return _current_repo_job_scope.get()


@contextlib.contextmanager
def bind_repo_job_scope(scope: Optional[RepoJobExecutionScope]) -> Iterator[Optional[RepoJobExecutionScope]]:
    """Bind `scope` to the calling context for the duration of the `with` block.

    Use this to explicitly propagate a captured `RepoJobExecutionScope` into
    a worker thread or a detached task; contextvars are not otherwise
    inherited by a thread started without an explicit context copy.
    """
    token = _current_repo_job_scope.set(scope)
    try:
        yield scope
    finally:
        _current_repo_job_scope.reset(token)


class RepoJobExecutionHandle:
    """Context manager returned by `RepoJobTraceCollector.start_execution`.

    Mirrors `execution_trace.ExecutionHandle`: binds the new scope on enter,
    restores the enclosing scope on exit (even after an exception), and
    emits an execution-finished observation on exit unless already finished.
    """

    def __init__(self, collector: "RepoJobTraceCollector", scope: RepoJobExecutionScope, origin: str) -> None:
        self._collector = collector
        self.scope = scope
        self._origin = origin
        self._token: Optional["contextvars.Token[Optional[RepoJobExecutionScope]]"] = None
        self._pending_outcome: Optional[Outcome] = None
        self._finished = False

    def set_outcome(self, outcome: Outcome) -> None:
        """Record the intended outcome to use when the scope exits normally."""
        self._pending_outcome = outcome

    def finish(self, outcome: Outcome, facts: Optional[RepoJobFacts] = None) -> None:
        """Explicitly emit the execution-finished observation now (idempotent)."""
        if self._finished:
            return
        self._finished = True
        self._collector._finish_execution(self.scope, self._origin, outcome, facts)

    def __enter__(self) -> "RepoJobExecutionHandle":
        self._token = _current_repo_job_scope.set(self.scope)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> "Literal[False]":
        try:
            if not self._finished:
                outcome = self._pending_outcome
                if outcome is None:
                    if exc_type is not None:
                        if isinstance(exc_type, type) and issubclass(exc_type, asyncio.CancelledError):
                            outcome = Outcome.CANCELLED
                        else:
                            outcome = Outcome.FAILED
                    else:
                        outcome = Outcome.UNKNOWN
                self._collector._finish_execution(self.scope, self._origin, outcome, None)
                self._finished = True
        finally:
            if self._token is not None:
                _current_repo_job_scope.reset(self._token)
        return False


class RepoJobTraceCollector:
    """Process-local singleton collecting repository-job diagnostic observations.

    Storage, identity, and retention are entirely independent of
    `execution_trace.TraceCollector`: a repository-job observation can never
    be filtered into, or mistaken for, Issue/PR evidence.
    """

    _instance: Optional["RepoJobTraceCollector"] = None
    _instance_lock = threading.Lock()

    def __new__(
        cls,
        max_observations: int = _DEFAULT_MAX_OBSERVATIONS,
        max_executions: int = _DEFAULT_MAX_EXECUTIONS,
        max_refs_per_record: int = _DEFAULT_MAX_REFS_PER_RECORD,
    ) -> "RepoJobTraceCollector":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._init(max_observations, max_executions, max_refs_per_record)
                    cls._instance = inst
        return cls._instance

    def _init(self, max_observations: int, max_executions: int, max_refs_per_record: int) -> None:
        self._observations: Deque[RepoJobObservation] = collections.deque(maxlen=max_observations)
        self._executions: "collections.OrderedDict[str, RepoJobExecutionSummary]" = collections.OrderedDict()
        self._max_executions = max_executions
        self._max_refs_per_record = max_refs_per_record
        self._seq_counter = 0
        self._publish_lock = threading.RLock()
        self.process_run_id = uuid.uuid4().hex
        self.observations_dropped = 0
        self.executions_dropped = 0

    # -- fact-shaping helpers (bounded, mutation-safe) -----------------

    def clip_source_issue_refs(self, numbers: Optional[Sequence[int]], total_count: Optional[int] = None, truncated: bool = False) -> ClippedNumberRefs:
        return _clip_numbers(numbers, self._max_refs_per_record, total_count, truncated)

    def clip_target_issue_refs(self, numbers: Optional[Sequence[int]], total_count: Optional[int] = None, truncated: bool = False) -> ClippedNumberRefs:
        return _clip_numbers(numbers, self._max_refs_per_record, total_count, truncated)

    def clip_trigger_delivery_refs(self, values: Optional[Sequence[str]], total_count: Optional[int] = None, truncated: bool = False) -> ClippedTextRefs:
        return _clip_text(values, self._max_refs_per_record, total_count, truncated)

    # -- internal plumbing -------------------------------------------------

    def _next_sequence(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _publish(self, build) -> Optional[RepoJobObservation]:  # type: ignore[no-untyped-def]
        try:
            with self._publish_lock:
                seq = self._next_sequence()
                observation = build(seq)
                if len(self._observations) == (self._observations.maxlen or 0):
                    self.observations_dropped += 1
                self._observations.append(observation)
                return observation
        except Exception:
            logger.exception("RepoJobTraceCollector: failed to publish observation; continuing without it")
            return None

    def _register_execution(self, summary: RepoJobExecutionSummary) -> None:
        try:
            with self._publish_lock:
                if len(self._executions) >= self._max_executions and summary.execution_id not in self._executions:
                    self._executions.popitem(last=False)
                    self.executions_dropped += 1
                self._executions[summary.execution_id] = summary
        except Exception:
            logger.exception("RepoJobTraceCollector: failed to register execution metadata; continuing")

    def _observation_id(self, sequence: int) -> str:
        return f"{self.process_run_id}:{sequence}"

    # -- unscoped recording API (no execution manufactured) ------------

    def _record_unscoped(self, kind: RepoJobObservationKind, target: RepoJobTarget, origin: str, label: Optional[str], facts: Optional[RepoJobFacts]) -> Optional[RepoJobObservation]:
        resolved_label = label or f"{target.job_kind} {kind.value}"

        def build(seq: int) -> RepoJobObservation:
            return RepoJobObservation(
                schema_version=SCHEMA_VERSION,
                observation_id=self._observation_id(seq),
                repository=target.repository,
                job_kind=target.job_kind,
                process_run_id=self.process_run_id,
                execution_id=None,
                execution_start_sequence=None,
                sequence=seq,
                origin=origin,
                stage_id=kind.value,
                label=resolved_label,
                kind=kind.value,
                timestamp=time.time(),
                outcome=None,
                facts=facts,
            )

        return self._publish(build)

    def record_intake(self, target: RepoJobTarget, origin: str, label: Optional[str] = None, facts: Optional[RepoJobFacts] = None) -> Optional[RepoJobObservation]:
        """Record repository-scoped intake evidence (e.g. a webhook accepted for this job).

        Never scoped to an execution: intake alone must not manufacture a
        scan attempt or a completion.
        """
        return self._record_unscoped(RepoJobObservationKind.INTAKE, target, origin, label, facts)

    def record_queued(self, target: RepoJobTarget, origin: str, label: Optional[str] = None, facts: Optional[RepoJobFacts] = None) -> Optional[RepoJobObservation]:
        """Record that this job is durably queued/coalesced, still pre-execution."""
        return self._record_unscoped(RepoJobObservationKind.QUEUED, target, origin, label, facts)

    def record_recovered(self, target: RepoJobTarget, origin: str, label: Optional[str] = None, facts: Optional[RepoJobFacts] = None) -> Optional[RepoJobObservation]:
        """Record recovery of durable pending work after a restart.

        This documents "known pending work exists" as of now; it must never
        reconstruct a lost historical execution, trigger, or completion.
        """
        return self._record_unscoped(RepoJobObservationKind.RECOVERED, target, origin, label, facts)

    # -- execution-scoped recording API ---------------------------------

    def start_execution(
        self,
        target: RepoJobTarget,
        origin: str,
        stage_id: str = "execution",
        label: Optional[str] = None,
        facts: Optional[RepoJobFacts] = None,
    ) -> RepoJobExecutionHandle:
        """Open a new execution scope and emit its `execution-started` observation.

        Always mints a fresh opaque execution id: a retry or a recovered
        attempt never reuses an earlier attempt's identity merely because
        the repository, durable queue token, invalidation generation,
        source delivery, or inputs match.
        """
        execution_id = uuid.uuid4().hex
        resolved_label = label or f"{target.job_kind} execution"

        def build(seq: int) -> RepoJobObservation:
            return RepoJobObservation(
                schema_version=SCHEMA_VERSION,
                observation_id=self._observation_id(seq),
                repository=target.repository,
                job_kind=target.job_kind,
                process_run_id=self.process_run_id,
                execution_id=execution_id,
                execution_start_sequence=seq,
                sequence=seq,
                origin=origin,
                stage_id=stage_id,
                label=resolved_label,
                kind=RepoJobObservationKind.EXECUTION_STARTED.value,
                timestamp=time.time(),
                outcome=None,
                facts=facts,
            )

        observation = self._publish(build)
        start_sequence = observation.sequence if observation is not None else -1
        scope = RepoJobExecutionScope(
            execution_id=execution_id,
            start_sequence=start_sequence,
            repository=target.repository,
            job_kind=target.job_kind,
            process_run_id=self.process_run_id,
        )
        self._register_execution(
            RepoJobExecutionSummary(
                execution_id=execution_id,
                start_sequence=start_sequence,
                repository=target.repository,
                job_kind=target.job_kind,
                finished=False,
                outcome=None,
            )
        )
        return RepoJobExecutionHandle(self, scope, origin)

    def record_stage_reached(
        self,
        stage_id: str,
        origin: str,
        label: Optional[str] = None,
        outcome: Optional[Outcome] = None,
        facts: Optional[RepoJobFacts] = None,
        scope: Optional[RepoJobExecutionScope] = None,
    ) -> Optional[RepoJobObservation]:
        """Record a `stage-reached` observation nested under an execution.

        Uses `scope` when given (explicit propagation of an earlier or
        remote scope); otherwise uses whatever `RepoJobExecutionScope` is
        bound to the calling context. With neither, nothing is recorded --
        this interface never invents an execution to attach a nested
        observation to.
        """
        active_scope = scope if scope is not None else current_repo_job_scope()
        if active_scope is None:
            return None
        resolved_label = label or stage_id

        def build(seq: int) -> RepoJobObservation:
            return RepoJobObservation(
                schema_version=SCHEMA_VERSION,
                observation_id=self._observation_id(seq),
                repository=active_scope.repository,
                job_kind=active_scope.job_kind,
                process_run_id=active_scope.process_run_id,
                execution_id=active_scope.execution_id,
                execution_start_sequence=active_scope.start_sequence,
                sequence=seq,
                origin=origin,
                stage_id=stage_id,
                label=resolved_label,
                kind=RepoJobObservationKind.STAGE_REACHED.value,
                timestamp=time.time(),
                outcome=outcome.value if outcome is not None else None,
                facts=facts,
            )

        return self._publish(build)

    def _finish_execution(
        self,
        scope: RepoJobExecutionScope,
        origin: str,
        outcome: Outcome,
        facts: Optional[RepoJobFacts],
    ) -> Optional[RepoJobObservation]:
        def build(seq: int) -> RepoJobObservation:
            return RepoJobObservation(
                schema_version=SCHEMA_VERSION,
                observation_id=self._observation_id(seq),
                repository=scope.repository,
                job_kind=scope.job_kind,
                process_run_id=scope.process_run_id,
                execution_id=scope.execution_id,
                execution_start_sequence=scope.start_sequence,
                sequence=seq,
                origin=origin,
                stage_id="execution",
                label=f"{scope.job_kind} execution",
                kind=RepoJobObservationKind.EXECUTION_FINISHED.value,
                timestamp=time.time(),
                outcome=outcome.value,
                facts=facts,
            )

        observation = self._publish(build)
        existing = self._executions.get(scope.execution_id)
        if existing is not None:
            self._register_execution(
                RepoJobExecutionSummary(
                    execution_id=existing.execution_id,
                    start_sequence=existing.start_sequence,
                    repository=existing.repository,
                    job_kind=existing.job_kind,
                    finished=True,
                    outcome=outcome.value,
                )
            )
        return observation

    # -- read API -------------------------------------------------

    def get_snapshot(self, target: Optional[RepoJobTarget] = None, limit: Optional[int] = None) -> RepoJobSnapshot:
        """Return an insulated snapshot of retained observations and execution metadata.

        `target` must be a validated `RepoJobTarget` (see
        `resolve_repo_job_target`); passing `None` means "no target filter",
        never "fall back to whatever is most recent" -- an unsupported/
        malformed raw target is rejected by `resolve_repo_job_target` before
        it ever reaches this method.
        """
        with self._publish_lock:
            observations = list(self._observations)
            executions = dict(self._executions)
            observations_truncated = self.observations_dropped > 0
            executions_truncated = self.executions_dropped > 0

        if target is not None:
            observations = [o for o in observations if o.repository == target.repository and o.job_kind == target.job_kind]
            executions = {k: v for k, v in executions.items() if v.repository == target.repository and v.job_kind == target.job_kind}
        if limit is not None:
            observations = observations[-limit:]

        return RepoJobSnapshot(
            process_run_id=self.process_run_id,
            schema_version=SCHEMA_VERSION,
            observations=[o.copy_for_snapshot() for o in observations],
            observations_truncated=observations_truncated,
            execution_metadata_truncated=executions_truncated,
            executions=executions,
        )

    def clear(self) -> None:
        self._observations.clear()
        self._executions.clear()
        self.observations_dropped = 0
        self.executions_dropped = 0


def get_repo_job_trace_collector() -> RepoJobTraceCollector:
    """Get the singleton `RepoJobTraceCollector` instance."""
    return RepoJobTraceCollector()


# -- pure snapshot-consumer helpers (no NiceGUI/dashboard dependency) -----


def executions_for_target(snapshot: RepoJobSnapshot, target: RepoJobTarget) -> List[RepoJobExecutionSummary]:
    """Executions for exactly this target, oldest start first.

    Ordering is by `start_sequence` (original execution-start publication
    order), never by wall-clock timestamp or list index, so late-arriving
    evidence for an older execution cannot reorder it.
    """
    matches = [s for s in snapshot.executions.values() if s.repository == target.repository and s.job_kind == target.job_kind]
    matches.sort(key=lambda s: s.start_sequence)
    return matches


def observations_for_execution(snapshot: RepoJobSnapshot, execution_id: str) -> List[RepoJobObservation]:
    """Observations belonging to exactly one execution, in publication order."""
    matched = [o for o in snapshot.observations if o.execution_id == execution_id]
    matched.sort(key=lambda o: o.sequence)
    return matched


def unassociated_observations_for_target(snapshot: RepoJobSnapshot, target: RepoJobTarget) -> List[RepoJobObservation]:
    """Observations for this target that do not (yet) belong to an execution.

    These are intake/queued/recovered observations recorded before any scan
    attempt started -- never assigned a guessed execution.
    """
    matched = [o for o in snapshot.observations if o.execution_id is None and o.repository == target.repository and o.job_kind == target.job_kind and o.kind in _UNSCOPED_KINDS]
    matched.sort(key=lambda o: o.sequence)
    return matched
