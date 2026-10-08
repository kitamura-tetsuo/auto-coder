"""Bounded, metadata-only diagnostics for one Muse MSP invocation.

The observer records what the controller has actually observed on its pipes to
the local Muse host.  It never takes part in execution decisions and every
public method isolates its own failures, so diagnostics can neither replace the
primary error nor change the result.  Direct provider (host-to-Meta) transport
progress is never observed here and is reported as ``unobserved``.
"""

from __future__ import annotations

import functools
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from .logger_config import get_logger
from .security_utils import redact_string

logger = get_logger(__name__)

MARKER = "Muse diagnostic: "
SCHEMA_VERSION = 1
HEARTBEAT_INTERVAL_SECONDS = 30.0
MAX_DISPLAY_CHARS = 128
MAX_PAYLOAD_BYTES = 8192
_TRUNCATION = "...[truncated]"
_MAX_SECONDARY_FAILURES = 4

PHASES = (
    "preparation",
    "host_startup",
    "initialization",
    "session_start_resume",
    "approval_mode_change",
    "turn_submission",
    "turn_terminal_wait",
    "post_terminal_host_exit_wait",
    "writer_settlement",
    "result_validation",
)
_RECOGNIZED_METHODS = frozenset(
    {
        "initialize",
        "initialized",
        "session/start",
        "session/resume",
        "session/setApprovalMode",
        "turn/start",
        "turn/completed",
        "item/updated",
        "item/completed",
        "approval/request",
        "approval/requested",
        "approval/updated",
        "approval/resolved",
        "userInput/requested",
    }
)
_TERMINALS = frozenset({"completed", "failed", "cancelled"})
_OBSERVATION_BOUNDARY = {
    "observed": "controller<->local Muse host MSP pipes",
    "provider_transport": "unobserved",
    "note": "silence, byte counts, an alive PID or a heartbeat do not establish provider, model or CLI state",
}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def display(value: object, limit: int = MAX_DISPLAY_CHARS) -> str:
    """Sanitize a metadata string for display only; never use it for comparisons."""
    text = redact_string(str(value))
    text = _CONTROL_CHARS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)
    if len(text) > limit:
        text = text[: limit - len(_TRUNCATION)] + _TRUNCATION
    return text


def rpc_id_label(direction: str, value: object) -> dict[str, object]:
    """Describe a typed JSON-RPC id without conflating integers and strings."""
    kind = "int" if type(value) is int else "str" if type(value) is str else type(value).__name__
    return {"direction": direction, "type": kind, "value": display(value) if type(value) is str else value if type(value) is int else None}


