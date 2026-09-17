"""Invocation-level shutdown protection and atomic LLM admission.

This module defines a daemon-instance-scoped model that protects individual
controller-initiated LLM invocations (an inference/agent-run request capable
of consuming provider tokens or quota, including a subscription-backed local
CLI invocation or the local submission of such work to an asynchronous
remote provider) from being silently dropped by a graceful shutdown, while
still letting the shutdown drain proceed once every invocation admitted
before the gate closed has reached its durable checkpoint.

Queueing, prompt preparation, cache lookups, version/authentication/quota
probes, ordinary GitHub operations, and maintenance work are not qualifying
invocations by themselves and must not be admitted through this gate. A
running agent CLI's internal tool/model loop belongs to the one invocation
that launched it; it does not authorize a second, independently admitted
invocation.

Wiring real callers and durable result/handoff checkpoints into production
code paths, and retiring the broader shutdown wait this model narrows, are
separate follow-up stages (see Issue #2009 and #2010). This module only
defines the standalone lifecycle model and its concurrency contract.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterator, List, Optional, Set, Tuple


class GateState(str, Enum):
    """Observable admission state of a single daemon lifetime's gate."""

    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FORCED = "forced"


class InvocationState(str, Enum):
    """Lifecycle state of one admitted invocation."""

    IN_FLIGHT = "in_flight"
    CHECKPOINTING = "checkpointing"
    SETTLED = "settled"


class DrainOutcome(str, Enum):
    """Distinguishes a fully-settled drain from an abandoned, forced one."""

    GRACEFUL = "graceful"
    FORCED = "forced"


class InvocationStateError(RuntimeError):
    """A caller attempted an invalid state transition for its own invocation."""


@dataclass(frozen=True)
class InvocationIdentity:
    """Identity of a single admitted invocation.

    Distinguishes concurrent and successive calls, including calls sharing
    the same repository/target/stage, via a unique ``invocation_id``.
    """

    daemon_scope: str
    repository: str
    target: str
    stage: str
    invocation_id: str


@dataclass
class _InvocationRecord:
    identity: InvocationIdentity
    state: InvocationState
    admitted_at: float
    updated_at: float
    checkpoint_failure_count: int = 0
    last_note: str = ""
    last_error: str = ""
    confirmation_id: Optional[str] = None


@dataclass(frozen=True)
class UnsettledInvocationSnapshot:
    """Read-only view of one not-yet-settled invocation, for observability."""

    invocation_id: str
    daemon_scope: str
    repository: str
    target: str
    stage: str
    state: InvocationState
    admitted_at: float
    updated_at: float
    checkpoint_failure_count: int


@dataclass(frozen=True)
class GateSnapshot:
    """Read-only view of the gate's current lifecycle state."""

    state: GateState
    is_graceful_ready: bool
    unsettled: Tuple[UnsettledInvocationSnapshot, ...]


class InvocationHandle:
    """Caller-held handle for one admitted invocation.

    Returned only on successful admission; there is no way to construct one
    outside :meth:`InvocationAdmissionGate.try_admit`, so holding a handle is
    itself evidence that admission and registration already happened
    atomically before the caller's controlled provider action can run.
    """

    __slots__ = ("_gate", "identity")

    def __init__(self, gate: "InvocationAdmissionGate", identity: InvocationIdentity) -> None:
        self._gate = gate
        self.identity = identity

    def begin_checkpointing(self, outcome: str = "") -> None:
        """Record that the local provider call returned or failed.

        Moves the invocation from IN_FLIGHT to CHECKPOINTING: its result,
        terminal failure, or remote-handoff outcome now awaits the owning
        caller's durable checkpoint. ``outcome`` is a free-form label (e.g.
        "result", "terminal_failure", "remote_handoff") kept only for
        observability.
        """
        self._gate._begin_checkpointing(self.identity.invocation_id, outcome)

    def record_checkpoint_attempt_failed(self, error: str) -> None:
        """Record a failed persistence attempt without changing state.

        The invocation stays CHECKPOINTING and protected; persistence may be
        retried without admitting another inference.
        """
        self._gate._record_checkpoint_attempt_failed(self.identity.invocation_id, error)

    def confirm_settled(self, confirmation_id: Optional[str] = None) -> bool:
        """Confirm this invocation's checkpoint committed durably.

        Returns True the first time this exact invocation settles, and False
        for any later duplicate call. Only ever affects this invocation.
        """
        return self._gate.confirm_settled(self.identity.invocation_id, confirmation_id)


