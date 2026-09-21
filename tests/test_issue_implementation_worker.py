from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from auto_coder.automation_config import AutomationConfig, CandidateProcessingResult
from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_implementation_worker import IssueImplementationWorker
from auto_coder.issue_stage_routing import IMPLEMENTATION_STAGE, IssueStageRoutingStore, LaneClassification
from auto_coder.specification_analyzer import SpecificationAnalysisResult
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle, ValidationDecision
from tests.test_implementation_ownership import _ready_engine, _standalone_snapshot
from tests.test_issue_stage_routing import REPO


def classification(number: int, generation: str, priority: int = 0) -> LaneClassification:
    return LaneClassification("owner/repo", IMPLEMENTATION_STAGE, number, generation, priority, True)


def test_worker_selects_priority_then_fifo_and_never_has_review_dependency(tmp_path):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    routing.reconcile(classification(1, "regular-a"))
    routing.reconcile(classification(2, "regular-b"))
    routing.reconcile(classification(2, "regular-b", 3))
    worker = IssueImplementationWorker(routing)
    calls = []

    def refresh(item):
        calls.append(("refresh", item.target_number))
        return routing.get(item.repository, item.stage, item.target_number)

    outcome = worker.run_one("owner/repo", refresh=refresh, dispatch=lambda item: calls.append(("dispatch", item.target_number)) or "deferred")

    assert outcome is not None
    assert (outcome.target_number, outcome.status) == (2, "deferred")
    assert calls == [("refresh", 2), ("refresh", 2), ("dispatch", 2)]
    assert [item.target_number for item in routing.pending("owner/repo", IMPLEMENTATION_STAGE)] == [2, 1]


def test_worker_drops_generation_that_changes_at_final_refresh(tmp_path):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    original = routing.reconcile(classification(1, "g1"))
    assert original is not None
    worker = IssueImplementationWorker(routing)
    refreshes = 0
    dispatched = False

    def refresh(item):
        nonlocal refreshes
        refreshes += 1
        if refreshes == 2:
            return routing.reconcile(classification(1, "g2"))
        return routing.get(item.repository, item.stage, item.target_number)

    def dispatch(_item):
        nonlocal dispatched
        dispatched = True
        return "owned"

    outcome = worker.run_one("owner/repo", refresh=refresh, dispatch=dispatch)

    assert outcome is not None and outcome.status == "stale"
    assert dispatched is False
    assert routing.get("owner/repo", IMPLEMENTATION_STAGE, 1).generation == "g2"


def test_worker_recovery_preserves_arrival_and_owned_suppression(tmp_path):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    item = routing.reconcile(classification(1, "g1"))
    assert item is not None and routing.begin(item)
    worker = IssueImplementationWorker(routing)
    worker.recover("owner/repo")
    recovered = routing.get("owner/repo", IMPLEMENTATION_STAGE, 1)
    assert recovered is not None and recovered.arrival == item.arrival and recovered.state == "pending"
    assert routing.begin(recovered)
    assert routing.mark_implementation_owned(recovered)
    worker.recover("owner/repo")
    assert routing.get("owner/repo", IMPLEMENTATION_STAGE, 1) is None
    assert routing.reconcile(classification(1, "g1")) is None


def _production_lane(tmp_path, monkeypatch, snapshots, *, capacity=2):
    config = AutomationConfig(repo_name=REPO)
    config.issue_specification_validation = True
    config.issue_decomposition_validation = False
    analyzer = MagicMock(side_effect=AssertionError("Implementation lane must not invoke semantic review"))
    engine, github = _ready_engine(tmp_path, monkeypatch, snapshots, config)
    lifecycle = SpecificationValidationLifecycle(REPO, "semantic-policy", tmp_path / "lane-ready.json", analyzer)
    engine._specification_validators[REPO] = lifecycle
    engine.implementation_slots = ImplementationSlotRepository(REPO, capacity, tmp_path / "lane-slots.json")
    dispatch = MagicMock(side_effect=lambda _repo, candidate, *_args, **_kwargs: CandidateProcessingResult("issue", candidate.data["number"], success=True, actions=["dispatched"]))
    engine._process_single_candidate_reserved = dispatch
    github.get_issue_comments_strict.return_value = []
    return engine, github, lifecycle, analyzer, dispatch


def _persist_ready(lifecycle, snapshot):
    identity = lifecycle.identity(snapshot["number"], snapshot["title"], snapshot["body"])
    lifecycle.store.save(ValidationDecision(identity, "READY"))


