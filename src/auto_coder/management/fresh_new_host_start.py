"""One trusted-host Docker first start for a genuinely NEW management scope.

The Docker daemon is the independent source of namespace/never-started facts.
Its no-restart, private-PID and network-none profile excludes an earlier
process inside this exact new container. The operator remains responsible for
keeping unrelated host controllers outside this newly reserved identity.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import socket
import sqlite3
import stat
import struct
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from loguru import logger

from .fresh_begin import _canonical, _lifetime, inspect_new_instance_origin
from .fresh_host_observation import HostObservationUnavailable, _assert_pid_flock
from .fresh_new_admission_fence import hold_new_guard_flock
from .fresh_new_docker import CreatedNewDockerSnapshot, _dedicated_network_id, _docker_created_fields, _engine_id, _inspect_created_scope, assert_running_new_network
from .fresh_new_first_launch import CHILD_LAUNCHED, HOST_FIRST_START, SOCKET, load_new_first_start
from .fresh_new_profile import inspect_new_instance_profile
from .fresh_preflight import _file, _open


def _create_host_start_claim(guard: Path, created: CreatedNewDockerSnapshot, *, private_source: object | None = None) -> str:
    """One-way transition: a failed Docker start must never erase this file."""
    value = {
        "schema_version": 1,
        "profile": "genuinely-new-private-engine-v1" if private_source is not None else "genuinely-new-docker-v1",
        "created": asdict(created),
        "launch_nonce": secrets.token_hex(32),
        "producer_lifetime": _lifetime(),
    }
    if private_source is not None:
        from .fresh_private_engine import PrivateEngineEvidence

        if not isinstance(private_source, PrivateEngineEvidence):
            raise TypeError("NEW private Engine original evidence required")
        value["private_source"] = asdict(private_source)
    encoded = _canonical(value)
    with _open(guard, directory=True) as descriptor:
        child = os.open(HOST_FIRST_START, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
        with os.fdopen(child, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(descriptor)
    return hashlib.sha256(encoded).hexdigest()


def _bootstrap_command_matches(created, bootstrap_config: str, request_file: Path) -> bool:
    """Bind actual Docker PID-1 argv to the selected NEW Supervisor entrypoint."""
    return created.entrypoint == ("python3", "-B", "-m", "auto_coder.management.bootstrap_supervisor") and created.command == ("--config", bootstrap_config, "--new-initialization", "--request-file", str(request_file))


class _LocalDockerConnection(http.client.HTTPConnection):
    """Use the selected local Docker Engine's native HTTP start result."""

    def __init__(self, socket_path: Path) -> None:
        super().__init__("localhost", timeout=20)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self._socket_path))
        except OSError:
            connection.close()
            raise
        self.sock = connection


def _start_docker_container_once(container_id: str, engine_id: str, *, docker_host: str | None = None) -> None:
    """Require Engine HTTP 204; CLI exit zero also includes an already-running 304."""
    if os.environ.get("DOCKER_CONTEXT") not in (None, "", "default"):
        raise HostObservationUnavailable("NEW Docker context must be the independently verified local engine")
    raw_host = docker_host if docker_host is not None else os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    endpoint = urlsplit(raw_host)
    if endpoint.scheme != "unix" or endpoint.netloc or not endpoint.path.startswith("/") or endpoint.query or endpoint.fragment:
        raise HostObservationUnavailable("NEW Docker first start requires a local Unix Engine socket")
    connection = _LocalDockerConnection(Path(endpoint.path))
    try:
        connection.request("GET", "/v1.41/info")
        info_response = connection.getresponse()
        info_bytes = info_response.read(262145)
        if info_response.status != 200 or len(info_bytes) > 262144:
            raise HostObservationUnavailable("NEW Docker Engine API identity is unavailable")
        info = json.loads(info_bytes)
        if not isinstance(info, dict) or info.get("ID") != engine_id:
            raise HostObservationUnavailable("NEW Docker API engine differs from verified original")
        connection.request("POST", f"/v1.41/containers/{container_id}/start")
        start_response = connection.getresponse()
        start_response.read(4096)
        if start_response.status != 204:
            raise HostObservationUnavailable("NEW Docker Engine did not perform an original start (possibly already running)")
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise HostObservationUnavailable("NEW Docker Engine one-shot start result is unavailable") from error
    finally:
        connection.close()


