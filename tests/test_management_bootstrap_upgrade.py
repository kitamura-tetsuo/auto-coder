"""Host upgrade plan and lost-response safety without live Docker mutations."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from auto_coder.management import bootstrap_upgrade as upgrade
from auto_coder.management.operation_store import ManagementOperationStore


def private_file(path: Path, data: bytes = b"{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def operator_root(tmp_path: Path) -> Path:
    root = tmp_path / "operator"
    root.mkdir(mode=0o700)
    return root


def profile(tmp_path: Path) -> upgrade.UpgradeProfile:
    source = tmp_path / "inputs"
    names = (
        "evidence_file",
        "intent_file",
        "bootstrap_config",
        "management_config",
        "prior_lock",
        "guard_dir",
        "mounts_file",
        "successor_mounts_file",
        "successor_proxy_config",
        "old_proxy_config",
        "old_watcher_config",
        "new_watcher_config",
    )
    paths = {}
    for name in names:
        value = source / name
        if name == "guard_dir":
            value.mkdir(parents=True, mode=0o700)
        else:
            private_file(value)
        paths[name] = str(value)
    return upgrade.UpgradeProfile(**paths, successor_proxy_id="b" * 64, successor_proxy_image="sha256:" + "c" * 64, source_sha="d" * 40)


def prepared(root: Path, inputs: upgrade.UpgradeProfile) -> tuple[str, upgrade.UpgradePlan]:
    plan = upgrade.UpgradePlan(
        version=1,
        profile=inputs,
        predecessor_management="a" * 64,
        predecessor_proxy="f" * 64,
        predecessor_management_image="sha256:" + "1" * 64,
        predecessor_proxy_image="sha256:" + "2" * 64,
        predecessor_started_at="2026-11-08T01:00:00Z",
        predecessor_proxy_started_at="2026-11-08T01:00:01Z",
        predecessor_evidence_sha256="1" * 64,
        input_sha256={},
        image_source_identity="d" * 40,
        tested_source_identity="3" * 40,
        supervisor_run_id=123,
        supervisor_run_attempt=1,
        worker_baseline={"beta-worker": {"process_run_id": "run", "deployment_generation": "gen", "supervisor_generation": "sup"}},
        worker_registry_sha256="4" * 64,
        oauth_authority_sha256="5" * 64,
        oauth_grant_sha256={},
        ancestor_identities=(),
        endpoint_port=18080,
        successor_management="9" * 64,
        successor_image="sha256:" + "8" * 64,
        successor_instance="manager-new",
        request_id="a" * 8 + "-" + "b" * 4 + "-" + "c" * 4 + "-" + "d" * 4 + "-" + "e" * 12,
    )
    raw = upgrade._canonical(asdict(plan))
    sha = hashlib.sha256(raw).hexdigest()
    lane = root / sha
    lane.mkdir(mode=0o700)
    upgrade._create_record(lane, "upgrade-plan.json", raw)
    upgrade._create_record(lane, "upgrade-operation.json", upgrade._canonical(asdict(upgrade.UpgradeOperation(sha, sha, "prepared", "", "", ()))))
    return sha, plan


def test_cli_requires_explicit_source_approval_without_any_docker_effect(tmp_path: Path) -> None:
    root = operator_root(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "auto_coder.management.bootstrap_upgrade", "prepare", "--profile", "/no-such-profile", "--auth-token-file", "/no-such-token"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"stage": "prerequisite-blocked", "reason": "source credential use requires explicit operator acceptance"}
    assert not list(root.iterdir())


def test_plan_identity_is_content_addressed_and_rejects_changed_review(tmp_path: Path) -> None:
    root = operator_root(tmp_path)
    inputs = profile(tmp_path)
    sha, plan = prepared(root, inputs)
    assert upgrade._load_plan(root, sha) == plan
    with pytest.raises(upgrade.UpgradeUnavailable, match="exact prepared plan identity"):
        upgrade._load_plan(root, "unapproved")
    private_file(root / sha / "upgrade-plan.json", upgrade._canonical({**asdict(plan), "endpoint_port": 18081}))
    with pytest.raises(upgrade.UpgradeUnavailable, match="immutable plan differs"):
        upgrade._load_plan(root, sha)


def test_repeated_operation_cannot_send_a_saved_unknown_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = operator_root(tmp_path)
    sha, plan = prepared(root, profile(tmp_path))
    operation = upgrade._operation(root, sha)
    monkeypatch.setattr(upgrade, "_immutable_inputs", lambda _: None)
    monkeypatch.setattr(upgrade, "_inspect", lambda _: [None, plan.predecessor_proxy_image, True, 12, "running", *([None] * 8), plan.predecessor_proxy_started_at])
    effects = []

    def interrupted(*arguments: str, timeout: float = 30) -> str:
        effects.append(arguments)
        raise subprocess.TimeoutExpired("docker", timeout)

    monkeypatch.setattr(upgrade, "_docker", interrupted)
    with pytest.raises(subprocess.TimeoutExpired):
        upgrade._effect(root / sha, operation, plan, "stop-proxy", plan.predecessor_proxy)
    persisted = upgrade._operation(root, sha)
    assert persisted.effect == "stop-proxy" and persisted.effect_target == plan.predecessor_proxy
    assert persisted.stage == "stopping" and persisted.completed == ()
    assert effects == [("kill", "--signal=SIGTERM", plan.predecessor_proxy)]
    with pytest.raises(upgrade.UpgradeUnavailable, match="already sent or completed"):
        upgrade._effect(root / sha, persisted, plan, "stop-proxy", plan.predecessor_proxy)
    assert len(effects) == 1


@pytest.mark.parametrize("lost_effect", ["stop-management", "start-management"])
def test_actual_apply_keeps_persisted_uncertain_target_after_lost_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_effect: str) -> None:
    """An apply callback may fail before assigning the already-fsynced effect."""
    root = operator_root(tmp_path)
    sha, plan = prepared(root, profile(tmp_path))
    journal_root = tmp_path / "prior-journal"
    ManagementOperationStore(journal_root)._connect().close()
    journal_stat = (journal_root / "management-operations.sqlite3").stat()

    class Predecessor:
        container_id = plan.predecessor_management
        image_id = plan.predecessor_management_image
        pid = os.getpid()
        instance_id = "manager-old"

    predecessor = Predecessor()
    running = {
        plan.predecessor_proxy: True,
        plan.predecessor_management: True,
        plan.successor_management: False,
    }
    images = {
        plan.predecessor_proxy: plan.predecessor_proxy_image,
        plan.predecessor_management: plan.predecessor_management_image,
        plan.successor_management: plan.successor_image,
    }
    started = {
        plan.predecessor_proxy: plan.predecessor_proxy_started_at,
        plan.predecessor_management: plan.predecessor_started_at,
        plan.successor_management: "2026-11-08T02:00:00Z",
    }
    sent: list[tuple[str, ...]] = []

    def inspect(container: str) -> list[object]:
        active = running[container]
        return [None, images[container], active, 17 if active else 0, "running" if active else "exited", *([None] * 8), started[container]]

    def docker(*arguments: str, timeout: float = 30) -> str:
        sent.append(arguments)
        target = arguments[-1]
        running[target] = arguments[0] == "start"
        lost_stop = arguments[0] == "kill" and target == plan.predecessor_management and lost_effect == "stop-management"
        lost_start = arguments[0] == "start" and target == plan.successor_management and lost_effect == "start-management"
        if lost_stop or lost_start:
            # Physical effect occurs, but the Docker acknowledgement is lost.
            raise subprocess.TimeoutExpired("docker", timeout)
        return ""

    def fenced(_intent: object, _evidence: object, _config: object, _prior: object, _network: object, _guard: object, *, on_fenced: object, on_stopped: object, on_listening: object) -> None:
        on_fenced()
        on_listening()

    monkeypatch.setattr(upgrade, "_immutable_inputs", lambda _: None)
    monkeypatch.setattr(
        upgrade,
        "_fresh_intent",
        lambda _: SimpleNamespace(
            new_container_id=plan.successor_management,
            image_digest=plan.successor_image,
            host_prior_journal_root=str(journal_root),
            prior_journal_device=journal_stat.st_dev,
            prior_journal_inode=journal_stat.st_ino,
        ),
    )
    monkeypatch.setattr(upgrade, "_evidence", lambda _: (predecessor, None, None))
    monkeypatch.setattr(upgrade, "_mount_profile", lambda _: {"management": ()})
    monkeypatch.setattr(upgrade, "capture_prior_container", lambda *args, **kwargs: predecessor)
    monkeypatch.setattr(upgrade, "_candidate_stack", lambda *args: None)
    monkeypatch.setattr(upgrade, "_candidate_image_source", lambda *args: plan.image_source_identity)
    monkeypatch.setattr(upgrade, "_source_authority", lambda *args: object())
    monkeypatch.setattr(
        upgrade,
        "_source_proof",
        lambda *args: (plan.tested_source_identity, plan.supervisor_run_id, plan.supervisor_run_attempt),
    )
    monkeypatch.setattr(upgrade, "_configured_source_credential", lambda _: tmp_path / "fixture-token")
    monkeypatch.setattr(upgrade, "_installed_candidate_preflight", lambda _: None)
    monkeypatch.setattr(upgrade, "_authenticated_baseline", lambda *args: plan.worker_baseline)
    monkeypatch.setattr(upgrade, "_watcher_lock", lambda *args: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(upgrade, "hold_claim_and_guard", fenced)
    monkeypatch.setattr(upgrade, "_inspect", inspect)
    monkeypatch.setattr(upgrade, "_docker", docker)

    with pytest.raises(upgrade.UpgradeUnavailable, match="original effect or host fence requires inspection") as raised:
        upgrade.apply(root, sha, tmp_path / "auth-client", authorize_source_read=True)
    assert raised.value.stage == "needs-attention"

    persisted = upgrade._operation(root, sha)
    assert persisted.stage == "needs-attention"
    assert persisted.effect == lost_effect
    expected_target = plan.predecessor_management if lost_effect == "stop-management" else plan.successor_management
    assert persisted.effect_target == expected_target
    assert persisted.completed == (("stop-proxy",) if lost_effect == "stop-management" else ("stop-proxy", "stop-management"))
    assert persisted.reason == "TimeoutExpired"
    report = upgrade.status(root, sha)
    assert report["stage"] == "needs-attention"
    assert report["uncertain_effect"] == lost_effect
    assert report["uncertain_target"] == expected_target
    assert len(sent) == (2 if lost_effect == "stop-management" else 3)
    assert upgrade._operation(root, sha) == persisted


def test_graceful_stop_observes_exact_target_and_records_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = operator_root(tmp_path)
    sha, plan = prepared(root, profile(tmp_path))
    monkeypatch.setattr(upgrade, "_immutable_inputs", lambda _: None)
    running = [True]

    def observed(_: str) -> list[object]:
        return [None, plan.predecessor_management_image, running[0], 12 if running[0] else 0, "running" if running[0] else "exited", *([None] * 8), plan.predecessor_started_at]

    def stop(*arguments: str, timeout: float = 30) -> str:
        assert arguments == ("kill", "--signal=SIGTERM", plan.predecessor_management)
        running[0] = False
        return ""

    monkeypatch.setattr(upgrade, "_inspect", observed)
    monkeypatch.setattr(upgrade, "_docker", stop)
    previous = upgrade._record_operation(root / sha, upgrade._operation(root, sha), stage="stopping", completed=("stop-proxy",))
    operation = upgrade._effect(root / sha, previous, plan, "stop-management", plan.predecessor_management)
    assert operation.completed == ("stop-proxy", "stop-management")
    assert operation.effect == operation.effect_target == ""
    assert upgrade._operation(root, sha) == operation


def test_status_of_lost_controller_cannot_resume_suspect_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = operator_root(tmp_path)
    sha, plan = prepared(root, profile(tmp_path))
    operation = upgrade._operation(root, sha)
    upgrade._record_operation(root / sha, operation, stage="stopping", effect="stop-management", target="a" * 64, completed=("stop-proxy",))
    monkeypatch.setattr(upgrade, "_inspect", lambda _: [None, plan.predecessor_management_image, False, 0, "exited", *([None] * 8), plan.predecessor_started_at])
    monkeypatch.setattr(upgrade, "_docker", lambda *args, **kwargs: pytest.fail("status must never dispatch another effect"))
    observed = upgrade.status(root, sha)
    assert observed["stage"] == "needs-attention"
    assert observed["uncertain_effect"] == "stop-management"
    assert observed["physical_effect_observation"] == "same-predecessor-stopped"
    assert observed["completed_effects"] == ["stop-proxy"]
    assert upgrade._operation(root, sha).effect == "stop-management"


def test_second_live_operator_cannot_acquire_the_same_lane(tmp_path: Path) -> None:
    root = operator_root(tmp_path)
    with upgrade._exclusive(root):
        with pytest.raises(upgrade.UpgradeUnavailable, match="live operator"):
            with upgrade._exclusive(root):
                pytest.fail("overlapping upgrades acquired the same lane")


def test_independent_operator_process_retains_the_live_barrier(tmp_path: Path) -> None:
    root = operator_root(tmp_path)
    script = """import sys
