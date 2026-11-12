"""Offline negative consumer regressions, never live end-to-end acceptance.

The HTTP cases exercise actual OAuth/grant, current policy and SQLite producers;
the existing Firebase network-edge fixture supplies offline identity inputs.
"""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import dot_mcp_acceptance as accept
from scripts import dot_mcp_negative_acceptance as negative
from scripts import management_mcp_rollout_smoke as smoke
from starlette.testclient import TestClient

from auto_coder.management.app import create_management_app
from auto_coder.management.mcp_runtime_tools import ManagementRuntime
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import AcceptedAuthority, ActionParameter, EffectAttempt, EffectState, ManagedTarget, OperationIntent, OperationPhase, OperationRecord
from auto_coder.management.source_release_dispatch import SourceReleaseDispatch
from auto_coder.management.worker_lifecycle import WorkerLifecycle
from tests.test_dot_mcp_acceptance import checkpoint as saved_checkpoint
from tests.test_dot_mcp_acceptance import fixture as disposable_fixture
from tests.test_management_mcp import _issue, real_boundary  # noqa: F401
from tests.test_management_runtime_mcp import _call, runtime_boundary  # noqa: F401


def checkpoint(**changes):
    return saved_checkpoint(release_target=disposable_fixture().target_ids["release"], **changes)


def policy_text(fixture, profile):
    return f"profile: {profile}\ntargets:\n" f"  {fixture.target_ids['beta']}:\n    channel: beta\n    repositories: [{accept.BETA_WORK_REPOSITORY}]\n" f"  {fixture.target_ids['release']}:\n    channel: release\n    repositories: [{accept.TEST_REPOSITORY}]\n"


def test_default_without_fixture_is_not_run_and_creates_only_rotating_log(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(Path(negative.__file__))],
        capture_output=True,
        text=True,
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path), "AUTO_CODER_MCP_ACCESS_TOKEN": "unused"},
        timeout=15,
    )
    assert completed.returncode == 3
    output = json.loads(completed.stdout.splitlines()[-1])
    assert output["verdict"] == "NOT_RUN"
    assert output["effect_dispatched"] is False
    assert output["negative_boundary_verified"] is False
    assert output["positive_deployment_proven"] is False
    assert (tmp_path / "auto-coder/dot-mcp-negative-acceptance.log").is_file()


@pytest.mark.parametrize("pending", ["SENT_UNKNOWN", "NEEDS_ATTENTION"])
def test_refusal_probe_cannot_bypass_original_unknown_with_another_key(pending):
    original = checkpoint(beta_operation_id="original", pending_operation_id="original", pending_effect_state=pending, idempotency_key="different-key")
    with pytest.raises(accept.Blocked, match="original unresolved"):
        negative.selected_checkpoint(disposable_fixture(), original, "observe-refusal", "beta", "operator-v1", probe=True)


def test_refusal_probe_requires_explicit_flag_and_operator_revision():
    original = checkpoint()
    with pytest.raises(accept.Blocked, match="explicit operator selection"):
        negative.selected_checkpoint(disposable_fixture(), original, "observe-refusal", "beta", "operator-v1", probe=False)
    with pytest.raises(accept.Blocked, match="revision"):
        negative.selected_checkpoint(disposable_fixture(), original, "observe-refusal", "beta", "different", probe=True)
    with pytest.raises(accept.Blocked, match="strictly read-only"):
        negative.selected_checkpoint(disposable_fixture(), original, "unknown-effect", "beta", "operator-v1", probe=True)


def test_observe_policy_requires_exact_two_fixture_targets(tmp_path):
    fixture = disposable_fixture()
    path = tmp_path / "operator.yaml"
    path.write_text(policy_text(fixture, "observe"))
    assert negative.observe_policy_revision(fixture, path).startswith("sha256:")
    path.write_text(policy_text(fixture, "full"))
    with pytest.raises(accept.Blocked, match="observe policy"):
        negative.observe_policy_revision(fixture, path)
    path.write_text(policy_text(fixture, "observe").replace(accept.TEST_REPOSITORY, "fixture/other", 1))
    with pytest.raises(accept.Blocked, match="exact two-target"):
        negative.observe_policy_revision(fixture, path)


