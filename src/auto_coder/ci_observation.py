"""Transport-independent, causally scoped CI observation state.

This module deliberately contains no GitHub client integration.  It models the
facts returned by a provider read and the short-lived authorization to reuse
those facts while a controller remains in the same read-only phase.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeAlias
from uuid import uuid4


class ObservationAvailability(str, Enum):
    """Completeness of the evidence captured by one provider read."""

    KNOWN = "known"
    KNOWN_EMPTY = "known_empty"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    THROTTLED = "throttled"
    SUPERSEDED = "superseded"


class CIConclusion(str, Enum):
    """A source-reported fact, not an aggregate required-check verdict."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    NEUTRAL = "neutral"
    TIMED_OUT = "timed_out"
    ACTION_REQUIRED = "action_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ObservationSubject:
    """The exact PR revision and provider boundary being observed."""

    api_origin: str
    repository: str
    pr_number: int
    head_sha: str

    def __post_init__(self) -> None:
        if not self.api_origin or not self.repository or not self.head_sha:
            raise ValueError("API origin, repository, and head SHA are required")
        if self.pr_number <= 0:
            raise ValueError("PR number must be positive")


@dataclass(frozen=True)
class ObservationRequest:
    """Source and representation requested from the provider."""

    source: str
    representation: str

    def __post_init__(self) -> None:
        if not self.source or not self.representation:
            raise ValueError("Observation source and representation are required")


@dataclass(frozen=True)
class WorkflowExecutionIdentity:
    """Stable provider identities for one workflow execution."""

    workflow_id: str
    run_id: str
    attempt: int | None

    def __post_init__(self) -> None:
        if not self.workflow_id or not self.run_id:
            raise ValueError("Workflow and run identities are required")
        if self.attempt is not None and self.attempt <= 0:
            raise ValueError("Workflow attempt must be positive when known")


@dataclass(frozen=True)
class CheckExecutionIdentity:
    """Stable check/App identity with explicitly known run association."""

    app_id: str
    check_id: str
    workflow_id: str | None = None
    run_id: str | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        if not self.app_id or not self.check_id:
            raise ValueError("Check App and check identities are required")
        if self.attempt is not None and self.attempt <= 0:
            raise ValueError("Check attempt must be positive when known")
        if self.attempt is not None and self.run_id is None:
            raise ValueError("A check attempt requires an explicit run association")
        if (self.workflow_id is None) != (self.run_id is None):
            raise ValueError("Workflow and run association must both be known or both unresolved")


@dataclass(frozen=True)
class WorkflowObservation:
    execution: WorkflowExecutionIdentity
    conclusion: CIConclusion
    display_name: str = ""
    waiting_for_deployment: bool = False
    workflow_path: str = ""


@dataclass(frozen=True)
class CheckObservation:
    execution: CheckExecutionIdentity
    conclusion: CIConclusion
    display_name: str = ""


CIObservationFact: TypeAlias = WorkflowObservation | CheckObservation