from pathlib import Path
from auto_coder.management.bootstrap_upgrade import _exclusive
with _exclusive(Path(sys.argv[1])):
    print('LOCK_HELD', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "LOCK_HELD"
        with pytest.raises(upgrade.UpgradeUnavailable, match="live operator"):
            with upgrade._exclusive(root):
                pytest.fail("two different host operators simultaneously acquired the effect lane")
    finally:
        process.communicate(input="\n", timeout=10)
    assert process.returncode == 0


def test_candidate_image_must_retain_both_exact_source_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import tarfile

    source = "c" * 40
    content = json.dumps({"schema_version": 1, "source_revision": source}).encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        entry = tarfile.TarInfo("build_provenance.json")
        entry.size = len(content)
        output.addfile(entry, io.BytesIO(content))
    monkeypatch.setattr(upgrade, "_docker", lambda *args, **kwargs: json.dumps({"Id": "sha256:" + "d" * 64, "Config": {"Labels": {"org.opencontainers.image.revision": source}}}))
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *args, **kwargs: type("Response", (), {"stdout": archive.getvalue()})())
    assert upgrade._candidate_image_source("a" * 64, "sha256:" + "d" * 64) == source
    changed = source[:-1] + "b"
    monkeypatch.setattr(upgrade, "_docker", lambda *args, **kwargs: json.dumps({"Id": "sha256:" + "d" * 64, "Config": {"Labels": {"org.opencontainers.image.revision": changed}}}))
    with pytest.raises(ValueError, match="provenance disagree"):
        upgrade._candidate_image_source("a" * 64, "sha256:" + "d" * 64)