class InvocationAdmissionGate:
    """Daemon-instance-scoped shutdown-protection model for LLM invocations.

    Each daemon lifetime owns exactly one instance. A later daemon lifetime
    must construct a new instance; it never reuses or regains authority from
    an earlier instance's handles or invocation identifiers.
    """

    def __init__(self, daemon_scope: Optional[str] = None) -> None:
        self.daemon_scope = daemon_scope or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._state = GateState.RUNNING
        self._all: Dict[str, _InvocationRecord] = {}
        self._active: Set[str] = set()
        self.close_reason: str = ""
        self.force_reason: str = ""

    @property
    def state(self) -> GateState:
        with self._lock:
            return self._state

    def try_admit(self, *, repository: str, target: str, stage: str) -> Optional[InvocationHandle]:
        """Atomically admit and register a new invocation, or refuse.

        Returns a handle when the gate is RUNNING at the moment this call is
        made; the invocation is registered before this call returns, so a
        concurrent drain either observes it as protected or has already
        closed admission and this call returns None instead. A DRAINING,
        STOPPED, or FORCED gate always refuses without ever registering the
        invocation, so the caller's controlled provider action must not run.
        """
        with self._lock:
            if self._state is not GateState.RUNNING:
                return None
            invocation_id = uuid.uuid4().hex
            identity = InvocationIdentity(
                daemon_scope=self.daemon_scope,
                repository=repository,
                target=target,
                stage=stage,
                invocation_id=invocation_id,
            )
            now = time.monotonic()
            self._all[invocation_id] = _InvocationRecord(
                identity=identity,
                state=InvocationState.IN_FLIGHT,
                admitted_at=now,
                updated_at=now,
            )
            self._active.add(invocation_id)
            return InvocationHandle(self, identity)

    def _begin_checkpointing(self, invocation_id: str, outcome: str) -> None:
        with self._lock:
            record = self._all.get(invocation_id)
            if record is None:
                raise InvocationStateError(f"unknown invocation {invocation_id}")
            if record.state is not InvocationState.IN_FLIGHT:
                raise InvocationStateError(f"cannot begin checkpointing invocation {invocation_id} from state {record.state}")
            record.state = InvocationState.CHECKPOINTING
            record.last_note = outcome
            record.updated_at = time.monotonic()

    def _record_checkpoint_attempt_failed(self, invocation_id: str, error: str) -> None:
        with self._lock:
            record = self._all.get(invocation_id)
            if record is None:
                raise InvocationStateError(f"unknown invocation {invocation_id}")
            if record.state is not InvocationState.CHECKPOINTING:
                raise InvocationStateError(f"cannot record a checkpoint failure for invocation {invocation_id} in state {record.state}")
            record.checkpoint_failure_count += 1
            record.last_error = error
            record.updated_at = time.monotonic()

    def confirm_settled(self, invocation_id: str, confirmation_id: Optional[str] = None) -> bool:
        """Confirm the durable checkpoint for one invocation committed.

        Only settles an invocation that is currently CHECKPOINTING in this
        gate. An unknown id, a duplicate confirmation for an already-SETTLED
        invocation, and a confirmation naming an invocation that is still
        IN_FLIGHT (skipping the checkpoint step) are all safe no-ops that
        return False; none of them ever settles a different invocation or
        grants execution authority.
        """
        with self._lock:
            record = self._all.get(invocation_id)
            if record is None:
                return False
            if record.state is not InvocationState.CHECKPOINTING:
                return False
            record.state = InvocationState.SETTLED
            record.confirmation_id = confirmation_id
            record.updated_at = time.monotonic()
            self._active.discard(invocation_id)
            return True

    def close_admission(self, reason: str = "") -> bool:
        """Close admission. Returns True for the first call that closes it."""
        with self._lock:
            if self._state is not GateState.RUNNING:
                return False
            self._state = GateState.DRAINING
            self.close_reason = reason
            return True

    def mark_stopped(self) -> bool:
        """Move DRAINING to STOPPED once every admitted invocation settled."""
        with self._lock:
            if self._state is GateState.DRAINING and not self._active:
                self._state = GateState.STOPPED
                return True
            return False

    def force_stop(self, reason: str = "") -> None:
        """Abandon the graceful wait. Never fabricates a settled result."""
        with self._lock:
            self._state = GateState.FORCED
            self.force_reason = reason

    @property
    def is_graceful_ready(self) -> bool:
        """True exactly when every invocation admitted before closure settled.

        False while RUNNING (admission has not closed), and false while
        FORCED (the wait was abandoned rather than gracefully completed),
        regardless of how many invocations happen to remain active.
        """
        with self._lock:
            if self._state in (GateState.RUNNING, GateState.FORCED):
                return False
            return not self._active

    def unsettled_snapshot(self) -> List[UnsettledInvocationSnapshot]:
        with self._lock:
            snapshots = []
            for invocation_id in sorted(self._active):
                record = self._all[invocation_id]
                snapshots.append(
                    UnsettledInvocationSnapshot(
                        invocation_id=invocation_id,
                        daemon_scope=record.identity.daemon_scope,
                        repository=record.identity.repository,
                        target=record.identity.target,
                        stage=record.identity.stage,
                        state=record.state,
                        admitted_at=record.admitted_at,
                        updated_at=record.updated_at,
                        checkpoint_failure_count=record.checkpoint_failure_count,
                    )
                )
            return snapshots

    def snapshot(self) -> GateSnapshot:
        with self._lock:
            return GateSnapshot(
                state=self._state,
                is_graceful_ready=self.is_graceful_ready,
                unsettled=tuple(self.unsettled_snapshot()),
            )

    async def wait_until_drained(self, poll_interval: float = 0.01) -> DrainOutcome:
        """Wait for a graceful drain, or return early once forced.

        Only meaningful after :meth:`close_admission`; while RUNNING this
        polls indefinitely since admission has not closed yet.
        """
        while True:
            with self._lock:
                if self._state is GateState.FORCED:
                    return DrainOutcome.FORCED
                if self._state in (GateState.DRAINING, GateState.STOPPED) and not self._active:
                    return DrainOutcome.GRACEFUL
            await asyncio.sleep(poll_interval)


