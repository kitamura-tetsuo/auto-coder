"""Regression tests for the invocation-level shutdown-protection model (Issue #2008).

These exercise the real InvocationAdmissionGate/InvocationHandle API across
real threads and the real asyncio event loop -- the production-reachable
boundary for this stage, per the Issue's own scope: wiring real production
callers into this model is a separate follow-up stage (#2009/#2010).
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_coder.invocation_admission import (
    DrainOutcome,
    GateState,
    InvocationAdmissionGate,
    InvocationState,
    InvocationStateError,
    current_invocation_gate,
    install_invocation_gate,
    reset_invocation_gate,
)

# ---------------------------------------------------------------------------
# AS-001 -- Admission and shutdown have two explicit outcomes
# ---------------------------------------------------------------------------


def test_concurrent_admission_and_closure_never_lose_or_double_admit():
    """Stress the real lock boundary: every racer is admitted xor refused,
    and the count of controlled provider actions run matches exactly the
    count of successful admissions (REQ-001, REQ-002)."""
    for _ in range(200):
        gate = InvocationAdmissionGate()
        worker_count = 8
        start_barrier = threading.Barrier(worker_count + 1)
        executed = 0
        executed_lock = threading.Lock()
        admitted_count = 0
        refused_count = 0
        result_lock = threading.Lock()

        def racer():
            nonlocal admitted_count, refused_count, executed
            start_barrier.wait(5)
            handle = gate.try_admit(repository="r", target="issue#1", stage="impl")
            if handle is None:
                with result_lock:
                    refused_count += 1
                return
            with result_lock:
                admitted_count += 1
            # The controlled provider action: must only ever run for an
            # actually-admitted invocation.
            with executed_lock:
                executed += 1

        threads = [threading.Thread(target=racer) for _ in range(worker_count)]
        for t in threads:
            t.start()
        start_barrier.wait(5)
        gate.close_admission("race test")
        for t in threads:
            t.join(5)

        assert admitted_count + refused_count == worker_count
        assert executed == admitted_count
        # Once close_admission has returned, admission is refused every time.
        assert gate.try_admit(repository="r", target="issue#1", stage="impl") is None


def test_task_enqueued_before_closure_but_blocked_in_preparation_is_refused():
    """A task that was scheduled before the drain started, but was still
    doing preparatory work (not yet at the final invocation boundary) when
    the gate closed, must be refused when it finally reaches that boundary."""
    gate = InvocationAdmissionGate()

    # Simulate "enqueued before closure": the task exists conceptually, but
    # has not yet called try_admit (still in preparation) when we close.
    gate.close_admission("shutdown while task still preparing")

    handle = gate.try_admit(repository="r", target="issue#2", stage="impl")
    assert handle is None


def test_draining_and_forced_gate_refuse_admission_without_provider_call():
    gate = InvocationAdmissionGate()
    gate.close_admission("drain")
    assert gate.state is GateState.DRAINING
    assert gate.try_admit(repository="r", target="issue#3", stage="impl") is None

    forced_gate = InvocationAdmissionGate()
    forced_gate.close_admission("drain")
    forced_gate.force_stop("second interrupt")
    assert forced_gate.state is GateState.FORCED
    assert forced_gate.try_admit(repository="r", target="issue#3", stage="impl") is None


# ---------------------------------------------------------------------------
# AS-002 -- Response is not the end of protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpointing_stays_protected_until_explicit_confirmation():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="r", target="issue#4", stage="impl")
    assert handle is not None

    # The controlled provider action returns.
    handle.begin_checkpointing("result")
    assert gate.unsettled_snapshot()[0].state is InvocationState.CHECKPOINTING

    gate.close_admission("shutdown")
    assert gate.is_graceful_ready is False

    # Simulate the owning caller's original waiter being cancelled by the
    # drain; cancellation must not silently settle anything.
    async def owning_waiter():
        await asyncio.sleep(10)

    waiter_task = asyncio.create_task(owning_waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task
    assert gate.is_graceful_ready is False
    assert gate.unsettled_snapshot()[0].state is InvocationState.CHECKPOINTING

    # A failed persistence attempt: still protected, may be retried.
    handle.record_checkpoint_attempt_failed("disk full")
    assert gate.is_graceful_ready is False
    assert gate.unsettled_snapshot()[0].checkpoint_failure_count == 1

    # The later successful checkpoint confirmation settles it exactly once.
    assert handle.confirm_settled(confirmation_id="ckpt-1") is True
    assert handle.confirm_settled(confirmation_id="ckpt-1") is False
    assert gate.unsettled_snapshot() == []
    assert gate.is_graceful_ready is True

    outcome = await gate.wait_until_drained(poll_interval=0.001)
    assert outcome is DrainOutcome.GRACEFUL


def test_begin_checkpointing_twice_raises_instead_of_silently_reordering():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="r", target="issue#5", stage="impl")
    assert handle is not None
    handle.begin_checkpointing("result")
    with pytest.raises(InvocationStateError):
        handle.begin_checkpointing("result-again")


def test_confirm_settled_before_checkpointing_is_a_safe_no_op():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="r", target="issue#6", stage="impl")
    assert handle is not None
    # Still IN_FLIGHT: a premature/erroneous confirmation must not settle it.
    assert handle.confirm_settled() is False
    assert gate.unsettled_snapshot()[0].state is InvocationState.IN_FLIGHT


# ---------------------------------------------------------------------------
# AS-003 -- Independent calls cannot erase each other
# ---------------------------------------------------------------------------


def test_independent_invocations_from_separate_threads_cannot_cross_settle():
    gate = InvocationAdmissionGate()
    handles = {}

    def admit(name, target):
        handles[name] = gate.try_admit(repository="r", target=target, stage="impl")

    # A and B share repository/issue/stage but get distinct invocation ids.
    t_a = threading.Thread(target=admit, args=("a", "issue#7"))
    t_b = threading.Thread(target=admit, args=("b", "issue#7"))
    t_a.start()
    t_a.join(5)
    t_b.start()
    t_b.join(5)

    handle_a, handle_b = handles["a"], handles["b"]
    assert handle_a.identity.invocation_id != handle_b.identity.invocation_id

    handle_b.begin_checkpointing("result")
    assert handle_b.confirm_settled(confirmation_id="b-1") is True

    # Duplicate confirmation for B: idempotent no-op.
    assert handle_b.confirm_settled(confirmation_id="b-1") is False

    # An old/mismatched confirmation (unknown invocation id) never settles
    # anything else, including A.
    assert gate.confirm_settled("not-a-real-invocation-id") is False

    unsettled_ids = {snap.invocation_id for snap in gate.unsettled_snapshot()}
    assert handle_a.identity.invocation_id in unsettled_ids
    assert handle_b.identity.invocation_id not in unsettled_ids


def test_retiring_one_invocation_does_not_clear_a_sibling_still_running():
    gate = InvocationAdmissionGate()
    handle_a = gate.try_admit(repository="r", target="issue#8", stage="impl")
    handle_b = gate.try_admit(repository="r", target="issue#8", stage="validation")
    assert handle_a is not None and handle_b is not None

    handle_a.begin_checkpointing("result")
    assert handle_a.confirm_settled() is True

    # B is still IN_FLIGHT and must remain fully protected.
    assert gate.is_graceful_ready is False
    remaining = gate.unsettled_snapshot()
    assert len(remaining) == 1
    assert remaining[0].invocation_id == handle_b.identity.invocation_id
    assert remaining[0].state is InvocationState.IN_FLIGHT


def test_isolation_across_daemon_scopes_on_a_recycled_executor_thread():
    """Simulate a real recycled worker thread being reused by two different
    daemon lifetimes (REQ-005, REQ-007)."""
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        gate_1 = InvocationAdmissionGate()
        handle_1 = executor.submit(lambda: gate_1.try_admit(repository="r", target="issue#9", stage="impl")).result(5)
        assert handle_1 is not None

        # A brand-new daemon lifetime's gate, run on the very same thread.
        gate_2 = InvocationAdmissionGate()
        handle_2 = executor.submit(lambda: gate_2.try_admit(repository="r", target="issue#9", stage="impl")).result(5)
        assert handle_2 is not None
        assert handle_2.identity.daemon_scope != handle_1.identity.daemon_scope

        # A stray confirmation using gate_1's invocation id against gate_2
        # must not settle anything in gate_2.
        assert executor.submit(lambda: gate_2.confirm_settled(handle_1.identity.invocation_id)).result(5) is False
        assert gate_1.unsettled_snapshot()[0].invocation_id == handle_1.identity.invocation_id

        # Closing gate_1 must not affect gate_2's admission at all.
        gate_1.close_admission("gate 1 shutdown")
        assert executor.submit(lambda: gate_2.try_admit(repository="r", target="issue#9", stage="retry")).result(5) is not None
    finally:
        executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# AS-004 -- Remote handoff is local completion, not remote completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_handoff_settles_on_local_durable_confirmation_only():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="r", target="issue#10", stage="remote-dispatch")
    assert handle is not None

    # The local submission of the remote work completes immediately; the
    # remote task itself has not finished at all.
    handle.begin_checkpointing("remote_handoff")

    gate.close_admission("shutdown during remote dispatch")

    wait_task = asyncio.create_task(gate.wait_until_drained(poll_interval=0.001))
    await asyncio.sleep(0.02)
    assert not wait_task.done()  # remote task still "in flight" upstream, but
    # the model only cares about the durable local handoff record.

    assert handle.confirm_settled(confirmation_id="handoff-recorded") is True
    outcome = await asyncio.wait_for(wait_task, timeout=5)
    assert outcome is DrainOutcome.GRACEFUL

    # No fresh launch or continuation is permitted during/after draining.
    assert gate.try_admit(repository="r", target="issue#10", stage="remote-dispatch-retry") is None


# ---------------------------------------------------------------------------
# AS-005 -- A loop cannot reserve future inference during RUNNING
# ---------------------------------------------------------------------------


def test_retry_continuation_and_fallback_each_need_fresh_admission():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="r", target="issue#11", stage="impl")
    assert handle is not None
    handle.begin_checkpointing("result")
    assert handle.confirm_settled() is True

    gate.close_admission("shutdown before retry")

    for stage in ("retry", "continuation", "fallback-backend"):
        assert gate.try_admit(repository="r", target="issue#11", stage=stage) is None


def test_being_inside_a_prior_admitted_invocation_grants_no_extra_authority():
    """A still-running admitted invocation's internal tool/model loop is part
    of that one invocation; it must never look like a second admission."""
    gate = InvocationAdmissionGate()
    outer = gate.try_admit(repository="r", target="issue#12", stage="impl")
    assert outer is not None

    gate.close_admission("shutdown while outer invocation runs")

    # Nothing about "being inside" outer's still-open IN_FLIGHT invocation
    # grants a second try_admit call any authority.
    assert gate.try_admit(repository="r", target="issue#12", stage="impl") is None

    outer.begin_checkpointing("result")
    assert outer.confirm_settled() is True


# ---------------------------------------------------------------------------
# AS-006 -- Forced is not successfully drained
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_stop_abandons_wait_without_fabricating_success():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="r", target="issue#13", stage="impl")
    assert handle is not None
    handle.begin_checkpointing("result")
    handle.record_checkpoint_attempt_failed("still failing")

    gate.close_admission("shutdown")
    wait_task = asyncio.create_task(gate.wait_until_drained(poll_interval=0.001))
    await asyncio.sleep(0.02)
    assert not wait_task.done()

    gate.force_stop("second interrupt")
    outcome = await asyncio.wait_for(wait_task, timeout=5)
    assert outcome is DrainOutcome.FORCED

    # Force-stopping never fabricates a settled result nor a graceful drain.
    assert gate.is_graceful_ready is False
    assert gate.unsettled_snapshot()[0].state is InvocationState.CHECKPOINTING
    assert gate.mark_stopped() is False


def test_new_daemon_lifetime_gate_ignores_an_old_handles_completion():
    old_gate = InvocationAdmissionGate()
    old_handle = old_gate.try_admit(repository="r", target="issue#14", stage="impl")
    assert old_handle is not None
    old_handle.begin_checkpointing("result")
    old_gate.close_admission("old lifetime shutdown")
    old_gate.force_stop("forced")

    new_gate = InvocationAdmissionGate()
    # Delivering the old handle's completion into the replacement gate must
    # not authorize or settle anything in it.
    assert new_gate.confirm_settled(old_handle.identity.invocation_id) is False
    assert new_gate.state is GateState.RUNNING
    fresh_handle = new_gate.try_admit(repository="r", target="issue#14", stage="impl")
    assert fresh_handle is not None


# ---------------------------------------------------------------------------
# Cross-cutting: REQ-007 event-loop / thread / ambient-context boundary,
# REQ-008 snapshot contract.
# ---------------------------------------------------------------------------


def test_ambient_gate_context_defaults_to_none_and_is_thread_local_per_context():
    assert current_invocation_gate() is None
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        assert current_invocation_gate() is gate

        seen = {}

        def other_thread():
            # A brand-new thread never inherits this context's installed
            # gate implicitly.
            seen["gate"] = current_invocation_gate()

        t = threading.Thread(target=other_thread)
        t.start()
        t.join(5)
        assert seen["gate"] is None
    finally:
        reset_invocation_gate(token)
    assert current_invocation_gate() is None


@pytest.mark.asyncio
async def test_to_thread_preserves_the_calling_contexts_ambient_gate():
    gate = InvocationAdmissionGate()
    token = install_invocation_gate(gate)
    try:
        seen = await asyncio.to_thread(current_invocation_gate)
        assert seen is gate
    finally:
        reset_invocation_gate(token)


def test_snapshot_exposes_state_readiness_and_unsettled_targets_without_secrets():
    gate = InvocationAdmissionGate()
    handle = gate.try_admit(repository="acme/widgets", target="issue#15", stage="implementation")
    assert handle is not None
    gate.close_admission("shutdown")

    snapshot = gate.snapshot()
    assert snapshot.state is GateState.DRAINING
    assert snapshot.is_graceful_ready is False
    assert len(snapshot.unsettled) == 1
    entry = snapshot.unsettled[0]
    assert entry.repository == "acme/widgets"
    assert entry.target == "issue#15"
    assert entry.stage == "implementation"
    assert entry.state is InvocationState.IN_FLIGHT
    for field_name in ("invocation_id", "repository", "target", "stage", "state", "admitted_at", "updated_at", "checkpoint_failure_count"):
        assert hasattr(entry, field_name)