def _assert_docker_policy_unchanged(original: tuple[object, ...], current: tuple[object, ...]) -> None:
    """Refuse restart-policy or other Docker runtime changes across admission."""
    if original[:3] != current[:3] or original[9:23] != current[9:23]:
        raise HostObservationUnavailable("NEW Docker static startup or restart policy changed")


def _assert_new_owner(peer_pid: int, owner_incarnation: str, state_root: Path, instance: str) -> None:
    """Bind a peer to the actual held owner lock and native supervisor row."""
    lock = state_root / ".supervisor-owner.lock"
    descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise HostObservationUnavailable("NEW owner lock is unprotected")
        _assert_pid_flock(peer_pid, descriptor, "WRITE")
    finally:
        os.close(descriptor)
    fields = (Path("/proc") / str(peer_pid) / "stat").read_text().rsplit(")", 1)[1].split()
    if len(fields) < 20 or not fields[19].isdecimal() or int(fields[19]) <= 0:
        raise HostObservationUnavailable("NEW live owner start token is unavailable")
    ledger = state_root / "management-child.sqlite3"
    if ledger.is_symlink() or not ledger.is_file():
        raise HostObservationUnavailable("NEW native child ledger is unavailable")
    with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
        rows = connection.execute("SELECT instance_id,incarnation,pid,start_token FROM supervisor_presence").fetchall()
    if rows != [(instance, owner_incarnation, 1, fields[19])]:
        raise HostObservationUnavailable("NEW owner incarnation differs from native presence")


def _serve_one_new_guard(
    listener: socket.socket,
    request_file: Path,
    container_id: str,
    image_id: str,
    host_claim_sha256: str,
    original_docker_fields: tuple[object, ...],
    *,
    timeout: float,
    source_sha256: str = "",
    assert_source_live: Callable[[], None] | None = None,
) -> None:
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    deadline = time.monotonic() + timeout
    listener.settimeout(0.25)
    while True:
        if time.monotonic() >= deadline:
            raise HostObservationUnavailable("NEW Docker owner did not request its first-start guard before the deadline")
        if _docker_created_fields(container_id)[3:5] != ("running", True):
            raise HostObservationUnavailable("NEW Docker owner exited before requesting its first-start guard")
        try:
            connection, _ = listener.accept()
            break
        except socket.timeout:
            continue
    with connection:
        connection.settimeout(10)
        peer_pid, peer_uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if peer_uid != os.geteuid():
            raise HostObservationUnavailable("NEW host guard peer UID differs")
        with connection.makefile("rb") as stream:
            line = stream.readline(4097)
        if not line.endswith(b"\n") or len(line) > 4096:
            raise HostObservationUnavailable("NEW grant challenge is incomplete")
        payload = json.loads(line)
        if not isinstance(payload, dict) or set(payload) != {"request_id", "intent_sha256", "owner_incarnation", "new_container_id", "challenge"}:
            raise HostObservationUnavailable("NEW grant challenge fields differ")
        owner = payload["owner_incarnation"]
        challenge = payload["challenge"]
        try:
            canonical_owner = str(uuid.UUID(owner))
        except (ValueError, TypeError) as error:
            raise HostObservationUnavailable("NEW grant owner incarnation differs") from error
        if not isinstance(challenge, str) or len(challenge) != 64 or any(char not in "0123456789abcdef" for char in challenge):
            raise HostObservationUnavailable("NEW grant challenge nonce differs")
        start = load_new_first_start(request_file, state_entries=None, operation_entries=frozenset())
        if (
            payload["request_id"] != start.origin.request_id
            or payload["intent_sha256"] != start.origin.record_sha256
            or payload["new_container_id"] != container_id
            or canonical_owner != owner
            or start.host_claim_sha256 != host_claim_sha256
            or start.created.image_id != image_id
            or start.private_source_sha256 != source_sha256
        ):
            raise HostObservationUnavailable("NEW grant does not bind the original host start")
        fields = _docker_created_fields(container_id)
        _assert_docker_policy_unchanged(original_docker_fields, fields)
        if fields[1] != image_id or fields[3:6] != ("running", True, peer_pid) or fields[8] != 0 or _engine_id() != start.created.engine_id:
            raise HostObservationUnavailable("NEW Docker PID-1 running identity differs")
        assert_running_new_network(request_file, start.created, fields)
        _assert_new_owner(peer_pid, owner, Path(request["state_root"]), start.profile.instance_id)
        # Recheck the independent engine and kernel owner after ledger reading.
        fields_again = _docker_created_fields(container_id)
        _assert_docker_policy_unchanged(original_docker_fields, fields_again)
        if fields_again != fields or _engine_id() != start.created.engine_id:
            raise HostObservationUnavailable("NEW Docker/owner observation changed")
        assert_running_new_network(request_file, start.created, fields_again)
        _assert_new_owner(peer_pid, owner, Path(request["state_root"]), start.profile.instance_id)
        if assert_source_live is not None:
            assert_source_live()
        connection.sendall(_canonical({**payload, "granted": "host-evidence-verified"}) + b"\n")
        logger.bind(request_id=start.origin.request_id, new_container=container_id).info("NEW host grant delivered to exact Docker owner")


