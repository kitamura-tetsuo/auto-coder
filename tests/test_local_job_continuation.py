from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import (
    ContinuationIntentState,
    ContinuationState,
    InvocationOutcome,
    LocalJobKind,
    LocalJobOffer,
    LocalJobStore,
)


def _running_job(tmp_path: Path):
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    identity = IssueAttemptIdentity("owner", "repo", 42, "attempt-1")
    guard.reserve(identity, CandidateHandoff("codex", "local"))
    offer = LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, "owner/repo", 42, "attempt-1", "codex", "implement")
    assert store.offer_issue(offer, identity, guard) is not None
    claim = store.claim(offer.job_id)
    assert claim is not None and claim.acquired
    assert store.bind_workspace(
        claim,
        workspace_path=tmp_path.resolve(),
        source_commit="a" * 40,
        source_ref="main",
        work_branch="work",
        publication_remote="origin",
        owner_incarnation="owner",
        owner_generation=None,
    )
    return store, offer, claim


def test_waiting_continuation_survives_reopen_without_completing_job(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    continuation = store.bind_continuation(claim, "session-1", "executor-1")
    assert continuation is not None
    task = store.observe_continuation_task(offer.job_id, "executor-1", "task-1", "running", provider_position="event-1")
    assert task is not None and task.sequence == 1
    assert store.transition_continuation(
        offer.job_id,
        "executor-1",
        ContinuationState.FOREGROUND,
        ContinuationState.WAITING_BACKGROUND,
    )

    reopened = LocalJobStore(store.path)
    retained = reopened.get_continuation(offer.job_id)
    assert retained is not None
    assert retained.provider_session == "session-1"
    assert retained.state is ContinuationState.WAITING_BACKGROUND
    assert reopened.get(offer.job_id) == store.get(offer.job_id)
    assert reopened.claim(offer.job_id) is not None
    assert not reopened.claim(offer.job_id).acquired  # type: ignore[union-attr]
    assert reopened.discover_pending_continuations() == (retained,)


def test_task_and_final_frontier_are_monotonic_and_executor_fenced(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "old") is not None
    running = store.observe_continuation_task(offer.job_id, "old", "task-1", "running")
    assert running is not None
    assert store.record_candidate_output(offer.job_id, "old", running.sequence, "too early")
    newer = store.observe_continuation_task(offer.job_id, "old", "task-2", "running")
    assert newer is not None
    assert not store.confirm_final_output(offer.job_id, "old", running.sequence)
    terminal = store.observe_continuation_task(offer.job_id, "old", "task-2", "terminal", terminal_outcome="completed", result_reference="result-2")
    assert terminal is not None
    assert store.observe_continuation_task(offer.job_id, "old", "task-2", "running") == terminal
    assert store.take_over_continuation(offer.job_id, "old", "new", expected_runner_owner=claim.record.runner_owner)
    assert not store.transition_continuation(offer.job_id, "old", ContinuationState.RECONCILIATION_REQUIRED, ContinuationState.CONTINUATION_PENDING)


def test_continuation_intent_replay_and_ambiguous_submission_are_durable(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "executor") is not None
    task = store.observe_continuation_task(offer.job_id, "executor", "task-1", "terminal", terminal_outcome="completed", result_reference="result-1")
    assert task is not None
    first = store.create_continuation_intent(offer.job_id, "executor", task.sequence, "continue with result-1")
    replay = store.create_continuation_intent(offer.job_id, "executor", task.sequence, "continue with result-1")
    assert replay == first
    assert first is not None
    assert store.transition_continuation_intent(
        first.intent_id,
        "executor",
        ContinuationIntentState.NOT_SUBMITTED,
        ContinuationIntentState.INDETERMINATE,
        provider_turn="turn-2",
    )
    assert LocalJobStore(store.path).create_continuation_intent(offer.job_id, "executor", task.sequence, "continue with result-1").intent_id == first.intent_id  # type: ignore[union-attr]
    assert store.create_continuation_intent(offer.job_id, "executor", task.sequence, "different payload") is None


def test_legacy_database_has_no_synthetic_continuation(tmp_path: Path) -> None:
    store, offer, _claim = _running_job(tmp_path)
    reopened = LocalJobStore(store.path)
    assert reopened.get_continuation(offer.job_id) is None
    assert reopened.discover_pending_continuations() == ()


def test_executor_takeover_contention_has_one_owner_and_fences_stale_writer(tmp_path: Path) -> None:
    """REQ-008: both contenders arrive before the one-winner CAS is released."""
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "original") is not None
    reached_transition = Barrier(3)

    def contend(new_executor: str) -> bool:
        contender = LocalJobStore(store.path)
        reached_transition.wait(timeout=10)
        return contender.take_over_continuation(offer.job_id, "original", new_executor, expected_runner_owner=claim.record.runner_owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(contend, executor) for executor in ("executor-a", "executor-b")]
        # The controller is the third participant, so this cannot release until
        # both workers have reached the contested production transition.
        reached_transition.wait(timeout=10)
        outcomes = [future.result(timeout=10) for future in futures]

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1
    retained = LocalJobStore(store.path).get_continuation(offer.job_id)
    assert retained is not None
    winner = retained.executor_token
    loser = "executor-b" if winner == "executor-a" else "executor-a"
    assert winner in {"executor-a", "executor-b"}
    assert retained.state is ContinuationState.RECONCILIATION_REQUIRED
    assert not store.observe_continuation_task(offer.job_id, "original", "stale-original", "running")
    assert not store.observe_continuation_task(offer.job_id, loser, "stale-loser", "running")
    assert store.transition_continuation(
        offer.job_id,
        winner,
        ContinuationState.RECONCILIATION_REQUIRED,
        ContinuationState.CONTINUATION_PENDING,
    )


