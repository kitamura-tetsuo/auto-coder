from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

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
    assert wake_attempts == [job_id]
    assert runner.wake_downstream() == 1
    assert wake_attempts == [job_id, job_id]
    assert adapter.calls == 1
    runner.close()


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
