"""Explicit, non-reusable provenance for a new management instance.

This record alone is not evidence of old-process absence or launch authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import secrets
import socket
import stat
import struct
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FreshInitializationIntent:
    request_id: str
    old_instance: str
    old_container_id: str
    new_instance: str
    new_container_id: str
    prior_evidence_sha256: str
    owner_lock_device: int
    owner_lock_inode: int
    host_listener_port: int
    bootstrap_config_sha256: str
    management_config_sha256: str
    image_digest: str
    executable_sha256: str
    application_sha256: str
    runtime_reference: str
    state_root: str
    journal_root: str
    oauth_root: str
    host_state_root: str
    host_journal_root: str
    host_oauth_root: str
    state_device: int
    state_inode: int
    journal_device: int
    journal_inode: int
    oauth_device: int
    oauth_inode: int
    guard_device: int
    guard_inode: int

    def validate(self) -> None:
        if str(uuid.UUID(self.request_id)) != self.request_id:
            raise ValueError("invalid initialization request")
        if not self.old_instance or not self.new_instance or self.old_instance == self.new_instance or not self.runtime_reference:
            raise ValueError("fresh instance and runtime identity required")
        for value in (self.old_instance, self.new_instance, self.runtime_reference):
            if len(value) > 128 or "\x00" in value:
                raise ValueError("unbounded initialization identity")
        if type(self.owner_lock_device) is not int or self.owner_lock_device < 0 or type(self.owner_lock_inode) is not int or self.owner_lock_inode <= 0:
            raise ValueError("invalid prior owner fence identity")
        if type(self.host_listener_port) is not int or not 0 < self.host_listener_port <= 65535:
            raise ValueError("invalid host listener")
        for value in (self.old_container_id, self.new_container_id, self.prior_evidence_sha256, self.bootstrap_config_sha256, self.management_config_sha256, self.executable_sha256, self.application_sha256):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("invalid initialization digest")
        if self.old_container_id == self.new_container_id:
            raise ValueError("fresh container must be distinct")
        if not self.image_digest.startswith("sha256:") or len(self.image_digest) != 71 or any(c not in "0123456789abcdef" for c in self.image_digest[7:]):
            raise ValueError("invalid immutable image digest")
        roots = tuple(Path(value) for value in (self.state_root, self.journal_root, self.oauth_root))
        if any(not root.is_absolute() or root != root.resolve() for root in roots) or len(set(roots)) != 3:
            raise ValueError("distinct canonical state roots required")
        if any(a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i + 1 :]):
            raise ValueError("state roots must not overlap")
        host_roots = tuple(Path(value) for value in (self.host_state_root, self.host_journal_root, self.host_oauth_root))
        if any(not root.is_absolute() or root != root.resolve() for root in host_roots) or len(set(host_roots)) != 3:
            raise ValueError("distinct canonical host roots required")
        if any(a in b.parents or b in a.parents for i, a in enumerate(host_roots) for b in host_roots[i + 1 :]):
            raise ValueError("host state roots must not overlap")
        for device, inode in ((self.state_device, self.state_inode), (self.journal_device, self.journal_inode), (self.oauth_device, self.oauth_inode), (self.guard_device, self.guard_inode)):
            if type(device) is not int or device < 0 or type(inode) is not int or inode <= 0:
                raise ValueError("invalid mounted root identity")


def claim_fresh_initialization(root: Path, intent: FreshInitializationIntent) -> str:
    """Claim an empty private directory once, leaving partial claims intact."""
    intent.validate()
    if root != Path(intent.host_state_root) or root.is_symlink():
        raise ValueError("initialization state root differs")
    encoded = json.dumps(asdict(intent), sort_keys=True, separators=(",", ":")).encode("utf-8")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    journal = -1
    try:
        info = os.fstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700 or (info.st_dev, info.st_ino) != (intent.state_device, intent.state_inode):
            raise PermissionError("initialization root must be owner-only")
        if os.listdir(directory):
            raise FileExistsError("initialization state is not empty")
        journal = os.open(intent.host_journal_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        journal_info = os.fstat(journal)
        if not stat.S_ISDIR(journal_info.st_mode) or journal_info.st_uid != os.geteuid() or stat.S_IMODE(journal_info.st_mode) != 0o700 or (journal_info.st_dev, journal_info.st_ino) != (intent.journal_device, intent.journal_inode):
            raise PermissionError("initialization journal root must be owner-only")
        if os.listdir(journal):
            raise FileExistsError("initialization journal is not empty")
        oauth = os.open(intent.host_oauth_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            oauth_info = os.fstat(oauth)
            if (oauth_info.st_dev, oauth_info.st_ino) != (intent.oauth_device, intent.oauth_inode):
                raise ValueError("retained OAuth root identity differs")
        finally:
            os.close(oauth)
        descriptor = os.open("fresh-initialization.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        if journal >= 0:
            os.close(journal)
        os.close(directory)
    return hashlib.sha256(encoded).hexdigest()


def acquire_prior_owner_fence(path: Path, *, expected_device: int, expected_inode: int) -> int:
    """Hold a shared lock that excludes the prior supervisor's exclusive lock.

    The caller owns the returned descriptor and must keep it open. This fence
    is not itself proof that the old child, container, or listener is absent.
    """
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("prior owner fence must be a fixed regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (expected_device, expected_inode):
            raise ValueError("prior owner fence identity differs")
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def application_tree_sha256(root: Path) -> str:
    """Hash every file in the selected immutable application closure."""
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("application closure is unavailable")
    digest = hashlib.sha256()

    def unavailable(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(root, followlinks=False, onerror=unavailable):
        directories.sort()
        files.sort()
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise ValueError("application closure contains a symlink")
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("application closure contains an unsupported entry")
            relative = path.relative_to(root).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def verify_initialization_inputs(intent: FreshInitializationIntent, *, bootstrap_config: Path, management_config: Path, executable: Path, application_root: Path) -> None:
    """Check bytes rather than trusting digests carried in the intent."""
    intent.validate()
    files = ((bootstrap_config, intent.bootstrap_config_sha256), (management_config, intent.management_config_sha256), (executable, intent.executable_sha256))
    for path, expected in files:
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("initialization input differs from intent")
    if application_tree_sha256(application_root) != intent.application_sha256:
        raise ValueError("application closure differs from intent")


def request_fresh_host_grant(socket_path: Path, *, request_id: str, intent_sha256: str, owner_incarnation: str, new_container_id: str, trusted_uid: int | None = None) -> dict[str, str]:
    """Ask the one-shot host guard for proof bound to this owner and request.

    The caller must still hold the old fence and durably claim the first launch.
    """
    if trusted_uid is None:
        trusted_uid = os.geteuid()
    if str(uuid.UUID(request_id)) != request_id or str(uuid.UUID(owner_incarnation)) != owner_incarnation:
        raise ValueError("invalid fresh grant identity")
    for value in (intent_sha256, new_container_id):
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid fresh grant binding")
    info = socket_path.lstat()
    if not socket_path.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != trusted_uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("fresh host guard socket is not protected")
    request = {"request_id": request_id, "intent_sha256": intent_sha256, "owner_incarnation": owner_incarnation, "new_container_id": new_container_id, "challenge": secrets.token_hex(32)}
    data = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(str(socket_path))
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if uid != trusted_uid:
            raise PermissionError("fresh host guard peer differs")
        connection.sendall(data)
        with connection.makefile("rb") as stream:
            line = stream.readline(4097)
        if len(line) > 4096 or not line.endswith(b"\n"):
            raise ValueError("fresh host grant is incomplete")
    response = json.loads(line)
    if response != {**request, "granted": "host-evidence-verified"}:
        raise ValueError("fresh host grant differs from the current challenge")
    return request


def load_fresh_initialization(state_root: Path, *, host: bool = False) -> tuple[FreshInitializationIntent, str]:
    """Read the original protected immutable intent from the mounted new root."""
    directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("fresh state root is not owner-only")
        descriptor = os.open("fresh-initialization.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PermissionError("fresh intent is not protected")
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise ValueError("fresh intent is too large")
        raw = json.loads(encoded)
        if not isinstance(raw, dict) or set(raw) != set(FreshInitializationIntent.__dataclass_fields__):
            raise ValueError("fresh intent fields differ")
        intent = FreshInitializationIntent(**raw)
        intent.validate()
        root_info = os.fstat(directory)
        expected_root = intent.host_state_root if host else intent.state_root
        if state_root != Path(expected_root) or (root_info.st_dev, root_info.st_ino) != (intent.state_device, intent.state_inode) or encoded != json.dumps(asdict(intent), sort_keys=True, separators=(",", ":")).encode():
            raise ValueError("fresh intent or mounted root differs")
        return intent, hashlib.sha256(encoded).hexdigest()
    finally:
        os.close(directory)


def request_and_claim_first_launch(state_root: Path, socket_path: Path, *, owner_incarnation: str, trusted_uid: int | None = None) -> FreshInitializationIntent:
    """Consume a fresh host grant into one durable, non-repeatable launch claim."""
    intent, digest = load_fresh_initialization(state_root)
    claim = state_root / "fresh-launch-claimed.json"
    if claim.exists() or claim.is_symlink():
        raise FileExistsError("fresh launch was already claimed or is unresolved")
    request = request_fresh_host_grant(socket_path, request_id=intent.request_id, intent_sha256=digest, owner_incarnation=owner_incarnation, new_container_id=intent.new_container_id, trusted_uid=trusted_uid)
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open("fresh-launch-claimed.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return intent


def _write_once(state_root: Path, name: str, value: dict[str, object]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("fresh state root is not protected")
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def record_fresh_launch(state_root: Path, intent: FreshInitializationIntent, *, incarnation: str, child_identity: str, verification_timeout: float) -> dict[str, object]:
    """Record an observed child and a fresh, bounded application challenge."""
    loaded, digest = load_fresh_initialization(state_root)
    if loaded != intent or str(uuid.UUID(incarnation)) != incarnation or not child_identity or len(child_identity) > 512 or not 0 < verification_timeout <= 3600:
        raise ValueError("fresh launch identity differs")
    claim = _read_record(state_root, "fresh-launch-claimed.json")
    _validate_fresh_claim(claim, intent, digest)
    if claim.get("owner_incarnation") != incarnation:
        raise ValueError("fresh launch claim differs")
    now = time.time()
    observation: dict[str, object] = {"request_id": intent.request_id, "owner_incarnation": incarnation, "child_identity": child_identity, "observation_id": secrets.token_hex(16), "started": now, "deadline": now + verification_timeout}
    _write_once(state_root, "fresh-launched.json", observation)
    return observation


def _read_record(state_root: Path, name: str) -> dict[str, object]:
    directory = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("fresh state root is not protected")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise PermissionError("fresh record is not protected")
            encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise ValueError("fresh record is too large")
        value = json.loads(encoded)
        if not isinstance(value, dict):
            raise ValueError("fresh record fields differ")
        return value
    finally:
        os.close(directory)


def record_fresh_outcome(state_root: Path, observation: dict[str, object], *, status: str) -> None:
    if status not in {"succeeded", "unknown"} or _read_record(state_root, "fresh-launched.json") != observation:
        raise ValueError("fresh outcome does not match launch")
    _write_once(state_root, "fresh-outcome.json", {"status": status, "observation_id": observation.get("observation_id"), "request_id": observation.get("request_id")})


def _validate_fresh_claim(claim: dict[str, object], intent: FreshInitializationIntent, digest: str) -> None:
    incarnation, challenge = claim.get("owner_incarnation"), claim.get("challenge")
    if (
        set(claim) != {"request_id", "intent_sha256", "owner_incarnation", "new_container_id", "challenge"}
        or claim.get("request_id") != intent.request_id
        or claim.get("intent_sha256") != digest
        or claim.get("new_container_id") != intent.new_container_id
        or not isinstance(incarnation, str)
        or str(uuid.UUID(incarnation)) != incarnation
        or not isinstance(challenge, str)
        or len(challenge) != 64
        or any(c not in "0123456789abcdef" for c in challenge)
    ):
        raise ValueError("fresh claim is inconsistent")


def read_fresh_status(state_root: Path) -> dict[str, object]:
    """Read local initialization state without creating or repairing files."""
    intent, digest = load_fresh_initialization(state_root)
    claim_path = state_root / "fresh-launch-claimed.json"
    launch_path = state_root / "fresh-launched.json"
    outcome_path = state_root / "fresh-outcome.json"
    if not claim_path.exists() and not claim_path.is_symlink():
        if launch_path.exists() or launch_path.is_symlink() or outcome_path.exists() or outcome_path.is_symlink():
            raise ValueError("fresh status is inconsistent")
        ledger = state_root / "management-child.sqlite3"
        return {"request_id": intent.request_id, "status": "unknown" if ledger.exists() or ledger.is_symlink() else "pending"}
    claim = _read_record(state_root, claim_path.name)
    _validate_fresh_claim(claim, intent, digest)
    if not launch_path.exists() and not launch_path.is_symlink():
        if outcome_path.exists() or outcome_path.is_symlink():
            raise ValueError("fresh outcome has no launch")
        return {"request_id": intent.request_id, "status": "unknown"}
    launch = _read_record(state_root, launch_path.name)
    if set(launch) != {"request_id", "owner_incarnation", "child_identity", "observation_id", "started", "deadline"} or launch.get("request_id") != intent.request_id or launch.get("owner_incarnation") != claim.get("owner_incarnation"):
        raise ValueError("fresh launch is inconsistent")
    identity, observation_id, started, deadline = launch.get("child_identity"), launch.get("observation_id"), launch.get("started"), launch.get("deadline")
    if (
        not isinstance(identity, str)
        or not identity
        or len(identity) > 512
        or "\x00" in identity
        or not isinstance(observation_id, str)
        or len(observation_id) != 32
        or any(c not in "0123456789abcdef" for c in observation_id)
        or not isinstance(started, (int, float))
        or isinstance(started, bool)
        or not math.isfinite(started)
        or not isinstance(deadline, (int, float))
        or isinstance(deadline, bool)
        or not math.isfinite(deadline)
        or not 0 < deadline - started <= 3600
    ):
        raise ValueError("fresh observation is invalid")
    if not outcome_path.exists() and not outcome_path.is_symlink():
        return {**launch, "status": "launched"}
    outcome = _read_record(state_root, outcome_path.name)
    if set(outcome) != {"status", "observation_id", "request_id"} or outcome.get("status") not in {"succeeded", "unknown"} or outcome.get("request_id") != intent.request_id or outcome.get("observation_id") != launch.get("observation_id"):
        raise ValueError("fresh outcome is inconsistent")
    return {**launch, "status": outcome["status"]}
