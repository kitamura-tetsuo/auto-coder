from __future__ import annotations

import time
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Lock, Thread

from auto_coder.invocation_admission import InvocationAdmissionGate
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobExecutionResult, LocalJobRunner


def _accepted(tmp_path: Path, attempt: str, prompt: str = "implement") -> tuple[LocalJobStore, str]:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / f"{attempt}.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, attempt)
    assert guard.reserve(identity, CandidateHandoff("codex", "local")).admitted
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, attempt, "codex", prompt)
    assert store.offer_issue(offer, identity, guard) is not None
    return store, offer.job_id


@dataclass
class BarrierAdapter:
    entered: Event = field(default_factory=Event)
    release: Event = field(default_factory=Event)
    calls: int = 0
    lock: Lock = field(default_factory=Lock)
    authorized: bool = True

    def authorize_provider_entry(self, job):  # type: ignore[no-untyped-def]
        assert job.repository == "owner/repo"
        assert job.backend_name == "codex"
        assert job.invocation_input.startswith("implement")
        return self.authorized

    def invoke(self, job):  # type: ignore[no-untyped-def]
        with self.lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return LocalJobExecutionResult(InvocationOutcome.COMPLETED, f"output:{job.invocation_input}")


def _wait(predicate, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_runner_releases_submitter_and_checkpoints_real_output(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "attempt-1")
    adapter = BarrierAdapter()
    wakes: list[str] = []
    runner = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, completion_wake=lambda job: wakes.append(job.job_id))

    assert runner.poll() == 1
    assert adapter.entered.wait(5)
    running = LocalJobStore(store.path).get(job_id)
    assert running is not None and running.state is LocalJobState.RUNNING
    assert running.provider_entered and running.runner_owner == runner.runner_id
    adapter.release.set()
    _wait(lambda: runner.active_count() == 0)

    completed = LocalJobStore(store.path).get(job_id)
    assert completed is not None and completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = store.get_result_artifact(completed.result_reference)
    assert artifact is not None and artifact.output == "output:implement"
    assert wakes == [job_id]
    assert runner.poll() == 0
    assert adapter.calls == 1
    runner.close()


def test_capacity_leaves_extra_job_pending_until_slot_frees(tmp_path: Path) -> None:
    store, first = _accepted(tmp_path, "attempt-1", "implement-one")
    _, second = _accepted(tmp_path, "attempt-2", "implement-two")
    adapter = BarrierAdapter()
    runner = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})

    assert runner.poll() == 1
    assert adapter.entered.wait(5)
    assert runner.poll() == 0
    assert store.get(first).state is LocalJobState.RUNNING  # type: ignore[union-attr]
    assert store.get(second).state is LocalJobState.PENDING  # type: ignore[union-attr]
    adapter.release.set()
    _wait(lambda: runner.active_count() == 0)
    adapter.entered.clear()
    assert runner.poll() == 1
    assert adapter.entered.wait(5)
    _wait(lambda: runner.active_count() == 0)
    assert adapter.calls == 2
    runner.close()


def test_runner_ignores_job_kinds_without_a_registered_adapter(tmp_path: Path) -> None:
    store, issue_job = _accepted(tmp_path, "attempt-1")
    adapter = BarrierAdapter()
    runner = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.PR_REVIEW_CORRECTION: adapter})

    assert runner.poll() == 0
    assert store.get(issue_job).state is LocalJobState.PENDING  # type: ignore[union-attr]
    assert adapter.calls == 0
    runner.close()


def test_pr_only_runner_does_not_consume_issue_downstream_effects(tmp_path: Path) -> None:
    store, issue_job = _accepted(tmp_path, "attempt-downstream")
    claim = store.claim(issue_job, "issue-runner")
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, '{"workspace_path":"/tmp/issue"}')
    assert artifact is not None
    assert store.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id)
    assert store.mark_downstream_pending(claim)
    wakes: list[str] = []
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.PR_REVIEW_CORRECTION: BarrierAdapter()},
        completion_wake=lambda job: wakes.append(job.job_id),
    )

    assert runner.wake_downstream() == 0
    time.sleep(0.05)
    retained = store.get(issue_job)
    assert retained is not None and retained.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert wakes == []
    runner.close()


