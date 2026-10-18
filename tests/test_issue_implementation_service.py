from pathlib import Path

from auto_coder.automation_config import CandidateProcessingResult
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_dispatch import DispatchOutcome, DispatchResult, IssueAttemptIdentity
from auto_coder.issue_implementation_service import IssueImplementationPorts, IssueImplementationRequest, IssueImplementationService
from auto_coder.issue_stage_routing import IssueStageRoutingStore

REPOSITORY = "owner/repo"
OWNER = ImplementationOwner("issue", 7)
GENERATION = "generation-7"


def _stores(tmp_path: Path) -> tuple[ImplementationSlotRepository, IssueStageRoutingStore]:
    return (
        ImplementationSlotRepository(REPOSITORY, 2, tmp_path / "slots.json"),
        IssueStageRoutingStore(tmp_path / "routing.sqlite3"),
    )


def _result(outcome: DispatchOutcome, *, tracking_complete: bool = True) -> CandidateProcessingResult:
    result = CandidateProcessingResult(type="issue", number=7, success=True)
    result.dispatch_result = DispatchResult(
        IssueAttemptIdentity("owner", "repo", 7, "attempt-7"),
        outcome,
        provider="controlled",
        provider_reference="provider-7",
        tracking_complete=tracking_complete,
    )
    return result


def _finish_spy(slots: ImplementationSlotRepository) -> tuple[list[str], object]:
    calls: list[str] = []
    finish = slots.finish_execution

    def observed(owner: ImplementationOwner, execution_id: str) -> bool:
        assert owner == OWNER
        calls.append(execution_id)
        return finish(owner, execution_id)

    slots.finish_execution = observed  # type: ignore[method-assign]
    return calls, finish


def test_completed_service_dispatch_finishes_only_its_exact_execution(tmp_path: Path) -> None:
    slots, routing = _stores(tmp_path)
    calls, _ = _finish_spy(slots)
    entered: list[str] = []

    def dispatch(_authority):  # type: ignore[no-untyped-def]
        execution_id = slots.current_execution_id(OWNER)
        assert execution_id is not None
        entered.append(execution_id)
        return _result(DispatchOutcome.LOCAL_COMPLETED)

    outcome = IssueImplementationService(routing, slots).execute(
        IssueImplementationRequest(REPOSITORY, 7, GENERATION),
        IssueImplementationPorts(lambda: GENERATION, dispatch, lambda: False),
    )

    assert outcome.success
    assert calls == entered
    assert len(calls) == 1


def test_local_transfer_and_inherited_execution_never_finish_in_returning_service(tmp_path: Path) -> None:
    slots, routing = _stores(tmp_path)
    transfer_calls, finish = _finish_spy(slots)

    transferred = IssueImplementationService(routing, slots).execute(
        IssueImplementationRequest(REPOSITORY, 7, GENERATION),
        IssueImplementationPorts(lambda: GENERATION, lambda _authority: _result(DispatchOutcome.LOCAL_ACCEPTED), lambda: False),
    )
    transferred_id = slots.current_execution_id(OWNER)
    assert transferred.dispatch_result is not None
    assert transferred_id is not None
    assert transfer_calls == []

    # Retire the transferred worker exactly as its downstream owner would, then
    # create an execution inherited by a nested service caller.
    finish(OWNER, transferred_id)
    inherited_id = slots.start_execution(OWNER, generation=GENERATION)
    assert inherited_id is not None
    inherited = IssueImplementationService(routing, slots).execute(
        IssueImplementationRequest(REPOSITORY, 7, GENERATION, continue_execution=True),
        IssueImplementationPorts(lambda: GENERATION, lambda _authority: _result(DispatchOutcome.LOCAL_COMPLETED), lambda: False),
    )
    assert inherited.success
    assert transfer_calls == []
    assert slots.active_execution_ids(OWNER) == (inherited_id,)


def test_tracking_incomplete_remote_acceptance_finishes_only_caller_execution_and_retains_remote_owner(tmp_path: Path) -> None:
    slots, routing = _stores(tmp_path)
    calls, _ = _finish_spy(slots)
    entered: list[str] = []

    def dispatch(_authority):  # type: ignore[no-untyped-def]
        execution_id = slots.current_execution_id(OWNER)
        assert execution_id is not None
        entered.append(execution_id)
        if not slots.has_provider_sessions(OWNER):
            assert slots.record_provider_session(OWNER, "provider-7")
        return _result(DispatchOutcome.REMOTE_ACCEPTED, tracking_complete=False)

    result = IssueImplementationService(routing, slots).execute(
        IssueImplementationRequest(REPOSITORY, 7, GENERATION),
        IssueImplementationPorts(lambda: GENERATION, dispatch, lambda: False),
    )

    assert result.dispatch_result is not None and not result.dispatch_result.tracking_complete
    assert calls == entered
    assert slots.active_execution_ids(OWNER) == ()
    assert slots.has_provider_sessions(OWNER)

    # Re-entry reconciles the same incomplete remote receipt through a new
    # caller execution. Settlement must finish that new exact ID only; the
    # retained remote lifetime remains authoritative throughout.
    replay = IssueImplementationService(routing, slots).execute(
        IssueImplementationRequest(REPOSITORY, 7, GENERATION),
        IssueImplementationPorts(lambda: GENERATION, dispatch, lambda: False),
    )
    assert replay.dispatch_result is not None and not replay.dispatch_result.tracking_complete
    assert calls == entered
    assert len(calls) == 2 and len(set(calls)) == 2
    assert slots.active_execution_ids(OWNER) == ()
    assert slots.has_provider_sessions(OWNER)
