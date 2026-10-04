"""Production-oriented regressions for bounded Codex initial-PR recovery."""

import asyncio
import time
from pathlib import Path

import pytest

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.cloud_task_client_base import CloudTaskState
from auto_coder.codex_cloud_client import CodexCloudClient
from auto_coder.codex_observation import (
    CodexExecutionEvidence,
    CodexRunObservation,
    ObservationBinding,
    PullRequestEvidence,
    PullRequestPresence,
)
from auto_coder.codex_pr_recovery import (
    CodexPRRecoveryMonitor,
    CodexPRRecoveryStore,
    RecoveryOutcome,
)
from auto_coder.codex_wham_client import FollowUpDeliveryOutcome, FollowUpDeliveryResult
from auto_coder.codex_work_accounting import CodexWorkAccounting
from auto_coder.codex_work_reconstruction import production_codex_reconstructor
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository

TASK = "task_e_6a26c19ac8a88326af83ebfb44b89fe2"


def _commit_retirement(slots: ImplementationSlotRepository, owner: ImplementationOwner) -> str:
    """Commit the same retired-history then active-removal sequence as production."""
    with slots.serialize(owner), slots._state_lock():
        active = slots._read()
        record = active[owner.key]
        incarnation = str(record["incarnation"])
        retired = slots._read_retired()
        retired[incarnation] = {
            "repository": slots.repo_name,
            "kind": owner.kind,
            "number": owner.number,
            "incarnation": incarnation,
            "implementation_prs": list(record.get("implementation_prs", [])),
            "provider_sessions": list(record.get("provider_sessions", [])),
            "generation": record.get("implementation_generation"),
            "retired_at": time.time(),
        }
        slots._write_retired(retired)
        del active[owner.key]
        slots._write(active)
    return incarnation


class Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


class Observations:
    def __init__(self, run: CloudRun) -> None:
        self.run = run
        self.pr = PullRequestEvidence(PullRequestPresence.NO_MATCHING_PR)
        self.state = CloudTaskState.COMPLETED
        self.turn = f"{TASK}~asst_1"
        self.user = f"{TASK}~user_1"
        self.calls = 0

    def observe(self, run: CloudRun) -> CodexRunObservation:
        assert run == self.run
        self.calls += 1
        execution = CodexExecutionEvidence(self.state, self.turn, self.user, self.state is CloudTaskState.COMPLETED, self.state.value)
        return CodexRunObservation(ObservationBinding.from_run(run), self.calls, self.calls, execution, self.pr, "open")


class Wham:
    def __init__(self, outcome: FollowUpDeliveryOutcome = FollowUpDeliveryOutcome.DELIVERED) -> None:
        self.outcome = outcome
        self.posts: list[tuple[str, str, str, bool]] = []

    def follow_up_preflight(self) -> bool:
        return True

    def send_follow_up(self, task_id: str, turn_id: str, text: str, qa: bool) -> FollowUpDeliveryResult:
        self.posts.append((task_id, turn_id, text, qa))
        return FollowUpDeliveryResult(self.outcome, 202 if self.outcome is FollowUpDeliveryOutcome.DELIVERED else 400)


def setup(tmp_path: Path, outcome: FollowUpDeliveryOutcome = FollowUpDeliveryOutcome.DELIVERED):
    run = CloudRun("owner/repo", 1864, 3, "codex-cloud", TASK, "codex-prod", "env-prod", "main")
    runs = CloudRunRepository("owner/repo", tmp_path / "runs.json")
    runs.save(run)
    observations = Observations(run)
    wham = Wham(outcome)
    store = CodexPRRecoveryStore(tmp_path / "recovery.sqlite3")
    clock = Clock()
    handed_off: list[int] = []

    async def enqueue(number: int) -> bool:
        handed_off.append(number)
        return True

    monitor = CodexPRRecoveryMonitor(runs, observations, wham, store, enqueue, now=clock, poll_interval=60, grace_period=120, retirement_accounting=False)
    return run, observations, wham, store, clock, handed_off, monitor


def poll(monitor: CodexPRRecoveryMonitor) -> None:
    asyncio.run(monitor.tick(asyncio.Event()))


def test_quiet_daemon_grace_exact_payload_and_restart_budget(tmp_path):
    run, observations, wham, store, clock, _, monitor = setup(tmp_path)
    poll(monitor)
    assert store.get(run.repo_name, run.task_id).state is RecoveryOutcome.COMPLETION_GRACE
    clock.value += 119
    monitor._next_due.clear()
    poll(monitor)
    assert wham.posts == []
    clock.value += 1
    monitor._next_due.clear()
    poll(monitor)
    assert wham.posts == [(TASK, f"{TASK}~asst_1", "Create PR", False)]
    assert store.get(run.repo_name, run.task_id).state is RecoveryOutcome.REMINDER_ACCEPTED

    # A fresh monitor/client after restart observes but cannot mint a new budget.
    restarted = CodexPRRecoveryMonitor(monitor.runs, observations, wham, store, monitor.enqueue_pr, now=clock, poll_interval=60, grace_period=120, retirement_accounting=False)
    poll(restarted)
    assert len(wham.posts) == 1


