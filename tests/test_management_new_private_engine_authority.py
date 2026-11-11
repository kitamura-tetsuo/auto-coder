"""Trusted host private Engine provenance and source-ownership regressions.

The unit oracle uses real protected files, Unix sockets and kernel locks. Docker
container behavior itself is covered by the separate Docker integration suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management import fresh_private_engine as authority
from auto_coder.management.fresh_begin import _canonical, begin_new_instance
from auto_coder.management.fresh_host_observation import HostObservationUnavailable
from auto_coder.management.fresh_new_docker import CreatedNewDockerSnapshot
from auto_coder.management.fresh_new_first_launch import HOST_FIRST_START, load_new_first_start
from auto_coder.management.fresh_new_host_start import _create_host_start_claim, start_new_docker_once
from tests.test_management_new_instance_preflight import request_file
from tests.test_management_new_instance_profile import configured_request

PARENT_ID = "1" * 64
PARENT_IMAGE = "sha256:" + "2" * 64


@pytest.fixture
def source(configured_request: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, dict[str, object], Path]:
    begin_new_instance(configured_request)
    request = json.loads(configured_request.read_bytes())
    root = configured_request.parent / "private-engine"
    root.mkdir(mode=0o700)
    sock_dir = root / "socket"
    data_dir = root / "data"
    sock_dir.mkdir(mode=0o700)
    data_dir.mkdir(mode=0o700)
    # A socket filesystem inode is sufficient for a unit-level stat oracle;
    # no listener, mock Docker process or network call is started here.
    connection = sock_dir / "docker.sock"
    os.mknod(connection, stat.S_IFSOCK | 0o600)
    parent_mounts = [(str(sock_dir), str(sock_dir), True), (str(data_dir), "/var/lib/docker", True)]
    for key in ("state_root", "operation_root", "guard_root", "oauth_root", "bootstrap_config", "management_config", "executable", "application_root"):
        path = str(request[key])
        parent_mounts.append((path, path, key in {"state_root", "operation_root", "guard_root", "oauth_root"}))
    parent_mounts.append((str(configured_request), str(configured_request), False))
    parent: dict[str, object] = {
        "Id": PARENT_ID,
        "Name": "/ac-new-private-engine-" + "0" * 16,
        "Image": PARENT_IMAGE,
        "Created": "2026-11-11T12:00:00Z",
        "RestartCount": 0,
        "State": {"Status": "running", "Running": True, "Pid": os.getpid(), "StartedAt": "2026-11-11T12:00:01Z"},
        "HostConfig": {
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "NetworkMode": "none",
            "PidMode": "host",
            "Privileged": True,
            "Init": True,
            "AutoRemove": False,
            "PortBindings": {},
        },
        "Config": {
            "Entrypoint": ["dockerd"],
            "Cmd": [
                f"--host=unix://{sock_dir / 'docker.sock'}",
                "--data-root=/var/lib/docker",
                "--storage-driver=vfs",
                "--iptables=false",
                "--bridge=none",
                "--ip-forward=false",
                "--ip-masq=false",
            ],
        },
        "NetworkSettings": {"Networks": {"none": {"NetworkID": "private-none"}}},
        "Mounts": [{"Type": "bind", "Source": a, "Destination": b, "RW": c} for a, b, c in parent_mounts],
    }
    monkeypatch.setattr(authority, "_parent", lambda *_args: parent)
    monkeypatch.setattr(authority, "_engine_id", lambda path: "outer-engine" if str(path) == "/var/run/docker.sock" else "inner-engine")
    monkeypatch.setattr(authority, "_docker", lambda _host, *arguments, **_kwargs: "" if arguments[0] == "ps" else "unreachable")
    yield configured_request, root, Path("/var/run/docker.sock"), parent, connection


def _observation(source: tuple[Path, Path, Path, dict[str, object], Path]) -> authority.PrivateEngineEvidence:
    request, root, outer, _, _ = source
    return authority._observe_source(request, root, outer, PARENT_ID, PARENT_IMAGE, require_empty_private_engine=True)


def _seal(source: tuple[Path, Path, Path, dict[str, object], Path]) -> authority.PrivateEngineEvidence:
    request, root, _, _, _ = source
    origin = authority.inspect_new_instance_origin(request)
    root_info = root.stat()
    socket_info = (root / "socket").stat()
    data_info = (root / "data").stat()
    intent = {
        "schema_version": 1,
        "origin_sha256": origin.record_sha256,
        "request_id": origin.request_id,
        "parent_image_id": PARENT_IMAGE,
        "outer_engine_id": "outer-engine",
        "nonce": "0" * 32,
        "private_root_dev": root_info.st_dev,
        "private_root_ino": root_info.st_ino,
        "socket_dir_dev": socket_info.st_dev,
        "socket_dir_ino": socket_info.st_ino,
        "data_dir_dev": data_info.st_dev,
        "data_dir_ino": data_info.st_ino,
    }
    created = {"schema_version": 1, "intent_sha256": hashlib.sha256(_canonical(intent)).hexdigest(), "parent_container_id": PARENT_ID}
    evidence = _observation(source)
    ready = {"schema_version": 1, "created_sha256": hashlib.sha256(_canonical(created)).hexdigest(), "evidence": asdict(evidence)}
    for name, record in ((authority.SOURCE_INTENT, intent), (authority.SOURCE_CREATED, created), (authority.SOURCE_READY, ready)):
        authority._record(root / name, record)
    return evidence


def test_private_engine_source_binds_separate_live_parent_kernel_lifetime_and_original_files(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    evidence = _seal(source)
    assert evidence.private_engine_id == "inner-engine"
    assert evidence.outer_engine_id == "outer-engine"
    assert evidence.parent_pid == os.getpid()
    assert evidence.parent_start_token.isdecimal()
    with authority.hold_private_engine_source(*source[:3]) as current:
        assert current == evidence
    assert {path.name for path in source[1].iterdir()} == {"socket", "data", authority.SOURCE_INTENT, authority.SOURCE_CREATED, authority.SOURCE_READY}


def test_daemon_data_root_does_not_need_host_operator_traversal(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    """The daemon can take ownership of data without expanding the host ACL."""
    evidence = _seal(source)
    data_root = source[1] / "data"
    data_root.chmod(0o000)
    try:
        with authority.hold_private_engine_source(*source[:3]) as observed:
            assert observed == evidence
            assert authority._data_stat(source[1]).st_ino == evidence.data_dir_ino
    finally:
        data_root.chmod(0o700)


def test_original_data_root_inode_cannot_be_substituted(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    _seal(source)
    data_root = source[1] / "data"
    data_root.rename(source[1].parent / "retired-data")
    data_root.mkdir(mode=0o700)
    with pytest.raises(HostObservationUnavailable, match="directories differ from pre-effect intent"):
        with authority.hold_private_engine_source(*source[:3]):
            pytest.fail("replaced original Docker data root unexpectedly admitted")


@pytest.mark.parametrize("isolated_net", [True, False])
def test_private_engine_restricted_proc_namespace_requires_verified_parent_probe(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch, isolated_net: bool) -> None:
    """A ptrace denial cannot become an unverified namespace assumption."""
    original_readlink = os.readlink
    host_pid_namespace = original_readlink("/proc/self/ns/pid")
    host_net_namespace = original_readlink("/proc/self/ns/net")
    forbidden = {f"/proc/{os.getpid()}/ns/pid", f"/proc/{os.getpid()}/ns/net"}

    def restricted_readlink(path: str | os.PathLike[str]) -> str:
        if os.fspath(path) in forbidden:
            raise PermissionError("ptrace-gated parent namespace")
        return original_readlink(path)

    probes: list[tuple[str, ...]] = []

    def private_probe(_socket: Path, *arguments: str, **_kwargs: object) -> str:
        if arguments[0] == "ps":
            return ""
        probes.append(arguments)
        if arguments[-1] == "/proc/self/ns/pid":
            return host_pid_namespace
        return "net:[999999999]" if isolated_net else host_net_namespace

    monkeypatch.setattr(authority.os, "readlink", restricted_readlink)
    monkeypatch.setattr(authority, "_docker", private_probe)
    if isolated_net:
        assert _observation(source).parent_pid_namespace == host_pid_namespace
        assert len(probes) == 4, "both before and after observations must recheck each namespace"
    else:
        with pytest.raises(HostObservationUnavailable, match="kernel lifetime unavailable"):
            _observation(source)
        assert len(probes) == 2


def test_transient_docker_exec_list_cannot_spoil_parent_provenance(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    reads = 0

    def live_parent(*_args: object) -> dict[str, object]:
        nonlocal reads
        reads += 1
        snapshot = deepcopy(source[3])
        snapshot["ExecIDs"] = ["temporary-namespace-observation"] if reads == 1 else None
        snapshot["State"]["Health"] = {"Status": "starting" if reads == 1 else "healthy"}
        return snapshot

    monkeypatch.setattr(authority, "_parent", live_parent)
    assert _observation(source).parent_container_id == PARENT_ID
    assert reads == 2


def test_parent_restart_policy_change_during_probe_still_refuses(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    reads = 0

    def changed_parent(*_args: object) -> dict[str, object]:
        nonlocal reads
        reads += 1
        snapshot = deepcopy(source[3])
        if reads == 2:
            snapshot["HostConfig"]["RestartPolicy"] = {"Name": "always", "MaximumRetryCount": 0}
        return snapshot

    monkeypatch.setattr(authority, "_parent", changed_parent)
    with pytest.raises(HostObservationUnavailable, match="parent or daemon changed"):
        _observation(source)


def test_parent_pid_change_during_probe_still_refuses(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    reads = 0

    def changed_parent(*_args: object) -> dict[str, object]:
        nonlocal reads
        reads += 1
        snapshot = deepcopy(source[3])
        if reads == 2:
            snapshot["State"]["Pid"] = os.getpid() + 10000
        return snapshot

    monkeypatch.setattr(authority, "_parent", changed_parent)
    with pytest.raises(HostObservationUnavailable, match="parent or daemon changed"):
        _observation(source)


def test_parent_new_network_attachment_during_probe_still_refuses(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    reads = 0

    def changed_parent(*_args: object) -> dict[str, object]:
        nonlocal reads
        reads += 1
        snapshot = deepcopy(source[3])
        if reads == 2:
            snapshot["NetworkSettings"]["Networks"]["unauthorized"] = {"NetworkID": "changed"}
        return snapshot

    monkeypatch.setattr(authority, "_parent", changed_parent)
    with pytest.raises(HostObservationUnavailable, match="network attachment"):
        _observation(source)


def test_private_engine_expected_child_id_requires_untruncated_list(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    child_id = "3" * 64
    seen: list[tuple[str, ...]] = []

    def list_children(_host: Path, *arguments: str, **_kwargs: object) -> str:
        seen.append(arguments)
        if arguments[0] == "ps":
            assert arguments == ("ps", "--all", "--quiet", "--no-trunc")
            return child_id
        return ""

    monkeypatch.setattr(authority, "_docker", list_children)
    request, root, outer, _, _ = source
    observed = authority._observe_source(request, root, outer, PARENT_ID, PARENT_IMAGE, require_empty_private_engine=False, expected_container_id=child_id)
    assert observed.parent_container_id == PARENT_ID
    assert any(command[0] == "ps" for command in seen)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("restart", {"Name": "always", "MaximumRetryCount": 0}),
        ("network", "host"),
        ("pid", ""),
        ("privileged", False),
        ("init", False),
        ("autoremove", True),
        ("running", False),
        ("restart_count", 1),
    ],
)
def test_private_engine_refuses_unenforced_or_changed_parent(source: tuple[Path, Path, Path, dict[str, object], Path], field: str, value: object) -> None:
    parent = source[3]
    if field == "restart_count":
        parent["RestartCount"] = value
    elif field == "running":
        parent["State"]["Running"] = value
    else:
        key = {"restart": "RestartPolicy", "network": "NetworkMode", "pid": "PidMode", "privileged": "Privileged", "init": "Init", "autoremove": "AutoRemove"}[field]
        parent["HostConfig"][key] = value
    with pytest.raises(HostObservationUnavailable):
        _observation(source)


def test_private_engine_rejects_unexpected_mount_and_already_configured_child(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    parent = source[3]
    parent["Mounts"].append({"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/host.sock", "RW": True})
    with pytest.raises(HostObservationUnavailable, match="mount count"):
        _observation(source)
    parent["Mounts"].pop()
    monkeypatch.setattr(authority, "_docker", lambda _host, *arguments, **_kwargs: PARENT_ID if arguments[0] == "ps" else "")
    with pytest.raises(HostObservationUnavailable, match="existing container"):
        _observation(source)


def test_private_engine_refuses_unowned_socket_or_extra_default_listener(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    source[3]["Config"]["Entrypoint"] = ["dockerd-entrypoint.sh"]
    with pytest.raises(HostObservationUnavailable, match="daemon command"):
        _observation(source)
    source[3]["Config"]["Entrypoint"] = ["dockerd"]
    source[4].chmod(0o666)
    with pytest.raises(HostObservationUnavailable, match="socket is not owner-scope"):
        _observation(source)


def test_private_engine_socket_custody_is_one_explicit_parent_exec(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []
    source[4].chmod(0o666)

    def custody(_host: Path, *arguments: str, **_kwargs: object) -> str:
        commands.append(arguments)
        source[4].chmod(0o660)
        return ""

    monkeypatch.setattr(authority, "_docker", custody)
    authority._grant_original_socket_owner(source[2], PARENT_ID, source[4], timeout=2)
    assert commands == [("exec", "--user", "0:0", PARENT_ID, "chown", f"{os.geteuid()}:{os.getegid()}", str(source[4]))]
    source[4].chmod(0o666)
    monkeypatch.setattr(authority, "_docker", lambda *_args, **_kwargs: "")
    with pytest.raises(HostObservationUnavailable, match="custody was not established"):
        authority._grant_original_socket_owner(source[2], PARENT_ID, source[4], timeout=2)


def test_private_engine_lifetime_change_during_held_source_is_refused(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    _seal(source)
    with pytest.raises(HostObservationUnavailable, match="replacement exclusion ended"):
        with authority.hold_private_engine_source(*source[:3]):
            source[3]["State"]["StartedAt"] = "2026-11-11T12:02:00Z"


def test_private_engine_source_records_are_irreversible_and_partial_evidence_refuses(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    _seal(source)
    with pytest.raises(FileExistsError):
        authority._record(source[1] / authority.SOURCE_READY, {"schema_version": 1})
    (source[1] / authority.SOURCE_CREATED).write_bytes(b'{"partial":')
    with pytest.raises((ValueError, HostObservationUnavailable)):
        with authority.hold_private_engine_source(*source[:3]):
            pytest.fail("partial original unexpectedly admitted")


def test_uncertain_private_parent_creation_poisoned_before_replay(configured_request: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = begin_new_instance(configured_request)
    root = configured_request.parent / "new-private-source"
    root.mkdir(mode=0o700)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(authority, "_engine_id", lambda *_args: "outer-engine")

    def uncertain_create(_socket: Path, *args: str, **_kwargs: object) -> str:
        commands.append(args)
        return "unknown-effect"

    monkeypatch.setattr(authority, "_docker", uncertain_create)
    with pytest.raises(HostObservationUnavailable, match="uncertain result"):
        authority.create_private_engine(configured_request, root, Path("/var/run/docker.sock"), PARENT_IMAGE)
    saved = authority._read_record(root / authority.SOURCE_INTENT)
    assert saved["origin_sha256"] == origin.record_sha256
    assert saved["parent_image_id"] == PARENT_IMAGE
    assert len(commands) == 1 and commands[0][0] == "create"
    assert not (root / authority.SOURCE_CREATED).exists()
    with pytest.raises(FileExistsError):
        authority.create_private_engine(configured_request, root, Path("/var/run/docker.sock"), PARENT_IMAGE)
    assert len(commands) == 1, "a replay must never create a second privileged parent"


def test_private_engine_source_is_durably_bound_to_host_first_launch_claim(source: tuple[Path, Path, Path, dict[str, object], Path]) -> None:
    evidence = _seal(source)
    request_file = source[0]
    original = authority.inspect_new_instance_origin(request_file)
    guard = request_file.parent / "guard"
    os.mknod(guard / "fresh-guard.sock", stat.S_IFSOCK | 0o600)
    created = CreatedNewDockerSnapshot(
        request_id=original.request_id,
        origin_sha256=original.record_sha256,
        engine_id=evidence.private_engine_id,
        container_id="3" * 64,
        image_id="sha256:" + "4" * 64,
        created_at="2026-11-11T12:02:00Z",
        entrypoint=("python3", "-B", "-m", "auto_coder.management.bootstrap_supervisor"),
        command=("--config", str(request_file.parent / "bootstrap.json")),
        mounts_sha256="5" * 64,
    )
    digest = _create_host_start_claim(guard, created, private_source=evidence)
    parsed = load_new_first_start(request_file)
    assert parsed.host_claim_sha256 == digest
    assert parsed.private_source_sha256 == hashlib.sha256(_canonical(asdict(evidence))).hexdigest()
    assert parsed.created.engine_id == evidence.private_engine_id
    assert parsed.created.container_id == created.container_id


def test_private_engine_launch_refuses_wrong_selected_socket_without_claim(source: tuple[Path, Path, Path, dict[str, object], Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _seal(source)
    selected_child = "3" * 64
    monkeypatch.setattr(authority, "_docker", lambda _host, *arguments, **_kwargs: selected_child if arguments[0] == "ps" else "")
    monkeypatch.setenv("DOCKER_HOST", "unix:///tmp/unrelated-docker.sock")
    with pytest.raises(HostObservationUnavailable, match="selected Engine socket"):
        start_new_docker_once(source[0], selected_child, "sha256:" + "4" * 64, private_root=source[1], outer_socket=source[2])
    assert not (source[0].parent / "guard" / HOST_FIRST_START).exists()
