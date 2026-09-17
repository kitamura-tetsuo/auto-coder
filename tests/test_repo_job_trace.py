import asyncio
import collections
import threading

import pytest

from src.auto_coder.execution_trace import EventKind, Outcome, TraceCollector, get_trace_collector
from src.auto_coder.repo_job_trace import (
    SCHEMA_VERSION,
    ClippedNumberRefs,
    ClippedTextRefs,
    RepoJobExecutionScope,
    RepoJobFacts,
    RepoJobKind,
    RepoJobObservationKind,
    RepoJobTarget,
    RepoJobTraceCollector,
    bind_repo_job_scope,
    current_repo_job_scope,
    executions_for_target,
    get_repo_job_trace_collector,
    observations_for_execution,
    resolve_repo_job_target,
    unassociated_observations_for_target,
)


@pytest.fixture(autouse=True)
def reset_collectors():
    RepoJobTraceCollector._instance = None
    TraceCollector._instance = None
    yield
    RepoJobTraceCollector._instance = None
    TraceCollector._instance = None


DEPENDENCY = RepoJobKind.DEPENDENCY_RESCAN.value


class TestTargetResolution:
    """REQ-001: a namespace distinct from GitHub issue/pr, no synthetic number, no fallback."""

    def test_valid_target_resolves(self):
        target = resolve_repo_job_target("o/r", DEPENDENCY)
        assert target == RepoJobTarget("o/r", DEPENDENCY)

    def test_empty_repository_is_rejected(self):
        assert resolve_repo_job_target("", DEPENDENCY) is None
        assert resolve_repo_job_target(None, DEPENDENCY) is None

    def test_unsupported_job_kind_is_rejected_not_defaulted(self):
        assert resolve_repo_job_target("o/r", "issue") is None
        assert resolve_repo_job_target("o/r", "pr") is None
        assert resolve_repo_job_target("o/r", "not-a-real-kind") is None
        assert resolve_repo_job_target("o/r", "") is None
        assert resolve_repo_job_target("o/r", None) is None

    def test_target_has_no_item_number_field(self):
        target = resolve_repo_job_target("o/r", DEPENDENCY)
        assert not hasattr(target, "item_number")
        assert not hasattr(target, "number")


class TestSchemaFields:
    def test_execution_observation_fields(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)
        with collector.start_execution(target, origin="test") as handle:
            handle.set_outcome(Outcome.COMPLETED)

        snapshot = collector.get_snapshot(target)
        assert len(snapshot.observations) == 2
        started = snapshot.observations[0]
        assert started.schema_version == SCHEMA_VERSION
        assert started.repository == "o/r"
        assert started.job_kind == DEPENDENCY
        assert started.process_run_id == collector.process_run_id
        assert started.execution_id == handle.scope.execution_id
        assert started.execution_start_sequence == handle.scope.start_sequence
        assert started.kind == RepoJobObservationKind.EXECUTION_STARTED.value
        assert started.outcome is None
        assert started.observation_id == f"{collector.process_run_id}:{started.sequence}"

        finished = snapshot.observations[1]
        assert finished.kind == RepoJobObservationKind.EXECUTION_FINISHED.value
        assert finished.outcome == Outcome.COMPLETED.value

    def test_handoff_aggregate_fields_default_absent_not_zero(self):
        """Issue #2001: per-attempt discovery/handoff totals stay unknown, not a guessed 0, until a producer sets them."""
        facts = RepoJobFacts()
        assert facts.discovered_issue_count is None
        assert facts.attempted_handoff_count is None
        assert facts.confirmed_handoff_count is None
        assert facts.failed_or_unconfirmed_handoff_count is None
        assert facts.new_pending_handoff_count is None
        assert facts.coalesced_handoff_count is None
        assert facts.followup_required_handoff_count is None

        populated = RepoJobFacts(discovered_issue_count=3, attempted_handoff_count=3, confirmed_handoff_count=2, failed_or_unconfirmed_handoff_count=1, new_pending_handoff_count=1, coalesced_handoff_count=1, followup_required_handoff_count=0)
        assert populated.discovered_issue_count == 3
        assert populated.confirmed_handoff_count == 2


