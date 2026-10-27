"""Authenticated beta deployment against a genuinely installed Docker worker.

Docker and installer egress belong solely to this external CI orchestrator.
The runtime management process only talks to the fixed signed Supervisor bridge.
"""

from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from auto_coder.beta_source_selection import MutationCertainty
from auto_coder.management.action_scopes import DEPLOY_SCOPE, READ_SCOPE
from auto_coder.management.app import create_management_app
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, WorkerNetworkResult
from auto_coder.tested_source_refs import serialize_record
from tests.test_beta_source_selection import selector
from tests.test_management_mcp import _issue, real_boundary
from tests.test_management_runtime_mcp import _call
from tests.test_management_worker_lifecycle import GitEvidenceBoundary
from tests.test_supervisor_container_integration import _container_facts, _docker, _image, _inspect_worker, _wait_for, _worker_observation

pytestmark = [
    pytest.mark.supervisor_integration,
    pytest.mark.skipif(
        not os.environ.get("AUTOCODER_SUPERVISOR_IMAGE") and os.environ.get("AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION") != "1",
        reason="requires externally provisioned production Docker image",
    ),
]


def _result(app, operation_id: str) -> dict[str, object]:
    record = app.state.management_runtime.journal.lifecycle_snapshot(operation_id)
    assert record is not None
    return record[1]


def _await_stage(app, operation_id: str, expected: str, *, timeout: float = 120) -> dict[str, object]:
    return _wait_for(lambda: result if (result := _result(app, operation_id)).get("stage") == expected else None, timeout=timeout)


def _argument(client: WorkerNetworkClient, sha: str, key: str) -> dict[str, object]:
    inspected = client.inspect_supervisor()
    assert inspected.result_code == "OK" and isinstance(inspected.data, dict)
    worker = inspected.data.get("worker")
    assert isinstance(worker, dict) and inspected.data.get("alive") is True
    assert isinstance(worker.get("process_run_id"), str)
    return {
        "target_id": "beta-worker",
        "selected_git_sha": sha,
        "idempotency_key": key,
        "expected_supervisor_generation": inspected.supervisor_generation,
        "expected_process_run_id": worker["process_run_id"],
        "expected_deployment_generation": worker["deployment_generation"],
        "expected_runtime_id": worker["runtime_id"],
    }


