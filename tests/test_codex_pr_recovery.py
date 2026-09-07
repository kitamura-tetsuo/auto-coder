"""Production-oriented regressions for bounded Codex initial-PR recovery."""

import asyncio
from pathlib import Path

from auto_coder.cloud_run import CloudRun, CloudRunRepository
from auto_coder.cloud_task_client_base import CloudTaskState
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

TASK = "task_e_6a26c19ac8a88326af83ebfb44b89fe2"


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

    monitor = CodexPRRecoveryMonitor(runs, observations, wham, store, enqueue, now=clock, poll_interval=60, grace_period=120)
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
    restarted = CodexPRRecoveryMonitor(monitor.runs, observations, wham, store, monitor.enqueue_pr, now=clock, poll_interval=60, grace_period=120)
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
