"""Durable NEW-origin intent only; no first-launch, grant or recovery authority."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .fresh_preflight import (
    NewInstanceAssessment,
    NewInstanceRequest,
    _application,
    _file,
    _identity,
    _open,
    _root,
    _unique,
    assess_new_instance,
    revalidate_new_instance,
)

RECORD = "new-instance-origin.json"


@dataclass(frozen=True)
class NewOriginReceipt:
    request_id: str
    binding_sha256: str
    record_sha256: str
    record_path: str
    status: str = "origin-recorded-only"
    launch_authorized: bool = False


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _lifetime() -> dict[str, object]:
    # Observed Linux producer lifetime, not a claim of launch or host ownership.
    start = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]
    if not start.isdecimal() or int(start) <= 0:
        raise ValueError("original producer start token unavailable")
    return {
        "pid": os.getpid(),
        "start_token": start,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def _verify_origin_lifetime(value: object) -> None:
    """Check the retained Linux origin fact's shape, not current process liveness."""
    if not isinstance(value, dict) or set(value) != {"pid", "start_token", "boot_id"}:
        raise ValueError("original producer lifetime fields differ")
    pid = value["pid"]
    token = value["start_token"]
    boot = value["boot_id"]
    if type(pid) is not int or pid <= 0:
        raise ValueError("original producer PID is invalid")
    if not isinstance(token, str) or not 0 < len(token) <= 32 or not token.isascii() or not token.isdecimal() or int(token) <= 0 or token != str(int(token)):
        raise ValueError("original producer start token is invalid")
    if not isinstance(boot, str):
        raise ValueError("original producer boot ID is invalid")
    try:
        canonical_boot = str(uuid.UUID(boot))
    except ValueError as error:
        raise ValueError("original producer boot ID is invalid") from error
    if boot != canonical_boot:
        raise ValueError("original producer boot ID is not canonical")


def _root_binding_matches(assessment: NewInstanceAssessment, fd: int) -> None:
    observed = _identity(Path(assessment.request.guard_root), fd)
    expected = assessment.roots[2]
    if (
        observed.device,
        observed.inode,
        observed.uid,
        observed.mode,
    ) != (
        expected.device,
        expected.inode,
        expected.uid,
        expected.mode,
    ):
        raise ValueError("new guard root identity changed")
    if observed.uid != os.geteuid() or observed.mode != 0o700:
        raise PermissionError("new guard root is not protected")
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        raise ValueError("new guard root is not a directory")


