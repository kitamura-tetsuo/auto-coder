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
import uuid
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
    assert report["schema"] == "current-containment-prerequisites-v3"
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


def _engine_boundary(monkeypatch: pytest.MonkeyPatch) -> tuple[dict, list[tuple[str, ...]]]:
    """Unit protocol fixture, explicitly not Docker/profile acceptance evidence."""
    objects = {
        key
        * 64: {
            "Id": key * 64,
            "Image": "sha256:" + "d" * 64,
            "State": {"Running": False, "Pid": 0, "Status": "exited", "StartedAt": "2026-11-10T00:00:00Z", "FinishedAt": "2026-11-10T00:01:00Z"},
            "HostConfig": {"RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}},
            "RestartCount": 0,
            "Mounts": [],
            "Config": {"Env": ["SECRET=must-not-be-emitted"]},
        }
        for key in "abc"
    }
    calls: list[tuple[str, ...]] = []

    def run(arguments: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(arguments)
        if arguments == ("docker", "container", "ls", "--all", "--quiet", "--no-trunc"):
            output = "\n".join(reversed(objects)) + "\n"
        elif arguments == ("docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"):
            output = json.dumps("unix:///var/run/docker.sock")
        elif arguments == ("docker", "info", "--format", "{{json .ID}}"):
            output = json.dumps("unit-engine")
        else:
            assert arguments[:4] == ("docker", "inspect", "--type", "container")
            output = json.dumps([objects[arguments[4]]])
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(observation.subprocess, "run", run)
    return objects, calls


def test_engine_inventory_includes_unselected_objects_and_exact_restart_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    objects, calls = _engine_boundary(monkeypatch)
    objects["c" * 64]["HostConfig"]["RestartPolicy"] = {"Name": "on-failure", "MaximumRetryCount": 4}
    result = observation._docker(_scope())
    assert [item.container_id for item in result] == [key * 64 for key in "abc"]
    assert result[2] == observation.DockerObservation("unit-engine", "c" * 64, "sha256:" + "d" * 64, observation._digest([objects["c" * 64]]), "on-failure", 4, False, "exited", 0, "2026-11-10T00:00:00Z", "2026-11-10T00:01:00Z", 0, observation._digest([]))
    assert calls.count(("docker", "container", "ls", "--all", "--quiet", "--no-trunc")) == 2
    assert "must-not-be-emitted" not in json.dumps([asdict(item) for item in result])


def test_verifier_reinspects_unselected_restart_route_and_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    objects, calls = _engine_boundary(monkeypatch)
    saved = json.loads(json.dumps(asdict(observation.observe(_scope()))))
    assert len(saved["docker"]) == 3
    assert not any(item["authority"].startswith("docker-restart:") for item in saved["unavailable"])
    # Selected IDs and installed files stay identical. The unselected route changes.
    objects["c" * 64]["HostConfig"]["RestartPolicy"]["Name"] = "unless-stopped"
    objects["c" * 64]["Mounts"] = [{"Type": "bind", "Source": "/alternate-root", "Destination": "/bootstrap", "RW": True}]
    calls.clear()
    current = observation.verify(_scope(), saved)
    reasons = {item.authority: item.reason for item in current.unavailable}
    assert "unless-stopped" in reasons["docker-restart:" + "c" * 64]
    assert reasons["saved-report:docker"] == "Independent current observation differs from saved report."
    assert calls.count(("docker", "inspect", "--type", "container", "c" * 64)) == 2
    assert current.status == "unavailable"
    assert "ordinary-route-closure" in reasons and "deployment-enrollment" in reasons


@pytest.mark.parametrize("policy", [None, {}, {"Name": "", "MaximumRetryCount": 0}, {"Name": "unknown", "MaximumRetryCount": 0}, {"Name": "no", "MaximumRetryCount": True}, {"Name": "no", "MaximumRetryCount": -1}])
def test_unknown_engine_restart_policy_refuses_entire_inventory(monkeypatch: pytest.MonkeyPatch, policy: object) -> None:
    objects, _ = _engine_boundary(monkeypatch)
    objects["c" * 64]["HostConfig"]["RestartPolicy"] = policy
    current = observation.observe(_scope())
    assert current.docker == ()
    assert {item.authority: item.reason for item in current.unavailable}["docker"] == "Docker restart policy unavailable or unsupported"


def test_engine_inventory_change_during_inspection_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _engine_boundary(monkeypatch)
    original = observation.subprocess.run
    enumerations = 0

    def raced(arguments: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        nonlocal enumerations
        result = original(arguments, **kwargs)
        if arguments[1:3] == ("container", "ls"):
            enumerations += 1
            if enumerations == 2:
                result.stdout += "e" * 64 + "\n"
        return result

    monkeypatch.setattr(observation.subprocess, "run", raced)
    current = observation.observe(_scope())
    assert current.docker == ()
    assert {item.authority: item.reason for item in current.unavailable}["docker"] == "Docker engine inventory changed during observation"


@pytest.mark.parametrize("listing", ["abc\n", "a" * 64 + "\n" + "a" * 64 + "\n", ("a" * 64 + "\n") * 513])
def test_engine_inventory_ambiguous_or_oversized_refuses(monkeypatch: pytest.MonkeyPatch, listing: str) -> None:
    def run(arguments: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(arguments, 0, listing, "")

    monkeypatch.setattr(observation.subprocess, "run", run)
    with pytest.raises(ValueError, match="Docker inventory"):
        observation._docker_ids()


@pytest.mark.supervisor_integration
def test_real_docker_inventory_and_separate_consumer(tmp_path: Path) -> None:
    """Real Docker sub-observation only; no closed-profile/controller acceptance."""

    def docker(*arguments: str) -> str:
        return subprocess.run(("docker", *arguments), check=True, capture_output=True, text=True, timeout=30).stdout.strip()

    # Deliberately fail rather than skip when the required engine is inaccessible.
    docker("info", "--format", "{{json .ID}}")
    image = docker("image", "inspect", os.environ.get("AUTOCODER_CONTAINMENT_TEST_IMAGE", os.environ.get("AUTOCODER_FRESH_TEST_BASE_IMAGE", os.environ.get("AUTOCODER_SUPERVISOR_IMAGE", "python:3.12-slim"))), "--format", "{{.Id}}")
    names: list[str] = []
    retained = tmp_path / "retained-evidence"
    retained.write_bytes(b"original evidence must survive")
    before = observation._file(retained)[1]
    source = tmp_path / "bootstrap_supervisor.py"
    source.write_bytes((ROOT / "src/auto_coder/management/bootstrap_supervisor.py").read_bytes())

    def create() -> str:
        name = "ac-containment-test-" + uuid.uuid4().hex
        container = docker("create", "--name", name, "--network", "none", "--restart", "no", "--mount", f"type=bind,src={source},dst=/bootstrap/bootstrap_supervisor.py,readonly", "--entrypoint", "/bin/sleep", image, "120")
        names.append(name)
        return container

    try:
        manager, proxy = create(), create()
        scope = observation.ProposedScope("fixture-deployment", "proposed-enrollment", "proposed-authorization", "ordinary-v1", manager, proxy, (str(source),))
        saved = _command(tmp_path, scope)
        assert {manager, proxy} <= {item["container_id"] for item in saved["docker"]}
        for item in saved["docker"]:
            if item["container_id"] in (manager, proxy):
                assert (item["restart_policy"], item["running"], item["status"], item["pid"]) == ("no", False, "created", 0)
        current = _command(tmp_path, scope, saved)
        assert current["docker"] == saved["docker"]
        assert "saved-report:docker" not in _reasons(current)
        assert current["observer_pid"] != saved["observer_pid"]
        assert "ordinary-route-closure" in _reasons(current)
        # Omitted from proposed scope, yet included in the independent inventory.
        alternate = create()
        docker("update", "--restart", "unless-stopped", alternate)
        changed = _command(tmp_path, scope, saved)
        routes = {item["container_id"]: item for item in changed["docker"]}
        assert routes[alternate]["restart_policy"] == "unless-stopped"
        assert "docker-restart:" + alternate in _reasons(changed)
        assert "saved-report:docker" in _reasons(changed)
        source.write_bytes(source.read_bytes() + b"\n# substituted installed fixture source\n")
        substituted = _command(tmp_path, scope, changed)
        assert "saved-report:files" in _reasons(substituted)
        assert observation._file(retained)[1] == before
    finally:
        # No selected production manager/proxy, image, volume or network is changed.
        for name in reversed(names):
            docker("rm", "-f", name)


def test_adapter_reobserves_real_docker_selected_process_without_claiming_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker protocol unit boundary plus real procfs; not Docker acceptance."""
    child = subprocess.Popen([sys.executable, "-u", "-c", "import sys; print('ready', flush=True); sys.stdin.read()"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        objects, _ = _engine_boundary(monkeypatch)
        objects["a" * 64]["State"].update(Running=True, Pid=child.pid, Status="running")
        saved = observation.observe(_scope())
        assert len(saved.processes) == 1
        assert saved.processes[0].pid == child.pid
        assert saved.processes[0].unavailable == ()
        assert saved.processes[0].tasks[0].start_token == saved.processes[0].start_token
        # Keep Docker's selected numeric PID unchanged, as with stale engine data.
        child.kill()
        child.communicate(timeout=10)
        current = observation.verify(_scope(), json.loads(json.dumps(asdict(saved))))
        assert current.processes[0].unavailable
        assert current.processes[0].tasks == ()
        reasons = {item.authority: item.reason for item in current.unavailable}
        assert reasons["saved-report:processes"] == "Independent current observation differs from saved report."
        assert {"kernel-containment", "deployment-enrollment", "ordinary-route-closure"} <= set(reasons)
        assert current.status == "unavailable"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)
