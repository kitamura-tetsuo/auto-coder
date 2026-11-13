"""Component regressions with real Linux flocks; Docker/trust are fixture seams.

These tests are not native systemd/Docker containment acceptance or genesis proof.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import pytest

from auto_coder.management import containment_enrollment as protected
from auto_coder.management import recovery_scope_observation as recovery
from auto_coder.management.fresh_host_observation import HostObservationUnavailable

_HOLDER = """
import fcntl, json, os, sys
fds = []
for path, mode in json.loads(sys.argv[1]):
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, (fcntl.LOCK_SH if mode == 'READ' else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    fds.append(fd)
print(json.dumps(fds), flush=True)
for line in sys.stdin:
    command, index = line.split()
    fd = fds[int(index)]
    if command == 'unlock':
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif command == 'close':
        os.close(fd)
    elif command in ('reopen', 'reopen-retain'):
        path = os.readlink('/proc/self/fd/' + str(fd))
        if command == 'reopen-retain':
            os.dup(fd)
        os.close(fd)
        assert os.open(path, os.O_RDONLY) == fd
    print('done', flush=True)
"""


def _private(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


@dataclass
class ScopeFixture:
    root: Path
    raw: dict
    inspections: dict
    exec_inspections: dict
    holder: subprocess.Popen
    enrollment_path: Path
    producers: list[tuple[int, int]] = field(default_factory=list)

    def save(self) -> None:
        _private(self.enrollment_path, json.dumps(self.raw).encode())

    def command(self, command: str, index: int) -> None:
        self.holder.stdin.write(f"{command} {index}\n")
        self.holder.stdin.flush()
        assert self.holder.stdout.readline().strip() == "done"

    def pair_digest(self, index: int, role: str) -> None:
        pair = self.raw["originals"][index]["pair"] if index < 2 else self.raw["target"]
        pair[role + "_inspection_sha256"] = recovery._digest(recovery.canonical_inspection(self.inspections[pair[role + "_id"]]))
        self.save()


@pytest.fixture
def scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The host-root trust and Docker seams are explicitly substituted. All
    # file reads, holder lifetimes, pidfds, fdinfo and flocks below are real.
    monkeypatch.setattr(recovery, "_require_capture_host", lambda: None)
    monkeypatch.setattr(protected, "_trusted_ancestors", lambda path, *, uid: None)
    monkeypatch.setattr(recovery, "_trusted_ancestors", lambda path, *, uid: None)
    monkeypatch.setattr(recovery, "_approved_directory", lambda path, *, uid: protected._approved_directory(path, uid=os.getuid()))
    monkeypatch.setattr(recovery, "_protected", lambda path, *, uid, size_limit: protected._protected(path, uid=os.getuid(), size_limit=size_limit))
    custody = recovery._custody
    monkeypatch.setattr(recovery, "_custody", lambda selection, mode, boot: custody(selection, mode, boot, expected_uid=os.getuid()))
    enrollment_root = tmp_path / "enrollment"
    enrollment_root.mkdir(mode=0o700)
    monkeypatch.setattr(recovery, "RECOVERY_ENROLLMENT_ROOT", enrollment_root)
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        monkeypatch.delenv(key, raising=False)
    pairs, inspections, lock_paths, configs = [], {}, [], []
    for index in range(3):
        manager, proxy = format(index * 2 + 1, "064x"), format(index * 2 + 2, "064x")
        state = tmp_path / f"generation-{index}" / "state"
        owner = state / ".supervisor-owner.lock"
        _private(owner)
        watcher_root = tmp_path / f"generation-{index}" / "watcher"
        lock = watcher_root / "proxy-watch.lock"
        _private(lock)
        image = "sha256:" + "a" * 64
        for identity in (manager, proxy):
            inspections[identity] = {"Id": identity, "Image": image, "Mounts": [{"Type": "bind", "Source": str(state), "Destination": "/management-child-state", "RW": True}], "State": {"Status": "exited" if index < 2 else "running"}}
        pairs.append({"manager_id": manager, "proxy_id": proxy, "manager_inspection_sha256": recovery._digest(inspections[manager]), "proxy_inspection_sha256": recovery._digest(inspections[proxy])})
        config = watcher_root / "config.json"
        _private(config, json.dumps({"state_root": str(watcher_root), "manager_id": manager, "manager_image": image, "proxy_id": proxy, "proxy_image": image, "config_sha256": "b" * 64}).encode())
        configs.append(config)
        if index < 2:
            lock_paths.append((str(owner), "READ"))
    lock_paths.extend((str(path.parent / "proxy-watch.lock"), "WRITE") for path in configs)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, json.dumps(lock_paths)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        fds = json.loads(holder.stdout.readline())
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        token = recovery._holder_start_token(holder.pid)

        def selection(index: int) -> dict:
            path = Path(lock_paths[index][0])
            info = path.stat()
            return asdict(recovery.CustodySelection(str(path), info.st_dev, info.st_ino, holder.pid, token, fds[index], boot))

        raw = {
            "schema": "approved-recovery-custody-scope-v1",
            "deployment_id": "fixture",
            "profile": "ordinary-recovery-custody-component-v1",
            "engine_id": "fixture-engine",
            "originals": [{"pair": pair, "owner": selection(index)} for index, pair in enumerate(pairs[:2])],
            "target": pairs[2],
            "watchers": [{"manager_id": pair["manager_id"], "config_path": str(config), "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(), "lock": selection(index + 2)} for index, (pair, config) in enumerate(zip(pairs, configs))],
        }
        exec_inspections = {
            identity: {
                "ID": identity,
                "ContainerID": "f" * 64,
                "Running": True,
                "ProcessConfig": {"entrypoint": "python", "arguments": ["worker_check.py"]},
            }
            for identity in ("d" * 64, "e" * 64)
        }
        fixture = ScopeFixture(tmp_path, raw, inspections, exec_inspections, holder, enrollment_root / "fixture.json")
        fixture.save()
        monkeypatch.setattr(recovery, "_docker_ids", lambda: tuple(sorted(inspections)))

        def docker(arguments):
            if arguments[0] == "context":
                return "unix:///var/run/docker.sock"
            if arguments[0] == "info":
                return "fixture-engine"
            assert arguments[:3] == ("inspect", "--type", "container")
            return [copy.deepcopy(inspections[arguments[3]])]

        def docker_exec(exec_id, engine_id):
            assert engine_id == "fixture-engine"
            return copy.deepcopy(exec_inspections[exec_id])

        monkeypatch.setattr(recovery, "_docker_exec_inspection", docker_exec)
        monkeypatch.setattr(recovery, "_docker_read", docker)
        yield fixture
    finally:
        holder.terminate()
        holder.communicate(timeout=5)
        if "fixture" in locals():
            for pid, release_fd in fixture.producers:
                os.close(release_fd)
                os.waitpid(pid, 0)


def test_actual_overlapping_shared_and_exclusive_custody_is_component_only(scope: ScopeFixture):
    before = {path: path.read_bytes() for path in scope.root.rglob("*") if path.is_file()}
    report = recovery.observe_recovery_custody("fixture")
    assert report.status == "observed-component-only"
    assert report.incomplete == recovery._MISSING
    assert len(report.engine_inventory) == 6
    assert [fact.mode for fact in report.custody] == ["READ", "READ", "WRITE", "WRITE", "WRITE"]
    for fact in report.custody:
        fd = os.open(fact.selection.path, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if fact.mode == "READ":
                # Prove material overlap: an independent reader succeeds while
                # the issuer's actual shared lock continues to block a writer.
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                recovery._assert_pid_flock(scope.holder.pid, fd, "READ")
            else:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    assert before == {path: path.read_bytes() for path in scope.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("mutation", ["empty", "omitted-watcher", "unknown-watcher", "duplicate-generation", "target-alias", "resource-alias", "boolean-pid", "huge-pid", "extra-field", "legacy-profile", "relative-path"])
def test_bad_enrollment_refuses(scope: ScopeFixture, mutation: str):
    raw = scope.raw
    if mutation == "empty":
        raw["originals"] = []
    elif mutation == "omitted-watcher":
        raw["watchers"].pop()
    elif mutation == "unknown-watcher":
        raw["watchers"][0]["manager_id"] = "f" * 64
    elif mutation == "duplicate-generation":
        raw["originals"].append(raw["originals"][0])
    elif mutation == "target-alias":
        raw["target"] = raw["originals"][0]["pair"]
    elif mutation == "resource-alias":
        raw["watchers"][0]["lock"] = raw["originals"][0]["owner"]
    elif mutation == "boolean-pid":
        raw["originals"][0]["owner"]["holder_pid"] = True
    elif mutation == "huge-pid":
        raw["originals"][0]["owner"]["holder_pid"] = 2**64
    elif mutation == "extra-field":
        raw["grant"] = True
    elif mutation == "legacy-profile":
        raw["profile"] = "ordinary-v1"
    else:
        raw["originals"][0]["owner"]["path"] = "relative/owner.lock"
    scope.save()
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("mutation", ["unknown-stopped", "missing-target", "foreign-engine", "changed-inspection", "changing-inventory"])
def test_engine_membership_is_unfiltered_and_repeated(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch, mutation: str):
    if mutation == "unknown-stopped":
        scope.inspections["f" * 64] = {"State": {"Status": "exited"}}
    elif mutation == "missing-target":
        scope.inspections.pop(scope.raw["target"]["manager_id"])
    elif mutation == "changed-inspection":
        scope.inspections[scope.raw["target"]["manager_id"]]["foreign"] = True
    elif mutation == "changing-inventory":
        ids = tuple(sorted(scope.inspections))
        calls = iter((ids, ids, ids[:-1]))
        monkeypatch.setattr(recovery, "_docker_ids", lambda: next(calls))
    else:
        docker = recovery._docker_read
        monkeypatch.setattr(recovery, "_docker_read", lambda args: "foreign-engine" if args[0] == "info" else docker(args))
    reason = {
        "unknown-stopped": "Docker object identity differs",
        "missing-target": "complete engine membership differs",
        "foreign-engine": "local system engine differs",
        "changed-inspection": "enrolled full Docker inspection differs",
        "changing-inventory": "complete engine membership differs",
    }[mutation]
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="^" + reason + "$"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("key", ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"])
def test_environment_cannot_redirect_engine(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch, key: str):
    monkeypatch.setenv(key, "foreign")
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="override"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("mutation", ["wrong-owner-path", "target-root", "shadow-state", "watcher-config", "wrong-watcher-lock"])
def test_real_resource_binding_refuses(scope: ScopeFixture, mutation: str):
    if mutation == "wrong-owner-path":
        scope.raw["originals"][0]["owner"]["path"] += ".invented"
        scope.save()
    elif mutation == "target-root":
        scope.inspections[scope.raw["target"]["manager_id"]]["Mounts"] = scope.inspections[scope.raw["originals"][0]["pair"]["manager_id"]]["Mounts"]
        scope.pair_digest(2, "manager")
    elif mutation == "shadow-state":
        scope.inspections[scope.raw["originals"][0]["pair"]["manager_id"]]["Mounts"].append({"Type": "bind", "Source": str(scope.root), "Destination": "/management-child-state/.supervisor-owner.lock", "RW": True})
        scope.pair_digest(0, "manager")
    elif mutation == "watcher-config":
        _private(Path(scope.raw["watchers"][0]["config_path"]), b"{}")
    else:
        scope.raw["watchers"][0]["lock"]["path"] += ".invented"
        scope.save()
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="lock|overlap|shadow|configuration"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("mutation", ["unlock", "close", "reopen", "replace", "foreign-boot", "stale-lifetime", "death", "missing", "mode"])
def test_actual_custody_refuses_missing_changed_or_exited_evidence(scope: ScopeFixture, mutation: str):
    recovery.observe_recovery_custody("fixture")
    owner = scope.raw["originals"][0]["owner"]
    path = Path(owner["path"])
    if mutation in ("unlock", "close", "reopen"):
        scope.command(mutation, 0)
    elif mutation == "replace":
        path.rename(path.with_suffix(".retained"))
        _private(path)
    elif mutation == "foreign-boot":
        owner["holder_boot_id"] = "00000000-0000-0000-0000-000000000001"
        scope.save()
    elif mutation == "stale-lifetime":
        owner["holder_start_token"] = "1"
        scope.save()
    elif mutation == "death":
        scope.holder.terminate()
        scope.holder.wait(timeout=5)
    elif mutation == "missing":
        path.unlink()
    else:
        path.chmod(0o644)
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.observe_recovery_custody("fixture")
    if mutation == "missing":
        assert not path.exists(), "observer must never create a missing lock"


def test_repeated_read_detects_unlock_during_observation(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch):
    snapshot = recovery._snapshot
    calls = 0

    def changed(enrollment):
        nonlocal calls
        calls += 1
        if calls == 2:
            scope.command("unlock", 0)
        return snapshot(enrollment)

    monkeypatch.setattr(recovery, "_snapshot", changed)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="fence"):
        recovery.observe_recovery_custody("fixture")


def _produce_from_separate_process(scope: ScopeFixture) -> dict:
    # fork preserves the fixture I/O seams but the observer has its own real
    # process lifetime. It acquires no locks and waits for fixture cleanup.
    read_fd, write_fd = os.pipe()
    hold_fd, release_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.close(release_fd)
        try:
            with os.fdopen(write_fd, "w") as stream:
                json.dump(asdict(recovery.observe_recovery_custody("fixture")), stream)
            os.read(hold_fd, 1)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(write_fd)
    os.close(hold_fd)
    scope.producers.append((pid, release_fd))
    with os.fdopen(read_fd) as stream:
        result = json.load(stream)
    return result


def test_separate_verifier_repeats_with_live_producer(scope: ScopeFixture):
    previous = _produce_from_separate_process(scope)
    current = recovery.verify_recovery_custody("fixture", previous)
    assert current.observer_pid == os.getpid()
    assert current.status == "observed-component-only"
    assert current.incomplete == recovery._MISSING
    assert previous["observer_pid"] != current.observer_pid
    assert previous["observation_id"] != current.observation_id


@pytest.mark.parametrize("mutation", ["foreign-scope", "stale", "future", "boolean-time", "grant", "partial", "resource", "unknown-field", "same-consumer", "dead-holder", "producer-exit", "producer-lifetime", "boolean-resource", "malformed-uuid"])
def test_separate_verifier_refuses_saved_claims(scope: ScopeFixture, mutation: str):
    previous = _produce_from_separate_process(scope)
    if mutation == "foreign-scope":
        previous["enrollment"]["deployment_id"] = "foreign"
    elif mutation == "stale":
        previous["started_monotonic_ns"] -= recovery._MAX_AGE_NS
    elif mutation == "future":
        previous["finished_monotonic_ns"] += recovery._MAX_AGE_NS
    elif mutation == "boolean-time":
        previous["started_monotonic_ns"] = True
    elif mutation == "grant":
        previous["status"] = "ready"
    elif mutation == "partial":
        previous["incomplete"] = []
    elif mutation == "resource":
        previous["custody"].pop()
    elif mutation == "unknown-field":
        previous["epoch"] = 1
    elif mutation == "same-consumer":
        previous["observer_pid"] = os.getpid()
    elif mutation == "producer-exit":
        pid, release_fd = scope.producers.pop()
        os.close(release_fd)
        assert os.waitpid(pid, 0)[1] == 0
    elif mutation == "producer-lifetime":
        previous["observer_start_token"] = "1"
    elif mutation == "boolean-resource":
        previous["custody"][0]["size"] = False
    elif mutation == "malformed-uuid":
        previous["observation_id"] = "-" * 36
    else:
        scope.holder.terminate()
        scope.holder.wait(timeout=5)
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.verify_recovery_custody("fixture", previous)


def test_inherited_shared_descriptor_is_not_an_independent_acquisition(scope: ScopeFixture):
    selection = recovery.CustodySelection.parse(scope.raw["originals"][0]["owner"])
    # This process independently acquires a second shared flock, then forks.
    # The child retains the FD but /proc records the parent's acquisition.
    fd = os.open(selection.path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(write_fd)
        os.read(read_fd, 1)
        os._exit(0)
    os.close(read_fd)
    try:
        inherited = replace(selection, holder_pid=child, holder_start_token=recovery._holder_start_token(child), holder_fd=fd)
        with pytest.raises(HostObservationUnavailable, match="fence"):
            with recovery._custody(inherited, "READ", selection.holder_boot_id):
                pytest.fail("inherited acquisition must refuse")
    finally:
        os.close(write_fd)
        os.waitpid(child, 0)
        os.close(fd)


def test_duplicate_json_keys_and_unprotected_enrollment_refuse(scope: ScopeFixture):
    scope.enrollment_path.write_text('{"schema": 1, "schema": 2}')
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="duplicate"):
        recovery.observe_recovery_custody("fixture")
    scope.save()
    scope.enrollment_path.chmod(0o644)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="protected"):
        recovery.observe_recovery_custody("fixture")


def test_component_has_no_execution_or_recovery_events(scope: ScopeFixture):
    from auto_coder import execution_trace

    collector = execution_trace.get_trace_collector()
    previous = _produce_from_separate_process(scope)
    handle = collector.start_execution("fixture/recovery", "issue", 2840, "component-regression")
    with execution_trace.bind_scope(handle.scope):
        before = collector.get_snapshot()
        recovery.verify_recovery_custody("fixture", previous)
        scope.command("unlock", 2)
        with pytest.raises(recovery.RecoveryScopeUnavailable):
            recovery.observe_recovery_custody("fixture")
        assert collector.get_snapshot() == before


@pytest.mark.parametrize("mutation", ["enrollment", "config", "lock-content"])
def test_changes_inside_sampling_window_refuse(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch, mutation: str):
    snapshot = recovery._snapshot
    calls = 0

    def changing(enrollment):
        nonlocal calls
        calls += 1
        if calls == 2:
            if mutation == "enrollment":
                # Even semantically equal enrollment bytes changing refuse.
                scope.enrollment_path.write_text(json.dumps(scope.raw, indent=2))
            elif mutation == "config":
                Path(scope.raw["watchers"][0]["config_path"]).write_text("{}")
            else:
                Path(scope.raw["originals"][0]["owner"]["path"]).write_text("changed")
        return snapshot(enrollment)

    monkeypatch.setattr(recovery, "_snapshot", changing)
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("mode,index", [("WRITE", 0), ("READ", 2)])
def test_wrong_actual_flock_mode_is_not_accepted(scope: ScopeFixture, mode: str, index: int):
    raw = scope.raw["originals"][0]["owner"] if index == 0 else scope.raw["watchers"][0]["lock"]
    selection = recovery.CustodySelection.parse(raw)
    with pytest.raises(HostObservationUnavailable, match="fence"):
        with recovery._custody(selection, mode, selection.holder_boot_id):
            pytest.fail("wrong actual kernel mode must refuse")


@pytest.mark.parametrize("mutation", ["deployment", "symlink", "directory-mode", "too-many"])
def test_fixed_protected_enrollment_boundary(scope: ScopeFixture, mutation: str):
    if mutation == "deployment":
        scope.raw["deployment_id"] = "foreign"
        scope.save()
    elif mutation == "symlink":
        target = scope.enrollment_path.with_suffix(".saved")
        scope.enrollment_path.rename(target)
        scope.enrollment_path.symlink_to(target)
    elif mutation == "directory-mode":
        scope.enrollment_path.parent.chmod(0o755)
    else:
        scope.raw["originals"] *= 17
        scope.save()
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.observe_recovery_custody("fixture")


def test_public_entry_requires_native_host_before_reading_enrollment(monkeypatch: pytest.MonkeyPatch):
    def unsupported():
        raise ValueError("initial-namespace host-root capture is required")

    monkeypatch.setattr(recovery, "_require_capture_host", unsupported)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="initial-namespace"):
        recovery.observe_recovery_custody("../../untrusted")


def test_same_holder_inode_lock_on_a_different_fd_is_insufficient(scope: ScopeFixture):
    recovery.observe_recovery_custody("fixture")
    scope.command("reopen-retain", 0)
    # /proc/locks still names this PID and inode. Only exact fdinfo inspection
    # distinguishes the enrolled unlocked descriptor from the retained lock.
    owner = scope.raw["originals"][0]["owner"]
    fd = os.open(owner["path"], os.O_RDONLY)
    try:
        recovery._assert_pid_flock(scope.holder.pid, fd, "READ")
    finally:
        os.close(fd)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="independent whole-file"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("mutation", ["original-inspection", "target-inspection", "unknown-member", "owner-inode", "watcher-config"])
def test_verifier_reobserves_real_mutations_after_positive_producer(scope: ScopeFixture, mutation: str):
    previous = _produce_from_separate_process(scope)
    if mutation == "original-inspection":
        scope.inspections[scope.raw["originals"][0]["pair"]["manager_id"]]["Changed"] = True
    elif mutation == "target-inspection":
        scope.inspections[scope.raw["target"]["manager_id"]]["Changed"] = True
    elif mutation == "unknown-member":
        scope.inspections["f" * 64] = {"State": {"Status": "exited"}}
    elif mutation == "owner-inode":
        path = Path(scope.raw["originals"][0]["owner"]["path"])
        path.rename(path.with_suffix(".retained"))
        _private(path)
    else:
        Path(scope.raw["watchers"][0]["config_path"]).write_text("{}")
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.verify_recovery_custody("fixture", previous)


def test_distinct_bind_paths_cannot_hide_same_kernel_file(scope: ScopeFixture):
    original = scope.root / "generation-0" / "config-source"
    target = scope.root / "generation-2" / "config-source"
    _private(original)
    os.link(original, target)
    for index, path in ((0, original), (2, target)):
        pair = scope.raw["originals"][index]["pair"] if index == 0 else scope.raw["target"]
        scope.inspections[pair["manager_id"]]["Mounts"].append({"Type": "bind", "Source": str(path), "Destination": "/bootstrap/config.json", "RW": False})
        scope.pair_digest(index, "manager")
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="identities alias"):
        recovery.observe_recovery_custody("fixture")


def test_target_root_inode_change_after_producer_refuses(scope: ScopeFixture):
    previous = _produce_from_separate_process(scope)
    root = Path(scope.inspections[scope.raw["target"]["manager_id"]]["Mounts"][0]["Source"])
    root.rename(root.with_name("retained-state"))
    root.mkdir()
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="component differs"):
        recovery.verify_recovery_custody("fixture", previous)


def test_deeply_nested_enrollment_is_typed_refusal(scope: ScopeFixture):
    # A protected input can be small yet exceed the JSON decoder depth limit.
    scope.enrollment_path.write_text("[" * 12000 + "0" + "]" * 12000)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unavailable|recursion|depth|maximum"):
        recovery.observe_recovery_custody("fixture")


def test_deeply_nested_saved_evidence_is_typed_refusal(scope: ScopeFixture):
    previous = _produce_from_separate_process(scope)
    nested = None
    for _ in range(12000):
        nested = [nested]
    previous["enrollment"] = nested
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="malformed|serialization"):
        recovery.verify_recovery_custody("fixture", previous)


def test_duplicate_engine_inventory_is_not_exact_membership(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch):
    # Repeating an enrolled ID must not turn an ambiguous inventory into closure.
    selected = tuple(sorted(scope.inspections))
    monkeypatch.setattr(recovery, "_docker_ids", lambda: selected + (selected[0],))
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="membership"):
        recovery.observe_recovery_custody("fixture")


def test_saved_producer_exiting_during_evidence_comparison_refuses(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch):
    # The original producer must remain alive through the last comparison,
    # not just at the beginning of consumer verification.
    previous = _produce_from_separate_process(scope)
    original_digest = recovery._digest
    exited = False

    def digest_after_exit(value: object) -> str:
        nonlocal exited
        if not exited and isinstance(value, dict) and value.get("status") == "observed-component-only" and "enrollment" in value:
            pid, release_fd = scope.producers.pop()
            os.close(release_fd)
            assert os.waitpid(pid, 0)[1] == 0
            exited = True
        return original_digest(value)

    monkeypatch.setattr(recovery, "_digest", digest_after_exit)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="producer lifetime"):
        recovery.verify_recovery_custody("fixture", previous)
    assert exited


def _unrelated_worker(scope: ScopeFixture) -> dict:
    """A complete independent Docker object, not an operator classification."""
    root = scope.root / "worker"
    root.mkdir()
    obj = {
        "Id": "f" * 64,
        "Image": "sha256:" + "c" * 64,
        "Config": {"Cmd": ["python", "worker.py"], "Entrypoint": None},
        "Path": "python",
        "Args": ["worker.py"],
        "HostConfig": {"NetworkMode": "bridge", "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0}},
        "State": {"Running": True, "Pid": 12345, "Status": "running", "StartedAt": "2026-11-01T00:00:00Z", "FinishedAt": "0001-01-01T00:00:00Z"},
        "RestartCount": 0,
        "Mounts": [{"Type": "bind", "Source": str(root), "Destination": "/work", "RW": True}],
        "ExecIDs": ["d" * 64],
    }
    scope.inspections[obj["Id"]] = obj
    return obj


def test_independent_worker_preserved_and_exec_ids_are_volatile(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch):
    worker = _unrelated_worker(scope)
    before = copy.deepcopy(scope.inspections)
    previous = _produce_from_separate_process(scope)
    worker["ExecIDs"] = ["e" * 64]
    reads = []
    inventories = []
    docker, ids = recovery._docker_read, recovery._docker_ids

    def read(arguments):
        reads.append(arguments)
        return docker(arguments)

    def inventory():
        result = ids()
        inventories.append(result)
        return result

    monkeypatch.setattr(recovery, "_docker_read", read)
    monkeypatch.setattr(recovery, "_docker_ids", inventory)
    current = recovery.verify_recovery_custody("fixture", previous)
    assert inventories == [tuple(sorted(scope.inspections))] * 4
    assert all(reads.count(("inspect", "--type", "container", identity)) == 2 for identity in scope.inspections)
    assert current.engine_inventory == tuple(sorted(scope.inspections))
    assert current.unrelated_inspections == ((worker["Id"], recovery._digest(recovery.unrelated_inspection(worker))),)
    assert current.status == "observed-component-only"
    assert current.incomplete == recovery._MISSING
    assert previous["unrelated_inspections"] == [list(current.unrelated_inspections[0])]
    before[worker["Id"]]["ExecIDs"] = ["e" * 64]
    assert scope.inspections == before


def test_exec_inspection_uses_read_only_local_engine_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercise actual Unix HTTP transport without touching a live Docker daemon."""
    socket_path = tmp_path / "docker.sock"
    exec_id = "d" * 64
    observed_paths: list[str] = []
    expected = {
        "ID": exec_id,
        "ContainerID": "f" * 64,
        "Running": True,
        "ProcessConfig": {"entrypoint": "python", "arguments": ["worker.py"]},
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            observed_paths.append(self.path)
            if self.path == "/v1.41/info":
                payload = {"ID": "approved-engine"}
            elif self.path == f"/v1.41/exec/{exec_id}/json":
                payload = expected
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = recovery._LocalDockerConnection
        monkeypatch.setattr(recovery, "_LocalDockerConnection", lambda _path: connection(socket_path))
        try:
            assert recovery._docker_exec_inspection(exec_id, "approved-engine") == expected
            assert observed_paths == ["/v1.41/info", f"/v1.41/exec/{exec_id}/json"]
            with pytest.raises(recovery.RecoveryScopeUnavailable, match="Engine exec observation unavailable"):
                recovery._docker_exec_inspection(exec_id, "wrong-engine")
            assert observed_paths[-1] == "/v1.41/info"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize(
    "entrypoint,arguments",
    [
        ("python", ["-m", "auto_coder.management.bootstrap_supervisor"]),
        ("auto-coder", ["management", "serve"]),
        ("/bin/sh", ["-c", "exec auto-coder management serve"]),
    ],
)
def test_unselected_exec_management_command_refuses(scope: ScopeFixture, entrypoint: str, arguments: list[str]):
    _unrelated_worker(scope)
    scope.exec_inspections["d" * 64]["ProcessConfig"] = {"entrypoint": entrypoint, "arguments": arguments}
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unexpected unselected management exec"):
        recovery.observe_recovery_custody("fixture")


def test_unselected_exec_with_no_arguments_is_unrelated(scope: ScopeFixture):
    worker = _unrelated_worker(scope)
    scope.exec_inspections["d" * 64]["ProcessConfig"] = {"entrypoint": "true", "arguments": None}
    observed = recovery.observe_recovery_custody("fixture")
    assert observed.status == "observed-component-only"
    assert observed.unrelated_inspections[0][0] == worker["Id"]


@pytest.mark.parametrize("mutation", ["foreign-container", "wrong-id", "missing-command", "duplicate-exec"])
def test_unselected_exec_identity_and_command_must_be_independently_read(scope: ScopeFixture, mutation: str):
    worker = _unrelated_worker(scope)
    details = scope.exec_inspections["d" * 64]
    if mutation == "foreign-container":
        details["ContainerID"] = scope.raw["target"]["manager_id"]
    elif mutation == "wrong-id":
        details["ID"] = "a" * 64
    elif mutation == "missing-command":
        details["ProcessConfig"].pop("entrypoint")
    else:
        worker["ExecIDs"] *= 2
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="exec"):
        recovery.observe_recovery_custody("fixture")


def test_unselected_exec_management_change_between_reads_refuses(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch):
    _unrelated_worker(scope)
    docker_exec = recovery._docker_exec_inspection
    reads = 0

    def changing(exec_id, engine_id):
        nonlocal reads
        if exec_id == "d" * 64:
            reads += 1
            if reads == 2:
                scope.exec_inspections["d" * 64]["ProcessConfig"]["arguments"] = ["-m", "auto_coder.management.bootstrap_supervisor"]
        return docker_exec(exec_id, engine_id)

    monkeypatch.setattr(recovery, "_docker_exec_inspection", changing)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unexpected unselected management exec"):
        recovery.observe_recovery_custody("fixture")
    assert reads == 2


@pytest.mark.parametrize("pair_index", [0, 1, 2])
@pytest.mark.parametrize("mutation", ["image", "proxy-image", "stopped-image", "supervisor", "module", "serve", "manager-network", "proxy-network", "bind-parent", "bind-child"])
def test_unselected_candidates_cannot_hide_as_workers(scope: ScopeFixture, pair_index: int, mutation: str):
    worker = _unrelated_worker(scope)
    pair = scope.raw["originals"][pair_index]["pair"] if pair_index < 2 else scope.raw["target"]
    manager = scope.inspections[pair["manager_id"]]
    if mutation in ("image", "proxy-image", "stopped-image"):
        role = "proxy" if mutation == "proxy-image" else "manager"
        scope.inspections[pair[role + "_id"]]["Image"] = "sha256:" + "b" * 64
        scope.pair_digest(pair_index, role)
        worker["Image"] = "sha256:" + "b" * 64
        if mutation == "stopped-image":
            worker["State"].update(Running=False, Pid=0, Status="exited")
    elif mutation in ("supervisor", "module", "serve"):
        worker["Config"]["Cmd"] = {
            "supervisor": ["python", "-m", "auto_coder.management.bootstrap_supervisor"],
            "module": ["python", "-m", "auto_coder.management.proxy_watch_cli", "tick"],
            "serve": ["python", "-m", "auto_coder.cli", "management", "serve"],
        }[mutation]
    elif mutation.endswith("network"):
        worker["HostConfig"]["NetworkMode"] = "container:" + pair["manager_id" if mutation == "manager-network" else "proxy_id"]
    else:
        root = Path(manager["Mounts"][0]["Source"])
        if mutation == "bind-child":
            root = root / "nested-worker"
            root.mkdir()
        else:
            root = root.parent
        worker["Mounts"][0]["Source"] = str(root)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unexpected unselected management/proxy candidate"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize(
    "command",
    [
        ["/bin/sh", "-c", "exec python -m auto_coder.management.bootstrap_supervisor"],
        ["/bin/bash", "-ec", "python3 -m auto_coder.cli management serve"],
        ["python", "-c", "import auto_coder.management.proxy_watch_cli"],
    ],
)
def test_shell_embedded_management_command_is_not_an_unrelated_worker(scope: ScopeFixture, command: list[str]):
    worker = _unrelated_worker(scope)
    worker["Config"]["Cmd"] = command
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unexpected unselected management/proxy candidate"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize(
    "command",
    [
        ["auto-coder", "management", "serve", "--config", "/etc/auto-coder/management.yml"],
        ["/opt/auto-coder/.venv/bin/auto-coder", "management", "serve"],
        ["/bin/sh", "-c", "exec auto-coder management serve --config /bootstrap/config.json"],
    ],
)
def test_installed_management_cli_cannot_hide_as_unrelated_worker(scope: ScopeFixture, command: list[str]):
    # The supported console-script entrypoint is equivalent to python -m auto_coder.cli.
    worker = _unrelated_worker(scope)
    worker["Config"]["Cmd"] = command
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unexpected unselected management/proxy candidate"):
        recovery.observe_recovery_custody("fixture")


