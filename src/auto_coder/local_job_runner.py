"""Capacity-bounded execution lane for durable local model jobs."""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
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


def _process_start_token(pid: int) -> Optional[str]:
    """Return Linux's immutable process start tick, or no liveness authority."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # The parenthesized process name may contain spaces. Fields following
        # its closing parenthesis start at field 3; starttime is field 22.
        fields_after_name = stat[stat.rindex(")") + 2 :].split()
        return fields_after_name[19]
    except (OSError, IndexError, ValueError):
        return None


def current_runner_owner() -> str:
    """Create restart-reconcilable evidence for this process lifetime."""
    pid = os.getpid()
    start = _process_start_token(pid)
    return f"process:{pid}:{start}" if start else f"ambiguous:{pid}"


def runner_owner_alive(owner: str) -> Optional[bool]:
    """Return authoritative liveness; None means replay must stay suppressed."""
    parts = owner.split(":")
    if len(parts) != 3 or parts[0] != "process" or not parts[1].isdigit():
        return None
    pid = int(parts[1])
    observed = _process_start_token(pid)
    if observed is None:
        return False if not os.path.exists(f"/proc/{pid}") else None
    return observed == parts[2]


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
        self.runner_id = runner_id or current_runner_owner()
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="local-job-runner")
        self._notification_executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="local-job-notification")
        self._lock = threading.Lock()
        self._active: dict[str, Future[None]] = {}
        self._checkpoint_handles: dict[str, InvocationHandle] = {}
        self._notifying: set[str] = set()
        self._closed = False

    def poll(self) -> int:
        """Schedule as many pending envelopes as free runner slots permit."""
        self._recover_unsettled()
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
                self._checkpoint_handles[claim.record.execution_incarnation] = handle
                future = self._executor.submit(self._execute, claim, handle)
                self._active[job.job_id] = future
                scheduled += 1
            return scheduled

    def wake_downstream(self) -> int:
        """Schedule durable notifications without running callbacks on the caller."""
        with self._lock:
            if self._closed:
                return 0
            eligible = [job for job in self.store.discover_unsettled() if job.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING and job.job_id not in self._notifying]
            for job in eligible:
                self._notifying.add(job.job_id)
                self._notification_executor.submit(self._notify_downstream, job)
            return len(eligible)

    def active_count(self) -> int:
        with self._lock:
            self._reap_locked()
            return len(self._active)

    def close(self, *, wait: bool = False) -> None:
        """Stop scheduling; waiting is optional and never delegated to a worker."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)
        self._notification_executor.shutdown(wait=wait, cancel_futures=False)

    def _reap_locked(self) -> None:
        self._active = {job_id: future for job_id, future in self._active.items() if not future.done()}

    def _recover_unsettled(self) -> None:
        for job in self.store.discover_unsettled():
            with self._lock:
                active = self._active.get(job.job_id)
                if active is not None and not active.done():
                    continue
            if job.state is LocalJobState.RUNNING and job.provider_entered:
                if not self.store.recover_recorded_result(job):
                    continue
                refreshed = self.store.get(job.job_id)
                if refreshed is None:
                    continue
                job = refreshed
            if job.state is LocalJobState.RESULT_RECORDED:
                if self.store.recover_downstream_pending(job):
                    self._settle_recovered_handle(job)
            elif job.state is LocalJobState.RUNNING and not job.provider_entered:
                alive = runner_owner_alive(job.runner_owner)
                if alive is False:
                    self.store.release_dead_owner_claim(job, "previous runner owner is authoritatively dead")

    def _settle_recovered_handle(self, job: LocalJobRecord) -> None:
        with self._lock:
            handle = self._checkpoint_handles.pop(job.execution_incarnation, None)
        if handle is not None:
            handle.confirm_settled(job.result_reference)

    def _notify_downstream(self, job: LocalJobRecord) -> None:
        try:
            if self.completion_wake is not None:
                self.completion_wake(job)
        except Exception as exc:
            logger.warning("Local job completion wake failed for {}: {}", job.job_id, exc)
        finally:
            with self._lock:
                self._notifying.discard(job.job_id)

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
            with self._lock:
                self._checkpoint_handles.pop(claim.record.execution_incarnation, None)
            current = self.store.get(claim.record.job_id)
            if current is not None and self.completion_wake is not None:
                self.wake_downstream()
        finally:
            reset_invocation_gate(gate_token)

    @staticmethod
    def _checkpoint_without_provider(handle: InvocationHandle, claim: LocalJobClaim, outcome: str, *, settle: bool = True) -> None:
        handle.begin_checkpointing(outcome)
        if settle:
            handle.confirm_settled(f"runner-refusal:{claim.record.execution_incarnation}")
        else:
            handle.record_checkpoint_attempt_failed(outcome)