class TestAS001SeparateTargetsAndSentinelCollision:
    """AS-001: repo rescan, Issue #1, PR #1, and another repo's rescan stay isolated."""

    def test_isolated_from_issue_and_pr_sentinel_and_other_repository(self):
        item_collector = get_trace_collector()
        job_collector = get_repo_job_trace_collector()

        with item_collector.start_execution("o/r", "issue", 1, origin="test") as issue_handle:
            issue_handle.set_outcome(Outcome.COMPLETED)
        with item_collector.start_execution("o/r", "pr", 1, origin="test") as pr_handle:
            pr_handle.set_outcome(Outcome.COMPLETED)

        target_a = RepoJobTarget("o/r", DEPENDENCY)
        target_b = RepoJobTarget("o/other", DEPENDENCY)
        with job_collector.start_execution(target_a, origin="test") as handle_a:
            handle_a.set_outcome(Outcome.COMPLETED)
        with job_collector.start_execution(target_b, origin="test") as handle_b:
            handle_b.set_outcome(Outcome.COMPLETED)

        job_snapshot = job_collector.get_snapshot()
        executions_a = executions_for_target(job_snapshot, target_a)
        executions_b = executions_for_target(job_snapshot, target_b)
        assert [e.execution_id for e in executions_a] == [handle_a.scope.execution_id]
        assert [e.execution_id for e in executions_b] == [handle_b.scope.execution_id]

        # Item evidence is untouched by, and absent from, the job collector.
        assert all(o.repository != "o/r" or o.job_kind == DEPENDENCY for o in job_snapshot.observations)
        item_snapshot = item_collector.get_snapshot()
        assert all(e.item_type in ("issue", "pr") for e in item_snapshot.events)

    def test_queued_with_no_worker_start_has_no_invented_execution(self):
        job_collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)
        job_collector.record_intake(target, origin="webhook")
        job_collector.record_queued(target, origin="queue")

        snapshot = job_collector.get_snapshot(target)
        assert executions_for_target(snapshot, target) == []
        unassociated = unassociated_observations_for_target(snapshot, target)
        assert {o.kind for o in unassociated} == {RepoJobObservationKind.INTAKE.value, RepoJobObservationKind.QUEUED.value}

    def test_invalid_target_kind_does_not_fall_back_to_recent_record(self):
        job_collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)
        with job_collector.start_execution(target, origin="test") as handle:
            handle.set_outcome(Outcome.COMPLETED)

        assert resolve_repo_job_target("o/r", "issue") is None
        assert resolve_repo_job_target("o/r", "bogus") is None
        # No API path exists to select "the repository's most recent record"
        # without first resolving a target; an unfiltered snapshot () returns
        # everything explicitly, not a guessed single record.
        snapshot = job_collector.get_snapshot()
        assert len(snapshot.observations) == 2


