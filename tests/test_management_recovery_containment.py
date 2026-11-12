"""Bounded component tests; native closed-host acceptance is a separate harness."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pwd
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from auto_coder.management import original_attempts
from auto_coder.management import recovery_containment as observer
from auto_coder.management import recovery_source_profile as source
from auto_coder.management.containment_enrollment import OriginalNetworkPin
from auto_coder.management.containment_route_manifest import DeploymentRootBinding, RecoveryRouteScope, RecoveryRouteSelection, _route_container_ids
from auto_coder.management.recovery_containment_model import RECOVERY_PROFILE, NamedDescriptor, RecoveryContainmentUnavailable, RecoveryEnrollment, RecoveryFile, RecoveryLock, RecoveryOriginal, RecoveryResources, RecoveryTarget, RecoveryWatcher
from auto_coder.management.recovery_containment_profile import RecoveryManagerFact, _ordinary_mount_effects, assert_disjoint_target

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
_BOOT = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def _use_real_commands():
    yield


@pytest.fixture
def _use_real_sleep():
    yield


def _file(path: str, inode: int) -> RecoveryFile:
    return RecoveryFile(path, "d" * 64, 1, inode)


def _original(index: int = 0, *, started: bool = True) -> RecoveryOriginal:
    manager, proxy = format(2 * index + 1, "064x"), format(2 * index + 2, "064x")
    base = "/private/generation-" + str(index)
    mounts = (DeploymentRootBinding(manager, "/management-child-state", base + "/state", True, 1, 100 + index, stat.S_IFDIR | 0o700), DeploymentRootBinding(proxy, "/etc/caddy/Caddyfile", base + "/Caddyfile", False, 1, 200 + index, stat.S_IFREG | 0o600))
    owner = RecoveryLock(base + "/state/.supervisor-owner.lock", 1, 1000 + index) if started else None
    prior = _file(base + "/prior-evidence.json", 2000 + index) if started else None
    pin = OriginalNetworkPin(42, "12345", _BOOT, 10 + index, "net:[" + str(3000 + index) + "]", 4, 3000 + index) if started else None
    return RecoveryOriginal(
        f"generation-{index:02d}",
        f"instance-{index:02d}",
        manager,
        "sha256:" + "a" * 64,
        proxy,
        "sha256:" + "b" * 64,
        "started" if started else "never-started",
        _BOOT if started else "",
        _file(base + "/bootstrap/config.json", 4000 + index),
        _file(base + "/bootstrap/management.yml", 5000 + index),
        _file(base + "/Caddyfile", 200 + index),
        mounts,
        owner,
        prior,
        pin,
    )


def _enrollment(*, generations: int = 1, watchers: int = 1, started: bool = True) -> RecoveryEnrollment:
    target_id = "f" * 64
    target = RecoveryTarget(
        "target-instance",
        target_id,
        "sha256:" + "a" * 64,
        _file("/private/target/bootstrap/config.json", 9000),
        _file("/private/target/bootstrap/management.yml", 9001),
        (DeploymentRootBinding(target_id, "/management-child-state", "/private/target/state", True, 1, 9002, stat.S_IFDIR | 0o700),),
        RecoveryLock("/private/target/state/.supervisor-owner.lock", 1, 9003),
        18081,
    )
    controls = tuple(
        RecoveryWatcher(f"auto-coder-management-proxy-watch-{index:02d}.service", _file(f"/private/watcher-{index}/config.json", 10000 + index), _file("/private/package/management/proxy_watch_cli.py", 20000), RecoveryLock(f"/private/watcher-{index}/state/proxy-watch.lock", 1, 30000 + index))
        for index in range(watchers)
    )
    return RecoveryEnrollment("approved-management-recovery-containment-v1", "deployment", "enrollment", "authorization", RECOVERY_PROFILE, "local-engine", "sha256:" + "c" * 64, tuple(_original(index, started=started) for index in range(generations)), target, controls, True)


def _json(value: object) -> object:
    return json.loads(json.dumps(asdict(value)))


def test_scope_round_trip_preserves_all_members_and_fixed_profile():
    expected = _enrollment(generations=3, watchers=3)
    actual = RecoveryEnrollment.parse(_json(expected))
    assert actual == expected
    assert actual.container_ids == tuple(sorted([expected.target.management_id, *(identifier for item in expected.originals for identifier in (item.management_id, item.proxy_id))]))
    assert len(actual.container_ids) == 7
    assert actual.approved_observation_only is True


@pytest.mark.parametrize("field,value", [("schema", "approved-current-containment-enrollment-v3"), ("profile", "ordinary-v1"), ("approved_observation_only", False), ("source_identity", "same-as-before"), ("deployment_id", "../other"), ("engine_id", "")])
def test_scope_refuses_foreign_legacy_or_authorizing_schema(field, value):
    raw = _json(_enrollment())
    raw[field] = value
    with pytest.raises(RecoveryContainmentUnavailable):
        RecoveryEnrollment.parse(raw)


def test_scope_refuses_unknown_fields_and_container_aliases():
    raw = _json(_enrollment())
    raw["trusted_root"] = "/request/override"
    with pytest.raises(RecoveryContainmentUnavailable, match="schema"):
        RecoveryEnrollment.parse(raw)
    raw.pop("trusted_root")
    raw["target"]["management_id"] = raw["originals"][0]["management_id"]
    raw["target"]["mounts"][0]["container_id"] = raw["target"]["management_id"]
    with pytest.raises(RecoveryContainmentUnavailable, match="alias"):
        RecoveryEnrollment.parse(raw)


def test_scope_refuses_shared_proxy_identity_between_generations():
    raw = _json(_enrollment(generations=2))
    old_proxy = raw["originals"][0]["proxy_id"]
    raw["originals"][1]["proxy_id"] = old_proxy
    raw["originals"][1]["mounts"][1]["container_id"] = old_proxy
    raw["originals"][1]["mounts"].sort(key=lambda item: (item["container_id"], item["destination"]))
    with pytest.raises(RecoveryContainmentUnavailable, match="alias"):
        RecoveryEnrollment.parse(raw)


def test_never_started_scope_cannot_invent_owner_or_historical_pin():
    selected = _enrollment(started=False)
    actual = RecoveryEnrollment.parse(_json(selected))
    assert actual.originals[0].owner is None
    assert actual.originals[0].prior_evidence is None
    assert actual.originals[0].namespace_pin is None
    raw = _json(selected)
    raw["originals"][0]["owner"] = asdict(RecoveryLock("/private/fabricated.lock", 1, 2))
    with pytest.raises(RecoveryContainmentUnavailable, match="invent"):
        RecoveryEnrollment.parse(raw)


def test_scope_requires_original_boot_and_real_namespace_capture_fields():
    raw = _json(_enrollment())
    raw["originals"][0]["boot_id"] = "00000000-0000-4000-8000-000000000002"
    with pytest.raises(RecoveryContainmentUnavailable, match="boot"):
        RecoveryEnrollment.parse(raw)
    raw = _json(_enrollment())
    raw["originals"][0]["namespace_pin"] = None
    with pytest.raises(ValueError, match="namespace pin"):
        RecoveryEnrollment.parse(raw)


@pytest.mark.parametrize("kind", ["owner-path", "owner-inode", "controller-owner", "unsorted"])
def test_scope_rejects_ambiguous_resource_authority(kind):
    raw = _json(_enrollment(generations=2, watchers=2))
    if kind == "owner-path":
        raw["originals"][1]["owner"]["path"] = raw["originals"][0]["owner"]["path"]
    elif kind == "owner-inode":
        raw["originals"][1]["owner"]["inode"] = raw["originals"][0]["owner"]["inode"]
    elif kind == "controller-owner":
        raw["watchers"][0]["lock"]["inode"] = raw["target"]["owner"]["inode"]
    else:
        raw["originals"].reverse()
    with pytest.raises(RecoveryContainmentUnavailable):
        RecoveryEnrollment.parse(raw)


def test_complete_peer_resource_bound_is_checked_before_observation():
    boundary = _enrollment(generations=16, watchers=29)
    assert RecoveryEnrollment.parse(_json(boundary)) == boundary
    with pytest.raises(RecoveryContainmentUnavailable, match="complete peer resource bound"):
        RecoveryEnrollment.parse(_json(_enrollment(generations=16, watchers=30)))


def test_named_descriptors_are_exact_sorted_and_have_no_aliases():
    expected = RecoveryResources(42, "12345", _BOOT, (NamedDescriptor("generation-00", 10),), (NamedDescriptor("watcher.service", 11),), (NamedDescriptor("generation-00", 12),))
    assert RecoveryResources.parse(_json(expected)) == expected
    raw = _json(expected)
    raw["namespace_fds"][0]["descriptor"] = 10
    with pytest.raises(RecoveryContainmentUnavailable, match="alias"):
        RecoveryResources.parse(raw)
    raw = _json(expected)
    raw["controller_fds"][0]["descriptor"] = True
    with pytest.raises(RecoveryContainmentUnavailable, match="descriptor"):
        RecoveryResources.parse(raw)


def test_protected_fixture_load_rejects_unknown_fields_duplicates_and_permissions():
    with tempfile.TemporaryDirectory(prefix="recovery-enrollment-component-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        root = Path(directory)
        path = root / "enrollment.json"
        selected = _enrollment()
        path.write_text(json.dumps(asdict(selected)))
        path.chmod(0o600)
        actual = observer._load_enrollment("enrollment", root=root, expected_uid=os.geteuid())
        assert actual.enrollment == selected
        assert actual.device == path.stat().st_dev and actual.inode == path.stat().st_ino
        assert actual.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        path.chmod(0o644)
        with pytest.raises(ValueError, match="protected"):
            observer._load_enrollment("enrollment", root=root, expected_uid=os.geteuid())
        path.chmod(0o600)
        content = json.dumps(asdict(selected))
        path.write_text(content[:-1] + ',"profile":"ordinary-recovery-v1"}')
        with pytest.raises(ValueError, match="duplicate"):
            observer._load_enrollment("enrollment", root=root, expected_uid=os.geteuid())


def test_recovery_route_selection_uses_every_member_root_and_configuration():
    selected = _enrollment(generations=3, watchers=3)
    approved = observer.RecoveryEnrollmentObservation(selected, "e" * 64, 1, 2, 3)
    scope = observer.route_selection(approved)
    assert _route_container_ids(scope) == selected.container_ids
    assert {item.path for item in scope.enrollment.original_files} >= {item.bootstrap_config.path for item in selected.originals} | {selected.target.bootstrap_config.path} | {item.configuration.path for item in selected.watchers}
    forged = RecoveryRouteSelection(RecoveryRouteScope("d", "e", "a", "ordinary-v1", selected.container_ids, ()), "e" * 64)
    with pytest.raises(ValueError, match="selection"):
        _route_container_ids(forged)


def _manager_fact(selected: RecoveryOriginal | RecoveryTarget, *, state: str, journal: str, oauth: str, port: int) -> RecoveryManagerFact:
    return RecoveryManagerFact(
        selected.instance_id, selected.instance_id, selected.management_id, selected.management_image_id, "component-only", "sha256:" + "c" * 64, "d" * 64, selected.bootstrap_config.sha256, selected.management_config.sha256, state, journal, oauth, selected.owner, port, selected.mounts
    )


def test_target_retains_original_oauth_and_separates_state_journal_and_listener():
    selected = _enrollment()
    old = _manager_fact(selected.originals[0], state="/private/old/state", journal="/private/old/journal", oauth="/private/oauth", port=18080)
    target = _manager_fact(selected.target, state="/private/target/state", journal="/private/target/journal", oauth="/private/oauth", port=18081)
    assert_disjoint_target(selected, (old, target))
    with pytest.raises(RecoveryContainmentUnavailable, match="OAuth"):
        assert_disjoint_target(selected, (old, replace(target, oauth_root="/private/copied-oauth")))
    with pytest.raises(RecoveryContainmentUnavailable, match="overlaps"):
        assert_disjoint_target(selected, (old, replace(target, state_root="/private/old/state/child")))
    with pytest.raises(RecoveryContainmentUnavailable, match="listener"):
        assert_disjoint_target(selected, (old, replace(target, published_port=18080)))


def test_mount_effects_reject_undeclared_writable_or_interpreter_inputs():
    config = {"state_root": "/management-child-state", "journal_root": "/management-journal", "oauth_root": "/management-oauth", "runtimes": []}
    selected = _enrollment().target.mounts[0]
    _ordinary_mount_effects(config, (selected,), target=True)
    for destination, writable in (("/unselected-data", True), ("/etc/ld.so.preload", False), ("/management-recovery", True)):
        with pytest.raises(RecoveryContainmentUnavailable, match="undeclared"):
            _ordinary_mount_effects(config, (replace(selected, destination=destination, writable=writable),), target=True)


def _archive(entries: list[tuple[str, bytes, bytes | None]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            if kind is not None:
                info.type = kind
                info.linkname = "auto_coder/__init__.py"
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _sources() -> list[tuple[str, bytes, bytes | None]]:
    return [
        ("auto_coder/__init__.py", b"", None),
        ("auto_coder/management/bootstrap_supervisor.py", b"# supervisor\n", None),
        ("auto_coder/management/original_attempts.py", b"# original\n", None),
        ("auto_coder/management.py", b"# flat name\n", None),
        ("auto_coder/management/a.py", b"# nested name\n", None),
    ]


def test_complete_source_archive_matches_existing_source_identity_framing(tmp_path, monkeypatch):
    package = tmp_path / "auto_coder"
    for name, content, _ in _sources():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(original_attempts, "__file__", str(package / "management" / "original_attempts.py"))
    assert source._source_archive_identity(_archive(list(reversed(_sources())))) == original_attempts.source_identity()


@pytest.mark.parametrize(
    "name,kind",
    [
        ("auto_coder/__pycache__/__init__.cpython-312.pyc", None),
        ("auto_coder/original.pyo", None),
        ("auto_coder/native.cpython-312-x86_64-linux-gnu.so", None),
        ("auto_coder/native.pyd", None),
        ("auto_coder/link.py", tarfile.SYMTYPE),
        ("auto_coder/hard.py", tarfile.LNKTYPE),
        ("auto_coder/../outside.py", None),
        ("/auto_coder/absolute.py", None),
        ("auto_coder//ambiguous.py", None),
        ("auto_coder/__init__.py", None),
    ],
)
def test_source_archive_rejects_alternate_executable_or_ambiguous_members(name, kind):
    with pytest.raises(ValueError):
        source._source_archive_identity(_archive(_sources() + [(name, b"unsafe", kind)]))


def test_source_archive_rejects_missing_history_and_bounded_content(monkeypatch):
    with pytest.raises(ValueError, match="incomplete"):
        source._source_archive_identity(_archive([("auto_coder/__init__.py", b"", None)]))
    monkeypatch.setattr(source, "_MAX_SOURCE", 4)
    with pytest.raises(ValueError, match="bound"):
        source._source_archive_identity(_archive(_sources()))


def test_source_reader_uses_exact_local_object_without_execution(monkeypatch):
    calls = []
    archive = _archive(_sources())

    def command(arguments, **options):
        calls.append((arguments, options))
        if arguments[1] == "context":
            return subprocess.CompletedProcess(arguments, 0, '"unix:///var/run/docker.sock"', "")
        return subprocess.CompletedProcess(arguments, 0, archive, b"")

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(source.subprocess, "run", command)
    assert source.read_image_source_identity("a" * 64) == source._source_archive_identity(archive)
    assert calls[1][0] == ("docker", "cp", "a" * 64 + ":/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder", "-")
    assert not any("exec" in arguments for arguments, _ in calls)
    monkeypatch.setenv("DOCKER_HOST", "unix:///unselected.sock")
    with pytest.raises(ValueError, match="override"):
        source.read_image_source_identity("a" * 64)
    assert len(calls) == 2


@pytest.mark.parametrize("mutation", ["grant", "partial", "hash", "unknown"])
def test_saved_report_cannot_supply_missing_or_authorizing_evidence(monkeypatch, mutation):
    raw = {
        "schema": "current-recovery-containment-v1",
        "status": "observed-current-recovery-containment",
        "observation_id": _BOOT,
        "observer_pid": 42,
        "observer_start_token": "12345",
        "boot_id": _BOOT,
        "started_monotonic_ns": 1,
        "finished_monotonic_ns": 2,
        "facts": {},
        "evidence_sha256": observer.digest({}),
        "grant_issued": False,
        "history_asserted": False,
        "admission_created": False,
    }
    if mutation == "grant":
        raw["grant_issued"] = True
    elif mutation == "partial":
        raw.pop("facts")
    elif mutation == "hash":
        raw["facts"] = {"forged": True}
    else:
        raw["repair_history"] = True
    monkeypatch.setattr(observer, "observe", lambda *_: pytest.fail("malformed prior report reached current observation"))
    with pytest.raises(RecoveryContainmentUnavailable):
        observer.verify("enrollment", raw, RecoveryResources(42, "12345", _BOOT, (), (), ()))


def _consumer_scope() -> tuple[RecoveryEnrollment, bytes]:
    selected = _enrollment()
    original, target = selected.originals[0], selected.target
    assert original.owner is not None and original.namespace_pin is not None
    mounts = (
        *target.mounts,
        DeploymentRootBinding(target.management_id, "/management-recovery", "/var/lib/auto-coder/recovery-epoch-peer", False, 1, 50000, stat.S_IFDIR | 0o700),
        DeploymentRootBinding(target.management_id, "/management-recovery-history/history.lock", "/private/history/history.lock", False, 1, 50001, stat.S_IFREG | 0o600),
        DeploymentRootBinding(target.management_id, "/management-recovery-originals/0/owner.lock", original.owner.path, False, original.owner.device, original.owner.inode, stat.S_IFREG | 0o600),
    )
    selected = replace(selected, target=replace(target, mounts=tuple(sorted(mounts, key=lambda item: item.destination))))
    payload = {
        "schema_version": 1,
        "request_id": "request",
        "authorization_id": selected.authorization_id,
        "target_id": "target",
        "issuer_uid": 0,
        "socket_directory_device": 1,
        "socket_directory_inode": 50000,
        "original_fences": [{"name": "history", "path": "/management-recovery-history/history.lock", "device": 1, "inode": 50001}, {"name": "owner-0", "path": "/management-recovery-originals/0/owner.lock", "device": original.owner.device, "inode": original.owner.inode}],
        "target_owner": {"name": "target-owner", "device": target.owner.device, "inode": target.owner.inode},
        "transferred_resources": [
            {"name": "epoch", "kind": "transferred-ex", "device": 1, "inode": 50002},
            {"name": "controller-0", "kind": "transferred-ex", "device": selected.watchers[0].lock.device, "inode": selected.watchers[0].lock.inode},
            {"name": "namespace-0", "kind": "network-namespace", "device": original.namespace_pin.device, "inode": original.namespace_pin.inode},
        ],
    }
    return selected, json.dumps(payload).encode()


def test_target_consumer_inputs_bind_the_exact_socket_owner_history_and_resources():
    selected, encoded = _consumer_scope()
    observer._consumer_custody_profile(selected, encoded)


@pytest.mark.parametrize("mutation", ["socket-source", "socket-writable", "socket-inode", "owner-source", "owner-inode", "extra-owner", "history-writable", "controller-inode", "namespace-inode", "omitted-epoch"])
def test_target_consumer_custody_inputs_refuse_substitution_or_omission(mutation):
    selected, encoded = _consumer_scope()
    payload = json.loads(encoded)
    mounts = list(selected.target.mounts)
    positions = {item.destination: index for index, item in enumerate(mounts)}
    if mutation == "socket-source":
        index = positions["/management-recovery"]
        mounts[index] = replace(mounts[index], source="/private/other-issuer")
    elif mutation == "socket-writable":
        index = positions["/management-recovery"]
        mounts[index] = replace(mounts[index], writable=True)
    elif mutation == "socket-inode":
        payload["socket_directory_inode"] += 1
    elif mutation == "owner-source":
        index = positions["/management-recovery-originals/0/owner.lock"]
        mounts[index] = replace(mounts[index], source="/private/unrelated.lock")
    elif mutation == "owner-inode":
        payload["original_fences"][1]["inode"] += 1
    elif mutation == "extra-owner":
        mounts.append(replace(mounts[positions["/management-recovery-originals/0/owner.lock"]], destination="/management-recovery-originals/1/owner.lock"))
    elif mutation == "history-writable":
        index = positions["/management-recovery-history/history.lock"]
        mounts[index] = replace(mounts[index], writable=True)
    elif mutation == "controller-inode":
        payload["transferred_resources"][1]["inode"] += 1
    elif mutation == "namespace-inode":
        payload["transferred_resources"][2]["inode"] += 1
    else:
        payload["transferred_resources"].pop(0)
    with pytest.raises(ValueError):
        observer._consumer_custody_profile(replace(selected, target=replace(selected.target, mounts=tuple(mounts))), json.dumps(payload).encode())
