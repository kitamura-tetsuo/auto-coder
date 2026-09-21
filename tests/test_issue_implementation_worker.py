from auto_coder.issue_implementation_worker import IssueImplementationWorker
from auto_coder.issue_stage_routing import IMPLEMENTATION_STAGE, IssueStageRoutingStore, LaneClassification


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
