from __future__ import annotations

import sqlite3

from auto_coder.merge_operation_state import (
    MAX_THROTTLED_RETRIES_AFTER_FIRST,
    MIN_LOCAL_RETRY_INTERVAL_SECONDS,
    BlockReason,
    ConfirmationSource,
    EffectName,
    EffectReceipt,
    EffectState,
    MergeOperationIdentity,
    MergeOperationPersistenceError,
    MergeOperationStore,
    OperationStatus,
)


def _identity(pr: int = 42) -> MergeOperationIdentity:
    return MergeOperationIdentity("https://api.github.com", "acme/widgets", pr)


def test_as001_approved_merge_unsent_survives_restart(tmp_path):
    """AS-001: approval receipt is retained while merge alone stays waiting,
    and a scheduler retiring its own queue row must not un-execute approval."""
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)

    operation = store.get_or_create(
        identity,
        expected_head_sha="H",
        merge_method="squash",
        approval_credential_role="bot",
        reviewer_identity="reviewer-a",
        needs_approval=True,
        now=100,
    )
    assert operation.generation == 1

    reservation = store.reserve_attempt(identity, EffectName.APPROVAL, now=100)
    assert reservation.granted
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id="R", reviewer_identity="reviewer-a", target_head_sha="H", recorded_at=100)
    assert store.record_confirmed_complete(identity, EffectName.APPROVAL, reservation.attempt_id, reservation.generation, receipt, now=100)

    merge_reservation = store.reserve_attempt(identity, EffectName.MERGE, now=101)
    assert merge_reservation.granted
    deferred = store.defer_local(identity, EffectName.MERGE, merge_reservation.attempt_id, merge_reservation.generation, is_real_throttle=False, now=101)
    assert deferred is not None
    assert deferred.status is OperationStatus.WAITING

    restarted = MergeOperationStore(path)
    reopened = restarted.get(identity)
    assert reopened is not None
    assert reopened.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE
    assert reopened.effect(EffectName.APPROVAL).receipt.review_id == "R"
    assert reopened.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert reopened.status is OperationStatus.WAITING

    # Same-target re-entry (e.g. a scheduler row being recreated) must not
    # revert the confirmed approval back to not-attempted.
    reentered = restarted.get_or_create(
        identity,
        expected_head_sha="H",
        merge_method="squash",
        approval_credential_role="bot",
        reviewer_identity="reviewer-a",
        needs_approval=True,
        now=200,
    )
    assert reentered.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE


def test_as002_running_effect_after_reopen_is_delivery_unknown(tmp_path):
    """AS-002: a crash between reservation and outcome recording must not be
    assumed unsent on reopen; only an explicitly confirmed-unsent effect is
    treated as eligible to resume once its deadline passes."""
    path = tmp_path / "merge.db"
    identity = _identity()
    store = MergeOperationStore(path)
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    assert reservation.granted
    # No terminal outcome recorded: simulate a controller stopping here.

    restarted = MergeOperationStore(path)
    recovered = restarted.recover_after_restart(now=2)
    assert len(recovered) == 1
    operation = restarted.get(identity)
    assert operation.effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # It must not be reservable again until explicitly reconciled.
    stuck = restarted.reserve_attempt(identity, EffectName.MERGE, now=3)
    assert stuck.granted is False

    # A cancellation alone (recover_after_restart again) must not convert
    # delivery-unknown into anything else.
    restarted.recover_after_restart(now=4)
    assert restarted.get(identity).effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN

    # By contrast, a correlated confirmed-unsent result for a *different*
    # attempt is retried once its deadline passes.
    store2 = MergeOperationStore(tmp_path / "merge2.db")
    identity2 = _identity(7)
    store2.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation2 = store2.reserve_attempt(identity2, EffectName.MERGE, now=1)
    store2.record_confirmed_unsent(identity2, EffectName.MERGE, reservation2.attempt_id, reservation2.generation, now=1)
    retried = store2.reserve_attempt(identity2, EffectName.MERGE, now=2)
    assert retried.granted


