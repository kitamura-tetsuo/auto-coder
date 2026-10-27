"""Single, bounded, durable management-operation dispatch loop.

Handlers are fixed at application construction. The journal, not the queue or
the HTTP request, owns idempotency, per-target exclusion and effect fencing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from .operation_store import ManagementOperationStore
from .operations import EffectState, OperationPhase, OperationRecord, QueryStatus, ReadStatus


@dataclass(frozen=True)
class OperationHandler:
    forward: Callable[[OperationRecord], OperationRecord | None]
    reconcile: Callable[[OperationRecord], OperationRecord | None]


class ManagementOperationDispatcher:
    """One service-owned worker; no provider/issue scheduler or retry timer."""

    def __init__(self, journal: ManagementOperationStore, *, capacity: int = 128) -> None:
        self.journal = journal
        self._handlers: dict[str, OperationHandler] = {}
        self._queue: asyncio.Queue[tuple[str, asyncio.Future[OperationRecord | None] | None] | None] = asyncio.Queue(maxsize=capacity)
        self._task: asyncio.Task[None] | None = None
        self._scheduled: set[str] = set()
        self._active_operation_id: str | None = None

    def register(self, action: str, handler: OperationHandler) -> None:
        if not action or action in self._handlers or self._task is not None:
            raise ValueError("management operation handler must be registered once before startup")
        self._handlers[action] = handler

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("management operation dispatcher is already running")
        self._task = asyncio.create_task(self._run(), name="management-operation-dispatcher")
        cursor = 0
        while True:
            page = await asyncio.to_thread(self.journal.list_operations, cursor=cursor, limit=100, outstanding_only=True)
            if page.status is not QueryStatus.OK:
                await self.stop()
                raise RuntimeError("management operation journal is unavailable")
            for record in page.records:
                self._scheduled.add(record.operation_id)
                await self._queue.put((record.operation_id, None))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def drain(self) -> None:
        """Complete the startup journal reconciliation before exposing readiness."""
        if self._task is None:
            raise RuntimeError("management operation dispatcher is unavailable")
        await self._queue.join()

    async def submit(self, operation_id: str) -> OperationRecord | None:
        if self._task is None:
            raise RuntimeError("management operation dispatcher is unavailable")
        response: asyncio.Future[OperationRecord | None] = asyncio.get_running_loop().create_future()
        await self._queue.put((operation_id, response))
        return await response

    def enqueue(self, operation_id: str) -> bool:
        """Queue a durable operation without holding its initiating HTTP request.

        This uses the same sole worker as ``submit``. Repeated readbacks share a
        queued/in-flight attempt; the journal remains authoritative if the
        bounded queue is full or the process exits before it consumes this ID.
        """
        if self._task is None:
            raise RuntimeError("management operation dispatcher is unavailable")
        if operation_id in self._scheduled or operation_id == self._active_operation_id:
            return True
        try:
            self._queue.put_nowait((operation_id, None))
        except asyncio.QueueFull:
            return False
        self._scheduled.add(operation_id)
        return True

    async def _run(self) -> None:
        while True:
            queued = await self._queue.get()
            if queued is None:
                self._queue.task_done()
                return
            operation_id, response = queued
            self._active_operation_id = operation_id
            try:
                result = await asyncio.to_thread(self._process, operation_id)
            except Exception as exc:
                # The durable lane is not released when an unexpected failure
                # prevents observation or journal mutation.
                logger.error("Management dispatch unavailable operation={} failure={}", operation_id, type(exc).__name__)
                result = None
            if response is not None and not response.done():
                response.set_result(result)
            if response is None:
                self._scheduled.discard(operation_id)
            self._active_operation_id = None
            self._queue.task_done()

    def _process(self, operation_id: str) -> OperationRecord | None:
        lookup = self.journal.get(operation_id)
        if lookup.status is not ReadStatus.FOUND or lookup.record is None:
            return None
        record = lookup.record
        if record.phase in {OperationPhase.SUCCEEDED, OperationPhase.FAILED, OperationPhase.ROLLED_BACK}:
            return record
        handler = self._handlers.get(record.intent.action)
        if handler is None:
            if record.phase is OperationPhase.ACCEPTED and record.effect.state is EffectState.NOT_SENT:
                return self.journal.transition(operation_id, record.revision, OperationPhase.NEEDS_ATTENTION, safe_error_category="unknown_handler").record
            return record
        if record.effect.state is EffectState.NOT_SENT:
            if record.phase is OperationPhase.ACCEPTED:
                return handler.forward(record)
            return record
        return handler.reconcile(record)