def test_successor_start_receipt_pins_the_actual_process_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = operator_root(tmp_path)
    sha, plan = prepared(root, profile(tmp_path))
    old = upgrade._record_operation(root / sha, upgrade._operation(root, sha), stage="stopping", completed=upgrade._EFFECTS[:2])
    running = [False]
    sent = []

    def observed(container: str) -> list[object]:
        assert container == plan.successor_management
        return [None, plan.successor_image, running[0], 17 if running[0] else 0, "running" if running[0] else "created", *([None] * 8), "2026-11-08T02:00:00Z" if running[0] else "0001-01-01T00:00:00Z"]

    def run(*arguments: str, timeout: float = 30) -> str:
        sent.append(arguments)
        running[0] = True
        return ""

    monkeypatch.setattr(upgrade, "_immutable_inputs", lambda _: None)
    monkeypatch.setattr(upgrade, "_inspect", observed)
    monkeypatch.setattr(upgrade, "_docker", run)
    updated = upgrade._effect(root / sha, old, plan, "start-management", plan.successor_management)
    assert sent == [("start", plan.successor_management)]
    assert updated.effect_receipts == {"start-management": "2026-11-08T02:00:00Z"}
    assert updated.completed == upgrade._EFFECTS[:3]
    assert upgrade._operation(root, sha) == updated
    with pytest.raises(upgrade.UpgradeUnavailable, match="already sent or completed"):
        upgrade._effect(root / sha, updated, plan, "start-management", plan.successor_management)
    assert len(sent) == 1


