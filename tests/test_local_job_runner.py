from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from auto_coder.invocation_admission import InvocationAdmissionGate, InvocationState
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import ContinuationState, InvocationOutcome, LocalJobClaim, LocalJobKind, LocalJobOffer, LocalJobRecord, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import LocalJobExecutionResult, LocalJobRunner


def _accepted(tmp_path: Path, attempt: str, prompt: str = "implement", repository: str = "owner/repo") -> tuple[LocalJobStore, str]:
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / f"{attempt}.sqlite3")
    owner, repo = repository.split("/", 1)
    identity = IssueAttemptIdentity(owner, repo, 42, attempt)
    assert guard.reserve(identity, CandidateHandoff("codex", "local")).admitted
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, repository, 42, attempt, "codex", prompt)
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
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, completion_wake=lambda job: wakes.append(job.job_id))

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


def test_runner_promotes_transformed_domain_result_under_muse_continuation_fence(tmp_path: Path) -> None:
    """A confirmed provider summary may be wrapped by the domain handoff."""
    store, job_id = _accepted(tmp_path, "muse-domain-result")
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    raw_output = "ACTION_SUMMARY: fixed the background failure"
    domain_output = '{"output":"ACTION_SUMMARY: fixed the background failure","workspace":"/bound"}'

    class ConfirmingAdapter:
        calls = 0

        def authorize_provider_entry(self, _job):  # type: ignore[no-untyped-def]
            return True

        def invoke(self, job):  # type: ignore[no-untyped-def]
            self.calls += 1
            claim = LocalJobClaim(job, True)
            assert store.bind_workspace(
                claim,
                workspace_path=workspace,
                source_commit="a" * 40,
                source_ref="main",
                work_branch="issue-42",
                publication_remote="origin",
                owner_incarnation="owner-1",
                owner_generation=None,
            )
            assert store.bind_continuation(claim, "muse/session-1", job.runner_owner) is not None
            continuation = store.get_continuation(job.job_id)
            assert continuation is not None
            assert store.record_candidate_output(job.job_id, job.runner_owner, continuation.frontier, raw_output)
            assert store.confirm_final_output(job.job_id, job.runner_owner, continuation.frontier)
            return LocalJobExecutionResult(InvocationOutcome.COMPLETED, domain_output)

    adapter = ConfirmingAdapter()
    wakes: list[str] = []
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter},
        completion_wake=lambda job: wakes.append(job.job_id),
    )

    assert runner.poll() == 1
    _wait(lambda: runner.active_count() == 0)
    completed = store.get(job_id)
    assert completed is not None
    assert completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    artifact = store.get_result_artifact(completed.result_reference)
    assert artifact is not None and artifact.output == domain_output
    continuation = store.get_continuation(job_id)
    assert continuation is not None and continuation.final_output == raw_output
    _wait(lambda: wakes == [job_id])
    assert adapter.calls == 1
    runner.close()