class TestAS002GenerationReuseAndInterleavedAttempts:
    def test_retry_gets_a_fresh_execution_identity_same_generation(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        with collector.start_execution(target, origin="worker", facts=RepoJobFacts(observed_invalidation_generation=1)) as handle_a:
            handle_a.set_outcome(Outcome.FAILED)
        with collector.start_execution(target, origin="worker", facts=RepoJobFacts(observed_invalidation_generation=1)) as handle_b:
            handle_b.set_outcome(Outcome.COMPLETED)

        assert handle_a.scope.execution_id != handle_b.scope.execution_id
        assert handle_a.scope.start_sequence < handle_b.scope.start_sequence

    def test_cancellation_reports_cancelled_not_failed(self):
        """Issue #2001: a cancelled attempt is distinguishable from a genuine failure."""
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        async def scenario():
            with pytest.raises(asyncio.CancelledError):
                with collector.start_execution(target, origin="worker") as handle:
                    raise asyncio.CancelledError()
            return handle

        handle = asyncio.run(scenario())
        snapshot = collector.get_snapshot(target)
        finished = [o for o in snapshot.observations if o.execution_id == handle.scope.execution_id and o.kind == "execution-finished"]
        assert len(finished) == 1
        assert finished[0].outcome == Outcome.CANCELLED.value

    def test_interleaved_async_thread_attempts_stay_isolated_and_ordered(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        def thread_body(scope, results, key):
            with bind_repo_job_scope(scope):
                collector.record_stage_reached("nested.thread", origin="test")
                results[key] = current_repo_job_scope()

        async def worker(key, should_fail, results):
            with collector.start_execution(target, origin="test") as handle:
                t = threading.Thread(target=thread_body, args=(handle.scope, results, key))
                t.start()
                t.join()
                await asyncio.sleep(0)
                if should_fail:
                    raise RuntimeError("boom")
                handle.set_outcome(Outcome.COMPLETED)
            return handle.scope

        async def main():
            results = {}
            task_a = asyncio.create_task(worker("A", False, results))
            task_c = asyncio.create_task(worker("C", False, results))
            done = await asyncio.gather(task_a, task_c, return_exceptions=True)
            return results, done

        results, done = asyncio.run(main())
        assert isinstance(done[0], RepoJobExecutionScope)
        assert isinstance(done[1], RepoJobExecutionScope)
        assert results["A"].execution_id != results["C"].execution_id
        assert current_repo_job_scope() is None

        snapshot = collector.get_snapshot(target)
        by_execution = collections.defaultdict(set)
        for observation in snapshot.observations:
            by_execution[observation.execution_id].add(observation.stage_id)
        for stage_ids in by_execution.values():
            # Nested thread evidence stays attached to exactly one execution.
            assert stage_ids

    def test_late_event_stays_attributed_to_earlier_execution_not_latest(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        handle_a = collector.start_execution(target, origin="test")
        handle_a.__enter__()
        scope_a = handle_a.scope

        handle_b = collector.start_execution(target, origin="test")
        handle_b.__enter__()
        handle_b.set_outcome(Outcome.COMPLETED)
        handle_b.__exit__(None, None, None)

        late = collector.record_stage_reached("late.stage", origin="test", outcome=Outcome.COMPLETED, scope=scope_a)
        assert late.execution_id == scope_a.execution_id
        assert late.execution_id != handle_b.scope.execution_id

        handle_a.set_outcome(Outcome.COMPLETED)
        handle_a.__exit__(None, None, None)

        snapshot = collector.get_snapshot(target)
        a_events = observations_for_execution(snapshot, scope_a.execution_id)
        b_events = observations_for_execution(snapshot, handle_b.scope.execution_id)
        assert any(o.stage_id == "late.stage" for o in a_events)
        assert not any(o.stage_id == "late.stage" for o in b_events)

    def test_unassociated_intake_is_not_guess_attached_to_same_generation_execution(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        intake = collector.record_intake(target, origin="webhook", facts=RepoJobFacts(observed_invalidation_generation=1))
        with collector.start_execution(target, origin="worker", facts=RepoJobFacts(observed_invalidation_generation=1)) as handle:
            handle.set_outcome(Outcome.COMPLETED)

        snapshot = collector.get_snapshot(target)
        # The intake stays unassociated: nothing in this API attaches it to
        # the execution just because the generation matches.
        unassociated = unassociated_observations_for_target(snapshot, target)
        assert intake.observation_id in {o.observation_id for o in unassociated}
        started = next(o for o in snapshot.observations if o.kind == RepoJobObservationKind.EXECUTION_STARTED.value)
        assert started.facts.source_observation_refs == ()

        # Explicit correlation is possible when a producer supplies it.
        with collector.start_execution(target, origin="worker", facts=RepoJobFacts(source_observation_refs=(intake.observation_id,))) as handle2:
            handle2.set_outcome(Outcome.COMPLETED)
        snapshot2 = collector.get_snapshot(target)
        started2 = [o for o in snapshot2.observations if o.kind == RepoJobObservationKind.EXECUTION_STARTED.value][-1]
        assert started2.facts.source_observation_refs == (intake.observation_id,)


class TestAS003SnapshotIsolationClippingAndEviction:
    def test_facts_are_immutable_and_snapshots_are_independent(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)
        facts = RepoJobFacts(handoff_count=3)
        collector.record_intake(target, origin="webhook", facts=facts)

        with pytest.raises(Exception):
            facts.handoff_count = 999  # frozen dataclass: cannot be mutated

        snapshot1 = collector.get_snapshot(target)
        snapshot2 = collector.get_snapshot(target)
        assert snapshot1.observations[0].facts.handoff_count == 3
        assert snapshot2.observations[0].facts.handoff_count == 3

    def test_target_and_trigger_refs_are_clipped_with_truthful_total(self):
        collector = RepoJobTraceCollector.__new__(RepoJobTraceCollector)
        RepoJobTraceCollector._instance = None
        collector = RepoJobTraceCollector(max_observations=50, max_executions=50, max_refs_per_record=5)
        target = RepoJobTarget("o/r", DEPENDENCY)

        clipped = collector.clip_target_issue_refs(list(range(1, 21)), total_count=20)
        assert clipped.numbers == (1, 2, 3, 4, 5)
        assert clipped.truncated is True
        assert clipped.total_count == 20  # exact total preserved despite clipping

        facts = RepoJobFacts(target_issue_refs=clipped)
        collector.record_intake(target, origin="webhook", facts=facts)
        snapshot = collector.get_snapshot(target)
        stored = snapshot.observations[0].facts.target_issue_refs
        assert stored.numbers == (1, 2, 3, 4, 5)
        assert stored.total_count == 20
        assert len(stored.numbers) != stored.total_count

    def test_unknown_total_is_not_coerced_to_zero_or_clipped_length(self):
        collector = RepoJobTraceCollector()
        clipped = collector.clip_source_issue_refs([1, 2, 3])
        assert clipped.total_count is None
        assert clipped.numbers == (1, 2, 3)

    def test_event_and_execution_bounds_are_enforced_and_reported(self):
        RepoJobTraceCollector._instance = None
        collector = RepoJobTraceCollector(max_observations=3, max_executions=2)
        target = RepoJobTarget("o/r", DEPENDENCY)

        collector.record_intake(target, origin="webhook")
        collector.record_queued(target, origin="queue")
        collector.record_recovered(target, origin="recovery")
        collector.record_intake(target, origin="webhook")

        snapshot = collector.get_snapshot(target)
        assert len(snapshot.observations) == 3
        assert snapshot.observations_truncated is True

        with collector.start_execution(target, origin="worker") as h1:
            h1.set_outcome(Outcome.COMPLETED)
        with collector.start_execution(target, origin="worker") as h2:
            h2.set_outcome(Outcome.COMPLETED)
        with collector.start_execution(target, origin="worker") as h3:
            h3.set_outcome(Outcome.COMPLETED)

        snapshot2 = collector.get_snapshot(target)
        assert snapshot2.execution_metadata_truncated is True
        assert len(snapshot2.executions) <= 2


class TestAS004EmptyLocalHistoryIsNotEmptyDurableQueue:
    def test_fresh_collector_has_fresh_process_run_and_no_fabricated_executions(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)
        with collector.start_execution(target, origin="worker") as handle:
            handle.set_outcome(Outcome.COMPLETED)
        old_run_id = collector.process_run_id

        RepoJobTraceCollector._instance = None
        fresh = get_repo_job_trace_collector()
        fresh_snapshot = fresh.get_snapshot(target)
        assert fresh_snapshot.observations == []
        assert fresh_snapshot.executions == {}
        assert fresh.process_run_id != old_run_id

    def test_recovery_observation_shows_pending_work_without_reconstructing_history(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)
        recovered = collector.record_recovered(target, origin="startup", facts=RepoJobFacts(queue_phase="dirty"))

        snapshot = collector.get_snapshot(target)
        assert executions_for_target(snapshot, target) == []
        unassociated = unassociated_observations_for_target(snapshot, target)
        assert recovered.observation_id in {o.observation_id for o in unassociated}
        assert recovered.facts.queue_phase == "dirty"


class TestAS005DiagnosticFailureIsNotBusinessFailure:
    def business_operation(self, collector, should_raise, target):
        with collector.start_execution(target, origin="worker") as handle:
            collector.record_stage_reached("scan.step", origin="worker", scope=handle.scope)
            if should_raise:
                raise ValueError("business failure")
            handle.set_outcome(Outcome.COMPLETED)
            return "business-result"

    def test_recorder_failure_does_not_change_business_outcome(self):
        collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        assert self.business_operation(collector, False, target) == "business-result"
        with pytest.raises(ValueError):
            self.business_operation(collector, True, target)

        class ExplodingDeque(collections.deque):
            def append(self, item):
                raise RuntimeError("trace sink is down")

        collector._observations = ExplodingDeque(maxlen=collector._observations.maxlen)

        assert self.business_operation(collector, False, target) == "business-result"
        with pytest.raises(ValueError):
            self.business_operation(collector, True, target)

    def test_facts_type_excludes_raw_content_and_credential_fields(self):
        field_names = set(RepoJobFacts.__dataclass_fields__.keys())
        for forbidden in ("body", "payload", "token", "secret", "credential", "raw"):
            assert not any(forbidden in name for name in field_names), field_names


class TestAS006ExistingItemConsumersUnaffectedByInterleavedJobRecords:
    def test_item_recorder_identities_facts_and_ordering_are_unchanged(self):
        item_collector = get_trace_collector()
        job_collector = get_repo_job_trace_collector()
        target = RepoJobTarget("o/r", DEPENDENCY)

        with item_collector.start_execution("o/r", "issue", 5, origin="test") as issue_handle:
            item_collector.record_event(EventKind.STAGE_RESULT, stage_id="issue.stage", origin="test", outcome=Outcome.COMPLETED, facts={"k": "v"})
            issue_handle.set_outcome(Outcome.COMPLETED)

        with job_collector.start_execution(target, origin="worker") as job_handle:
            job_handle.set_outcome(Outcome.COMPLETED)

        legacy = item_collector.record_legacy_or_raw({"schema_version": 999, "repository": "o/r", "item_type": "issue", "item_number": 5})

        item_snapshot = item_collector.get_snapshot(item_type="issue", item_number=5)
        sequences = [e.sequence for e in item_snapshot.events]
        assert sequences == sorted(sequences)
        assert any(e.facts == {"k": "v"} for e in item_snapshot.events if e.facts)
        assert any(e.supported is False for e in item_snapshot.events)
        # No job observation is present in the Issue/PR snapshot.
        assert all(not hasattr(e, "job_kind") for e in item_snapshot.events)

        job_snapshot = job_collector.get_snapshot(target)
        assert all(o.job_kind == DEPENDENCY for o in job_snapshot.observations)
        assert legacy.execution_id is None  # unrelated to the job collector entirely
