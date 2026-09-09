"""Unit tests for the pure dashboard detail-view logic in `dashboard_detail.py`.

These exercise selection, diagram-building, escaping, and evidence
projection directly against the real `TraceCollector`, independent of any
NiceGUI rendering.
"""

import pytest

from src.auto_coder.dashboard_detail import (
    SelectionMode,
    build_observed_path_diagram,
    escape_for_mermaid_label,
    events_for_execution,
    evidence_rows,
    execution_finished_event,
    execution_start_event_present,
    executions_for_item,
    format_fact_value,
    is_resolvable_item_number,
    is_supported_item_type,
    resolve_selected_execution,
    unscoped_events_for_item,
)
from src.auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


class TestItemValidation:
    def test_supported_item_types(self):
        assert is_supported_item_type("issue")
        assert is_supported_item_type("pr")
        assert not is_supported_item_type("commit")
        assert not is_supported_item_type("")

    def test_resolvable_item_number(self):
        assert is_resolvable_item_number(1)
        assert not is_resolvable_item_number(0)
        assert not is_resolvable_item_number(-5)
        assert not is_resolvable_item_number(True)  # bool is not a real item number


class TestMermaidEscaping:
    def test_safe_characters_pass_through(self):
        assert escape_for_mermaid_label("hello world 123") == "hello world 123"

    def test_quotes_brackets_and_arrows_are_neutralized(self):
        raw = '"];click x call evil() -->|x| Y'
        escaped = escape_for_mermaid_label(raw)
        assert '"' not in escaped
        assert "[" not in escaped
        assert "]" not in escaped
        assert "-->" not in escaped
        assert "|" not in escaped

    def test_html_and_script_tags_are_neutralized(self):
        raw = "<script>alert(1)</script>"
        escaped = escape_for_mermaid_label(raw)
        assert "<" not in escaped
        assert ">" not in escaped

    def test_newlines_become_spaces(self):
        assert "\n" not in escape_for_mermaid_label("line1\nline2")
        assert "\r" not in escape_for_mermaid_label("line1\r\nline2")

    def test_mermaid_directive_injection_is_inert(self):
        raw = 'end\n    subgraph evil["gotcha"]'
        escaped = escape_for_mermaid_label(raw)
        assert "\n" not in escaped
        assert "[" not in escaped
        assert "]" not in escaped
        assert '"' not in escaped


class TestFormatFactValue:
    def test_none_is_explicit_not_empty(self):
        assert format_fact_value(None) == "(not recorded)"

    def test_booleans_are_literal_not_coerced(self):
        assert format_fact_value(True) == "true"
        assert format_fact_value(False) == "false"

    def test_structured_values_render_as_json(self):
        assert format_fact_value({"a": 1}) == '{"a": 1}'