@pytest.mark.parametrize("outcome", [InvocationOutcome.FAILED, InvocationOutcome.INTERRUPTED])
@pytest.mark.parametrize("writer_owner", ["process:999999999:1", "unknown", "live"])
@pytest.mark.parametrize("recorded", [False, True])
def test_unsuccessful_continuation_recovery_requires_settled_writers(tmp_path: Path, outcome: InvocationOutcome, writer_owner: str, recorded: bool) -> None:
    """A timed-out Muse job must settle its failure without confirming success."""
    from auto_coder.local_job_runner import current_runner_owner

    store, job_id = _accepted(tmp_path, "timeout-muse")
    dead_owner = "process:999999998:1"
    claim = store.claim(job_id, dead_owner)
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    assert store.bind_workspace(
        claim,
        workspace_path=tmp_path.resolve(),
        source_commit="a" * 40,
        source_ref="main",
        work_branch="issue-42",
        publication_remote="origin",
        owner_incarnation="owner-1",
        owner_generation=None,
    )
    assert store.bind_continuation(claim, "muse/timed-out", dead_owner, deadline_at=0.0)
    writer = current_runner_owner() if writer_owner == "live" else writer_owner
    assert store.bind_continuation_writer(job_id, dead_owner, tmp_path, writer)
    assert store.observe_continuation_task(job_id, dead_owner, "unfinished-task", "running")
    artifact = store.persist_result_artifact(claim, outcome, "Muse invocation timed out", executor_token=dead_owner)
    assert artifact is not None
    if recorded:
        assert store.record_result(claim, outcome, artifact.artifact_id, "provider raised", executor_token=dead_owner)
    wakes: list[str] = []
    gate = InvocationAdmissionGate()
    adapter = BarrierAdapter()
    reopened = LocalJobStore(store.path)
    runner = LocalJobRunner(reopened, gate, repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, completion_wake=lambda job: wakes.append(job.job_id))
    try:
        assert runner.poll() == 0
        recovered = reopened.get(job_id)
        continuation = reopened.get_continuation(job_id)
        assert recovered is not None and continuation is not None
        assert recovered.invocation_outcome is outcome
        assert recovered.result_reference == artifact.artifact_id
        assert continuation.final_output == ""
        assert adapter.calls == 0
        if writer_owner == "process:999999999:1":
            assert recovered.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
            assert continuation.state is ContinuationState(outcome.value)
            _wait(lambda: wakes == [job_id])
        else:
            assert recovered.state is LocalJobState.RESULT_RECORDED
            assert continuation.state is ContinuationState.FOREGROUND
            assert wakes == []
    finally:
        runner.close(wait=True)