def test_incomplete_or_conflicting_receipt_cannot_be_reconciled(tmp_path: Path) -> None:
    root = operator_root(tmp_path)
    sha, _ = prepared(root, profile(tmp_path))
    original = upgrade._operation(root, sha)
    malformed = asdict(original)
    malformed["completed"] = upgrade._EFFECTS[:3]
    upgrade._replace_record(root / sha, "upgrade-operation.json", upgrade._canonical(malformed))
    with pytest.raises(upgrade.UpgradeUnavailable, match="operation identity differs"):
        upgrade._operation(root, sha)
    malformed["effect_receipts"] = {"start-management": "2026-11-08T02:00:00Z"}
    malformed["effect"] = "start-proxy"
    malformed["effect_target"] = ""
    upgrade._replace_record(root / sha, "upgrade-operation.json", upgrade._canonical(malformed))
    with pytest.raises(upgrade.UpgradeUnavailable, match="operation identity differs"):
        upgrade._operation(root, sha)


def test_completed_generations_retained_separately_and_uncertain_generation_blocks_prepare(tmp_path: Path) -> None:
    root = operator_root(tmp_path)
    sha, _ = prepared(root, profile(tmp_path))
    old = upgrade._operation(root, sha)
    updated = upgrade.UpgradeOperation(
        sha,
        sha,
        "application-ready",
        "",
        "",
        upgrade._EFFECTS,
        watcher_pid=123,
        watcher_start_token="12345",
        effect_receipts={"start-management": "2026-11-08T02:00:00Z", "start-proxy": "2026-11-08T02:00:01Z"},
    )
    upgrade._replace_record(root / sha, "upgrade-operation.json", upgrade._canonical(asdict(updated)))
    assert upgrade._operation(root, sha) == updated
    another_sha, _ = prepared(root, profile(tmp_path / "next"))
    assert sha != another_sha
    assert upgrade._operation(root, sha) == updated
    assert upgrade._operation(root, another_sha).stage == "prepared"
    # No new generation may silently overwrite or ignore an unresolved plan.
    with pytest.raises(upgrade.UpgradeUnavailable, match="earlier host upgrade is unresolved"):
        upgrade.prepare(root, tmp_path / "does-not-exist.json", tmp_path / "no-token", authorize_source_read=True)
    assert upgrade._operation(root, sha) == updated
    assert upgrade._operation(root, another_sha).stage == "prepared"
    assert old.effect_receipts == {}