_current_gate: ContextVar[Optional[InvocationAdmissionGate]] = ContextVar("auto_coder_invocation_gate", default=None)


def install_invocation_gate(gate: InvocationAdmissionGate) -> Token[Optional[InvocationAdmissionGate]]:
    """Install this daemon lifetime's gate in the current operation context."""
    return _current_gate.set(gate)


def reset_invocation_gate(token: Token[Optional[InvocationAdmissionGate]]) -> None:
    _current_gate.reset(token)


def current_invocation_gate() -> Optional[InvocationAdmissionGate]:
    """Return the ambient gate for this context, or None outside any daemon.

    A standalone, non-daemon command that never installs a gate always sees
    None here, never a previously closed daemon's gate; the value only ever
    comes from an explicit :func:`install_invocation_gate` call on this
    context, so a new daemon lifetime's context is isolated from an earlier
    one even when the thread running it is reused.
    """
    return _current_gate.get()


@dataclass(frozen=True)
class InvocationTarget:
    """Caller-supplied classification for the next admitted invocation.

    ``defer_checkpoint`` tells the production boundary in
    ``backend_manager.py`` that this caller owns a durable checkpoint step of
    its own (e.g. persisting a validation decision, or recording a remote
    handoff receipt) and will retrieve the admitted handle via
    :func:`take_pending_invocation_handle` to confirm settlement itself once
    that write commits. When false (the default), a successful invocation is
    settled immediately after the provider call returns, because the
    response flows synchronously to a caller that has no separate durable
    write to wait for.
    """

    repository: str
    target: str
    stage: str
    defer_checkpoint: bool = False


_current_target: ContextVar[Optional[InvocationTarget]] = ContextVar("auto_coder_invocation_target", default=None)


@contextmanager
def bind_invocation_target(repository: str, target: str, stage: str, defer_checkpoint: bool = False) -> Iterator[None]:
    """Classify the invocation(s) made by the production boundary in this block."""
    token = _current_target.set(InvocationTarget(repository=repository, target=target, stage=stage, defer_checkpoint=defer_checkpoint))
    try:
        yield
    finally:
        _current_target.reset(token)


def current_invocation_target() -> Optional[InvocationTarget]:
    """Return the ambient invocation classification, or None if unbound."""
    return _current_target.get()


_pending_handle: ContextVar[Optional[InvocationHandle]] = ContextVar("auto_coder_pending_invocation_handle", default=None)


def set_pending_invocation_handle(handle: Optional[InvocationHandle]) -> None:
    """Stash a checkpointing handle for its owning caller to confirm later.

    Only meaningful for a caller that bound ``defer_checkpoint=True``; the
    production boundary calls this instead of settling the invocation
    itself, and the caller retrieves it with
    :func:`take_pending_invocation_handle` once its own durable write
    commits.
    """
    _pending_handle.set(handle)


def take_pending_invocation_handle() -> Optional[InvocationHandle]:
    """Retrieve and clear the pending handle left by the last deferred call.

    Returns None if no invocation is currently awaiting this caller's own
    checkpoint confirmation (e.g. no gate is installed, or the last call did
    not defer its checkpoint).
    """
    handle = _pending_handle.get()
    if handle is not None:
        _pending_handle.set(None)
    return handle
