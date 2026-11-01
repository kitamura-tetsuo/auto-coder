"""Real OAuth/MCP adapter tests for the injected, fenced prior journal."""

from pathlib import Path

from starlette.testclient import TestClient

from auto_coder.management.action_scopes import DEPLOY_SCOPE, OPERATE_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.mcp_runtime_tools import ManagementRuntime
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operations import ActionParameter, ManagedTarget, OperationIntent, OperationPhase
from auto_coder.management.retained_journal import RetainedJournal
from tests.test_management_mcp import _issue, real_boundary  # noqa: F401
from tests.test_management_runtime_mcp import _application, _call, _request, runtime_boundary  # noqa: F401
from tests.test_management_retained_journal import retained
from tests.test_management_worker_lifecycle import LifecycleBridge


def identity(path: Path) -> tuple[int, int]:
    value = path.stat()
    return value.st_dev, value.st_ino


def test_authenticated_retained_read_replay_and_exclusion(runtime_boundary, tmp_path: Path) -> None:
    settings, grants, signed, _policy, beta, release = runtime_boundary
    with TestClient(_application(runtime_boundary), base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        other = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE)["access_token"]
    grant = grants.authenticate_access(token, settings.oauth.resource, (READ_SCOPE, OPERATE_SCOPE))
    authority = ManagementRuntime._journal_authority(grant, grants.accepted_authority(grant))
    intent = OperationIntent(
        ManagedTarget(beta.config.instance_id, beta.config.channel, beta.config.repository),
        "reevaluate_target",
        (ActionParameter("target_id", "beta-worker"), ActionParameter("target_type", "issue"), ActionParameter("number", 2603)),
        "key-1",
        beta.run,
        beta.deployment,
    )
    old_root = tmp_path / "retained"
    old = ManagementOperationStore(old_root)
    admitted = old.admit(intent, authority)
    assert admitted.record is not None
    old_id = admitted.record.operation_id
    assert old.transition(old_id, 0, OperationPhase.NEEDS_ATTENTION, safe_error_category="unknown_prior_effect").record is not None
    # A current row may already exist after a crash or bad earlier setup.
    # Startup reconciliation must not dispatch it across the old active lane.
    assert settings.operation_state_root is not None
    current = ManagementOperationStore(settings.operation_state_root)
    current_intent = OperationIntent(intent.target, intent.action, intent.parameters, "current-key", intent.expected_incarnation, intent.expected_artifact)
    current_record = current.admit(current_intent, authority).record
    assert current_record is not None
    database = old_root / "management-operations.sqlite3"
    fence = tmp_path / "prior.lock"
    fence.touch()
    with RetainedJournal(database, fence, database_identity=identity(database), fence_identity=identity(fence)) as reader:
        app = create_management_app(settings, grants, worker_clients={"beta": beta, "release": release}, retained_journal=reader)
        with TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
            query = {"target_id": "beta-worker", "operation_id": old_id}
            result = _call(client, token, "auto_coder_operation_get", query)
            assert result["status"] == "NEEDS_ATTENTION" and result["safe_reason"] == "unknown_prior_effect"
            assert _call(client, other, "auto_coder_operation_get", query)["status"] == "NOT_FOUND"
            replay = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
            assert replay["operation_id"] == old_id and replay["status"] == "NEEDS_ATTENTION"
            changed = {**_request(beta), "number": 2604}
            assert _call(client, token, "auto_coder_reevaluate_target", changed)["status"] == "CONFLICT"
            assert _call(client, token, "auto_coder_reevaluate_target", _request(beta, key="different"))["status"] == "BUSY"
            assert beta.send_calls == 0
            assert current.get(current_record.operation_id).record == current_record
            reader.close()
            assert _call(client, token, "auto_coder_operation_get", query)["status"] == "UNAVAILABLE"
            assert _call(client, token, "auto_coder_reevaluate_target", _request(beta, key="different"))["status"] == "UNAVAILABLE"
            assert beta.send_calls == 0


