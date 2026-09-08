"""Tests for the merge-operation resumption scheduler (Issue #1939).

These exercise the real ``MergeOperationScheduler`` run loop against a real,
on-disk ``MergeOperationStore``, asserting on wake timing and on how many
times the resume callback fires -- never on a mocked return value standing
in for the loop's own behavior.
"""

from __future__ import annotations

import asyncio

import pytest

from src.auto_coder.merge_operation_scheduler import MergeOperationScheduler
from src.auto_coder.merge_operation_state import EffectName, EffectState, MergeOperationIdentity, MergeOperationStore, OperationStatus


def make_store(tmp_path) -> MergeOperationStore:
    return MergeOperationStore(db_path=tmp_path / "merge_ops.db")


def make_identity(pr_number: int = 1) -> MergeOperationIdentity:
    return MergeOperationIdentity("https://api.github.com", "acme/widgets", pr_number)


@pytest.mark.asyncio
async def test_resumes_a_due_operation_promptly(tmp_path):
    """A waiting operation whose deadline has already passed is resumed
    quickly once the loop starts, without needing an explicit wake call."""
    store = make_store(tmp_path)
    identity = make_identity()
    store.get_or_create(identity, expected_head_sha="deadbeef", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    store.defer_local(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, is_real_throttle=False, retry_after_seconds=0.0)

    scheduler = MergeOperationScheduler(store, poll_interval=0.05)
    resumed = []

    def resume(operation):
        resumed.append(operation.identity.pr_number)

    scheduler.register_resume_handler(resume)

    shutdown = asyncio.Event()
    task = asyncio.ensure_future(scheduler.run(shutdown))
    try:
        for _ in range(50):
            if resumed:
                break
            await asyncio.sleep(0.05)
    finally:
        shutdown.set()
        scheduler.wake()
        await asyncio.wait_for(task, timeout=5)

    assert 1 in resumed


@pytest.mark.asyncio
async def test_never_resumes_before_the_deadline(tmp_path):
    """An operation deferred into the near future must not be resumed early."""
    store = make_store(tmp_path)
    identity = make_identity()
    store.get_or_create(identity, expected_head_sha="deadbeef", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    store.defer_local(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, is_real_throttle=False, retry_after_seconds=1.0)

    scheduler = MergeOperationScheduler(store, poll_interval=0.05)
    resumed = []
    scheduler.register_resume_handler(lambda operation: resumed.append(operation.identity.pr_number))

    shutdown = asyncio.Event()
    task = asyncio.ensure_future(scheduler.run(shutdown))
    try:
        await asyncio.sleep(0.3)
        assert resumed == []
    finally:
        shutdown.set()
        scheduler.wake()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_blocked_operation_is_never_dispatched(tmp_path):
    """An operationally-blocked operation is never handed to the resume callback."""
    from src.auto_coder.merge_operation_state import BlockReason

    store = make_store(tmp_path)
    identity = make_identity()
    store.get_or_create(identity, expected_head_sha="deadbeef", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    store.operationally_block(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, BlockReason.AUTHENTICATION)

    scheduler = MergeOperationScheduler(store, poll_interval=0.05)
    resumed = []
    scheduler.register_resume_handler(lambda operation: resumed.append(operation.identity.pr_number))

    shutdown = asyncio.Event()
    task = asyncio.ensure_future(scheduler.run(shutdown))
    try:
        await asyncio.sleep(0.3)
        assert resumed == []
        assert store.get(identity).status is OperationStatus.OPERATIONALLY_BLOCKED
    finally:
        shutdown.set()
        scheduler.wake()
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_interrupted_running_effect_is_reopened_as_delivery_unknown(tmp_path):
    """A crash-recovered RUNNING effect becomes DELIVERY_UNKNOWN and is
    handed to the resume callback again rather than silently dropped."""
    store = make_store(tmp_path)
    identity = make_identity()
    store.get_or_create(identity, expected_head_sha="deadbeef", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)
    store.reserve_attempt(identity, EffectName.MERGE)  # left RUNNING, simulating a crash mid-dispatch

    scheduler = MergeOperationScheduler(store, poll_interval=0.05)
    resumed = []
    scheduler.register_resume_handler(lambda operation: resumed.append(operation.identity.pr_number))

    shutdown = asyncio.Event()
    task = asyncio.ensure_future(scheduler.run(shutdown))
    try:
        for _ in range(50):
            if resumed:
                break
            await asyncio.sleep(0.05)
    finally:
        shutdown.set()
        scheduler.wake()
        await asyncio.wait_for(task, timeout=5)

    assert 1 in resumed
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


@pytest.mark.asyncio
async def test_confirmed_operation_is_never_resumed(tmp_path):
    """A fully confirmed operation is retired, never handed to the resume callback."""
    from src.auto_coder.merge_operation_state import ConfirmationSource, EffectReceipt

    store = make_store(tmp_path)
    identity = make_identity()
    store.get_or_create(identity, expected_head_sha="deadbeef", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)
    reservation = store.reserve_attempt(identity, EffectName.MERGE)
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="mergedsha", target_head_sha="deadbeef")
    store.record_confirmed_complete(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, receipt)

    scheduler = MergeOperationScheduler(store, poll_interval=0.05)
    resumed = []
    scheduler.register_resume_handler(lambda operation: resumed.append(operation.identity.pr_number))

    shutdown = asyncio.Event()
    task = asyncio.ensure_future(scheduler.run(shutdown))
    try:
        await asyncio.sleep(0.3)
        assert resumed == []
        assert store.get(identity).status is OperationStatus.MERGE_CONFIRMED
    finally:
        shutdown.set()
        scheduler.wake()
        await asyncio.wait_for(task, timeout=5)


def test_snapshot_reports_every_operation(tmp_path):
    store = make_store(tmp_path)
    identity = make_identity()
    store.get_or_create(identity, expected_head_sha="deadbeef", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False)

    scheduler = MergeOperationScheduler(store)
    snapshot = scheduler.snapshot()

    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["repository"] == "acme/widgets"
    assert entry["pr_number"] == 1
    assert entry["status"] == OperationStatus.WAITING.value
    assert entry["effects"][EffectName.MERGE.value] == EffectState.NOT_ATTEMPTED.value