def begin_new_instance(request_file: Path) -> NewOriginReceipt:
    """Persist one first-origin fact; interrupted records poison retries.

    This never creates state stores, consumes a first-launch claim or launches a
    process. It cannot be treated as a completed initialization or an OAuth
    authorization. Existing OAuth bytes are never opened by this producer.
    """
    assessment = assess_new_instance(request_file)
    revalidate_new_instance(assessment)
    guard = Path(assessment.request.guard_root)
    value = {
        "schema_version": 1,
        "kind": "new-instance-origin-only",
        "assessment": asdict(assessment),
        "producer_lifetime": _lifetime(),
    }
    encoded = _canonical(value)
    with _open(guard, directory=True) as directory:
        _root_binding_matches(assessment, directory)
        if os.listdir(directory):
            raise FileExistsError("new origin already begun or interrupted")
        # O_EXCL is the actual one-use origin arbitration point. Never unlink
        # a partial record after crash or I/O failure to obtain another try.
        descriptor = os.open(
            RECORD,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        _root_binding_matches(assessment, directory)
        if os.listdir(directory) != [RECORD]:
            raise ValueError("new origin guard changed during write")
        recorded, actual = _file(guard / RECORD, private=True, limit=1024 * 1024)
        if actual != encoded or recorded.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("new original intent changed during publication")
        if _root(guard, private=True, empty=False).inode != assessment.roots[2].inode:
            raise ValueError("new guard root replaced after publication")
    return NewOriginReceipt(
        request_id=assessment.request.request_id,
        binding_sha256=assessment.binding_sha256,
        record_sha256=hashlib.sha256(encoded).hexdigest(),
        record_path=str(guard / RECORD),
    )


def inspect_new_instance_origin(request_file: Path) -> NewOriginReceipt:
    """Independently re-read original files; no assertion of current launch eligibility."""
    # No restoration, rewriting, lock acquisition or silent conversion of a
    # partial record is allowed. Inspection is a separate read-only process.
    original, request_bytes = _file(request_file, private=True, limit=65536)
    current_request = json.loads(request_bytes, object_pairs_hook=_unique)
    if not isinstance(current_request, dict) or set(current_request) != set(NewInstanceRequest.__dataclass_fields__):
        raise ValueError("invalid original request")
    guard_path = Path(current_request["guard_root"])
    with _open(guard_path, directory=True) as directory:
        if os.listdir(directory) != [RECORD]:
            raise ValueError("new origin is missing, partial or conflicting")
        snapshot, encoded = _file(guard_path / RECORD, private=True, limit=1024 * 1024)
        value = json.loads(encoded)
        if not isinstance(value, dict) or set(value) != {"schema_version", "kind", "assessment", "producer_lifetime"} or value["schema_version"] != 1 or value["kind"] != "new-instance-origin-only" or _canonical(value) != encoded:
            raise ValueError("new origin record is not canonical")
        _verify_origin_lifetime(value["producer_lifetime"])
        saved = value["assessment"]
        if not isinstance(saved, dict) or set(saved) != set(NewInstanceAssessment.__dataclass_fields__) or saved["request"] != current_request or saved["original_request"] != asdict(original) or saved["status"] != "assessed-only" or saved["launch_authorized"] is not False:
            raise ValueError("original NEW assessment differs")
        roots = saved["roots"]
        if not isinstance(roots, list) or len(roots) != 4:
            raise ValueError("original NEW root scope differs")
        for index, path in enumerate((current_request["state_root"], current_request["operation_root"], current_request["guard_root"], current_request["oauth_root"])):
            now = _root(Path(path), private=index < 3, empty=index < 2)
            expected = roots[index]
            if index == 2:
                # Guard mtime/ctime necessarily change with record creation.
                for field in ("path", "device", "inode", "uid", "mode"):
                    if getattr(now, field) != expected[field]:
                        raise ValueError("original guard identity replaced")
                _root_binding_matches_for_record(expected, directory)
            elif asdict(now) != expected:
                raise ValueError("original NEW root identity changed")
        inputs = [_file(Path(current_request[field]), private=index < 2)[0] for index, field in enumerate(("bootstrap_config", "management_config", "executable"))]
        if [asdict(item) for item in inputs] != saved["inputs"]:
            raise ValueError("original selected inputs changed")
        if [asdict(item) for item in _application(Path(current_request["application_root"]))] != saved["application"]:
            raise ValueError("original application identity changed")
        bound = {
            "request": saved["request"],
            "original_request": saved["original_request"],
            "roots": saved["roots"],
            "inputs": saved["inputs"],
            "application": saved["application"],
        }
        digest = hashlib.sha256(_canonical(bound)).hexdigest()
        if digest != saved["binding_sha256"]:
            raise ValueError("original binding digest differs")
        if _file(guard_path / RECORD, private=True, limit=1024 * 1024)[0] != snapshot:
            raise ValueError("original record changed during independent observation")
    return NewOriginReceipt(
        request_id=current_request["request_id"],
        binding_sha256=digest,
        record_sha256=hashlib.sha256(encoded).hexdigest(),
        record_path=str(guard_path / RECORD),
    )


def _root_binding_matches_for_record(saved: dict[str, object], descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) != (saved["device"], saved["inode"], saved["uid"], saved["mode"]) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("new origin guard descriptor changed")