def test_operator_cli_forwards_exact_approved_plan_and_does_not_deploy_beta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root = operator_root(tmp_path)
    sha, _ = prepared(root, profile(tmp_path))
    token = tmp_path / "auth-client-token"
    private_file(token, b"not-real")
    called = []

    def fake_apply(operator: Path, identity: str, credential: Path, *, authorize_source_read: bool) -> upgrade.UpgradeOperation:
        called.append((operator, identity, credential, authorize_source_read))
        return upgrade._operation(root, sha)

    monkeypatch.setattr(upgrade, "OPERATOR_ROOT", root)
    monkeypatch.setattr(upgrade, "apply", fake_apply)
    monkeypatch.setattr(sys, "argv", ["bootstrap_upgrade", "apply", "--plan-sha256", sha, "--auth-token-file", str(token), "--authorize-source-read"])
    assert upgrade.main() == 0
    assert json.loads(capsys.readouterr().out)["operation_id"] == sha
    assert called == [(root, sha, token, True)]


def test_authenticated_loopback_refuses_redirect_without_reusing_bearer() -> None:
    observed = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            observed.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{receiver.server_port}/other")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [Thread(target=server.serve_forever, daemon=True) for server in (receiver, redirect)]
    for thread in threads:
        thread.start()
    try:
        with pytest.raises(ValueError, match="loopback response is unavailable"):
            upgrade._mcp_call(redirect.server_port, "disposable-test-bearer", "auto_coder_whoami", {})
        assert observed == []
    finally:
        for server in (receiver, redirect):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_authenticated_loopback_reads_only_a_bounded_json_tool_response() -> None:
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            received.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            content = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"uid": "operator", "scopes": ["read"]}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        result = upgrade._mcp_call(server.server_port, "disposable-test-bearer", "auto_coder_whoami", {})
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result == {"uid": "operator", "scopes": ["read"]}
    assert received == [
        (
            "/mcp",
            "Bearer disposable-test-bearer",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "auto_coder_whoami", "arguments": {}}},
        )
    ]


