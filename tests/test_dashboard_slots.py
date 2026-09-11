"""Unit tests for the pure Implementation Slots panel projection logic in
`dashboard_slots.py` (Issue #1993). These exercise the projection
functions directly against real `implementation_slots` dataclasses,
independent of NiceGUI; production-to-mounted-view coverage lives in
`tests/test_dashboard_slots_observability.py`.
"""

from __future__ import annotations

from auto_coder.dashboard_slots import (
    SlotExecutionRow,
    format_optional_bool,
    format_timestamp,
    owner_row,
    owner_rows,
    summarize,
)
from auto_coder.implementation_slots import (
    ImplementationExecutionSnapshot,
    ImplementationOwnerSnapshot,
    ImplementationSlotSnapshot,
)


def test_format_optional_bool_distinguishes_false_from_not_recorded():
    assert format_optional_bool(None) == "not recorded"
    assert format_optional_bool(False) == "false"
    assert format_optional_bool(True) == "true"


def test_format_timestamp_is_hh_mm_ss():
    formatted = format_timestamp(0.0)
    assert len(formatted) == 8
    assert formatted.count(":") == 2


def test_owner_row_projects_every_recorded_field_literally():
    owner = ImplementationOwnerSnapshot(
        kind="issue",
        number=42,
        owner_key="issue:42",
        emergency=False,
        executions=(ImplementationExecutionSnapshot(execution_id="exec-1", pid=123, started_at=100.5),),
        implementation_prs=(43, 44),
        provider_sessions=("session-<script>",),
        admission_pending=False,
        admission_established=None,
    )

    row = owner_row(owner)

    assert row.key == "issue:42"
    assert row.kind == "issue"
    assert row.number == 42
    assert row.detail_path == "/detail/issue/42"
    assert row.class_label == "normal"
    assert row.executions == (SlotExecutionRow(execution_id="exec-1", pid_display="123", started_at_display="100.5"),)
    assert row.implementation_prs == (43, 44)
    # Rendered verbatim -- no markup interpretation or escaping decision is
    # this module's responsibility (the NiceGUI text binding handles that).
    assert row.provider_sessions == ("session-<script>",)
    assert row.admission_pending_display == "false"
    assert row.admission_established_display == "not recorded"


def test_owner_row_marks_absent_execution_fields_not_recorded():
    owner = ImplementationOwnerSnapshot(
        kind="pr",
        number=7,
        owner_key="pr:7",
        emergency=True,
        executions=(ImplementationExecutionSnapshot(execution_id="exec-2", pid=None, started_at=None),),
        implementation_prs=(),
        provider_sessions=(),
        admission_pending=None,
        admission_established=None,
    )

    row = owner_row(owner)

    assert row.class_label == "emergency"
    assert row.executions[0].pid_display == "not recorded"
    assert row.executions[0].started_at_display == "not recorded"
    assert row.implementation_prs == ()
    assert row.provider_sessions == ()


def test_owner_rows_preserves_snapshot_order():
    snapshot = ImplementationSlotSnapshot(
        repository="owner/repo",
        storage_path="/tmp/slots.json",
        observed_at=1000.0,
        normal_limit=3,
        owners=(
            ImplementationOwnerSnapshot("issue", 1, "issue:1", False, (), (), (), None, None),
            ImplementationOwnerSnapshot("pr", 2, "pr:2", False, (), (), (), None, None),
        ),
        normal_usage=2,
        normal_available=1,
        emergency_usage=0,
    )

    rows = owner_rows(snapshot)

    assert [row.key for row in rows] == ["issue:1", "pr:2"]


def test_summarize_copies_counters_verbatim_including_above_limit_usage():
    """REQ-002: above-limit normal occupancy must not be clamped or hidden
    by this projection -- it is copied straight from the snapshot."""
    snapshot = ImplementationSlotSnapshot(
        repository="owner/repo",
        storage_path="/tmp/slots.json",
        observed_at=1700000000.0,
        normal_limit=1,
        owners=(),
        normal_usage=3,
        normal_available=0,
        emergency_usage=1,
    )

    summary = summarize(snapshot)

    assert summary.repository == "owner/repo"
    assert summary.storage_path == "/tmp/slots.json"
    assert summary.normal_used == 3
    assert summary.normal_limit == 1
    assert summary.normal_available == 0
    assert summary.emergency_usage == 1
