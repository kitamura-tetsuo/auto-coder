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
from auto_coder.management.operations import ActionParameter, AdmissionStatus, EffectState, ManagedTarget, MutationStatus, OperationIntent, OperationPhase
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
            observed["artifact"]["source_revision"]["origin"] = self.artifact_origin
            observed["artifact"]["distribution_version"]["origin"] = self.artifact_version_origin
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


@pytest.mark.parametrize("delay_terminal_settlement", [False, True])
def test_same_sha_new_release_supersedes_staged_but_not_replacement_effect(release_flow, monkeypatch, delay_terminal_settlement):
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
        service = d.app.state.management_runtime
        if delay_terminal_settlement:
            # Expose the real durable snapshot before the separate journal
            # transition, without fabricating a terminal receipt or outcome.
            with monkeypatch.context() as paused:
                paused.setattr(service.lifecycle, "_settle", lambda record, stage, reason: record)
                _drive(d.app, operation_id, 12)
                pending = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
                assert pending["status"] == "SUPERSEDED" and pending["phase"] != OperationPhase.FAILED.value
                assert pending["reason"] == "worker_lifecycle"
                record = service.journal.get(operation_id).record
                assert record is not None and record.effect.state is EffectState.SENT_UNKNOWN and record.effect.receipt is None
                replay = _call(client, token, "auto_coder_deploy_release_source", arguments)
                assert replay["operation_id"] == operation_id and replay["phase"] != OperationPhase.FAILED.value
                competing = _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "pending-supersession-competitor"})
                assert competing["status"] == "BUSY" and competing["operation_id"] == operation_id
                assert [name for name, _ in d.bridge.actions] == ["PREPARE"]
        _drive(d.app, operation_id, 12)
        status = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert status["status"] == "SUPERSEDED" and status["phase"] == OperationPhase.FAILED.value
        assert status["reason"] == "original_release_operation_superseded"
        settled = service.journal.get(operation_id).record
        assert settled is not None and settled.effect.state is EffectState.CONFIRMED
        assert settled.effect.receipt is not None and settled.effect.receipt.correlation_identity == settled.effect.correlation_identity
        assert settled.effect.receipt.safe_result == "original_release_operation_superseded"
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


@pytest.mark.parametrize("downgrade", ("revoked", "expired"))
def test_original_release_grant_downgrade_after_staging_prevents_worker_transition(release_flow, downgrade):
    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        admitted = _call(client, token, "auto_coder_deploy_release_source", _release_arguments(d, promotion))
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
            raise AssertionError("release staging did not reach the drain boundary")
        assert [name for name, _ in d.bridge.actions] == ["PREPARE"]

        with d.grants.store.connect() as db:
            if downgrade == "revoked":
                db.execute("UPDATE grants SET revoked=?", (d.state["now"],))
            else:
                db.execute("UPDATE grants SET expires=?", (d.state["now"] - 1,))
        _drive(d.app, operation_id, 30)
        current = runtime.journal.get(operation_id).record
        assert current is not None
        status = runtime.lifecycle.project(current)
        assert status["status"] == "NEEDS_ATTENTION" and status["reason"] == "original_authority_denied", status
        assert [name for name, _ in d.bridge.actions] == ["PREPARE"]


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
        assert recovered["reason"] == "new_worker_failed_previous_runtime_restored"
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
            assert outcome["reason"] == "new_worker_failed_previous_runtime_restored"
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START", "RECOVER_PREVIOUS"]
            assert outcome["recovery_attempts"] == 1
        elif intervention in {"wrong_artifact_origin", "wrong_version_origin"}:
            assert outcome["status"] == "RECONCILING" and outcome["heartbeats"] == [], outcome
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
        else:
            assert outcome["status"] == "DRAINING" and outcome["steps"]["drain"]["state"] == "SENT_UNKNOWN", outcome
            assert actions == ["PREPARE", "REQUEST_GRACEFUL_STOP"]
            assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "different"})["status"] == "BUSY"


@pytest.mark.parametrize("boundary", ("drain", "start"))
@pytest.mark.parametrize("fault", (None, "digest", "selection", "live-binding"))
def test_release_reconstructed_supervisor_requires_original_transition_evidence(release_flow, monkeypatch, boundary, fault):
    """Controlled signed boundary test, not an installed-worker acceptance claim."""
    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        arguments = _release_arguments(d, promotion)
        admitted = _call(client, token, "auto_coder_deploy_release_source", arguments)
        operation_id = str(admitted["operation_id"])
        effect_id = ("d-" if boundary == "drain" else "s-") + operation_id
        for _ in range(20):
            _drive(d.app, operation_id, 1)
            if effect_id in d.bridge.receipts:
                break
        else:
            raise AssertionError("original release transition was not dispatched")
        actions = list(d.bridge.actions)
        original_digest = d.bridge.receipts[effect_id]["intent_digest"]
        d.bridge.supervisor_generation = "supervisor-reconstructed"
        if fault == "digest":
            d.bridge.receipts[effect_id]["intent_digest"] = "0" * 64
        elif fault == "selection":
            if boundary == "drain":
                d.bridge.selected = "unrelated-runtime"
            else:
                assert d.bridge.worker is not None
                d.bridge.worker = {**d.bridge.worker, "deployment_generation": "unrelated-child"}
        elif fault == "live-binding":
            if boundary == "drain":
                # No owner may be adopted while the old OS lifetime is alive.
                d.bridge.worker = {"process_run_id": "old-live", "deployment_generation": "old-live", "runtime_id": "runtime-old"}
            else:
                original_observe = d.bridge.observe_worker

                def altered_binding(nonce=None):
                    result = original_observe(nonce)
                    result.data["runtime_binding"]["content_identity"] = "0" * 64
                    return result

                monkeypatch.setattr(d.bridge, "observe_worker", altered_binding)
        _drive(d.app, operation_id, 30)
        status = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        if fault is None:
            assert status["status"] == "SUCCEEDED", status
            assert [name for name, _ in d.bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]
            assert d.bridge.receipts[effect_id]["intent_digest"] == original_digest
        else:
            assert status["status"] == "NEEDS_ATTENTION", status
            assert d.bridge.actions == actions
            assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-reconstruction"})["status"] == "BUSY"
        assert _call(client, token, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
        assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "selected_git_sha": "b" * 40})["status"] == "CONFLICT"


