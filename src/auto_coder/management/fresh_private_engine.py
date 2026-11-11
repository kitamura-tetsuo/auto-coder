"""Trusted-host custody of a newly created, separate Docker Engine.

This module belongs to the local host operator, never to the management
application or its OAuth/MCP process. A parent container has to be created by
this producer; observing an arbitrary existing DinD container cannot establish
that its engine has no historical startup sources.

The records are one-way. A crash at any effect boundary requires an explicit
reconciliation; the producer must not quietly start a second parent.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from .fresh_begin import _canonical, inspect_new_instance_origin
from .fresh_host_observation import HostObservationUnavailable
from .fresh_new_profile import inspect_new_instance_profile
from .fresh_preflight import _file, _open, _path

SOURCE_INTENT = "private-engine-intent.json"
SOURCE_CREATED = "private-engine-created.json"
SOURCE_READY = "private-engine-ready.json"
_HEX_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class PrivateEngineEvidence:
    request_id: str
    origin_sha256: str
    private_root: str
    private_root_dev: int
    private_root_ino: int
    socket_dir_dev: int
    socket_dir_ino: int
    data_dir_dev: int
    data_dir_ino: int
    socket_dev: int
    socket_ino: int
    outer_engine_id: str
    parent_container_id: str
    parent_image_id: str
    parent_created_at: str
    parent_started_at: str
    parent_pid: int
    parent_start_token: str
    parent_pid_namespace: str
    parent_net_namespace: str
    host_boot_id: str
    private_engine_id: str


def _docker(host_socket: Path, *args: str, timeout: float = 20) -> str:
    """Select the exact local Engine even when a shell has a Docker context."""
    _path(str(host_socket))
    try:
        result = subprocess.run(
            ["docker", "--host", f"unix://{host_socket}", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "DOCKER_CONTEXT": "default"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise HostObservationUnavailable("private Engine host Docker command unavailable") from error
    return result.stdout.strip()


def _engine_id(host_socket: Path) -> str:
    try:
        value = json.loads(_docker(host_socket, "info", "--format", "{{json .ID}}"))
    except ValueError as error:
        raise HostObservationUnavailable("private Engine host identity is malformed") from error
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        raise HostObservationUnavailable("private Engine host identity is invalid")
    return value


def _root_stat(path: Path, *, require_empty: bool = False) -> os.stat_result:
    with _open(path, directory=True) as descriptor:
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("private Engine root must be owner-only")
        if require_empty and os.listdir(descriptor):
            raise FileExistsError("private Engine root already contains an effect or prior state")
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise HostObservationUnavailable("private Engine directory identity changed")
        return info


def _data_stat(private_root: Path) -> os.stat_result:
    """Stat the new data root without reading daemon-owned Docker contents.

    Docker may chown the mounted data-root directory to root during its own
    initialization. Only the original owner-only parent directory can name
    this inode; requiring the host operator to traverse Docker's internal
    directory would needlessly expand access.
    """
    with _open(private_root, directory=True) as descriptor:
        info = os.stat("data", dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
            raise HostObservationUnavailable("private Engine data root identity or write protection differs")
        return info


def _record(path: Path, value: dict[str, object]) -> str:
    """Durably publish once; a partial/uncertain record is not retried."""
    encoded = _canonical(value)
    with _open(path.parent, directory=True) as descriptor:
        output = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(output, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(descriptor)
    return hashlib.sha256(encoded).hexdigest()


def _read_record(path: Path) -> dict[str, object]:
    _, encoded = _file(path, private=True, limit=65536)
    value = json.loads(encoded)
    if not isinstance(value, dict) or _canonical(value) != encoded:
        raise HostObservationUnavailable("private Engine source record is malformed")
    return value


def _private_paths(private_root: Path, request_file: Path) -> tuple[Path, Path, Path]:
    private_root = _path(str(private_root))
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    protected = [Path(request[key]) for key in ("state_root", "operation_root", "guard_root", "oauth_root", "application_root")]
    protected.extend(Path(request[key]) for key in ("bootstrap_config", "management_config", "executable"))
    protected.append(request_file)
    if any(private_root == path or private_root in path.parents or path in private_root.parents for path in protected):
        raise ValueError("private Docker Engine source must not overlap the NEW identity or OAuth")
    return private_root, private_root / "socket", private_root / "data"


def _parent(host_socket: Path, container_id: str) -> dict[str, object]:
    if not _HEX_ID.fullmatch(container_id):
        raise ValueError("exact private Engine parent container ID required")
    try:
        value = json.loads(_docker(host_socket, "inspect", "--type", "container", "--format", "{{json .}}", container_id))
    except ValueError as error:
        raise HostObservationUnavailable("private Engine parent inspection malformed") from error
    if not isinstance(value, dict) or value.get("Id") != container_id:
        raise HostObservationUnavailable("private Engine parent identity differs")
    return value


def _lifetime(pid: int, *, outer_socket: Path, parent_id: str) -> tuple[str, str, str, str]:
    if type(pid) is not int or pid <= 0:
        raise HostObservationUnavailable("private Engine parent PID unavailable")
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        token = raw[19]
        boot_id = (Path("/proc/sys/kernel/random/boot_id")).read_text().strip()
        try:
            pid_namespace = os.readlink(Path("/proc") / str(pid) / "ns/pid")
            net_namespace = os.readlink(Path("/proc") / str(pid) / "ns/net")
        except PermissionError:
            # A non-root host operator can inspect the parent PID start token
            # but Linux may deny ptrace-gated /proc/<root-pid>/ns links. Ask
            # the exact Docker-created parent to read its own kernel links;
            # the already verified Docker policy fixes PID mode to host and
            # network mode to none. Reject a parent in the host network or a
            # caller outside the outer Engine's host PID namespace.
            pid_namespace = _docker(outer_socket, "exec", "--user", "0:0", parent_id, "readlink", "/proc/self/ns/pid")
            net_namespace = _docker(outer_socket, "exec", "--user", "0:0", parent_id, "readlink", "/proc/self/ns/net")
            if pid_namespace != os.readlink("/proc/self/ns/pid") or net_namespace == os.readlink("/proc/self/ns/net"):
                raise HostObservationUnavailable("private Engine parent namespace cannot be independently bound to this host")
        if not token.isdecimal() or int(token) <= 0 or str(uuid.UUID(boot_id)) != boot_id:
            raise ValueError("invalid Linux parent lifetime")
    except (OSError, IndexError, ValueError) as error:
        raise HostObservationUnavailable("private Engine parent kernel lifetime unavailable") from error
    if not pid_namespace.startswith("pid:[") or not net_namespace.startswith("net:["):
        raise HostObservationUnavailable("private Engine parent namespaces unavailable")
    return token, pid_namespace, net_namespace, boot_id


def _mounts_are_private(parent: dict[str, object], expected: list[tuple[str, str, bool]]) -> None:
    actual = parent.get("Mounts")
    if not isinstance(actual, list) or len(actual) != len(expected):
        raise HostObservationUnavailable("private Engine parent mount count differs")
    entries = []
    for item in actual:
        if not isinstance(item, dict) or item.get("Type") != "bind" or type(item.get("RW")) is not bool:
            raise HostObservationUnavailable("private Engine parent has a non-bind or unverified mount")
        entries.append((item.get("Source"), item.get("Destination"), item["RW"]))
    if sorted(entries) != sorted(expected):
        raise HostObservationUnavailable("private Engine parent mounts differ from original scope")


def _parent_security_snapshot(parent: dict[str, object]) -> dict[str, object]:
    """Compare actual immutable authority fields, not Docker runtime telemetry.

    A running Docker Engine updates inspect metadata during exec probes and
    daemon activity. Its parent identity, original image, exact mounts,
    container configuration, host security policy and lifetime must not move.
    This explicitly excludes volatile health, exec and display counters;
    it does not relax any field used to authorize the independent source.
    """
    state = parent.get("State")
    network = parent.get("NetworkSettings")
    if not isinstance(state, dict) or not isinstance(network, dict) or not isinstance(network.get("Networks"), dict):
        raise HostObservationUnavailable("private Engine parent state or network evidence is invalid")
    network_names = network["Networks"]
    if set(network_names) - {"none"}:
        raise HostObservationUnavailable("private Engine parent acquired an independent network attachment")
    static_state_keys = ("Status", "Running", "Restarting", "Pid", "StartedAt", "FinishedAt", "Dead", "OOMKilled", "ExitCode", "Error")
    return {
        **{key: parent.get(key) for key in ("Id", "Name", "Image", "Created", "RestartCount", "Path", "Args", "HostConfig", "Config", "Mounts")},
        "State": {key: state.get(key) for key in static_state_keys},
        "Networks": sorted(network_names),
    }


def _observe_source(
    request_file: Path,
    private_root: Path,
    outer_socket: Path,
    parent_id: str,
    parent_image_id: str,
    *,
    require_empty_private_engine: bool,
    original: PrivateEngineEvidence | None = None,
    expected_container_id: str | None = None,
) -> PrivateEngineEvidence:
    """Independently recheck parent policy, kernel lifetime and private Engine."""
    private_root, socket_dir, data_dir = _private_paths(private_root, request_file)
    root_info = _root_stat(private_root)
    socket_dir_info = _root_stat(socket_dir)
    data_dir_info = _data_stat(private_root)
    private_socket = socket_dir / "docker.sock"
    # Opening a Unix socket with open(2) is unsupported. Stat its directory
    # entry through a held no-follow parent directory descriptor instead.
    with _open(socket_dir, directory=True) as descriptor:
        socket_info = os.stat("docker.sock", dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.geteuid() or socket_info.st_gid != os.getegid() or socket_info.st_mode & 0o007:
            raise HostObservationUnavailable("private Engine socket is not owner-scope protected")
    if not _IMAGE_ID.fullmatch(parent_image_id):
        raise ValueError("immutable parent image ID required")
    if original is None:
        origin = inspect_new_instance_origin(request_file)
        profile = inspect_new_instance_profile(request_file)
        if profile.origin_sha256 != origin.record_sha256 or profile.request_id != origin.request_id:
            raise HostObservationUnavailable("private Engine original intent and application differ")
        request_id, origin_digest = origin.request_id, origin.record_sha256
    else:
        # A held launch has added its one-use claim to the guard root. The
        # existing first-start reader independently verifies those additions;
        # rechecking this source must not confuse them with a new origin.
        request_id, origin_digest = original.request_id, original.origin_sha256
    outer_id = _engine_id(outer_socket)
    parent = _parent(outer_socket, parent_id)
    cfg = parent.get("HostConfig")
    state = parent.get("State")
    config = parent.get("Config")
    if not isinstance(cfg, dict) or not isinstance(state, dict) or not isinstance(config, dict):
        raise HostObservationUnavailable("private Engine parent configuration unavailable")
    if (
        parent.get("Image") != parent_image_id
        or state.get("Status") != "running"
        or state.get("Running") is not True
        or parent.get("RestartCount") != 0
        or cfg.get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
        or cfg.get("NetworkMode") != "none"
        or cfg.get("PidMode") != "host"
        or cfg.get("Privileged") is not True
        or cfg.get("Init") is not True
        or cfg.get("AutoRemove") is not False
        or cfg.get("PortBindings") not in (None, {})
    ):
        raise HostObservationUnavailable("private Engine parent runtime or restart source is unsupported")
    expected_mounts = [(str(socket_dir), str(socket_dir), True), (str(data_dir), "/var/lib/docker", True)]
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    for key in ("state_root", "operation_root", "guard_root", "oauth_root", "bootstrap_config", "management_config", "executable", "application_root"):
        path = str(request[key])
        expected_mounts.append((path, path, key in {"state_root", "operation_root", "guard_root", "oauth_root"}))
    expected_mounts.append((str(request_file), str(request_file), False))
    _mounts_are_private(parent, expected_mounts)
    # Match the proved CI fixture: bypass the DinD entrypoint, which would
    # otherwise add TCP listeners, and disable the isolated bridge/firewall.
    expected_cmd = [
        f"--host=unix://{private_socket}",
        "--data-root=/var/lib/docker",
        "--storage-driver=vfs",
        "--iptables=false",
        "--bridge=none",
        "--ip-forward=false",
        "--ip-masq=false",
    ]
    if config.get("Cmd") != expected_cmd or config.get("Entrypoint") != ["dockerd"]:
        raise HostObservationUnavailable("private Engine parent selected daemon command differs")
    created = parent.get("Created")
    started = state.get("StartedAt")
    if not isinstance(created, str) or not created or not isinstance(started, str) or not started or started.startswith("0001-"):
        raise HostObservationUnavailable("private Engine parent first lifetime is unavailable")
    pid = state.get("Pid")
    if type(pid) is not int or pid <= 0:
        raise HostObservationUnavailable("private Engine parent host PID is unavailable")
    token, pid_ns, net_ns, boot_id = _lifetime(pid, outer_socket=outer_socket, parent_id=parent_id)
    private_id = _engine_id(private_socket)
    if private_id == outer_id:
        raise HostObservationUnavailable("private Engine is the outer production Docker Engine")
    if require_empty_private_engine or expected_container_id is not None:
        children = _docker(private_socket, "ps", "--all", "--quiet", "--no-trunc").splitlines()
        if require_empty_private_engine and children:
            raise HostObservationUnavailable("private Engine has existing container startup sources")
        if expected_container_id is not None and children != [expected_container_id]:
            raise HostObservationUnavailable("private Engine has another configured startup source")
    if _parent_security_snapshot(_parent(outer_socket, parent_id)) != _parent_security_snapshot(parent) or _engine_id(outer_socket) != outer_id or _engine_id(private_socket) != private_id:
        raise HostObservationUnavailable("private Engine parent or daemon changed during observation")
    if _lifetime(pid, outer_socket=outer_socket, parent_id=parent_id) != (token, pid_ns, net_ns, boot_id):
        raise HostObservationUnavailable("private Engine parent lifetime changed during observation")
    if original is None and (inspect_new_instance_origin(request_file) != origin or inspect_new_instance_profile(request_file) != profile):
        raise HostObservationUnavailable("private Engine original identity changed")
    return PrivateEngineEvidence(
        request_id,
        origin_digest,
        str(private_root),
        root_info.st_dev,
        root_info.st_ino,
        socket_dir_info.st_dev,
        socket_dir_info.st_ino,
        data_dir_info.st_dev,
        data_dir_info.st_ino,
        socket_info.st_dev,
        socket_info.st_ino,
        outer_id,
        parent_id,
        parent_image_id,
        created,
        started,
        pid,
        token,
        pid_ns,
        net_ns,
        boot_id,
        private_id,
    )


def assert_private_engine_live(request_file: Path, private_root: Path, outer_socket: Path, expected: PrivateEngineEvidence, *, container_id: str) -> None:
    """Recheck one held source at the launch and host-guard effect boundaries."""
    observed = _observe_source(
        request_file,
        private_root,
        outer_socket,
        expected.parent_container_id,
        expected.parent_image_id,
        require_empty_private_engine=False,
        original=expected,
        expected_container_id=container_id,
    )
    if observed != expected:
        raise HostObservationUnavailable("private Engine parent/namespace/source changed before child launch")


def _assert_original_source_identity(observed: PrivateEngineEvidence, intent: dict[str, object]) -> None:
    identity = {
        "private_root_dev": observed.private_root_dev,
        "private_root_ino": observed.private_root_ino,
        "socket_dir_dev": observed.socket_dir_dev,
        "socket_dir_ino": observed.socket_dir_ino,
        "data_dir_dev": observed.data_dir_dev,
        "data_dir_ino": observed.data_dir_ino,
    }
    if any(intent.get(key) != value for key, value in identity.items()):
        raise HostObservationUnavailable("private Engine directories differ from pre-effect intent")


@contextmanager
def hold_private_engine_source(request_file: Path, private_root: Path, outer_socket: Path, *, expected_container_id: str | None = None) -> Iterator[PrivateEngineEvidence]:
    """Hold original source custody while the caller performs one launch."""
    private_root, _, _ = _private_paths(private_root, request_file)
    with _open(private_root, directory=True) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        expected_entries = {"socket", "data", SOURCE_INTENT, SOURCE_CREATED, SOURCE_READY}
        if set(os.listdir(descriptor)) != expected_entries:
            raise HostObservationUnavailable("private Engine source contains an unexpected or missing entry")
        ready = _read_record(private_root / SOURCE_READY)
        created = _read_record(private_root / SOURCE_CREATED)
        intent = _read_record(private_root / SOURCE_INTENT)
        if set(intent) != {"schema_version", "origin_sha256", "request_id", "parent_image_id", "outer_engine_id", "nonce", "private_root_dev", "private_root_ino", "socket_dir_dev", "socket_dir_ino", "data_dir_dev", "data_dir_ino"} or intent.get("schema_version") != 1:
            raise HostObservationUnavailable("private Engine source intent is invalid")
        if set(created) != {"schema_version", "intent_sha256", "parent_container_id"} or created.get("schema_version") != 1 or created.get("intent_sha256") != hashlib.sha256(_canonical(intent)).hexdigest():
            raise HostObservationUnavailable("private Engine source creation is invalid")
        if set(ready) != {"schema_version", "created_sha256", "evidence"} or ready.get("schema_version") != 1 or ready.get("created_sha256") != hashlib.sha256(_canonical(created)).hexdigest():
            raise HostObservationUnavailable("private Engine source admission record is invalid")
        nonce = intent["nonce"]
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise HostObservationUnavailable("private Engine original producer nonce is invalid")
        parent = _parent(outer_socket, str(created["parent_container_id"]))
        if parent.get("Name") != "/ac-new-private-engine-" + nonce[:16]:
            raise HostObservationUnavailable("private Engine parent was not the selected original source")
        observed = _observe_source(request_file, private_root, outer_socket, str(created["parent_container_id"]), str(intent["parent_image_id"]), require_empty_private_engine=False, expected_container_id=expected_container_id)
        _assert_original_source_identity(observed, intent)
        if asdict(observed) != ready["evidence"] or observed.outer_engine_id != intent["outer_engine_id"] or (observed.request_id, observed.origin_sha256) != (intent["request_id"], intent["origin_sha256"]):
            raise HostObservationUnavailable("private Engine live source differs from durable original")
        yield observed
        if set(os.listdir(descriptor)) != expected_entries or any(_read_record(private_root / path) != original for path, original in ((SOURCE_INTENT, intent), (SOURCE_CREATED, created), (SOURCE_READY, ready))):
            raise HostObservationUnavailable("private Engine durable source changed during launch")
        if _observe_source(request_file, private_root, outer_socket, observed.parent_container_id, observed.parent_image_id, require_empty_private_engine=False, original=observed, expected_container_id=expected_container_id) != observed:
            raise HostObservationUnavailable("private Engine replacement exclusion ended during launch")


def _create_dir(parent: Path, name: str) -> None:
    with _open(parent, directory=True) as descriptor:
        os.mkdir(name, mode=0o700, dir_fd=descriptor)
        os.fsync(descriptor)


def _grant_original_socket_owner(outer_socket: Path, parent_id: str, private_socket: Path, *, timeout: float) -> None:
    """Transfer only the new DinD socket to the trusted host operator once.

    The created-parent receipt already exists before this effect. If the
    owner transfer is interrupted, initialization is poisoned rather than
    running another transient privileged process on retry.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        parent = _parent(outer_socket, parent_id)
        state = parent.get("State")
        if not isinstance(state, dict) or state.get("Status") != "running" or state.get("Running") is not True:
            raise HostObservationUnavailable("private Engine parent exited before socket custody")
        try:
            info = os.stat(private_socket, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode):
                raise HostObservationUnavailable("private Engine created a non-socket endpoint")
            break
        except FileNotFoundError:
            time.sleep(0.25)
    else:
        raise HostObservationUnavailable("private Engine private socket did not appear")
    _docker(outer_socket, "exec", "--user", "0:0", parent_id, "chown", f"{os.geteuid()}:{os.getegid()}", str(private_socket), timeout=timeout)
    info = os.stat(private_socket, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or (info.st_uid, info.st_gid) != (os.geteuid(), os.getegid()) or info.st_mode & 0o007:
        raise HostObservationUnavailable("private Engine host operator socket custody was not established")


def create_private_engine(request_file: Path, private_root: Path, outer_socket: Path, parent_image_id: str, *, timeout: float = 60) -> PrivateEngineEvidence:
    """Make exactly one parent Engine from new protected roots, then seal it.

    There is deliberately no automatic cleanup or retry after a Docker effect:
    a killed producer must not hide whether a parent previously ran.
    """
    if not _IMAGE_ID.fullmatch(parent_image_id):
        raise ValueError("immutable Docker-in-Docker parent image ID required")
    private_root, socket_dir, data_dir = _private_paths(private_root, request_file)
    root_info = _root_stat(private_root, require_empty=True)
    origin = inspect_new_instance_origin(request_file)
    profile = inspect_new_instance_profile(request_file)
    if profile.origin_sha256 != origin.record_sha256:
        raise HostObservationUnavailable("private Engine source profile differs")
    outer_id = _engine_id(outer_socket)
    nonce = uuid.uuid4().hex
    # Directory creation is still before the first Docker effect. A crash
    # here leaves a nonempty root, which must refuse any second producer.
    _create_dir(private_root, "socket")
    _create_dir(private_root, "data")
    socket_info = _root_stat(socket_dir)
    data_info = _data_stat(private_root)
    intent = {
        "schema_version": 1,
        "origin_sha256": origin.record_sha256,
        "request_id": origin.request_id,
        "parent_image_id": parent_image_id,
        "outer_engine_id": outer_id,
        "nonce": nonce,
        "private_root_dev": root_info.st_dev,
        "private_root_ino": root_info.st_ino,
        "socket_dir_dev": socket_info.st_dev,
        "socket_dir_ino": socket_info.st_ino,
        "data_dir_dev": data_info.st_dev,
        "data_dir_ino": data_info.st_ino,
    }
    _record(private_root / SOURCE_INTENT, intent)
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    cmd = [
        "create",
        "--name",
        "ac-new-private-engine-" + nonce[:16],
        "--privileged",
        "--pid",
        "host",
        "--network",
        "none",
        "--restart",
        "no",
        "--init",
        "--entrypoint",
        "dockerd",
        "--env",
        "DOCKER_TLS_CERTDIR=",
        "--mount",
        f"type=bind,src={socket_dir},dst={socket_dir}",
        "--mount",
        f"type=bind,src={data_dir},dst=/var/lib/docker",
    ]
    for key in ("state_root", "operation_root", "guard_root", "oauth_root", "bootstrap_config", "management_config", "executable", "application_root"):
        path = str(request[key])
        cmd.extend(["--mount", f"type=bind,src={path},dst={path}" + ("" if key in {"state_root", "operation_root", "guard_root", "oauth_root"} else ",readonly")])
    cmd.extend(["--mount", f"type=bind,src={request_file},dst={request_file},readonly"])
    cmd.extend(
        [
            parent_image_id,
            f"--host=unix://{socket_dir / 'docker.sock'}",
            "--data-root=/var/lib/docker",
            "--storage-driver=vfs",
            "--iptables=false",
            "--bridge=none",
            "--ip-forward=false",
            "--ip-masq=false",
        ]
    )
    parent_id = _docker(outer_socket, *cmd, timeout=timeout)
    if not _HEX_ID.fullmatch(parent_id):
        raise HostObservationUnavailable("private Engine parent creation had an uncertain result")
    created = {"schema_version": 1, "intent_sha256": hashlib.sha256(_canonical(intent)).hexdigest(), "parent_container_id": parent_id}
    _record(private_root / SOURCE_CREATED, created)
    from .fresh_new_host_start import _start_docker_container_once

    current = _engine_id(outer_socket)
    if current != outer_id:
        raise HostObservationUnavailable("outer Docker Engine changed before source start")
    _start_docker_container_once(parent_id, outer_id, docker_host="unix://" + str(outer_socket))
    _grant_original_socket_owner(outer_socket, parent_id, socket_dir / "docker.sock", timeout=timeout)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            evidence = _observe_source(request_file, private_root, outer_socket, parent_id, parent_image_id, require_empty_private_engine=True)
            _assert_original_source_identity(evidence, intent)
            _record(private_root / SOURCE_READY, {"schema_version": 1, "created_sha256": hashlib.sha256(_canonical(created)).hexdigest(), "evidence": asdict(evidence)})
            return evidence
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            last_error = error
            time.sleep(0.25)
    raise HostObservationUnavailable("new private Engine did not reach an independently verified empty origin") from last_error