class TestExecutionSelectionOrdering:
    """AS-002 / AS-003: ordering is by start_sequence, never wall clock/index."""

    def test_executions_ordered_by_start_sequence_not_creation_call_order(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "issue", 1, origin="t") as a:
            a.set_outcome(Outcome.COMPLETED)
        with collector.start_execution("o/r", "issue", 1, origin="t") as b:
            b.set_outcome(Outcome.COMPLETED)

        snapshot = collector.get_snapshot()
        executions = executions_for_item(snapshot, "o/r", "issue", 1)
        assert [e.execution_id for e in executions] == [a.scope.execution_id, b.scope.execution_id]

    def test_other_repository_or_item_excluded(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "issue", 1, origin="t"):
            pass
        with collector.start_execution("o/other", "issue", 1, origin="t"):
            pass
        with collector.start_execution("o/r", "issue", 2, origin="t"):
            pass
        with collector.start_execution("o/r", "pr", 1, origin="t"):
            pass

        snapshot = collector.get_snapshot()
        executions = executions_for_item(snapshot, "o/r", "issue", 1)
        assert len(executions) == 1

    def test_late_event_for_older_execution_does_not_move_it(self):
        """Two executions of the same item; a late explicitly-scoped event for the
        older one must not reattribute it to the newer one, and its start
        sequence must not change (REQ-003, AS-003)."""
        collector = get_trace_collector()
        handle_a = collector.start_execution("o/r", "issue", 1, origin="t")
        scope_a = handle_a.__enter__()
        handle_b = collector.start_execution("o/r", "issue", 1, origin="t")
        scope_b = handle_b.__enter__()
        handle_b.set_outcome(Outcome.COMPLETED)
        handle_b.__exit__(None, None, None)

        # Late event explicitly scoped to A, recorded after B started/finished.
        collector.record_event(EventKind.STAGE_RESULT, stage_id="late.stage", origin="t", outcome=Outcome.COMPLETED, scope=scope_a.scope)
        handle_a.set_outcome(Outcome.COMPLETED)
        handle_a.__exit__(None, None, None)

        snapshot = collector.get_snapshot()
        executions = executions_for_item(snapshot, "o/r", "issue", 1)
        assert [e.execution_id for e in executions] == [scope_a.scope.execution_id, scope_b.scope.execution_id]

        a_events = events_for_execution(snapshot, scope_a.scope.execution_id)
        assert any(e.stage_id == "late.stage" for e in a_events)
        b_events = events_for_execution(snapshot, scope_b.scope.execution_id)
        assert not any(e.stage_id == "late.stage" for e in b_events)


class TestResolveSelectedExecution:
    def test_follow_latest_picks_newest_start_sequence(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "issue", 1, origin="t"):
            pass
        with collector.start_execution("o/r", "issue", 1, origin="t") as b:
            pass
        snapshot = collector.get_snapshot()
        executions = executions_for_item(snapshot, "o/r", "issue", 1)

        selection = resolve_selected_execution(executions, SelectionMode.FOLLOW_LATEST, None)
        assert selection.execution is not None
        assert selection.execution.execution_id == b.scope.execution_id
        assert not selection.pinned_evicted

    def test_pinned_execution_stays_pinned_when_a_newer_one_starts(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "issue", 1, origin="t") as a:
            pass
        executions = executions_for_item(collector.get_snapshot(), "o/r", "issue", 1)
        selection = resolve_selected_execution(executions, SelectionMode.PINNED, a.scope.execution_id)
        assert selection.execution.execution_id == a.scope.execution_id

        with collector.start_execution("o/r", "issue", 1, origin="t"):
            pass
        executions = executions_for_item(collector.get_snapshot(), "o/r", "issue", 1)
        selection = resolve_selected_execution(executions, SelectionMode.PINNED, a.scope.execution_id)
        assert selection.execution.execution_id == a.scope.execution_id
        assert not selection.pinned_evicted

    def test_evicted_pinned_execution_is_reported_not_substituted(self):
        """The pinned id is looked up by identity; if retention drops it, no
        other execution takes its former index (REQ-004, AS-003)."""
        executions = []  # simulates the pinned execution having been evicted
        selection = resolve_selected_execution(executions, SelectionMode.PINNED, "some-evicted-id")
        assert selection.execution is None
        assert selection.pinned_evicted

    def test_follow_latest_with_no_executions_is_explicit_absence(self):
        selection = resolve_selected_execution([], SelectionMode.FOLLOW_LATEST, None)
        assert selection.execution is None
        assert not selection.pinned_evicted