@pytest.mark.parametrize("recorded", [False, True])
def test_confirmed_result_recovery_uses_retained_executor_token(tmp_path: Path, recorded: bool) -> None:
    """Recovery tokens differ from the original dead runner after takeover."""
    store, job_id = _accepted(tmp_path, "confirmed-muse-recovery")
    dead_owner = "process:999999999:1"
    claim = store.claim(job_id, dead_owner)
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    assert store.bind_workspace(
        claim,
        workspace_path=tmp_path.resolve(),
        source_commit="a" * 40,
        source_ref="main",
        work_branch="issue-42",
        publication_remote="origin",
        owner_incarnation="owner-1",
        owner_generation=None,
    )
    token = "recovery-executor"
    assert store.bind_continuation(claim, "muse/confirmed", token)
    assert store.record_candidate_output(job_id, token, 0, "ACTION_SUMMARY: implemented")
    assert store.confirm_final_output(job_id, token, 0)
    artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, "domain:implemented", executor_token=token)
    assert artifact is not None
    if recorded:
        assert store.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id, executor_token=token)
    adapter = BarrierAdapter()
    runner = LocalJobRunner(LocalJobStore(store.path), InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    try:
        runner.recover_unsettled()
        recovered = store.get(job_id)
        assert recovered is not None and recovered.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
        assert recovered.result_reference == artifact.artifact_id
        assert recovered.runner_owner == dead_owner
        assert store.get_continuation(job_id).executor_token == token
        assert adapter.calls == 0
    finally:
        runner.close(wait=True)


def test_dead_host_with_live_descendant_does_not_confirm_writer_settlement(_use_real_commands) -> None:
    from auto_coder.local_job_runner import process_owner, writer_group_settled

    host = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,sys; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(child.pid,flush=True); sys.stdin.readline()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert host.stdout is not None and host.stdin is not None
        descendant = int(host.stdout.readline())
        owner = process_owner(host.pid)
        host.stdin.write("exit\n")
        host.stdin.flush()
        assert host.wait(timeout=5) == 0
        assert Path(f"/proc/{descendant}").exists()
        assert not writer_group_settled(owner)
    finally:
        os.killpg(host.pid, signal.SIGKILL)
        host.communicate(timeout=5)
    _wait(lambda: writer_group_settled(owner))


def test_dead_owner_recovery_reuses_retained_session_and_original_deadline(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "recover-muse")
    workspace = (tmp_path / "retained-workspace").resolve()
    workspace.mkdir()
    dead_owner = "process:999999999:1"
    claim = store.claim(job_id, dead_owner)
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    assert store.bind_workspace(
        claim,
        workspace_path=workspace,
        source_commit="a" * 40,
        source_ref="main",
        work_branch="issue-42",
        publication_remote="origin",
        owner_incarnation="owner-1",
        owner_generation=None,
    )
    deadline = time.time() + 120
    assert store.bind_continuation(claim, "muse/session-retained", dead_owner, deadline_at=deadline)

    class RecoveryAdapter(BarrierAdapter):
        recovered_session = ""
        remaining = 0.0

        def authorize_recovery(self, job):  # type: ignore[no-untyped-def]
            return job.job_id == job_id

        def recover_continuation(self, job, provider_session, remaining_seconds):  # type: ignore[no-untyped-def]
            self.recovered_session = provider_session
            self.remaining = remaining_seconds
            retained = store.get_continuation(job.job_id)
            assert retained is not None
            assert store.record_candidate_output(job.job_id, retained.executor_token, retained.frontier, "ACTION_SUMMARY: recovered")
            assert store.confirm_final_output(job.job_id, retained.executor_token, retained.frontier)
            return LocalJobExecutionResult(InvocationOutcome.COMPLETED, "domain:recovered")

    adapter = RecoveryAdapter()
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    runner.recover_unsettled()

    recovered = store.get(job_id)
    continuation = store.get_continuation(job_id)
    assert recovered is not None and recovered.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
    assert continuation is not None and continuation.state is ContinuationState.FINAL_CONFIRMED
    assert continuation.executor_token.startswith(f"{runner.runner_id}:recovery:")
    assert adapter.recovered_session == "muse/session-retained"
    assert 0 < adapter.remaining <= 120
    assert adapter.calls == 0
    assert not store.record_candidate_output(job_id, dead_owner, continuation.frontier, "ACTION_SUMMARY: stale")
    assert not store.confirm_final_output(job_id, dead_owner, continuation.frontier)

    contender_adapter = RecoveryAdapter()
    contender = LocalJobRunner(
        LocalJobStore(store.path),
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: contender_adapter},
    )
    contender.recover_unsettled()
    after_contender = store.get_continuation(job_id)
    assert after_contender == continuation
    assert contender_adapter.recovered_session == ""
    contender.close()
    runner.close()


@pytest.mark.parametrize(
    ("deadline", "reason"),
    [
        (None, "legacy continuation has no trustworthy invocation deadline"),
        (0.0, "logical invocation deadline expired"),
    ],
)
def test_recovery_without_live_budget_stays_non_authorizing(tmp_path: Path, deadline: float | None, reason: str) -> None:
    store, job_id = _accepted(tmp_path, "legacy-muse")
    workspace = (tmp_path / "legacy-workspace").resolve()
    workspace.mkdir()
    claim = store.claim(job_id, "process:999999999:1")
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    assert store.bind_workspace(
        claim,
        workspace_path=workspace,
        source_commit="a" * 40,
        source_ref="main",
        work_branch="issue-42",
        publication_remote="origin",
        owner_incarnation="owner-1",
        owner_generation=None,
    )
    assert store.bind_continuation(
        claim,
        "muse/legacy",
        claim.record.runner_owner,
        deadline_at=deadline,
    )

    class RefusingAdapter(BarrierAdapter):
        recovery_calls = 0

        def authorize_recovery(self, _job):  # type: ignore[no-untyped-def]
            return True

        def recover_continuation(self, *_args):  # type: ignore[no-untyped-def]
            self.recovery_calls += 1
            raise AssertionError("legacy recovery must not enter the provider")

    adapter = RefusingAdapter()
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    runner.recover_unsettled()

    retained = store.get_continuation(job_id)
    assert retained is not None
    assert retained.state is ContinuationState.RECONCILIATION_REQUIRED
    assert retained.reconciliation_reason == reason
    assert adapter.recovery_calls == 0
    runner.close()


