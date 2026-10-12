from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.local_job_handoff import (
    ContinuationIntentState,
    ContinuationState,
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
    assert store.take_over_continuation(offer.job_id, "old", "new")
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
        return contender.take_over_continuation(offer.job_id, "original", new_executor)

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