class TestObservedPathDiagram:
    """AS-001: repeated/unknown stages all render without a hard-coded node map."""

    def test_empty_events_yields_empty_diagram(self):
        assert build_observed_path_diagram([]) == ""

    def test_repeated_stage_and_unknown_stage_both_render_as_distinct_nodes(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "pr", 7, origin="t") as handle:
            collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.repeatable", origin="t", outcome=Outcome.COMPLETED)
            collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.repeatable", origin="t", outcome=Outcome.FAILED)
            collector.record_event(EventKind.STAGE_RESULT, stage_id="provider.example.new-stage", origin="t", outcome=Outcome.BLOCKED)
            handle.set_outcome(Outcome.COMPLETED)

        snapshot = collector.get_snapshot()
        events = events_for_execution(snapshot, handle.scope.execution_id)
        diagram = build_observed_path_diagram(events)

        assert diagram.count('n0["') == 1
        # 5 events -> 5 distinct nodes (start, two repeatable, new stage, finished)
        assert len(events) == 5
        for i in range(len(events)):
            assert f"n{i}[" in diagram
        assert "provider.example.new-stage" in diagram
        assert "observed order" in diagram

    def test_edges_are_labeled_observed_order_not_causality(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "pr", 1, origin="t") as handle:
            handle.set_outcome(Outcome.COMPLETED)
        snapshot = collector.get_snapshot()
        events = events_for_execution(snapshot, handle.scope.execution_id)
        diagram = build_observed_path_diagram(events)
        assert "-->|observed order|" in diagram
        assert "-->|causes|" not in diagram


class TestEvidenceRows:
    def test_only_stage_result_and_finished_events_with_evidence_are_rows(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "pr", 1, origin="t") as handle:
            collector.record_event(EventKind.STAGE_STARTED, stage_id="pr.stage", origin="t")
            collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.stage", origin="t", outcome=Outcome.DEFERRED, facts={"reason": "usage limit"})
            handle.finish(Outcome.COMPLETED, facts={"pr_number": 42})

        snapshot = collector.get_snapshot()
        events = events_for_execution(snapshot, handle.scope.execution_id)
        rows = evidence_rows(events)

        outcomes = {row["outcome"] for row in rows}
        assert "deferred" in outcomes
        assert "completed" in outcomes
        assert all("reason: usage limit" in row["facts"] or "pr_number: 42" in row["facts"] for row in rows)

    def test_unknown_outcome_is_not_coerced_to_success_or_failure(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "pr", 1, origin="t") as handle:
            collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.ci", origin="t", facts={"available": False})
            handle.set_outcome(Outcome.UNKNOWN)

        snapshot = collector.get_snapshot()
        events = events_for_execution(snapshot, handle.scope.execution_id)
        rows = evidence_rows(events)
        assert all(row["outcome"] in ("unknown",) for row in rows if row["stage"] == "pr.ci")


class TestUnscopedAndLegacyEvidence:
    def test_events_without_execution_are_unscoped_not_guessed(self):
        collector = get_trace_collector()
        collector.record_event(EventKind.STAGE_RESULT, stage_id="pr.strict-refresh", origin="t", outcome=Outcome.DEFERRED)
        snapshot = collector.get_snapshot()
        unscoped = unscoped_events_for_item(snapshot, "", "", -1)
        assert len(unscoped) == 1
        assert unscoped[0].execution_id is None

    def test_unsupported_schema_record_is_marked_not_promoted(self):
        collector = get_trace_collector()
        event = collector.record_legacy_or_raw({"schema_version": 999, "repository": "o/r", "item_type": "pr", "item_number": 5, "category": "Old", "message": "legacy text"})
        assert event.supported is False
        assert event.legacy is True
        snapshot = collector.get_snapshot()
        unscoped = unscoped_events_for_item(snapshot, "o/r", "pr", 5)
        assert len(unscoped) == 1
        assert unscoped[0].supported is False


class TestExecutionCompletionEvidence:
    def test_missing_finished_event_is_not_treated_as_completion(self):
        collector = get_trace_collector()
        handle = collector.start_execution("o/r", "issue", 1, origin="t")
        scope = handle.__enter__()
        snapshot = collector.get_snapshot()
        events = events_for_execution(snapshot, scope.scope.execution_id)
        assert execution_start_event_present(events)
        assert execution_finished_event(events) is None
        handle.__exit__(None, None, None)

    def test_finished_event_present_once_execution_completes(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "issue", 1, origin="t") as handle:
            handle.set_outcome(Outcome.COMPLETED)
        snapshot = collector.get_snapshot()
        events = events_for_execution(snapshot, handle.scope.execution_id)
        finished = execution_finished_event(events)
        assert finished is not None
        assert finished.outcome == Outcome.COMPLETED.value