def test_missing_production_recovery_boundary_retains_diagnostic(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "missing-recovery-boundary")
    workspace = (tmp_path / "retained-workspace").resolve()
    workspace.mkdir()
    claim = store.claim(job_id, "process:999999999:1")
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    assert store.bind_workspace(
        claim,
        workspace_path=workspace,
        source_commit="a" * 40,
        source_ref="main",
        work_branch="issue-42",
        publication_remote="origin",
        owner_incarnation="owner-1",
        owner_generation=None,
    )
    assert store.bind_continuation(
        claim,
        "muse/no-adapter",
        claim.record.runner_owner,
        deadline_at=time.time() + 30,
    )
    runner = LocalJobRunner(
        store,
        InvocationAdmissionGate(),
        repository="owner/repo",
        capacity=1,
        adapters={LocalJobKind.ISSUE_IMPLEMENTATION: BarrierAdapter()},
    )

    runner.recover_unsettled()

    retained = store.get_continuation(job_id)
    assert retained is not None
    assert retained.reconciliation_reason == ("domain adapter does not support retained continuation recovery")
    runner.close()


@pytest.mark.parametrize("foreign_phase", ["pending", "unentered", "artifact", "result_recorded", "downstream_pending"])
def test_repository_scope_preserves_foreign_jobs_through_every_runner_phase(tmp_path: Path, foreign_phase: str) -> None:
    store, foreign_id = _accepted(tmp_path, "foreign", repository="owner/other")
    if foreign_phase != "pending":
        claim = store.claim(foreign_id, "process:999999999:1")
        assert claim is not None and claim.acquired
        if foreign_phase != "unentered":
            assert store.mark_provider_entered(claim)
            artifact = store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, "foreign output")
            assert artifact is not None
            if foreign_phase != "artifact":
                assert store.record_result(claim, InvocationOutcome.COMPLETED, artifact.artifact_id)
                if foreign_phase == "downstream_pending":
                    assert store.mark_downstream_pending(claim)
    with sqlite3.connect(store.path) as connection:
        before = connection.execute("SELECT * FROM local_jobs WHERE job_id=?", (foreign_id,)).fetchone()
    _, own_id = _accepted(tmp_path, "own")
    adapter = BarrierAdapter()
    adapter.release.set()
    wakes: list[str] = []

    def settle_own_job(job: LocalJobRecord) -> None:
        assert job.job_id == own_id
        assert store.settle(LocalJobClaim(job, True), "own publication completed")
        wakes.append(job.job_id)

    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, completion_wake=settle_own_job)
    try:
        assert runner.poll() == 1
        assert adapter.entered.wait(5)
        _wait(lambda: runner.active_count() == 0 and wakes == [own_id])
        assert runner.poll() == 0
        runner.recover_unsettled()
        runner.wake_downstream()
    finally:
        runner.close(wait=True)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT * FROM local_jobs WHERE job_id=?", (foreign_id,)).fetchone() == before
    assert adapter.calls == 1
    assert wakes == [own_id]
    own = store.get(own_id)
    assert own is not None and own.state is LocalJobState.SETTLED


class PreEntryBarrierAdapter(BarrierAdapter):
    def authorize_provider_entry(self, job):  # type: ignore[no-untyped-def]
        authorized = super().authorize_provider_entry(job)
        self.entered.set()
        assert self.release.wait(5)
        return authorized