@dataclass(frozen=True)
class CIObservationSnapshot:
    """Immutable output of exactly one provider read cycle."""

    subject: ObservationSubject
    request: ObservationRequest
    cycle_id: str
    invalidation_epoch: int
    availability: ObservationAvailability
    facts: tuple[CIObservationFact, ...] = field(default_factory=tuple)
    unavailable_reason: str | None = None
    diagnostic_facts: tuple[CIObservationFact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.cycle_id:
            raise ValueError("A globally distinct read-cycle identity is required")
        if self.invalidation_epoch < 0:
            raise ValueError("Invalidation epoch cannot be negative")
        if self.availability is ObservationAvailability.KNOWN_EMPTY and self.facts:
            raise ValueError("Known-empty observations cannot contain current facts")
        if self.availability is ObservationAvailability.KNOWN and not self.facts:
            raise ValueError("A complete known observation must contain facts")
        if self.availability in {ObservationAvailability.UNAVAILABLE, ObservationAvailability.THROTTLED, ObservationAvailability.SUPERSEDED} and self.facts:
            raise ValueError("Non-authoritative observations cannot expose current facts")
        if self.availability in {ObservationAvailability.UNAVAILABLE, ObservationAvailability.THROTTLED} and not self.unavailable_reason:
            raise ValueError("Unavailable and throttled observations require a safe reason")

    @property
    def complete(self) -> bool:
        return self.availability in {ObservationAvailability.KNOWN, ObservationAvailability.KNOWN_EMPTY}

    def workflow_execution(self, workflow_id: str, run_id: str, attempt: int) -> WorkflowObservation | None:
        """Return only an exact, explicitly ordered workflow execution."""
        for fact in self.facts:
            if isinstance(fact, WorkflowObservation) and fact.execution == WorkflowExecutionIdentity(workflow_id, run_id, attempt):
                return fact
        return None


@dataclass(frozen=True)
class ObservationRead:
    """Capability issued for one read in one active phase."""

    phase_id: str
    cycle_id: str
    subject: ObservationSubject
    request: ObservationRequest
    captured_epoch: int

    def snapshot(
        self,
        availability: ObservationAvailability,
        facts: Iterable[CIObservationFact] = (),
        *,
        unavailable_reason: str | None = None,
        diagnostic_facts: Iterable[CIObservationFact] = (),
    ) -> CIObservationSnapshot:
        return CIObservationSnapshot(
            subject=self.subject,
            request=self.request,
            cycle_id=self.cycle_id,
            invalidation_epoch=self.captured_epoch,
            availability=availability,
            facts=tuple(facts),
            unavailable_reason=unavailable_reason,
            diagnostic_facts=tuple(diagnostic_facts),
        )


@dataclass(frozen=True)
class PublishResult:
    accepted: bool
    snapshot: CIObservationSnapshot
    reason: str | None = None


class CIObservationPhaseStore:
    """Own the single bounded current slot for a read-only controller phase.

    A phase and every read cycle use fresh opaque identities rather than queue
    generations or revision-derived keys.  Ending a phase drops current action
    authority; at most one prior snapshot is retained for diagnostics.
    """

    def __init__(self, *, identity_factory: Callable[[], str] | None = None, initial_epoch: int = 0, persisted_diagnostic: CIObservationSnapshot | None = None) -> None:
        if initial_epoch < 0:
            raise ValueError("Initial invalidation epoch cannot be negative")
        self._identity_factory = identity_factory or (lambda: str(uuid4()))
        self._epoch = initial_epoch
        self._phase_id: str | None = None
        self._subject: ObservationSubject | None = None
        self._request: ObservationRequest | None = None
        self._active_cycle_id: str | None = None
        self._current: CIObservationSnapshot | None = None
        self._diagnostic = persisted_diagnostic
        self._obsolete_diagnostic: CIObservationSnapshot | None = None
        self._lock = threading.Lock()

    @property
    def invalidation_epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def diagnostic_snapshot(self) -> CIObservationSnapshot | None:
        with self._lock:
            return self._diagnostic

    @property
    def obsolete_diagnostic(self) -> CIObservationSnapshot | None:
        """Return at most the most recent fenced completion."""
        with self._lock:
            return self._obsolete_diagnostic

    def begin_phase(self, subject: ObservationSubject, request: ObservationRequest) -> str:
        """Start a new read-only phase, fencing all earlier capabilities."""
        with self._lock:
            self._end_phase_locked()
            self._phase_id = self._new_identity()
            self._subject = subject
            self._request = request
            return self._phase_id

    def begin_read(self, phase_id: str) -> ObservationRead:
        """Start a unique provider read and fence any older in-flight read."""
        with self._lock:
            if phase_id != self._phase_id or self._subject is None or self._request is None:
                raise ValueError("The observation phase is not active")
            cycle_id = self._new_identity()
            self._active_cycle_id = cycle_id
            return ObservationRead(phase_id, cycle_id, self._subject, self._request, self._epoch)

    def publish(self, read: ObservationRead, snapshot: CIObservationSnapshot) -> PublishResult:
        """Publish only if phase, cycle, scope, request, and epoch remain current."""
        with self._lock:
            reason = self._publication_mismatch(read, snapshot)
            if reason is not None:
                obsolete = self._as_superseded(snapshot)
                self._obsolete_diagnostic = obsolete
                return PublishResult(False, obsolete, reason)
            if snapshot.availability in {ObservationAvailability.UNAVAILABLE, ObservationAvailability.THROTTLED} and not snapshot.diagnostic_facts and self._current is not None:
                snapshot = self._with_diagnostic_facts(snapshot, self._current.diagnostic_facts or self._current.facts)
            self._current = snapshot
            self._diagnostic = snapshot
            return PublishResult(True, snapshot)

    def reusable(self, phase_id: str, subject: ObservationSubject, request: ObservationRequest) -> CIObservationSnapshot | None:
        """Return current facts only under the exact active phase capability."""
        with self._lock:
            snapshot = self._current
            if phase_id != self._phase_id or subject != self._subject or request != self._request or snapshot is None or snapshot.invalidation_epoch != self._epoch or snapshot.subject != subject or snapshot.request != request:
                return None
            return snapshot

    def invalidate(self) -> int:
        """Accept relevant invalidation knowledge and fence the active phase."""
        with self._lock:
            self._epoch += 1
            self._end_phase_locked()
            return self._epoch

    def end_phase(self) -> None:
        """End authority before mutation, waits, or external/LLM work."""
        with self._lock:
            self._end_phase_locked()

    def _end_phase_locked(self) -> None:
        if self._current is not None:
            self._diagnostic = self._current
        self._phase_id = None
        self._subject = None
        self._request = None
        self._active_cycle_id = None
        self._current = None

    def _new_identity(self) -> str:
        identity = self._identity_factory()
        if not identity:
            raise ValueError("Identity factory returned an empty identity")
        return identity

    def _publication_mismatch(self, read: ObservationRead, snapshot: CIObservationSnapshot) -> str | None:
        if read.phase_id != self._phase_id:
            return "phase_superseded"
        if read.cycle_id != self._active_cycle_id or snapshot.cycle_id != read.cycle_id:
            return "cycle_superseded"
        if read.captured_epoch != self._epoch or snapshot.invalidation_epoch != read.captured_epoch:
            return "epoch_superseded"
        if read.subject != self._subject or snapshot.subject != read.subject:
            return "subject_mismatch"
        if read.request != self._request or snapshot.request != read.request:
            return "request_mismatch"
        return None

    @staticmethod
    def _as_superseded(snapshot: CIObservationSnapshot) -> CIObservationSnapshot:
        diagnostics = snapshot.diagnostic_facts or snapshot.facts
        return CIObservationSnapshot(
            snapshot.subject,
            snapshot.request,
            snapshot.cycle_id,
            snapshot.invalidation_epoch,
            ObservationAvailability.SUPERSEDED,
            unavailable_reason="completion was fenced by newer observation state",
            diagnostic_facts=diagnostics,
        )

    @staticmethod
    def _with_diagnostic_facts(snapshot: CIObservationSnapshot, diagnostics: tuple[CIObservationFact, ...]) -> CIObservationSnapshot:
        return CIObservationSnapshot(
            snapshot.subject,
            snapshot.request,
            snapshot.cycle_id,
            snapshot.invalidation_epoch,
            snapshot.availability,
            unavailable_reason=snapshot.unavailable_reason,
            diagnostic_facts=diagnostics,
        )
