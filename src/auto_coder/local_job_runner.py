"""Capacity-bounded execution lane for durable local model jobs."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from loguru import logger

from .invocation_admission import InvocationAdmissionGate, InvocationHandle, install_invocation_gate, reset_invocation_gate
from .local_job_handoff import InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobRecord, LocalJobState, LocalJobStore


@dataclass(frozen=True)
class LocalJobExecutionResult:
    """The actual outcome returned by one registered domain adapter."""

    outcome: InvocationOutcome
    output: str
    diagnostic: str = ""


class LocalJobDomainAdapter(Protocol):
    """Domain-owned authority and invocation boundary used by the runner."""

    def authorize_provider_entry(self, job: LocalJobRecord) -> bool:
        """Return current authoritative permission for this exact incarnation."""

    def invoke(self, job: LocalJobRecord) -> LocalJobExecutionResult:
        """Invoke the captured backend in the captured repository context."""


CompletionWake = Callable[[LocalJobRecord], None]


class LocalJobRunner:
    """Run accepted jobs without borrowing Issue or PR worker capacity.

    ``poll`` only schedules work and never awaits inference. The private executor
    is the documented capacity boundary; pending envelopes remain durable when
    every slot is occupied.
    """

    def __init__(
        self,
        store: LocalJobStore,
        admission_gate: InvocationAdmissionGate,
        *,
        capacity: int,
        adapters: dict[LocalJobKind, LocalJobDomainAdapter],
        completion_wake: Optional[CompletionWake] = None,
        runner_id: str = "",
    ) -> None:
        if capacity < 1:
            raise ValueError("local runner capacity must be at least one")
        self.store = store
        self.admission_gate = admission_gate
        self.capacity = capacity
        self.adapters = dict(adapters)
        self.completion_wake = completion_wake
        self.runner_id = runner_id or uuid.uuid4().hex
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="local-job-runner")
        self._lock = threading.Lock()
        self._active: dict[str, Future[None]] = {}
        self._closed = False

    def poll(self) -> int:
        """Schedule as many pending envelopes as free runner slots permit."""
        self.wake_downstream()
        with self._lock:
            self._reap_locked()
            if self._closed:
                return 0
            free = self.capacity - len(self._active)
            if free <= 0:
                return 0
            pending = [job for job in self.store.discover_unsettled() if job.state is LocalJobState.PENDING]
            scheduled = 0
            for job in pending[:free]:
                # Admission happens before claiming. A draining daemon leaves the
                # definitely-not-started envelope pending for a later lifetime.
                handle = self.admission_gate.try_admit(
                    repository=job.repository,
                    target=f"{job.kind.value}#{job.target_number}",
                    stage="durable_local_job",
                )
                if handle is None:
                    break
                claim = self.store.claim(job.job_id, self.runner_id)
                if claim is None or not claim.acquired:
                    handle.begin_checkpointing("claim_lost")
                    handle.confirm_settled(f"claim-lost:{job.job_id}")
                    continue
                future = self._executor.submit(self._execute, claim, handle)
                self._active[job.job_id] = future
                scheduled += 1
            return scheduled

    def wake_downstream(self) -> int:
        """Replay durable completion eligibility without claiming or invoking."""
        with self._lock:
            if self._closed:
                return 0
        return self._wake_downstream()

    def active_count(self) -> int:
        with self._lock:
            self._reap_locked()
            return len(self._active)

    def close(self, *, wait: bool = False) -> None:
        """Stop scheduling; waiting is optional and never delegated to a worker."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _reap_locked(self) -> None:
        self._active = {job_id: future for job_id, future in self._active.items() if not future.done()}

    def _wake_downstream(self) -> int:
        if self.completion_wake is None:
            return 0
        notified = 0
        for job in self.store.discover_unsettled():
            if job.state is not LocalJobState.DOWNSTREAM_EFFECTS_PENDING:
                continue
            try:
                self.completion_wake(job)
                notified += 1
            except Exception as exc:
                # Eligibility remains durable and can be replayed later.
                logger.warning("Local job completion wake failed for {}: {}", job.job_id, exc)
        return notified

    def _execute(self, claim: LocalJobClaim, handle: InvocationHandle) -> None:
        # Keep the same daemon admission gate visible at provider boundaries in
        # this independently owned thread.
        gate_token = install_invocation_gate(self.admission_gate)
        try:
            adapter = self.adapters.get(claim.record.kind)
            if adapter is None:
                self.store.release_unentered_claim(claim, "no registered domain adapter")
                self._checkpoint_without_provider(handle, claim, "adapter_unavailable")
                return
            try:
                authorized = adapter.authorize_provider_entry(claim.record)
            except Exception as exc:
                self.store.release_unentered_claim(claim, f"authorization unavailable: {exc}")
                self._checkpoint_without_provider(handle, claim, "authorization_unavailable")
                return
            if not authorized:
                self.store.release_unentered_claim(claim, "authoritative provider-entry permission denied")
                self._checkpoint_without_provider(handle, claim, "authorization_denied")
                return
            if not self.store.mark_provider_entered(claim):
                self.store.record_runner_diagnostic(claim, "provider-entry checkpoint failed")
                self._checkpoint_without_provider(handle, claim, "entry_checkpoint_failed", settle=False)
                return

            try:
                result = adapter.invoke(claim.record)
            except Exception as exc:
                result = LocalJobExecutionResult(InvocationOutcome.FAILED, f"{type(exc).__name__}: {exc}", "provider raised")
            except BaseException as exc:
                result = LocalJobExecutionResult(InvocationOutcome.INTERRUPTED, f"{type(exc).__name__}: {exc}", "provider interrupted")

            handle.begin_checkpointing(result.outcome.value)
            artifact = self.store.persist_result_artifact(claim, result.outcome, result.output)
            if artifact is None or not self.store.record_result(claim, result.outcome, artifact.artifact_id, result.diagnostic):
                handle.record_checkpoint_attempt_failed("local job result checkpoint failed")
                return
            if not self.store.mark_downstream_pending(claim):
                handle.record_checkpoint_attempt_failed("downstream eligibility checkpoint failed")
                return
            handle.confirm_settled(artifact.artifact_id)
            current = self.store.get(claim.record.job_id)
            if current is not None and self.completion_wake is not None:
                try:
                    self.completion_wake(current)
                except Exception as exc:
                    # Eligibility is durable; a replayed wake cannot reinvoke.
                    logger.warning("Local job completion wake failed for {}: {}", current.job_id, exc)
        finally:
            reset_invocation_gate(gate_token)

    @staticmethod
    def _checkpoint_without_provider(handle: InvocationHandle, claim: LocalJobClaim, outcome: str, *, settle: bool = True) -> None:
        handle.begin_checkpointing(outcome)
        if settle:
            handle.confirm_settled(f"runner-refusal:{claim.record.execution_incarnation}")
        else:
            handle.record_checkpoint_attempt_failed(outcome)