def test_closed_admission_and_denied_authority_never_enter_provider(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "attempt-1")
    gate = InvocationAdmissionGate()
    gate.close_admission("test drain")
    adapter = BarrierAdapter(authorized=False)
    runner = LocalJobRunner(store, gate, capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    assert runner.poll() == 0
    assert store.get(job_id).state is LocalJobState.PENDING  # type: ignore[union-attr]
    assert adapter.calls == 0
    runner.close()

    open_gate = InvocationAdmissionGate()
    runner = LocalJobRunner(store, open_gate, capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    assert runner.poll() == 1
    _wait(lambda: runner.active_count() == 0)
    record = store.get(job_id)
    assert record is not None and record.state is LocalJobState.PENDING
    assert not record.provider_entered and "denied" in record.diagnostic
    assert adapter.calls == 0
    runner.close()


def test_competing_runners_enter_provider_only_once(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "attempt-1")
    adapter = BarrierAdapter()
    first = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    second = LocalJobRunner(LocalJobStore(store.path), InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    assert first.poll() + second.poll() == 1
    assert adapter.entered.wait(5)
    adapter.release.set()
    _wait(lambda: first.active_count() + second.active_count() == 0)
    assert adapter.calls == 1
    assert store.get(job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
    first.close()
    second.close()


def test_failed_completion_wake_replays_without_reinvoking_provider(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "attempt-1")
    adapter = BarrierAdapter()
    wake_attempts: list[str] = []

    def wake(job):  # type: ignore[no-untyped-def]
        wake_attempts.append(job.job_id)
        if len(wake_attempts) == 1:
            raise RuntimeError("consumer unavailable")

    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=wake,
    )
    assert runner.poll() == 1
    assert adapter.entered.wait(5)
    adapter.release.set()
    _wait(lambda: runner.active_count() == 0)

    assert store.get(job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
    _wait(lambda: wake_attempts == [job_id])
    assert runner.wake_downstream() == 1
    _wait(lambda: wake_attempts == [job_id, job_id])
    assert adapter.calls == 1
    runner.close()


class FailDownstreamOnceStore(LocalJobStore):
    failed = False

    def mark_downstream_pending(self, claim):  # type: ignore[no-untyped-def]
        if not self.failed:
            self.failed = True
            return False
        return super().mark_downstream_pending(claim)


class FailRecordOnceStore(LocalJobStore):
    failed = False

    def record_result(self, claim, outcome, result_reference, diagnostic=""):  # type: ignore[no-untyped-def]
        if not self.failed:
            self.failed = True
            return False
        return super().record_result(claim, outcome, result_reference, diagnostic)


def test_restart_recovers_committed_result_without_reinvoking_provider(tmp_path: Path) -> None:
    accepted_store, job_id = _accepted(tmp_path, "attempt-1")
    failing_store = FailDownstreamOnceStore(accepted_store.path)
    adapter = BarrierAdapter()
    first = LocalJobRunner(
        failing_store,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
    )
    assert first.poll() == 1
    assert adapter.entered.wait(5)
    adapter.release.set()
    _wait(lambda: first.active_count() == 0)
    assert LocalJobStore(failing_store.path).get(job_id).state is LocalJobState.RESULT_RECORDED  # type: ignore[union-attr]
    first.close()

    wakes: list[str] = []
    restarted = LocalJobRunner(
        LocalJobStore(failing_store.path),
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )
    assert restarted.poll() == 0
    _wait(lambda: wakes == [job_id])
    recovered = LocalJobStore(failing_store.path).get(job_id)
    assert recovered is not None and recovered.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert LocalJobStore(failing_store.path).get_result_artifact(recovered.result_reference).output == "output:implement"  # type: ignore[union-attr]
    assert adapter.calls == 1
    restarted.close()


def test_same_runner_recovery_settles_original_drain_handle(tmp_path: Path) -> None:
    accepted_store, job_id = _accepted(tmp_path, "attempt-1")
    store = FailDownstreamOnceStore(accepted_store.path)
    gate = InvocationAdmissionGate()
    adapter = BarrierAdapter()
    adapter.release.set()
    wakes: list[str] = []
    runner = LocalJobRunner(
        store,
        gate,
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )
    assert runner.poll() == 1
    assert adapter.entered.wait(5)
    _wait(lambda: runner.active_count() == 0)
    assert store.get(job_id).state is LocalJobState.RESULT_RECORDED  # type: ignore[union-attr]
    assert len(gate.unsettled_snapshot()) == 1

    assert runner.poll() == 0
    _wait(lambda: wakes == [job_id])
    gate.close_admission("regression drain")
    assert gate.unsettled_snapshot() == []
    assert gate.is_graceful_ready
    assert store.get(job_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]
    assert adapter.calls == 1
    runner.close()


def test_restart_recovers_persisted_artifact_after_result_checkpoint_failure(tmp_path: Path) -> None:
    accepted_store, job_id = _accepted(tmp_path, "attempt-1")
    store = FailRecordOnceStore(accepted_store.path)
    adapter = BarrierAdapter()
    adapter.release.set()
    first = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    assert first.poll() == 1
    assert adapter.entered.wait(5)
    _wait(lambda: first.active_count() == 0)
    stranded = LocalJobStore(store.path).get(job_id)
    assert stranded is not None and stranded.state is LocalJobState.RUNNING
    assert stranded.provider_entered and stranded.result_reference == ""
    first.close()

    wakes: list[str] = []
    restarted_store = LocalJobStore(store.path)
    restarted = LocalJobRunner(
        restarted_store,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )
    assert restarted.poll() == 0
    _wait(lambda: wakes == [job_id])
    recovered = restarted_store.get(job_id)
    assert recovered is not None and recovered.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = restarted_store.get_result_artifact(recovered.result_reference)
    assert artifact is not None and artifact.output == "output:implement"
    assert artifact.execution_incarnation == recovered.execution_incarnation
    assert adapter.calls == 1
    restarted.close()


def test_blocking_completion_notification_never_occupies_submitter_or_capacity(tmp_path: Path) -> None:
    store, completed_id = _accepted(tmp_path, "attempt-1", "implement-completed")
    first_adapter = BarrierAdapter()
    runner = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: first_adapter})
    assert runner.poll() == 1
    assert first_adapter.entered.wait(5)
    first_adapter.release.set()
    _wait(lambda: runner.active_count() == 0)
    assert store.get(completed_id).state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING  # type: ignore[union-attr]

    _, pending_id = _accepted(tmp_path, "attempt-2", "implement-pending")
    notification_entered = Event()
    release_notification = Event()

    def blocking_wake(job):  # type: ignore[no-untyped-def]
        if job.job_id == completed_id:
            notification_entered.set()
            assert release_notification.wait(5)

    next_adapter = BarrierAdapter()
    next_adapter.release.set()
    runner.completion_wake = blocking_wake
    runner.adapters[LocalJobKind.ISSUE_IMPLEMENTATION] = next_adapter
    poll_returned = Event()
    submitter = Thread(target=lambda: (runner.poll(), poll_returned.set()))
    submitter.start()

    assert notification_entered.wait(5)
    assert poll_returned.wait(1)
    assert next_adapter.entered.wait(1)
    assert store.get(pending_id).provider_entered  # type: ignore[union-attr]
    release_notification.set()
    submitter.join(5)
    runner.close(wait=True)


@dataclass
class PreentryProcessAdapter:
    marker: str

    def authorize_provider_entry(self, job):  # type: ignore[no-untyped-def]
        Path(self.marker).write_text(job.execution_incarnation, encoding="utf-8")
        while True:
            time.sleep(1)

    def invoke(self, job):  # type: ignore[no-untyped-def]
        raise AssertionError("provider must not be reached in terminated process")


def _run_until_preentry_blocked(store_path: str, marker: str) -> None:
    runner = LocalJobRunner(
        LocalJobStore(Path(store_path)),
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: PreentryProcessAdapter(marker)},
    )
    runner.poll()
    while True:
        time.sleep(1)


def test_restart_recovers_dead_preentry_owner_but_suppresses_ambiguous_owner(tmp_path: Path) -> None:
    store, recoverable_id = _accepted(tmp_path, "attempt-1")
    marker = tmp_path / "preentry"
    child = get_context("spawn").Process(target=_run_until_preentry_blocked, args=(str(store.path), str(marker)))
    child.start()
    _wait(marker.exists)
    claimed = store.get(recoverable_id)
    assert claimed is not None and claimed.state is LocalJobState.RUNNING
    assert not claimed.provider_entered and claimed.runner_owner.startswith(f"process:{child.pid}:")
    child.terminate()
    child.join(5)
    assert not child.is_alive()

    adapter = BarrierAdapter()
    adapter.release.set()
    restarted = LocalJobRunner(store, InvocationAdmissionGate(), capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    assert restarted.poll() == 1
    assert adapter.entered.wait(5)
    _wait(lambda: restarted.active_count() == 0)
    assert adapter.calls == 1

    _, ambiguous_id = _accepted(tmp_path, "attempt-2")
    ambiguous = store.claim(ambiguous_id, "unverifiable-owner")
    assert ambiguous is not None and ambiguous.acquired
    adapter.entered.clear()
    assert restarted.poll() == 0
    assert not adapter.entered.wait(0.1)
    retained = store.get(ambiguous_id)
    assert retained is not None and retained.state is LocalJobState.RUNNING
    assert not retained.provider_entered
    restarted.close()


@dataclass
class EmptyOutputAdapter:
    def authorize_provider_entry(self, job):  # type: ignore[no-untyped-def]
        return True

    def invoke(self, job):  # type: ignore[no-untyped-def]
        return LocalJobExecutionResult(InvocationOutcome.COMPLETED, "")


def test_empty_actual_output_is_still_durably_checkpointed(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "attempt-1")
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: EmptyOutputAdapter()},
    )
    assert runner.poll() == 1
    _wait(lambda: runner.active_count() == 0)

    record = store.get(job_id)
    assert record is not None and record.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = store.get_result_artifact(record.result_reference)
    assert artifact is not None and artifact.output == ""
    runner.close()


class FailingArtifactStore(LocalJobStore):
    def persist_result_artifact(self, claim, outcome, output):  # type: ignore[no-untyped-def]
        return None


def test_failed_result_checkpoint_never_signals_downstream_completion(tmp_path: Path) -> None:
    accepted_store, job_id = _accepted(tmp_path, "attempt-1")
    store = FailingArtifactStore(accepted_store.path)
    adapter = BarrierAdapter()
    wakes: list[str] = []
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )

    assert runner.poll() == 1
    assert adapter.entered.wait(5)
    adapter.release.set()
    _wait(lambda: runner.active_count() == 0)

    restarted = LocalJobStore(store.path)
    record = restarted.get(job_id)
    assert record is not None and record.state is LocalJobState.RUNNING
    assert record.provider_entered
    assert record.result_reference == ""
    assert wakes == []
    assert runner.wake_downstream() == 0
    assert adapter.calls == 1
    runner.close()