def test_watcher_must_not_start_if_real_proxy_namespace_is_unproven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = operator_root(tmp_path)
    sha, plan = prepared(root, profile(tmp_path))
    operation = upgrade.UpgradeOperation(
        sha,
        sha,
        "successor-launch-uncertain",
        "",
        "",
        upgrade._EFFECTS[:4],
        effect_receipts={"start-management": "2026-11-08T02:00:00Z", "start-proxy": "2026-11-08T02:00:01Z"},
    )
    upgrade._replace_record(root / sha, "upgrade-operation.json", upgrade._canonical(asdict(operation)))
    monkeypatch.setattr(upgrade, "_immutable_inputs", lambda _: None)
    monkeypatch.setattr(upgrade, "_inspect", lambda _: [None, plan.successor_image, True, 17, "running", *([None] * 8), "2026-11-08T02:00:00Z"])
    monkeypatch.setattr(
        upgrade.DockerProxyObservation,
        "inspect",
        lambda _: {"image": plan.profile.successor_proxy_image, "running": True, "pid": 19, "started": "2026-11-08T02:00:01Z"},
    )
    monkeypatch.setattr(upgrade.DockerProxyObservation, "namespace", lambda _: "net:[222]")
    monkeypatch.setattr(upgrade.os, "readlink", lambda _: "net:[111]")
    monkeypatch.setattr(upgrade.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("unverified watcher must not launch"))
    with pytest.raises(upgrade.UpgradeUnavailable, match="not attached"):
        upgrade._start_watcher(root / sha, operation, plan)
    assert upgrade._operation(root, sha) == operation