@pytest.mark.parametrize("unavailability", ("transport", "binding", "receipt", "first-inspection", "second-inspection"))
def test_release_reconstruction_waits_for_same_child_binding_to_become_observable(release_flow, monkeypatch, unavailability):
    d = release_flow
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        arguments = _release_arguments(d, promotion)
        operation_id = str(_call(client, token, "auto_coder_deploy_release_source", arguments)["operation_id"])
        for _ in range(20):
            _drive(d.app, operation_id, 1)
            if "s-" + operation_id in d.bridge.receipts:
                break
        else:
            raise AssertionError("original start missing")
        actions = list(d.bridge.actions)
        d.bridge.supervisor_generation = "supervisor-reconstructed"
        original_observe = d.bridge.observe_worker

        def unavailable(nonce=None):
            if unavailability == "transport":
                return WorkerNetworkResult(0, "UNAVAILABLE", None, d.bridge.supervisor_generation, "unavailable")
            result = original_observe(nonce)
            result.data["binding_status"] = "UNAVAILABLE"
            result.data["runtime_binding"] = None
            return result

        original_read = d.bridge.read_worker_operation
        original_inspect = d.bridge.inspect_supervisor
        if unavailability == "receipt":
            monkeypatch.setattr(d.bridge, "read_worker_operation", lambda _operation: WorkerNetworkResult(0, "UNAVAILABLE", None, d.bridge.supervisor_generation, "unavailable"))
        elif unavailability in {"first-inspection", "second-inspection"}:
            # Each attempt first observes the changed owner, then the rebind
            # performs one inspection before and one after the original receipt.
            count = 0
            cadence = 2 if unavailability == "first-inspection" else 3

            def intermittent_inspect():
                nonlocal count
                count += 1
                if count % cadence == 0:
                    return WorkerNetworkResult(0, "UNAVAILABLE", None, d.bridge.supervisor_generation, "unavailable")
                return original_inspect()

            monkeypatch.setattr(d.bridge, "inspect_supervisor", intermittent_inspect)
        else:
            monkeypatch.setattr(d.bridge, "observe_worker", unavailable)
        _drive(d.app, operation_id, 8)
        retained = d.app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
        assert retained is not None and retained[1]["stage"] not in {"SUCCEEDED", "FAILED", "NEEDS_ATTENTION"}
        assert d.bridge.actions == actions
        assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-unavailable"})["status"] == "BUSY"
        monkeypatch.setattr(d.bridge, "observe_worker", original_observe)
        monkeypatch.setattr(d.bridge, "read_worker_operation", original_read)
        monkeypatch.setattr(d.bridge, "inspect_supervisor", original_inspect)
        _drive(d.app, operation_id, 30)
        finished = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert finished["status"] == "SUCCEEDED", finished
        assert _call(client, token, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
        assert d.bridge.actions == actions


@pytest.fixture
def pending_release_recovery(release_flow, monkeypatch, request):
    """Controlled transport regression, not installed/OS acceptance evidence."""
    d = release_flow
    d.bridge.crash_new = True
    gate = {"readable": False}
    dispatch_original = d.bridge.dispatch_worker_operation
    read_original = d.bridge.read_worker_operation

    def lose_recovery_reply(operation_id, operation, **kwargs):
        result = dispatch_original(operation_id, operation, **kwargs)
        return WorkerNetworkResult(0, "UNKNOWN", None, result.supervisor_generation, result.request_id) if operation == "RECOVER_PREVIOUS" else result

    def read_recovery(operation_id):
        if operation_id.startswith("r-") and not gate["readable"]:
            return WorkerNetworkResult(0, "UNAVAILABLE", None, d.bridge.supervisor_generation, "unavailable")
        return read_original(operation_id)

    monkeypatch.setattr(d.bridge, "dispatch_worker_operation", lose_recovery_reply)
    monkeypatch.setattr(d.bridge, "read_worker_operation", read_recovery)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        operation_id = str(_call(client, token, "auto_coder_deploy_release_source", args)["operation_id"])
        _drive(d.app, operation_id, 40)
        journal = d.app.state.management_runtime.journal
        phase, receipt_run_missing = getattr(request, "param", ("RESTORING", False))
        if receipt_run_missing:
            receipt = d.bridge.receipts["r-" + operation_id]
            receipt["worker"] = {**receipt["worker"], "process_run_id": None}
        if phase == "RECONCILING_PRIOR":
            gate["readable"] = True
            for _ in range(4):
                _drive(d.app, operation_id, 1)
                if journal.lifecycle_snapshot(operation_id)[1]["stage"] == phase:
                    break
            gate["readable"] = False
        original = journal.get(operation_id).record
        snapshot = journal.lifecycle_snapshot(operation_id)[1]
        assert snapshot["stage"] == phase and snapshot["recovery_state"] == "UNKNOWN"
        assert snapshot["steps"]["recovery"]["state"] == ("CONFIRMED" if phase == "RECONCILING_PRIOR" else "SENT_UNKNOWN")
        assert snapshot["recovery_attempts"] == 1
        actions = list(d.bridge.actions)
        assert [action for action, _ in actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START", "RECOVER_PREVIOUS"]
        d.bridge.supervisor_generation = "supervisor-after-recovery"
        yield SimpleNamespace(**vars(d), client=client, token=token, arguments=args, operation_id=operation_id, original=original, snapshot=snapshot, journal=journal, actions=actions, recovery_gate=gate)


def _assert_recovery_lane_retained(d, stage):
    result = _call(d.client, d.token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": d.operation_id})
    assert result["status"] == stage and result["recovery_state"] == "UNKNOWN", result
    assert result["recovery_attempts"] == 1
    assert d.bridge.actions == d.actions
    replay = _call(d.client, d.token, "auto_coder_deploy_release_source", d.arguments)
    assert replay["operation_id"] == d.operation_id and replay["status"] == stage
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", {**d.arguments, "selected_git_sha": "b" * 40})["status"] == "CONFLICT"
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", {**d.arguments, "catalog_oid": "f" * 40})["status"] == "CONFLICT"
    competing = _call(d.client, d.token, "auto_coder_deploy_release_source", {**d.arguments, "idempotency_key": "competing-recovery-owner"})
    assert competing["status"] == "BUSY" and competing["operation_id"] == d.operation_id
    record = d.journal.get(d.operation_id).record
    snapshot = d.journal.lifecycle_snapshot(d.operation_id)[1]
    assert record.intent == d.original.intent and record.authority == d.original.authority
    for field in ("steps", "deadline", "original_deadline", "new_worker", "previous_binding", "incumbent", "accepted_evidence"):
        assert snapshot[field] == d.snapshot[field], field


@pytest.mark.parametrize("pending_release_recovery", (("RESTORING", False), ("RESTORING", True), ("RECONCILING_PRIOR", False), ("RECONCILING_PRIOR", True)), indirect=True)
def test_release_unknown_recovery_rebinds_only_its_original_receipt(pending_release_recovery):
    d = pending_release_recovery
    _drive(d.app, d.operation_id, 8)
    _assert_recovery_lane_retained(d, d.snapshot["stage"])
    d.recovery_gate["readable"] = True
    _drive(d.app, d.operation_id, 30)
    result = _call(d.client, d.token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": d.operation_id})
    assert result["status"] == "FAILED" and result["recovery_state"] == "CONFIRMED", result
    assert result["reason"] == "new_worker_failed_previous_runtime_restored"
    assert result["recovery_attempts"] == 1 and d.bridge.actions == d.actions
    assert result["restored_worker"]["runtime_id"] == d.snapshot["previous_binding"]["runtime_id"]
    assert result["new_worker"] == d.snapshot["new_worker"]
    assert result["original_deadline"] == d.snapshot["original_deadline"]
    for field in ("operation_id", "expected_digest", "owner_generation", "attempt_id", "correlation_identity", "sent_at"):
        assert result["steps"]["recovery"][field] == d.snapshot["steps"]["recovery"][field]
    assert result["steps"]["start"] == d.snapshot["steps"]["start"]
    assert d.journal.lifecycle_snapshot(d.operation_id)[1]["current_supervisor_generation"] == d.bridge.supervisor_generation
    record = d.journal.get(d.operation_id).record
    assert record.phase is OperationPhase.FAILED and record.intent == d.original.intent and record.authority == d.original.authority
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", d.arguments)["operation_id"] == d.operation_id
    assert d.bridge.actions == d.actions


@pytest.mark.parametrize("fault", ("receipt-id", "start-digest", "receipt-worker", "selected-runtime", "failed-generation", "incumbent-generation", "failed-fence", "runtime_id", "content_identity", "source_sha", "lock_sha256", "distribution_version", "source-origin", "version-origin"))
def test_release_recovery_reconstruction_rejects_conflicting_evidence(pending_release_recovery, monkeypatch, fault):
    d = pending_release_recovery
    d.recovery_gate["readable"] = True
    receipt = d.bridge.receipts["r-" + d.operation_id]
    if fault == "receipt-id":
        receipt["operation_id"] = "s-" + d.operation_id
    elif fault == "start-digest":
        receipt["intent_digest"] = d.snapshot["steps"]["start"]["expected_digest"]
    elif fault == "receipt-worker":
        receipt["worker"] = {**receipt["worker"], "deployment_generation": "unrelated"}
    elif fault == "selected-runtime":
        d.bridge.selected = "unrelated"
    elif fault in {"failed-generation", "incumbent-generation"}:
        fence = d.snapshot["new_worker"] if fault == "failed-generation" else d.snapshot["incumbent"]
        d.bridge.worker["deployment_generation"] = fence["deployment_generation"]
    elif fault == "failed-fence":
        revision, snapshot = d.journal.lifecycle_snapshot(d.operation_id)
        snapshot["new_worker"] = None
        assert d.journal.lifecycle_save(d.operation_id, revision, snapshot)
        d.snapshot = snapshot
    elif fault in {"source-origin", "version-origin"}:
        setattr(d.bridge, "artifact_origin" if fault == "source-origin" else "artifact_version_origin", "declared_configuration")
    else:
        observe_original = d.bridge.observe_worker

        def wrong_binding(nonce=None):
            result = observe_original(nonce)
            result.data["runtime_binding"][fault] = "unrelated"
            return result

        monkeypatch.setattr(d.bridge, "observe_worker", wrong_binding)
    _drive(d.app, d.operation_id, 12)
    _assert_recovery_lane_retained(d, "NEEDS_ATTENTION")


@pytest.mark.parametrize("unavailability", ("receipt", "inspection", "observation", "binding"))
def test_release_recovery_reconstruction_waits_without_changing_deadline(pending_release_recovery, monkeypatch, unavailability):
    d = pending_release_recovery
    d.recovery_gate["readable"] = unavailability != "receipt"
    original_inspect = d.bridge.inspect_supervisor
    original_observe = d.bridge.observe_worker

    def unavailable_observation(nonce=None):
        if unavailability == "observation":
            return WorkerNetworkResult(0, "UNKNOWN", None, d.bridge.supervisor_generation, "unavailable")
        result = original_observe(nonce)
        result.data.update(binding_status="UNAVAILABLE", runtime_binding=None)
        return result

    if unavailability == "inspection":
        monkeypatch.setattr(d.bridge, "inspect_supervisor", lambda: WorkerNetworkResult(0, "UNAVAILABLE", None, d.bridge.supervisor_generation, "unavailable"))
    elif unavailability != "receipt":
        monkeypatch.setattr(d.bridge, "observe_worker", unavailable_observation)
    _drive(d.app, d.operation_id, 8)
    _assert_recovery_lane_retained(d, "RESTORING")
    monkeypatch.setattr(d.bridge, "inspect_supervisor", original_inspect)
    monkeypatch.setattr(d.bridge, "observe_worker", original_observe)
    d.recovery_gate["readable"] = True
    _drive(d.app, d.operation_id, 30)
    result = _call(d.client, d.token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": d.operation_id})
    assert result["status"] == "FAILED" and result["recovery_state"] == "CONFIRMED", result
    assert d.bridge.actions == d.actions


@pytest.fixture
def expired_release_recovery(pending_release_recovery):
    d = pending_release_recovery
    revision, snapshot = d.journal.lifecycle_snapshot(d.operation_id)
    snapshot["deadline"] = snapshot["accepted_at"] - 1
    # A candidate sample must never count as restored-runtime forward progress.
    snapshot["heartbeats"] = [900]
    assert d.journal.lifecycle_save(d.operation_id, revision, snapshot)
    d.snapshot = snapshot
    d.bridge.supervisor_generation = snapshot["incumbent"]["supervisor_generation"]
    _drive(d.app, d.operation_id, 1)
    assert d.journal.get(d.operation_id).record.phase is OperationPhase.NEEDS_ATTENTION
    assert d.journal.lifecycle_snapshot(d.operation_id)[1]["reason"] == "lifecycle_deadline_exceeded"
    d.bridge.supervisor_generation = "supervisor-after-deadline"
    yield d


def _assert_recovery_terminal_proof(d):
    record = d.journal.get(d.operation_id).record
    snapshot = d.journal.lifecycle_snapshot(d.operation_id)[1]
    assert record.phase is OperationPhase.FAILED and record.effect.state.value == "confirmed"
    assert record.effect.receipt is not None
    assert record.effect.receipt.correlation_identity == d.original.effect.correlation_identity
    assert record.effect.receipt.observation_source == "supervisor-and-worker-lifecycle-journal"
    assert record.intent == d.original.intent and record.authority == d.original.authority
    assert snapshot["stage"] == "FAILED" and snapshot["recovery_state"] == "CONFIRMED"
    assert d.app.state.management_runtime.lifecycle.project(record)["reason"] == "new_worker_failed_previous_runtime_restored"
    assert snapshot["recovery_attempts"] == 1
    assert snapshot["deadline"] == d.snapshot["deadline"] and snapshot["original_deadline"] == d.snapshot["original_deadline"]
    assert snapshot["new_worker"] == d.snapshot["new_worker"]
    assert snapshot["accepted_evidence"] == d.snapshot["accepted_evidence"]
    for field in ("operation_id", "owner_generation", "attempt_id", "correlation_identity", "expected_digest", "sent_at"):
        assert snapshot["steps"]["recovery"][field] == d.snapshot["steps"]["recovery"][field]
    assert d.bridge.actions == d.actions
    return snapshot


def test_release_expired_unknown_recovery_resolves_only_after_original_readiness_and_progress(expired_release_recovery, monkeypatch):
    d = expired_release_recovery
    _drive(d.app, d.operation_id, 6)
    _assert_recovery_lane_retained(d, "NEEDS_ATTENTION")
    progress = {"serving": False, "sequence": 100}
    observe_original = d.bridge.observe_worker

    def observe(nonce=None):
        result = observe_original(nonce)
        result.data["observation"]["lifecycle"]["daemon_serving"] = progress["serving"]
        result.data["observation"]["heartbeat"]["sequence"] = progress["sequence"]
        return result

    monkeypatch.setattr(d.bridge, "observe_worker", observe)
    d.recovery_gate["readable"] = True
    _drive(d.app, d.operation_id, 6)
    _assert_recovery_lane_retained(d, "NEEDS_ATTENTION")
    progress["serving"] = True
    _drive(d.app, d.operation_id, 6)
    pending = d.journal.lifecycle_snapshot(d.operation_id)[1]
    assert pending["stage"] == "NEEDS_ATTENTION" and pending["recovery_state"] == "UNKNOWN"
    assert pending["recovery_read_only_heartbeats"] == pending["heartbeats"] == [100]
    assert pending["restored_worker"]["process_run_id"] == "run-restored"
    assert d.journal.get(d.operation_id).record.effect.state.value == "sent_unknown"
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", d.arguments)["operation_id"] == d.operation_id
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", {**d.arguments, "idempotency_key": "competing-frozen-recovery"})["status"] == "BUSY"
    assert d.bridge.actions == d.actions
    progress["sequence"] = 101
    _drive(d.app, d.operation_id, 1)
    settled = _assert_recovery_terminal_proof(d)
    assert settled["heartbeats"] == [100, 101]
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", d.arguments)["operation_id"] == d.operation_id
    assert d.bridge.actions == d.actions


@pytest.mark.parametrize("blocker", ("receipt", "digest", "selected-runtime", "binding", "policy", "producer-origin"))
def test_release_expired_recovery_keeps_lane_until_original_evidence_and_authority_return(expired_release_recovery, monkeypatch, blocker):
    d = expired_release_recovery
    d.recovery_gate["readable"] = blocker != "receipt"
    receipt = d.bridge.receipts["r-" + d.operation_id]
    digest = receipt["intent_digest"]
    selected = d.bridge.selected
    original_policy = d.settings.operating_policy_path.read_bytes()
    observe_original = d.bridge.observe_worker
    if blocker == "digest":
        receipt["intent_digest"] = d.snapshot["steps"]["start"]["expected_digest"]
    elif blocker == "selected-runtime":
        d.bridge.selected = "unrelated"
    elif blocker == "policy":
        assert b"profile: full" in original_policy
        d.settings.operating_policy_path.write_bytes(original_policy.replace(b"profile: full", b"profile: observe"))
    elif blocker in {"binding", "producer-origin"}:

        def unavailable(nonce=None):
            result = observe_original(nonce)
            if blocker == "binding":
                result.data.update(binding_status="UNAVAILABLE", runtime_binding=None)
            else:
                result.data["observation"]["heartbeat"]["stage"] = "diagnostic:read"
            return result

        monkeypatch.setattr(d.bridge, "observe_worker", unavailable)
    _drive(d.app, d.operation_id, 8)
    _assert_recovery_lane_retained(d, "NEEDS_ATTENTION")
    receipt["intent_digest"] = digest
    d.bridge.selected = selected
    d.settings.operating_policy_path.write_bytes(original_policy)
    monkeypatch.setattr(d.bridge, "observe_worker", observe_original)
    d.recovery_gate["readable"] = True
    _drive(d.app, d.operation_id, 8)
    _assert_recovery_terminal_proof(d)


@pytest.mark.parametrize("failure", ("snapshot", "terminal-transition"))
def test_release_expired_recovery_does_not_release_lane_before_durable_terminal_proof(expired_release_recovery, monkeypatch, failure):
    from auto_coder.management.operations import MutationResult

    d = expired_release_recovery
    d.recovery_gate["readable"] = True
    lifecycle = d.app.state.management_runtime.lifecycle
    save_original = lifecycle._save
    resolve_original = d.journal.resolve_needs_attention
    if failure == "snapshot":

        def deny_terminal(record, revision, snapshot):
            return False if snapshot.get("stage") == "FAILED" else save_original(record, revision, snapshot)

        monkeypatch.setattr(lifecycle, "_save", deny_terminal)
    else:
        monkeypatch.setattr(d.journal, "resolve_needs_attention", lambda *args, **kwargs: MutationResult(MutationStatus.UNAVAILABLE))
    _drive(d.app, d.operation_id, 8)
    record = d.journal.get(d.operation_id).record
    assert record.phase is OperationPhase.NEEDS_ATTENTION and record.effect.state.value == "sent_unknown"
    assert record.effect.receipt is None
    assert lifecycle.project(record)["reason"] == "lifecycle_deadline_exceeded"
    assert d.journal.lifecycle_snapshot(d.operation_id)[1]["stage"] == ("NEEDS_ATTENTION" if failure == "snapshot" else "FAILED")
    assert _call(d.client, d.token, "auto_coder_deploy_release_source", {**d.arguments, "idempotency_key": "competing-undurable-terminal"})["status"] == "BUSY"
    assert d.bridge.actions == d.actions
    monkeypatch.setattr(lifecycle, "_save", save_original)
    monkeypatch.setattr(d.journal, "resolve_needs_attention", resolve_original)
    _drive(d.app, d.operation_id, 8)
    _assert_recovery_terminal_proof(d)


@pytest.mark.parametrize("mismatch", ("row-phase", "effect-state", "correlation", "source", "outcome", "snapshot-phase", "snapshot-reason"))
def test_release_terminal_reason_requires_matching_durable_receipt(expired_release_recovery, mismatch):
    d = expired_release_recovery
    d.recovery_gate["readable"] = True
    _drive(d.app, d.operation_id, 8)
    snapshot = _assert_recovery_terminal_proof(d)
    record = d.journal.get(d.operation_id).record
    receipt = record.effect.receipt
    assert receipt is not None and record.safe_error_category == "lifecycle_deadline_exceeded"
    if mismatch == "row-phase":
        record = replace(record, phase=OperationPhase.NEEDS_ATTENTION)
    elif mismatch == "effect-state":
        record = replace(record, effect=replace(record.effect, state=EffectState.SENT_UNKNOWN))
    elif mismatch == "correlation":
        record = replace(record, effect=replace(record.effect, receipt=replace(receipt, correlation_identity="other-effect")))
    elif mismatch == "source":
        record = replace(record, effect=replace(record.effect, receipt=replace(receipt, observation_source="unrelated-producer")))
    elif mismatch == "outcome":
        record = replace(record, effect=replace(record.effect, receipt=replace(receipt, safe_result="unrelated-outcome")))
    elif mismatch == "snapshot-phase":
        snapshot = {**snapshot, "stage": "SUCCEEDED"}
    else:
        snapshot = {**snapshot, "reason": "unrelated-snapshot"}
    assert d.app.state.management_runtime.lifecycle.project_snapshot(record, snapshot)["reason"] == "lifecycle_deadline_exceeded"


def test_release_expired_recovery_new_login_cannot_replace_revoked_original_authority(expired_release_recovery):
    d = expired_release_recovery
    original_token = d.token
    d.token = _token(d.client, d)
    d.grants.revoke("alice-client", original_token)
    d.recovery_gate["readable"] = True
    _drive(d.app, d.operation_id, 12)
    _assert_recovery_lane_retained(d, "NEEDS_ATTENTION")
    record = d.journal.get(d.operation_id).record
    assert record.phase is OperationPhase.NEEDS_ATTENTION and record.effect.receipt is None
    assert record.authority == d.original.authority
    assert "recovery_read_only_heartbeats" not in d.journal.lifecycle_snapshot(d.operation_id)[1]


@pytest.fixture
def live_failed_release(release_flow, monkeypatch):
    """Controlled signed-boundary evidence, not installed-worker acceptance."""
    d = release_flow
    observe = d.bridge.observe_worker
    failure = {"failure_id": "f" * 32, "reason": "startup_reconciliation_abandoned"}

    def failed_observation(nonce=None):
        result = observe(nonce)
        if d.bridge.selected == "runtime-new":
            worker = result.data["observation"]
            worker["lifecycle"] = {"state": "failed_awaiting_drain", "startup_reconciled": False, "daemon_serving": False, "startup_failure": dict(failure)}
            worker["invocations"] = {"state": "draining", "unsettled_count": 1, "is_graceful_ready": False}
        return result

    monkeypatch.setattr(d.bridge, "observe_worker", failed_observation)
    return SimpleNamespace(**vars(d), failure=failure)


def _reach_release_stage(d, operation_id, stage):
    runtime = d.app.state.management_runtime
    for _ in range(40):
        snapshot = runtime.journal.lifecycle_snapshot(operation_id)
        if snapshot is not None and snapshot[1]["stage"] == stage:
            return snapshot[1]
        runtime.lifecycle.process(runtime.journal.get(operation_id).record)
    raise AssertionError(f"original release did not reach {stage}: {runtime.journal.lifecycle_snapshot(operation_id)}")


def test_release_failed_live_candidate_retains_original_drain_across_reconstruction(live_failed_release, monkeypatch):
    import copy

    d = live_failed_release
    dispatch = d.bridge.dispatch_worker_operation
    barrier = {"held": True}

    def hold_failed_drain(operation_id, operation, **kwargs):
        worker = d.bridge.worker
        result = dispatch(operation_id, operation, **kwargs)
        if operation_id.startswith("f-"):
            assert operation == "REQUEST_GRACEFUL_STOP"
            assert kwargs["process_run_id"] == "run-new" and kwargs["deployment_generation"] == "generation-new"
            d.bridge.worker = worker
            d.bridge.receipts[operation_id]["status"] = "DRAINING"
        return result

    monkeypatch.setattr(d.bridge, "dispatch_worker_operation", hold_failed_drain)
    read = d.bridge.read_worker_operation

    def lose_failed_drain_receipt(operation_id):
        if operation_id.startswith("f-") and barrier["held"]:
            return d.bridge._result("UNKNOWN", {})
        return read(operation_id)

    monkeypatch.setattr(d.bridge, "read_worker_operation", lose_failed_drain_receipt)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", args)["operation_id"]
        reserved = _reach_release_stage(d, operation_id, "RECOVERY_DRAINING")
        assert reserved["recovery_attempts"] == 1 and reserved["startup_failure"] == d.failure
        assert "recovery_drain" not in reserved["steps"]
        _drive(d.app, operation_id, 10)
        journal = d.app.state.management_runtime.journal
        original = copy.deepcopy(journal.lifecycle_snapshot(operation_id)[1])
        original_record = journal.get(operation_id).record
        actions = list(d.bridge.actions)
        assert actions[-1] == ("REQUEST_GRACEFUL_STOP", "f-" + operation_id)
        assert original["steps"]["recovery_drain"]["state"] == "SENT_UNKNOWN"
        d.bridge.supervisor_generation = "reconstructed-failed-drain-supervisor"
        for _ in range(3):
            held = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
            assert held["status"] == "RECOVERY_DRAINING" and held["recovery_attempts"] == 1
            assert held["steps"] == original["steps"] and held["deadline"] == original["deadline"]
            assert _call(client, token, "auto_coder_deploy_release_source", args)["operation_id"] == operation_id
            assert _call(client, token, "auto_coder_deploy_release_source", {**args, "idempotency_key": "competing"})["status"] == "BUSY"
            assert d.bridge.actions == actions
        barrier["held"] = False
        # The real Supervisor retains DRAINING even after a worker terminal
        # receipt exists, until the separate OS lifetime observation is absent.
        _drive(d.app, operation_id, 3)
        assert d.bridge.actions == actions
        d.bridge.worker = None
        d.bridge.receipts["f-" + operation_id]["status"] = "STOPPED"
        _drive(d.app, operation_id, 35)
        done = d.app.state.management_runtime.lifecycle.project(journal.get(operation_id).record)
        assert done["status"] == "FAILED" and done["recovery_state"] == "CONFIRMED", done
        assert done["reason"] == "new_worker_failed_previous_runtime_restored"
        assert done["recovery_attempts"] == 1 and done["original_deadline"] == original["original_deadline"]
        assert d.bridge.actions == [*actions, ("RECOVER_PREVIOUS", "r-" + operation_id)]
        assert done["steps"]["recovery_drain"]["expected_digest"] == original["steps"]["recovery_drain"]["expected_digest"]
        finished_record = journal.get(operation_id).record
        assert finished_record.authority == original_record.authority
        assert finished_record.effect.attempt_id == original_record.effect.attempt_id
        assert finished_record.effect.correlation_identity == original_record.effect.correlation_identity


@pytest.mark.parametrize("boundary", ["RECOVERY_DRAINING", "RESTORING"])
@pytest.mark.parametrize("loss", ["grant", "policy"])
def test_failed_live_candidate_checks_original_authority_before_each_recovery_effect(live_failed_release, boundary, loss):
    d = live_failed_release
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", _release_arguments(d, promotion))["operation_id"]
        _reach_release_stage(d, operation_id, boundary)
        actions = list(d.bridge.actions)
        if loss == "grant":
            with d.grants.store.connect() as db:
                db.execute("UPDATE grants SET revoked=?", (d.state["now"],))
        else:
            policy = d.settings.operating_policy_path.read_text(encoding="utf-8")
            d.settings.operating_policy_path.write_text(policy.replace("profile: full", "profile: observe"), encoding="utf-8")
        _drive(d.app, operation_id, 20)
        runtime = d.app.state.management_runtime
        result = runtime.lifecycle.project(runtime.journal.get(operation_id).record)
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["reason"] == ("original_authority_denied" if loss == "grant" else "current_policy_denied")
        assert result["recovery_attempts"] == 1 and d.bridge.actions == actions


@pytest.mark.parametrize("damage", ["missing", "malformed", "serving", "reconciled", "gate-running", "wrong-run", "wrong-source", "unverified", "binding-run", "foreign-supervisor"])
def test_live_release_recovery_requires_exact_typed_failed_candidate(live_failed_release, monkeypatch, damage):
    d = live_failed_release
    observe = d.bridge.observe_worker

    def corrupted(nonce=None):
        result = observe(nonce)
        if d.bridge.selected == "runtime-new":
            worker = result.data["observation"]
            if damage == "missing":
                worker["lifecycle"].pop("startup_failure")
            elif damage == "malformed":
                worker["lifecycle"]["startup_failure"]["failure_id"] = "timeout"
            elif damage == "serving":
                worker["lifecycle"]["daemon_serving"] = True
            elif damage == "reconciled":
                worker["lifecycle"]["startup_reconciled"] = True
            elif damage == "gate-running":
                worker["invocations"]["state"] = "running"
            elif damage == "wrong-run":
                worker["process_run_id"] = "foreign"
            elif damage == "wrong-source":
                worker["artifact"]["source_revision"]["value"] = "0" * 40
            elif damage == "unverified":
                result.data["binding_status"] = "UNAVAILABLE"
            elif damage == "binding-run":
                result.data["runtime_binding"]["process_run_id"] = "foreign"
            elif damage == "foreign-supervisor":
                result = replace(result, supervisor_generation="foreign-supervisor")
        return result

    monkeypatch.setattr(d.bridge, "observe_worker", corrupted)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", _release_arguments(d, promotion))["operation_id"]
        _drive(d.app, operation_id, 30)
        result = _call(client, token, "auto_coder_operation_get", {"target_id": "release-worker", "operation_id": operation_id})
        assert result["status"] == "RECONCILING" and result["recovery_attempts"] == 0
        assert [name for name, _ in d.bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]


@pytest.fixture
def expired_live_failure_drain(live_failed_release, monkeypatch):
    import copy
    import time

    d = live_failed_release
    dispatch = d.bridge.dispatch_worker_operation

    def hold_original_drain(operation_id, operation, **kwargs):
        worker = d.bridge.worker
        response = dispatch(operation_id, operation, **kwargs)
        if operation_id.startswith("f-"):
            d.bridge.worker = worker
            d.bridge.receipts[operation_id]["status"] = "DRAINING"
        return response

    monkeypatch.setattr(d.bridge, "dispatch_worker_operation", hold_original_drain)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", args)["operation_id"]
        _reach_release_stage(d, operation_id, "RECOVERY_DRAINING")
        _drive(d.app, operation_id, 5)
        runtime = d.app.state.management_runtime
        revision, snapshot = runtime.journal.lifecycle_snapshot(operation_id)
        snapshot["deadline"] = time.time() - 1
        assert runtime.journal.lifecycle_save(operation_id, revision, snapshot)
        original = copy.deepcopy(snapshot)
        actions = list(d.bridge.actions)
        d.bridge.supervisor_generation = "reconstructed-expired-drain"
        _drive(d.app, operation_id, 3)
        assert runtime.journal.get(operation_id).record.phase is OperationPhase.NEEDS_ATTENTION
        assert runtime.journal.lifecycle_snapshot(operation_id)[1]["steps"] == original["steps"]
        yield SimpleNamespace(**vars(d), client=client, token=token, arguments=args, operation_id=operation_id, runtime=runtime, snapshot=original, actions=actions)


def test_expired_recovery_drain_settles_original_failure_without_launch_or_deadline_reset(expired_live_failure_drain):
    d = expired_live_failure_drain
    d.bridge.worker = None
    d.bridge.receipts["f-" + d.operation_id]["status"] = "STOPPED"
    _drive(d.app, d.operation_id, 10)
    result = d.runtime.lifecycle.project(d.runtime.journal.get(d.operation_id).record)
    assert result["status"] == "FAILED" and result["recovery_state"] == "NOT_LAUNCHED", result
    assert result["reason"] == "failed_candidate_stopped_recovery_not_launched"
    assert result["recovery_attempts"] == 1 and "recovery" not in result["steps"]
    assert result["deadline"] == d.snapshot["deadline"] and result["original_deadline"] == d.snapshot["original_deadline"]
    assert result["steps"]["recovery_drain"]["state"] == "CONFIRMED"
    for field in ("operation_id", "sent_at", "owner_generation", "attempt_id", "correlation_identity", "expected_digest"):
        assert result["steps"]["recovery_drain"][field] == d.snapshot["steps"]["recovery_drain"][field]
    assert d.bridge.actions == d.actions
    settled = d.runtime.journal.get(d.operation_id).record
    assert settled.effect.state is EffectState.CONFIRMED
    assert settled.effect.receipt.correlation_identity == settled.effect.correlation_identity
    assert settled.effect.receipt.observation_source == "supervisor-and-worker-lifecycle-journal"
    for _ in range(3):
        replay = _call(d.client, d.token, "auto_coder_deploy_release_source", d.arguments)
        assert replay["operation_id"] == d.operation_id and replay["status"] == "FAILED"
        assert d.bridge.actions == d.actions


@pytest.mark.parametrize("damage", ["receipt-unavailable", "digest", "live-child", "selected-runtime", "other-unknown", "grant", "grant-after-proof", "snapshot-write", "terminal-write"])
def test_expired_failed_drain_keeps_lane_until_complete_original_proof_is_durable(expired_live_failure_drain, monkeypatch, damage):
    d = expired_live_failure_drain
    if damage != "live-child":
        d.bridge.worker = None
    d.bridge.receipts["f-" + d.operation_id]["status"] = "STOPPED"
    if damage == "receipt-unavailable":
        monkeypatch.setattr(d.bridge, "read_worker_operation", lambda _operation_id: d.bridge._result("UNAVAILABLE", {}))
    elif damage == "digest":
        d.bridge.receipts["f-" + d.operation_id]["intent_digest"] = "foreign"
    elif damage == "selected-runtime":
        d.bridge.selected = "foreign"
    elif damage == "other-unknown":
        revision, snapshot = d.runtime.journal.lifecycle_snapshot(d.operation_id)
        snapshot["steps"]["start"]["state"] = "SENT_UNKNOWN"
        assert d.runtime.journal.lifecycle_save(d.operation_id, revision, snapshot)
    elif damage == "grant":
        with d.grants.store.connect() as db:
            db.execute("UPDATE grants SET revoked=?", (d.state["now"],))
    elif damage == "grant-after-proof":
        read = d.bridge.read_worker_operation

        def revoke_during_original_read(operation_id):
            result = read(operation_id)
            with d.grants.store.connect() as db:
                db.execute("UPDATE grants SET revoked=?", (d.state["now"],))
            return result

        monkeypatch.setattr(d.bridge, "read_worker_operation", revoke_during_original_read)
    elif damage == "snapshot-write":
        save = d.runtime.lifecycle._save
        monkeypatch.setattr(d.runtime.lifecycle, "_save", lambda record, revision, snapshot: False if snapshot.get("stage") == "FAILED" else save(record, revision, snapshot))
    elif damage == "terminal-write":
        from auto_coder.management.operation_store import MutationResult

        resolve = d.runtime.journal.resolve_needs_attention
        monkeypatch.setattr(d.runtime.journal, "resolve_needs_attention", lambda *_args, **_kwargs: MutationResult(MutationStatus.UNAVAILABLE))
    _drive(d.app, d.operation_id, 10)
    record = d.runtime.journal.get(d.operation_id).record
    assert record.phase is OperationPhase.NEEDS_ATTENTION and record.effect.state is EffectState.SENT_UNKNOWN
    snapshot = d.runtime.journal.lifecycle_snapshot(d.operation_id)[1]
    assert snapshot["deadline"] == d.snapshot["deadline"] and snapshot["original_deadline"] == d.snapshot["original_deadline"]
    assert "recovery" not in snapshot["steps"] and d.bridge.actions == d.actions
    if damage not in {"grant", "grant-after-proof"}:
        competing = _call(d.client, d.token, "auto_coder_deploy_release_source", {**d.arguments, "idempotency_key": "expired-competing"})
        assert competing["status"] == "BUSY" and competing["operation_id"] == d.operation_id
    if damage in {"snapshot-write", "terminal-write"}:
        if damage == "snapshot-write":
            monkeypatch.setattr(d.runtime.lifecycle, "_save", save)
        else:
            monkeypatch.setattr(d.runtime.journal, "resolve_needs_attention", resolve)
        _drive(d.app, d.operation_id, 6)
        completed = d.runtime.lifecycle.project(d.runtime.journal.get(d.operation_id).record)
        assert completed["status"] == "FAILED" and completed["recovery_state"] == "NOT_LAUNCHED"
        assert d.bridge.actions == d.actions


@pytest.mark.parametrize("boundary", ["reservation", "recovery_drain"])
def test_live_failed_recovery_persistence_failure_never_dispatches(live_failed_release, monkeypatch, boundary):
    d = live_failed_release
    runtime = d.app.state.management_runtime
    save = runtime.lifecycle._save

    def unavailable(record, revision, snapshot):
        if snapshot.get("stage") == "RECOVERY_DRAINING" and (boundary == "reservation" or "recovery_drain" in snapshot["steps"]):
            return False
        return save(record, revision, snapshot)

    monkeypatch.setattr(runtime.lifecycle, "_save", unavailable)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", _release_arguments(d, promotion))["operation_id"]
        _drive(d.app, operation_id, 35)
        persisted = runtime.journal.lifecycle_snapshot(operation_id)[1]
        assert persisted["stage"] == ("RECONCILING" if boundary == "reservation" else "RECOVERY_DRAINING")
        assert persisted["recovery_attempts"] == (0 if boundary == "reservation" else 1)
        assert "recovery_drain" not in persisted["steps"]
        assert [name for name, _ in d.bridge.actions] == ["PREPARE", "REQUEST_GRACEFUL_STOP", "START"]


@pytest.mark.parametrize("blocker", [None, "authority", "policy", "persistence", "candidate-generation", "prior-unverified", "budget-reserved", "missing-drain", "drain-unknown", "drain-digest", "ready", "partial", "unavailable"])
def test_expired_release_candidate_reserves_one_original_reboot_budget(release_flow, monkeypatch, blocker):
    """Controlled contract test; the installed case waits the real deadline."""
    import time

    d = release_flow
    observe = d.bridge.observe_worker

    def unready(nonce=None):
        if d.bridge.selected != "runtime-new":
            return observe(nonce)
        if blocker == "unavailable":
            return d.bridge._result("UNAVAILABLE", {})
        result = observe(nonce)
        lifecycle = result.data["observation"]["lifecycle"]
        lifecycle.update(startup_reconciled=blocker == "ready", daemon_serving=blocker == "ready")
        if blocker == "partial":
            lifecycle.pop("startup_reconciled")
        return result

    monkeypatch.setattr(d.bridge, "observe_worker", unready)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        arguments = _release_arguments(d, promotion)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", arguments)["operation_id"]
        _reach_release_stage(d, operation_id, "RECONCILING")
        runtime = d.app.state.management_runtime
        journal = runtime.journal
        version, snapshot = journal.lifecycle_snapshot(operation_id)
        assert snapshot["steps"]["drain"]["state"] == snapshot["steps"]["start"]["state"] == "CONFIRMED"
        snapshot["deadline"] = time.time() - 1
        if blocker == "prior-unverified":
            snapshot["previous_runtime_verified"] = False
        if blocker == "budget-reserved":
            snapshot["recovery_attempts"] = 1
        if blocker == "missing-drain":
            snapshot["steps"].pop("drain")
        elif blocker == "drain-unknown":
            snapshot["steps"]["drain"]["state"] = "SENT_UNKNOWN"
        elif blocker == "drain-digest":
            snapshot["steps"]["drain"]["observation"]["intent_digest"] = "wrong"
        assert journal.lifecycle_save(operation_id, version, snapshot)
        original_record = journal.get(operation_id).record
        actions = list(d.bridge.actions)
        if blocker == "authority":
            monkeypatch.setattr(runtime.lifecycle, "_authority_refusal", lambda record: "original_authority_denied")
        elif blocker == "policy":
            monkeypatch.setattr(runtime, "retained_effect_permitted", lambda record: False)
        elif blocker == "persistence":
            monkeypatch.setattr(journal, "lifecycle_save", lambda *args, **kwargs: False)
        elif blocker == "candidate-generation":
            d.bridge.worker["deployment_generation"] = "different-candidate"
        runtime.lifecycle.process(original_record)
        retained = journal.lifecycle_snapshot(operation_id)[1]
        assert d.bridge.actions == actions
        assert retained["original_deadline"] == snapshot["original_deadline"]
        assert retained["steps"] == snapshot["steps"]
        assert journal.get(operation_id).record.intent == original_record.intent
        assert journal.get(operation_id).record.authority == original_record.authority
        if blocker is not None:
            assert retained["deadline"] == snapshot["deadline"]
            assert retained["recovery_attempts"] == snapshot["recovery_attempts"]
            assert retained["stage"] == ("RECONCILING" if blocker == "persistence" else "NEEDS_ATTENTION")
            assert retained.get("recovery_method") is None
            return
        assert retained["stage"] == "RESTORING" and retained["recovery_method"] == "REBOOT" and retained["recovery_attempts"] == 1
        assert retained["deadline"] > time.time()
        assert runtime.lifecycle.project(journal.get(operation_id).record)["recovery_method"] == "REBOOT"
        dispatch = d.bridge.dispatch_worker_operation

        def lose_reboot(operation_id, operation, **kwargs):
            if operation == "RECOVER_PREVIOUS_REBOOT":
                d.bridge.actions.append((operation, operation_id))
                return d.bridge._result("UNKNOWN", {})
            return dispatch(operation_id, operation, **kwargs)

        monkeypatch.setattr(d.bridge, "dispatch_worker_operation", lose_reboot)
        monkeypatch.setattr(d.bridge, "read_worker_operation", lambda operation_id: d.bridge._result("UNAVAILABLE", {}))
        _drive(d.app, operation_id, 6)
        sent = journal.lifecycle_snapshot(operation_id)[1]
        assert sent["steps"]["recovery"]["state"] == "SENT_UNKNOWN"
        assert sent["steps"]["recovery"]["operation_id"] == "r-" + operation_id
        assert d.bridge.actions == actions + [("RECOVER_PREVIOUS_REBOOT", "r-" + operation_id)]
        assert sent["deadline"] == retained["deadline"] and sent["recovery_attempts"] == 1
        assert _call(client, token, "auto_coder_deploy_release_source", arguments)["operation_id"] == operation_id
        assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "competing-reboot"})["status"] == "BUSY"
        assert journal.lifecycle_snapshot(operation_id)[1] == sent
        version, expired = journal.lifecycle_snapshot(operation_id)
        expired["deadline"] = time.time() - 1
        assert journal.lifecycle_save(operation_id, version, expired)
        _drive(d.app, operation_id, 8)
        attention = journal.lifecycle_snapshot(operation_id)[1]
        assert attention["stage"] == "NEEDS_ATTENTION" and attention["recovery_method"] == "REBOOT"
        assert attention["deadline"] == expired["deadline"] and attention["recovery_attempts"] == 1
        assert attention["steps"] == sent["steps"]
        assert d.bridge.actions == actions + [("RECOVER_PREVIOUS_REBOOT", "r-" + operation_id)]
        assert journal.get(operation_id).record.effect.state is EffectState.SENT_UNKNOWN
        assert _call(client, token, "auto_coder_deploy_release_source", {**arguments, "idempotency_key": "unavailable-restart-competing"})["status"] == "BUSY"


@pytest.mark.parametrize("boundary", ["before-step", "after-durable-step"])
@pytest.mark.parametrize("readiness", ["ready", "unavailable"])
def test_reboot_rechecks_candidate_before_dispatch_without_releasing_original_lane(release_flow, monkeypatch, boundary, readiness):
    import time

    d = release_flow
    observe = d.bridge.observe_worker
    change = {"value": False}

    def observe_candidate(nonce=None):
        if d.bridge.selected == "runtime-new" and change["value"] and readiness == "unavailable":
            return d.bridge._result("UNAVAILABLE", {})
        result = observe(nonce)
        if d.bridge.selected == "runtime-new":
            result.data["observation"]["lifecycle"].update(startup_reconciled=change["value"], daemon_serving=change["value"])
        return result

    monkeypatch.setattr(d.bridge, "observe_worker", observe_candidate)
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        operation_id = _call(client, token, "auto_coder_deploy_release_source", args)["operation_id"]
        _reach_release_stage(d, operation_id, "RECONCILING")
        runtime = d.app.state.management_runtime
        journal = runtime.journal
        version, snapshot = journal.lifecycle_snapshot(operation_id)
        snapshot["deadline"] = time.time() - 1
        assert journal.lifecycle_save(operation_id, version, snapshot)
        runtime.lifecycle.process(journal.get(operation_id).record)
        reserved = journal.lifecycle_snapshot(operation_id)[1]
        assert reserved["stage"] == "RESTORING" and reserved["recovery_attempts"] == 1
        if boundary == "before-step":
            change["value"] = True
        else:
            save = journal.lifecycle_save

            def changed_after_write(operation_id, version, state):
                saved = save(operation_id, version, state)
                if saved and "recovery" in state.get("steps", {}):
                    change["value"] = True
                return saved

            monkeypatch.setattr(journal, "lifecycle_save", changed_after_write)
            monkeypatch.setattr(d.bridge, "read_worker_operation", lambda operation_id: d.bridge._result("UNAVAILABLE", {}))
        actions = list(d.bridge.actions)
        _drive(d.app, operation_id, 6)
        retained = journal.lifecycle_snapshot(operation_id)[1]
        assert d.bridge.actions == actions and retained["deadline"] == reserved["deadline"] and retained["recovery_attempts"] == 1
        if boundary == "before-step":
            assert retained["stage"] == "NEEDS_ATTENTION" and "recovery" not in retained["steps"]
        else:
            assert retained["stage"] == "RESTORING" and retained["steps"]["recovery"]["state"] == "SENT_UNKNOWN"
        assert _call(client, token, "auto_coder_deploy_release_source", {**args, "idempotency_key": "changed-readiness-competing"})["status"] == "BUSY"


def test_reboot_is_not_admitted_for_originally_stopped_release(release_flow):
    import time

    d = release_flow
    d.bridge.worker = None
    with TestClient(d.app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        promotion = _call(client, token, MCP_PROMOTION_ACTION, d.args)
        args = _release_arguments(d, promotion)
        args.pop("expected_process_run_id")
        args.pop("expected_deployment_generation")
        operation_id = _call(client, token, "auto_coder_deploy_release_source", args)["operation_id"]
        _reach_release_stage(d, operation_id, "RECONCILING")
        runtime = d.app.state.management_runtime
        version, snapshot = runtime.journal.lifecycle_snapshot(operation_id)
        assert snapshot["incumbent"]["process_run_id"] is None and "drain" not in snapshot["steps"]
        snapshot["deadline"] = time.time() - 1
        assert runtime.journal.lifecycle_save(operation_id, version, snapshot)
        _drive(d.app, operation_id, 5)
        retained = runtime.journal.lifecycle_snapshot(operation_id)[1]
        assert retained["stage"] == "NEEDS_ATTENTION" and retained["recovery_attempts"] == 0
        assert retained["deadline"] == snapshot["deadline"]
        assert [name for name, _ in d.bridge.actions] == ["PREPARE", "START"]