def test_unrelated_command_with_management_words_is_not_cli(scope: ScopeFixture):
    worker = _unrelated_worker(scope)
    worker["Config"]["Cmd"] = ["auto-coder-metrics", "management", "serve"]
    observed = recovery.observe_recovery_custody("fixture")
    assert observed.status == "observed-component-only"


@pytest.mark.parametrize("mutation", ["disappear", "swap", "state", "command", "mount"])
def test_worker_changes_between_producer_and_consumer_refuse(scope: ScopeFixture, mutation: str):
    worker = _unrelated_worker(scope)
    previous = _produce_from_separate_process(scope)
    if mutation in ("disappear", "swap"):
        scope.inspections.pop(worker["Id"])
        if mutation == "swap":
            worker["Id"] = "e" * 64
            scope.exec_inspections["d" * 64]["ContainerID"] = worker["Id"]
            scope.inspections[worker["Id"]] = worker
    elif mutation == "state":
        worker["RestartCount"] += 1
    elif mutation == "command":
        worker["Config"]["Cmd"].append("--changed")
    else:
        worker["Mounts"][0]["RW"] = False
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="independently repeated recovery component differs"):
        recovery.verify_recovery_custody("fixture", previous)


@pytest.mark.parametrize("mutation", ["disappear", "swap", "state"])
def test_worker_changes_during_sampling_refuse(scope: ScopeFixture, monkeypatch: pytest.MonkeyPatch, mutation: str):
    worker = _unrelated_worker(scope)
    snapshot = recovery._snapshot
    calls = 0

    def changing(enrollment):
        nonlocal calls
        calls += 1
        if calls == 2:
            if mutation in ("disappear", "swap"):
                scope.inspections.pop(worker["Id"])
                if mutation == "swap":
                    worker["Id"] = "e" * 64
                    scope.exec_inspections["d" * 64]["ContainerID"] = worker["Id"]
                    scope.inspections[worker["Id"]] = worker
            else:
                worker["RestartCount"] += 1
        return snapshot(enrollment)

    monkeypatch.setattr(recovery, "_snapshot", changing)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="scope or bindings changed"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize(
    "status,running,pid",
    [
        ("running", False, 0),
        ("paused", False, 0),
        ("created", True, 12345),
        ("exited", True, 12345),
        ("dead", True, 12345),
    ],
)
def test_unrelated_worker_refuses_contradictory_docker_lifetime(scope: ScopeFixture, status: str, running: bool, pid: int):
    worker = _unrelated_worker(scope)
    worker["State"].update(Status=status, Running=running, Pid=pid)
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="contradictory"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("status,running,pid", [("paused", True, 12345), ("exited", False, 0)])
def test_unrelated_worker_accepts_consistent_docker_lifetime(scope: ScopeFixture, status: str, running: bool, pid: int):
    worker = _unrelated_worker(scope)
    worker["State"].update(Status=status, Running=running, Pid=pid)
    observed = recovery.observe_recovery_custody("fixture")
    assert observed.status == "observed-component-only"
    assert observed.unrelated_inspections[0][0] == worker["Id"]


