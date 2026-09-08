from collections.abc import Iterator

import pytest

from auto_coder.ci_observation import (
    CheckExecutionIdentity,
    CheckObservation,
    CIConclusion,
    CIObservationPhaseStore,
    CIObservationSnapshot,
    ObservationAvailability,
    ObservationRequest,
    ObservationSubject,
    WorkflowExecutionIdentity,
    WorkflowObservation,
)


def identities() -> Iterator[str]:
    index = 0
    while True:
        index += 1
        yield f"identity-{index}"


@pytest.fixture
def identity_source() -> Iterator[str]:
    return identities()


@pytest.fixture
def subject() -> ObservationSubject:
    return ObservationSubject("https://api.github.com", "owner/repo", 42, "head-H")


@pytest.fixture
def observation_request() -> ObservationRequest:
    return ObservationRequest("github-actions", "workflow-runs-and-check-runs-v1")


def workflow(attempt: int | None, conclusion: CIConclusion = CIConclusion.SUCCESS, *, workflow_id: str = "workflow-W", run_id: str = "run-R", name: str = "Tests") -> WorkflowObservation:
    return WorkflowObservation(WorkflowExecutionIdentity(workflow_id, run_id, attempt), conclusion, name)


def new_store(source: Iterator[str], **kwargs: object) -> CIObservationPhaseStore:
    return CIObservationPhaseStore(identity_factory=lambda: next(source), **kwargs)  # type: ignore[arg-type]


def publish_known(store: CIObservationPhaseStore, phase: str, fact: WorkflowObservation) -> CIObservationSnapshot:
    read = store.begin_read(phase)
    snapshot = read.snapshot(ObservationAvailability.KNOWN, (fact,))
    assert store.publish(read, snapshot).accepted is True
    return snapshot


