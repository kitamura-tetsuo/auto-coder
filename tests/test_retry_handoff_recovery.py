from pathlib import Path

from auto_coder.cloud_manager import CloudManager, CloudTaskBinding
from auto_coder.cloud_run import CloudRunRepository
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_stage_routing import IssueStageRoutingStore
from auto_coder.retry_dispatch import RetryDispatchRepository
from auto_coder.retry_handoff_recovery import (
    RetryHandoffDisposition,
    discover_unfinished_codex_handoffs,
    settle_codex_retry_handoff,
)

REPOSITORY = "owner/repo"
ISSUE = 2224


def _accepted_receipt(request_id: str = "request-1", task_id: str = "task-1") -> RetryDispatchRepository:
    routing = IssueStageRoutingStore(Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3")
    routing.accept_retry_request(request_id, REPOSITORY, ISSUE, "generation-1")
    routing.capture_retry_predecessor(request_id, None, None, None)
    authority = routing.mark_retry_owned(request_id, f"execution-{request_id}")
    dispatch = RetryDispatchRepository(REPOSITORY)
    dispatch.claim(authority, "codex-cloud", "codex-alias", {"base_branch": "main"})
    dispatch.allocate_numeric_attempt(request_id, [0])
    dispatch.record_outcome(request_id, "accepted", external_id=task_id, environment_id="environment-original")
    return dispatch


def _slots() -> ImplementationSlotRepository:
    slots = ImplementationSlotRepository(REPOSITORY, max_implementations=3)
    assert slots.reserve_new(ImplementationOwner("issue", ISSUE))
    return slots


def test_accepted_receipt_finishes_run_pointer_slot_and_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    dispatch = _accepted_receipt()

    result = settle_codex_retry_handoff(REPOSITORY, "request-1", _slots())

    assert result.disposition is RetryHandoffDisposition.SUCCESS
    assert result.phase == "complete"
    assert "numeric_attempt=1 task=task-1" in result.diagnostic
    assert CloudManager(REPOSITORY).read_bindings_strict()[str(ISSUE)] == CloudTaskBinding("codex-cloud", "task-1", "codex-alias")
    run = CloudRunRepository(REPOSITORY).get(ISSUE, 1)
    assert run is not None
    assert (run.task_id, run.environment_id, run.base_branch) == ("task-1", "environment-original", "main")
    retained = dispatch.get("request-1")
    assert retained is not None
    assert (retained.tracking_complete, retained.projection_disposition) == (True, "accepted-current")


def test_legacy_provider_complete_receipt_with_missing_slot_is_discovered(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    dispatch = _accepted_receipt()
    dispatch.mark_tracking_complete("request-1")
    slots = ImplementationSlotRepository(REPOSITORY, max_implementations=3)

    deferred = discover_unfinished_codex_handoffs(REPOSITORY, slots)

    assert len(deferred) == 1
    assert deferred[0].disposition is RetryHandoffDisposition.DEFERRED
    assert deferred[0].reason == "enclosing implementation slot is unavailable"
    retained = dispatch.get("request-1")
    assert retained is not None
    assert retained.projection_disposition == "accepted-tracking-incomplete"

    assert slots.reserve_new(ImplementationOwner("issue", ISSUE))
    assert discover_unfinished_codex_handoffs(REPOSITORY, slots) == ()
    assert dispatch.get("request-1").tracking_complete is True


def test_older_replay_is_historical_after_later_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _accepted_receipt("request-1", "task-1")
    _accepted_receipt("request-2", "task-2")
    slots = _slots()

    older = settle_codex_retry_handoff(REPOSITORY, "request-1", slots)
    newer = settle_codex_retry_handoff(REPOSITORY, "request-2", slots)

    assert older.disposition is RetryHandoffDisposition.SKIPPED
    assert older.phase == "historical"
    assert newer.disposition is RetryHandoffDisposition.SUCCESS
    assert CloudManager(REPOSITORY).read_bindings_strict()[str(ISSUE)].task_id == "task-2"
