"""Read-only NEW-instance assessment, never admission or launch authority.

The trusted local operator supplies intent; no OAuth data or old history is
opened. Observations are bounded snapshots, not reservations or durable claims.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class NewInstanceRequest:
    schema_version: int
    purpose: str
    request_id: str
    new_instance: str
    runtime_reference: str
    state_root: str
    operation_root: str
    guard_root: str
    oauth_root: str
    bootstrap_config: str
    management_config: str
    executable: str
    application_root: str


@dataclass(frozen=True)
class ObservedIdentity:
    path: str
    device: int
    inode: int
    uid: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str = ""


@dataclass(frozen=True)
class NewInstanceAssessment:
    request: NewInstanceRequest
    original_request: ObservedIdentity
    roots: tuple[ObservedIdentity, ...]
    inputs: tuple[ObservedIdentity, ...]
    application: tuple[ObservedIdentity, ...]
    binding_sha256: str
    status: str = "assessed-only"
    launch_authorized: bool = False


def _path(value: str) -> Path:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid path")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("canonical absolute path required")
    return path


@contextmanager
def _open(path: Path, *, directory: bool = False) -> Iterator[int]:
    """Walk held directory descriptors; never follow any symlink component."""
    _path(str(path))
    with ExitStack() as stack:
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, parent)
        for component in path.parts[1:-1]:
            parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, parent)
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        descriptor = os.open(path.name, flags, dir_fd=parent)
        stack.callback(os.close, descriptor)
        yield descriptor


def _identity(path: Path, descriptor: int, digest: str = "") -> ObservedIdentity:
    info = os.fstat(descriptor)
    return ObservedIdentity(str(path), info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns, digest)


def _file(path: Path, *, private: bool, limit: int = 64 * 1024 * 1024) -> tuple[ObservedIdentity, bytes]:
    with _open(path) as descriptor:
        before = _identity(path, descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("regular input file required")
        if private and (before.uid != os.geteuid() or before.mode != 0o600):
            raise PermissionError("operator input must be owned by effective UID with mode 0600")
        if os.fstat(descriptor).st_nlink != 1:
            raise ValueError("input file has aliases")
        if before.size > limit:
            raise ValueError("input file exceeds assessment limit")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit or before != _identity(path, descriptor):
            raise ValueError("input changed during assessment")
        return _identity(path, descriptor, hashlib.sha256(data).hexdigest()), data


def _root(path: Path, *, private: bool, empty: bool) -> ObservedIdentity:
    with _open(path, directory=True) as descriptor:
        before = _identity(path, descriptor)
        if private and (before.uid != os.geteuid() or before.mode != 0o700):
            raise PermissionError("new root must be owned by effective UID with mode 0700")
        if empty and os.listdir(descriptor):
            raise FileExistsError("new root contains existing or partial state")
        if before != _identity(path, descriptor):
            raise ValueError("root changed during assessment")
        return before


def _application(root: Path) -> tuple[ObservedIdentity, ...]:
    entries: list[ObservedIdentity] = []

    def visit(path: Path, depth: int) -> None:
        if depth > 64 or len(entries) >= 4096:
            raise ValueError("application exceeds assessment limit")
        with _open(path, directory=True) as descriptor:
            before = _identity(path, descriptor)
            entries.append(before)
            for name in sorted(os.listdir(descriptor)):
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child = path / name
                if stat.S_ISDIR(info.st_mode):
                    visit(child, depth + 1)
                elif stat.S_ISREG(info.st_mode):
                    if len(entries) >= 4096:
                        raise ValueError("application exceeds assessment limit")
                    entries.append(_file(child, private=False)[0])
                else:
                    raise ValueError("application contains symlink or unsupported entry")
            if before != _identity(path, descriptor):
                raise ValueError("application directory changed during assessment")

    visit(root, 0)
    if not any(entry.sha256 for entry in entries):
        raise ValueError("application is empty")
    return tuple(entries)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate request field")
        value[key] = item
    return value


def assess_new_instance(request_file: Path) -> NewInstanceAssessment:
    """Inspect an explicit request without provisioning, claiming or launching.

    File hashes bind original bytes, including whitespace. The request's runtime
    label is operator intent, not proof of a selected installed runtime.
    """
    original, encoded = _file(request_file, private=True, limit=65536)
    raw = json.loads(encoded, object_pairs_hook=_unique)
    if not isinstance(raw, dict) or set(raw) != set(NewInstanceRequest.__dataclass_fields__):
        raise ValueError("NEW-instance request fields differ")
    request = NewInstanceRequest(**raw)
    if type(request.schema_version) is not int or request.schema_version != 1 or request.purpose != "NEW":
        raise ValueError("explicit schema 1 NEW request required")
    for text in (request.request_id, request.new_instance, request.runtime_reference):
        if not isinstance(text, str) or not text.strip() or len(text) > 128 or "\x00" in text:
            raise ValueError("bounded instance, request and runtime identities required")
    if str(uuid.UUID(request.request_id)) != request.request_id:
        raise ValueError("canonical request UUID required")
    roots = tuple(_path(value) for value in (request.state_root, request.operation_root, request.guard_root, request.oauth_root))
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i + 1 :]):
        raise ValueError("roots must be distinct and non-overlapping")
    inputs = tuple(_path(value) for value in (request.bootstrap_config, request.management_config, request.executable))
    application = _path(request.application_root)
    # New roots must stay empty; retained OAuth contents are never inspected.
    for path in (request_file, *inputs, application):
        if any(path == root or root in path.parents or path in root.parents for root in roots):
            raise ValueError("inputs and roots must not overlap")
    root_identities = tuple(_root(path, private=index < 3, empty=index < 3) for index, path in enumerate(roots))
    if len({(entry.device, entry.inode) for entry in root_identities}) != 4:
        raise ValueError("mounted root identities are aliased")
    input_identities = tuple(_file(path, private=index < 2)[0] for index, path in enumerate(inputs))
    if not input_identities[2].mode & 0o111:
        raise ValueError("selected executable has no executable mode")
    application_identities = _application(application)
    # Re-observe every path to reject replacements during the bounded scan.
    if original != _file(request_file, private=True, limit=65536)[0] or root_identities != tuple(_root(path, private=index < 3, empty=index < 3) for index, path in enumerate(roots)):
        raise ValueError("request or roots changed during assessment")
    if input_identities != tuple(_file(path, private=index < 2)[0] for index, path in enumerate(inputs)) or application_identities != _application(application):
        raise ValueError("input identities changed during assessment")
    binding = {"request": asdict(request), "original_request": asdict(original), "roots": [asdict(entry) for entry in root_identities], "inputs": [asdict(entry) for entry in input_identities], "application": [asdict(entry) for entry in application_identities]}
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return NewInstanceAssessment(request, original, root_identities, input_identities, application_identities, digest)


def revalidate_new_instance(assessment: NewInstanceAssessment) -> None:
    """Compare a fresh observation; this still confers no launch eligibility."""
    if assess_new_instance(Path(assessment.original_request.path)) != assessment:
        raise ValueError("NEW-instance assessment changed; no launch authority")