@pytest.mark.parametrize(
    "status,field,value",
    [
        ("running", "Paused", True),
        ("paused", "Paused", False),
        ("restarting", "Restarting", False),
        ("running", "Restarting", True),
        ("dead", "Dead", False),
        ("exited", "Dead", True),
        ("running", "Paused", 1),
        ("running", "Restarting", "false"),
        ("running", "Dead", None),
    ],
)
def test_unrelated_worker_refuses_inconsistent_optional_docker_state_flags(scope: ScopeFixture, status: str, field: str, value: object):
    worker = _unrelated_worker(scope)
    worker["State"]["Status"] = status
    worker["State"][field] = value
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize(
    "status,running,pid,paused,dead",
    [
        ("running", True, 12345, False, False),
        ("paused", True, 12345, True, False),
        ("exited", False, 0, False, False),
        ("dead", False, 0, False, True),
    ],
)
def test_unrelated_worker_preserves_consistent_optional_docker_state_flags(scope: ScopeFixture, status: str, running: bool, pid: int, paused: bool, dead: bool):
    worker = _unrelated_worker(scope)
    worker["State"].update(Status=status, Running=running, Pid=pid, Paused=paused, Restarting=False, Dead=dead)
    observed = recovery.observe_recovery_custody("fixture")
    assert observed.status == "observed-component-only"
    assert observed.unrelated_inspections[0][0] == worker["Id"]


