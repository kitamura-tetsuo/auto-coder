"""Conservative MSP evidence folding for Muse background work.

The fold in this module deliberately has no command-producing operations.  It
turns decoded MSP frames into evidence which a controller may persist and use
later; it never interprets silence or a missing live-task row as completion.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .local_job_handoff import ContinuationState, LocalJobClaim, LocalJobRecord, LocalJobStore


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


@dataclass(frozen=True)
class MuseJobObservationContext:
    store: LocalJobStore
    job: LocalJobRecord
    executor_token: str


_CURRENT_MUSE_JOB: contextvars.ContextVar[Optional[MuseJobObservationContext]] = contextvars.ContextVar("auto_coder_muse_job_observation", default=None)


@contextlib.contextmanager
def bind_muse_job_observation(store: LocalJobStore, job: LocalJobRecord, executor_token: str):
    """Bind the accepted durable job whose Muse transport may emit evidence."""
    token = _CURRENT_MUSE_JOB.set(MuseJobObservationContext(store, job, executor_token))
    try:
        yield
    finally:
        _CURRENT_MUSE_JOB.reset(token)


def current_muse_job_observation() -> Optional[MuseJobObservationContext]:
    return _CURRENT_MUSE_JOB.get()


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
    _inventory_tracked_tasks: set[str] = field(default_factory=set)
    _seen: set[tuple[str, int, str]] = field(default_factory=set)
    _positions: list[str] = field(default_factory=list)
    _queued: set[str] = field(default_factory=set)
    _active: set[str] = field(default_factory=set)
    _terminal_turns: set[str] = field(default_factory=set)
    _completed_turns: set[str] = field(default_factory=set)
    _known_turns: set[str] = field(default_factory=set)
    _incorporated: set[str] = field(default_factory=set)
    _turn_incorporated: dict[str, set[str]] = field(default_factory=dict)
    _foreground_state: str = "running"
    _assistant_candidates: dict[str, str] = field(default_factory=dict)
    _candidate_order: list[str] = field(default_factory=list)
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
            if turn_id not in self._terminal_turns:
                self._queued.add(turn_id)
        elif disposition == "started":
            if turn_id not in self._terminal_turns:
                self._active.add(turn_id)
        self._known_turns.add(turn_id)

    def consume_result_delivery(self, turn_id: str, result_references: Iterable[str], provenance: str) -> None:
        """Record result references proven present at a resumed turn frontier."""
        if not turn_id:
            self._gap = "result delivery omitted turn identity"
            return
        delivered = {reference for reference in result_references if reference}
        self._incorporated.update(delivered)
        self._turn_incorporated.setdefault(turn_id, set()).update(delivered)
        self._known_turns.add(turn_id)
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
            self._inventory_tracked_tasks.add(task_id)
            self._observe_task("task/updated", row, provenance)
        missing = [task_id for task_id, task in self._tasks.items() if task.state == "running" and task_id in self._inventory_tracked_tasks and task_id not in seen]
        if missing:
            self._gap = "live task disappeared without attributable terminal evidence: " + ", ".join(sorted(missing))

    def consume_session_snapshot(self, snapshot: Mapping[str, object], provenance: str) -> None:
        session = snapshot.get("session")
        session_id = session.get("sessionId") if isinstance(session, Mapping) else snapshot.get("sessionId")
        if session_id != self.binding.provider_session:
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
        history = snapshot.get("history")
        if isinstance(history, Mapping) and history.get("mode") == "inline":
            items = history.get("items")
            if not isinstance(items, list):
                self._gap = "inline session replay omitted items"
                return
            for item in items:
                if isinstance(item, Mapping):
                    self._observe_item({"item": item}, provenance)
            last_turn = snapshot.get("lastTurn")
            if isinstance(last_turn, Mapping) and isinstance(last_turn.get("turnId"), str):
                self._observe_turn_terminal("turn/completed", last_turn)
            # An inline read without a continuation cursor is an authoritative
            # complete view. Overlapping items are revision-deduplicated.
            if snapshot.get("nextCursor") in (None, ""):
                self._gap = ""
        if provenance and provenance not in self._positions:
            self._positions.append(provenance)

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
        final = ""
        final_turn = self._candidate_order[-1] if self._candidate_order else ""
        required_results = {task.result_reference for task in tasks if task.result_reference}
        candidate_frontier = self._turn_incorporated.get(final_turn, set())
        if not outstanding and not self._gap and not self._active and not self._queued and final_turn in self._completed_turns and final_turn in self._known_turns and required_results <= candidate_frontier:
            final = self._assistant_candidates.get(final_turn, "")
        return MuseBackgroundObservation(
            self.binding,
            self.foreground_turn,
            self._foreground_state,
            tasks,
            tuple(sorted(self._queued)),
            tuple(sorted(self._active)),
            tuple(sorted(self._incorporated)),
            final,
            final_turn,
            readiness,
            reason,
            tuple(self._positions),
        )

    def _observe_turn(self, method: str, params: Mapping[str, object]) -> None:
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            self._gap = "turn event omitted identity"
            return
        self._known_turns.add(turn_id)
        if turn_id in self._terminal_turns:
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
        self._known_turns.add(turn_id)
        self._terminal_turns.add(turn_id)
        if method == "turn/completed" and params.get("terminal") in {None, "completed"}:
            self._completed_turns.add(turn_id)
            candidate = self._assistant_candidates.get(turn_id)
            if candidate is not None and any(line.strip().startswith("ACTION_SUMMARY:") or line.strip() == "CANNOT_FIX" for line in candidate.splitlines()):
                for task_id, task in tuple(self._tasks.items()):
                    if task.turn_id == turn_id and task.state == "running" and task_id not in self._inventory_tracked_tasks:
                        reference = task.result_reference or f"{task_id}@{task.revision}"
                        self._incorporated.add(reference)
                        self._turn_incorporated.setdefault(turn_id, set()).add(reference)
                        self._tasks[task_id] = MuseTaskObservation(
                            task.task_id,
                            task.turn_id,
                            "terminal",
                            MuseTaskOutcome.CANCELLED,
                            reference,
                            True,
                            task.revision,
                            task.provenance,
                            task.contradictory,
                        )
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
            "succeeded": MuseTaskOutcome.SUCCESS,
            "failure": MuseTaskOutcome.FAILURE,
            "failed": MuseTaskOutcome.FAILURE,
            "cancelled": MuseTaskOutcome.CANCELLED,
            "interrupted": MuseTaskOutcome.INTERRUPTED,
        }
        outcome = aliases.get(str(raw_outcome), MuseTaskOutcome.UNKNOWN) if terminal else MuseTaskOutcome.UNKNOWN
        if terminal and str(raw_outcome) == "completed":
            exit_code = task.get("exitCode")
            result_payload = task.get("result")
            if not isinstance(exit_code, int) and isinstance(result_payload, Mapping):
                exit_code = result_payload.get("exitCode")
            if not isinstance(exit_code, int) and isinstance(task.get("visibleOutput"), str):
                try:
                    output_payload = json.loads(str(task["visibleOutput"]))
                    if isinstance(output_payload, Mapping):
                        exit_code = output_payload.get("exit_code")
                except json.JSONDecodeError:
                    pass
            if isinstance(exit_code, int):
                outcome = MuseTaskOutcome.SUCCESS if exit_code == 0 else MuseTaskOutcome.FAILURE
        result = task.get("resultReference", task.get("resultId", ""))
        result_reference = result if isinstance(result, str) else ""
        if terminal and not result_reference:
            result_reference = f"{task_id}@{revision}"
        in_turn_unified_delivery = terminal and task_id not in self._inventory_tracked_tasks and turn_id in self._active
        if in_turn_unified_delivery and result_reference:
            self._incorporated.add(result_reference)
            self._turn_incorporated.setdefault(turn_id, set()).add(result_reference)
        delivered = bool(task.get("resultDelivered", False)) or in_turn_unified_delivery or (old is not None and old.result_delivered and old.result_reference == result_reference) or (bool(result_reference) and result_reference in self._incorporated)
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

    def _is_background_tool_item(self, item: Mapping[str, object]) -> bool:
        task_id = item.get("taskId", item.get("itemId", item.get("id")))
        if item.get("background") is True:
            if isinstance(task_id, str) and task_id:
                self._inventory_tracked_tasks.add(task_id)
            return True
        if isinstance(task_id, str) and task_id in self._tasks:
            return True
        if item.get("status") not in {"inProgress", "running"}:
            return False
        bg_status = item.get("backgroundStatus")
        if isinstance(bg_status, Mapping) and bg_status.get("state") in {"backgrounded", "running", "inProgress"}:
            return True
        visible_output = item.get("visibleOutput")
        if isinstance(visible_output, str) and "background_running" in visible_output:
            try:
                payload = json.loads(visible_output)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, Mapping) and payload.get("execution_state") == "background_running":
                return True
        return False

    def _observe_item(self, params: Mapping[str, object], provenance: str) -> None:
        item = params.get("item")
        if not isinstance(item, Mapping):
            return
        kind = item.get("kind")
        turn_id = item.get("turnId")
        if kind in {"tool", "toolCall"} and self._is_background_tool_item(item):
            self._observe_task("item/completed" if params.get("item") is item and item.get("status") != "inProgress" else "item/updated", item, provenance)
        elif kind in {"message", "agentMessage"} and isinstance(turn_id, str) and isinstance(item.get("text"), str):
            if item.get("status") in {"inProgress", "running"}:
                return
            self._known_turns.add(turn_id)
            if str(item["text"]).strip():
                self._assistant_candidates[turn_id] = str(item["text"])
                if turn_id in self._candidate_order:
                    self._candidate_order.remove(turn_id)
                self._candidate_order.append(turn_id)


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
        MuseContinuationReadiness.NATIVE_ACTIVE: ContinuationState.NATIVE_CONTINUATION_ACTIVE,
        MuseContinuationReadiness.CONTROLLER_READY: ContinuationState.CONTROLLER_READY,
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
    payload = json.dumps(
        {
            "binding": {
                "backend_name": binding.backend_name,
                "provider_session": binding.provider_session,
                "execution_incarnation": binding.execution_incarnation,
                "upstream_attempt": binding.upstream_attempt,
                "workspace": str(binding.workspace),
            },
            "foreground_turn": observation.foreground_turn,
            "foreground_state": observation.foreground_state,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "turn_id": task.turn_id,
                    "state": task.state,
                    "outcome": task.outcome.value,
                    "result_reference": task.result_reference,
                    "result_delivered": task.result_delivered,
                    "revision": task.revision,
                    "provenance": task.provenance,
                    "contradictory": task.contradictory,
                }
                for task in observation.tasks
            ],
            "queued_turns": observation.queued_turns,
            "active_turns": observation.active_turns,
            "incorporated_results": observation.incorporated_results,
            "final_assistant": observation.final_assistant,
            "final_assistant_turn": observation.final_assistant_turn,
            "readiness": observation.readiness.value,
            "reconciliation_reason": observation.reconciliation_reason,
            "provider_positions": observation.provider_positions,
        },
        sort_keys=True,
    )
    return store.persist_continuation_observation(job_id, executor_token, payload)


def recover_muse_observation(store: LocalJobStore, job_id: str) -> Optional[MuseBackgroundObservation]:
    """Recover the exact normalized Muse evidence persisted for a continuation."""
    payload = store.get_continuation_observation(job_id)
    if not payload:
        return None
    try:
        raw = json.loads(payload)
        binding = raw["binding"]
        return MuseBackgroundObservation(
            MuseObservationBinding(
                str(binding["backend_name"]),
                str(binding["provider_session"]),
                str(binding["execution_incarnation"]),
                str(binding["upstream_attempt"]),
                Path(str(binding["workspace"])),
            ),
            str(raw["foreground_turn"]),
            str(raw["foreground_state"]),
            tuple(
                MuseTaskObservation(
                    str(task["task_id"]),
                    str(task["turn_id"]),
                    str(task["state"]),
                    MuseTaskOutcome(str(task["outcome"])),
                    str(task["result_reference"]),
                    bool(task["result_delivered"]),
                    int(task["revision"]),
                    str(task["provenance"]),
                    bool(task["contradictory"]),
                )
                for task in raw["tasks"]
            ),
            tuple(str(value) for value in raw["queued_turns"]),
            tuple(str(value) for value in raw["active_turns"]),
            tuple(str(value) for value in raw["incorporated_results"]),
            str(raw["final_assistant"]),
            str(raw["final_assistant_turn"]),
            MuseContinuationReadiness(str(raw["readiness"])),
            str(raw["reconciliation_reason"]),
            tuple(str(value) for value in raw["provider_positions"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