def test_independent_queue_generation_and_restart_require_new_cycle(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    first_phase = store.begin_phase(subject, observation_request)
    first = publish_known(store, first_phase, workflow(1))
    store.end_phase()

    # A reused scheduler generation is intentionally not an input to this API.
    second_phase = store.begin_phase(subject, observation_request)
    second_read = store.begin_read(second_phase)
    assert second_read.cycle_id != first.cycle_id
    assert store.reusable(second_phase, subject, observation_request) is None

    restarted = new_store(identity_source, persisted_diagnostic=first)
    restart_phase = restarted.begin_phase(subject, observation_request)
    restart_read = restarted.begin_read(restart_phase)
    assert restart_read.cycle_id not in {first.cycle_id, second_read.cycle_id}
    assert restarted.diagnostic_snapshot == first
    assert restarted.reusable(restart_phase, subject, observation_request) is None


def test_unavailable_read_retains_identity_only_as_non_authoritative_diagnostic(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    known = publish_known(store, phase, workflow(1))

    failed_read = store.begin_read(phase)
    failed = failed_read.snapshot(ObservationAvailability.UNAVAILABLE, unavailable_reason="provider request failed")
    published_failure = store.publish(failed_read, failed)
    assert published_failure.accepted is True
    current = store.reusable(phase, subject, observation_request)
    assert current is not None
    assert current.availability is ObservationAvailability.UNAVAILABLE
    assert current.facts == ()
    assert current.diagnostic_facts == known.facts

    recovered_read = store.begin_read(phase)
    recovered = recovered_read.snapshot(ObservationAvailability.KNOWN, (workflow(1),))
    assert store.publish(recovered_read, recovered).accepted is True
    assert recovered.facts[0].execution == known.facts[0].execution  # type: ignore[union-attr]


def test_same_sha_rerun_and_reverse_completion_cannot_restore_old_success(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    old_read = store.begin_read(phase)
    old_success = old_read.snapshot(ObservationAvailability.KNOWN, (workflow(1),))

    rerun_read = store.begin_read(phase)
    rerun = rerun_read.snapshot(ObservationAvailability.KNOWN, (workflow(2, CIConclusion.PENDING),))
    assert store.publish(rerun_read, rerun).accepted is True
    late = store.publish(old_read, old_success)

    assert late.accepted is False
    assert late.reason == "cycle_superseded"
    assert late.snapshot.availability is ObservationAvailability.SUPERSEDED
    assert store.reusable(phase, subject, observation_request) == rerun
    assert rerun.workflow_execution("workflow-W", "run-R", 2) == workflow(2, CIConclusion.PENDING)
    assert rerun.workflow_execution("workflow-W", "run-R", 1) is None


def test_unknown_attempt_remains_unresolved(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    snapshot = publish_known(store, phase, workflow(None))

    assert snapshot.facts[0].execution.attempt is None  # type: ignore[union-attr]
    assert snapshot.workflow_execution("workflow-W", "run-R", 1) is None
    assert snapshot.workflow_execution("workflow-W", "run-R", 2) is None


def test_invalidation_fences_blocked_read_without_overwriting_new_phase(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source, initial_epoch=7)
    phase_a = store.begin_phase(subject, observation_request)
    read_a = store.begin_read(phase_a)
    assert read_a.captured_epoch == 7

    assert store.invalidate() == 8
    phase_b = store.begin_phase(subject, observation_request)
    read_b = store.begin_read(phase_b)
    snapshot_b = read_b.snapshot(ObservationAvailability.KNOWN, (workflow(2, CIConclusion.PENDING),))
    assert store.publish(read_b, snapshot_b).accepted is True

    snapshot_a = read_a.snapshot(ObservationAvailability.KNOWN, (workflow(1),))
    rejected = store.publish(read_a, snapshot_a)
    assert rejected.accepted is False
    assert rejected.reason == "phase_superseded"
    assert store.reusable(phase_b, subject, observation_request) == snapshot_b
    assert store.diagnostic_snapshot == snapshot_b
    assert store.obsolete_diagnostic == rejected.snapshot


def test_invalidation_while_queued_ends_phase_authority(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    queued_read = store.begin_read(phase)

    store.invalidate()

    assert store.reusable(phase, subject, observation_request) is None
    rejected = store.publish(queued_read, queued_read.snapshot(ObservationAvailability.KNOWN_EMPTY))
    assert rejected.accepted is False


def test_similar_display_names_preserve_distinct_workflow_and_app_identities(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    read = store.begin_read(phase)
    facts = (
        workflow(1, workflow_id="workflow-1", run_id="run-1", name="Tests"),
        workflow(1, CIConclusion.FAILURE, workflow_id="workflow-2", run_id="run-2", name="Tests"),
        CheckObservation(CheckExecutionIdentity("app-1", "check-1"), CIConclusion.SUCCESS, "Tests"),
        CheckObservation(CheckExecutionIdentity("app-2", "check-2"), CIConclusion.FAILURE, "Tests"),
    )
    snapshot = read.snapshot(ObservationAvailability.KNOWN, facts)

    assert store.publish(read, snapshot).accepted is True
    assert snapshot.facts == facts
    assert len({fact.execution for fact in snapshot.facts}) == 4


def test_availability_contract_does_not_flatten_empty_or_failed_reads(subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    empty = CIObservationSnapshot(subject, observation_request, "cycle-empty", 0, ObservationAvailability.KNOWN_EMPTY)
    assert empty.complete is True
    assert empty.facts == ()

    unavailable = CIObservationSnapshot(subject, observation_request, "cycle-failed", 0, ObservationAvailability.UNAVAILABLE, unavailable_reason="malformed response")
    assert unavailable.complete is False
    assert unavailable.facts == ()
    with pytest.raises(ValueError, match="Known-empty"):
        CIObservationSnapshot(subject, observation_request, "bad-empty", 0, ObservationAvailability.KNOWN_EMPTY, (workflow(1),))
    with pytest.raises(ValueError, match="safe reason"):
        CIObservationSnapshot(subject, observation_request, "bad-failure", 0, ObservationAvailability.UNAVAILABLE)


@pytest.mark.parametrize("boundary", ["mutation", "governor_wait", "external_work", "llm_work"])
def test_phase_boundaries_revoke_reuse_without_running_effects(boundary: str, identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    effects: list[str] = []
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    snapshot = publish_known(store, phase, workflow(1, CIConclusion.ACTION_REQUIRED))
    assert store.reusable(phase, subject, observation_request) == snapshot
    assert effects == []

    # Controllers call the same explicit boundary before each named activity.
    store.end_phase()

    assert store.reusable(phase, subject, observation_request) is None
    assert store.diagnostic_snapshot == snapshot
    assert effects == []


def test_scope_request_and_head_must_match_exactly(identity_source: Iterator[str], subject: ObservationSubject, observation_request: ObservationRequest) -> None:
    store = new_store(identity_source)
    phase = store.begin_phase(subject, observation_request)
    publish_known(store, phase, workflow(1))

    other_head = ObservationSubject(subject.api_origin, subject.repository, subject.pr_number, "head-H2")
    other_pr = ObservationSubject(subject.api_origin, subject.repository, 43, subject.head_sha)
    other_request = ObservationRequest(observation_request.source, "checks-only-v1")
    assert store.reusable(phase, other_head, observation_request) is None
    assert store.reusable(phase, other_pr, observation_request) is None
    assert store.reusable(phase, subject, other_request) is None


def test_check_association_cannot_be_partially_or_artificially_ordered() -> None:
    unresolved = CheckExecutionIdentity("app", "check")
    assert unresolved.workflow_id is None
    assert unresolved.run_id is None
    assert unresolved.attempt is None
    with pytest.raises(ValueError, match="requires an explicit run"):
        CheckExecutionIdentity("app", "check", attempt=1)
    with pytest.raises(ValueError, match="both be known"):
        CheckExecutionIdentity("app", "check", workflow_id="workflow")
