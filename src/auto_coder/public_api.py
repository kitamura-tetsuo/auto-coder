"""Minimal anonymous, read-only JSON diagnostic API (``/api/``).

The surface only *observes* state the daemon already holds: the engine's local
worker/queue occupancy, the controller's persisted implementation-slot
snapshot, and the repository-scoped structured events of the process-local
``TraceCollector``. Every response is built from typed dataclasses (an
allowlisted projection), never from a generic object encoder, so unknown
internal fields cannot become public. Free text is redacted *before* it is
clipped, and the serialized body is bounded in entries and bytes.

The routes are registered only when ``AUTO_CODER_PUBLIC_API_ENABLED=1`` at
application construction; otherwise the paths do not exist (plain 404).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Union

from fastapi import FastAPI, Request
from fastapi.responses import Response
from loguru import logger

from .execution_trace import EventKind, StructuredEvent, TraceSnapshot, get_trace_collector
from .implementation_slots import (
    ImplementationOwnerSnapshot,
    ImplementationSlotRepository,
    ImplementationSlotSnapshot,
    ImplementationSlotSnapshotUnavailable,
)

ENABLE_ENV_VAR = "AUTO_CODER_PUBLIC_API_ENABLED"
SCHEMA_VERSION = 1
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_TEXT_CHARS = 2000
MAX_RESPONSE_BYTES = 256 * 1024
MAX_MEMBERSHIPS = 100
REDACTION_MARKER = "[REDACTED]"
PATH_MARKER = "[REDACTED_PATH]"
URL_MARKER = "[REDACTED_URL]"
RETENTION_SCOPE = "process_local_bounded"
FACT_KEYS = ("reason", "error", "phase", "backend", "provider", "attempt_id", "request_id", "provider_task_id", "head_sha", "exit_code")

_CREDENTIAL_PATTERNS = re.compile(
    "|".join(
        [
            r"gh[pousr]_[A-Za-z0-9]+",
            r"github_pat_[A-Za-z0-9_]+",
            r"AIza[0-9A-Za-z_-]{35}",
            r"sk-[A-Za-z0-9_-]+",
            r"(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}",
            r"xox[baprs]-[A-Za-z0-9-]+",
            r"glpat-[A-Za-z0-9_-]+",
        ]
    )
)
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_PATH_PATTERN = re.compile(r"(?<![\w:/.@+-])(?:/(?:[\w.@+-]+/)+[\w.@+-]*|~/[\w.@+/-]+|[A-Za-z]:\\[^\s\"']+)")
_AUTH_PATTERN = re.compile(r"\b(bearer|basic)[ \t]+\S+", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/#-]{1,200}")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_POSITIVE_INT = re.compile(r"[0-9]{1,9}")
_TYPE_NAME = re.compile(r"[a-z0-9_-]{1,32}")
_SEQUENCE = re.compile(r"[0-9]{1,15}")
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,%d}" % 1024)
MAX_SEQUENCE = 10**15
CURSOR_VERSION = 1
OMIT_IDENTITY = "identity_unrepresentable"
OMIT_RESPONSE_BOUND = "representation_exceeds_response_bound"

_STATUS_PARAMS = frozenset({"limit"})
_LOGS_PARAMS = frozenset({"limit", "item_type", "item_number", "execution_id", "process_run_id", "after_sequence", "cursor"})
_SELECTOR_PARAMS = ("after_sequence", "process_run_id", "item_type", "item_number", "execution_id")


# -- public schema (the only fields that can leave the process) --------------


@dataclass
class Section:
    availability: str = "unavailable"
    sampled_at: Optional[float] = None
    error_code: Optional[str] = None
    total_count: Optional[int] = None
    returned_count: Optional[int] = None
    truncated: bool = False


@dataclass
class Target:
    type: Optional[str] = None
    number: Optional[int] = None


@dataclass
class WorkerEntry:
    worker_id: int = 0
    state: str = "idle"
    target: Optional[Target] = None


@dataclass
class QueueEntry:
    target: Target = field(default_factory=Target)
    priority: Optional[int] = None


@dataclass
class WorkersSection(Section):
    entries: Optional[List[WorkerEntry]] = None


@dataclass
class QueueSection(Section):
    entries: Optional[List[QueueEntry]] = None


@dataclass
class OwnerEntry:
    owner_type: str = ""
    owner_number: int = 0
    slot_class: str = "normal"
    execution_ids: List[str] = field(default_factory=list)
    implementation_prs: List[int] = field(default_factory=list)
    provider_session_ids: List[str] = field(default_factory=list)
    memberships_truncated: bool = False
    omitted_memberships: int = 0
    admission_pending: Optional[bool] = None
    admission_established: Optional[bool] = None
    filtered: bool = False


@dataclass
class SlotsSection(Section):
    normal_limit: Optional[int] = None
    normal_usage: Optional[int] = None
    normal_available: Optional[int] = None
    emergency_usage: Optional[int] = None
    incomplete: bool = False
    owners: Optional[List[OwnerEntry]] = None


@dataclass
class StatusResponse:
    repository: str
    sampled_at: float
    workers: WorkersSection
    queue: QueueSection
    implementation_slots: SlotsSection
    schema_version: int = SCHEMA_VERSION
    notes: str = "Sections are independent observations, not an atomic snapshot. Sampling time is not execution progress."


@dataclass
class PublicFacts:
    reason: Optional[str] = None
    error: Optional[str] = None
    phase: Optional[str] = None
    backend: Optional[str] = None
    provider: Optional[str] = None
    attempt_id: Optional[str] = None
    request_id: Optional[str] = None
    provider_task_id: Optional[str] = None
    head_sha: Optional[str] = None
    exit_code: Optional[int] = None


@dataclass
class EventEntry:
    scope: str
    repository: Optional[str]
    item_type: Optional[str]
    item_number: Optional[int]
    process_run_id: str
    execution_id: Optional[str]
    execution_start_sequence: Optional[int]
    sequence: int
    timestamp: Optional[float]
    origin: str
    stage_id: str
    label: str
    kind: str
    outcome: Optional[str]
    supported: bool
    legacy: bool
    facts: dict = field(default_factory=dict)
    filtered: bool = False
    text_truncated: bool = False
    links: Optional[dict] = None


@dataclass
class LogFilter:
    item_type: Optional[str] = None
    item_number: Optional[int] = None
    execution_id: Optional[str] = None
    process_run_id: Optional[str] = None
    limit: int = DEFAULT_LIMIT


@dataclass
class ResponseLimits:
    matching_count: int = 0
    returned_count: int = 0
    truncated: bool = False
    omitted_unrepresentable: int = 0
    incomplete: bool = False


@dataclass
class LogsResponse:
    repository: str
    sampled_at: float
    process_run_id: str
    events_truncated: bool
    execution_metadata_truncated: bool
    filter: LogFilter
    result: str
    response: ResponseLimits
    events: List[EventEntry]
    retention_scope: str = RETENTION_SCOPE
    availability: str = "available"
    schema_version: int = SCHEMA_VERSION


@dataclass
class IncrementalFilter:
    process_run_id: str
    item_type: Optional[str] = None
    item_number: Optional[int] = None
    execution_id: Optional[str] = None
    limit: int = DEFAULT_LIMIT


@dataclass
class RetentionBoundaries:
    sequence_high_watermark: int = 0
    oldest_retained_sequence: Optional[int] = None
    discarded_through_sequence: int = 0


@dataclass
class Omission:
    reason: str
    count: int
    first_sequence: int
    last_sequence: int


@dataclass
class IncrementalLogsResponse:
    repository: str
    sampled_at: float
    process_run_id: str
    events_truncated: bool
    execution_metadata_truncated: bool
    filter: IncrementalFilter
    result: str
    response: ResponseLimits
    after_sequence: int
    snapshot_upper_sequence: int
    next_after_sequence: int
    has_more: bool
    next_cursor: Optional[str]
    retention: RetentionBoundaries
    omissions: List[Omission]
    events: List[EventEntry]
    retention_scope: str = RETENTION_SCOPE
    availability: str = "available"
    schema_version: int = SCHEMA_VERSION


@dataclass
class RetentionGapResponse:
    repository: str
    error: ErrorDetail
    process_run_id: str
    after_sequence: int
    snapshot_upper_sequence: int
    retention: RetentionBoundaries
    coverage: str = "unknown"
    schema_version: int = SCHEMA_VERSION


@dataclass
class IncrementalRequest:
    after_sequence: int
    filter: IncrementalFilter
    snapshot_upper_sequence: Optional[int] = None


@dataclass
class ErrorDetail:
    code: str
    message: str


@dataclass
class ErrorResponse:
    repository: str
    error: ErrorDetail
    process_run_id: Optional[str] = None
    schema_version: int = SCHEMA_VERSION


# -- redaction / clipping -------------------------------------------------------


def sanitize_text(value: str, paths: bool = False) -> tuple[str, bool, bool]:
    """Redact credentials/URLs (and, for free text, filesystem paths) first, then clip.

    Returns (text, filtered, clipped).
    """
    text = _URL_PATTERN.sub(URL_MARKER, value)
    text = _AUTH_PATTERN.sub(lambda m: f"{m.group(1)} {REDACTION_MARKER}", text)
    text = _CREDENTIAL_PATTERNS.sub(REDACTION_MARKER, text)
    if paths:
        text = _PATH_PATTERN.sub(PATH_MARKER, text)
    filtered = text != value
    clipped = len(text) > MAX_TEXT_CHARS
    if clipped:
        text = text[:MAX_TEXT_CHARS]
    return text, filtered, clipped


def _identifier(value: object) -> Optional[str]:
    """Accept an identity-like string only if it survives unchanged."""
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value) and sanitize_text(value)[0] == value:
        return value
    return None


# -- projection --------------------------------------------------------------


def _target(item_type: object, number: object) -> Target:
    kind = item_type if isinstance(item_type, str) and _TYPE_NAME.fullmatch(item_type) else "unrecognized"
    count = number if isinstance(number, int) and not isinstance(number, bool) else None
    return Target(type=kind, number=count)


def project_workers(engine: Any, limit: int) -> WorkersSection:
    sampled = time.time()
    workers = sorted(dict(engine.active_workers).items(), key=lambda kv: kv[0])
    entries = []
    for worker_id, candidate in workers[:limit]:
        if candidate is None:
            entries.append(WorkerEntry(worker_id=int(worker_id), state="idle"))
        else:
            entries.append(WorkerEntry(worker_id=int(worker_id), state="busy", target=_target(candidate.type, candidate.data.get("number"))))
    return WorkersSection("available", sampled, None, len(workers), len(entries), len(workers) > len(entries), entries)


def project_queue(engine: Any, limit: int) -> QueueSection:
    sampled = time.time()
    queued = list(engine.queue._queue)
    entries = []
    for candidate in queued[:limit]:
        priority = candidate.priority if isinstance(candidate.priority, int) and not isinstance(candidate.priority, bool) else None
        entries.append(QueueEntry(_target(candidate.type, candidate.data.get("number")), priority))
    return QueueSection("available", sampled, None, len(queued), len(entries), len(queued) > len(entries), entries)


def _owner_entry(owner: ImplementationOwnerSnapshot) -> OwnerEntry:
    """Project one owner; an identity that cannot be exported exactly is omitted and counted."""
    filtered = False
    omitted = 0
    sessions: List[str] = []
    for session in owner.provider_sessions[:MAX_MEMBERSHIPS]:
        text, was_filtered, was_clipped = sanitize_text(session)
        if was_clipped:
            omitted += 1
            continue
        filtered = filtered or was_filtered
        sessions.append(text)
    executions = []
    for execution in owner.executions[:MAX_MEMBERSHIPS]:
        identifier = _identifier(execution.execution_id)
        if identifier is None:
            omitted += 1
        else:
            executions.append(identifier)
    return OwnerEntry(
        owner_type=owner.kind,
        owner_number=owner.number,
        slot_class="emergency" if owner.emergency else "normal",
        execution_ids=executions,
        implementation_prs=list(owner.implementation_prs[:MAX_MEMBERSHIPS]),
        provider_session_ids=sessions,
        memberships_truncated=any(len(x) > MAX_MEMBERSHIPS for x in (owner.executions, owner.implementation_prs, owner.provider_sessions)),
        omitted_memberships=omitted,
        admission_pending=owner.admission_pending,
        admission_established=owner.admission_established,
        filtered=filtered,
    )


def observe_slots(engine: Any, repo_name: str) -> object:
    """Read the persisted slot snapshot without touching the controller's admission binding.

    ``engine.get_implementation_slot_snapshot`` lazily establishes
    ``engine.implementation_slots`` when unbound; an anonymous observation must
    not, so an unbound (or differently bound) controller is observed through a
    detached repository over the same store.
    """
    current = engine.implementation_slots
    if current is not None and current.repo_name == repo_name:
        return current.snapshot()
    return ImplementationSlotRepository(repo_name, engine.config.MAX_CONCURRENT_IMPLEMENTATIONS).snapshot()


def project_slots(observation: object, limit: int) -> SlotsSection:
    if isinstance(observation, ImplementationSlotSnapshotUnavailable):
        return SlotsSection(error_code="slot_snapshot_unavailable")
    if not isinstance(observation, ImplementationSlotSnapshot):
        return SlotsSection(error_code="slot_observation_invalid")
    owners = [_owner_entry(o) for o in observation.owners[:limit]]
    incomplete = any(o.omitted_memberships or o.memberships_truncated for o in owners)
    return SlotsSection(
        "available",
        float(observation.observed_at),
        None,
        len(observation.owners),
        len(owners),
        len(observation.owners) > len(owners),
        observation.normal_limit,
        observation.normal_usage,
        observation.normal_available,
        observation.emergency_usage,
        incomplete,
        owners,
    )


def _facts(raw: object) -> tuple[dict, bool, bool]:
    if not isinstance(raw, dict):
        return {}, False, False
    out = PublicFacts()
    filtered = clipped = False
    for key in FACT_KEYS:
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if key == "exit_code":
            if isinstance(value, int):
                out.exit_code = value
        elif isinstance(value, (str, int, float)):
            text, was_filtered, was_clipped = sanitize_text(str(value), paths=True)
            setattr(out, key, text)
            filtered, clipped = filtered or was_filtered, clipped or was_clipped
    return {k: v for k, v in dataclasses.asdict(out).items() if v is not None}, filtered, clipped


def project_event(event: StructuredEvent, repo_name: str) -> Optional[EventEntry]:
    """Project one event, or return None when its identity cannot be represented exactly."""
    scoped = event.execution_id is not None
    origin, stage_id, kind = _identifier(event.origin), _identifier(event.stage_id), _identifier(event.kind)
    run_id = _identifier(event.process_run_id)
    execution_id = _identifier(event.execution_id) if scoped else None
    if None in (origin, stage_id, kind, run_id) or (scoped and execution_id is None):
        return None
    if not isinstance(event.sequence, int) or event.sequence < 0:
        return None
    if scoped and event.item_type not in ("issue", "pr"):
        return None
    label, label_filtered, label_clipped = sanitize_text(str(event.label), paths=True)
    facts, facts_filtered, facts_clipped = _facts(event.facts)
    number = event.item_number if scoped and isinstance(event.item_number, int) and event.item_number > 0 else None
    if scoped and number is None:
        return None
    links = None
    if scoped:
        links = {
            "logs": f"/api/logs?item_type={event.item_type}&item_number={number}",
            "github": f"https://github.com/{repo_name}/{'pull' if event.item_type == 'pr' else 'issues'}/{number}",
        }
    start = event.execution_start_sequence
    return EventEntry(
        scope="execution" if scoped else "unscoped",
        repository=repo_name if scoped else None,
        item_type=event.item_type if scoped else None,
        item_number=number,
        process_run_id=run_id or "",
        execution_id=execution_id,
        execution_start_sequence=start if isinstance(start, int) and scoped else None,
        sequence=event.sequence,
        timestamp=event.timestamp if isinstance(event.timestamp, (int, float)) and math.isfinite(event.timestamp) else None,
        origin=origin or "",
        stage_id=stage_id or "",
        label=label,
        kind=kind or "",
        outcome=_identifier(event.outcome) if event.outcome is not None else None,
        supported=bool(event.supported),
        legacy=bool(event.legacy),
        facts=facts,
        filtered=label_filtered or facts_filtered,
        text_truncated=label_clipped or facts_clipped,
        links=links,
    )


def _selectable(event: StructuredEvent, repo_name: str, flt: LogFilter) -> bool:
    if event.execution_id is not None:
        if not isinstance(event.repository, str) or event.repository.casefold() != repo_name.casefold():
            return False
    else:
        # Unscoped structured observations are only ever listed in unfiltered
        # reads; ingested repository-less legacy records are never adopted.
        if event.repository != "" or event.kind not in (EventKind.STAGE_STARTED.value, EventKind.STAGE_RESULT.value) or not event.supported:
            return False
        return flt.item_type is None and flt.item_number is None and flt.execution_id is None
    if flt.item_type is not None and event.item_type != flt.item_type:
        return False
    if flt.item_number is not None and event.item_number != flt.item_number:
        return False
    if flt.execution_id is not None and event.execution_id != flt.execution_id:
        return False
    return True


def build_logs(repo_name: str, flt: LogFilter) -> "LogsResponse | ErrorResponse":
    snapshot = get_trace_collector().get_snapshot()
    if flt.process_run_id is not None and flt.process_run_id != snapshot.process_run_id:
        return ErrorResponse(repo_name, ErrorDetail("process_run_changed", "The supplied process_run_id is not the current process run."), snapshot.process_run_id)
    matching = sorted((e for e in snapshot.events if _selectable(e, repo_name, flt)), key=lambda e: e.sequence)
    selected: List[EventEntry] = []
    omitted = 0
    for event in reversed(matching):
        if len(selected) >= flt.limit:
            break
        entry = project_event(event, repo_name)
        if entry is None:
            omitted += 1
        else:
            selected.append(entry)
    selected.reverse()
    response = LogsResponse(
        repo_name,
        time.time(),
        snapshot.process_run_id,
        snapshot.events_truncated,
        snapshot.execution_metadata_truncated,
        flt,
        "events" if matching else "no_retained_match",
        ResponseLimits(matching_count=len(matching), omitted_unrepresentable=omitted),
        selected,
    )
    return response


# -- incremental pages -----------------------------------------------------------


class ObservationUnavailable(Exception):
    """Raised when continuity evidence is missing or inconsistent; carries no source text."""


def _repo_digest(repo_name: str) -> str:
    return hashlib.sha256(repo_name.casefold().encode("utf-8")).hexdigest()[:16]


def encode_cursor(repo_name: str, flt: IncrementalFilter, after: int, upper: int) -> str:
    payload = {"v": CURSOR_VERSION, "r": flt.process_run_id, "p": _repo_digest(repo_name), "t": flt.item_type, "n": flt.item_number, "e": flt.execution_id, "a": after, "h": upper}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _is_int(value: object, low: int = 0, high: int = MAX_SEQUENCE) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def decode_cursor(raw: str, repo_name: str, limit: int) -> IncrementalRequest:
    """Strictly validate an opaque cursor; any defect raises ``InvalidRequest`` without echoing it."""
    if not _CURSOR.fullmatch(raw):
        raise InvalidRequest
    try:
        data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True).decode("utf-8"))
    except (binascii.Error, ValueError):
        raise InvalidRequest from None
    keys = {"v", "r", "p", "t", "n", "e", "a", "h"}
    if not isinstance(data, dict) or set(data) != keys or data["v"] != CURSOR_VERSION:
        raise InvalidRequest
    run, item_type, number, execution_id = data["r"], data["t"], data["n"], data["e"]
    if not isinstance(run, str) or not _OPAQUE_ID.fullmatch(run) or data["p"] != _repo_digest(repo_name):
        raise InvalidRequest
    if (item_type is None) != (number is None) or (item_type is not None and (item_type not in ("issue", "pr") or not _is_int(number, 1))):
        raise InvalidRequest
    if execution_id is not None and (not isinstance(execution_id, str) or not _OPAQUE_ID.fullmatch(execution_id)):
        raise InvalidRequest
    after, upper = data["a"], data["h"]
    if not _is_int(after) or not _is_int(upper) or after > upper:
        raise InvalidRequest
    return IncrementalRequest(after, IncrementalFilter(run, item_type, number, execution_id, limit), upper)


def _checked_continuity(snapshot: TraceSnapshot) -> RetentionBoundaries:
    high, oldest, discarded = snapshot.sequence_high_watermark, snapshot.oldest_retained_sequence, snapshot.discarded_through_sequence
    if not _is_int(high) or not _is_int(discarded, 0, high) or not isinstance(snapshot.process_run_id, str):
        raise ObservationUnavailable
    if oldest is not None and not _is_int(oldest, discarded + 1, high):
        raise ObservationUnavailable
    sequences = [e.sequence for e in snapshot.events]
    if not all(_is_int(seq, 1, high) for seq in sequences) or (min(sequences) if sequences else None) != oldest:
        raise ObservationUnavailable
    return RetentionBoundaries(high, oldest, discarded)


@dataclass
class _Consumed:
    sequence: int
    entry: Optional[EventEntry]
    reason: str = ""


def _omissions(consumed: Sequence[_Consumed]) -> List[Omission]:
    found: "dict[str, Omission]" = {}
    for item in consumed:
        if item.entry is not None:
            continue
        current = found.get(item.reason)
        if current is None:
            found[item.reason] = Omission(item.reason, 1, item.sequence, item.sequence)
        else:
            current.count += 1
            current.last_sequence = item.sequence
    return list(found.values())


def build_incremental_logs(repo_name: str, request: IncrementalRequest) -> "IncrementalLogsResponse | ErrorResponse | RetentionGapResponse":
    """Return the oldest unread representable prefix of a fixed ``(after, upper]`` interval."""
    flt = request.filter
    snapshot = get_trace_collector().get_snapshot()
    retention = _checked_continuity(snapshot)
    if flt.process_run_id != snapshot.process_run_id:
        return ErrorResponse(repo_name, ErrorDetail("process_run_changed", "The supplied process_run_id is not the current process run."), snapshot.process_run_id)
    after = request.after_sequence
    upper = retention.sequence_high_watermark if request.snapshot_upper_sequence is None else request.snapshot_upper_sequence
    if after > upper or upper > retention.sequence_high_watermark:
        raise InvalidRequest
    if after < upper and retention.discarded_through_sequence > after:
        detail = ErrorDetail("retention_gap", "Retained events at or after the checkpoint were evicted or cleared; matching-filter coverage of the interval is unknown.")
        return RetentionGapResponse(repo_name, detail, snapshot.process_run_id, after, upper, retention)
    selection = LogFilter(flt.item_type, flt.item_number, flt.execution_id, flt.process_run_id, flt.limit)
    matching = sorted((e for e in snapshot.events if after < e.sequence <= upper and _selectable(e, repo_name, selection)), key=lambda e: e.sequence)

    consumed: List[_Consumed] = []
    delivered = 0
    cursor_index = 0
    while cursor_index < len(matching) and delivered < flt.limit:
        event = matching[cursor_index]
        cursor_index += 1
        entry = project_event(event, repo_name)
        if entry is None:
            consumed.append(_Consumed(event.sequence, None, OMIT_IDENTITY))
        else:
            delivered += 1
            consumed.append(_Consumed(event.sequence, entry))
    pending = matching[cursor_index:]

    def page(prefix: int) -> IncrementalLogsResponse:
        taken = consumed[:prefix]
        unread = consumed[prefix].sequence if prefix < len(consumed) else pending[0].sequence if pending else None
        next_after = upper if unread is None else unread - 1
        events = [c.entry for c in taken if c.entry is not None]
        omissions = _omissions(taken)
        omitted = sum(o.count for o in omissions)
        has_more = next_after < upper
        if events:
            result = "events"
        else:
            result = "omitted_only" if omitted else "no_retained_match"
        return IncrementalLogsResponse(
            repo_name,
            time.time(),
            snapshot.process_run_id,
            snapshot.events_truncated,
            snapshot.execution_metadata_truncated,
            flt,
            result,
            ResponseLimits(len(matching), len(events), unread is not None, omitted, omitted > 0),
            after,
            upper,
            next_after,
            has_more,
            encode_cursor(repo_name, flt, next_after, upper) if has_more else None,
            retention,
            omissions,
            events,  # type: ignore[arg-type]
        )

    def fits(prefix: int) -> bool:
        return len(_dump(page(prefix))) <= MAX_RESPONSE_BYTES

    while True:
        low, high = 0, len(consumed)
        while low < high:
            mid = (low + high + 1) // 2
            if fits(mid):
                low = mid
            else:
                high = mid - 1
        if low > 0 or not consumed:
            return page(low)
        # The oldest unread record alone exceeds the bound: omit and consume it explicitly.
        consumed[0] = _Consumed(consumed[0].sequence, None, OMIT_RESPONSE_BOUND)


# -- bounded serialization -----------------------------------------------------


def _dump(payload: object) -> bytes:
    return json.dumps(dataclasses.asdict(payload), allow_nan=False, separators=(",", ":"), ensure_ascii=True).encode("utf-8")  # type: ignore[call-overload]


def _fit_logs(response: LogsResponse) -> bytes:
    """Drop the oldest returned events until the body fits, keeping ascending order."""
    limits = response.response
    returned = len(response.events)
    while True:
        limits.returned_count = len(response.events)
        limits.truncated = limits.matching_count - limits.omitted_unrepresentable > limits.returned_count
        limits.incomplete = limits.truncated or limits.omitted_unrepresentable > 0
        body = _dump(response)
        if len(body) <= MAX_RESPONSE_BYTES or not response.events:
            return body
        excess = len(body) - MAX_RESPONSE_BYTES
        drop = 1
        if returned > 8:
            average = max(1, len(body) // max(1, len(response.events)))
            drop = max(1, min(len(response.events), excess // average))
        del response.events[:drop]


def _fit_status(response: StatusResponse) -> bytes:
    """Drop trailing entries of the largest collection until the body fits."""
    collections: Sequence[tuple[Section, Optional[list]]] = (
        (response.workers, response.workers.entries),
        (response.queue, response.queue.entries),
        (response.implementation_slots, response.implementation_slots.owners),
    )
    while True:
        for section, entries in collections:
            if entries is not None:
                section.returned_count = len(entries)
                section.truncated = (section.total_count or 0) > len(entries)
        body = _dump(response)
        if len(body) <= MAX_RESPONSE_BYTES:
            return body
        largest = max((entries for _, entries in collections if entries), key=lambda e: len(_dump_any(e)), default=None)
        if largest is None:
            return body
        del largest[-max(1, len(largest) // 8) :]


def _dump_any(entries: list) -> bytes:
    return json.dumps([dataclasses.asdict(e) for e in entries], allow_nan=False).encode("utf-8")


def _json_response(payload: object, status_code: int = 200) -> Response:
    body = _fit_logs(payload) if isinstance(payload, LogsResponse) else _fit_status(payload) if isinstance(payload, StatusResponse) else _dump(payload)
    return Response(body, status_code=status_code, media_type="application/json", headers={"Cache-Control": "no-store"})


# -- request parsing -----------------------------------------------------------


class InvalidRequest(Exception):
    """Raised for an invalid query; carries no caller-supplied text."""


def _parse_limit(raw: Optional[str]) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    if not _POSITIVE_INT.fullmatch(raw) or not 1 <= int(raw) <= MAX_LIMIT:
        raise InvalidRequest
    return int(raw)


def _parse_params(request: Request, allowed: frozenset) -> dict:
    params = request.query_params
    keys = [k for k, _ in params.multi_items()]
    if len(keys) != len(set(keys)) or any(k not in allowed for k in keys):
        raise InvalidRequest
    return dict(params.items())


def parse_log_request(request: Request, repo_name: str) -> Union[LogFilter, IncrementalRequest]:
    params = _parse_params(request, _LOGS_PARAMS)
    limit = _parse_limit(params.get("limit"))
    if "cursor" in params:
        if any(name in params for name in _SELECTOR_PARAMS):
            raise InvalidRequest
        return decode_cursor(params["cursor"], repo_name, limit)
    item_type, number = params.get("item_type"), params.get("item_number")
    if (item_type is None) != (number is None):
        raise InvalidRequest
    if item_type is not None and (item_type not in ("issue", "pr") or not _POSITIVE_INT.fullmatch(number or "") or int(number or 0) < 1):
        raise InvalidRequest
    execution_id, run_id, after = params.get("execution_id"), params.get("process_run_id"), params.get("after_sequence")
    if (execution_id is not None or after is not None) and run_id is None:
        raise InvalidRequest
    for value in (execution_id, run_id):
        if value is not None and not _OPAQUE_ID.fullmatch(value):
            raise InvalidRequest
    count = int(number) if number is not None else None
    if after is not None:
        if not _SEQUENCE.fullmatch(after) or run_id is None:
            raise InvalidRequest
        return IncrementalRequest(int(after), IncrementalFilter(run_id, item_type, count, execution_id, limit))
    return LogFilter(item_type, count, execution_id, run_id, limit)


# -- application wiring ---------------------------------------------------------


def public_api_enabled() -> bool:
    return os.environ.get(ENABLE_ENV_VAR) == "1"


def init_public_api(app: FastAPI, engine: Any, repo_name: str) -> None:
    """Register the three read-only routes on ``app`` (GET only; other methods get 405)."""

    def error(status: int, code: str, message: str, run_id: Optional[str] = None) -> Response:
        return _json_response(ErrorResponse(repo_name, ErrorDetail(code, message), run_id), status)

    invalid = lambda: error(422, "invalid_request", "Unsupported or invalid query parameters.")  # noqa: E731

    @app.get("/api/", include_in_schema=False)
    async def api_index() -> Response:
        run_id: Optional[str] = None
        try:
            run_id = await asyncio.to_thread(lambda: get_trace_collector().process_run_id)
        except Exception:
            logger.exception("public API index: collector unavailable")
        doc = _index_document(repo_name, run_id)
        return Response(json.dumps(doc, allow_nan=False).encode("utf-8"), media_type="application/json", headers={"Cache-Control": "no-store"})

    @app.get("/api/status", include_in_schema=False)
    async def api_status(request: Request) -> Response:
        try:
            limit = _parse_limit(_parse_params(request, _STATUS_PARAMS).get("limit"))
        except InvalidRequest:
            return invalid()

        async def observe(project: Callable[[], Section], blank: Section, code: str) -> Section:
            try:
                return await asyncio.to_thread(project)
            except Exception:
                logger.exception(f"public API status: {code}")
                blank.error_code = code
                return blank

        workers = await observe(lambda: project_workers(engine, limit), WorkersSection(), "worker_observation_unavailable")
        queue = await observe(lambda: project_queue(engine, limit), QueueSection(), "queue_observation_unavailable")

        def slots() -> Section:
            return project_slots(observe_slots(engine, repo_name), limit)

        slot_section = await observe(slots, SlotsSection(), "slot_observation_unavailable")
        return _json_response(StatusResponse(repo_name, time.time(), workers, queue, slot_section))  # type: ignore[arg-type]

    @app.get("/api/logs", include_in_schema=False)
    async def api_logs(request: Request) -> Response:
        try:
            parsed = parse_log_request(request, repo_name)
        except InvalidRequest:
            return invalid()
        try:
            result: object
            if isinstance(parsed, IncrementalRequest):
                result = await asyncio.to_thread(build_incremental_logs, repo_name, parsed)
            else:
                result = await asyncio.to_thread(build_logs, repo_name, parsed)
        except InvalidRequest:
            return invalid()
        except Exception:
            logger.exception("public API logs: observation failed")
            return error(503, "observation_unavailable", "Diagnostic events could not be read.")
        if isinstance(result, (ErrorResponse, RetentionGapResponse)):
            return _json_response(result, 409)
        try:
            return await asyncio.to_thread(_json_response, result)
        except Exception:
            logger.exception("public API logs: projection failed")
            return error(503, "observation_unavailable", "Diagnostic events could not be projected.")


def _index_document(repo_name: str, run_id: Optional[str]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "api": "auto-coder-public-diagnostics",
        "repository": repo_name,
        "process_run_id": run_id,
        "routes": {
            "status": {
                "path": "/api/status",
                "method": "GET",
                "parameters": {"limit": f"integer 1-{MAX_LIMIT}, default {DEFAULT_LIMIT}, applied to each collection"},
            },
            "logs": {
                "path": "/api/logs",
                "method": "GET",
                "parameters": {
                    "limit": f"integer 1-{MAX_LIMIT}, default {DEFAULT_LIMIT}; newest matching events, returned in increasing sequence",
                    "item_type": "issue|pr; must be paired with item_number",
                    "item_number": "positive integer; must be paired with item_type",
                    "execution_id": "exact execution; requires process_run_id",
                    "process_run_id": "must equal the current process run, otherwise 409 process_run_changed",
                    "after_sequence": "opt-in incremental mode: nonnegative checkpoint; requires process_run_id, at most the current sequence_high_watermark; pages the oldest unread matching events up to a fixed snapshot_upper_sequence",
                    "cursor": "opaque next_cursor from an incremental response; only limit may accompany it",
                },
            },
        },
        "limits": {"max_entries_per_collection": MAX_LIMIT, "max_text_chars": MAX_TEXT_CHARS, "max_response_bytes": MAX_RESPONSE_BYTES},
        "meanings": {
            "unavailable": "The source could not be observed; its data is null. It is not empty and not healthy.",
            "no_retained_match": "No retained event matches the filter. It does not mean the item does not exist, nothing ran, or all is healthy.",
            "retention_loss": "events_truncated / execution_metadata_truncated report that the bounded process-local source evicted older records.",
            "response_clipping": "response.truncated / per-section truncated report entries omitted by limit or byte bound; text_truncated marks clipped text; filtered marks redaction.",
            "incremental": (
                "Incremental pages never skip unread retained events: next_after_sequence is the safely consumed boundary, "
                "has_more/next_cursor continue the fixed interval ending at snapshot_upper_sequence, and omissions lists matching "
                "records consumed but not returned. 409 retention_gap: coverage of the pending interval is unknown; "
                "409 process_run_changed: the process restarted. Acknowledge next_after_sequence only after processing a page."
            ),
            "process_local_logs": "Logs are structured diagnostic events held in this process's memory; they reset on restart and are not raw logs or a durable history.",
        },
    }
