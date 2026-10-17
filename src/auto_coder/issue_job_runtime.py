"""Repository-bound assembly for durable local Issue jobs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Optional

from .implementation_slots import ImplementationSlotRepository
from .invocation_admission import InvocationAdmissionGate
from .issue_job_finalizer import IssueJobFinalizer, IssuePublicationResult
from .issue_local_job import IssueJobAuthorizationPorts, IssueLocalJobAdapter
from .local_job_handoff import LocalJobKind, LocalJobState, LocalJobStore
from .local_job_runner import LocalJobRunner, runner_owner_alive


class IssueJobRuntime:
    """Coordinate execution, recovery, and publication for one repository."""

    def __init__(
        self,
        repository: str,
        store: LocalJobStore,
        slots: ImplementationSlotRepository,
        invocation_gate: InvocationAdmissionGate,
        ports: IssueJobAuthorizationPorts,
        repository_path: Path,
        lookup_pr: Callable[[str, str], Optional[Mapping[str, object]]],
        create_pr: Callable[[object, str], object],
        publication_observer: Callable[[IssuePublicationResult, str, bool], None],
    ) -> None:
        self.repository = repository
        self.store = store
        self.slots = slots
        self._invocation_gate = invocation_gate
        self._adapter = IssueLocalJobAdapter(ports, repository, store, slots, repository_path)
        self._finalizer = IssueJobFinalizer(store, slots, lookup_pr, create_pr)
        self._publication_observer = publication_observer

    def new_runner(
        self,
        *,
        capacity: int,
        job_ids: Optional[frozenset[str]] = None,
        completion_wake: Optional[Callable[[object], None]] = None,
    ) -> LocalJobRunner:
        """Build a runner over the runtime's single durable authority."""
        return LocalJobRunner(
            self.store,
            self._invocation_gate,
            repository=self.repository,
            capacity=capacity,
            adapters={LocalJobKind.ISSUE_IMPLEMENTATION: self._adapter},
            job_ids=job_ids,
            completion_wake=completion_wake,
        )

    def recover_start_notifications(self) -> None:
        self._adapter.recover_start_notifications()

    def finalize(
        self,
        *,
        job_id: Optional[str] = None,
        origin: str,
        restored_job_ids: frozenset[str] = frozenset(),
    ) -> tuple[IssuePublicationResult, ...]:
        """Resume only requested publication effects, or all repository effects."""
        results = (self._finalizer.finalize(job_id),) if job_id is not None else self._finalizer.resume_all(self.repository)
        for result in results:
            self._publication_observer(result, origin, result.job_id in restored_job_ids)
        return results

    def wait_for_target(self, job_id: str, *, resume: bool = False) -> None:
        """Drive and wait for exactly one accepted job without draining peers."""
        job = self.store.get(job_id)
        if job is None:
            raise RuntimeError("Accepted local implementation job disappeared")
        runner = self.new_runner(capacity=1, job_ids=frozenset({job_id}))
        try:
            live_external_owner = job.runner_owner != runner.runner_id and job.state in {LocalJobState.RUNNING, LocalJobState.RESULT_RECORDED} and runner_owner_alive(job.runner_owner) is True
            if resume and job.state is LocalJobState.RUNNING and not job.result_reference:
                runner.resume(job_id)
            elif not live_external_owner:
                runner.poll()
            while True:
                if not runner.active_count():
                    current = self.store.get(job_id)
                    if current is None or current.state not in {LocalJobState.RUNNING, LocalJobState.RESULT_RECORDED} or current.runner_owner == runner.runner_id or runner_owner_alive(current.runner_owner) is not True:
                        break
                time.sleep(0.1)
            runner.recover_unsettled()
        finally:
            runner.close(wait=True)