def test_foreign_runner_cannot_reclaim_a_live_job_with_an_unresolvable_container_pid(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "live")
    adapter = PreEntryBarrierAdapter()
    # The owning process exists in another PID namespace from the observer.
    owner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, runner_id="process:999999999:1")
    observer = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/other", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: BarrierAdapter()}, job_ids=frozenset({job_id}))
    try:
        assert owner.poll() == 1
        assert adapter.entered.wait(5)
        running = store.get(job_id)
        assert running is not None and running.state is LocalJobState.RUNNING
        assert running.provider_entered is False
        assert observer.poll() == 0
        observer.recover_unsettled()
        assert store.get(job_id) == running
        adapter.release.set()
        _wait(lambda: owner.active_count() == 0)
        completed = store.get(job_id)
        assert completed is not None and completed.state is LocalJobState.DOWNSTREAM_EFFECTS_PENDING
        assert completed.execution_incarnation == running.execution_incarnation
        assert adapter.calls == 1
    finally:
        adapter.release.set()
        owner.close(wait=True)
        observer.close(wait=True)


def test_released_pending_row_cannot_enqueue_another_future_while_its_job_is_active(tmp_path: Path) -> None:
    store, job_id = _accepted(tmp_path, "active")
    adapter = PreEntryBarrierAdapter()
    gate = InvocationAdmissionGate()
    runner = LocalJobRunner(store, gate, repository="owner/repo", capacity=2, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter}, runner_id="process:999999999:1")
    try:
        assert runner.poll() == 1
        assert adapter.entered.wait(5)
        running = store.get(job_id)
        assert running is not None and not running.provider_entered
        assert store.release_dead_owner_claim(running, "external pre-entry recovery")
        pending = store.get(job_id)
        assert pending is not None and pending.state is LocalJobState.PENDING
        assert len(gate.unsettled_snapshot()) == 1
        invocation_id = gate.unsettled_snapshot()[0].invocation_id
        assert runner.poll() == 0
        assert store.get(job_id) == pending
        assert len(gate.unsettled_snapshot()) == 1
        assert runner.active_count() == 1
        adapter.release.set()
        _wait(lambda: runner.active_count() == 0)
        assert adapter.calls == 0
        unfinished = gate.unsettled_snapshot()
        assert len(unfinished) == 1
        assert unfinished[0].invocation_id == invocation_id
        assert unfinished[0].state is InvocationState.CHECKPOINTING
        assert unfinished[0].checkpoint_failure_count == 1
    finally:
        adapter.release.set()
        runner.close(wait=True)


def test_capacity_leaves_extra_job_pending_until_slot_frees(tmp_path: Path) -> None:
    store, first = _accepted(tmp_path, "attempt-1", "implement-one")
    _, second = _accepted(tmp_path, "attempt-2", "implement-two")
    adapter = BarrierAdapter()
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})

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
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.PR_REVIEW_CORRECTION: adapter})

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
        repository="owner/repo",
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
    runner = LocalJobRunner(store, gate, repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    assert runner.poll() == 0
    assert store.get(job_id).state is LocalJobState.PENDING  # type: ignore[union-attr]
    assert adapter.calls == 0
    runner.close()

    open_gate = InvocationAdmissionGate()
    runner = LocalJobRunner(store, open_gate, repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
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
    first = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
    second = LocalJobRunner(LocalJobStore(store.path), InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
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
        repository="owner/repo",
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
    # The callback records its attempt before the notification lane releases
    # ownership. Retry once that asynchronous cleanup actually permits it.
    _wait(lambda: runner.wake_downstream() == 1)
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
        repository="owner/repo",
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
        repository="owner/repo",
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
        repository="owner/repo",
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
    first = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
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
        repository="owner/repo",
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
    runner = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: first_adapter})
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
        repository="owner/repo",
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
    restarted = LocalJobRunner(store, InvocationAdmissionGate(), repository="owner/repo", capacity=1, adapters={LocalJobKind.ISSUE_IMPLEMENTATION: adapter})
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
        repository="owner/repo",
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
        repository="owner/repo",
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