def test_authenticated_beta_source_installs_real_version_and_reconciles_after_lost_reply(real_boundary, tmp_path: Path, monkeypatch, _use_real_commands: None, _use_real_sleep: None) -> None:
    """REQ-013: join original Git proof, OAuth, SQLite, Docker and producer beats."""
    image = _image()
    source = Path(os.environ.get("GITHUB_WORKSPACE", str(Path(__file__).parents[1]))).resolve()
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True).stdout.strip()
    runtime, workspace, routing = (tmp_path / name for name in ("runtime", "workspace", "routing"))
    for folder in (runtime, workspace, routing, runtime / "home", runtime / "logs"):
        folder.mkdir()
    (routing / "ownership.json").write_text(json.dumps({"fixture/beta": "beta"}), encoding="utf-8")
    (runtime / "llm_config.toml").write_text('[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "ci-offline-unused"\n', encoding="utf-8")
    secret_bytes = os.urandom(48)
    (runtime / "bridge-secret").write_bytes(base64.b64encode(secret_bytes) + b"\n")
    fixtures = Path(__file__).parent / "fixtures"
    host_user = f"{os.getuid()}:{os.getgid()}"

    # The installed version is produced by uv --locked in a disposable
    # external-egress container, not fabricated by the manager or Supervisor.
    staged = _docker(
        "run",
        "--rm",
        "--network",
        "bridge",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        host_user,
        "-v",
        f"{source}:/source:ro",
        "-v",
        f"{runtime}:/runtime",
        "-v",
        f"{fixtures}:/fixtures:ro",
        "-e",
        "HOME=/runtime/home",
        "--entrypoint",
        "python",
        image,
        "/fixtures/supervisor_stage_ready.py",
        sha,
        timeout=1200,
    )
    ready = json.loads(staged.stdout.strip().splitlines()[-1])["receipt"]
    assert ready["source_sha"] == sha and ready["installed_runtime_id"] != "bootstrap"

    # Exercise the production proof/selection validators over a controlled
    # Git/Actions boundary whose proof matches the *installed* uv receipt.
    remote = GitEvidenceBoundary()
    remote.executions = {(11, 1): (sha, "2026-10-01T01:00:00Z")}
    remote.proofs.clear()
    remote.tags.clear()
    remote.refs.clear()
    remote.add_proof(sha, "c" * 40, 101, 1)
    oid, original = remote.proofs[sha]
    proof = replace(original, distribution_version=ready["distribution_version"], lock_sha256=ready["lock_sha256"])
    remote.proofs[sha] = oid, proof
    remote.tags[oid]["message"] = serialize_record(proof)
    assert selector(remote).advance("r77", None).certainty is MutationCertainty.CONFIRMED
    reader = BetaEvidenceReader(remote, "owner/repo")  # type: ignore[arg-type]
    original_evidence = reader.read(sha).canonical()
    monkeypatch.setattr("auto_coder.management.worker_lifecycle.WorkerLifecycle._source", lambda _self: reader)

    subnet = random.SystemRandom().randrange(64, 239)
    network_name = "real-beta-management-" + uuid.uuid4().hex[:12]
    worker_name = "real-beta-worker-" + uuid.uuid4().hex[:12]
    worker_ip = f"10.251.{subnet}.11"
    _docker("network", "create", "--driver", "bridge", "--internal", "--subnet", f"10.251.{subnet}.0/24", network_name)
    try:
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            worker_name,
            "--network",
            network_name,
            "--ip",
            worker_ip,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            host_user,
            "--entrypoint",
            "python",
            "-v",
            f"{runtime}:/runtime",
            "-v",
            f"{workspace}:/workspace",
            "-v",
            f"{routing}:/routing",
            "-v",
            f"{fixtures}:/fixtures:ro",
            "-v",
            f"{source}:/source:ro",
            "-e",
            "AUTO_CODER_SUPERVISOR_INSTANCE_ID=beta-worker",
            "-e",
            "AUTO_CODER_CHANNEL=beta",
            "-e",
            "AUTO_CODER_REPOSITORY=fixture/beta",
            "-e",
            "AUTO_CODER_RUNTIME_ROOT=/runtime",
            "-e",
            "AUTO_CODER_WORKSPACE_ROOT=/workspace",
            "-e",
            "AUTO_CODER_OWNERSHIP_FILE=/routing/ownership.json",
            "-e",
            "AUTO_CODER_ARTIFACT_DIGEST=ci-isolated",
            "-e",
            "AUTO_CODER_SUPERVISOR_AUTO_START=1",
            "-e",
            "AUTO_CODER_SOURCE_REPOSITORY_ROOT=/source",
            "-e",
            f"AUTO_CODER_BRIDGE_BIND_ADDRESS={worker_ip}",
            "-e",
            "AUTO_CODER_BRIDGE_PORT=8843",
            "-e",
            "AUTO_CODER_BRIDGE_SECRET_FILE=/runtime/bridge-secret",
            "-e",
            "AUTO_CODER_CONFIG_PATH=/runtime/llm_config.toml",
            "-e",
            "AUTO_CODER_HEALTH_LOG_ENABLED=0",
            "-e",
            "AUTO_CODER_DISABLE_AUTO_UPDATE=1",
            "-e",
            "GITHUB_TOKEN=nonfunctional-ci-placeholder",
            "-e",
            "HOME=/runtime/home",
            image,
            "/fixtures/supervisor_offline_pid1.py",
            timeout=60,
        )
        facts = _wait_for(lambda: result if (result := _container_facts(worker_name))["socket"] else None, timeout=30)
        assert facts["docker_socket"] is False and facts["docker_binary"] is None
        _wait_for(lambda: _inspect_worker(worker_name), timeout=90)
        _wait_for(lambda: observed if (observed := _worker_observation(worker_name))["lifecycle"]["daemon_serving"] is True else None, timeout=90)
        config = WorkerEndpointConfig("beta-worker", "beta", "fixture/beta", worker_ip, 8843, secret_bytes)
        network_client = WorkerNetworkClient(config)
        arguments = _argument(network_client, sha, "real-deploy-lost-stop")
        assert arguments["expected_runtime_id"] == "bootstrap"

        base, _base_grants, identity, signed, _uids, state = real_boundary
        origin = base.oauth.canonical_issuer
        oauth = OAuthSettings(base.oauth.database_path, origin, base.oauth.firebase_project_id, (OAuthClient("alice-client", (origin + "/alice-callback",), allowed_scopes=(READ_SCOPE, DEPLOY_SCOPE)),), enabled_scopes=(READ_SCOPE, DEPLOY_SCOPE))
        policy = tmp_path / "operator.yaml"
        policy.write_text("profile: beta\ntargets:\n  beta-worker:\n    channel: beta\n    repositories: [fixture/beta]\n", encoding="utf-8")
        source_token = tmp_path / "source-token"
        source_token.write_text("fixture-source-credential" * 2, encoding="utf-8")
        settings = ManagementSettings(tmp_path, oauth, port=base.port, operating_policy_path=policy, operation_state_root=tmp_path / "journal", source_repository="owner/repo", source_evidence_token_file=source_token)
        grants = OAuthGrantService(oauth, identity, clock=lambda: state["now"])
        sent_stops: list[str] = []
        original_dispatch = network_client.dispatch_worker_operation

        def lose_first_stop(operation_id, operation, **kwargs):
            result = original_dispatch(operation_id, operation, **kwargs)
            if operation == "REQUEST_GRACEFUL_STOP":
                sent_stops.append(operation_id)
                return WorkerNetworkResult(0, "UNKNOWN", None, result.supervisor_generation, result.request_id)
            return result

        monkeypatch.setattr(network_client, "dispatch_worker_operation", lose_first_stop)
        first_app = create_management_app(settings, grants, worker_clients={"beta": network_client})
        with TestClient(first_app, base_url=origin) as http:
            bearer = _issue(http, settings, signed, "alice", "alice-client", READ_SCOPE + " " + DEPLOY_SCOPE)["access_token"]
            accepted = _call(http, bearer, "auto_coder_deploy_beta_source", arguments)
            operation_id = str(accepted["operation_id"])
            _wait_for(lambda: sent_stops if sent_stops else None, timeout=120)
            assert _result(first_app, operation_id)["accepted_evidence"] == original_evidence

        # A new management process/client has no cached bridge incarnation.
        # The original signed effect receipt must be reconciled, not resent.
        fresh_client = WorkerNetworkClient(config)
        assert fresh_client.supervisor_generation is None
        second_app = create_management_app(settings, grants, worker_clients={"beta": fresh_client})
        with TestClient(second_app, base_url=origin) as http:
            _await_stage(second_app, operation_id, "SUCCEEDED", timeout=180)
            assert len(sent_stops) == 1
            assert fresh_client.supervisor_generation == network_client.supervisor_generation
            snapshot = _result(second_app, operation_id)
            assert snapshot["accepted_evidence"] == original_evidence
            assert len(snapshot["heartbeats"]) >= 2
            assert snapshot["staged"]["installed_runtime_id"] == ready["installed_runtime_id"]
            observed = fresh_client.observe_worker()
            assert observed.result_code == "OK" and observed.data is not None
            assert observed.data["binding_status"] == "VERIFIED"
            assert observed.data["runtime_binding"]["source_sha"] == sha
            assert observed.data["runtime_binding"]["content_identity"] == ready["content_identity"]
            assert observed.data["observation"]["artifact"]["distribution_version"]["value"] == ready["distribution_version"]

            # A new successful run selecting the *same* SHA is a distinct
            # authenticated request with unchanged original immutable proof.
            remote.executions[(12, 1)] = (sha, "2026-10-02T03:00:00Z")
            assert selector(remote).advance("r78", sha).certainty is MutationCertainty.CONFIRMED
            new_run = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(fresh_client, sha, "real-deploy-new-run"))
            next_id = str(new_run["operation_id"])
            _await_stage(second_app, next_id, "SUCCEEDED", timeout=180)
            next_evidence = json.loads(str(_result(second_app, next_id)["accepted_evidence"]))
            assert next_evidence["proof_run_id"] == 101
            assert (next_evidence["selected_run_id"], next_evidence["selected_run_attempt"]) == (12, 1)
            assert next_evidence["tested_source_oid"] == json.loads(original_evidence)["tested_source_oid"]

            # At the retry attempt, supersession before the first replacement
            # is fenced even though the selected source SHA is unchanged.
            remote.executions[(12, 2)] = (sha, "2026-10-03T04:00:00Z")
            assert selector(remote).advance("r79", sha).certainty is MutationCertainty.CONFIRMED
            gate_entered, gate_release = threading.Event(), threading.Event()
            lifecycle = second_app.state.management_runtime.lifecycle
            original_step = lifecycle._step
            target: dict[str, str] = {}

            def barrier_step(record, version, snapshot, name, client, operation, runtime_id):
                if name == "drain" and record.operation_id == target.get("id"):
                    gate_entered.set()
                    assert gate_release.wait(timeout=15)
                return original_step(record, version, snapshot, name, client, operation, runtime_id)

            monkeypatch.setattr(lifecycle, "_step", barrier_step)
            retry = _call(http, bearer, "auto_coder_deploy_beta_source", _argument(fresh_client, sha, "real-deploy-retry"))
            target["id"] = str(retry["operation_id"])
            assert gate_entered.wait(timeout=90)
            try:
                retry_evidence = json.loads(str(_result(second_app, target["id"])["accepted_evidence"]))
                assert retry_evidence["proof_run_id"] == 101
                assert (retry_evidence["selected_run_id"], retry_evidence["selected_run_attempt"]) == (12, 2)
                remote.executions[(13, 1)] = (sha, "2026-10-04T05:00:00Z")
                assert selector(remote).advance("r80", sha).certainty is MutationCertainty.CONFIRMED
            finally:
                gate_release.set()
            _await_stage(second_app, target["id"], "SUPERSEDED", timeout=90)
            assert "drain" not in _result(second_app, target["id"])["steps"]

            # Revocation of the accepted original grant must also stop new
            # effects; direct journal inspection remains available afterward.
            revoked_arguments = _argument(fresh_client, sha, "real-deploy-revoked")
            revoked = _call(http, bearer, "auto_coder_deploy_beta_source", revoked_arguments)
            revoked_id = str(revoked["operation_id"])
            grants.revoke("alice-client", bearer)
            _await_stage(second_app, revoked_id, "NEEDS_ATTENTION", timeout=90)
            assert "drain" not in _result(second_app, revoked_id)["steps"]
    finally:
        _docker("rm", "-f", worker_name, check=False)
        _docker("network", "rm", network_name, check=False)