def test_production_lane_skips_pending_high_priority_review_and_dispatches_ready_lower_priority(tmp_path, monkeypatch):
    """REQ-002/005/011: Review-pending work is absent from Implementation competition."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    urgent = _standalone_snapshot(1, created)
    urgent["labels"].append({"name": "breaking-change"})
    ready = _standalone_snapshot(2, created)
    engine, _github, lifecycle, analyzer, dispatch = _production_lane(tmp_path, monkeypatch, {1: urgent, 2: ready})
    _persist_ready(lifecycle, ready)
    engine._route_issue_stages_authoritatively(REPO, 1, urgent)
    engine._route_issue_stages_authoritatively(REPO, 2, ready)

    outcomes = engine.pump_issue_implementation_lane(REPO, max_items=1)

    assert [(outcome.target_number, outcome.status) for outcome in outcomes] == [(2, "owned")]
    assert dispatch.call_count == 1
    assert dispatch.call_args.args[1].data["number"] == 2
    assert [item.target_number for item in engine.issue_stage_routing.pending(REPO, IMPLEMENTATION_STAGE)] == []
    analyzer.assert_not_called()


def test_production_lane_priority_fifo_and_reprioritization_reach_owned_dispatch(tmp_path, monkeypatch):
    """REQ-005/016: current priority wins while equal priority retains durable FIFO."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshots = {number: _standalone_snapshot(number, created) for number in (1, 2, 3)}
    engine, _github, lifecycle, analyzer, dispatch = _production_lane(tmp_path, monkeypatch, snapshots, capacity=3)
    for number in (1, 2, 3):
        _persist_ready(lifecycle, snapshots[number])
        engine._route_issue_stages_authoritatively(REPO, number, snapshots[number])
    first_arrivals = {item.target_number: item.arrival for item in engine.issue_stage_routing.pending(REPO, IMPLEMENTATION_STAGE)}
    snapshots[3]["labels"].append({"name": "urgent"})
    engine._route_issue_stages_authoritatively(REPO, 3, snapshots[3])
    reprioritized = engine.issue_stage_routing.get(REPO, IMPLEMENTATION_STAGE, 3)
    assert reprioritized is not None
    assert reprioritized.arrival == first_arrivals[3]

    outcomes = engine.pump_issue_implementation_lane(REPO, max_items=3)

    assert [outcome.target_number for outcome in outcomes] == [3, 1, 2]
    assert all(outcome.status == "owned" for outcome in outcomes)
    assert [call.args[1].data["number"] for call in dispatch.call_args_list] == [3, 1, 2]
    assert first_arrivals[1] < first_arrivals[2] < first_arrivals[3]
    analyzer.assert_not_called()


def test_production_lane_final_refresh_rejects_readiness_loss_without_slot_or_review(tmp_path, monkeypatch):
    """REQ-007/011: readiness removal between queueing and dispatch retires the item."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    initial = _standalone_snapshot(1, created)
    current = dict(initial)
    current["labels"] = []
    engine, github, lifecycle, analyzer, dispatch = _production_lane(tmp_path, monkeypatch, {1: initial})
    _persist_ready(lifecycle, initial)
    engine._route_issue_stages_authoritatively(REPO, 1, initial)
    github.get_issue_dispatch_snapshot_strict.side_effect = lambda _repo, _number: dict(current)

    outcomes = engine.pump_issue_implementation_lane(REPO, max_items=1)

    assert [(outcome.target_number, outcome.status) for outcome in outcomes] == [(1, "stale")]
    assert engine.implementation_slots.active_owners() == ()
    dispatch.assert_not_called()
    analyzer.assert_not_called()


def test_production_lane_duplicate_wakeup_and_restart_cannot_redispatch_owned_generation(tmp_path, monkeypatch):
    """REQ-006/008/014: the real ownership handoff suppresses replay after restart."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot = _standalone_snapshot(1, created)
    engine, _github, lifecycle, analyzer, dispatch = _production_lane(tmp_path, monkeypatch, {1: snapshot})
    _persist_ready(lifecycle, snapshot)
    engine._route_issue_stages_authoritatively(REPO, 1, snapshot)
    first = engine.pump_issue_implementation_lane(REPO, max_items=1)
    generation = engine.implementation_slots.implementation_generation(ImplementationOwner("issue", 1))
    assert generation is not None
    assert [(outcome.target_number, outcome.status) for outcome in first] == [(1, "owned")]

    engine.issue_stage_routing.recover(REPO)
    engine._route_issue_stages_authoritatively(REPO, 1, snapshot)
    second = engine.pump_issue_implementation_lane(REPO, max_items=1)

    assert second == []
    assert dispatch.call_count == 1
    assert engine.issue_stage_routing.is_implementation_owned(REPO, 1, generation)
    analyzer.assert_not_called()


def test_production_lane_capacity_deferral_preserves_arrival_and_reuses_ready(tmp_path, monkeypatch):
    """REQ-004/015/016: an operational slot refusal neither renews arrival nor reviews."""
    created = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    snapshot = _standalone_snapshot(1, created)
    engine, _github, lifecycle, analyzer, dispatch = _production_lane(tmp_path, monkeypatch, {1: snapshot}, capacity=1)
    _persist_ready(lifecycle, snapshot)
    engine._route_issue_stages_authoritatively(REPO, 1, snapshot)
    queued = engine.issue_stage_routing.get(REPO, IMPLEMENTATION_STAGE, 1)
    assert queued is not None
    blocker = ImplementationOwner("issue", 99)
    blocker_execution = engine.implementation_slots.start_execution(blocker, generation="other-generation")
    assert blocker_execution is not None

    deferred = engine.pump_issue_implementation_lane(REPO, max_items=1)

    retained = engine.issue_stage_routing.get(REPO, IMPLEMENTATION_STAGE, 1)
    assert [(outcome.target_number, outcome.status) for outcome in deferred] == [(1, "deferred")]
    assert retained is not None and retained.arrival == queued.arrival
    dispatch.assert_not_called()
    analyzer.assert_not_called()

    engine.implementation_slots.finish_execution(blocker, blocker_execution)
    assert engine.implementation_slots.release_unbound_idle_owner(blocker)
    admitted = engine.pump_issue_implementation_lane(REPO, max_items=1)

    assert [(outcome.target_number, outcome.status) for outcome in admitted] == [(1, "owned")]
    assert dispatch.call_count == 1
    analyzer.assert_not_called()