def _safe(default: Any = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Isolate observation failures from the observed execution."""

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def wrapper(self: "MuseInvocationObserver", *args: Any, **kwargs: Any) -> Any:
            if not self.enabled:
                return default
            try:
                return method(self, *args, **kwargs)
            except Exception:  # noqa: BLE001 - diagnostics must never alter execution
                return default

        return wrapper

    return decorate


class TimedFrame(dict):  # type: ignore[type-arg]
    """A retained notification carrying only its fixed-size local decode time."""

    decoded_at: Optional[float]

    def __init__(self, frame: dict[str, object], decoded_at: float) -> None:
        super().__init__(frame)
        self.decoded_at = decoded_at


@dataclass
class _Stream:
    bytes_total: int = 0
    first_at: Optional[float] = None
    last_at: Optional[float] = None
    eof_at: Optional[float] = None


@dataclass
class _Milestone:
    first_at: Optional[float] = None
    value: Optional[str] = None
    confirmed: bool = False
    before_ack: bool = False


@dataclass
class _RequestState:
    method: Optional[str] = None
    request_id: Optional[dict[str, object]] = None
    command_id: Optional[str] = None
    bytes_written: int = 0
    total_bytes: int = 0
    write_state: str = "not_started"
    response_received: bool = False
    ack_accepted: bool = False


@dataclass
class MuseInvocationObserver:
    """Fixed-size fold of one invocation's observations."""

    enabled: bool = True
    diagnostic_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    interval: float = field(default_factory=lambda: HEARTBEAT_INTERVAL_SECONDS)
    metadata: dict[str, object] = field(default_factory=dict)
    sequence: int = 0

    def __post_init__(self) -> None:
        self.started = time.monotonic()
        self.started_wall = datetime.now(timezone.utc)
        self.phase: Optional[str] = None
        self.phase_started = self.started
        self.phase_seen: dict[str, float] = {}
        self.phase_durations: dict[str, float] = {}
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.frames = 0
        self.frames_first_at: Optional[float] = None
        self.frames_last_at: Optional[float] = None
        self.last_category: Optional[str] = None
        self.last_method: Optional[str] = None
        self.text = _Milestone()
        self.terminal = _Milestone()
        self.candidate_text_at: Optional[float] = None
        self.candidate_terminal_at: Optional[float] = None
        self.pre_ack_notifications = 0
        self.session_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.request = _RequestState()
        self.write = _RequestState()
        self.write_kind: Optional[str] = None
        self.rpc_error_code: Optional[int] = None
        self.operation: Optional[str] = None
        self._operations: list[str] = []
        self.operation_started = self.started
        self.anchor: Optional[float] = None
        self.next_due: Optional[float] = None
        self.execution_deadline: Optional[float] = None
        self.budget_fn: Optional[Callable[[], Optional[float]]] = None
        self.buffered_fn: Optional[Callable[[], int]] = None
        self.process: Any = None
        self.host_pid: Optional[int] = None
        self.host_state = "not_started"
        self.host_observed_at: Optional[float] = None
        self.host_exit: Optional[dict[str, object]] = None
        self.stdin_closed_at: Optional[float] = None
        self.settlement = "unobserved"
        self.primary: Optional[dict[str, object]] = None
        self.secondary: list[dict[str, object]] = []
        self.hint: Optional[str] = None
        self.ended = False
        self.outcome = "unfinished"
        self.exception_class: Optional[str] = None
        self._emitting = False

    # --- time helpers -------------------------------------------------

    def _utc(self, mono: Optional[float]) -> Optional[str]:
        if mono is None:
            return None
        return (self.started_wall + timedelta(seconds=max(mono - self.started, 0.0))).isoformat(timespec="milliseconds")

    @staticmethod
    def _age(now: float, mono: Optional[float]) -> Optional[float]:
        return None if mono is None else round(max(now - mono, 0.0), 3)

    # --- context ------------------------------------------------------

    @_safe()
    def update_metadata(self, **values: object) -> None:
        self.metadata.update({key: self._scalar(value) for key, value in values.items()})

    @staticmethod
    def _scalar(value: object) -> object:
        """Keep only bounded scalars; untrusted structured metadata is never retained."""
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            return value if abs(value) < 10**15 else "invalid"
        if isinstance(value, str):
            return display(value)
        return f"invalid-{type(value).__name__}"[:MAX_DISPLAY_CHARS]

    @_safe()
    def set_budget_sources(self, deadline: float, approval_remaining: Callable[[], Optional[float]], buffered_bytes: Callable[[], int]) -> None:
        self.execution_deadline = deadline
        self.budget_fn = approval_remaining
        self.buffered_fn = buffered_bytes

    @_safe()
    def host_started(self, process: Any) -> None:
        self.process = process
        self.host_pid = process.pid
        self.host_state = "running"
        self.host_observed_at = time.monotonic()

    @_safe()
    def session_confirmed(self, session_id: str) -> None:
        self.session_id = session_id

    @_safe()
    def stdin_closed(self) -> None:
        if self.stdin_closed_at is None:
            self.stdin_closed_at = time.monotonic()

    @_safe()
    def settlement_result(self, state: str) -> None:
        self.settlement = state

    # --- phases and monitored operations -------------------------------

    @_safe()
    def start(self) -> None:
        self._emit("start", "INFO", time.monotonic())
        self.enter_phase("preparation")

    @_safe()
    def enter_phase(self, phase: str) -> None:
        now = time.monotonic()
        if self.phase is not None:
            self.phase_durations[self.phase] = self.phase_durations.get(self.phase, 0.0) + now - self.phase_started
        self.phase = phase
        self.phase_started = now
        if phase not in self.phase_seen:
            self.phase_seen[phase] = now
            self._emit("phase", "INFO", now)

    @_safe(False)
    def begin_operation(self, name: str) -> bool:
        """Start a monitored operation unless a caller-owned one already spans it."""
        if self.operation == "host_exit_wait":
            return False
        now = time.monotonic()
        # Nested operations (e.g. a receipt write inside a response wait) restore the enclosing one.
        self._operations.append(name)
        self.operation = name
        self.operation_started = now
        if self.anchor is None:
            self.anchor = now
            self.next_due = now + self.interval
        self._tick(now)
        return True

    @_safe()
    def end_operation(self, started: bool) -> None:
        if started:
            if self._operations:
                self._operations.pop()
            self.operation = self._operations[-1] if self._operations else None

    @_safe()
    def tick(self) -> None:
        self._tick(time.monotonic())

    def _tick(self, now: float) -> None:
        if self.operation is None or self.next_due is None or now < self.next_due:
            return
        assert self.anchor is not None
        # One current snapshot, then resume the anchored schedule (no catch-up burst).
        self.next_due = self.anchor + self.interval * (int((now - self.anchor) // self.interval) + 1)
        self._emit("heartbeat", "INFO", now)

    @_safe()
    def select_timeout(self) -> Optional[float]:
        """Seconds until the next report is due; a wake-up hint, never an execution timeout."""
        if self.operation is None or self.next_due is None:
            return None
        return max(self.next_due - time.monotonic(), 0.0)

    # --- transport ----------------------------------------------------

    @_safe()
    def begin_write(self, frame: dict[str, object], total_bytes: int) -> None:
        method = frame.get("method")
        has_id = "id" in frame
        kind = "request" if isinstance(method, str) and has_id else "notification" if isinstance(method, str) else "response"
        params = frame.get("params")
        command_id = params.get("commandId") if isinstance(params, dict) else None
        state = _RequestState(
            method=display(method) if isinstance(method, str) else None,
            request_id=rpc_id_label("client_to_host" if kind == "request" else "host_to_client", frame.get("id")) if has_id else None,
            command_id=display(command_id) if isinstance(command_id, str) else None,
            total_bytes=total_bytes,
        )
        self.write = state
        self.write_kind = kind
        if kind == "request":
            self.request = state

    @_safe()
    def write_progress(self, written: int) -> None:
        state = self.write
        state.bytes_written = written
        state.write_state = "complete" if written >= state.total_bytes else "partial" if written > 0 else "not_started"

    @_safe()
    def read(self, stream: str, count: int) -> None:
        now = time.monotonic()
        target = self.stdout if stream == "stdout" else self.stderr
        target.bytes_total += count
        target.first_at = target.first_at if target.first_at is not None else now
        target.last_at = now

    @_safe()
    def eof(self, stream: str) -> None:
        target = self.stdout if stream == "stdout" else self.stderr
        if target.eof_at is None:
            target.eof_at = time.monotonic()

    @_safe(0.0)
    def decoded_frame(self, frame: dict[str, object]) -> float:
        now = time.monotonic()
        self.frames += 1
        self.frames_first_at = self.frames_first_at if self.frames_first_at is not None else now
        self.frames_last_at = now
        method = frame.get("method")
        if isinstance(method, str):
            self.last_category = "server_request" if "id" in frame else "notification"
            self.last_method = method if method in _RECOGNIZED_METHODS else "other"
        else:
            self.last_category = "response" if "id" in frame else "other"
            self.last_method = None
        return now

    @_safe()
    def correlated_response(self, error_code: object = None) -> None:
        self.request.response_received = True
        if type(error_code) is int:
            self.rpc_error_code = error_code

    @_safe()
    def ack_accepted(self) -> None:
        self.request.ack_accepted = True

    @_safe()
    def failure_hint(self, category: str) -> None:
        self.hint = category

    # --- turn milestones ----------------------------------------------

    @staticmethod
    def _shape(frame: dict[str, object]) -> Optional[str]:
        params = frame.get("params")
        if not isinstance(params, dict):
            return None
        method = frame.get("method")
        if method == "turn/completed":
            return "terminal"
        item = params.get("item")
        if method == "item/completed" and isinstance(item, dict) and item.get("status") == "completed" and isinstance(item.get("text"), str) and (item.get("kind") == "agentMessage" or (item.get("kind") == "message" and item.get("role") == "assistant")):
            return "text"
        return None

    def _match(self, frame: dict[str, object]) -> Optional[str]:
        kind = self._shape(frame)
        params = frame.get("params")
        if kind is None or not isinstance(params, dict) or params.get("sessionId") != self.session_id:
            return None
        turn = params.get("item", {}).get("turnId") if kind == "text" and isinstance(params.get("item"), dict) else params.get("turnId")
        return kind if turn == self.turn_id else None

    @staticmethod
    def _normalize_terminal(frame: dict[str, object]) -> str:
        params = frame.get("params")
        value = params.get("terminal") if isinstance(params, dict) else None
        return value if isinstance(value, str) and value in _TERMINALS else "unknown"

    @_safe()
    def notification(self, frame: TimedFrame) -> None:
        """Classify a notification at its decode time; before the turn ack it stays unconfirmed."""
        decoded_at = frame.decoded_at
        if decoded_at is None:
            return
        if self.turn_id is None:
            self.pre_ack_notifications += 1
            kind = self._shape(frame)
            if kind == "text" and self.candidate_text_at is None:
                self.candidate_text_at = decoded_at
            elif kind == "terminal" and self.candidate_terminal_at is None:
                self.candidate_terminal_at = decoded_at
            return
        self._fold(frame, False)

    def _fold(self, frame: TimedFrame, before_ack: bool) -> None:
        kind = self._match(frame)
        decoded_at = frame.decoded_at
        if kind is None or decoded_at is None:
            return
        milestone = self.text if kind == "text" else self.terminal
        if milestone.first_at is None:
            milestone.first_at = decoded_at
            milestone.confirmed = True
            milestone.before_ack = before_ack
            milestone.value = self._normalize_terminal(frame) if kind == "terminal" else None

    @_safe()
    def turn_acknowledged(self, turn_id: str, notifications: list[dict[str, object]]) -> None:
        """Fold pre-ack timing annotations once identity exists, then release them."""
        self.turn_id = turn_id
        self.candidate_text_at = self.candidate_terminal_at = None
        for frame in notifications:
            if isinstance(frame, TimedFrame):
                self._fold(frame, True)
                frame.decoded_at = None

    # --- failure and summary --------------------------------------------

    @staticmethod
    def classify(exc: BaseException) -> str:
        if not isinstance(exc, Exception):
            return "interrupted"
        if isinstance(exc, AutoCoderTimeoutError):
            return "timeout"
        if isinstance(exc, AutoCoderUsageLimitError):
            return "usage_limit"
        if isinstance(exc, OSError):
            return "os_error"
        if isinstance(exc, ValueError):
            return "invalid_input"
        return "runtime"

    @_safe()
    def failure(self, exc: BaseException, phase: Optional[str] = None) -> None:
        """Record the first failure as primary and later ones as secondary cleanup/validation."""
        category = self.classify(exc)
        if category in {"runtime", "os_error", "invalid_input"}:
            category = self.hint or category
        self.hint = None
        record: dict[str, object] = {"phase": phase or self.phase, "class": type(exc).__name__, "category": category}
        if self.primary is None:
            self.primary = record
        elif len(self.secondary) < _MAX_SECONDARY_FAILURES:
            self.secondary.append(record)

    @_safe()
    def finish(self, error: Optional[BaseException]) -> None:
        """Attempt the single final summary after all handled cleanup and validation."""
        if self.ended:
            return
        self.ended = True
        if error is not None and self.primary is None:
            self.failure(error)
        now = time.monotonic()
        if self.phase is not None:
            self.phase_durations[self.phase] = self.phase_durations.get(self.phase, 0.0) + now - self.phase_started
        outcome = "returned" if error is None else "raised" if isinstance(error, Exception) else "interrupted"
        self.operation = None
        self.exception_class = type(error).__name__ if error is not None else None
        self.outcome = outcome
        self._emit("end", "INFO" if error is None else "WARNING", now)

    # --- emission -------------------------------------------------------

    def _observe_host(self, now: float) -> dict[str, object]:
        state, exit_info = self.host_state, self.host_exit
        if self.process is not None:
            try:
                code = self.process.poll()
            except Exception:  # noqa: BLE001
                state, code = "unknown", None
            else:
                if code is not None:
                    state = "exited"
                    exit_info = {"code": code if code >= 0 else None, "signal": -code if code < 0 else None}
                    self.host_exit = exit_info
                elif state != "unknown":
                    state = "running"
            self.host_state, self.host_observed_at = state, now
        return {"state": state, "pid": self.host_pid, "observed": self._utc(self.host_observed_at), "exit": exit_info}

    def _milestone(self, milestone: _Milestone, candidate: Optional[float]) -> dict[str, object]:
        if milestone.first_at is not None:
            result: dict[str, object] = {"state": "observed", "confirmed": True, "first_decoded": self._utc(milestone.first_at), "before_ack": milestone.before_ack}
            if milestone.value is not None:
                result["terminal"] = milestone.value
            return result
        if candidate is not None:
            return {"state": "unconfirmed", "confirmed": False, "first_decoded": self._utc(candidate)}
        return {"state": "unobserved"}

    def _stream(self, stream: _Stream, now: float) -> dict[str, object]:
        return {"bytes": stream.bytes_total, "first": self._utc(stream.first_at), "last": self._utc(stream.last_at), "age_s": self._age(now, stream.last_at), "eof": stream.eof_at is not None}

    def _request(self, state: _RequestState) -> dict[str, object]:
        return {
            "method": state.method,
            "id": state.request_id,
            "command_id": state.command_id,
            "bytes_written": state.bytes_written,
            "total_bytes": state.total_bytes,
            "write": state.write_state,
            "response_received": state.response_received,
            "ack_accepted": state.ack_accepted,
        }

    def _payload(self, kind: str, now: float) -> dict[str, object]:
        self.sequence += 1
        core: dict[str, object] = {
            "schema": SCHEMA_VERSION,
            "kind": kind,
            "diagnostic_id": display(self.diagnostic_id),
            "seq": self.sequence,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "phase": self.phase,
            "elapsed_s": round(max(now - self.started, 0.0), 3),
        }
        sections: dict[str, object] = {}
        sections["context"] = {key: ("unavailable" if value is None else display(value) if isinstance(value, str) else value) for key, value in self.metadata.items() if key != "requested_session_id"}
        requested = self.metadata.get("requested_session_id")
        sections["identity"] = {
            "requested_session": display(requested) if isinstance(requested, str) else "none-fresh",
            "confirmed_session": display(self.session_id) if self.session_id else "unobserved",
            "turn": display(self.turn_id) if self.turn_id else "unobserved",
        }
        sections["boundary"] = _OBSERVATION_BOUNDARY
        if kind == "phase":
            return {**core, **sections}
        buffered = self.buffered_fn() if self.buffered_fn else None
        activity = {
            "stdout": self._stream(self.stdout, now),
            "stderr": self._stream(self.stderr, now),
            "frames": {"count": self.frames, "first": self._utc(self.frames_first_at), "last": self._utc(self.frames_last_at), "age_s": self._age(now, self.frames_last_at)},
            "stdout_buffered_bytes": buffered,
            "last_category": self.last_category,
            "last_method": self.last_method,
        }
        host = self._observe_host(now)
        host.update({"stdin_closed": self.stdin_closed_at is not None, "stdout_eof": self.stdout.eof_at is not None, "writers": self.settlement})
        pre_ack = {"pre_ack_notifications": self.pre_ack_notifications} if self.turn_id is None else {}
        sections.update(
            {
                "wait": {"reason": self.operation, "phase_elapsed_s": round(max(now - self.phase_started, 0.0), 3), "request": self._request(self.request), "write": self._request(self.write)},
                "activity": activity,
                "milestones": {"assistant_text": self._milestone(self.text, self.candidate_text_at), "turn_terminal": self._milestone(self.terminal, self.candidate_terminal_at), **pre_ack},
                "host": host,
            }
        )
        if kind != "end" and self.execution_deadline is not None:
            budgets: dict[str, object] = {"execution_remaining_s": round(max(self.execution_deadline - now, 0.0), 3)}
            approval = self.budget_fn() if self.budget_fn else None
            if approval is not None:
                budgets["approval_remaining_s"] = round(max(approval, 0.0), 3)
            sections["budgets"] = budgets
        if kind == "end":
            sections["summary"] = {
                "outcome": self.outcome,
                "exception_class": display(self.exception_class) if self.exception_class else None,
                "phase_s": {name: round(value, 3) for name, value in self.phase_durations.items()},
                "primary_failure": self.primary,
                "secondary_failures": self.secondary,
                "rpc_error_code": self.rpc_error_code,
                "note": "returned means this adapter returned normally, not that handoff or validation succeeded",
            }
        return {**core, **sections}

    @staticmethod
    def _serialize(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)

    def _fit(self, payload: dict[str, object]) -> str:
        text = self._serialize(payload)
        # Shed optional detail, never the core status/correlation fields.
        for droppable in ("context", "boundary", "wait", "activity", "budgets", "host", "milestones"):
            if len(text.encode()) <= MAX_PAYLOAD_BYTES:
                return text
            payload.pop(droppable, None)
            text = self._serialize(payload)
        return text

    def _emit(self, kind: str, level: str, now: float) -> None:
        if self._emitting:
            return
        self._emitting = True
        try:
            emit_record(level, MARKER + self._fit(self._payload(kind, now)))
        except Exception:  # noqa: BLE001 - never replace the primary execution outcome
            pass
        finally:
            self._emitting = False


def emit_record(level: str, line: str) -> None:
    """Write one record through the application's configured Loguru sinks."""
    logger.log(level, line)
