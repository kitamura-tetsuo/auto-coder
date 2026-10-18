"""Focused tests for the extracted Issue retry-admission service."""

from auto_coder.implementation_slots import ImplementationOwner, ImplementationSlotRepository
from auto_coder.issue_retry_admission import IssueRetryAdmissionService, IssueRetryDisposition, IssueRetryOrigin
from auto_coder.issue_stage_routing import IssueStageRoutingStore
from auto_coder.stale_jules_recovery import capture_stale_jules_recovery

REPOSITORY = "owner/repository"


def _service(tmp_path, capacity=1):
    routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    slots = ImplementationSlotRepository(REPOSITORY, capacity, tmp_path / "slots.json")
    return IssueRetryAdmissionService(routing, slots), routing, slots


def test_manual_admission_preserves_identity_across_reopened_stores(tmp_path):
    service, routing, slots = _service(tmp_path)

    first = service.admit(REPOSITORY, 7, "generation-a", "request-a", IssueRetryOrigin.MANUAL)

    assert first.disposition is IssueRetryDisposition.OWNED
    assert first.authority is not None
    assert first.authority.request_id == "request-a"
    assert first.authority.generation == "generation-a"
    assert first.authority.ownership_reference

    reopened_routing = IssueStageRoutingStore(tmp_path / "routing.sqlite3")
    reopened_slots = ImplementationSlotRepository(REPOSITORY, 1, slots.storage_path, slots.retired_storage_path)
    replay = IssueRetryAdmissionService(reopened_routing, reopened_slots).admit(REPOSITORY, 7, "generation-a", "request-a", IssueRetryOrigin.MANUAL)

    assert replay.disposition is IssueRetryDisposition.OWNED
    assert replay.authority == first.authority
    assert reopened_slots.active_execution_ids(ImplementationOwner("issue", 7)) == (first.authority.ownership_reference,)


def test_competing_manual_request_cannot_pass_unresolved_request(tmp_path):
    service, routing, slots = _service(tmp_path)
    routing.accept_retry_request("earlier", REPOSITORY, 7, "generation-a")

    competing = service.admit(REPOSITORY, 7, "generation-a", "later", IssueRetryOrigin.MANUAL)

    assert competing.disposition is IssueRetryDisposition.DEFERRED
    assert competing.authority is not None and competing.authority.request_id == "earlier"
    assert routing.retry_request("later") is None
    assert slots.active_execution_ids(ImplementationOwner("issue", 7)) == ()


def test_stale_jules_wait_resumes_same_request_without_capacity_bypass(tmp_path):
    service, routing, slots = _service(tmp_path, capacity=1)
    owner = ImplementationOwner("issue", 7)
    predecessor = slots.start_execution(owner, generation="generation-a")
    assert predecessor is not None
    assert slots.record_provider_session(owner, "stale-session")
    slots.finish_execution(owner, predecessor)
    authority = capture_stale_jules_recovery(routing, REPOSITORY, 7, 91, "generation-a")
    assert authority is not None

    waiting = service.admit(REPOSITORY, 7, "generation-a", authority.request_id, IssueRetryOrigin.STALE_JULES)
    assert waiting.disposition is IssueRetryDisposition.DEFERRED
    assert routing.retry_request(authority.request_id) == authority

    assert slots.finish_provider_session(owner, "stale-session")
    blocker = ImplementationOwner("issue", 8)
    assert slots.start_execution(blocker, generation="other") is not None
    capacity_wait = service.admit(REPOSITORY, 7, "generation-a", authority.request_id, IssueRetryOrigin.STALE_JULES)
    assert capacity_wait.disposition is IssueRetryDisposition.DEFERRED
    assert capacity_wait.authority is not None and capacity_wait.authority.request_id == authority.request_id

    for execution in slots.active_execution_ids(blocker):
        slots.finish_execution(blocker, execution)
    assert slots.release_unbound_idle_owner(blocker)
    resumed = service.admit(REPOSITORY, 7, "generation-a", authority.request_id, IssueRetryOrigin.STALE_JULES)
    assert resumed.disposition is IssueRetryDisposition.OWNED
    assert resumed.authority is not None and resumed.authority.attempt_id == authority.attempt_id


def test_stale_jules_generation_mismatch_never_acquires(tmp_path):
    service, routing, slots = _service(tmp_path)
    authority = capture_stale_jules_recovery(routing, REPOSITORY, 7, 91, "generation-a")
    assert authority is not None

    stale = service.admit(REPOSITORY, 7, "generation-b", authority.request_id, IssueRetryOrigin.STALE_JULES)

    assert stale.disposition is IssueRetryDisposition.STALE
    assert stale.authority == authority
    assert slots.active_execution_ids(ImplementationOwner("issue", 7)) == ()
