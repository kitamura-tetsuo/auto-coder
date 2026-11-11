"""Production HTTPS MCP + real consent/journal + controlled GitHub REST boundary.

The remote executes the actual source publisher after a dispatch creates a run;
no canned completed run or Boolean substitutes for acceptance or promotion.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.operations import EffectState, OperationPhase
from auto_coder.source_release_catalog import RELEASE_WORKFLOW, SourceReleaseRequest, canonical
from auto_coder.source_release_promotion import SourceReleasePromotion
from auto_coder.util.gh_cache import GitDataResponse
from tests.test_management_mcp import _issue, _mcp, real_boundary  # noqa: F401
from tests.test_source_release_promotion import OTHER_SHA, RELEASE_REF, ReleaseGit, _harness

ACTION = "auto_coder_promote_accepted_source_release"
WORKFLOW_ID = 321


@pytest.fixture(autouse=True)
def in_process_http_port(monkeypatch):
    # TestClient runs ASGI in-process. It needs no TCP listener or free-port
    # probe, which also keeps this component suite runnable without sockets.
    monkeypatch.setattr("tests.test_management_mcp._free_port", lambda: 8081)


class WorkflowBoundary:
    """Stateful Actions boundary: every run originates at the dispatch POST."""

    def __init__(self, *args):
        super().__init__(*args)
        self.runs = {}
        self.dispatches = []
        self.inputs = {}
        self.failure = ""
        self.publish = True
        self.complete = True
        self.listing = ""
        self.list_prefix = []
        self.mutate_run = None
        self.mutate_workflow = None
        self.response_change = None
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.block = False
        self.run_reads_unavailable = False
        self.configured = None
        self.clock = None
        self.after_workflow_read = None
        self.on_send = None

    def request(self, method, path, payload=None):
        if method == "GET" and path == f"actions/workflows/{WORKFLOW_ID}":
            document = {"id": WORKFLOW_ID, "path": RELEASE_WORKFLOW, "state": "active", "url": f"https://api.github.com/repos/owner/repo/actions/workflows/{WORKFLOW_ID}"}
            if self.mutate_workflow:
                self.mutate_workflow(document)
            if self.after_workflow_read:
                self.after_workflow_read()
            return GitDataResponse(200, document)
        if method == "POST" and path == f"actions/workflows/{WORKFLOW_ID}/dispatches":
            self.dispatches.append(payload)
            if self.on_send:
                self.on_send()
            if self.block:
                self.entered.set()
                assert self.resume.wait(10)
            if self.failure == "before":
                raise RuntimeError("lost before remote effect")
            if self.failure == "denied":
                return GitDataResponse(403)
            inputs = payload["inputs"]
            self.inputs = inputs
            run_id = 900 + len(self.runs)
            run = {
                "id": run_id,
                "run_attempt": 1,
                "workflow_id": WORKFLOW_ID,
                "path": RELEASE_WORKFLOW,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "display_title": f"accepted-release/{inputs['operation_id']}",
                # Workflow checkout is deliberately different from promoted source.
                "head_sha": OTHER_SHA,
                "repository": {"full_name": "owner/repo"},
                "status": "in_progress",
                "conclusion": None,
                "url": f"https://api.github.com/repos/owner/repo/actions/runs/{run_id}",
                "html_url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            }
            self.runs[run_id] = run
            if self.publish:
                SourceReleasePromotion(self, self.configured, clock=self.clock).execute(SourceReleaseRequest(**inputs, promotion_run_id=run_id, promotion_run_attempt=1))
            if self.complete:
                run.update(status="completed", conclusion="success")
            if self.mutate_run:
                self.mutate_run(run)
            if self.failure == "after":
                raise RuntimeError("lost after remote effect")
            if self.failure == "crash":
                raise SystemExit("manager exits after remote acceptance")
            response = {"workflow_run_id": run_id, "run_url": run["url"], "html_url": run["html_url"]}
            if self.response_change:
                self.response_change(response)
            return GitDataResponse(200, response)
        if method == "GET" and path.startswith(f"actions/workflows/{WORKFLOW_ID}/runs?"):
            if self.listing == "unavailable":
                return GitDataResponse(503)
            runs = self.list_prefix + list(self.runs.values())
            if self.listing == "duplicate" and runs:
                duplicate = dict(runs[0], id=901, url="https://api.github.com/repos/owner/repo/actions/runs/901", html_url="https://github.com/owner/repo/actions/runs/901")
                runs.append(duplicate)
            total = len(runs) + (1 if self.listing == "gap" else 0)
            if self.listing == "empty":
                runs, total = [], 0
            if self.listing == "overflow":
                total = 501
            page = int(path.rsplit("=", 1)[1])
            return GitDataResponse(200, {"total_count": total, "workflow_runs": runs[(page - 1) * 100 : page * 100]})
        if method == "GET" and path.startswith("actions/runs/"):
            parts = path.split("/")
            run_id = int(parts[2])
            if run_id in self.runs and (len(parts) == 3 or parts[3:] == ["attempts", "1"]):
                if self.run_reads_unavailable:
                    return GitDataResponse(503)
                return GitDataResponse(200, dict(self.runs[run_id]))
        return super().request(method, path, payload)


class WorkflowGit(WorkflowBoundary, ReleaseGit):
    """Controlled workflow transport over the in-memory Git boundary."""


def _call(client, token, name, arguments):
    response = _mcp(client, token, "tools/call", 41, {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result.get("isError") is not True, result
    return result["structuredContent"]


@pytest.fixture
def dispatch(real_boundary, tmp_path, monkeypatch):
    settings, _, identity, signed, uid_policy, state = real_boundary
    remote, smoke, request, configured = _harness(tmp_path / "evidence", remote=WorkflowGit())
    remote.configured, remote.clock = configured, lambda: smoke.clock.now + 1
    # A local deterministic clock, not a production-authority bypass.
    monkeypatch.setattr("auto_coder.management.source_release_dispatch.time", SimpleNamespace(time=remote.clock))
    scopes = (READ_SCOPE, DEPLOY_SCOPE)
    oauth = replace(settings.oauth, clients=tuple(replace(client, allowed_scopes=scopes) for client in settings.oauth.clients), enabled_scopes=scopes)
    promotion_policy = tmp_path / "source-release-policy.json"
    promotion_policy.write_text(canonical(asdict(configured)))
    token_file = tmp_path / "github-token"
    token_file.write_text("never-log-this-provider-token")
    transport_calls = []

    def github_http(request):
        assert request.url.scheme == "https"
        assert request.url.host == "api.github.com"
        assert request.url.path.startswith("/repos/owner/repo/")
        assert request.headers["Authorization"] == "Bearer never-log-this-provider-token"
        payload = json.loads(request.content) if request.content and request.content != b"null" else None
        path = request.url.raw_path.decode().removeprefix("/repos/owner/repo/")
        transport_calls.append((request.method, path))
        if remote.run_reads_unavailable and path.startswith("actions/runs/900"):
            raise httpx.ReadTimeout("controlled exact-run read timeout", request=request)
        try:
            response = remote.request(request.method, path, payload)
        except RuntimeError as exc:
            if str(exc).startswith("lost "):
                raise httpx.ReadTimeout("controlled lost dispatch response", request=request) from exc
            raise
        return httpx.Response(response.status, json=response.data)

    @contextmanager
    def controlled_github_http_client(**_kwargs):
        with httpx.Client(transport=httpx.MockTransport(github_http)) as client:
            yield client

    # Control only HTTP; production constructs its canonical gh_cache adapter
    # and reads credentials exclusively from the operator's configured file.
    monkeypatch.setattr("auto_coder.util.gh_cache.github_http_client", controlled_github_http_client)
    operating = tmp_path / "operating.yaml"
    operating.write_text("profile: full\ntargets:\n  release-source:\n    channel: release\n    repositories: [owner/repo]\n")
    settings = replace(settings, oauth=oauth, operation_state_root=tmp_path / "operations", operating_policy_path=operating, source_release_policy_path=promotion_policy, source_release_token_file=token_file, source_release_target_id="release-source", source_release_workflow_id=WORKFLOW_ID)
    grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
    arguments = {key: getattr(request, key) for key in ("source_sha", "ci_run_id", "ci_run_attempt", "tested_source_ref", "tested_source_oid", "tested_source_record_sha256", "acceptance_ref", "acceptance_oid")}
    arguments.update(target_id="release-source", idempotency_key="dispatch-key")

    def app():
        return create_management_app(settings, grants, worker_clients={})

    return SimpleNamespace(settings=settings, grants=grants, signed=signed, state=state, uid_policy=uid_policy, operating=operating, promotion_policy=promotion_policy, remote=remote, smoke=smoke, args=arguments, app=app, transport_calls=transport_calls)


def _token(client, d, uid="alice", scope=f"{READ_SCOPE} {DEPLOY_SCOPE}"):
    return _issue(client, d.settings, d.signed, uid, f"{uid}-client", scope)["access_token"]


def _read(client, token, operation_id):
    return _call(client, token, "auto_coder_operation_get", {"operation_id": operation_id})


def test_exact_dispatch_publishes_original_producer_evidence_and_catalog(dispatch):
    d = dispatch
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        result = _call(client, token, ACTION, d.args)
        assert result["status"] == "SUCCEEDED", result
        assert result["source_promotion"] == "SOURCE_PROMOTION_CONFIRMED"
        assert result["worker_deployed"] is False
        assert result["workflow_run"]["head_sha"] == OTHER_SHA != result["selected_source_sha"]
        assert result["workflow_run"]["id"] == 900
        assert d.remote.dispatches == [{"ref": "main", "inputs": {**{k: v for k, v in d.args.items() if k not in {"target_id", "idempotency_key"}}, "operation_id": result["operation_id"], "source_repository": "owner/repo", "release_target": "release"}, "return_run_details": True}]
        assert _call(client, token, ACTION, d.args)["operation_id"] == result["operation_id"]
        assert _read(client, token, result["operation_id"])["status"] == "SUCCEEDED"
        assert len(d.remote.dispatches) == 1
        changed = _call(client, token, ACTION, {**d.args, "source_sha": OTHER_SHA, "tested_source_ref": f"refs/tags/auto-coder-tested-{OTHER_SHA}"})
        assert changed["status"] == "CONFLICT"
        stored = client.app.state.management_runtime.journal.get(result["operation_id"]).record
        assert stored.authority.uid == "alice"
        assert stored.authority.oauth_client_id == "alice-client"
        assert stored.effect.state is EffectState.CONFIRMED
        assert stored.effect.attempt_id == stored.owner_attempt_id
        assert "never-log-this-provider-token" not in canonical(result)
        assert d.remote.release_pointer_writes == 1


@pytest.mark.parametrize("failure", ["before", "after", "denied"])
def test_lost_or_denied_dispatch_restart_never_sends_again(dispatch, failure):
    d = dispatch
    d.remote.failure = failure
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        assert first["status"] == "NEEDS_ATTENTION"
        assert first["effect_state"] == "sent_unknown"
        operation_id = first["operation_id"]
    d.remote.failure = ""
    app = d.app()
    with TestClient(app, base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        before = app.state.management_runtime.journal.get(operation_id).record
        prior_dispatches = list(d.remote.dispatches)
        prior_writes = d.remote.release_pointer_writes
        inspected = _call(client, token, "auto_coder_operation_inspect", {"operation_id": operation_id})
        assert inspected["operation_id"] == operation_id
        assert app.state.management_runtime.journal.get(operation_id).record == before
        assert d.remote.dispatches == prior_dispatches
        assert d.remote.release_pointer_writes == prior_writes
        result = _read(client, token, operation_id)
        assert result["status"] == ("SUCCEEDED" if failure == "after" else "NEEDS_ATTENTION"), result
        assert len(d.remote.dispatches) == 1
        if failure != "after":
            other = _call(client, token, ACTION, {**d.args, "idempotency_key": "new-key"})
            assert other["status"] == "BUSY"
            assert other["operation_id"] == operation_id


@pytest.mark.parametrize("listing", ["empty", "gap", "duplicate", "unavailable", "overflow"])
def test_ambiguous_listing_retains_original_lane(dispatch, listing):
    d = dispatch
    d.remote.failure, d.remote.listing = "after", listing
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["workflow_run"] is None
        assert _call(client, _token(client, d, "bob"), ACTION, {**d.args, "idempotency_key": "other-client"})["status"] == "BUSY"
        assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("field,value", [("workflow_id", 987), ("path", ".github/workflows/promote-release.yml"), ("repository", {"full_name": "wrong/repo"}), ("head_branch", "other"), ("event", "push"), ("display_title", "other"), ("head_sha", "main"), ("id", 901), ("run_attempt", 2)])
def test_direct_run_identity_mismatch_never_confirms(dispatch, field, value):
    d = dispatch
    d.remote.mutate_run = lambda run: run.update({field: value})
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        assert first["status"] == "NEEDS_ATTENTION"
        assert _read(client, token, first["operation_id"])["status"] == "NEEDS_ATTENTION"
        assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("field,value", [("workflow_run_id", 0), ("workflow_run_id", True), ("run_url", "https://api.github.com/repos/other/repo/actions/runs/900"), ("html_url", "https://example.com/runs/900")])
def test_invalid_positive_dispatch_response_is_not_success(dispatch, field, value):
    d = dispatch
    d.remote.response_change = lambda response: response.update({field: value})
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        first = _call(client, _token(client, d), ACTION, d.args)
        assert first["status"] == "NEEDS_ATTENTION"
        assert first["workflow_run"] is None
        assert len(d.remote.dispatches) == 1


def test_workflow_success_without_catalog_is_not_promotion(dispatch):
    d = dispatch
    d.remote.publish = False
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        result = _call(client, token, ACTION, d.args)
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["safe_reason"] == "successful_workflow_catalog_missing"
        assert result["source_promotion"] == "UNRESOLVED"
        assert _call(client, token, ACTION, {**d.args, "idempotency_key": "new-key"})["status"] == "BUSY"
        assert d.remote.release_pointer_writes == 0


@pytest.mark.parametrize("missing", ["pointer", "release", "catalog", "acceptance", "tested"])
def test_success_with_partial_or_changed_publication_retains_lane(dispatch, missing):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        if missing == "pointer":
            del d.remote.refs[RELEASE_REF]
        elif missing == "release":
            d.remote.releases.clear()
        elif missing == "catalog":
            del d.remote.refs[f"refs/tags/auto-coder-source-release-{first['operation_id']}"]
        else:
            ref = d.args["acceptance_ref" if missing == "acceptance" else "tested_source_ref"]
            d.remote.refs[ref] = ("tag", "f" * 40)
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["source_promotion"] == "UNRESOLVED"
        assert len(d.remote.dispatches) == 1


def test_superseded_confirmed_historical_publication_has_distinct_outcome(dispatch):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        d.remote.refs[RELEASE_REF] = ("commit", OTHER_SHA)
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "SUCCEEDED"
        assert result["source_promotion"] == "COMPLETED_BUT_SUPERSEDED"
        assert result["worker_deployed"] is False
        assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_failed_workflow_with_possible_partial_effect_does_not_free_lane(dispatch, conclusion):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        d.remote.runs[900]["conclusion"] = conclusion
        result = _read(client, token, first["operation_id"])
        assert result["safe_reason"] == "workflow_failed_effect_unresolved"
        assert result["status"] == "NEEDS_ATTENTION"
        assert _call(client, token, ACTION, {**d.args, "idempotency_key": "after-failure"})["status"] == "BUSY"
        assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("profile", ["observe", "beta"])
def test_current_policy_refuses_admission(dispatch, profile):
    d = dispatch
    d.operating.write_text(d.operating.read_text().replace("full", profile))
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "DENIED"
        assert d.remote.dispatches == []
        assert client.app.state.management_runtime.journal.list_operations().records == ()


@pytest.mark.parametrize("revoke", ["policy", "grant", "expired", "uid", "consent", "missing-grant"])
def test_authority_downgrade_at_final_send_boundary_proves_no_effect(dispatch, revoke):
    d = dispatch

    def downgrade():
        if revoke == "policy":
            d.operating.write_text(d.operating.read_text().replace("full", "observe"))
        elif revoke == "uid":
            d.uid_policy.write_text(json.dumps({"project_id": d.settings.oauth.firebase_project_id, "allowed_uids": ["bob"]}))
        else:
            with d.grants.store.connect() as db:
                if revoke == "missing-grant":
                    # Remove dependent tokens before the original durable grant.
                    db.execute("DELETE FROM access_tokens")
                    db.execute("DELETE FROM refresh_tokens")
                    db.execute("DELETE FROM grants")
                elif revoke == "expired":
                    db.execute("UPDATE grants SET expires=?", (d.state["now"] - 1,))
                elif revoke == "consent":
                    db.execute("UPDATE grants SET scope=?", (READ_SCOPE,))
                else:
                    db.execute("UPDATE grants SET revoked=?", (d.state["now"],))

    d.remote.after_workflow_read = downgrade
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "FAILED", result
        assert result["effect_state"] == "rejected"
        assert d.remote.dispatches == []


@pytest.mark.parametrize("field,value", [("source_sha", OTHER_SHA), ("acceptance_oid", "f" * 40), ("tested_source_oid", "e" * 40), ("ci_run_id", 999), ("source_sha", "main"), ("acceptance_ref", "refs/heads/main")])
def test_mismatched_or_mutable_source_evidence_is_not_dispatched(dispatch, field, value):
    d = dispatch
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, {**d.args, field: value})
        assert result["status"] == "UNAVAILABLE"
        assert result["reason"] == "exact_source_evidence_unavailable"
        assert d.remote.dispatches == []


def test_response_identity_survives_run_read_timeout_and_restart(dispatch):
    d = dispatch
    d.remote.run_reads_unavailable = True
    # Publication itself needs an Actions read, so leave remote workflow pending.
    d.remote.publish, d.remote.complete = False, False
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        first = _call(client, _token(client, d), ACTION, d.args)
        assert first["status"] == "NEEDS_ATTENTION"
        assert client.app.state.management_runtime.journal.source_release_run(first["operation_id"], kind="response") == {"id": 900}
    d.remote.run_reads_unavailable = False
    d.remote.listing = "duplicate"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _read(client, _token(client, d), first["operation_id"])
        assert result["workflow_run"]["id"] == 900
        assert result["safe_reason"] == "workflow_pending"
        assert len(d.remote.dispatches) == 1


def test_reconciliation_after_original_grant_revocation_and_policy_loss_is_read_only(dispatch):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        old_token = _token(client, d)
        first = _call(client, old_token, ACTION, d.args)
        d.grants.revoke("alice-client", old_token)
        d.operating.write_text(d.operating.read_text().replace("full", "observe"))
    d.remote.calls.clear()
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d, scope=READ_SCOPE)
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "SUCCEEDED"
        assert len(d.remote.dispatches) == 1
        assert all(method == "GET" for method, _ in d.remote.calls)
        assert result["accepted_actor"]["scopes"] == [READ_SCOPE, DEPLOY_SCOPE]


def test_concurrent_clients_and_reconstructed_stores_share_real_lane(dispatch):
    d = dispatch
    d.remote.block, d.remote.publish = True, False
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        alice, bob = _token(client, d), _token(client, d, "bob")
        with ThreadPoolExecutor(max_workers=3) as pool:
            first_future = pool.submit(_call, client, alice, ACTION, d.args)
            assert d.remote.entered.wait(5)
            competing = pool.submit(_call, client, bob, ACTION, {**d.args, "idempotency_key": "bob-key"}).result(5)
            assert competing["status"] == "BUSY"
            original = ManagementOperationStore(d.settings.operation_state_root).get(competing["operation_id"]).record
            assert original.effect.state is EffectState.SENT_UNKNOWN
            # A second production handler has its own SQLite connections and
            # process lock descriptor, and cannot reconcile an active sender.
            other = d.app().state.management_runtime
            assert other.source_release.process(original) == original
            assert other.journal.get(original.operation_id).record.phase is OperationPhase.RECONCILING
            replay_future = pool.submit(_call, client, alice, ACTION, d.args)
            d.remote.resume.set()
            first, replay = first_future.result(5), replay_future.result(5)
            assert first["operation_id"] == replay["operation_id"] == original.operation_id
            assert len(d.remote.dispatches) == 1


def test_authentication_scope_and_untrusted_configuration_cannot_elevate(dispatch):
    d = dispatch
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        params = {"name": ACTION, "arguments": d.args}
        assert _mcp(client, "", "tools/call", 1, params).status_code == 401
        read_token = _token(client, d, scope=READ_SCOPE)
        assert _mcp(client, read_token, "tools/call", 2, params).status_code == 403
        token = _token(client, d)
        for field, value in [("workflow_id", 987), ("branch", "other"), ("oauth_scopes", [READ_SCOPE, DEPLOY_SCOPE]), ("inputs", {"source_sha": OTHER_SHA}), ("api_url", "https://example.com")]:
            response = _mcp(client, token, "tools/call", 3, {"name": ACTION, "arguments": {**d.args, field: value}})
            assert response.status_code == 400
        assert _call(client, token, ACTION, {**d.args, "target_id": "unconfigured"})["status"] == "DENIED"
        assert _call(client, token, ACTION, {**d.args, "idempotency_key": "a" * 129})["status"] == "INVALID_REQUEST"
        assert d.remote.dispatches == []


def test_original_actor_owns_readback_and_journal_bindings_are_immutable(dispatch):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        alice = _token(client, d)
        result = _call(client, alice, ACTION, d.args)
        journal = client.app.state.management_runtime.journal
        record = journal.get(result["operation_id"]).record
        assert record.effect.attempt_id == record.owner_attempt_id != ""
        version, snapshot = journal.lifecycle_snapshot(record.operation_id)
        assert journal.lifecycle_save(record.operation_id, version, {**snapshot, "accepted": {}}) is False
        assert journal.lifecycle_snapshot(record.operation_id) == (version, snapshot)
        assert _read(client, _token(client, d, "bob"), record.operation_id)["status"] == "NOT_FOUND"
        observed = _read(client, alice, record.operation_id)
        assert observed["status"] == "SUCCEEDED"
        confirmed = journal.get(record.operation_id).record
        assert confirmed.owner_attempt_id == record.owner_attempt_id
        assert confirmed.effect.attempt_id == record.effect.attempt_id
        assert confirmed.authority == record.authority
        bound = journal.source_release_run(record.operation_id)
        assert bound["id"] == 900
        assert journal.bind_source_release_run(record, {**bound, "id": 901}) is False
        history = journal.history(record.operation_id).entries
        assert history[0].effect_state is EffectState.NOT_SENT
        assert any(item.effect_state is EffectState.SENT_UNKNOWN for item in history)
        assert history[-1].effect_state is EffectState.CONFIRMED
        assert len(history) == confirmed.revision + 1
        assert [item.revision for item in history] == list(range(confirmed.revision + 1))
        # A future manager's production retained-journal validator requires
        # one coherent history event for every operation revision.
        import sqlite3

        from auto_coder.management.retained_journal import RetainedJournal

        with sqlite3.connect(d.settings.operation_state_root / "management-operations.sqlite3") as connection:
            retained = RetainedJournal._validated_records(connection)
        assert retained == ((confirmed, snapshot),)


def test_lane_reservation_is_durable_before_http_send(dispatch):
    d = dispatch

    def at_send():
        journal = ManagementOperationStore(d.settings.operation_state_root)
        records = journal.list_operations(outstanding_only=True).records
        assert len(records) == 1
        record = records[0]
        assert record.phase is OperationPhase.RECONCILING
        assert record.effect.state is EffectState.SENT_UNKNOWN
        assert record.effect.correlation_identity == record.operation_id
        assert record.effect.attempt_id == record.owner_attempt_id != ""
        snapshot = journal.lifecycle_snapshot(record.operation_id)[1]
        assert snapshot["accepted"]["profile_id"] == "beta-runtime-smoke-v1"
        assert snapshot["workflow_id"] == WORKFLOW_ID
        assert record.intent.expected_artifact == d.args["source_sha"]

    d.remote.on_send = at_send
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "SUCCEEDED"
        posts = [(method, path) for method, path in d.transport_calls if method != "GET"]
        assert posts == [("POST", f"actions/workflows/{WORKFLOW_ID}/dispatches")]


def test_pre_send_reservation_ack_loss_survives_manager_reconstruction(dispatch, monkeypatch):
    d = dispatch
    app = d.app()
    journal = app.state.management_runtime.journal
    original_begin = journal.begin_effect

    def lost_ack(*args, **kwargs):
        original_begin(*args, **kwargs)
        raise RuntimeError("manager loses local acknowledgement after reservation")

    monkeypatch.setattr(journal, "begin_effect", lost_ack)
    with TestClient(app, base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        stored = journal.get(result["operation_id"]).record
        assert stored.effect.state is EffectState.SENT_UNKNOWN
        owner = stored.owner_attempt_id
        assert d.remote.dispatches == []
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        retained = _read(client, token, stored.operation_id)
        assert retained["status"] == "NEEDS_ATTENTION"
        assert retained["effect_state"] == "sent_unknown"
        assert _call(client, token, ACTION, {**d.args, "idempotency_key": "replacement"})["status"] == "BUSY"
        recovered = client.app.state.management_runtime.journal.get(stored.operation_id).record
        assert recovered.owner_attempt_id == owner
        assert recovered.effect == stored.effect
        assert d.remote.dispatches == []


def test_definitely_unsent_failure_replay_cannot_become_new_send(dispatch):
    d = dispatch
    d.remote.after_workflow_read = lambda: d.operating.write_text(d.operating.read_text().replace("full", "observe"))
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        assert first["status"] == "FAILED"
        d.operating.write_text(d.operating.read_text().replace("observe", "full"))
        d.remote.after_workflow_read = None
        replay = _call(client, token, ACTION, d.args)
        assert replay["operation_id"] == first["operation_id"]
        assert replay["status"] == "FAILED"
        assert replay["effect_state"] == "rejected"
        assert d.remote.dispatches == []
        # This independent explicit intent is allowed only because the original
        # operation positively proves no external call occurred.
        other = _call(client, token, ACTION, {**d.args, "idempotency_key": "new-explicit-intent"})
        assert other["status"] == "SUCCEEDED"
        assert other["operation_id"] != first["operation_id"]
        assert len(d.remote.dispatches) == 1


def test_original_bound_workflow_checkout_cannot_change_on_later_reads(dispatch):
    d = dispatch
    d.remote.publish, d.remote.complete = False, False
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        assert first["safe_reason"] == "workflow_pending"
        d.remote.runs[900]["head_sha"] = "c" * 40
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["safe_reason"] == "bound_run_identity_changed"
        assert result["workflow_run"]["head_sha"] == OTHER_SHA
        assert len(d.remote.dispatches) == 1


def test_original_ci_attempt_downgrade_blocks_dispatch(dispatch):
    d = dispatch
    # Production tested-source verifier obtains this original run from REST.
    d.remote.failed_attempts.add((d.args["ci_run_id"], d.args["ci_run_attempt"]))
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "UNAVAILABLE"
        assert d.remote.dispatches == []


def test_successful_dispatch_run_is_not_preselected_and_history_is_not_oci(dispatch):
    d = dispatch
    d.remote.publish = False
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        assert d.remote.runs == {}
        # An old unrelated OCI release cannot stand in for this source catalog.
        d.remote.releases["auto-coder-release-legacy"] = {"id": 10, "tag_name": "auto-coder-release-legacy", "image": "ghcr.io/owner/repo@sha256:" + "a" * 64}
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["source_promotion"] == "UNRESOLVED"
        assert result["worker_deployed"] is False
        encoded = canonical(d.remote.dispatches)
        assert "digest" not in encoded
        assert "ghcr.io" not in encoded
        assert "restart" not in encoded


def test_actual_manager_process_death_at_send_boundary_retains_original_ownership(dispatch, tmp_path):
    import multiprocessing
    import os

    d = dispatch
    marker = tmp_path / "crashed-send.json"

    def child_manager():
        def die_at_http_boundary():
            payload = d.remote.dispatches[-1]
            marker.write_text(canonical(payload))
            os._exit(71)

        d.remote.on_send = die_at_http_boundary
        with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
            _call(client, _token(client, d), ACTION, d.args)
        os._exit(72)

    process = multiprocessing.get_context("fork").Process(target=child_manager)
    process.start()
    process.join(10)
    if process.is_alive():
        process.kill()
        process.join(5)
    assert process.exitcode == 71
    payload = json.loads(marker.read_text())
    operation_id = payload["inputs"]["operation_id"]
    journal = ManagementOperationStore(d.settings.operation_state_root)
    original = journal.get(operation_id).record
    assert original.effect.state is EffectState.SENT_UNKNOWN
    assert original.owner_attempt_id == original.effect.attempt_id != ""
    # The dead process's file lock is released by the OS. Recovery can now
    # observe this row, but cannot replace its original owner or repeat POST.
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _read(client, _token(client, d), operation_id)
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["effect_state"] == "sent_unknown"
        recovered = journal.get(operation_id).record
        assert recovered.effect == original.effect
        assert recovered.owner_attempt_id == original.owner_attempt_id
        assert d.remote.dispatches == []
        assert not any(method == "POST" for method, _ in d.transport_calls)


def test_bounded_second_page_discovers_original_run_without_latest_guess(dispatch):
    d = dispatch
    d.remote.failure = "after"
    d.remote.list_prefix = [{"id": 1000 + index, "display_title": f"unrelated-operation-{index}"} for index in range(100)]
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "SUCCEEDED"
        assert result["workflow_run"]["id"] == 900
        listing_calls = [path for _, path in d.transport_calls if path.startswith(f"actions/workflows/{WORKFLOW_ID}/runs?")]
        assert listing_calls == [f"actions/workflows/{WORKFLOW_ID}/runs?event=workflow_dispatch&branch=main&per_page=100&page={page}" for page in (1, 2)]
        assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("field,value", [("source_sha", OTHER_SHA), ("acceptance_oid", "e" * 40), ("promotion_run_id", 901), ("original_promotion_run_attempt", 2), ("policy_revision", "f" * 64), ("record_type", "oci-release")])
def test_catalog_for_other_original_identity_cannot_complete_operation(dispatch, field, value):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        _, oid = d.remote.refs[f"refs/tags/auto-coder-source-release-{first['operation_id']}"]
        tag = d.remote.tags[oid]
        original = json.loads(tag["message"])
        tag["message"] = canonical({**original, field: value})
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "NEEDS_ATTENTION"
        assert result["source_promotion"] == "UNRESOLVED"
        assert _call(client, token, ACTION, {**d.args, "idempotency_key": "other-intent"})["status"] == "BUSY"
        assert len(d.remote.dispatches) == 1


@pytest.mark.parametrize("field,value", [("path", ".github/workflows/promote-release.yml"), ("id", 987), ("state", "disabled_manually"), ("url", "https://api.github.com/repos/wrong/repo/actions/workflows/321")])
def test_configured_workflow_must_read_back_exactly_before_possible_send(dispatch, field, value):
    d = dispatch
    d.remote.mutate_workflow = lambda workflow: workflow.update({field: value})
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "FAILED"
        assert result["effect_state"] == "rejected"
        assert d.remote.dispatches == []
        assert not any(method == "POST" for method, _ in d.transport_calls)


@pytest.mark.parametrize("remove", ["source_release_policy_path", "source_release_token_file", "source_release_target_id", "source_release_workflow_id"])
def test_trusted_settings_require_complete_operator_dispatch_configuration(dispatch, tmp_path, remove):
    from auto_coder.management.oauth_config import load_management_settings

    d = dispatch
    raw = {
        "state_root": str(d.settings.state_root),
        "public_origin": d.settings.oauth.canonical_issuer,
        "firebase_project_id": d.settings.oauth.firebase_project_id,
        "clients": [{"client_id": "alice-client", "redirect_uris": ["https://console.example.com/callback"]}],
        "source_release_policy_path": str(d.promotion_policy),
        "source_release_token_file": str(d.settings.source_release_token_file),
        "source_release_target_id": "release-source",
        "source_release_workflow_id": WORKFLOW_ID,
    }
    path = tmp_path / "management.yml"
    path.write_text(canonical(raw))
    loaded = load_management_settings(path)
    assert loaded.source_release_target_id == "release-source"
    assert loaded.source_release_workflow_id == WORKFLOW_ID
    assert loaded.source_release_policy_path == d.promotion_policy
    del raw[remove]
    path.write_text(canonical(raw))
    with pytest.raises(ValueError, match="source release configuration must be complete"):
        load_management_settings(path)


def test_policy_downgrade_replay_only_reconciles_original_operation(dispatch):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d)
        first = _call(client, token, ACTION, d.args)
        d.operating.write_text(d.operating.read_text().replace("full", "observe"))
        d.transport_calls.clear()
        replay = _call(client, token, ACTION, d.args)
        assert replay["operation_id"] == first["operation_id"]
        assert replay["status"] == "SUCCEEDED"
        assert replay["safe_reason"] is None
        assert all(method == "GET" for method, _ in d.transport_calls)
        assert len(d.remote.dispatches) == 1


def test_expired_original_grant_allows_only_historical_readback_with_new_read_consent(dispatch):
    d = dispatch
    d.remote.failure = "after"
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        first = _call(client, _token(client, d), ACTION, d.args)
        with d.grants.store.connect() as db:
            db.execute("UPDATE grants SET expires=?", (d.state["now"] - 1,))
    d.transport_calls.clear()
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        token = _token(client, d, scope=READ_SCOPE)
        result = _read(client, token, first["operation_id"])
        assert result["status"] == "SUCCEEDED"
        assert result["worker_deployed"] is False
        assert all(method == "GET" for method, _ in d.transport_calls)
        assert len(d.remote.dispatches) == 1


def test_stale_original_acceptance_is_rejected_without_reselecting_beta(dispatch):
    d = dispatch
    d.smoke.clock.now += 86401
    with TestClient(d.app(), base_url=d.settings.oauth.canonical_issuer) as client:
        result = _call(client, _token(client, d), ACTION, d.args)
        assert result["status"] == "UNAVAILABLE"
        assert result["reason"] == "exact_source_evidence_unavailable"
        assert d.remote.dispatches == []