class _FailBeforeCommitConnection:
    """Delegate to a real SQLite connection but reject its commit boundary."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exception_type, exception, traceback):
        return self._connection.__exit__(exception_type, exception, traceback)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def commit(self) -> None:
        raise sqlite3.OperationalError("injected failure before commit")


def test_failed_intent_commit_returns_no_authority_and_preserves_prior_evidence(tmp_path: Path, monkeypatch) -> None:
    """REQ-008: failed persistence cannot acknowledge or expose a command."""
    store, offer, claim = _running_job(tmp_path)
    bound = store.bind_continuation(claim, "session-1", "executor")
    assert bound is not None
    task = store.observe_continuation_task(
        offer.job_id,
        "executor",
        "task-1",
        "terminal",
        terminal_outcome="completed",
        result_reference="result-1",
    )
    assert task is not None

    failing_store = LocalJobStore(store.path)
    real_connect = failing_store._connect
    monkeypatch.setattr(failing_store, "_connect", lambda: _FailBeforeCommitConnection(real_connect()))

    assert (
        failing_store.create_continuation_intent(
            offer.job_id,
            "executor",
            task.sequence,
            "continue with result-1",
        )
        is None
    )

    reopened = LocalJobStore(store.path)
    assert reopened.get_continuation(offer.job_id) == store.get_continuation(offer.job_id)
    # A successful creation after reopen proves the failed transaction left no
    # hidden executable intent or acknowledgement occupying this causal batch.
    intent = reopened.create_continuation_intent(
        offer.job_id,
        "executor",
        task.sequence,
        "continue with result-1",
    )
    assert intent is not None
    assert intent.state is ContinuationIntentState.NOT_SUBMITTED


def test_waiting_takeover_blocks_stale_completion_and_downstream_eligibility(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "executor-a") is not None
    assert store.observe_continuation_task(offer.job_id, "executor-a", "background", "running") is not None
    assert store.transition_continuation(
        offer.job_id,
        "executor-a",
        ContinuationState.FOREGROUND,
        ContinuationState.WAITING_BACKGROUND,
    )
    assert store.take_over_continuation(offer.job_id, "executor-a", "executor-b", expected_runner_owner=claim.record.runner_owner)

    assert store.persist_result_artifact(claim, InvocationOutcome.COMPLETED, "foreground pause", executor_token="executor-a") is None
    assert not store.record_result(claim, InvocationOutcome.COMPLETED, "missing", executor_token="executor-a")
    assert not store.mark_downstream_pending(claim, executor_token="executor-a")

    reopened = LocalJobStore(store.path)
    job = reopened.get(offer.job_id)
    continuation = reopened.get_continuation(offer.job_id)
    assert job is not None
    assert job.invocation_outcome is None
    assert job.result_reference == ""
    assert continuation is not None
    assert continuation.state is ContinuationState.RECONCILIATION_REQUIRED
    assert continuation.executor_token == "executor-b"
    assert reopened.discover_pending_continuations() == (continuation,)


def test_replaced_job_lineage_rejects_every_old_continuation_authority(tmp_path: Path) -> None:
    store, offer, first_claim = _running_job(tmp_path)
    assert store.bind_continuation(first_claim, "session-1", "executor-old") is not None
    task = store.observe_continuation_task(
        offer.job_id,
        "executor-old",
        "task-1",
        "terminal",
        terminal_outcome="completed",
        result_reference="result-1",
    )
    assert task is not None
    assert store.record_candidate_output(offer.job_id, "executor-old", task.sequence, "candidate")
    intent = store.create_continuation_intent(offer.job_id, "executor-old", task.sequence, "continue")
    assert intent is not None
    assert store.release_unentered_claim(first_claim, "pre-entry retry")
    second_claim = store.claim(offer.job_id)
    assert second_claim is not None and second_claim.acquired
    assert second_claim.record.execution_incarnation != first_claim.record.execution_incarnation

    reopened = LocalJobStore(store.path)
    assert reopened.observe_continuation_task(offer.job_id, "executor-old", "stale", "running") is None
    assert reopened.create_continuation_intent(offer.job_id, "executor-old", task.sequence, "continue") is None
    assert not reopened.record_candidate_output(offer.job_id, "executor-old", task.sequence, "replacement")
    assert not reopened.confirm_final_output(offer.job_id, "executor-old", task.sequence)
    assert not reopened.transition_continuation(
        offer.job_id,
        "executor-old",
        ContinuationState.FOREGROUND,
        ContinuationState.WAITING_BACKGROUND,
    )
    assert not reopened.transition_continuation_intent(
        intent.intent_id,
        "executor-old",
        ContinuationIntentState.NOT_SUBMITTED,
        ContinuationIntentState.ADMITTED,
    )
    assert not reopened.take_over_continuation(offer.job_id, "executor-old", "executor-new", expected_runner_owner=first_claim.record.runner_owner)


@pytest.mark.parametrize("command_kind", ["controller", "native"])
def test_admitted_continued_turn_invalidates_older_candidate_after_reopen(tmp_path: Path, command_kind: str) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "executor") is not None
    task = store.observe_continuation_task(
        offer.job_id,
        "executor",
        "task-1",
        "terminal",
        terminal_outcome="completed",
        result_reference="result-1",
        provider_position="1",
    )
    assert task is not None
    assert store.record_candidate_output(offer.job_id, "executor", task.sequence, "turn-1 output")
    intent = store.create_continuation_intent(
        offer.job_id,
        "executor",
        task.sequence,
        "continue",
        command_kind=command_kind,
    )
    assert intent is not None
    assert store.transition_continuation_intent(
        intent.intent_id,
        "executor",
        ContinuationIntentState.NOT_SUBMITTED,
        ContinuationIntentState.ADMITTED,
        provider_turn="turn-2",
    )

    reopened = LocalJobStore(store.path)
    advanced = reopened.get_continuation(offer.job_id)
    assert advanced is not None
    assert advanced.frontier == task.sequence + 1
    assert advanced.provider_position == "turn-2"
    assert not reopened.confirm_final_output(offer.job_id, "executor", task.sequence)
    assert reopened.record_candidate_output(offer.job_id, "executor", advanced.frontier, "turn-2 output")
    assert reopened.confirm_final_output(offer.job_id, "executor", advanced.frontier)
    assert reopened.get_continuation(offer.job_id).final_output == "turn-2 output"  # type: ignore[union-attr]
    artifact = reopened.persist_result_artifact(
        claim,
        InvocationOutcome.COMPLETED,
        "turn-2 output",
        executor_token="executor",
    )
    assert artifact is not None
    assert reopened.record_result(
        claim,
        InvocationOutcome.COMPLETED,
        artifact.artifact_id,
        executor_token="executor",
    )
    assert reopened.mark_downstream_pending(claim, executor_token="executor")


def test_generic_transition_cannot_bypass_final_confirmation(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "executor") is not None
    first = store.observe_continuation_task(offer.job_id, "executor", "task-1", "terminal", terminal_outcome="completed", result_reference="result-1")
    assert first is not None
    assert store.record_candidate_output(offer.job_id, "executor", first.sequence, "obsolete")
    assert store.observe_continuation_task(offer.job_id, "executor", "task-2", "running") is not None
    assert store.transition_continuation(
        offer.job_id,
        "executor",
        ContinuationState.FOREGROUND,
        ContinuationState.WAITING_BACKGROUND,
    )
    assert not store.transition_continuation(
        offer.job_id,
        "executor",
        ContinuationState.WAITING_BACKGROUND,
        ContinuationState.FINAL_CONFIRMED,
    )

    reopened = LocalJobStore(store.path)
    retained = reopened.get_continuation(offer.job_id)
    assert retained is not None
    assert retained.state is ContinuationState.WAITING_BACKGROUND
    assert retained.final_output == ""
    assert reopened.discover_pending_continuations() == (retained,)


def test_older_terminal_observation_cannot_replace_newer_provider_evidence(tmp_path: Path) -> None:
    store, offer, claim = _running_job(tmp_path)
    assert store.bind_continuation(claim, "session-1", "executor") is not None
    newest = store.observe_continuation_task(
        offer.job_id,
        "executor",
        "task-1",
        "terminal",
        terminal_outcome="completed",
        result_reference="result-20",
        provider_position="20",
    )
    assert newest is not None
    replay = store.observe_continuation_task(
        offer.job_id,
        "executor",
        "task-1",
        "terminal",
        terminal_outcome="failed",
        result_reference="result-10",
        provider_position="10",
    )
    assert replay == newest

    reopened = LocalJobStore(store.path)
    continuation = reopened.get_continuation(offer.job_id)
    assert continuation is not None
    assert continuation.frontier == newest.sequence
    assert continuation.provider_position == "20"
    assert reopened.create_continuation_intent(offer.job_id, "executor", newest.sequence + 1, "obsolete batch") is None
    intent = reopened.create_continuation_intent(offer.job_id, "executor", newest.sequence, "accepted batch")
    assert intent is not None
    assert intent.causal_frontier == newest.sequence
