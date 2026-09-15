from auto_coder.issue_review_worker import FreshReviewView, IssueReviewWorker
from auto_coder.issue_stage_routing import (
    REVIEW_STAGE,
    IssueStageRoutingStore,
    ReviewRequirement,
    review_classification,
)
from auto_coder.validation_scheduler import ValidationScheduler

REPO = "owner/repo"


def _store(tmp_path, target, priority, generation="gen-1", identities=("id-1",)):
    store = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    requirements = tuple(ReviewRequirement("individual", target, key) for key in identities)
    item = store.reconcile(review_classification(REPO, target, generation, priority, True, requirements))
    assert item is not None
    return store


def _harness(store, calls, verdicts, persisted, handoffs, terminal=None):
    terminal = terminal or {}

    def refresh(item):
        current = store.pending(REPO, REVIEW_STAGE)
        keys = next((i.remaining_identity_keys for i in current if i.target_number == item.target_number), ())
        return FreshReviewView(True, True, tuple(keys))

    def lookup_terminal(key):
        return terminal.get(key)

    def review_identity(key):
        calls.append(key)
        return verdicts.get(key, "READY")

    def persist(key, verdict):
        persisted[key] = verdict
        terminal[key] = verdict

    def on_routing_request(item):
        handoffs.append(item.target_number)

    return refresh, lookup_terminal, review_identity, persist, on_routing_request


def test_high_priority_overtakes_and_equal_priority_fifo(tmp_path):
    store = _store(tmp_path, 1, 0, "g1")
    store.reconcile(review_classification(REPO, 2, "g2", 0, True, (ReviewRequirement("individual", 2, "id-2"),)))
    store.reconcile(review_classification(REPO, 3, "g3", 3, True, (ReviewRequirement("individual", 3, "id-3"),)))
    worker = IssueReviewWorker(store, ValidationScheduler(2))
    assert [i.target_number for i in store.pending(REPO, REVIEW_STAGE)] == [3, 1, 2]
    assert worker.next_work(REPO).target_number == 3


def test_terminal_reuse_makes_zero_backend_calls(tmp_path):
    store = _store(tmp_path, 1, 0)
    worker = IssueReviewWorker(store, ValidationScheduler(2))
    calls, persisted, handoffs = [], {}, []
    args = _harness(store, calls, {}, persisted, handoffs, terminal={"id-1": "READY"})
    outcome = worker.run_one(REPO, refresh=args[0], lookup_terminal=args[1], review_identity=args[2], persist=args[3], on_routing_request=args[4])
    assert outcome.status == "completed" and outcome.handed_off
    assert calls == [] and outcome.backend_calls == 0
    assert outcome.verdicts == {"id-1": "READY"}
    assert store.pending(REPO, REVIEW_STAGE) == ()


def test_stale_generation_never_invokes_reviewer(tmp_path):
    store = _store(tmp_path, 1, 0)
    worker = IssueReviewWorker(store, ValidationScheduler(2))
    calls = []

    def refresh(item):
        return FreshReviewView(False, True, ())

    outcome = worker.run_one(
        REPO,
        refresh=refresh,
        lookup_terminal=lambda key: None,
        review_identity=lambda key: calls.append(key) or "READY",
        persist=lambda key, verdict: None,
        on_routing_request=lambda item: None,
    )
    assert outcome.status == "stale"
    assert calls == []
    assert store.pending(REPO, REVIEW_STAGE) == ()


def test_malformed_reviewer_output_becomes_retryable_error(tmp_path):
    store = _store(tmp_path, 1, 0)
    worker = IssueReviewWorker(store, ValidationScheduler(2))
    calls, persisted, handoffs = [], {}, []
    args = _harness(store, calls, {"id-1": "SYNTHESIZED-READY"}, persisted, handoffs)
    outcome = worker.run_one(REPO, refresh=args[0], lookup_terminal=args[1], review_identity=args[2], persist=args[3], on_routing_request=args[4])
    assert outcome.status == "error" and not outcome.handed_off
    assert persisted == {}
    assert len(store.pending(REPO, REVIEW_STAGE)) == 1


def test_duplicate_identities_coalesce_to_single_execution(tmp_path):
    store = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    requirements = (ReviewRequirement("individual", 1, "same-id"), ReviewRequirement("individual", 1, "same-id"))
    item = store.reconcile(review_classification(REPO, 1, "g1", 0, True, requirements))
    assert item is not None
    worker = IssueReviewWorker(store, ValidationScheduler(2))
    calls = []
    outcome = worker.run_one(
        REPO,
        refresh=lambda item: FreshReviewView(True, True, ("same-id", "same-id")),
        lookup_terminal=lambda key: None,
        review_identity=lambda key: calls.append(key) or "READY",
        persist=lambda key, verdict: None,
        on_routing_request=lambda item: None,
    )
    assert outcome.status == "completed"
    assert calls == ["same-id"]


def test_worker_never_touches_implementation_slots():
    import auto_coder.issue_review_worker as module

    source = open(module.__file__).read()
    assert "implementation_slots" not in source
    assert "ImplementationSlot" not in source
    assert "mark_implementation_owned" not in source
    assert "from .implementation_slots import" not in source


def test_crash_after_persist_before_wake_recovers_without_rereview(tmp_path):
    path = tmp_path / "routing.sqlite3"
    store = _store(tmp_path, 1, 0)
    worker = IssueReviewWorker(store, ValidationScheduler(2))
    terminal, persisted, handoffs = {}, {}, []

    def review_identity(key):
        persisted[key] = "READY"
        terminal[key] = "READY"
        raise RuntimeError("crash before wake")

    try:
        worker.run_one(
            REPO,
            refresh=lambda item: FreshReviewView(True, True, ("id-1",)),
            lookup_terminal=lambda key: None,
            review_identity=review_identity,
            persist=lambda key, verdict: persisted.__setitem__(key, verdict),
            on_routing_request=lambda item: handoffs.append(item.target_number),
        )
    except RuntimeError:
        pass
    restarted = IssueStageRoutingStore(path)
    restarted.recover(REPO)
    resumed = IssueReviewWorker(restarted, ValidationScheduler(2))
    calls = []
    args = _harness(restarted, calls, {}, {}, handoffs, terminal=terminal)
    outcome = resumed.run_one(REPO, refresh=args[0], lookup_terminal=args[1], review_identity=args[2], persist=args[3], on_routing_request=args[4])
    assert outcome.status == "completed" and calls == []
    assert handoffs == [1]


def test_reprioritization_keeps_arrival_and_review_progresses_without_implementation_capacity(tmp_path):
    store = _store(tmp_path, 1, 0)
    first = store.pending(REPO, REVIEW_STAGE)[0]
    updated = store.reconcile(review_classification(REPO, 1, "gen-1", 7, True, (ReviewRequirement("individual", 1, "id-1"),)))
    assert updated.arrival == first.arrival and updated.priority == 7
    # Implementation capacity being full is irrelevant: the worker has no slot dependency.
    worker = IssueReviewWorker(store, ValidationScheduler(1))
    calls, persisted, handoffs = [], {}, []
    args = _harness(store, calls, {"id-1": "BLOCKED"}, persisted, handoffs)
    outcome = worker.run_one(REPO, refresh=args[0], lookup_terminal=args[1], review_identity=args[2], persist=args[3], on_routing_request=args[4])
    assert outcome.status == "completed" and outcome.verdicts == {"id-1": "BLOCKED"}
