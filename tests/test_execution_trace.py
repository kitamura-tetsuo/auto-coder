import asyncio
import collections
import threading
import time

import pytest

from src.auto_coder.execution_trace import (
    SCHEMA_VERSION,
    EventKind,
    ExecutionScope,
    Outcome,
    TraceCollector,
    bind_scope,
    current_scope,
    get_trace_collector,
)


@pytest.fixture(autouse=True)
def reset_collector():
    TraceCollector._instance = None
    yield
    TraceCollector._instance = None


class TestSchemaFields:
    """REQ-001: every structured event carries the full required field set."""

    def test_execution_started_event_fields(self):
        collector = get_trace_collector()
        with collector.start_execution("o/r", "issue", 42, origin="test") as handle:
            handle.set_outcome(Outcome.COMPLETED)

        snapshot = collector.get_snapshot()
        assert len(snapshot.events) == 2  # started + finished
        started = snapshot.events[0]
        assert started.schema_version == SCHEMA_VERSION
        assert started.repository == "o/r"
        assert started.item_type == "issue"
        assert started.item_number == 42
        assert started.process_run_id == collector.process_run_id
        assert started.execution_id == handle.scope.execution_id
        assert started.execution_start_sequence == handle.scope.start_sequence
        assert started.sequence == handle.scope.start_sequence
        assert started.origin == "test"
        assert started.kind == EventKind.EXECUTION_STARTED.value
        assert started.outcome is None  # unknown until finished, never invented

        finished = snapshot.events[1]
        assert finished.kind == EventKind.EXECUTION_FINISHED.value
        assert finished.outcome == Outcome.COMPLETED.value

    def test_missing_outcome_stays_unknown_not_successful(self):
        collector = get_trace_collector()
        collector.record_event(EventKind.STAGE_RESULT, stage_id="stage.a", origin="test")
        event = collector.get_snapshot().events[0]
        assert event.outcome is None  # never silently "successful"


class TestExecutionIdentity:
    """AS-001: same item, same attempt/head/timestamp is not the same execution."""

    def test_two_identical_evaluations_get_distinct_executions(self):
        collector = get_trace_collector()
        fixed_ts = time.time()

        with collector.start_execution("o/r", "issue", 42, origin="test") as handle_a:
            handle_a.set_outcome(Outcome.COMPLETED)
        with collector.start_execution("o/r", "issue", 42, origin="test") as handle_b:
            handle_b.set_outcome(Outcome.COMPLETED)

        assert handle_a.scope.execution_id != handle_b.scope.execution_id
        assert handle_a.scope.start_sequence < handle_b.scope.start_sequence

        snapshot = collector.get_snapshot(item_type="issue", item_number=42)
        sequences = [e.sequence for e in snapshot.events]
        assert sequences == sorted(sequences)  # deterministic publication order

        # Completion never asserts issue closure / implementation success by itself.
        for event in snapshot.events:
            assert not hasattr(event, "issue_closed")
            assert not hasattr(event, "implemented")


class TestAsyncAndThreadPropagation:
    """AS-002/AS-003: explicit scope propagation across tasks and threads, isolated."""

    def test_concurrent_tasks_with_thread_handoff_stay_isolated(self):
        collector = get_trace_collector()

        def thread_body(scope, results, key):
            with bind_scope(scope):
                collector.record_event(EventKind.STAGE_STARTED, stage_id="nested.thread", origin="test")
                results[key] = current_scope()

        async def worker(item_number, should_fail, results):
            with collector.start_execution("o/r", "issue", item_number, origin="test") as handle:
                t = threading.Thread(target=thread_body, args=(handle.scope, results, item_number))
                t.start()
                t.join()
                await asyncio.sleep(0)
                if should_fail:
                    raise RuntimeError("boom")
                handle.set_outcome(Outcome.COMPLETED)
            return handle.scope

        async def main():
            results = {}
            task_ok = asyncio.create_task(worker(1, False, results))
            task_fail = asyncio.create_task(worker(2, True, results))
            done = await asyncio.gather(task_ok, task_fail, return_exceptions=True)
            return results, done

        results, done = asyncio.run(main())

        assert isinstance(done[0], ExecutionScope)
        assert isinstance(done[1], RuntimeError)

        # The thread nested under each task recorded events scoped to that
        # task's own execution -- no crossover between the two items.
        assert results[1].execution_id != results[2].execution_id
        assert results[1].item_number == 1
        assert results[2].item_number == 2

        # No leakage back into the caller's context after either task exits.
        assert current_scope() is None

        snapshot = collector.get_snapshot()
        by_execution = {}
        for event in snapshot.events:
            by_execution.setdefault(event.execution_id, set()).add(event.item_number)
        for item_numbers in by_execution.values():
            assert len(item_numbers) == 1  # every execution's events stay on one item

        # The failed task's execution is recorded as failed, not the healthy one.
        finished = {e.execution_id: e.outcome for e in snapshot.events if e.kind == EventKind.EXECUTION_FINISHED.value}
        assert finished[results[1].execution_id] == Outcome.COMPLETED.value
        assert finished[results[2].execution_id] == Outcome.FAILED.value

    def test_late_event_stays_attributed_to_earlier_execution(self):
        collector = get_trace_collector()

        handle_a = collector.start_execution("o/r", "issue", 7, origin="test")
        handle_a.__enter__()
        scope_a = handle_a.scope

        handle_b = collector.start_execution("o/r", "issue", 7, origin="test")
        handle_b.__enter__()

        # Late, explicitly A-scoped evidence arrives after B has already started.
        late_event = collector.record_event(
            EventKind.STAGE_RESULT,
            stage_id="late.stage",
            origin="test",
            outcome=Outcome.COMPLETED,
            scope=scope_a,
        )

        assert late_event.execution_id == scope_a.execution_id
        assert late_event.execution_start_sequence == scope_a.start_sequence
        assert late_event.execution_id != handle_b.scope.execution_id

        handle_b.set_outcome(Outcome.COMPLETED)
        handle_b.__exit__(None, None, None)
        handle_a.set_outcome(Outcome.COMPLETED)
        handle_a.__exit__(None, None, None)


