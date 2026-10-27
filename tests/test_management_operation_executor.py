"""Bounded asynchronous dispatch preserves durable work and prompt replays."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from auto_coder.management.operation_executor import ManagementOperationDispatcher, OperationHandler
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, EffectReceipt, ManagedTarget, OperationIntent, OperationPhase, OperationRecord


def _admit(journal: ManagementOperationStore, name: str) -> OperationRecord:
    intent = OperationIntent(ManagedTarget(name, "management", "owner/repo"), "fixed-action", (), name)
    authority = AcceptedAuthority("issuer", "resource", "project", "operator", "client", "original-grant", ("read", "deploy"), 1, 1)
    record = journal.admit(intent, authority).record
    assert record is not None
    return record


def _refuse(journal: ManagementOperationStore, record: OperationRecord) -> OperationRecord | None:
    return journal.reject_before_effect(record.operation_id, record.revision, category="NOT_READY:fixture", evidence=EffectReceipt(record.operation_id, "component-preflight", 2, "not_started")).record


@pytest.mark.asyncio
async def test_slow_forward_replays_share_one_existing_dispatch_attempt(tmp_path: Path) -> None:
    journal = ManagementOperationStore(tmp_path)
    dispatcher = ManagementOperationDispatcher(journal, capacity=1)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def forward(record: OperationRecord) -> OperationRecord | None:
        calls.append(record.operation_id)
        entered.set()
        assert release.wait(5)
        return _refuse(journal, record)

    dispatcher.register("fixed-action", OperationHandler(forward, forward))
    await dispatcher.start()
    record = _admit(journal, "same-operation")
    try:
        for _ in range(200):
            assert dispatcher.enqueue(record.operation_id)
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(200):
            assert dispatcher.enqueue(record.operation_id)
        assert journal.get(record.operation_id).record == record
        assert calls == [record.operation_id]
    finally:
        release.set()
        await dispatcher.drain()
        await dispatcher.stop()
    assert calls == [record.operation_id]
    completed = journal.get(record.operation_id).record
    assert completed is not None and completed.phase is OperationPhase.FAILED
    assert completed.authority == record.authority


@pytest.mark.asyncio
async def test_full_queue_keeps_unqueued_intent_for_same_dispatcher_reconstruction(tmp_path: Path) -> None:
    journal = ManagementOperationStore(tmp_path)
    dispatcher = ManagementOperationDispatcher(journal, capacity=1)
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def forward(record: OperationRecord) -> OperationRecord | None:
        calls.append(record.operation_id)
        if record.intent.target.instance_id == "first":
            entered.set()
            assert release.wait(5)
        return _refuse(journal, record)

    handler = OperationHandler(forward, forward)
    dispatcher.register("fixed-action", handler)
    await dispatcher.start()
    first, second, retained = (_admit(journal, name) for name in ("first", "second", "retained"))
    try:
        assert dispatcher.enqueue(first.operation_id)
        assert await asyncio.to_thread(entered.wait, 5)
        assert dispatcher.enqueue(second.operation_id)
        assert not dispatcher.enqueue(retained.operation_id)
        assert dispatcher.enqueue(second.operation_id)
        assert journal.get(retained.operation_id).record == retained
    finally:
        release.set()
        await dispatcher.drain()
        await dispatcher.stop()
    assert calls == [first.operation_id, second.operation_id]
    reconstructed = ManagementOperationDispatcher(ManagementOperationStore(tmp_path), capacity=1)
    reconstructed.register("fixed-action", handler)
    await reconstructed.start()
    await reconstructed.drain()
    await reconstructed.stop()
    assert calls == [first.operation_id, second.operation_id, retained.operation_id]
    completed = journal.get(retained.operation_id).record
    assert completed is not None and completed.phase is OperationPhase.FAILED
    assert completed.authority == retained.authority
