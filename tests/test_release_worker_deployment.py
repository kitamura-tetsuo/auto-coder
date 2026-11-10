"""Release provenance and authenticated activation contract regressions.

Actual acceptance/catalog/publication and dispatch readback producers execute
against controlled GitHub transport and SQLite. The real_git case uses real
isolated annotated objects/refs. Worker observations and Actions remain controlled
test boundaries; genuine OS-lifetime or installed-package acceptance is separate.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from auto_coder.management.app import create_management_app
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import ActionParameter, AdmissionStatus, ManagedTarget, MutationStatus, OperationIntent, OperationPhase
from auto_coder.management.release_worker_deployment import ReleaseWorkerEvidenceIdentity, ReleaseWorkerEvidenceReader
from auto_coder.management.source_release_dispatch import ACTION, INPUT_FIELDS, SourceReleaseDispatch
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkResult
from auto_coder.source_release_catalog import SourceReleaseError
from auto_coder.source_release_promotion import AcceptedSourceValidator
from auto_coder.tested_source_refs import TestedSourcePublisher
from tests.test_management_mcp import real_boundary
from tests.test_management_source_release_dispatch import ACTION as MCP_PROMOTION_ACTION
from tests.test_management_source_release_dispatch import WORKFLOW_ID, WorkflowBoundary, WorkflowGit, _call, _token, dispatch
from tests.test_management_worker_lifecycle import LifecycleBridge, _drive
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
    assert facts.release_operation_selection_proven is True
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
    with pytest.raises(SourceReleaseError, match="SUPERSEDED"):
        d.reader().read(identity)
    facts = d.reader().read(other)
    assert facts.current_source_sha == other.source_sha
    assert facts.release_operation_selection_proven is True
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


@pytest.fixture
def release_flow(dispatch, monkeypatch):
    """Authenticated MCP plus original Git producers and a controlled bridge.

    These cases verify contract/fencing, not genuine OS lifecycle acceptance.
    The independent process suite must still verify the Supervisor producers.
    """
    d = dispatch
    d.operating.write_text(
        "profile: full\ntargets:\n  release-source:\n    channel: release\n    repositories: [owner/repo]\n  release-worker:\n    channel: release\n    repositories: [owner/repo]\n",
        encoding="utf-8",
    )
    tested = TestedSourcePublisher(d.remote, "owner/repo").read(d.smoke.worker.source_sha)
    assert tested is not None

    class ReleaseBridge(LifecycleBridge):
        def __init__(self):
            super().__init__()
            self.config = WorkerEndpointConfig("release-worker", "release", "owner/repo", "10.23.10.13", 17891, b"r" * 48, target_id="release-worker")
            self.artifact_origin = "build_embedded"
            self.artifact_version_origin = "installed_distribution_metadata"

        def prepare_version(self, operation_id: str, source_sha: str) -> WorkerNetworkResult:
            result = super().prepare_version(operation_id, source_sha)
            assert self.preparation is not None
            self.preparation.update(lock_sha256=tested.record.lock_sha256, distribution_version=tested.record.distribution_version)
            return result

        def observe_worker(self, nonce: str | None = None) -> WorkerNetworkResult:
            result = super().observe_worker(nonce)
            assert result.data is not None
            observed = result.data["observation"]
            binding = result.data["runtime_binding"]
            assert isinstance(observed, dict) and isinstance(binding, dict)
            if self.selected == "runtime-new":
                binding.update(lock_sha256=tested.record.lock_sha256, distribution_version=tested.record.distribution_version)
                observed["artifact"] = {
                    "source_revision": {"available": True, "value": tested.record.source_sha, "origin": self.artifact_origin},
                    "distribution_version": {"available": True, "value": tested.record.distribution_version, "origin": self.artifact_version_origin},
                }
            return result

    bridge = ReleaseBridge()
    original_init = ReleaseWorkerEvidenceReader.__init__

    def fixed_observation_clock(self, *args, **kwargs):
        kwargs["clock"] = d.remote.clock
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ReleaseWorkerEvidenceReader, "__init__", fixed_observation_clock)
    app = create_management_app(d.settings, d.grants, worker_clients={"release": bridge})

    async def no_background_monitor() -> None:
        pass

    monkeypatch.setattr(app.state.management_runtime, "start_monitor", no_background_monitor)
    return SimpleNamespace(**{**vars(d), "bridge": bridge, "app": app})


def _release_arguments(d, promotion: dict[str, object], *, key: str = "release-deploy-1") -> dict[str, object]:
    operation_id = str(promotion["operation_id"])
    catalog = d.remote.request("GET", f"git/ref/tags/auto-coder-source-release-{operation_id}")
    assert catalog.status == 200 and isinstance(catalog.data, dict)
    return {
        "target_id": "release-worker",
        "selected_git_sha": d.smoke.worker.source_sha,
        "promotion_operation_id": operation_id,
        "catalog_ref": f"refs/tags/auto-coder-source-release-{operation_id}",
        "catalog_oid": catalog.data["object"]["sha"],
        "acceptance_ref": d.args["acceptance_ref"],
        "acceptance_oid": d.args["acceptance_oid"],
        "idempotency_key": key,
        "expected_supervisor_generation": "supervisor-a",
        "expected_runtime_id": "runtime-old",
        "expected_process_run_id": "run-old",
        "expected_deployment_generation": "generation-old",
    }


def test_authenticated_release_deployment_reuses_original_producer_and_journal(release_flow):
    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        assert promotion["status"] == "SUCCEEDED"
        arguments = _release_arguments(d, promotion)
        first = _call(client, token, "auto_coder_deploy_release_source", arguments)
        assert first.get("action") == "deploy_release_source" and first["status"] in {"PREPARING", "ACCEPTED"}, first
        operation_id = str(first["operation_id"])
        # Original readback precedes fresh Git resolution for identical replay.
        assert _call(client, token, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
        assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": "b" * 40})["status"] == "CONFLICT"
        competing = _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-key"})
        assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id
        _drive(d.app, operation_id, 50)
        finished = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert finished["status"] == "SUCCEEDED", finished
        assert finished["source_sha"] == d.smoke.worker.source_sha
        assert finished["release_identity"]["catalog_oid"] == arguments["catalog_oid"]
        assert len(finished["heartbeats"]) == 2
        assert finished["steps"]["drain"]["expected_digest"] == d.bridge.receipts["d-" + operation_id]["intent_digest"]
        assert [name for name, _ in d.bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        assert len(d.remote.dispatches) == 1 and d.remote.release_pointer_writes == 1


def test_same_sha_new_release_supersedes_staged_but_not_replacement_effect(release_flow):
    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        arguments = _release_arguments(d, promotion)
        admitted = _call(client, token, "auto_coder_deploy_release_source", arguments)
        assert admitted["status"] in {"PREPARING", "ACCEPTED"}, admitted
        operation_id = str(admitted["operation_id"])
        for _ in range(12):
            record = d.app.state.management_runtime.journal.get(operation_id).record
            assert record is not None
            d.app.state.management_runtime.lifecycle.process(record)
            current = d.app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
            if current is not None and current[1]["stage"] == "DRAINING":
                break
        else:
            raise AssertionError("release staging did not reach the replacement boundary")
        next_promotion = _call(client, token, MCP_PROMOTION_ACTION, {**d.args, "idempotency_key": "same-sha-new-release"})
        assert next_promotion["status"] == "SUCCEEDED"
        assert d.remote.release_pointer_writes == 1 and d.remote.release_selection_writes == 2
        _drive(d.app, operation_id, 12)
        status = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert status["status"] == "SUPERSEDED" and status["reason"] == "original_release_operation_superseded"
        assert [name for name, _ in d.bridge.actions] == ["PREPARE"]


def test_stopped_release_incumbent_requires_independent_absence_before_start(release_flow):
    d = release_flow
    d.bridge.worker = None
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        args.pop("expected_process_run_id")
        args.pop("expected_deployment_generation")
        admitted = _call(client, token, "auto_coder_deploy_release_source", args)
        assert admitted["status"] in {"ACCEPTED", "PREPARING"}, admitted
        operation_id = str(admitted["operation_id"])
        _drive(d.app, operation_id, 50)
        outcome = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert outcome["status"] == "SUCCEEDED", outcome
        assert [name for name, _ in d.bridge.actions] == ["PREPARE", "START"]


def test_release_selection_changed_during_durable_pre_effect_write_never_dispatches(release_flow, monkeypatch):
    from auto_coder.source_release_catalog import RELEASE_SELECTION_REF

    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        admitted = _call(client, token, "auto_coder_deploy_release_source", _release_arguments(d, promotion))
        operation_id = str(admitted["operation_id"])
        lifecycle = d.app.state.management_runtime.lifecycle
        old_save = lifecycle._save

        def change_after_durable_step(record, revision, snapshot):
            saved = old_save(record, revision, snapshot)
            steps = snapshot.get("steps")
            if saved and record.operation_id == operation_id and isinstance(steps, dict) and "drain" in steps:
                d.remote.refs[RELEASE_SELECTION_REF] = ("tag", OTHER_SHA)
            return saved

        monkeypatch.setattr(lifecycle, "_save", change_after_durable_step)
        _drive(d.app, operation_id, 25)
        outcome = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert outcome["status"] == "DRAINING" and outcome["steps"]["drain"]["state"] == "SENT_UNKNOWN", outcome
        assert [name for name, _ in d.bridge.actions] == ["PREPARE"]
        assert _call(client, token, "auto_coder_deploy_release_source", {**_release_arguments(d, promotion), "idempotency_key": "other"})["status"] == "BUSY"


def test_release_recovery_unknown_receipt_retains_lane_and_recovers_original_step(release_flow, monkeypatch):
    d = release_flow
    d.bridge.crash_new = True
    original_dispatch = d.bridge.dispatch_worker_operation
    original_read = d.bridge.read_worker_operation
    allow_readback = {"value": False}

    def lose_recovery_reply(operation_id: str, operation: str, **kwargs) -> WorkerNetworkResult:
        response = original_dispatch(operation_id, operation, **kwargs)
        if operation == "RECOVER_PREVIOUS":
            return WorkerNetworkResult(0, "UNKNOWN", None, response.supervisor_generation, response.request_id)
        return response

    def withhold_recovery_receipt(operation_id: str) -> WorkerNetworkResult:
        if operation_id.startswith("r-") and not allow_readback["value"]:
            return d.bridge._result("UNKNOWN", {"status": "UNKNOWN", "operation_id": operation_id})
        return original_read(operation_id)

    monkeypatch.setattr(d.bridge, "dispatch_worker_operation", lose_recovery_reply)
    monkeypatch.setattr(d.bridge, "read_worker_operation", withhold_recovery_receipt)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        admitted = _call(client, token, "auto_coder_deploy_release_source", args)
        operation_id = str(admitted["operation_id"])
        _drive(d.app, operation_id, 40)
        held = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert held["status"] == "RESTORING" and held["recovery_state"] == "UNKNOWN", held
        assert held["recovery_attempts"] == 1
        assert [name for name, _ in d.bridge.actions].count("RECOVER_PREVIOUS") == 1
        assert _call(client, token, "auto_coder_deploy_release_source", {**args, "idempotency_key": "competing"})["status"] == "BUSY"
        allow_readback["value"] = True
        _drive(d.app, operation_id, 20)
        recovered = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert recovered["status"] == "FAILED" and recovered["recovery_state"] == "CONFIRMED", recovered
        assert [name for name, _ in d.bridge.actions].count("RECOVER_PREVIOUS") == 1


@pytest.mark.parametrize("intervention", ("policy_downgrade", "missing_selection", "lost_drain_reply", "crash_new", "wrong_artifact_origin", "wrong_version_origin", "unreadable_supervisor_receipt"))
def test_release_deploy_rechecks_policy_reconciles_lost_replies_and_refuses_unverified_activation(release_flow, monkeypatch, intervention):
    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        assert promotion["status"] == "SUCCEEDED"
        arguments = _release_arguments(d, promotion)
        admitted = _call(client, token, "auto_coder_deploy_release_source", arguments)
        operation_id = str(admitted["operation_id"])
        runtime = d.app.state.management_runtime
        for _ in range(15):
            record = runtime.journal.get(operation_id).record
            assert record is not None
            runtime.lifecycle.process(record)
            snapshot = runtime.journal.lifecycle_snapshot(operation_id)
            if snapshot is not None and snapshot[1]["stage"] == "DRAINING":
                break
        else:
            raise AssertionError("release staging did not reach drain boundary")
        if intervention == "policy_downgrade":
            d.operating.write_text(d.operating.read_text().replace("profile: full", "profile: observe"))
        elif intervention == "missing_selection":
            from auto_coder.source_release_catalog import RELEASE_SELECTION_REF

            d.remote.refs.pop(RELEASE_SELECTION_REF)
        elif intervention == "lost_drain_reply":
            d.bridge.lost_stop_response = True
        elif intervention == "crash_new":
            d.bridge.crash_new = True
        elif intervention == "wrong_artifact_origin":
            d.bridge.artifact_origin = "manager_reported"
        elif intervention == "wrong_version_origin":
            d.bridge.artifact_version_origin = "unverified"
        elif intervention == "unreadable_supervisor_receipt":
            original_read = d.bridge.read_worker_operation

            def unreadable_stop(effect_id: str) -> WorkerNetworkResult:
                if effect_id.startswith("d-"):
                    return d.bridge._result("UNKNOWN", {"status": "UNKNOWN", "operation_id": effect_id})
                return original_read(effect_id)

            monkeypatch.setattr(d.bridge, "read_worker_operation", unreadable_stop)
        _drive(d.app, operation_id, 40)
        outcome = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        actions = [name for name, _ in d.bridge.actions]
        if intervention in {"policy_downgrade", "missing_selection"}:
            assert outcome["status"] in {"NEEDS_ATTENTION", "SUPERSEDED"}, outcome
            assert actions == ["PREPARE"]
        elif intervention == "lost_drain_reply":
            assert outcome["status"] == "SUCCEEDED", outcome
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        elif intervention == "crash_new":
            assert outcome["status"] == "FAILED" and outcome["recovery_state"] == "CONFIRMED", outcome
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START", "RECOVER_PREVIOUS"]
            assert outcome["recovery_attempts"] == 1
        elif intervention in {"wrong_artifact_origin", "wrong_version_origin"}:
            assert outcome["status"] == "RECONCILING" and outcome["heartbeats"] == [], outcome
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        else:
            assert outcome["status"] == "DRAINING" and outcome["steps"]["drain"]["state"] == "SENT_UNKNOWN", outcome
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP"]
            assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "different"})["status"] == "BUSY"