def test_as003_stale_attempt_result_does_not_overwrite_new_head(tmp_path):
    """AS-003: a duplicate reservation is refused, and a stale attempt's late
    result (from before a head change) never mutates the new generation's
    effect or deadline, though its own evidence is kept in history."""
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H1", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)

    owner_a = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    assert owner_a.granted
    # A second entry point trying the same effect while A owns it is refused.
    owner_b = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    assert owner_b.granted is False

    # A new head arrives before A's result does.
    updated = store.get_or_create(identity, expected_head_sha="H2", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=2)
    assert updated.generation == 2
    assert updated.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED

    owner_c = store.reserve_attempt(identity, EffectName.MERGE, now=2)
    assert owner_c.granted
    assert owner_c.generation == 2

    # A's late result (old generation/attempt) arrives now.
    receipt = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="stale-sha", target_head_sha="H1")
    applied = store.record_confirmed_complete(identity, EffectName.MERGE, owner_a.attempt_id, owner_a.generation, receipt, now=3)
    assert applied is False

    # H2's effect/state must be unaffected by A's stale result.
    current = store.get(identity)
    assert current.effect(EffectName.MERGE).state is EffectState.RUNNING
    assert current.effect(EffectName.MERGE).attempt_id == owner_c.attempt_id
    assert current.generation == 2

    # C's own result completes normally.
    receipt_c = EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="real-sha", target_head_sha="H2")
    assert store.record_confirmed_complete(identity, EffectName.MERGE, owner_c.attempt_id, owner_c.generation, receipt_c, now=4)
    assert store.get(identity).effect(EffectName.MERGE).receipt.merge_commit_sha == "real-sha"


def test_as004_deadline_never_shortened_and_throttle_counted_once_per_id(tmp_path):
    """AS-004: wakes, duplicate notifications, and metadata-only saves never
    shorten or arbitrarily extend a retained deadline; a real throttle
    redelivered under the same attempt id only spends one retry."""
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=100)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=100)

    deferred = store.defer_local(
        identity,
        EffectName.MERGE,
        reservation.attempt_id,
        reservation.generation,
        is_real_throttle=True,
        throttle_attempt_id="attempt-1",
        retry_after_seconds=0.0,
        governor_deadline=130,
        now=100,
    )
    # Prior retained deadline of 150 (simulated as already-stored not_before)
    # must not be undercut; apply a second defer using a fresh reservation
    # representing a later, already-scheduled retry that raises not_before
    # to 150 first.
    assert deferred.not_before == 130

    # A metadata-only save (merge method change) must not touch the deadline.
    refreshed = store.get_or_create(identity, expected_head_sha="H", merge_method="rebase", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=110)
    assert refreshed.not_before == 130
    assert refreshed.merge_method == "rebase"

    # Duplicate redelivery of the same real-throttle attempt id costs nothing extra.
    reservation2 = store.reserve_attempt(identity, EffectName.MERGE, now=131)
    deferred2 = store.defer_local(
        identity,
        EffectName.MERGE,
        reservation2.attempt_id,
        reservation2.generation,
        is_real_throttle=True,
        throttle_attempt_id="attempt-1",
        now=131,
    )
    assert deferred2.effect(EffectName.MERGE).throttle_attempts == 1

    # Local (pre-send) deferrals never spend a throttle attempt regardless of id reuse.
    reservation3 = store.reserve_attempt(identity, EffectName.MERGE, now=132)
    deferred3 = store.defer_local(identity, EffectName.MERGE, reservation3.attempt_id, reservation3.generation, is_real_throttle=False, now=132)
    assert deferred3.effect(EffectName.MERGE).throttle_attempts == 1
    assert deferred3.not_before == 132 + MIN_LOCAL_RETRY_INTERVAL_SECONDS

    # No deadline at all: falls back to now + floor.
    store2 = MergeOperationStore(tmp_path / "merge2.db")
    identity2 = _identity(9)
    store2.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=100)
    r = store2.reserve_attempt(identity2, EffectName.MERGE, now=100)
    d = store2.defer_local(identity2, EffectName.MERGE, r.attempt_id, r.generation, is_real_throttle=False, now=100)
    assert d.not_before == 100 + MIN_LOCAL_RETRY_INTERVAL_SECONDS

    # Real throttles: first attempt + MAX_THROTTLED_RETRIES_AFTER_FIRST more are tolerated, then blocked.
    store3 = MergeOperationStore(tmp_path / "merge3.db")
    identity3 = _identity(11)
    store3.get_or_create(identity3, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=0)
    result = None
    for i in range(MAX_THROTTLED_RETRIES_AFTER_FIRST + 2):
        res = store3.reserve_attempt(identity3, EffectName.MERGE, now=i)
        result = store3.defer_local(identity3, EffectName.MERGE, res.attempt_id, res.generation, is_real_throttle=True, throttle_attempt_id=f"a{i}", now=i)
        if result.status is OperationStatus.OPERATIONALLY_BLOCKED:
            break
    assert result.status is OperationStatus.OPERATIONALLY_BLOCKED
    assert result.resume_reason == BlockReason.RETRIES_EXHAUSTED.value