def test_authenticated_old_worker_lifecycle_snapshot_and_no_retransmit(real_boundary, tmp_path: Path) -> None:
    base, _base_grants, identity_service, signed, _uids, state = real_boundary
    origin = base.oauth.canonical_issuer
    scopes = (READ_SCOPE, DEPLOY_SCOPE)
    oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=scopes),), enabled_scopes=scopes)
    policy = tmp_path / "policy.yaml"
    policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
    settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "new")
    grants = OAuthGrantService(oauth, identity_service, clock=lambda: state["now"])
    bridge = LifecycleBridge()
    with TestClient(create_management_app(settings, grants, worker_clients={"beta": bridge}), base_url=origin) as client:  # type: ignore[dict-item]
        token = _issue(client, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
    grant = grants.authenticate_access(token, oauth.resource, scopes)
    authority = ManagementRuntime._journal_authority(grant, grants.accepted_authority(grant))
    intent = OperationIntent(
        ManagedTarget("beta-worker", "beta", "fixture/beta"),
        "start_worker",
        (ActionParameter("target_id", "beta-worker"), ActionParameter("supervisor_generation", "old-supervisor"), ActionParameter("runtime_id", "bootstrap"), ActionParameter("source_sha", "-")),
        "old-start",
        "-",
        "-",
    )
    old = ManagementOperationStore(tmp_path / "old")
    snapshot = {"schema_version": 1, "stage": "NEEDS_ATTENTION", "incumbent": {"supervisor_generation": "old-supervisor", "runtime_id": "bootstrap"}, "steps": {"start": {"state": "SENT_UNKNOWN", "operation_id": "original-effect"}}}
    operation = old.admit(intent, authority, lifecycle_snapshot=snapshot).record
    assert operation is not None
    assert old.transition(operation.operation_id, 0, OperationPhase.NEEDS_ATTENTION, safe_error_category="lifecycle_deadline_exceeded").record is not None
    database = tmp_path / "old" / "management-operations.sqlite3"
    fence = tmp_path / "owner.lock"
    fence.touch()
    with RetainedJournal(database, fence, database_identity=identity(database), fence_identity=identity(fence)) as reader:
        app = create_management_app(settings, grants, worker_clients={"beta": bridge}, retained_journal=reader)  # type: ignore[dict-item]
        with TestClient(app, base_url=origin) as client:
            query = {"target_id": "beta-worker", "operation_id": operation.operation_id}
            result = _call(client, token, "auto_coder_operation_get", query)
            assert result["status"] == "NEEDS_ATTENTION" and result["steps"] == snapshot["steps"]
            args = {"target_id": "beta-worker", "idempotency_key": "old-start", "expected_supervisor_generation": "old-supervisor", "expected_runtime_id": "bootstrap"}
            assert _call(client, token, "auto_coder_start_worker", args)["operation_id"] == operation.operation_id
            assert _call(client, token, "auto_coder_start_worker", {**args, "idempotency_key": "different"})["status"] == "BUSY"
            assert bridge.actions == []


def test_prior_fence_lost_after_admission_prevents_actual_effect(runtime_boundary, tmp_path: Path) -> None:
    settings, grants, signed, _policy, beta, release = runtime_boundary
    prior_root = tmp_path / "independent-prior"
    prior_root.mkdir()
    reader, _, _ = retained(prior_root)
    app = create_management_app(settings, grants, worker_clients={"beta": beta, "release": release}, retained_journal=reader)  # type: ignore[dict-item]
    with reader, TestClient(app, base_url=settings.oauth.canonical_issuer) as client:
        token = _issue(client, settings, signed, "bob", "bob-client", READ_SCOPE + " " + OPERATE_SCOPE)["access_token"]
        app.state.management_runtime.journal._after_admission_commit = reader.close
        result = _call(client, token, "auto_coder_reevaluate_target", _request(beta))
        assert result["phase"] == "accepted" and beta.send_calls == 0
        assert app.state.management_runtime.journal.get(result["operation_id"]).record is not None
