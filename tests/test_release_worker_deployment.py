"""Read-only provenance component tests, not authenticated activation evidence.

Actual acceptance/catalog/publication and dispatch readback producers execute
against controlled GitHub transport and SQLite. The real_git case uses real
isolated annotated objects/refs. Worker observations and Actions remain controlled
test boundaries; these tests cannot satisfy release-worker lifecycle acceptance.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, AdmissionStatus, ManagedTarget, MutationStatus, OperationIntent, OperationPhase
from auto_coder.management.release_worker_deployment import ReleaseWorkerEvidenceIdentity, ReleaseWorkerEvidenceReader
from auto_coder.management.source_release_dispatch import ACTION, INPUT_FIELDS, SourceReleaseDispatch
from auto_coder.source_release_catalog import SourceReleaseError
from auto_coder.source_release_promotion import AcceptedSourceValidator
from tests.test_management_source_release_dispatch import WORKFLOW_ID, WorkflowBoundary, WorkflowGit
from tests.test_source_release_promotion import OTHER_SHA, RELEASE_REF, RealReleaseGit, _harness


@pytest.fixture
def joined(tmp_path, request):
    class RealWorkflowGit(WorkflowBoundary, RealReleaseGit):
        """The same workflow transport backed by real local Git objects."""

    remote = RealWorkflowGit(tmp_path / "release.git") if getattr(request, "param", None) == "real_git" else WorkflowGit()
    remote, smoke, candidate, policy = _harness(tmp_path / "evidence", remote=remote)
    remote.configured, remote.clock = policy, lambda: smoke.clock.now + 1
    journal = ManagementOperationStore(tmp_path / "operations")
    runtime = SimpleNamespace(journal=journal, settings=SimpleNamespace(), _params=lambda record: {item.name: item.value for item in record.intent.parameters})
    observer = SourceReleaseDispatch(runtime)

    def publish(key="promotion", *, complete=True):
        values = {field: getattr(candidate, field) for field in INPUT_FIELDS}
        params = {**values, "target_id": "release-source", "release_policy_revision": policy.revision, "workflow_id": WORKFLOW_ID}
        intent = OperationIntent(ManagedTarget("accepted-source-release", "release", policy.source_repository), ACTION, tuple(ActionParameter(k, v) for k, v in params.items()), key, policy.release_ref, candidate.source_sha)
        accepted = AcceptedSourceValidator(remote, policy, clock=remote.clock).validate(candidate)
        snapshot = {"schema_version": 1, "kind": ACTION, "policy": asdict(policy), "accepted": asdict(accepted), "validated_at": remote.clock(), "workflow_id": WORKFLOW_ID}
        admitted = journal.admit(intent, smoke.authority, lifecycle_snapshot=snapshot)
        assert admitted.status is AdmissionStatus.CREATED and admitted.record is not None
        record = admitted.record
        begun = journal.begin_effect(record.operation_id, record.revision, owner_attempt_id="fixture-attempt", attempt_id="fixture-attempt", correlation_identity=record.operation_id)
        assert begun.status is MutationStatus.UPDATED and begun.record is not None
        record = begun.record
        remote.complete = complete
        inputs = {**values, "operation_id": record.operation_id, "source_repository": policy.source_repository, "release_target": policy.release_target}
        sent = remote.request("POST", f"actions/workflows/{WORKFLOW_ID}/dispatches", {"ref": "main", "inputs": inputs, "return_run_details": True})
        assert sent.status == 200
        assert journal.bind_source_release_run(record, {"id": sent.data["workflow_run_id"]}, kind="response")
        record = journal.get(record.operation_id).record
        # The production observer derives its terminal receipt from the real
        # producer artifacts; the fixture never supplies a success receipt.
        observed = observer._observe(record, policy, WORKFLOW_ID, snapshot, remote)
        assert observed.phase is (OperationPhase.SUCCEEDED if complete else OperationPhase.NEEDS_ATTENTION)
        catalog = remote.request("GET", f"git/ref/tags/auto-coder-source-release-{record.operation_id}")
        identity = ReleaseWorkerEvidenceIdentity(record.operation_id, candidate.source_sha, f"refs/tags/auto-coder-source-release-{record.operation_id}", catalog.data["object"]["sha"], candidate.acceptance_ref, candidate.acceptance_oid)
        return identity

    def reader(*, clock=None, store=None, configured=None):
        return ReleaseWorkerEvidenceReader(store or journal, remote, configured or policy, promotion_target_id="release-source", workflow_id=WORKFLOW_ID, clock=clock or remote.clock)

    return SimpleNamespace(remote=remote, journal=journal, policy=policy, publish=publish, reader=reader, root=tmp_path / "operations")


@pytest.mark.parametrize("joined", ["real_git"], indirect=True)
def test_real_git_producer_join_survives_journal_reconstruction_without_authorizing(joined, _use_real_commands):
    d = joined
    identity = d.publish()
    original = d.journal.get(identity.promotion_operation_id).record
    snapshot = d.journal.lifecycle_snapshot(identity.promotion_operation_id)
    history = d.journal.history(identity.promotion_operation_id)
    assert d.remote._git("cat-file", "-t", identity.catalog_oid) == "tag"
    assert d.remote._git("rev-parse", identity.catalog_ref + "^{}") == identity.source_sha
    assert d.remote._git("rev-parse", identity.acceptance_ref) == identity.acceptance_oid
    calls = []
    request = d.remote.request

    def read_only(method, path, payload=None):
        calls.append((method, path))
        assert method == "GET"
        return request(method, path, payload)

    d.remote.request = read_only
    reconstructed = ManagementOperationStore(d.root)
    facts = d.reader(store=reconstructed).read(identity)
    assert facts.identity == identity
    assert facts.catalog.tag_object_oid == identity.catalog_oid
    assert facts.acceptance.local_evidence_id == facts.catalog.record.local_evidence_id
    assert facts.acceptance.local_evidence_content_sha256 == facts.catalog.record.local_evidence_content_sha256
    assert facts.current_source_sha == identity.source_sha != OTHER_SHA
    assert facts.promotion_intent_fingerprint == original.intent_fingerprint
    assert facts.release_operation_selection_proven is False
    assert facts.deployment_authorized is False
    assert facts.worker_deployed is False
    assert calls
    assert reconstructed.get(identity.promotion_operation_id).record == original
    assert reconstructed.lifecycle_snapshot(identity.promotion_operation_id) == snapshot
    assert reconstructed.history(identity.promotion_operation_id) == history
    assert len(d.remote.dispatches) == d.remote.release_pointer_writes == 1


@pytest.mark.parametrize(
    "field,value",
    [("source_sha", OTHER_SHA), ("catalog_oid", OTHER_SHA), ("catalog_ref", "refs/tags/other"), ("acceptance_oid", OTHER_SHA), ("acceptance_ref", "refs/tags/auto-coder-beta-acceptance-other"), ("promotion_operation_id", "another-promotion"), ("source_sha", "main"), ("catalog_oid", "A" * 40)],
)
def test_exact_request_identity_mismatch_never_substitutes_latest(joined, field, value):
    d = joined
    identity = d.publish()
    d.remote.calls.clear()
    with pytest.raises(SourceReleaseError):
        d.reader().read(replace(identity, **{field: value}))
    assert all(method == "GET" for method, _ in d.remote.calls)
    assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("fault", ["missing_catalog", "missing_release", "draft_release", "moved_pointer", "unreadable_pointer", "missing_acceptance", "failed_run", "changed_checkout", "wrong_workflow", "stale_acceptance", "changed_policy", "unavailable_run"])
def test_producer_evidence_faults_are_refused_with_no_writes(joined, fault):
    d = joined
    identity = d.publish()
    tag = identity.catalog_ref.removeprefix("refs/tags/")
    clock, policy = d.remote.clock, d.policy
    if fault == "missing_catalog":
        del d.remote.refs[identity.catalog_ref]
    elif fault == "missing_release":
        del d.remote.releases[tag]
    elif fault == "draft_release":
        d.remote.releases[tag]["draft"] = True
    elif fault == "moved_pointer":
        d.remote.refs[RELEASE_REF] = ("commit", OTHER_SHA)
    elif fault == "unreadable_pointer":
        d.remote.unreadable_pointer = True
    elif fault == "missing_acceptance":
        del d.remote.refs[identity.acceptance_ref]
    elif fault == "failed_run":
        d.remote.runs[900]["conclusion"] = "failure"
    elif fault == "changed_checkout":
        d.remote.runs[900]["head_sha"] = identity.source_sha
    elif fault == "wrong_workflow":
        d.remote.runs[900]["workflow_id"] += 1
    elif fault == "stale_acceptance":
        clock = lambda: d.remote.clock() + 86401
    elif fault == "changed_policy":
        policy = replace(policy, maximum_evidence_age_seconds=1)
    elif fault == "unavailable_run":
        d.remote.run_reads_unavailable = True
    d.remote.calls.clear()
    original = d.journal.get(identity.promotion_operation_id).record
    with pytest.raises(SourceReleaseError):
        d.reader(clock=clock, configured=policy).read(identity)
    assert all(method == "GET" for method, _ in d.remote.calls)
    assert d.journal.get(identity.promotion_operation_id).record == original
    assert len(d.remote.dispatches) == 1


def test_pending_promotion_and_same_sha_new_publication_never_establish_deploy_authority(joined):
    d = joined
    identity = d.publish()
    other = d.publish("another-promotion")
    assert other.promotion_operation_id != identity.promotion_operation_id
    assert other.catalog_oid != identity.catalog_oid
    assert len(d.remote.dispatches) == 2
    facts = d.reader().read(identity)
    assert facts.current_source_sha == identity.source_sha
    assert facts.release_operation_selection_proven is False
    assert facts.deployment_authorized is False
    pending = d.publish("pending-promotion", complete=False)
    d.remote.calls.clear()
    with pytest.raises(SourceReleaseError, match="not confirmed"):
        d.reader().read(pending)
    assert d.remote.calls == []


def test_original_snapshot_is_immutable_through_journal_api(joined):
    d = joined
    identity = d.publish()
    version, snapshot = d.journal.lifecycle_snapshot(identity.promotion_operation_id)
    original_digest = snapshot["accepted"]["local_evidence_content_sha256"]
    snapshot["accepted"]["local_evidence_content_sha256"] = "f" * 64
    assert d.journal.lifecycle_save(identity.promotion_operation_id, version, snapshot) is False
    assert d.reader().read(identity).acceptance.local_evidence_content_sha256 == original_digest
