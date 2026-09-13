from auto_coder.issue_stage_routing import (
    IMPLEMENTATION_STAGE,
    REVIEW_STAGE,
    ContractIdentity,
    IssueStageRoutingStore,
    ReviewRequirement,
    family_review_generation,
    implementation_classification,
    implementation_generation,
    issue_priority,
    review_classification,
    standalone_review_generation,
)

REPO = "owner/repo"


def contract(number: int, body: str = "body", role: str = "standalone") -> ContractIdentity:
    return ContractIdentity(REPO, number, number * 10, f"Issue {number}", body, role)


def requirement(number: int, verdict: str | None = None, *, category: str = "individual", in_flight: bool = False) -> ReviewRequirement:
    return ReviewRequirement(category, number, f"{category}-{number}", verdict, in_flight)


def test_review_generation_excludes_status_priority_and_filters_terminal_work(tmp_path):
    store = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    target = contract(1)
    requirements = (requirement(1, "READY"), requirement(2), requirement(3, "BLOCKED"), requirement(4, "ERROR"), requirement(5, in_flight=True))
    generation = standalone_review_generation(target, requirements)

    first = store.reconcile(review_classification(REPO, 1, generation, 0, True, requirements), now=10)
    assert first is not None
    assert first.remaining_identity_keys == ("individual-2", "individual-4")
    assert first.arrival == 1

    changed_status = tuple(requirement(item.subject_number, "BLOCKED" if item.subject_number == 2 else item.verdict, in_flight=item.in_flight) for item in requirements)
    assert standalone_review_generation(target, changed_status) == generation
    updated = store.reconcile(review_classification(REPO, 1, generation, 7, True, changed_status), now=99)
    assert updated is not None
    assert updated.arrival == first.arrival
    assert updated.priority == 7
    assert updated.remaining_identity_keys == ("individual-4",)


def test_family_generation_changes_for_contract_policy_and_membership_not_verdict():
    parent = contract(10, role="parent")
    children = (contract(11, role="child"), contract(12, role="child"))
    evidence = (requirement(10, category="decomposition"), requirement(11), requirement(12))
    generation = family_review_generation(REPO, parent, children, evidence)

    terminal = tuple(ReviewRequirement(item.category, item.subject_number, item.identity_key, "READY") for item in evidence)
    assert family_review_generation(REPO, parent, children, terminal) == generation
    assert family_review_generation(REPO, parent, children[:1], evidence[:2]) != generation
    assert family_review_generation(REPO, parent, (contract(11, "edited", "child"), children[1]), evidence) != generation
    changed_policy = (*evidence[:-1], ReviewRequirement("individual", 12, "new-policy"))
    assert family_review_generation(REPO, parent, children, changed_policy) != generation


def test_semantic_ineligibility_renews_arrival_but_operational_retry_does_not(tmp_path):
    store = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    evidence = (requirement(1),)
    classification = review_classification(REPO, 1, "generation", 0, True, evidence)
    first = store.reconcile(classification)
    assert first is not None and store.begin(first)
    assert store.defer(first)
    assert store.pending(REPO, REVIEW_STAGE)[0].arrival == first.arrival

    assert store.reconcile(review_classification(REPO, 1, "generation", 0, False, evidence)) is None
    restored = store.reconcile(classification)
    assert restored is not None
    assert restored.arrival > first.arrival


def test_supersession_and_lane_fifo_priority_are_durable_across_restart(tmp_path):
    path = tmp_path / "routing.sqlite3"
    store = IssueStageRoutingStore(path)
    evidence = (requirement(1),)
    old = store.reconcile(review_classification(REPO, 1, "old", 0, True, evidence))
    equal = store.reconcile(review_classification(REPO, 2, "equal", 0, True, evidence))
    urgent = store.reconcile(review_classification(REPO, 3, "urgent", 3, True, evidence))
    assert old is not None and equal is not None and urgent is not None
    assert [item.target_number for item in store.pending(REPO, REVIEW_STAGE)] == [3, 1, 2]

    replacement = store.reconcile(review_classification(REPO, 1, "new", 0, True, evidence))
    assert replacement is not None and replacement.arrival > equal.arrival
    restarted = IssueStageRoutingStore(path)
    assert [(item.target_number, item.generation) for item in restarted.pending(REPO, REVIEW_STAGE)] == [(3, "urgent"), (2, "equal"), (1, "new")]


def test_implementation_requires_all_ready_and_owned_generation_never_restarts(tmp_path):
    path = tmp_path / "routing.sqlite3"
    store = IssueStageRoutingStore(path)
    target = contract(12, role="child")
    generation = implementation_generation(target, (contract(10, role="parent").key, contract(11, role="child").key, target.key))
    incomplete = (requirement(11, "READY"), requirement(12, "BLOCKED"), requirement(10, "READY", category="decomposition"))
    assert store.reconcile(implementation_classification(REPO, 12, generation, 0, True, incomplete)) is None

    ready = tuple(ReviewRequirement(item.category, item.subject_number, item.identity_key, "READY") for item in incomplete)
    item = store.reconcile(implementation_classification(REPO, 12, generation, 3, True, ready))
    assert item is not None and store.begin(item)
    assert store.mark_implementation_owned(item, now=50)
    assert store.is_implementation_owned(REPO, 12, generation)

    restarted = IssueStageRoutingStore(path)
    restarted.recover(REPO)
    assert restarted.reconcile(implementation_classification(REPO, 12, generation, 7, True, ready)) is None
    changed = implementation_generation(contract(12, "edited", "child"), (contract(10, role="parent").key,))
    assert restarted.reconcile(implementation_classification(REPO, 12, changed, 0, True, ready)) is not None


def test_crash_before_owned_start_retries_same_arrival(tmp_path):
    path = tmp_path / "routing.sqlite3"
    store = IssueStageRoutingStore(path)
    ready = (requirement(1, "READY"),)
    item = store.reconcile(implementation_classification(REPO, 1, "generation", 0, True, ready))
    assert item is not None and store.begin(item)

    restarted = IssueStageRoutingStore(path)
    restarted.recover(REPO)
    recovered = restarted.pending(REPO, IMPLEMENTATION_STAGE)
    assert len(recovered) == 1
    assert recovered[0].arrival == item.arrival
    assert recovered[0].generation == item.generation


def test_priority_contract_and_review_versus_child_sources():
    assert issue_priority(["urgent"]) == 3
    assert issue_priority(["urgent", "deprecation"]) == 7
    assert issue_priority(["implementation-ready"]) == 0
    family_priority = max(issue_priority([]), issue_priority(["urgent"]), issue_priority([]))
    child_priority = issue_priority([])
    assert (family_priority, child_priority) == (3, 0)