class HttpSession:
    """Adapter to actual management HTTP; no fabricated MCP result."""

    def __init__(self, client, bearer, *, before=None, lose_response=False):
        self.client = client
        self.bearer = bearer
        self.before = before
        self.lose_response = lose_response
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append(name)
        if name == "auto_coder_reevaluate_target" and self.before:
            self.before()
        value = _call(self.client, self.bearer, name, arguments)
        if name == "auto_coder_reevaluate_target" and self.lose_response:
            raise TimeoutError("original response lost after actual producer call")
        return SimpleNamespace(isError=False, structuredContent=value)


def fixture_app(boundary, profile, *, selected_fixture=None, deploy_scope=False):
    settings, grants, signed, policy, beta, release = boundary
    if deploy_scope:
        scopes = (*settings.oauth.enabled_scopes, "auto-coder.deploy")
        clients = tuple(replace(client, allowed_scopes=(*client.allowed_scopes, "auto-coder.deploy")) for client in settings.oauth.clients)
        oauth = replace(settings.oauth, clients=clients, enabled_scopes=scopes)
        settings = replace(settings, oauth=oauth)
        grants = OAuthGrantService(oauth, grants.identity, clock=grants.clock)
    fixture = selected_fixture or disposable_fixture()
    for role, worker in (("beta", beta), ("release", release)):
        worker.config = replace(worker.config, instance_id=fixture.target_ids[role], repository=accept.BETA_WORK_REPOSITORY if role == "beta" else accept.TEST_REPOSITORY)
    policy.write_text(policy_text(fixture, profile))
    app = create_management_app(settings, grants, worker_clients={"beta": beta, "release": release})
    app.state.management_runtime.journal.get("initialize-empty-journal")
    return fixture, settings, signed, policy, beta, app


def live_session(session):
    return accept.Live(session, {"auto_coder_operation_inspect", "auto_coder_deployment_status", "auto_coder_beta_acceptance_get", "auto_coder_reevaluate_target", "auto_coder_promote_accepted_source_release"})


