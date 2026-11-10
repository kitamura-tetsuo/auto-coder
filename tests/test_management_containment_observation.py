"""Real-entrypoint refusal and read-only identity regressions, not host acceptance.

These run the checked-out production module in independent Linux processes.
They do not claim closed-route, Docker or original-profile positive acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management import containment_observation as observation

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
ROOT = Path(__file__).resolve().parents[1]


def _scope(files: tuple[Path, ...] = (), profile: str = "ordinary-v1") -> observation.ProposedScope:
    return observation.ProposedScope("fixture-deployment", "proposed-enrollment", "proposed-authorization", profile, "a" * 64, "b" * 64, tuple(sorted(str(path) for path in files)))


def _command(tmp_path: Path, scope: observation.ProposedScope, previous: object | None = None) -> dict:
    scope_path = tmp_path / "scope.json"
    scope_path.write_text(json.dumps(asdict(scope)))
    command = [sys.executable, "-B", "-m", "auto_coder.management.containment_observation", "observe" if previous is None else "verify", "--scope", str(scope_path)]
    if previous is not None:
        previous_path = tmp_path / "previous.json"
        previous_path.write_text(json.dumps(previous))
        command += ["--previous", str(previous_path)]
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["status"] == "unavailable"
    return report


def _reasons(report: dict) -> dict[str, str]:
    return {item["authority"]: item["reason"] for item in report["unavailable"]}


@pytest.mark.parametrize("profile", observation.PROFILES)
def test_production_entrypoint_refuses_operator_scope_as_authority(tmp_path: Path, profile: str) -> None:
    # Actual production source is read, not a successful helper stand-in.
    source = ROOT / "src/auto_coder/management/bootstrap_supervisor.py"
    original = source.read_bytes()
    report = _command(tmp_path, _scope((source,), profile))
    assert report["schema"] == "current-containment-prerequisites-v1"
    assert report["scope"]["profile"] == profile
    assert report["files"][0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert report["files"][0]["inode"] == source.stat().st_ino
    assert source.read_bytes() == original
    reasons = _reasons(report)
    assert "supplied identities and file lists select scope only" in reasons["deployment-enrollment"]
    assert "watcher/reconcile, services/timers, socket activation, Docker restart" in reasons["ordinary-route-closure"]
    assert "configured local/remote scripts, repair/recreate and alternative roots" in reasons["ordinary-route-closure"]
    assert "current installation bytes cannot establish original lock-before-launch evidence" in reasons["original-source-profile"]
    assert "saved namespace inode strings cannot establish absence" in reasons["kernel-containment"]
    assert ("quarantined-original-profile" in reasons) == (profile == "quarantined-original-v1")
    assert report["resource_limit"] == "All read descriptors are released before return; no namespace pin or owner fence is retained."
    assert report["started_monotonic_ns"] <= report["finished_monotonic_ns"]
    assert report["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def test_separate_consumer_after_producer_exit_reobserves_real_source_and_complete_scope(tmp_path: Path) -> None:
    source = tmp_path / "controller.py"
    source.write_bytes((ROOT / "src/auto_coder/management/proxy_watch_cli.py").read_bytes())
    scope = _scope((source,))
    saved = _command(tmp_path, scope)
    # subprocess.run has reaped the producer. The consumer receives no live pins.
    assert not Path(f'/proc/{saved["observer_pid"]}').exists()
    source.write_bytes(source.read_bytes() + b"\n# Changed installed source\n")
    current = _command(tmp_path, scope, saved)
    assert current["observer_pid"] != saved["observer_pid"]
    assert current["observation_id"] != saved["observation_id"]
    assert current["files"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert current["files"][0]["sha256"] != saved["files"][0]["sha256"]
    assert _reasons(current)["saved-report:files"] == "Independent current observation differs from saved report."
    assert "deployment-enrollment" in _reasons(current)


@pytest.mark.parametrize("field,value", [("deployment_id", "another-deployment"), ("enrollment_id", "another-enrollment"), ("authorization_id", "another-authorization"), ("profile", "quarantined-original-v1"), ("management_id", "c" * 64)])
def test_cross_scope_saved_report_never_narrows_consumer_scope(tmp_path: Path, field: str, value: str) -> None:
    scope = _scope()
    saved = _command(tmp_path, scope)
    changed = {**asdict(scope), field: value, "files": []}
    current = _command(tmp_path, observation.ProposedScope.parse(changed), saved)
    assert current["scope"][field] == value
    assert {"saved-report:scope", "saved-report:scope_sha256"} <= set(_reasons(current))


def test_new_alternative_root_is_observed_and_refused_not_ignored(tmp_path: Path) -> None:
    primary = tmp_path / "original" / "watcher.json"
    alternative = tmp_path / "alternative" / "watcher.json"
    for path in (primary, alternative):
        path.parent.mkdir()
        path.write_text('{"state_root":"/a/different/root"}')
    saved = _command(tmp_path, _scope((primary,)))
    current = _command(tmp_path, _scope((primary, alternative)), saved)
    assert [item["path"] for item in current["files"]] == sorted((str(primary), str(alternative)))
    assert "saved-report:scope" in _reasons(current)
    assert "alternative roots; none are excluded from scope" in _reasons(current)["ordinary-route-closure"]


@pytest.mark.parametrize("field,value", [("status", "contained"), ("boot_id", "00000000-0000-0000-0000-000000000000"), ("resource_limit", "continuous exclusion")])
def test_replayed_or_authorizing_claim_never_survives_current_verification(tmp_path: Path, field: str, value: str) -> None:
    scope = _scope()
    saved = _command(tmp_path, scope)
    saved[field] = value
    current = _command(tmp_path, scope, saved)
    reason = "saved-report" if field == "status" else "saved-report:" + field
    assert reason in _reasons(current)
    assert current["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def test_original_stores_and_owner_file_are_only_fingerprinted(tmp_path: Path) -> None:
    paths = tuple(tmp_path / name for name in (".supervisor-owner.lock", "oauth.sqlite3", "management-operations.sqlite3", "watcher.json"))
    for path in paths:
        path.write_bytes(b"original retained evidence\x00")
    before = [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in paths]
    saved = _command(tmp_path, _scope(paths))
    current = _command(tmp_path, _scope(paths), saved)
    assert current["files"] == saved["files"]
    assert before == [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_ctime_ns) for path in paths]
    assert not any(key.startswith("saved-report:") for key in _reasons(current))
    assert "kernel-containment" in _reasons(current)


def test_missing_file_and_symlink_component_report_precise_unavailability(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    source = target / "config.json"
    source.write_text("{}")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    missing = tmp_path / "missing.json"
    report = _command(tmp_path, _scope((link / source.name, missing)))
    assert report["files"] == []
    assert "symlink component" in _reasons(report)["file:" + str(link / source.name)]
    assert "No such file" in _reasons(report)["file:" + str(missing)]


def test_docker_remote_override_is_not_local_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://192.0.2.1:2375")
    report = _command(tmp_path, _scope())
    assert report["docker"] == []
    assert _reasons(report)["docker"] == "Docker endpoint override is unsupported for local observation"


def test_scope_duplicate_keys_are_rejected_by_real_entrypoint(tmp_path: Path) -> None:
    scope = tmp_path / "scope.json"
    scope.write_text('{"profile":"ordinary-v1","profile":"quarantined-original-v1"}')
    result = subprocess.run([sys.executable, "-B", "-m", "auto_coder.management.containment_observation", "observe", "--scope", str(scope)], env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {"status": "unavailable", "reason": "duplicate exclusion provenance key"}


def test_file_descriptor_is_released_on_nonregular_refusal(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError, match="not a regular file"):
        observation._file(fifo)
    assert set(os.listdir("/proc/self/fd")) == before


def test_unit_boundary_file_race_is_not_silently_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Controlled interleaving supplements real-process tests; no host oracle."""
    source = tmp_path / "controller.py"
    source.write_text("original source")
    original = observation._file
    reads = 0

    def changed(path: Path) -> tuple[bytes, observation.FileObservation]:
        nonlocal reads
        reads += 1
        if reads == 2:
            source.write_text("substituted source")
        return original(path)

    monkeypatch.setattr(observation, "_file", changed)
    # Avoid even a Docker connection in this explicitly unit-level interleaving.
    monkeypatch.setattr(observation, "_docker", lambda _: ())
    report = observation.observe(_scope((source,)))
    assert reads == 2
    reasons = {item.authority: item.reason for item in report.unavailable}
    assert reasons["file:" + str(source)] == "file identity or content changed during observation"
    assert report.status == "unavailable"
