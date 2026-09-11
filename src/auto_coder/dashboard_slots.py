"""
Pure, UI-framework-independent projection logic for the dashboard's
Implementation Slots panel (Issue #1993).

Reads an `ImplementationSlotObservation` (the result of
`ImplementationSlotRepository.snapshot()`, consumed here through
`AutomationEngine.get_implementation_slot_snapshot`) and derives what the
panel should display: repository/store identity, capacity counters, and
one row per recorded owner. This module never talks to GitHub/providers,
never probes process liveness, and never mutates anything -- it only
projects an already-observed snapshot into display-ready values.

Kept separate from `dashboard.py` (which owns the NiceGUI wiring and the
stale/unavailable retention state machine) so this projection can be
unit-tested without a NiceGUI runtime, matching `dashboard_detail.py`'s
split for the detail view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .implementation_slots import (
    ImplementationExecutionSnapshot,
    ImplementationOwnerSnapshot,
    ImplementationSlotSnapshot,
)


def format_optional_bool(value: Optional[bool]) -> str:
    """Render a recorded/absent Boolean distinctly.

    `None` means the field was never recorded (e.g. a legacy reservation
    written before admission flags existed); it must never be displayed
    the same way as an explicit `False` (REQ-003 of Issue #1993).
    """
    if value is None:
        return "not recorded"
    return "true" if value else "false"


def format_timestamp(observed_at: float) -> str:
    """Format a snapshot's observation time for display."""
    return datetime.fromtimestamp(observed_at).strftime("%H:%M:%S")


@dataclass(frozen=True)
class SlotExecutionRow:
    """One recorded execution, displayed as local evidence only -- never a
    liveness claim (REQ-003)."""

    execution_id: str
    pid_display: str
    started_at_display: str


def _execution_row(execution: ImplementationExecutionSnapshot) -> SlotExecutionRow:
    return SlotExecutionRow(
        execution_id=execution.execution_id,
        pid_display=str(execution.pid) if execution.pid is not None else "not recorded",
        started_at_display=str(execution.started_at) if execution.started_at is not None else "not recorded",
    )


@dataclass(frozen=True)
class SlotOwnerRow:
    """One recorded owner, projected for display.

    This is a local recorded-ownership/evidence projection, not an
    assertion that the owner is currently running, completed, or free
    (REQ-003). `detail_path` reuses the existing repository-scoped
    `/detail/{item_type}/{item_number}` route so owner/PR links never
    invent a separate navigation target.
    """

    key: str
    kind: str
    number: int
    detail_path: str
    class_label: str  # "normal" | "emergency"
    executions: Tuple[SlotExecutionRow, ...]
    implementation_prs: Tuple[int, ...]
    provider_sessions: Tuple[str, ...]
    admission_pending_display: str
    admission_established_display: str


def _detail_path(kind: str, number: int) -> str:
    return f"/detail/{kind}/{number}"


def owner_row(owner: ImplementationOwnerSnapshot) -> SlotOwnerRow:
    """Project one `ImplementationOwnerSnapshot` into a display row.

    Every field is copied verbatim from the observation: an owner with
    empty membership lists or not-recorded admission flags is still shown
    and counted, never silently treated as free capacity (REQ-003, AS-006).
    """
    return SlotOwnerRow(
        key=owner.owner_key,
        kind=owner.kind,
        number=owner.number,
        detail_path=_detail_path(owner.kind, owner.number),
        class_label="emergency" if owner.emergency else "normal",
        executions=tuple(_execution_row(execution) for execution in owner.executions),
        implementation_prs=owner.implementation_prs,
        provider_sessions=owner.provider_sessions,
        admission_pending_display=format_optional_bool(owner.admission_pending),
        admission_established_display=format_optional_bool(owner.admission_established),
    )


def owner_rows(snapshot: ImplementationSlotSnapshot) -> Tuple[SlotOwnerRow, ...]:
    """Project every owner in `snapshot`, preserving its stable sort order."""
    return tuple(owner_row(owner) for owner in snapshot.owners)


@dataclass(frozen=True)
class SlotPanelSummary:
    """Display-ready capacity summary for a known snapshot.

    `normal_used`/`normal_available` and `emergency_usage` are copied
    verbatim from the snapshot: normal usage above the configured limit is
    shown as-is, never clamped or hidden (REQ-002).
    """

    repository: str
    storage_path: str
    observed_at_display: str
    normal_used: int
    normal_limit: int
    normal_available: int
    emergency_usage: int


def summarize(snapshot: ImplementationSlotSnapshot) -> SlotPanelSummary:
    """Project a known snapshot's repository/store identity and counters."""
    return SlotPanelSummary(
        repository=snapshot.repository,
        storage_path=snapshot.storage_path,
        observed_at_display=format_timestamp(snapshot.observed_at),
        normal_used=snapshot.normal_usage,
        normal_limit=snapshot.normal_limit,
        normal_available=snapshot.normal_available,
        emergency_usage=snapshot.emergency_usage,
    )