class TestBoundedRetention:
    """AS-004: bounded storage, truthful truncation, mutation insulation."""

    def test_eviction_is_reported_and_snapshot_is_insulated(self):
        TraceCollector._instance = None
        collector = TraceCollector(max_events=3, max_executions=2)

        collector.record_event(EventKind.STAGE_STARTED, stage_id="s1", origin="test", facts={"n": 1})
        collector.record_event(EventKind.STAGE_STARTED, stage_id="s2", origin="test")
        collector.record_event(EventKind.STAGE_STARTED, stage_id="s3", origin="test")
        collector.record_event(EventKind.STAGE_STARTED, stage_id="s4", origin="test")

        snapshot = collector.get_snapshot()
        assert len(snapshot.events) == 3
        assert snapshot.events_truncated is True

        # Mutating the returned facts dict must not affect retained evidence.
        snapshot2 = collector.get_snapshot()
        for event in snapshot2.events:
            if event.facts is not None:
                event.facts["n"] = 999
        snapshot3 = collector.get_snapshot()
        for event in snapshot3.events:
            if event.facts is not None:
                assert event.facts["n"] != 999

        with collector.start_execution("o/r", "issue", 1, origin="test") as h1:
            h1.set_outcome(Outcome.COMPLETED)
        with collector.start_execution("o/r", "issue", 2, origin="test") as h2:
            h2.set_outcome(Outcome.COMPLETED)
        with collector.start_execution("o/r", "issue", 3, origin="test") as h3:
            h3.set_outcome(Outcome.COMPLETED)

        snapshot4 = collector.get_snapshot()
        assert snapshot4.execution_metadata_truncated is True
        assert len(snapshot4.executions) <= 2

        # A fresh process instance starts with empty history / no resurrected state.
        TraceCollector._instance = None
        fresh = TraceCollector()
        fresh_snapshot = fresh.get_snapshot()
        assert fresh_snapshot.events == []
        assert fresh_snapshot.executions == {}
        assert fresh_snapshot.process_run_id != collector.process_run_id


class TestExtensibilityAndLegacy:
    """AS-005: new stage identifiers pass through; legacy/unsupported stay identifiable."""

    def test_unseen_stage_identifier_preserved_unchanged(self):
        collector = get_trace_collector()
        collector.record_event(
            EventKind.STAGE_RESULT,
            stage_id="provider.example.new-stage",
            origin="test",
            label="Brand New Stage",
            outcome=Outcome.COMPLETED,
            facts={"detail": "abc"},
        )
        event = collector.get_snapshot().events[0]
        assert event.stage_id == "provider.example.new-stage"
        assert event.label == "Brand New Stage"
        assert event.facts == {"detail": "abc"}
        assert event.outcome == Outcome.COMPLETED.value

    def test_legacy_message_and_unsupported_version_are_identifiable(self):
        collector = get_trace_collector()

        legacy = collector.record_legacy_or_raw({"category": "Queue", "message": "queued item"})
        assert legacy.legacy is True
        assert legacy.execution_id is None

        unsupported = collector.record_legacy_or_raw({"schema_version": 999, "category": "Queue"})
        assert unsupported.supported is False
        # Never silently promoted into a valid successful execution.
        assert unsupported.execution_id is None
        assert unsupported.outcome is None


class TestNonAuthoritativeDiagnostics:
    """AS-006/REQ-008: recorder failures never change business results."""

    def business_operation(self, collector, should_raise, scope=None):
        with collector.start_execution("o/r", "issue", 99, origin="test") as handle:
            collector.record_event(EventKind.STAGE_STARTED, stage_id="work", origin="test", scope=scope or handle.scope)
            if should_raise:
                raise ValueError("business failure")
            handle.set_outcome(Outcome.COMPLETED)
            return "business-result"

    def test_recorder_failure_does_not_change_business_outcome(self):
        collector = get_trace_collector()

        result_healthy = self.business_operation(collector, should_raise=False)
        assert result_healthy == "business-result"

        with pytest.raises(ValueError):
            self.business_operation(collector, should_raise=True)

        # Break the underlying sink (not the safety wrapper itself) and confirm
        # business behavior is identical: `_publish`'s own try/except must
        # swallow this, per REQ-008 ("ordinary trace-sink failures must not
        # fail or block automation").
        class ExplodingDeque(collections.deque):
            def append(self, item):
                raise RuntimeError("trace sink is down")

        collector._events = ExplodingDeque(maxlen=collector._events.maxlen)

        result_with_broken_sink = self.business_operation(collector, should_raise=False)
        assert result_with_broken_sink == "business-result"

        with pytest.raises(ValueError):
            self.business_operation(collector, should_raise=True)
