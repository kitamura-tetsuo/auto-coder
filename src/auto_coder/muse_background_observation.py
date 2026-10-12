"""Conservative MSP evidence folding for Muse background work.

The fold in this module deliberately has no command-producing operations.  It
turns decoded MSP frames into evidence which a controller may persist and use
later; it never interprets silence or a missing live-task row as completion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .local_job_handoff import ContinuationState, LocalJobStore


class MuseTaskOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class MuseContinuationReadiness(str, Enum):
    NATIVE_ACTIVE = "native_active"
    CONTROLLER_READY = "controller_ready"
    NOT_READY = "not_ready"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class MuseObservationBinding:
    backend_name: str
    provider_session: str
    execution_incarnation: str
    upstream_attempt: str
    workspace: Path


@dataclass(frozen=True)
class MuseRuntimeSupport:
    supported: bool
    profile: str = ""
    reason: str = ""
    native_wake: Optional[bool] = None


@dataclass(frozen=True)
class MuseRuntimeConformanceEvidence:
    """Facts observed from one real MSP host lifecycle.

    These fields are deliberately not host-advertised capability flags.  A
    caller may set them only after the named operation/event has crossed the
    wire and the decoder has correlated it to the same session and task.
    """

    task_list_observed: bool = False
    session_replay_observed: bool = False
    task_result_delivery_observed: bool = False
    continuation_ownership: str = ""


@dataclass(frozen=True)
class MuseTaskObservation:
    task_id: str
    turn_id: str
    state: str
    outcome: MuseTaskOutcome = MuseTaskOutcome.UNKNOWN
    result_reference: str = ""
    result_delivered: bool = False
    revision: int = 0
    provenance: str = ""
    contradictory: bool = False


@dataclass(frozen=True)
class MuseBackgroundObservation:
    binding: MuseObservationBinding
    foreground_turn: str
    foreground_state: str = "unknown"
    tasks: tuple[MuseTaskObservation, ...] = ()
    queued_turns: tuple[str, ...] = ()
    active_turns: tuple[str, ...] = ()
    incorporated_results: tuple[str, ...] = ()
    final_assistant: str = ""
    final_assistant_turn: str = ""
    readiness: MuseContinuationReadiness = MuseContinuationReadiness.NOT_READY
    reconciliation_reason: str = ""
    provider_positions: tuple[str, ...] = ()


def decode_muse_msp_frame(raw: bytes) -> dict[str, object]:
    """Decode one MSP JSON line for both production transport and replay tests."""
    try:
        frame = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Muse MSP host emitted an invalid protocol frame") from exc
    if not isinstance(frame, dict):
        raise ValueError("Muse MSP host emitted a non-object protocol frame")
    return frame


def detect_muse_background_support(
    initialized: Mapping[str, object],
    evidence: MuseRuntimeConformanceEvidence,
) -> MuseRuntimeSupport:
    """Earn support from observed runtime behavior, never a fingerprint.

    Muse Code 1.4.6-R6000.1 is the first recorded positive Auto-Coder profile.
    Additive schema fields and fingerprints are intentionally ignored.  Other
    versions can qualify by producing the same decisive observations.
    """
    server = initialized.get("serverInfo")
    version = server.get("version") if isinstance(server, Mapping) else None
    observed = {
        "task/list": evidence.task_list_observed,
        "session replay": evidence.session_replay_observed,
        "task-result delivery": evidence.task_result_delivery_observed,
    }
    missing = tuple(name for name, present in observed.items() if not present)
    if not evidence.continuation_ownership:
        missing += ("continuation ownership",)
    if missing:
        return MuseRuntimeSupport(False, reason="missing conformance observations: " + ", ".join(missing))
    ownership = evidence.continuation_ownership
    if ownership not in ("native", "controller"):
        return MuseRuntimeSupport(False, reason="continuation ownership is ambiguous")
    profile = f"Muse Code {version}" if isinstance(version, str) and version else "Muse MSP capability profile"
    return MuseRuntimeSupport(True, profile=profile, native_wake=ownership == "native")


@dataclass
class MuseObservationFold:
    """Fold decoded MSP notifications and snapshots in receive order."""

    binding: MuseObservationBinding
    foreground_turn: str
    support: MuseRuntimeSupport
    _tasks: dict[str, MuseTaskObservation] = field(default_factory=dict)
    _seen: set[tuple[str, int, str]] = field(default_factory=set)
    _positions: list[str] = field(default_factory=list)
    _queued: set[str] = field(default_factory=set)
    _active: set[str] = field(default_factory=set)
    _incorporated: set[str] = field(default_factory=set)
    _foreground_state: str = "running"
    _final_assistant: str = ""
    _final_assistant_turn: str = ""
    _gap: str = ""

    def consume(self, frame: Mapping[str, object]) -> None:
        """Consume one frame already decoded by the production MSP transport."""
        method = frame.get("method")
        params = frame.get("params")
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return
        session_id = params.get("sessionId")
        if session_id is not None and session_id != self.binding.provider_session:
            return
        cursor = params.get("viewCursor", frame.get("viewCursor", ""))
        position = str(cursor) if isinstance(cursor, (str, int)) else ""
        if position and position not in self._positions:
            self._positions.append(position)
        if method in {"view/gap", "session/viewUnavailable"}:
            self._gap = str(params.get("reason") or method)
        elif method in {"turn/started", "turn/queued"}:
            self._observe_turn(method, params)
        elif method in {"turn/completed", "turn/unqueued"}:
            self._observe_turn_terminal(method, params)
        elif method in {"item/completed", "item/updated"}:
            self._observe_item(params, position)

    def consume_turn_admission(self, result: Mapping[str, object]) -> None:
        """Record a correlated ``turn/start`` acknowledgement as admission only."""
        turn_id = result.get("turnId")
        disposition = result.get("disposition")
        if not isinstance(turn_id, str) or disposition not in {"started", "queued", "steered"}:
            self._gap = "turn admission is invalid or unrecognized"
            return
        if disposition == "queued":
            self._queued.add(turn_id)
        elif disposition == "started":
            self._active.add(turn_id)

    def consume_result_delivery(self, turn_id: str, result_references: Iterable[str], provenance: str) -> None:
        """Record result references proven present at a resumed turn frontier."""
        if not turn_id:
            self._gap = "result delivery omitted turn identity"
            return
        delivered = {reference for reference in result_references if reference}
        self._incorporated.update(delivered)
        for task_id, task in tuple(self._tasks.items()):
            if task.result_reference in delivered and not task.result_delivered:
                self._tasks[task_id] = MuseTaskObservation(
                    task.task_id,
                    task.turn_id,
                    task.state,
                    task.outcome,
                    task.result_reference,
                    True,
                    task.revision,
                    task.provenance,
                    task.contradictory,
                )
        if provenance and provenance not in self._positions:
            self._positions.append(provenance)

    def consume_task_inventory(self, rows: Optional[Iterable[Mapping[str, object]]], provenance: str) -> None:
        """Fold live ``task/list`` rows; absence never terminalizes known work."""
        if rows is None:
            self._gap = "task/list unavailable"
            return
        seen: set[str] = set()
        for row in rows:
            task_id = row.get("taskId", row.get("id"))
            if not isinstance(task_id, str) or not task_id:
                self._gap = "task/list contained an unrecognized workload"
                continue
            seen.add(task_id)
            self._observe_task("task/updated", row, provenance)
        missing = [task_id for task_id, task in self._tasks.items() if task.state == "running" and task_id not in seen]
        if missing:
            self._gap = "live task disappeared without attributable terminal evidence: " + ", ".join(sorted(missing))

    def consume_session_snapshot(self, snapshot: Mapping[str, object], provenance: str) -> None:
        if snapshot.get("sessionId") != self.binding.provider_session:
            self._gap = "snapshot identity mismatch"
            return
        if snapshot.get("gap"):
            self._gap = str(snapshot.get("gap"))
        raw_turns = snapshot.get("turns")
        turns = raw_turns if isinstance(raw_turns, list) else []
        for turn in turns:
            if isinstance(turn, Mapping):
                state = turn.get("state")
                self.consume({"method": f"turn/{'started' if state == 'running' else state}", "params": {**turn, "sessionId": self.binding.provider_session, "viewCursor": provenance}})

    def observation(self) -> MuseBackgroundObservation:
        tasks = tuple(sorted(self._tasks.values(), key=lambda task: task.task_id))
        outstanding = any(task.state != "terminal" or not task.result_delivered or (task.result_reference and task.result_reference not in self._incorporated) for task in tasks)
        contradictory = any(task.contradictory for task in tasks)
        if not self.support.supported:
            readiness = MuseContinuationReadiness.UNSUPPORTED
            reason = self.support.reason
        elif self._gap or contradictory:
            readiness = MuseContinuationReadiness.RECONCILIATION_REQUIRED
            reason = self._gap or "contradictory terminal task evidence"
        elif self._queued or any(turn != self.foreground_turn for turn in self._active):
            readiness = MuseContinuationReadiness.NATIVE_ACTIVE
            reason = ""
        elif outstanding or self._foreground_state == "running":
            readiness = MuseContinuationReadiness.NOT_READY
            reason = ""
        elif self.support.native_wake is False:
            readiness = MuseContinuationReadiness.CONTROLLER_READY
            reason = ""
        else:
            readiness = MuseContinuationReadiness.NOT_READY
            reason = "native runtime owns continuation"
        final = self._final_assistant
        if outstanding or self._gap or self._final_assistant_turn not in ({self.foreground_turn} | self._active | self._queued):
            final = ""
        return MuseBackgroundObservation(
            self.binding,
            self.foreground_turn,
            self._foreground_state,
            tasks,
            tuple(sorted(self._queued)),
            tuple(sorted(self._active)),
            tuple(sorted(self._incorporated)),
            final,
            self._final_assistant_turn,
            readiness,
            reason,
            tuple(self._positions),
        )

    def _observe_turn(self, method: str, params: Mapping[str, object]) -> None:
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            self._gap = "turn event omitted identity"
            return
        (self._queued if method == "turn/queued" else self._active).add(turn_id)
        if method == "turn/started":
            self._queued.discard(turn_id)
        if turn_id == self.foreground_turn:
            self._foreground_state = "running"

    def _observe_turn_terminal(self, method: str, params: Mapping[str, object]) -> None:
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str):
            self._gap = "terminal turn event omitted identity"
            return
        self._active.discard(turn_id)
        self._queued.discard(turn_id)
        if turn_id == self.foreground_turn:
            self._foreground_state = str(params.get("terminal") or ("unqueued" if method == "turn/unqueued" else "unknown"))

    def _observe_task(self, method: str, params: Mapping[str, object], provenance: str) -> None:
        task = params.get("task") if isinstance(params.get("task"), Mapping) else params
        assert isinstance(task, Mapping)
        task_id = task.get("taskId", task.get("itemId", task.get("id")))
        old = self._tasks.get(task_id) if isinstance(task_id, str) else None
        turn_id = task.get("turnId")
        if not isinstance(task_id, str) or not task_id:
            self._gap = "task evidence omitted task identity"
            return
        if not isinstance(turn_id, str) or not turn_id:
            # Published task/list rows intentionally omit turn identity. Join a
            # previously item-visible row; retain registry-only work with an
            # explicit unknown lineage and keep it non-authorizing.
            turn_id = old.turn_id if old is not None else "unknown"
            if old is None:
                self._gap = f"task {task_id} has no attributable turn identity"
        revision = task.get("revision", 0)
        if type(revision) is not int or revision < 0:
            self._gap = f"task {task_id} has invalid revision"
            return
        state_value = task.get("state", task.get("status", "running"))
        terminal = method in {"task/completed", "task/terminal", "item/completed"} or state_value not in {"inProgress", "running"}
        raw_outcome = task.get("outcome", state_value if terminal else "unknown")
        aliases = {
            "success": MuseTaskOutcome.SUCCESS,
            "completed": MuseTaskOutcome.SUCCESS,
            "succeeded": MuseTaskOutcome.SUCCESS,
            "failure": MuseTaskOutcome.FAILURE,
            "failed": MuseTaskOutcome.FAILURE,
            "cancelled": MuseTaskOutcome.CANCELLED,
            "interrupted": MuseTaskOutcome.INTERRUPTED,
        }
        outcome = aliases.get(str(raw_outcome), MuseTaskOutcome.UNKNOWN) if terminal else MuseTaskOutcome.UNKNOWN
        result = task.get("resultReference", task.get("resultId", ""))
        result_reference = result if isinstance(result, str) else ""
        if terminal and not result_reference:
            result_reference = f"{task_id}@{revision}"
        delivered = bool(task.get("resultDelivered", False))
        key = (task_id, revision, provenance)
        if key in self._seen:
            return
        self._seen.add(key)
        if old is not None and revision < old.revision:
            return
        contradictory = old.contradictory if old else False
        if old is not None and old.state == "terminal" and terminal and old.outcome != outcome:
            contradictory = True
        if old is not None and old.state == "terminal" and not terminal:
            return
        self._tasks[task_id] = MuseTaskObservation(task_id, turn_id, "terminal" if terminal else "running", outcome, result_reference, delivered, revision, provenance, contradictory)

    def _observe_item(self, params: Mapping[str, object], provenance: str) -> None:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return
        kind = item.get("kind")
        turn_id = item.get("turnId")
        if kind in {"tool", "toolCall"} and item.get("background") is True:
            self._observe_task("item/completed" if params.get("item") is item and item.get("status") != "inProgress" else "item/updated", item, provenance)
        elif kind in {"message", "agentMessage"} and isinstance(turn_id, str) and isinstance(item.get("text"), str):
            self._final_assistant = str(item["text"])
            self._final_assistant_turn = turn_id


def persist_muse_observation(
    store: LocalJobStore,
    job_id: str,
    executor_token: str,
    observation: MuseBackgroundObservation,
) -> bool:
    """Persist one normalized observation through #2480's fenced boundary."""
    job = store.get(job_id)
    continuation = store.get_continuation(job_id)
    if job is None or continuation is None:
        return False
    binding = observation.binding
    if (
        continuation.executor_token != executor_token
        or continuation.provider_session != binding.provider_session
        or continuation.execution_incarnation != binding.execution_incarnation
        or job.backend_name != binding.backend_name
        or job.upstream_attempt != binding.upstream_attempt
        or Path(job.workspace_path).resolve() != binding.workspace.resolve()
    ):
        return False
    for task in observation.tasks:
        retained = store.observe_continuation_task(
            job_id,
            executor_token,
            task.task_id,
            task.state,
            terminal_outcome=task.outcome.value if task.state == "terminal" else "",
            result_reference=task.result_reference,
            provider_position=task.provenance,
        )
        if retained is None:
            return False
    current = store.get_continuation(job_id)
    if current is None:
        return False
    desired = {
        MuseContinuationReadiness.NATIVE_ACTIVE: ContinuationState.CONTINUATION_PENDING,
        MuseContinuationReadiness.CONTROLLER_READY: ContinuationState.CONTINUATION_PENDING,
        MuseContinuationReadiness.NOT_READY: ContinuationState.WAITING_BACKGROUND,
        MuseContinuationReadiness.RECONCILIATION_REQUIRED: ContinuationState.RECONCILIATION_REQUIRED,
        MuseContinuationReadiness.UNSUPPORTED: ContinuationState.RECONCILIATION_REQUIRED,
    }[observation.readiness]
    if current.state is not desired and not store.transition_continuation(
        job_id,
        executor_token,
        current.state,
        desired,
        provider_position=observation.provider_positions[-1] if observation.provider_positions else "",
    ):
        return False
    return True
