"""Repository-scoped composition for Issue runtime startup and recovery."""

from __future__ import annotations

import asyncio
from typing import Callable, Mapping

from .codex_retry_reconciliation import CodexRetryReconciler
from .github_pending_work import PendingWorkScheduler, PendingWorkStore, StageHandler
from .issue_job_runtime import IssueJobRuntime
from .local_job_runner import LocalJobRunner
from .retry_dispatch import RetryDispatchRepository


class IssueRuntimeComposition:
    """Assemble and start the existing Issue recovery authorities once.

    The composition owns no durable state and performs no domain transition.
    It only binds the repository's existing pending-work scheduler, local-job
    runtime, and accepted-retry reconciler into one lifecycle.
    """

    def __init__(
        self,
        repository: str,
        pending_store: PendingWorkStore,
        issue_jobs: IssueJobRuntime,
        retry_reconciler: CodexRetryReconciler,
    ) -> None:
        self.repository = repository
        self.issue_jobs = issue_jobs
        self.retry_reconciler = retry_reconciler
        self.pending_work = PendingWorkScheduler(pending_store, repository=repository)
        self._registered = False
        self._recovery_started = False
        self._local_started = False

    def register(self, handlers: Mapping[str, StageHandler]) -> PendingWorkScheduler:
        """Register the repository's existing stage owners exactly once."""
        if self._registered:
            raise RuntimeError("Issue runtime handlers are already registered")
        for stage, handler in handlers.items():
            self.pending_work.register_handler(stage, handler)
        self._registered = True
        return self.pending_work

    def start_recovery(
        self,
        shutdown_event: asyncio.Event,
        *,
        schedule_retry: Callable[[str, int], None],
    ) -> asyncio.Task[None]:
        """Rediscover retained receipts and start pending-work service."""
        if not self._registered:
            raise RuntimeError("Issue runtime handlers must be registered before startup")
        if self._recovery_started:
            raise RuntimeError("Issue runtime recovery is already active")
        self._recovery_started = True

        # This is enumeration of already-accepted receipts only.  Reconciliation
        # runs later through the registered handler and cannot submit provider
        # work or allocate a replacement identity.
        for handoff in RetryDispatchRepository(self.repository).list_unfinished_accepted():
            if handoff.repository != self.repository:
                continue
            schedule_retry(handoff.request_id, handoff.issue_number)

        return asyncio.create_task(
            self.pending_work.run(shutdown_event),
            name="pending-work-scheduler",
        )

    def start_local_jobs(
        self,
        shutdown_event: asyncio.Event,
        *,
        capacity: int,
        completion_wake: Callable[[object], None],
    ) -> tuple[LocalJobRunner, asyncio.Task[None]]:
        """Start accepted local jobs after the startup admission barrier."""
        if not self._recovery_started:
            raise RuntimeError("Issue runtime recovery must start before local jobs")
        if self._local_started:
            raise RuntimeError("Issue local-job execution is already active")
        self._local_started = True
        runner = self.issue_jobs.new_runner(
            capacity=max(1, capacity),
            completion_wake=completion_wake,
        )
        return (
            runner,
            asyncio.create_task(
                self._run_local_jobs(runner, shutdown_event),
                name="durable-issue-local-job-runner",
            ),
        )

    @staticmethod
    async def _run_local_jobs(runner: LocalJobRunner, shutdown_event: asyncio.Event) -> None:
        while not shutdown_event.is_set():
            await asyncio.to_thread(runner.poll)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
