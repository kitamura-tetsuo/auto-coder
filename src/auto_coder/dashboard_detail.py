"""
Pure, UI-framework-independent logic for the dashboard detail view.

This module reads the shared schema-version-1 diagnostic snapshot
(`execution_trace.TraceSnapshot`) and derives what a detail view should
show: which execution is selected, the events belonging to it, and a
Mermaid diagram of the *observed* event sequence. It never infers a
business workflow, never talks to GitHub/provider APIs, and never mutates
anything -- it only projects already-recorded evidence.

Kept separate from `dashboard.py` (which owns the NiceGUI wiring) so this
selection/rendering logic can be unit-tested without a NiceGUI runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from .execution_trace import EventKind, ExecutionSummary, StructuredEvent, TraceSnapshot

VALID_ITEM_TYPES = ("issue", "pr")

# Characters that are safe to emit verbatim inside a quoted Mermaid label.
# Everything else (quotes, brackets, braces, backticks, pipes, semicolons,
# percent signs, newlines, HTML angle brackets, ...) is converted to a
# numeric HTML character reference so it can never end the quoted label,
# start a new Mermaid directive/node/edge, or be interpreted as
# markup/script by whatever ends up rendering the SVG. A bare hyphen is
# allowed (stage ids are hyphen-heavy, e.g. "pr.ci-eligibility"): it cannot
# by itself end the quoted label or start a new statement, and the arrow
# sequence "-->" stays inert here because ">" is not in this allow-list.
_MERMAID_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:/_()+=@!?-")


def is_supported_item_type(item_type: str) -> bool:
    """Whether ``item_type`` is a recognized diagnostic scope."""
    return item_type in VALID_ITEM_TYPES


def is_resolvable_item_number(item_number: int) -> bool:
    """Whether ``item_number`` could plausibly identify a GitHub Issue/PR."""
    return isinstance(item_number, int) and not isinstance(item_number, bool) and item_number > 0


def escape_for_mermaid_label(text: str) -> str:
    """Render ``text`` as inert content inside a quoted Mermaid node label.

    Every character outside a small safe allow-list is replaced with its
    numeric HTML character reference (``#NN;``). This keeps embedded
    quotes, brackets, arrows, semicolons, and HTML tags from ever being
    parsed as Mermaid syntax or markup -- they are always displayed as
    plain escaped text.
    """
    out: List[str] = []
    for ch in text:
        if ch in _MERMAID_SAFE_CHARS:
            out.append(ch)
        elif ch in ("\n", "\r"):
            out.append(" ")
        else:
            out.append(f"#{ord(ch)};")
    return "".join(out)


def format_fact_value(value: Any) -> str:
    """Render one recorded fact value as literal text, without coercion.

    Booleans are rendered as ``true``/``false`` (never remapped to
    "Success"/"Failure"), ``None`` is rendered as an explicit
    "not recorded" marker rather than an empty string, and structured
    values are rendered as compact JSON rather than reinterpreted.
    """
    if value is None:
        return "(not recorded)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


class SelectionMode(str, Enum):
    """Whether a detail view follows the newest execution or is pinned."""

    FOLLOW_LATEST = "follow_latest"
    PINNED = "pinned"


@dataclass(frozen=True)
class DetailSelection:
    """The execution (if any) a detail view should currently render."""

    mode: SelectionMode
    execution: Optional[ExecutionSummary]
    pinned_execution_id: Optional[str]
    pinned_evicted: bool


def executions_for_item(snapshot: TraceSnapshot, repository: str, item_type: str, item_number: int) -> List[ExecutionSummary]:
    """Executions for exactly this (repository, item_type, item_number), oldest start first.

    Ordering is by ``start_sequence`` (original execution-start publication
    order), never by wall-clock timestamp, last activity, or list index, so
    late-arriving evidence for an older execution cannot reorder it (REQ-003).
    """
    matches = [s for s in snapshot.executions.values() if s.repository == repository and s.item_type == item_type and s.item_number == item_number]
    matches.sort(key=lambda s: s.start_sequence)
    return matches


def events_for_execution(snapshot: TraceSnapshot, execution_id: str) -> List[StructuredEvent]:
    """Structured events belonging to exactly one execution, in publication order."""
    matched = [e for e in snapshot.events if e.execution_id == execution_id]
    matched.sort(key=lambda e: e.sequence)
    return matched


def unscoped_events_for_item(snapshot: TraceSnapshot, repository: str, item_type: str, item_number: int) -> List[StructuredEvent]:
    """Events for this item that never got an execution identity.

    Includes both legacy/pre-migration records and structured events
    recorded before any execution scope was open. These are shown as raw
    unscoped evidence and are never assigned a guessed execution (REQ-007).
    """
    matched = [e for e in snapshot.events if e.execution_id is None and e.repository == repository and e.item_type == item_type and e.item_number == item_number]
    matched.sort(key=lambda e: e.sequence)
    return matched


def execution_start_event_present(events: Sequence[StructuredEvent]) -> bool:
    """Whether this execution's own start event is still retained."""
    return any(e.kind == EventKind.EXECUTION_STARTED.value for e in events)


def execution_finished_event(events: Sequence[StructuredEvent]) -> Optional[StructuredEvent]:
    """The execution-finished event for this execution, if retained."""
    for event in events:
        if event.kind == EventKind.EXECUTION_FINISHED.value:
            return event
    return None


def resolve_selected_execution(
    executions: Sequence[ExecutionSummary],
    mode: SelectionMode,
    pinned_execution_id: Optional[str],
) -> DetailSelection:
    """Decide which execution a detail view should show right now.

    In FOLLOW_LATEST mode this is always the execution with the greatest
    ``start_sequence`` currently retained. In PINNED mode the exact pinned
    execution id is looked up by identity, never by its former position in
    ``executions``: if retention has evicted it, ``pinned_evicted`` is True
    and no other execution is silently substituted (REQ-004, AS-003).
    """
    if mode is SelectionMode.FOLLOW_LATEST:
        newest = executions[-1] if executions else None
        return DetailSelection(mode=mode, execution=newest, pinned_execution_id=None, pinned_evicted=False)

    for summary in executions:
        if summary.execution_id == pinned_execution_id:
            return DetailSelection(mode=mode, execution=summary, pinned_execution_id=pinned_execution_id, pinned_evicted=False)
    return DetailSelection(mode=mode, execution=None, pinned_execution_id=pinned_execution_id, pinned_evicted=True)


def build_observed_path_diagram(events: Sequence[StructuredEvent]) -> str:
    """Render the selected execution's observed events as a Mermaid graph.

    Nodes are emitted strictly in event-sequence order using each event's
    own stage id/label/kind/outcome; repeated occurrences of a stage get
    distinct nodes (keyed by position, not by stage id) so they remain
    individually visible. Edges are labeled "observed order" -- they assert
    only that one event was recorded after another, never an inferred
    control-flow/causal edge (REQ-002, REQ-009).
    """
    if not events:
        return ""

    lines = ["graph TD"]
    node_ids: List[str] = []
    for idx, event in enumerate(events):
        node_id = f"n{idx}"
        node_ids.append(node_id)
        label_parts = [
            escape_for_mermaid_label(event.label or event.stage_id),
            escape_for_mermaid_label(f"kind: {event.kind}"),
            escape_for_mermaid_label(f"outcome: {event.outcome or 'unknown'}"),
        ]
        label = "<br/>".join(label_parts)
        lines.append(f'    {node_id}["{label}"]')

    for a, b in zip(node_ids, node_ids[1:]):
        lines.append(f"    {a} -->|observed order| {b}")

    return "\n".join(lines)


def evidence_rows(events: Sequence[StructuredEvent]) -> List[Dict[str, str]]:
    """One display row per stage-result/execution-finished event with evidence.

    Each row keeps its producing event's own stage/label, observation
    timestamp, explicit outcome (falling back to the literal string
    "unknown", never a coerced success/failure), and raw recorded facts
    rendered without boolean coercion (REQ-005, REQ-006). Rows are not
    merged across events, so evidence from different stages/revisions is
    never combined into a synthetic composite state.
    """
    rows: List[Dict[str, str]] = []
    for event in events:
        if event.kind not in (EventKind.STAGE_RESULT.value, EventKind.EXECUTION_FINISHED.value):
            continue
        if not event.facts and event.outcome is None:
            continue
        fact_lines = [f"{key}: {format_fact_value(value)}" for key, value in sorted((event.facts or {}).items())]
        rows.append(
            {
                "time": datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S"),
                "stage": event.label or event.stage_id,
                "kind": event.kind,
                "outcome": event.outcome or "unknown",
                "facts": "\n".join(fact_lines) if fact_lines else "(no facts recorded)",
                "sequence": str(event.sequence),
            }
        )
    return rows
