"""Host upgrade plan and lost-response safety without live Docker mutations."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management import bootstrap_upgrade as upgrade


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