def test_pr_race_suppresses_post_and_hands_open_pr_to_normal_queue(tmp_path):
    run, observations, wham, store, clock, handed_off, monitor = setup(tmp_path)
    poll(monitor)
    clock.value += 120
    observations.pr = PullRequestEvidence(PullRequestPresence.PR_PRESENT, 77, "https://github.com/owner/repo/pull/77")
    monitor._next_due.clear()
    poll(monitor)
    record = store.get(run.repo_name, run.task_id)
    assert wham.posts == []
    assert handed_off == [77]
    assert record.state is RecoveryOutcome.PR_OBSERVED
    assert record.handoff_complete is True


@pytest.mark.parametrize("presence", [PullRequestPresence.PR_PRESENT, PullRequestPresence.PREVIOUSLY_PUBLISHED])
def test_completed_publication_stops_polling_across_restart(tmp_path, presence):
    run, observations, wham, store, clock, handed_off, monitor = setup(tmp_path)
    observations.pr = PullRequestEvidence(presence, 77, "https://github.com/owner/repo/pull/77")
    poll(monitor)
    record = store.get(run.repo_name, run.task_id)
    assert record.state is RecoveryOutcome.PR_OBSERVED
    assert record.pr_number == 77
    assert record.handoff_complete is True
    assert handed_off == ([77] if presence is PullRequestPresence.PR_PRESENT else [])
    assert observations.calls == 1

    # Later unavailable provider/GitHub data must not revive completed recovery.
    observations.pr = PullRequestEvidence()
    observations.state = CloudTaskState.UNKNOWN
    clock.value += 60
    poll(monitor)
    restarted_store = CodexPRRecoveryStore(store.path)
    restarted = CodexPRRecoveryMonitor(monitor.runs, observations, wham, restarted_store, monitor.enqueue_pr, now=clock, retirement_accounting=False)
    poll(restarted)
    assert observations.calls == 1
    assert restarted_store.get(run.repo_name, run.task_id) == record
    assert handed_off == ([77] if presence is PullRequestPresence.PR_PRESENT else [])
    assert wham.posts == []


def test_failed_handoff_keeps_polling_until_verified_pr_closure(tmp_path):
    run, observations, wham, store, clock, handed_off, monitor = setup(tmp_path)

    async def unavailable_queue(number):
        handed_off.append(number)
        return False

    monitor.enqueue_pr = unavailable_queue
    observations.pr = PullRequestEvidence(PullRequestPresence.PR_PRESENT, 77)
    poll(monitor)
    assert store.get(run.repo_name, run.task_id).handoff_complete is False
    clock.value += 60
    poll(monitor)
    assert handed_off == [77, 77]
    assert store.get(run.repo_name, run.task_id).handoff_complete is False

    observations.pr = PullRequestEvidence(PullRequestPresence.PREVIOUSLY_PUBLISHED, 77)
    clock.value += 60
    poll(monitor)
    assert store.get(run.repo_name, run.task_id).handoff_complete is True
    assert store.get(run.repo_name, run.task_id).pr_number == 77
    assert handed_off == [77, 77]
    assert observations.calls == 3
    clock.value += 60
    poll(monitor)
    assert observations.calls == 3
    assert wham.posts == []


def test_closed_pr_completion_write_failure_remains_retryable(tmp_path, monkeypatch):
    run, observations, wham, store, clock, handed_off, monitor = setup(tmp_path)
    observations.pr = PullRequestEvidence(PullRequestPresence.PREVIOUSLY_PUBLISHED, 77)
    original = store.mark_handoff
    monkeypatch.setattr(store, "mark_handoff", lambda candidate: False)
    poll(monitor)
    assert store.get(run.repo_name, run.task_id).handoff_complete is False
    monkeypatch.setattr(store, "mark_handoff", original)
    clock.value += 60
    poll(monitor)
    assert observations.calls == 2
    assert store.get(run.repo_name, run.task_id).handoff_complete is True
    assert handed_off == []
    assert wham.posts == []


def test_unavailability_preserves_completion_anchor(tmp_path):
    run, observations, wham, store, clock, _, monitor = setup(tmp_path)
    poll(monitor)
    anchor = store.get(run.repo_name, run.task_id).completion_observed_at
    original = observations.observe

    def unavailable(candidate):
        result = original(candidate)
        return CodexRunObservation(result.binding, result.generation, result.activity_generation, result.execution, PullRequestEvidence(), "unknown", ("GitHub unavailable",))

    observations.observe = unavailable
    clock.value += 180
    monitor._next_due.clear()
    poll(monitor)
    assert wham.posts == []
    assert store.get(run.repo_name, run.task_id).completion_observed_at == anchor
    observations.observe = original
    monitor._next_due.clear()
    poll(monitor)
    assert len(wham.posts) == 1