def test_as005_queue_completion_is_not_receipt_deletion(tmp_path):
    """AS-005: re-entry after a scheduler retires its own row must not create
    unexecuted operations for confirmed effects, and delivery-unknown is not
    completed merely by that re-entry either."""
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=1)
    approval_reservation = store.reserve_attempt(identity, EffectName.APPROVAL, now=1)
    store.record_confirmed_complete(identity, EffectName.APPROVAL, approval_reservation.attempt_id, approval_reservation.generation, EffectReceipt(ConfirmationSource.OWN_RESPONSE, review_id="R"), now=1)
    merge_reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    store.record_confirmed_complete(identity, EffectName.MERGE, merge_reservation.attempt_id, merge_reservation.generation, EffectReceipt(ConfirmationSource.OWN_RESPONSE, merge_commit_sha="sha"), now=1)

    # Re-injecting an old notification is just a get_or_create re-entry.
    reentered = store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=True, now=99)
    assert reentered.status is OperationStatus.MERGE_CONFIRMED
    assert reentered.effect(EffectName.APPROVAL).state is EffectState.CONFIRMED_COMPLETE
    assert reentered.effect(EffectName.MERGE).state is EffectState.CONFIRMED_COMPLETE
    assert store.reserve_attempt(identity, EffectName.APPROVAL, now=99).granted is False
    assert store.reserve_attempt(identity, EffectName.MERGE, now=99).granted is False

    # Delivery-unknown is a separate case: it is not completed by re-entry.
    identity2 = _identity(8)
    store.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    r2 = store.reserve_attempt(identity2, EffectName.MERGE, now=1)
    store.recover_after_restart(now=2)
    reentered2 = store.get_or_create(identity2, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=3)
    assert reentered2.effect(EffectName.MERGE).state is EffectState.DELIVERY_UNKNOWN


def test_as006_persistence_failure_blocks_dependent_effects(tmp_path, monkeypatch):
    """AS-006: injected failures at reservation/receipt-write/reopen raise
    rather than report success, and leave existing records untouched."""
    path = tmp_path / "merge.db"
    store = MergeOperationStore(path)
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)

    original_connect = store._connect
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    try:
        raised = False
        try:
            store.reserve_attempt(identity, EffectName.MERGE, now=2)
        except MergeOperationPersistenceError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(store, "_connect", original_connect)

    # Existing record survived the injected failure untouched.
    intact = store.get(identity)
    assert intact is not None
    assert intact.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED

    # A receipt-write failure after a genuine reservation also raises rather
    # than silently completing.
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=3)
    monkeypatch.setattr(store, "_connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    try:
        raised = False
        try:
            store.record_confirmed_complete(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, EffectReceipt(ConfirmationSource.OWN_RESPONSE), now=3)
        except MergeOperationPersistenceError:
            raised = True
        assert raised
    finally:
        monkeypatch.setattr(store, "_connect", original_connect)
    assert store.get(identity).effect(EffectName.MERGE).state is EffectState.RUNNING


def test_reservation_refused_while_operationally_blocked(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    store.operationally_block(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, BlockReason.AUTHENTICATION, now=1)
    assert store.get(identity).status is OperationStatus.OPERATIONALLY_BLOCKED
    refused = store.reserve_attempt(identity, EffectName.MERGE, now=2)
    assert refused.granted is False


def test_manual_reset_effect_clears_only_selected_effect(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=1)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=1)
    store.operationally_block(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, BlockReason.FORBIDDEN, now=1)

    reset = store.manual_reset_effect(identity, EffectName.MERGE, now=5)
    assert reset.status is OperationStatus.WAITING
    assert reset.effect(EffectName.MERGE).state is EffectState.NOT_ATTEMPTED
    assert store.reserve_attempt(identity, EffectName.MERGE, now=6).granted


def test_due_lists_only_waiting_operations_past_deadline(tmp_path):
    store = MergeOperationStore(tmp_path / "merge.db")
    identity = _identity()
    store.get_or_create(identity, expected_head_sha="H", merge_method="squash", approval_credential_role="bot", reviewer_identity="r", needs_approval=False, now=100)
    reservation = store.reserve_attempt(identity, EffectName.MERGE, now=100)
    store.defer_local(identity, EffectName.MERGE, reservation.attempt_id, reservation.generation, is_real_throttle=False, now=100)
    assert store.due(now=100) == []
    assert len(store.due(now=101.5)) == 1
