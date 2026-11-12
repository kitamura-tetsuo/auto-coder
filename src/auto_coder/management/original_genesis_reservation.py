"""One-time prospective root reservation; deliberately not genesis authority.

This component creates new roots itself. It cannot enroll an existing deployment,
observe Docker, initialize stores, or authorize bootstrap. Retain the returned
binding independently; a record found on disk is not its own trust anchor.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from loguru import logger

from .original_attempts import OriginalEvidenceUnavailable, _lifetime, canonical, identity, read_original, source_identity, validate_original_lifetime

PROFILE = "ordinary-genesis-root-reservation-v1"
MISSING = (
    "exact local engine and created/never-started manager/proxy observations unavailable",
    "ordinary bootstrap genesis, native stores and first-launch originals unavailable",
    "genesis-bound successor frontier unavailable",
)
DIRECTORIES = ("state", "journal", "history")


@dataclass(frozen=True)
class ReservedRoot:
    name: str
    device: int
    inode: int


@dataclass(frozen=True)
class ReservationBinding:
    path: str
    deployment: str
    source: str
    roots: tuple[ReservedRoot, ...]
    lock: tuple[int, int]
    record: tuple[int, int]
    sha256: str


@dataclass(frozen=True)
class ReservationEvidence:
    binding: ReservationBinding
    original: bytes
    # These absent producers are never promoted into a no-launch observation.
    unavailable: tuple[str, ...] = MISSING


def _check_directory(path: Path, descriptor: int) -> tuple[int, int]:
    expected = identity(path, directory=True)
    info = os.fstat(descriptor)
    if (info.st_dev, info.st_ino) != expected:
        raise OriginalEvidenceUnavailable("reservation directory replaced")
    return expected


def _check_lock(root: Path, descriptor: int, expected: tuple[int, int], mode: str) -> None:
    info = os.fstat(descriptor)
    if (info.st_dev, info.st_ino) != expected or identity(root / "reservation.lock") != expected:
        raise OriginalEvidenceUnavailable("reservation lock replaced")
    if read_original(root / "reservation.lock") != (b"", expected):
        raise OriginalEvidenceUnavailable("reservation lock original differs")
    lines = Path(f"/proc/self/fdinfo/{descriptor}").read_text().splitlines()
    if not any(line.startswith("lock:") and f" FLOCK  ADVISORY  {mode} " in line for line in lines):
        raise OriginalEvidenceUnavailable("current reservation fence unavailable")


def _publish(directory: int, name: str, data: bytes) -> tuple[int, int]:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        info = os.fstat(stream.fileno())
    os.fsync(directory)
    return info.st_dev, info.st_ino


def reserve(path: Path, deployment: str) -> ReservationBinding:
    """Create a previously nonexistent namespace, never reuse an empty old root.

    Every failure after mkdir leaves its partial namespace untouched. There is no
    cleanup, retry, relocation, permission repair or resume operation here.
    """
    if not isinstance(deployment, str) or not deployment.strip() or len(deployment) > 128:
        raise OriginalEvidenceUnavailable("explicit deployment scope required")
    if not path.is_absolute() or path != path.resolve() or path.name in {"", ".", ".."}:
        raise OriginalEvidenceUnavailable("canonical new reservation path required")
    parent = path.parent
    identity(parent, directory=True)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_fd = lock_fd = -1
    try:
        _check_directory(parent, parent_fd)
        # mkdir, rather than an emptiness test, is the one-time arbitration.
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        root_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        namespace = _check_directory(path, root_fd)
        lock_id = _publish(root_fd, "reservation.lock", b"")
        lock_fd = os.open("reservation.lock", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_lock(path, lock_fd, lock_id, "WRITE")
        roots = [ReservedRoot("namespace", *namespace)]
        for name in DIRECTORIES:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
            child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                roots.append(ReservedRoot(name, *_check_directory(path / name, child_fd)))
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            os.fsync(root_fd)
        source = source_identity()
        lifetime = _lifetime()
        validate_original_lifetime(lifetime)
        encoded = canonical({"profile": PROFILE, "deployment": deployment, "source": source, "path": str(path), "roots": [asdict(root) for root in roots], "lock": lock_id, "lifetime": lifetime})
        record_id = _publish(root_fd, "reservation.json", encoded)
        binding = ReservationBinding(str(path), deployment, source, tuple(roots), lock_id, record_id, hashlib.sha256(encoded).hexdigest())
        _read(binding, lock_fd, "WRITE")
        _check_directory(parent, parent_fd)
        logger.bind(genesis_reservation=str(path), genesis_boundary="roots-reserved", evidence_only=True).info("Prospective roots reserved; original genesis remains unavailable: {}", path)
        return binding
    finally:
        for descriptor in (lock_fd, root_fd, parent_fd):
            if descriptor >= 0:
                os.close(descriptor)


def _read(binding: ReservationBinding, fence: int, mode: str) -> ReservationEvidence:
    root = Path(binding.path)
    _check_lock(root, fence, binding.lock, mode)
    if tuple(item.name for item in binding.roots) != ("namespace", *DIRECTORIES):
        raise OriginalEvidenceUnavailable("reservation root set differs")
    if len({(item.device, item.inode) for item in binding.roots}) != len(binding.roots):
        raise OriginalEvidenceUnavailable("reservation roots alias")

    def check_roots() -> None:
        for item in binding.roots:
            path = root if item.name == "namespace" else root / item.name
            if identity(path, directory=True) != (item.device, item.inode):
                raise OriginalEvidenceUnavailable("reserved root identity differs")
            names = {entry.name for entry in path.iterdir()}
            expected = {*DIRECTORIES, "reservation.lock", "reservation.json"} if item.name == "namespace" else set()
            if names != expected:
                raise OriginalEvidenceUnavailable("reservation namespace changed or partial")

    check_roots()
    encoded, record_id = read_original(root / "reservation.json")
    if record_id != binding.record or hashlib.sha256(encoded).hexdigest() != binding.sha256:
        raise OriginalEvidenceUnavailable("reservation original differs")
    raw = json.loads(encoded)
    if not isinstance(raw, dict) or set(raw) != {"profile", "deployment", "source", "path", "roots", "lock", "lifetime"}:
        raise OriginalEvidenceUnavailable("unsupported reservation record")
    validate_original_lifetime(raw["lifetime"])
    expected_record = {"profile": PROFILE, "deployment": binding.deployment, "source": binding.source, "path": binding.path, "roots": [asdict(item) for item in binding.roots], "lock": binding.lock, "lifetime": raw["lifetime"]}
    if encoded != canonical(expected_record) or source_identity() != binding.source:
        raise OriginalEvidenceUnavailable("reservation source or scope differs")
    # Inspect original bytes and all names again; an earlier digest is not a fence.
    if read_original(root / "reservation.json") != (encoded, record_id):
        raise OriginalEvidenceUnavailable("reservation original changed during observation")
    check_roots()
    _check_lock(root, fence, binding.lock, mode)
    return ReservationEvidence(binding, encoded)


@contextmanager
def observe(binding: ReservationBinding) -> Iterator[int]:
    """Hold a temporary shared reservation fence, never a history/owner grant."""
    root = Path(binding.path)
    identity(root, directory=True)
    descriptor = os.open(root / "reservation.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OriginalEvidenceUnavailable("reservation observation contested") from error
        _check_lock(root, descriptor, binding.lock, "READ")
        yield descriptor
    finally:
        os.close(descriptor)


def verify(binding: ReservationBinding, fence: int) -> ReservationEvidence:
    """Reopen originals against an independently retained binding; never repair."""
    return _read(binding, fence, "READ")