@pytest.mark.parametrize("mutation", ["image", "state", "policy", "config", "missing-command", "command", "entrypoint", "network", "namespace-alias", "mounts", "bind", "volume", "symlink", "exec"])
def test_malformed_unselected_facts_refuse(scope: ScopeFixture, mutation: str):
    worker = _unrelated_worker(scope)
    if mutation == "image":
        worker["Image"] = "worker:latest"
    elif mutation == "state":
        worker["State"]["Pid"] = False
    elif mutation == "policy":
        worker["HostConfig"]["RestartPolicy"] = None
    elif mutation == "config":
        worker.pop("Config")
    elif mutation == "missing-command":
        worker["Config"].pop("Cmd")
    elif mutation == "command":
        worker["Config"]["Cmd"] = "worker"
    elif mutation == "entrypoint":
        worker["Config"]["Entrypoint"] = [False]
    elif mutation == "network":
        worker["HostConfig"].pop("NetworkMode")
    elif mutation == "namespace-alias":
        worker["HostConfig"]["NetworkMode"] = "container:ambiguous-name"
    elif mutation == "mounts":
        worker["Mounts"] = [None]
    elif mutation == "bind":
        worker["Mounts"][0]["Source"] = "relative"
    elif mutation == "volume":
        worker["Mounts"][0]["Type"] = "volume"
    elif mutation == "symlink":
        alias = scope.root / "worker-alias"
        alias.symlink_to(scope.root / "worker", target_is_directory=True)
        worker["Mounts"][0]["Source"] = str(alias)
    else:
        worker["ExecIDs"] = ["short-id"]
    with pytest.raises(recovery.RecoveryScopeUnavailable):
        recovery.observe_recovery_custody("fixture")