def _await_observed_first_child(state_root: Path, request_id: str, host_claim_sha256: str, container_id: str, *, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = state_root / CHILD_LAUNCHED
        if path.exists() or path.is_symlink():
            _, encoded = _file(path, private=True, limit=65536)
            record = json.loads(encoded)
            if not isinstance(record, dict) or record.get("request_id") != request_id or record.get("host_claim_sha256") != host_claim_sha256 or not isinstance(record.get("child_identity"), str) or _canonical(record) != encoded:
                raise HostObservationUnavailable("NEW child launch receipt is mismatched")
            if _docker_created_fields(container_id)[3:5] != ("running", True):
                raise HostObservationUnavailable("NEW container exited during child observation")
            return {"status": "first-child-observed-awaiting-authenticated-readback", "request_id": request_id, "child_identity": record["child_identity"], "launch_authorized": False}
        if _docker_created_fields(container_id)[3:5] != ("running", True):
            raise HostObservationUnavailable("NEW container exited before first-child receipt")
        time.sleep(0.1)
    raise HostObservationUnavailable("NEW first child launch remains unknown after deadline")


def start_new_docker_once(
    request_file: Path,
    container_id: str,
    image_id: str,
    *,
    timeout: float = 120,
    private_root: Path | None = None,
    outer_socket: Path = Path("/var/run/docker.sock"),
) -> dict[str, object]:
    """Consume a first-start claim and invoke Docker start exactly once.

    The result never asserts authenticated readiness. A Docker failure, killed
    guardian or uncertain child leaves the original claim intact and poisoned.
    """
    if not 1 <= timeout <= 3600:
        raise ValueError("NEW start timeout is invalid")
    if private_root is None:
        raise HostObservationUnavailable("NEW original start requires a verified independent private Docker Engine (--private-root)")
    with ExitStack() as stack:
        source = None
        source_recheck: Callable[[], None] | None = None
        from .fresh_private_engine import assert_private_engine_live, hold_private_engine_source

        source = stack.enter_context(hold_private_engine_source(request_file, private_root, outer_socket, expected_container_id=container_id))
        expected_host = f"unix://{private_root / 'socket' / 'docker.sock'}"
        if os.environ.get("DOCKER_HOST") != expected_host:
            raise HostObservationUnavailable("NEW selected Engine socket is not the recorded isolated host")
        inspection_socket: Path | None = None

        def recheck_source() -> None:
            assert source is not None
            assert_private_engine_live(request_file, private_root, outer_socket, source, container_id=container_id, inspection_socket=inspection_socket)

        source_recheck = recheck_source
        fence = stack.enter_context(hold_new_guard_flock(request_file))
        created = _inspect_created_scope(request_file, container_id, image_id)
        if source is not None and created.engine_id != source.private_engine_id:
            raise HostObservationUnavailable("NEW selected Docker Engine differs from held private host source")
        original_docker_fields = _docker_created_fields(container_id)
        if original_docker_fields[9] != {"Name": "no", "MaximumRetryCount": 0} or original_docker_fields[3:9] != ("created", False, 0, "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", 0):
            raise HostObservationUnavailable("NEW Docker created state or restart policy changed before first-start claim")
        request = json.loads(_file(request_file, private=True, limit=65536)[1])
        if not _bootstrap_command_matches(created, request["bootstrap_config"], request_file):
            raise HostObservationUnavailable("NEW Docker command is not the selected Supervisor first start")
        guard = Path(request["guard_root"])
        socket_path = guard / SOCKET
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            old_umask = os.umask(0o077)
            try:
                listener.bind(str(socket_path))
                os.chmod(socket_path, 0o600, follow_symlinks=False)
            finally:
                os.umask(old_umask)
            listener.listen(1)
            claim = _create_host_start_claim(guard, created, private_source=source)
            observed = load_new_first_start(request_file)
            if observed.host_claim_sha256 != claim or (fence.request_id, fence.origin_sha256) != (observed.origin.request_id, observed.origin.record_sha256) or observed.private_source_sha256 != (hashlib.sha256(_canonical(asdict(source))).hexdigest() if source is not None else ""):
                raise HostObservationUnavailable("NEW first-start original was not durably bound")
            current_docker_fields = _docker_created_fields(container_id)
            _assert_docker_policy_unchanged(original_docker_fields, current_docker_fields)
            if current_docker_fields[3:9] != ("created", False, 0, "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", 0) or _engine_id() != created.engine_id:
                raise HostObservationUnavailable("NEW Docker scope changed before first start")
            if created.network_id:
                bootstrap = json.loads(_file(Path(request["bootstrap_config"]), private=True, limit=65536)[1])
                if _dedicated_network_id(bootstrap["new_docker_network"]) != created.network_id:
                    raise HostObservationUnavailable("NEW Docker dedicated network changed before first start")
            if source_recheck is not None:
                source_recheck()
            from .fresh_private_engine_gate import hold_one_start_api_gate

            # The engine socket remains inaccessible to other new clients
            # across the physical start, authenticated host guard, and first
            # child receipt. This is not a durable management API endpoint.
            with hold_one_start_api_gate(private_root, container_id, daemon_net_pid=source.parent_pid, daemon_net_namespace=source.parent_net_namespace) as gate_socket:
                inspection_socket = gate_socket
                os.environ["DOCKER_HOST"] = f"unix://{gate_socket}"
                try:
                    source_recheck()
                    # The source can have been raced just before sealing by
                    # another Docker client that then disconnected. Reprove
                    # never-started state through the now-exclusive API gate.
                    sealed_docker_fields = _docker_created_fields(container_id)
                    _assert_docker_policy_unchanged(original_docker_fields, sealed_docker_fields)
                    if sealed_docker_fields[3:9] != ("created", False, 0, "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", 0):
                        raise HostObservationUnavailable("NEW Docker selected child was already started before its original guarded effect")
                    _start_docker_container_once(container_id, created.engine_id)
                    _serve_one_new_guard(listener, request_file, container_id, image_id, claim, original_docker_fields, timeout=timeout, source_sha256=observed.private_source_sha256, assert_source_live=source_recheck)
                    return _await_observed_first_child(Path(request["state_root"]), observed.origin.request_id, claim, container_id, timeout=timeout)
                finally:
                    os.environ["DOCKER_HOST"] = expected_host