def test_actual_observe_refusal_has_no_admission_and_preserves_terminal_refused_ticket(runtime_boundary, tmp_path):
    fixture, settings, signed, policy, beta, app = fixture_app(runtime_boundary, "observe")
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.operate")["access_token"]
        session = HttpSession(client, token)
        original = checkpoint()
        report = asyncio.run(negative.refusal_probe(live_session(session), fixture, original, "observe-refusal", 14, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        assert report.verdict == "PASS" and report.negative_boundary_verified
        assert report.effect_dispatched is False and report.positive_deployment_proven is False
        assert beta.send_calls == 0
        assert negative.journal_original_key(settings.operation_state_root / "management-operations.sqlite3", fixture.target_ids["beta"], original.idempotency_key) == ()
        ticket = json.loads(negative.refusal_ticket("observe-refusal", tmp_path).read_text())
        assert ticket["effect_state"] == "REFUSED" and ticket["operation_id"] == ""
        assert ticket["refusal_evidence"]["operation_absent"] is True
        assert session.calls == ["auto_coder_deployment_status", "auto_coder_reevaluate_target"]
        with pytest.raises(accept.Blocked, match="previous admission"):
            asyncio.run(negative.refusal_probe(live_session(session), fixture, original, "observe-refusal", 14, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        assert beta.send_calls == 0


def test_policy_downgrade_requires_actual_prior_reevaluation_receipt(runtime_boundary, tmp_path):
    fixture, settings, signed, policy, beta, app = fixture_app(runtime_boundary, "beta")
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.operate")["access_token"]
        prior = checkpoint(idempotency_key="prior-allowed")
        arguments = {"target_id": fixture.target_ids["beta"], "target_type": "issue", "number": 14, "idempotency_key": prior.idempotency_key, "expected_process_run_id": beta.run, "expected_deployment_generation": beta.deployment}
        path = tmp_path / "dot-fixture-reevaluate-original-send.json"
        accept.persist_original_ticket(path, fixture, prior, "auto_coder_reevaluate_target", arguments)
        admitted = _call(client, token, "auto_coder_reevaluate_target", arguments)
        assert admitted["status"] == "accepted_for_reevaluation"
        accept.update_original_ticket(path, fixture, admitted["operation_id"], "ADMITTED")
        accept.update_original_ticket(path, fixture, admitted["operation_id"], "SUCCEEDED")
        policy.write_text(policy_text(fixture, "observe"))
        session = HttpSession(client, token)
        original = checkpoint(idempotency_key="separate-denial-probe")
        report = asyncio.run(negative.refusal_probe(live_session(session), fixture, original, "policy-downgrade", 14, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        assert report.verdict == "PASS" and beta.send_calls == 1
        assert session.calls == ["auto_coder_operation_inspect", "auto_coder_deployment_status", "auto_coder_reevaluate_target"]
        assert json.loads(path.read_text())["operation_id"] == admitted["operation_id"]


def test_actual_beta_policy_refuses_independently_published_original_source(runtime_boundary, monkeypatch, tmp_path):
    """Real HTTP policy refusal with offline Git/worker producer prerequisites."""
    from auto_coder.management.source_acceptance import BetaAcceptanceHandler, SourceAcceptancePublisher
    from tests import test_beta_runtime_smoke as upstream
    from tests.test_source_release_promotion import _harness

    # The shared deterministic Git edge uses these synthetic namespaces. The
    # actual fixture CLI restrictions are tested separately; no remote is used.
    monkeypatch.setattr(accept, "TEST_REPOSITORY", "owner/repo")
    monkeypatch.setattr(accept, "BETA_WORK_REPOSITORY", "fixture/beta")
    original_intent = upstream.OperationIntent

    def targeted_intent(target, action, parameters, *args):
        return original_intent(target, action, (*parameters, ActionParameter("target_id", target.instance_id)), *args)

    monkeypatch.setattr(upstream, "OperationIntent", targeted_intent)
    remote, producer, request, release_policy = _harness(tmp_path / "upstream")
    publisher = SourceAcceptancePublisher(remote, "owner/repo")
    handler = BetaAcceptanceHandler(None, producer.producer(), publisher)
    original = producer.operation
    begun = producer.journal.begin_effect(original.operation_id, original.revision, owner_attempt_id="publish-owner", attempt_id="publish-original", correlation_identity="publication-correlation").record
    assert begun is not None
    published = publisher.read(original.operation_id)
    assert published is not None
    completed = handler._complete(begun, published)
    assert completed.phase is OperationPhase.SUCCEEDED and completed.effect.state is EffectState.CONFIRMED
    projection_runtime = SimpleNamespace(_project_operation=ManagementRuntime._project_operation, beta_acceptance=handler)

    fixture = replace(disposable_fixture(), repository="owner/repo", source_fixture_sha=request.source_sha, target_ids={"beta": "beta-worker", "release": "release-worker"})
    original = replace(
        checkpoint(),
        source_sha=request.source_sha,
        beta_target=fixture.target_ids["beta"],
        release_target=fixture.target_ids["release"],
        ci_run_id=str(request.ci_run_id),
        ci_run_attempt=str(request.ci_run_attempt),
        tested_source_ref=request.tested_source_ref,
        tested_source_oid=request.tested_source_oid,
        beta_acceptance_ref=request.acceptance_ref,
        beta_acceptance_oid=request.acceptance_oid,
        idempotency_key="original-beta-denied-promotion",
    )
    release_policy_path = tmp_path / "source-release-policy.json"
    release_policy_path.write_text(json.dumps(asdict(release_policy)))
    token_path = tmp_path / "offline-release-token"
    token_path.write_text("unused-offline-placeholder")
    settings, grants, signed, policy, beta, release = runtime_boundary
    settings = replace(settings, source_release_policy_path=release_policy_path, source_release_token_file=token_path, source_release_target_id=fixture.target_ids["release"], source_release_workflow_id=321)
    fixture, settings, signed, policy, beta, app = fixture_app((settings, grants, signed, policy, beta, release), "beta", selected_fixture=fixture, deploy_scope=True)
    monkeypatch.setattr(accept, "_source_client", lambda: remote)

    class PublishedPrerequisiteSession(HttpSession):
        async def call_tool(self, name, arguments):
            if name == "auto_coder_beta_acceptance_get":
                self.calls.append(name)
                assert arguments == {"target_id": fixture.target_ids["beta"], "operation_id": completed.operation_id}
                value = ManagementRuntime._project_beta_acceptance(projection_runtime, completed)
                return SimpleNamespace(isError=False, structuredContent=value)
            return await super().call_tool(name, arguments)

    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.deploy")["access_token"]
        session = PublishedPrerequisiteSession(client, bearer)
        remote.calls.clear()
        report = asyncio.run(negative.refusal_probe(live_session(session), fixture, original, "beta-release-refusal", 0, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        assert report.verdict == "PASS" and report.negative_boundary_verified
        assert report.effect_dispatched is False and report.positive_deployment_proven is False
        assert session.calls == ["auto_coder_beta_acceptance_get", "auto_coder_promote_accepted_source_release"]
        retained = json.loads(negative.refusal_ticket("beta-release-refusal", tmp_path).read_text())
        assert retained["target"] == fixture.target_ids["release"]
        assert retained["effect_state"] == "REFUSED" and retained["operation_id"] == ""
        assert retained["refusal_evidence"]["reason"] == "release_policy_denied"
        assert negative.journal_original_key(settings.operation_state_root / "management-operations.sqlite3", fixture.target_ids["release"], original.idempotency_key) == ()
        assert remote.calls and all(method == "GET" for method, _ in remote.calls)
        assert beta.send_calls == release.send_calls == 0


def test_beta_release_refusal_requires_real_source_before_ticket_or_request(runtime_boundary, monkeypatch, tmp_path):
    from tests.test_source_acceptance import Git

    fixture, settings, signed, policy, _beta, app = fixture_app(runtime_boundary, "beta", deploy_scope=True)
    monkeypatch.setattr(accept, "_source_client", lambda: Git())
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        bearer = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.deploy")["access_token"]
        session = HttpSession(client, bearer)
        with pytest.raises(accept.Blocked, match="exact immutable tested-source"):
            asyncio.run(negative.refusal_probe(live_session(session), fixture, checkpoint(), "beta-release-refusal", 0, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        assert session.calls == []
        assert not negative.refusal_ticket("beta-release-refusal", tmp_path).exists()


@pytest.mark.parametrize("stage", ["observe", "full"])
def test_beta_release_probe_requires_explicit_current_beta_stage(stage):
    with pytest.raises(accept.Blocked, match="operator-selected beta stage"):
        negative.selected_checkpoint(disposable_fixture(), checkpoint(stage=stage), "beta-release-refusal", stage, "operator-v1", probe=True)


def test_unexpected_actual_admission_is_failed_and_original_ticket_remains_held(runtime_boundary, tmp_path):
    fixture, settings, signed, policy, beta, app = fixture_app(runtime_boundary, "observe")
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.operate")["access_token"]
        session = HttpSession(client, token, before=lambda: policy.write_text(policy_text(fixture, "beta")))
        with pytest.raises(accept.Failed, match="admitted an operation"):
            asyncio.run(negative.refusal_probe(live_session(session), fixture, checkpoint(), "observe-refusal", 14, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        record = json.loads(negative.refusal_ticket("observe-refusal", tmp_path).read_text())
        assert record["effect_state"] == "ADMITTED" and record["operation_id"]
        report = negative.exception_report("observe-refusal", "unexpected admission", verdict="FAIL", root=tmp_path)
        assert report.operation_id == record["operation_id"] and report.effect_dispatched is None
        assert beta.send_calls == 1


def test_lost_actual_refusal_response_never_reports_no_send(runtime_boundary, tmp_path):
    fixture, settings, signed, policy, beta, app = fixture_app(runtime_boundary, "observe")
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.operate")["access_token"]
        with pytest.raises(TimeoutError):
            asyncio.run(negative.refusal_probe(live_session(HttpSession(client, token, lose_response=True)), fixture, checkpoint(), "observe-refusal", 14, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        report = negative.exception_report("observe-refusal", "lost", root=tmp_path)
        assert report.effect_dispatched is None and report.verdict == "BLOCKED"
        assert json.loads(negative.refusal_ticket("observe-refusal", tmp_path).read_text())["effect_state"] == "ADMISSION_UNKNOWN"
        assert beta.send_calls == 0


def test_busy_response_does_not_adopt_another_original_operation(runtime_boundary, tmp_path):
    fixture, settings, signed, policy, beta, app = fixture_app(runtime_boundary, "beta")
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "alice", "alice-client", "auto-coder.read auto-coder.operate")["access_token"]
        beta.drop_before_receipt = True
        arguments = {"target_id": fixture.target_ids["beta"], "target_type": "issue", "number": 14, "idempotency_key": "another-original-key", "expected_process_run_id": beta.run, "expected_deployment_generation": beta.deployment}
        outstanding = _call(client, token, "auto_coder_reevaluate_target", arguments)
        assert outstanding["operation_id"] and outstanding["effect_state"] == "sent_unknown"
        policy.write_text(policy_text(fixture, "observe"))
        session = HttpSession(client, token, before=lambda: policy.write_text(policy_text(fixture, "beta")))
        with pytest.raises(accept.Blocked, match="not this probe's original admission"):
            asyncio.run(negative.refusal_probe(live_session(session), fixture, checkpoint(), "observe-refusal", 14, root=tmp_path, policy_path=policy, journal_path=settings.operation_state_root / "management-operations.sqlite3"))
        ticket = json.loads(negative.refusal_ticket("observe-refusal", tmp_path).read_text())
        assert ticket["effect_state"] == "ADMISSION_UNKNOWN" and ticket["operation_id"] == ""
        report = negative.exception_report("observe-refusal", "busy", root=tmp_path)
        assert report.operation_id == "" and report.effect_dispatched is None
        assert beta.send_calls == 1


def original_record(fixture):
    target = ManagedTarget(fixture.target_ids["beta"], "beta", accept.BETA_WORK_REPOSITORY)
    intent = OperationIntent(target, "deploy_beta_source", (ActionParameter("target_id", target.instance_id),), "original-key", "old-process", "old-generation")
    authority = AcceptedAuthority("https://fixture.example", "https://fixture.example/mcp", fixture.firebase_project_id, "owner", "dot-disposable-operator", "original-grant", ("auto-coder.read", "auto-coder.deploy"), 1.0, 1.0)
    return OperationRecord("original-operation", intent, intent.fingerprint(), authority, OperationPhase.NEEDS_ATTENTION, 1, safe_error_category="lifecycle_deadline_exceeded", effect=EffectAttempt(state=EffectState.SENT_UNKNOWN))


class ProjectedOriginal:
    """Offline real lifecycle projection with no claim of physical worker evidence."""

    def __init__(self, record, snapshot, current=None):
        self.record = record
        self.snapshot = snapshot
        self.current = current
        self.observations = []
        self.calls = []
        self.producer = WorkerLifecycle(SimpleNamespace(_params=ManagementRuntime._params))

    async def call(self, name, arguments, *, mutation=False):
        assert mutation is False
        self.calls.append((name, arguments))
        assert arguments.get("target_id") == self.record.intent.target.instance_id
        if name == "auto_coder_operation_inspect":
            assert arguments.get("operation_id") == self.record.operation_id
            return self.producer.project_snapshot(self.record, self.snapshot)
        assert name == "auto_coder_deployment_status" and self.current is not None
        return self.current


class ProjectedPublication:
    """Actual original journal/projections, with no network or publication claim."""

    def __init__(self, journal, record):
        self.journal = journal
        self.record = record
        self.observations = []
        self.calls = []
        self.producer = SourceReleaseDispatch(SimpleNamespace(journal=journal, settings=None, _project_operation=ManagementRuntime._project_operation))

    async def call(self, name, arguments, *, mutation=False):
        assert mutation is False and name == "auto_coder_operation_inspect"
        self.calls.append((name, arguments))
        assert arguments == {"target_id": ManagementRuntime._params(self.record)["target_id"], "operation_id": self.record.operation_id}
        record = self.journal.get(self.record.operation_id).record
        assert record is not None
        if record.intent.action == "promote_accepted_source_release":
            return self.producer.project(record)
        return ManagementRuntime._project_operation(record)


def publication_original(tmp_path, action, *, source_sha=None):
    fixture = disposable_fixture()
    role = "release" if action == "promote_accepted_source_release" else "beta"
    target = ManagedTarget("accepted-source-release" if role == "release" else fixture.target_ids[role], role, accept.TEST_REPOSITORY if role == "release" else accept.BETA_WORK_REPOSITORY)
    source_sha = source_sha or fixture.source_fixture_sha
    intent = OperationIntent(target, action, (ActionParameter("target_id", fixture.target_ids[role]), ActionParameter("source_sha", source_sha)), "original-publication-key", "original-process", source_sha)
    journal = ManagementOperationStore(tmp_path / "journal")
    record = journal.admit(intent, original_record(fixture).authority).record
    assert record is not None
    record = journal.begin_effect(record.operation_id, record.revision, owner_attempt_id="original-owner", attempt_id="original-attempt", correlation_identity="original-correlation").record
    assert record is not None and record.effect.state is EffectState.SENT_UNKNOWN
    binding = {"promotion_operation_id" if role == "release" else "beta_operation_id": record.operation_id}
    saved = checkpoint(**binding, idempotency_key=intent.idempotency_key, pending_operation_id=record.operation_id, pending_effect_state="SENT_UNKNOWN")
    return fixture, journal, record, saved


def test_release_absence_query_includes_actual_canonical_source_lane(tmp_path):
    fixture, _journal, record, saved = publication_original(tmp_path, "promote_accepted_source_release")
    assert negative.journal_original_key(tmp_path / "journal/management-operations.sqlite3", fixture.target_ids["release"], saved.idempotency_key) == (record.operation_id,)


@pytest.mark.parametrize("action", ["promote_accepted_source_release", "run_beta_acceptance"])
def test_unknown_publication_uses_real_original_projection_without_dispatch(tmp_path, action):
    fixture, journal, record, saved = publication_original(tmp_path, action)
    live = ProjectedPublication(journal, record)
    report = asyncio.run(negative.original_scenario(live, fixture, saved, "unknown-effect", root=tmp_path, journal_path=tmp_path / "journal/management-operations.sqlite3"))
    assert report.verdict == "BLOCKED" and report.negative_boundary_verified
    assert report.operation_id == record.operation_id and report.effect_dispatched is False
    assert report.positive_deployment_proven is False
    assert [call[0] for call in live.calls] == ["auto_coder_operation_inspect", "auto_coder_operation_inspect"]
    assert journal.get(record.operation_id).record == record
    if action == "run_beta_acceptance":
        assert report.observations[-1].status == "BOUND"


def test_unknown_promotion_cannot_use_release_deployment_binding(tmp_path):
    fixture, journal, record, saved = publication_original(tmp_path, "promote_accepted_source_release")
    saved = replace(saved, promotion_operation_id="", release_operation_id=record.operation_id)
    with pytest.raises(accept.Blocked, match="original action"):
        asyncio.run(negative.original_scenario(ProjectedPublication(journal, record), fixture, saved, "unknown-effect", root=tmp_path))


@pytest.mark.parametrize("action", ["promote_accepted_source_release", "run_beta_acceptance"])
def test_original_publication_requires_actual_selected_source(tmp_path, action):
    fixture, journal, record, saved = publication_original(tmp_path, action, source_sha="f" * 40)
    with pytest.raises(accept.Failed, match="immutable selected source"):
        asyncio.run(negative.original_scenario(ProjectedPublication(journal, record), fixture, saved, "unknown-effect", root=tmp_path, journal_path=tmp_path / "journal/management-operations.sqlite3"))


def test_original_acceptance_requires_independent_fixed_key_intent(tmp_path):
    fixture, journal, record, saved = publication_original(tmp_path, "run_beta_acceptance")
    live = ProjectedPublication(journal, record)
    with pytest.raises(accept.Blocked, match="independently checked"):
        asyncio.run(negative.original_scenario(live, fixture, saved, "unknown-effect", root=tmp_path, journal_path=tmp_path / "missing.sqlite3"))
    with pytest.raises(accept.Blocked, match="fixed key"):
        asyncio.run(negative.original_scenario(live, fixture, replace(saved, idempotency_key="replacement-key"), "unknown-effect", root=tmp_path, journal_path=tmp_path / "journal/management-operations.sqlite3"))


@pytest.mark.parametrize("scenario", ["held-drain", "repeated-failure", "data-rollback"])
def test_publication_does_not_become_a_worker_recovery_scenario(tmp_path, scenario):
    fixture, journal, record, saved = publication_original(tmp_path, "promote_accepted_source_release")
    with pytest.raises(accept.Blocked, match="original action"):
        asyncio.run(negative.original_scenario(ProjectedPublication(journal, record), fixture, saved, scenario, root=tmp_path))


def test_unknown_original_remains_blocked_and_never_uses_effectful_get(tmp_path):
    fixture = disposable_fixture()
    record = original_record(fixture)
    live = ProjectedOriginal(record, {"stage": "NEEDS_ATTENTION", "source_sha": fixture.source_fixture_sha, "recovery_attempts": 0})
    saved = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="SENT_UNKNOWN")
    report = asyncio.run(negative.original_scenario(live, fixture, saved, "unknown-effect", root=tmp_path))
    assert report.verdict == "BLOCKED" and report.negative_boundary_verified
    assert report.operation_id == record.operation_id and report.effect_dispatched is False
    assert report.positive_deployment_proven is False
    assert [call[0] for call in live.calls] == ["auto_coder_operation_inspect", "auto_coder_operation_inspect"]


def test_true_held_drain_requires_same_actual_worker_and_no_start(tmp_path):
    fixture = disposable_fixture()
    record = original_record(fixture)
    incumbent = {"process_run_id": "old-process", "deployment_generation": "old-generation", "runtime_id": "old-runtime", "supervisor_generation": "original-supervisor"}
    snapshot = {"stage": "DRAINING", "source_sha": fixture.source_fixture_sha, "incumbent": incumbent, "steps": {"drain": {"operation_id": "d-" + record.operation_id, "state": "SENT_UNKNOWN"}}, "recovery_attempts": 0}
    current = {"status": "OK", "target_id": fixture.target_ids["beta"], "instance_id": fixture.target_ids["beta"], "repository": accept.BETA_WORK_REPOSITORY, "channel": "beta", "alive": True, "worker": incumbent, "process_state": "DRAINING", "supervisor_generation": "original-supervisor"}
    live = ProjectedOriginal(record, snapshot, current)
    saved = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="NEEDS_ATTENTION")
    report = asyncio.run(negative.original_scenario(live, fixture, saved, "held-drain", root=tmp_path))
    assert report.verdict == "BLOCKED" and report.negative_boundary_verified
    current["worker"] = {**incumbent, "process_run_id": "replacement"}
    with pytest.raises(accept.Blocked, match="lifetime"):
        asyncio.run(negative.original_scenario(live, fixture, saved, "held-drain", root=tmp_path))
    snapshot["steps"]["start"] = {"operation_id": "s-" + record.operation_id}
    with pytest.raises(accept.Blocked, match="held graceful drain"):
        asyncio.run(negative.original_scenario(live, fixture, saved, "held-drain", root=tmp_path))


def test_consumed_producer_recovery_is_durable_and_cannot_be_rebound(tmp_path):
    fixture = disposable_fixture()
    record = original_record(fixture)
    live = ProjectedOriginal(record, {"stage": "NEEDS_ATTENTION", "source_sha": fixture.source_fixture_sha, "recovery_attempts": 1, "recovery_state": "UNKNOWN"})
    saved = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="NEEDS_ATTENTION")
    report = asyncio.run(negative.original_scenario(live, fixture, saved, "repeated-failure", root=tmp_path))
    assert report.verdict == "BLOCKED" and report.negative_boundary_verified
    path = tmp_path / f"dot-recovery-{report.failure_signature}.json"
    original_bytes = path.read_bytes()
    assert path.stat().st_mode & 0o077 == 0
    assert asyncio.run(negative.original_scenario(live, fixture, saved, "repeated-failure", root=tmp_path)).failure_signature == report.failure_signature
    assert path.read_bytes() == original_bytes
    identity = negative.FailureIdentity(record.intent.action, record.intent.target.instance_id, fixture.source_fixture_sha, record.safe_error_category)
    with pytest.raises(accept.Blocked, match="cannot replace"):
        negative.record_exhausted_failure(tmp_path, fixture, identity, "invented-retry")
    assert path.read_bytes() == original_bytes


def test_missing_recovery_evidence_does_not_create_exhaustion_pass(tmp_path):
    fixture = disposable_fixture()
    record = original_record(fixture)
    live = ProjectedOriginal(record, {"stage": "NEEDS_ATTENTION", "source_sha": fixture.source_fixture_sha, "recovery_attempts": 0})
    saved = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="NEEDS_ATTENTION")
    with pytest.raises(accept.Blocked, match="one consumed recovery"):
        asyncio.run(negative.original_scenario(live, fixture, saved, "repeated-failure", root=tmp_path))
    assert list(tmp_path.iterdir()) == []


def confirmed_prior_runtime_recovery(fixture, tmp_path):
    journal = ManagementOperationStore(tmp_path / "journal")
    original = original_record(fixture)
    snapshot = {"schema_version": 1, "stage": "RECONCILING_PRIOR", "source_sha": fixture.source_fixture_sha, "recovery_attempts": 1, "recovery_state": "CONFIRMED", "restored_worker": {"runtime_id": "previous"}, "heartbeats": [1, 2]}
    record = journal.admit(original.intent, original.authority, lifecycle_snapshot=snapshot).record
    assert record is not None
    record = journal.begin_effect(record.operation_id, record.revision, owner_attempt_id="original-owner", attempt_id="original-attempt", correlation_identity="original-correlation").record
    assert record is not None
    producer = WorkerLifecycle(SimpleNamespace(journal=journal, _params=ManagementRuntime._params))
    version, stored = journal.lifecycle_snapshot(record.operation_id)
    record = producer._finish(record, version, stored, "FAILED", "new_beta_worker_failed_previous_runtime_restored")
    _, stored = journal.lifecycle_snapshot(record.operation_id)
    assert record.phase is OperationPhase.FAILED and record.effect.state is EffectState.CONFIRMED
    return record, stored


def test_confirmed_prior_runtime_still_exhausts_original_failure_budget(tmp_path):
    fixture = disposable_fixture()
    record, snapshot = confirmed_prior_runtime_recovery(fixture, tmp_path)
    live = ProjectedOriginal(record, snapshot)
    saved = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="SENT_UNKNOWN")
    report = asyncio.run(negative.original_scenario(live, fixture, saved, "repeated-failure", root=tmp_path))
    assert report.verdict == "BLOCKED" and report.negative_boundary_verified
    retained = json.loads((tmp_path / f"dot-recovery-{report.failure_signature}.json").read_text())
    assert retained["failure"]["reason"] == "new_beta_worker_failed_previous_runtime_restored"
    assert retained["original_operation_id"] == record.operation_id
    assert retained["recovery_attempts"] == 1


def test_runtime_receipt_cannot_claim_persistent_data_rollback(tmp_path):
    fixture = disposable_fixture()
    record, snapshot = confirmed_prior_runtime_recovery(fixture, tmp_path)
    live = ProjectedOriginal(record, snapshot)
    saved = checkpoint(beta_operation_id=record.operation_id, pending_operation_id=record.operation_id, pending_effect_state="NEEDS_ATTENTION")
    report = asyncio.run(negative.original_scenario(live, fixture, saved, "data-rollback", root=tmp_path))
    assert report.verdict == "BLOCKED" and "unsupported" in report.reason
    assert not report.positive_deployment_proven and not report.negative_boundary_verified
    assert live.calls == [("auto_coder_operation_inspect", {"target_id": fixture.target_ids["beta"], "operation_id": record.operation_id})]
