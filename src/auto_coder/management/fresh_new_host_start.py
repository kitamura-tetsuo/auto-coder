"""One trusted-host Docker first start for a genuinely NEW management scope.

The Docker daemon is the independent source of namespace/never-started facts.
Its no-restart, private-PID and network-none profile excludes an earlier
process inside this exact new container. The operator remains responsible for
keeping unrelated host controllers outside this newly reserved identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import stat
import struct
import subprocess
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from .fresh_begin import _canonical, _lifetime, inspect_new_instance_origin
from .fresh_host_observation import HostObservationUnavailable, _assert_pid_flock
from .fresh_new_admission_fence import hold_new_guard_flock
from .fresh_new_docker import CreatedNewDockerSnapshot, _docker_created_fields, _engine_id, _inspect_created_scope
from .fresh_new_first_launch import CHILD_LAUNCHED, HOST_FIRST_START, SOCKET, load_new_first_start
from .fresh_new_profile import inspect_new_instance_profile
from .fresh_preflight import _file, _open


def _create_host_start_claim(guard: Path, created: CreatedNewDockerSnapshot) -> str:
    """One-way transition: a failed Docker start must never erase this file."""
    value = {
        "schema_version": 1,
        "profile": "genuinely-new-docker-v1",
        "created": asdict(created),
        "launch_nonce": secrets.token_hex(32),
        "producer_lifetime": _lifetime(),
    }
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


def _serve_one_new_guard(listener: socket.socket, request_file: Path, container_id: str, image_id: str, host_claim_sha256: str, *, timeout: float) -> None:
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    listener.settimeout(timeout)
    connection, _ = listener.accept()
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
        if payload["request_id"] != start.origin.request_id or payload["intent_sha256"] != start.origin.record_sha256 or payload["new_container_id"] != container_id or canonical_owner != owner or start.host_claim_sha256 != host_claim_sha256 or start.created.image_id != image_id:
            raise HostObservationUnavailable("NEW grant does not bind the original host start")
        fields = _docker_created_fields(container_id)
        if fields[1] != image_id or fields[3:6] != ("running", True, peer_pid) or fields[8] != 0 or _engine_id() != start.created.engine_id:
            raise HostObservationUnavailable("NEW Docker PID-1 running identity differs")
        _assert_new_owner(peer_pid, owner, Path(request["state_root"]), start.profile.instance_id)
        # Recheck the independent engine and kernel owner after ledger reading.
        fields_again = _docker_created_fields(container_id)
        if fields_again != fields or _engine_id() != start.created.engine_id:
            raise HostObservationUnavailable("NEW Docker/owner observation changed")
        _assert_new_owner(peer_pid, owner, Path(request["state_root"]), start.profile.instance_id)
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


def start_new_docker_once(request_file: Path, container_id: str, image_id: str, *, timeout: float = 120) -> dict[str, object]:
    """Consume a first-start claim and invoke Docker start exactly once.

    The result never asserts authenticated readiness. A Docker failure, killed
    guardian or uncertain child leaves the original claim intact and poisoned.
    """
    if not 1 <= timeout <= 3600:
        raise ValueError("NEW start timeout is invalid")
    with hold_new_guard_flock(request_file) as fence:
        created = _inspect_created_scope(request_file, container_id, image_id)
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
            claim = _create_host_start_claim(guard, created)
            observed = load_new_first_start(request_file)
            if observed.host_claim_sha256 != claim or (fence.request_id, fence.origin_sha256) != (observed.origin.request_id, observed.origin.record_sha256):
                raise HostObservationUnavailable("NEW first-start original was not durably bound")
            if _docker_created_fields(container_id)[3:9] != ("created", False, 0, "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z", 0) or _engine_id() != created.engine_id:
                raise HostObservationUnavailable("NEW Docker scope changed before first start")
            subprocess.run(["docker", "start", container_id], check=True, capture_output=True, timeout=20)
            _serve_one_new_guard(listener, request_file, container_id, image_id, claim, timeout=timeout)
            return _await_observed_first_child(Path(request["state_root"]), observed.origin.request_id, claim, container_id, timeout=timeout)