def test_rejected_post_spends_budget_and_needs_attention(tmp_path):
    run, observations, wham, store, clock, _, monitor = setup(tmp_path, FollowUpDeliveryOutcome.NOT_DELIVERED)
    poll(monitor)
    clock.value += 120
    monitor._next_due.clear()
    poll(monitor)
    assert store.get(run.repo_name, run.task_id).state is RecoveryOutcome.REMINDER_REJECTED
    monitor._next_due.clear()
    poll(monitor)
    assert store.get(run.repo_name, run.task_id).state is RecoveryOutcome.REMINDER_REJECTED
    assert len(wham.posts) == 1


def test_production_recovery_registers_before_direct_wham_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    owner = ImplementationOwner("issue", 1864)
    slots = ImplementationSlotRepository(repository, 1)
    assert slots.reserve(owner)
    run = CloudRun(repository, 1864, 3, "codex-cloud", TASK, "codex-prod", "env-prod", "main", launch_identity="attempt-3")
    runs = CloudRunRepository(repository)
    runs.save(run)
    observations = Observations(run)
    wham = Wham()
    clock = Clock()

    async def enqueue(number: int) -> bool:
        return True

    monitor = CodexPRRecoveryMonitor(runs, observations, wham, CodexPRRecoveryStore(), enqueue, now=clock, grace_period=120)
    poll(monitor)
    clock.value += 120
    monitor._next_due.clear()
    poll(monitor)

    assert len(wham.posts) == 1
    with slots._state_lock():
        accounting = slots._read()[owner.key]["codex_work_accounting"]
    publications = [value for value in accounting["operations"].values() if value["kind"] == "publication"]
    assert len(publications) == 1
    assert publications[0]["task_id"] == TASK
    assert publications[0]["causal_baseline"] == f"{TASK}~asst_1"


def test_retired_task_blocks_reconstructed_followup_and_direct_recovery_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    repository = "owner/repo"
    owner = ImplementationOwner("issue", 1864)
    slots = ImplementationSlotRepository(repository, 1)
    assert slots.reserve(owner)
    incarnation = slots.owner_incarnation(owner)
    assert incarnation is not None
    run = CloudRun(repository, 1864, 3, "codex-cloud", TASK, "codex-prod", "env-prod", "main", launch_identity="attempt-3")
    runs = CloudRunRepository(repository)
    runs.save(run)
    reconstructor = production_codex_reconstructor(repository, owner.number)
    CodexWorkAccounting(slots, reconstructor.consistency_ids).reconcile_from_receipt(owner, incarnation, reconstructor.reconstruct())

    observations = Observations(run)
    recovery_wham = Wham()
    clock = Clock()

    async def enqueue(number: int) -> bool:
        return True

    monitor = CodexPRRecoveryMonitor(runs, observations, recovery_wham, CodexPRRecoveryStore(), enqueue, now=clock, grace_period=120)
    poll(monitor)
    retired_incarnation = _commit_retirement(slots, owner)
    assert retired_incarnation == incarnation
    assert slots.is_incarnation_retired(incarnation) is True

    class FollowupWham:
        def __init__(self) -> None:
            self.posts = []

        def resolve_latest_assistant_turn(self, task_id):
            return f"{task_id}~asst_1"

        def send_follow_up(self, task_id, turn_id, prompt):
            self.posts.append((task_id, turn_id, prompt))
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, 202)

    followup_wham = FollowupWham()
    for _ in range(2):
        client = CodexCloudClient.__new__(CodexCloudClient)
        client.repo_name = repository
        client.wham_client = followup_wham
        client.active_tasks = {}
        assert client.send_followup(TASK, "Do not resume retired work", ("retired-repair",)) is False
    assert followup_wham.posts == []

    clock.value += 120
    monitor._next_due.clear()
    poll(monitor)
    assert recovery_wham.posts == []
    assert slots.owner_incarnation(owner) is None


def test_unresolved_and_superseded_runs_are_diagnostics_not_enrolled(tmp_path):
    run, observations, wham, _, _, _, monitor = setup(tmp_path)
    run.submission_outcome = "indeterminate"
    monitor.runs.save(run)
    poll(monitor)
    assert observations.calls == 0
    assert wham.posts == []


def test_concurrent_store_instances_allow_only_one_send_reservation(tmp_path):
    run, _, _, store, _, _, monitor = setup(tmp_path)
    poll(monitor)
    other = CodexPRRecoveryStore(store.path)
    results: list[bool] = []

    async def race() -> None:
        results.extend(
            await asyncio.gather(
                asyncio.to_thread(store.reserve_send, run, f"{TASK}~asst_1", f"{TASK}~user_1", 1120),
                asyncio.to_thread(other.reserve_send, run, f"{TASK}~asst_1", f"{TASK}~user_1", 1120),
            )
        )

    asyncio.run(race())
    assert sorted(results) == [False, True]
    assert store.get(run.repo_name, run.task_id).state is RecoveryOutcome.REMINDER_RESERVED