@pytest.mark.parametrize(
    "boundary",
    (
        "complete",
        "variable-matrix",
        "legacy-single-job",
        "missing-operator",
        "missing-aggregate",
        "skipped-required",
        "skipped-step",
        "wrong-attempt",
        "wrong-run",
        "wrong-source",
        "missing-shard",
        "duplicate-shard",
        "missing-source-plan",
        "missing-source-receipt",
        "wrong-source-attempt",
    ),
)
def test_source_proof_requires_every_producer_job_at_exact_run_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    """Use the real source receipt validator and native Supervisor step contract.

    Fixture-local GitHub responses are not public source/Actions/image evidence.
    """
    from tests.test_tested_source_refs import FakeShardEvidence

    remote = FakeShardEvidence(tmp_path / "source")
    source = remote.source_sha
    run_id, attempt = 4242, 3
    profiles = {
        "management-self-update": 2 if boundary != "variable-matrix" else 4,
        "fresh-host": 3 if boundary != "variable-matrix" else 2,
        "isolated-containers": 1 if boundary != "variable-matrix" else 5,
    }
    fixed = upgrade._SUPERVISOR_FIXED_STEPS
    matrix = upgrade._SUPERVISOR_MATRIX_STEPS
    definitions = [(name, steps) for name, steps in fixed.items()]
    for profile, count in profiles.items():
        definitions.extend((f"Supervisor {profile} (shard {number})", matrix[profile]) for number in range(1, count + 1))
    jobs = [
        {
            "id": 200 + n,
            "name": name,
            "run_id": run_id,
            "run_attempt": attempt,
            "status": "completed",
            "conclusion": "success",
            "steps": [{"name": step, "status": "completed", "conclusion": "success"} for step in steps],
        }
        for n, (name, steps) in enumerate(definitions)
    ]
    if boundary == "legacy-single-job":
        jobs = [dict(jobs[0], name="Fresh host verification")]
    elif boundary in {"missing-operator", "missing-aggregate"}:
        omitted = "Operator upgrade production-origin Docker" if boundary == "missing-operator" else "Supervisor Integration Tests"
        jobs = [job for job in jobs if job["name"] != omitted]
    elif boundary == "skipped-required":
        jobs[0]["conclusion"] = "skipped"
    elif boundary == "skipped-step":
        jobs[0]["steps"][0]["conclusion"] = "skipped"
    elif boundary == "wrong-attempt":
        jobs[0]["run_attempt"] = attempt - 1
    elif boundary == "wrong-run":
        jobs[0]["run_id"] = run_id + 1
    elif boundary == "missing-shard":
        jobs = [job for job in jobs if job["name"] != "Supervisor fresh-host (shard 2)"]
    elif boundary == "duplicate-shard":
        jobs.append(dict(jobs[-1], id=900))
    elif boundary == "missing-source-plan":
        remote.plan_path.unlink()
    elif boundary == "missing-source-receipt":
        (remote.artifacts_root / "source-test-shard-17-2-1" / "receipt.json").unlink()
    elif boundary == "wrong-source-attempt":
        remote.run_attempt = 1

    run = {
        "head_sha": "b" * 40 if boundary == "wrong-source" else source,
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/supervisor-integration-tests.yml",
        "id": run_id,
        "run_attempt": attempt,
    }

    observed_paths: list[str] = []

    def github_request(method: str, path: str) -> SimpleNamespace:
        assert method == "GET"
        observed_paths.append(path)
        if "/workflows/supervisor-integration-tests.yml/runs?" in path:
            return SimpleNamespace(status=200, data={"workflow_runs": [run]})
        if path.startswith(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page="):
            page = int(path.rsplit("=", 1)[1])
            return SimpleNamespace(
                status=200,
                data={"jobs": jobs[(page - 1) * 100 : page * 100], "total_count": len(jobs)},
            )
        return remote.request(method, path)

    class ControlledClient:
        request = staticmethod(github_request)

    client = ControlledClient()
    proof = SimpleNamespace(
        tag_object_oid="c" * 40,
        record=SimpleNamespace(run_id=17, run_attempt=2),
    )

    def read(requested: str) -> SimpleNamespace:
        assert requested == source
        return proof

    def verify_run(record: object, *, recorded_attempt: bool) -> None:
        assert record is proof.record and recorded_attempt

    authority = SimpleNamespace(client=client, read=read, verify_run=verify_run)
    monkeypatch.setattr(upgrade, "_source_proof_inputs", lambda *_args: (remote.plan_path, remote.artifacts_root))
    if boundary in {"complete", "variable-matrix"}:
        assert upgrade._source_proof(authority, source) == ("c" * 40, run_id, attempt)
    else:
        with pytest.raises(upgrade.UpgradeUnavailable, match="evidence|qualification|cases"):
            upgrade._source_proof(authority, source)
    # The real verifier must reject a missing original plan before requesting
    # any source-job evidence. Every other case reaches the native reader.
    if boundary == "missing-source-plan":
        assert observed_paths == []
    else:
        assert "actions/runs/17/attempts/2" in observed_paths


def test_operator_source_proof_requires_private_original_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "provenance"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(upgrade, "SOURCE_PROOF_ROOT", root)
    source = "a" * 40
    with pytest.raises(upgrade.UpgradeUnavailable, match="protected original source"):
        upgrade._source_proof_inputs(source, 17, 2)
    source_root = root / source
    source_root.mkdir(mode=0o700)
    attempt = source_root / "17-2"
    attempt.mkdir(mode=0o700)
    artifacts = attempt / "artifacts"
    artifacts.mkdir(mode=0o700)
    private_file(attempt / "plan.json", b"{}")
    receipt_dir = artifacts / "source-test-shard-17-2-1"
    receipt_dir.mkdir(mode=0o700)
    private_file(receipt_dir / "receipt.json", b"{}")
    assert upgrade._source_proof_inputs(source, 17, 2) == (attempt / "plan.json", artifacts)
    (receipt_dir / "receipt.json").chmod(0o644)
    with pytest.raises(upgrade.UpgradeUnavailable, match="protected original source"):
        upgrade._source_proof_inputs(source, 17, 2)


def test_fresh_status_asks_child_for_exact_authenticated_observation_without_claiming_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A protected whoami read produces proof; only the observer can seal success."""
    credential = private_file(tmp_path / "fresh-client", b"disposable-authenticated-client")
    observation = "a" * 32
    calls: list[tuple[object, ...]] = []

    def whoami(port: int, token: str, tool: str, arguments: dict[str, object]) -> dict[str, str]:
        calls.append((port, token, tool, arguments))
        return {"uid": "operator"}

    monkeypatch.setattr(upgrade, "_mcp_call", whoami)
    upgrade._request_fresh_readback(18080, credential, {"status": "launched", "observation_id": observation})
    assert calls == [(18080, "disposable-authenticated-client", "auto_coder_whoami", {"observation_id": observation})]

    for invalid in ("short", "g" * 32, "", None):
        with pytest.raises(ValueError, match="fresh observation identity"):
            upgrade._request_fresh_readback(18080, credential, {"status": "launched", "observation_id": invalid})
    assert len(calls) == 1

    def denied(*_args: object) -> dict[str, object]:
        return {"status": "NOT_AUTHORIZED"}

    monkeypatch.setattr(upgrade, "_mcp_call", denied)
    with pytest.raises(ValueError, match="observation-bound authenticated identity unavailable"):
        upgrade._request_fresh_readback(18080, credential, {"status": "launched", "observation_id": observation})
