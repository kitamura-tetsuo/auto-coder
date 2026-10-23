"""Explicit retries replace stopped local jobs without losing retained evidence."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.automation_config import ExplicitTargetOutcome
from auto_coder.execution_trace import Outcome, TraceCollector, get_trace_collector
from auto_coder.implementation_slots import ImplementationOwner
from auto_coder.issue_dispatch import CandidateHandoff, IssueAttemptIdentity, IssueDispatchGuard
from auto_coder.issue_retry_admission import IssueRetryAdmissionService, IssueRetryDisposition, IssueRetryOrigin
from auto_coder.local_job_handoff import ContinuationState, InvocationOutcome, LocalJobKind, LocalJobOffer, LocalJobState, LocalJobStore
from auto_coder.local_job_runner import current_runner_owner, process_owner, runner_owner_alive, writer_group_settled

from .test_dashboard_observability import _assert_required_stage_visible, _mounted_detail
from .test_implementation_slots import execution_process
from .test_specification_validation_lifecycle import GitHubFlow, engine_with_gate, lifecycle, snapshot

REPOSITORY = "owner/repo"
NUMBER = 1728
OWNER = ImplementationOwner("issue", NUMBER)


@pytest.fixture
def stopped_issue(tmp_path, _use_real_commands):
    TraceCollector._instance = None
    # Use actual OS liveness evidence rather than assuming a missing PID is dead.
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        dead_owner = process_owner(process.pid)
    finally:
        process.kill()
        process.wait(timeout=5)
    assert runner_owner_alive(dead_owner) is False
    assert writer_group_settled(dead_owner)

    engine, candidate = engine_with_gate(tmp_path, GitHubFlow([snapshot()]), lifecycle(tmp_path, "READY"))
    slots = engine.implementation_slots
    generation = engine._compute_implementation_generation(REPOSITORY, snapshot(), None)
    execution = slots.start_execution(OWNER, generation=generation)
    assert execution is not None
    engine.issue_stage_routing.record_implementation_owned(REPOSITORY, NUMBER, generation)
    store = LocalJobStore(tmp_path / "jobs.sqlite3")
    engine.local_job_store = store
    identity = IssueAttemptIdentity("owner", "repo", NUMBER, "previous-attempt")
    guard = IssueDispatchGuard(tmp_path / "dispatch.sqlite3")
    assert guard.reserve(identity, CandidateHandoff("muse", "local")).admitted
    offered = store.offer_issue(LocalJobOffer(LocalJobKind.ISSUE_IMPLEMENTATION, REPOSITORY, NUMBER, identity.implementation_attempt_id, "muse", "Implement the Issue", execution), identity, guard)
    assert offered is not None
    claim = store.claim(offered.job_id, runner_owner=dead_owner)
    assert claim is not None and claim.acquired
    assert store.mark_provider_entered(claim)
    workspace = tmp_path / "retained-workspace"
    workspace.mkdir()
    (workspace / "partial.txt").write_text("retained edits\n")
    assert store.bind_workspace(claim, workspace_path=workspace, source_commit="a" * 40, source_ref="main", work_branch="issue-1728", publication_remote="origin", owner_incarnation=slots.owner_incarnation(OWNER), owner_generation=generation)
    assert store.bind_continuation(claim, "previous-session", dead_owner) is not None
    assert store.bind_continuation_writer(offered.job_id, dead_owner, workspace, dead_owner)
    return engine, candidate, store, claim, generation, workspace


def _retry(engine, candidate, request_id="operator-retry"):
    return engine._process_single_candidate_unified(REPOSITORY, candidate, engine.config, explicit_only=True, force=True, retry=True, retry_request_id=request_id, origin="explicit-single-target")


@pytest.mark.parametrize("paused", [False, True])
@patch("auto_coder.dashboard.ui")
def test_stopped_predecessor_retry_reaches_new_attempt_and_mounted_detail(mock_ui, stopped_issue, paused):
    engine, candidate, store, claim, generation, workspace = stopped_issue
    old = store.get(claim.record.job_id)
    continuation = store.get_continuation(old.job_id)
    if paused:
        assert store.pause_continuation(old, continuation.executor_token, "interrupted")

    result = _retry(engine, candidate)

    assert result.success is True
    engine._process_single_candidate_reserved.assert_called_once()
    authority = engine._process_single_candidate_reserved.call_args.kwargs["retry_authority"]
    assert authority.request_id == "operator-retry"
    assert authority.attempt_id != old.upstream_attempt
    assert authority.generation == generation
    assert authority.status == "owned"
    assert authority.ownership_reference != old.implementation_execution_id
    retained = LocalJobStore(store.path).get(old.job_id)
    assert retained.state is LocalJobState.SETTLED
    assert retained.invocation_outcome is None
    assert retained.result_reference == ""
    assert store.claim(old.job_id).acquired is False
    assert retained.workspace_path == old.workspace_path
    assert (workspace / "partial.txt").read_text() == "retained edits\n"
    after = store.get_continuation(old.job_id)
    assert after.provider_session == continuation.provider_session
    assert after.state is ContinuationState.INTERRUPTED
    assert after.executor_token != continuation.executor_token
    assert store.persist_result_artifact(claim, InvocationOutcome.FAILED, "late result", executor_token=continuation.executor_token) is None
    assert engine.implementation_slots.active_execution_ids(OWNER) == ()
    assert engine.issue_stage_routing.is_implementation_owned(REPOSITORY, NUMBER, generation)

    events = get_trace_collector().get_snapshot(repository=REPOSITORY, item_type="issue", item_number=NUMBER).events
    retirement = next(event for event in events if event.stage_id == "issue.retry-predecessor-retirement")
    authorization = next(event for event in events if event.stage_id == "issue.manual-retry")
    assert retirement.outcome == Outcome.SUPERSEDED.value
    assert retirement.execution_id == authorization.execution_id
    assert retirement.sequence < authorization.sequence
    assert retirement.facts == {"request_id": authority.request_id, "job_id": old.job_id, "execution_id": old.implementation_execution_id, "reason": "explicit retry after runner and writer settlement"}
    _mounted_detail(mock_ui, "issue", NUMBER)
    older = next(call.kwargs["on_click"] for call in mock_ui.button.call_args_list if call.kwargs.get("icon") == "arrow_downward")
    older()
    diagram = mock_ui.mermaid.return_value.classes.return_value.set_content.call_args[0][0]
    _assert_required_stage_visible(diagram, "stopped retry predecessor")
    assert "outcome: superseded" in diagram


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("protection", ["runner_live", "runner_unknown", "writer_live", "writer_unknown", "artifact", "effect", "final", "missing_continuation", "owner", "generation", "readiness"])
def test_retry_preserves_live_unknown_completed_and_unsubmitted_work(stopped_issue, monkeypatch, retry, protection):
    engine, candidate, store, claim, generation, _workspace = stopped_issue
    old = store.get(claim.record.job_id)
    continuation = store.get_continuation(old.job_id)
    if protection.startswith("runner_"):
        monkeypatch.setattr("auto_coder.issue_retry_admission.runner_owner_alive", lambda _: True if protection == "runner_live" else None)
    elif protection.startswith("writer_"):
        writer = current_runner_owner() if protection == "writer_live" else "unknown-owner"
        assert store.bind_continuation_writer(old.job_id, continuation.executor_token, Path(old.workspace_path), writer)
    elif protection == "artifact":
        assert store.persist_result_artifact(claim, InvocationOutcome.FAILED, "actual failure", executor_token=continuation.executor_token) is not None
    elif protection == "effect":
        artifact = store.persist_result_artifact(claim, InvocationOutcome.FAILED, "actual failure", executor_token=continuation.executor_token)
        assert artifact is not None
        assert store.record_result(claim, InvocationOutcome.FAILED, artifact.artifact_id, executor_token=continuation.executor_token)
        assert store.record_effect(claim, "publication", "pending")
        old = store.get(old.job_id)
    elif protection == "final":
        assert store.record_candidate_output(old.job_id, continuation.executor_token, continuation.frontier, "ACTION_SUMMARY: actual result")
        assert store.confirm_final_output(old.job_id, continuation.executor_token, continuation.frontier)
    elif protection == "missing_continuation":
        monkeypatch.setattr(store, "get_continuation", lambda _: None)
    elif protection in {"owner", "generation"}:
        with store._connect() as connection:
            connection.execute(
                "UPDATE local_jobs SET owner_incarnation=?, owner_generation=? WHERE job_id=?",
                ("wrong-owner" if protection == "owner" else old.owner_incarnation, "wrong-generation" if protection == "generation" else generation, old.job_id),
            )
        old = store.get(old.job_id)
    else:
        engine.github = GitHubFlow([snapshot(ready=False)])

    result = engine._process_single_candidate_unified(REPOSITORY, candidate, engine.config, explicit_only=True, force=True, retry=retry, retry_request_id="refused-retry" if retry else None)

    assert result.success is False
    assert result.target_outcome is (ExplicitTargetOutcome.SKIPPED if protection == "readiness" and retry else ExplicitTargetOutcome.DEFERRED)
    engine._process_single_candidate_reserved.assert_not_called()
    assert store.get(old.job_id) == old
    assert engine.implementation_slots.active_execution_ids(OWNER) == (old.implementation_execution_id,)
    assert not any(event.stage_id == "issue.retry-predecessor-retirement" for event in get_trace_collector().get_snapshot(repository=REPOSITORY, item_type="issue", item_number=NUMBER).events)
    assert engine.issue_stage_routing.retry_requests(REPOSITORY, NUMBER) == ()


@pytest.mark.parametrize("change", ["resume", "result", "writer"])
def test_concurrent_checkpoint_wins_over_stopped_predecessor_snapshot(stopped_issue, monkeypatch, change):
    engine, candidate, store, claim, _generation, _workspace = stopped_issue
    old = store.get(claim.record.job_id)
    continuation = store.get_continuation(old.job_id)
    supersede = store.supersede_stopped_issue_for_retry

    def resumed(job, observed, request_id):
        if change == "resume":
            assert store.claim_explicit_resume(old, continuation, current_runner_owner(), "resumed-executor")
        elif change == "result":
            assert store.persist_result_artifact(claim, InvocationOutcome.FAILED, "actual result", executor_token=continuation.executor_token) is not None
        else:
            assert store.bind_continuation_writer(old.job_id, continuation.executor_token, Path(old.workspace_path), current_runner_owner())
        return supersede(job, observed, request_id)

    monkeypatch.setattr(store, "supersede_stopped_issue_for_retry", resumed)
    result = _retry(engine, candidate)

    assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
    engine._process_single_candidate_reserved.assert_not_called()
    assert store.get(old.job_id).state is LocalJobState.RUNNING
    assert store.get_continuation(old.job_id).executor_token == ("resumed-executor" if change == "resume" else continuation.executor_token)
    assert engine.implementation_slots.active_execution_ids(OWNER) == (old.implementation_execution_id,)


def test_reopened_retry_completes_crash_interrupted_retirement(stopped_issue, monkeypatch):
    engine, _candidate, store, claim, generation, _workspace = stopped_issue
    slots = engine.implementation_slots
    service = IssueRetryAdmissionService(engine.issue_stage_routing, slots, store)
    finish = slots.finish_execution

    def unavailable(*_args):
        raise OSError("controller stopped before slot projection")

    monkeypatch.setattr(slots, "finish_execution", unavailable)
    first = service.admit(REPOSITORY, NUMBER, generation, "crash-retry", IssueRetryOrigin.MANUAL)
    assert first.disposition is IssueRetryDisposition.FAILED
    assert store.get(claim.record.job_id).state is LocalJobState.SETTLED
    assert slots.active_execution_ids(OWNER) == (claim.record.implementation_execution_id,)
    monkeypatch.setattr(slots, "finish_execution", finish)
    reopened = IssueRetryAdmissionService(engine.issue_stage_routing, slots, LocalJobStore(store.path))
    recovered = reopened.admit(REPOSITORY, NUMBER, generation, "crash-retry", IssueRetryOrigin.MANUAL)
    assert recovered.disposition is IssueRetryDisposition.OWNED
    assert recovered.authority.attempt_id == first.authority.attempt_id
    assert slots.active_execution_ids(OWNER) == (recovered.authority.ownership_reference,)


def test_retry_without_transferred_job_reclaims_dead_execution(tmp_path, _use_real_commands):
    engine, candidate = engine_with_gate(tmp_path, GitHubFlow([snapshot()]), lifecycle(tmp_path, "READY"))
    slots = engine.implementation_slots
    process, execution = execution_process(slots.storage_path, owner_number=NUMBER, lifetime=0)
    process.wait(timeout=5)
    generation = engine._compute_implementation_generation(REPOSITORY, snapshot(), None)
    engine.issue_stage_routing.record_implementation_owned(REPOSITORY, NUMBER, generation)

    result = _retry(engine, candidate)

    assert result.success is True
    engine._process_single_candidate_reserved.assert_called_once()
    authority = engine._process_single_candidate_reserved.call_args.kwargs["retry_authority"]
    assert authority.ownership_reference != execution
    assert slots.active_execution_ids(OWNER) == ()


def test_unreadable_predecessor_evidence_preserves_execution(stopped_issue, monkeypatch):
    engine, _candidate, store, claim, generation, _workspace = stopped_issue
    old = store.get(claim.record.job_id)

    def unavailable(*_args):
        raise RuntimeError("local job storage unavailable")

    monkeypatch.setattr(store, "issue_job_evidence", unavailable)
    result = IssueRetryAdmissionService(engine.issue_stage_routing, engine.implementation_slots, store).admit(REPOSITORY, NUMBER, generation, "unreadable-retry", IssueRetryOrigin.MANUAL)

    assert result.disposition is IssueRetryDisposition.FAILED
    assert result.reason == "local job storage unavailable"
    assert engine.issue_stage_routing.retry_requests(REPOSITORY, NUMBER) == ()
    assert store.get(old.job_id) == old
    assert engine.implementation_slots.active_execution_ids(OWNER) == (old.implementation_execution_id,)
