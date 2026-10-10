"""Terminal PR resumptions retire stale work before implementation admission."""

import asyncio
from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import PR_PROCESSING_STAGE, AutomationEngine, _MergeOperationResumeHandler, _PrProcessingStageHandler
from auto_coder.execution_trace import EventKind, Outcome, TraceCollector
from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.merge_operation_scheduler import MergeOperationScheduler
from auto_coder.merge_operation_state import EffectName, MergeOperationIdentity, MergeOperationStore, OperationStatus
from tests.test_dashboard_observability import _mounted_detail


@pytest.mark.parametrize("handler_kind", ["merge", "pending"])
@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("changed_head", [False, True])
@pytest.mark.parametrize("live_execution", [False, True])
def test_terminal_resumption_never_readmits_implementation(tmp_path, monkeypatch, handler_kind, merged, changed_head, live_execution):
    repository = "owner/repo"
    number = 5476
    github = Mock()
    pr = {"number": number, "state": "closed", "merged": merged, "head": {"sha": "new-head" if changed_head else "head"}, "body": ""}
    github.get_pull_request_metadata_strict.return_value = pr
    github.get_pull_request.return_value = pr
    github.get_pr_details.side_effect = lambda raw: raw
    github.get_issue.return_value = None
    monkeypatch.setattr(TraceCollector, "_instance", None)
    collector = TraceCollector()
    store = MergeOperationStore(tmp_path / "merge.db")
    monkeypatch.setattr("auto_coder.merge_operation_state.get_merge_operation_store", lambda: store)
    engine = AutomationEngine(github, AutomationConfig())
    slots = ImplementationSlotRepository(repository, 1, tmp_path / "slots.json")
    engine.implementation_slots = slots
    owner = ImplementationOwner("pr", number)
    assert slots.reserve(owner)
    execution = slots.start_execution(owner) if live_execution else None
    if live_execution:
        assert execution is not None
    engine._process_single_candidate = Mock(side_effect=AssertionError("Terminal PR must not enter implementation dispatch"))
    engine.invalidations = Mock()
    identity = MergeOperationIdentity("https://api.github.com", repository, number)
    operation = store.get_or_create(identity, expected_head_sha="head", merge_method="squash", approval_credential_role="role", reviewer_identity="", needs_approval=False, now=100.0)

    if handler_kind == "merge":
        scheduler = MergeOperationScheduler(store, clock=lambda: 100.0)
        scheduler.register_resume_handler(_MergeOperationResumeHandler(engine, repository), repository=repository)

        async def run_due():
            await scheduler._dispatch_due()
            if scheduler._tasks:
                await asyncio.gather(*list(scheduler._tasks))

        asyncio.run(run_due())
        retained = store.get(identity)
        assert retained.status is OperationStatus.SUPERSEDED
        assert retained.effects == operation.effects
        assert retained.effect(EffectName.MERGE).receipt == operation.effect(EffectName.MERGE).receipt
        assert store.due(10000.0, repository=repository) == []
        stage = "pr.merge-operation-resume-refresh"
    else:
        obligation = PendingObligation(WorkIdentity(repository, f"pr:{number}", PR_PROCESSING_STAGE, "head"), PendingReason.ADMISSION_DEFERRED, 100.0, (PR_PROCESSING_STAGE,))
        outcome = _PrProcessingStageHandler(engine, repository).dispatch(obligation)
        assert outcome.superseded is True
        assert outcome.error is None
        stage = "pr.pending-work-resume-refresh"

    engine._process_single_candidate.assert_not_called()
    engine.invalidations.retire_ci_watches.assert_called_once_with(repository, number)
    assert slots.active_owners() == ((owner,) if live_execution else ())
    if live_execution:
        assert slots.active_execution_ids(owner) == (execution,)
        slots.finish_execution(owner, execution)
        slots.reconcile(github)
        assert slots.active_owners() == ()
    events = collector.get_snapshot(repository=repository).events
    refresh = [event for event in events if event.stage_id == stage]
    assert len(refresh) == 1
    assert refresh[0].outcome == Outcome.SUPERSEDED.value
    assert refresh[0].facts["reason"] == "PR is terminal"
    finished = [event for event in events if event.kind == EventKind.EXECUTION_FINISHED.value]
    assert len(finished) == 1
    assert finished[0].outcome == Outcome.SUPERSEDED.value
    assert not any(event.stage_id == "pr.implementation-admission" for event in events)
    with patch("auto_coder.dashboard.ui") as ui:
        diagram = _mounted_detail(ui, "pr", number)
    assert "resume refresh" in diagram
    assert "outcome: superseded" in diagram
    assert "merge delivery" not in diagram
    github.close_pr.assert_not_called()
    github.merge_pr.assert_not_called()