def test_selected_exec_ids_remain_full_inspection_evidence(scope: ScopeFixture):
    selected = scope.raw["target"]["manager_id"]
    scope.inspections[selected]["ExecIDs"] = ["d" * 64]
    scope.pair_digest(2, "manager")
    _unrelated_worker(scope)
    recovery.observe_recovery_custody("fixture")
    scope.inspections[selected]["ExecIDs"] = ["e" * 64]
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="^enrolled full Docker inspection differs$"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize(
    ("path", "arguments"),
    [
        ("python", ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json"]),
        ("/bin/sh", ["-c", "exec python3 -m auto_coder.management.proxy_watch_cli --config /watcher/config.json"]),
        ("/opt/auto-coder/.venv/bin/auto-coder", ["management", "serve"]),
    ],
)
def test_actual_docker_launch_cannot_hide_management_behind_benign_config(scope: ScopeFixture, path: str, arguments: list[str]):
    # Docker's actual Path/Args are independent candidate evidence from Config.Cmd.
    worker = _unrelated_worker(scope)
    worker["Path"] = path
    worker["Args"] = arguments
    assert worker["Config"]["Cmd"] == ["python", "worker.py"]
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="unexpected unselected management/proxy candidate"):
        recovery.observe_recovery_custody("fixture")


@pytest.mark.parametrize("mutation", ["path-missing", "args-missing", "path-type", "args-type", "args-too-many"])
def test_actual_docker_launch_requires_complete_facts_if_present(scope: ScopeFixture, mutation: str):
    worker = _unrelated_worker(scope)
    worker["Path"] = "python"
    worker["Args"] = ["worker.py"]
    if mutation == "path-missing":
        del worker["Path"]
    elif mutation == "args-missing":
        del worker["Args"]
    elif mutation == "path-type":
        worker["Path"] = 123
    elif mutation == "args-type":
        worker["Args"] = "worker.py"
    else:
        worker["Args"] = ["worker.py"] * 65
    with pytest.raises(recovery.RecoveryScopeUnavailable, match="actual launch"):
        recovery.observe_recovery_custody("fixture")
