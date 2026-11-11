"""Read a separately installed, root-owned containment enrollment.

An enrollment is present-time authority for *selecting* an exact deployment.
It is not historical provenance, route inhibition, kernel containment, or a
launch grant. Only the trusted host operator may install this record; the
observer never creates, repairs, updates, or enrolls an installation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .archived_exclusions import unique_pairs
from .fresh_host import _evidence
from .fresh_host_observation import _validate_host_proc_profile

ENROLLMENT_ROOT = Path("/var/lib/auto-coder/approved-containment-enrollments")
_MAX_BYTES = 65536
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HASH = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")


class EnrollmentUnavailable(ValueError):
    """No independently installed and inspectable enrollment is available."""


@dataclass(frozen=True)
class OriginalFile:
    """An original, separately reviewed installed source/configuration identity."""

    purpose: str
    path: str
    sha256: str
    device: int
    inode: int

    @classmethod
    def parse(cls, value: object) -> OriginalFile:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise EnrollmentUnavailable("approved original-file schema differs")
        if value["purpose"] not in ("bootstrap-source", "bootstrap-config", "management-config", "watcher-source", "watcher-config", "proxy-config", "support-source"):
            raise EnrollmentUnavailable("unsupported approved original-file purpose")
        path = value["path"]
        if not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute():
            raise EnrollmentUnavailable("approved original-file path differs")
        if not isinstance(value["sha256"], str) or not _HASH.fullmatch(value["sha256"]):
            raise EnrollmentUnavailable("approved original-file digest differs")
        if any(type(value[name]) is not int or value[name] <= 0 for name in ("device", "inode")):
            raise EnrollmentUnavailable("approved original-file identity differs")
        return cls(value["purpose"], path, value["sha256"], value["device"], value["inode"])


@dataclass(frozen=True)
class ApprovedEnrollment:
    schema: str
    deployment_id: str
    enrollment_id: str
    authorization_id: str
    profile: str
    engine_id: str
    management_id: str
    management_image_id: str
    proxy_id: str
    proxy_image_id: str
    original_files: tuple[OriginalFile, ...]
    original_prior_evidence_path: str
    original_prior_evidence_sha256: str
    approved_observation_only: bool

    @classmethod
    def parse(cls, value: object) -> ApprovedEnrollment:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise EnrollmentUnavailable("approved enrollment schema differs")
        if value["schema"] != "approved-current-containment-enrollment-v1" or value["approved_observation_only"] is not True:
            raise EnrollmentUnavailable("unsupported or authorizing enrollment record")
        for key in ("deployment_id", "enrollment_id", "authorization_id"):
            if not isinstance(value[key], str) or not _IDENTIFIER.fullmatch(value[key]):
                raise EnrollmentUnavailable("approved enrollment identity differs")
        if value["profile"] not in ("ordinary-v1", "quarantined-original-v1"):
            raise EnrollmentUnavailable("approved enrollment profile differs")
        if not isinstance(value["engine_id"], str) or not _IDENTIFIER.fullmatch(value["engine_id"]):
            raise EnrollmentUnavailable("approved Docker engine identity differs")
        for key in ("management_id", "proxy_id"):
            if not isinstance(value[key], str) or not _HASH.fullmatch(value[key]):
                raise EnrollmentUnavailable("approved Docker object identity differs")
        if value["management_id"] == value["proxy_id"]:
            raise EnrollmentUnavailable("approved Docker objects are not distinct")
        for key in ("management_image_id", "proxy_image_id"):
            if not isinstance(value[key], str) or not _IMAGE.fullmatch(value[key]):
                raise EnrollmentUnavailable("approved original image identity differs")
        originals = value["original_files"]
        if not isinstance(originals, list) or not 1 <= len(originals) <= 16:
            raise EnrollmentUnavailable("approved original-file set differs")
        files = tuple(OriginalFile.parse(item) for item in originals)
        if len({item.path for item in files}) != len(files) or len({item.purpose for item in files}) != len(files):
            raise EnrollmentUnavailable("duplicate approved original-file binding")
        if tuple(item.purpose for item in files) != tuple(sorted(item.purpose for item in files)):
            raise EnrollmentUnavailable("approved original-file purposes are not sorted")
        if not {"bootstrap-source", "bootstrap-config", "management-config", "proxy-config"} <= {item.purpose for item in files}:
            raise EnrollmentUnavailable("approved original-file binding is incomplete")
        if value["profile"] == "quarantined-original-v1" and "support-source" not in {item.purpose for item in files}:
            raise EnrollmentUnavailable("approved quarantine support-source binding is incomplete")
        evidence_path = value["original_prior_evidence_path"]
        evidence_sha = value["original_prior_evidence_sha256"]
        if not isinstance(evidence_path, str) or not Path(evidence_path).is_absolute() or len(evidence_path) > 4096 or not isinstance(evidence_sha, str) or not _HASH.fullmatch(evidence_sha):
            raise EnrollmentUnavailable("approved original Docker/lifetime provenance differs")
        return cls(
            value["schema"],
            value["deployment_id"],
            value["enrollment_id"],
            value["authorization_id"],
            value["profile"],
            value["engine_id"],
            value["management_id"],
            value["management_image_id"],
            value["proxy_id"],
            value["proxy_image_id"],
            files,
            evidence_path,
            evidence_sha,
            True,
        )


@dataclass(frozen=True)
class EnrollmentObservation:
    """Bounded readback only: this object is never sufficient for containment."""

    enrollment: ApprovedEnrollment
    enrollment_sha256: str
    enrollment_device: int
    enrollment_inode: int
    enrollment_ctime_ns: int
    original_file_sha256: tuple[tuple[str, str], ...]
    original_prior_evidence_sha256: str


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _protected(path: Path, *, uid: int, size_limit: int) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise EnrollmentUnavailable("approved evidence path is not canonical")
    _trusted_ancestors(path.parent, uid=uid)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != uid or stat.S_IMODE(before.st_mode) != 0o600:
            raise EnrollmentUnavailable("approved evidence file is not protected")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(size_limit + 1)
        after = os.fstat(descriptor)
        if len(content) > size_limit:
            raise EnrollmentUnavailable("approved evidence exceeds size bound")

        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(path.lstat()) or path != path.resolve(strict=True):
            raise EnrollmentUnavailable("approved evidence changed during read")
        return content, after
    finally:
        os.close(descriptor)


def _trusted_ancestors(path: Path, *, uid: int) -> None:
    # This also covers alternative roots or a path exchanged between the
    # initial canonical resolution and the final readback.
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise EnrollmentUnavailable("approved evidence ancestor is mutable")


def _approved_directory(root: Path, *, uid: int) -> None:
    if not root.is_absolute() or root != root.resolve(strict=True):
        raise EnrollmentUnavailable("approved enrollment root is not canonical")
    _trusted_ancestors(root.parent, uid=uid)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise EnrollmentUnavailable("approved enrollment root is not private")


def observe_approved_enrollment(proposed: object) -> EnrollmentObservation:
    """Read only the fixed trusted-host enrollment store; no caller override."""
    try:
        if os.geteuid() != 0:
            raise ValueError("trusted initial host root is required")
        for namespace in ("pid", "user", "mnt", "net"):
            if os.readlink("/proc/self/ns/" + namespace) != os.readlink("/proc/1/ns/" + namespace):
                raise ValueError("observer is not in the original host namespaces")
        _validate_host_proc_profile(
            os.readlink("/proc/self/ns/pid"),
            Path("/proc/self/uid_map").read_text(encoding="ascii"),
            Path("/proc/self/mountinfo").read_text(encoding="ascii"),
        )
    except (OSError, ValueError) as exc:
        raise EnrollmentUnavailable("approved enrollment host-root observation is unavailable") from exc
    return _observe_approved_enrollment(proposed, root=ENROLLMENT_ROOT, expected_uid=0)


def _observe_approved_enrollment(proposed: object, *, root: Path, expected_uid: int) -> EnrollmentObservation:
    """Internal reader with a distinct private-root injection for unit fixtures."""
    if os.geteuid() != expected_uid:
        raise EnrollmentUnavailable("approved enrollment requires trusted host owner")
    enrollment_id = getattr(proposed, "enrollment_id", None)
    if not isinstance(enrollment_id, str) or not _IDENTIFIER.fullmatch(enrollment_id):
        raise EnrollmentUnavailable("invalid approved enrollment selector")
    _approved_directory(root, uid=expected_uid)
    enrollment_file = root / (enrollment_id + ".json")
    content, identity = _protected(enrollment_file, uid=expected_uid, size_limit=_MAX_BYTES)
    try:
        enrollment = ApprovedEnrollment.parse(json.loads(content, object_pairs_hook=unique_pairs))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise EnrollmentUnavailable("approved enrollment cannot be decoded") from exc
    for field in ("deployment_id", "enrollment_id", "authorization_id", "profile", "management_id", "proxy_id"):
        if getattr(enrollment, field) != getattr(proposed, field, None):
            raise EnrollmentUnavailable("approved enrollment and proposed deployment differ")
    originals = []
    original_identities = []
    for item in enrollment.original_files:
        original, info = _protected(Path(item.path), uid=expected_uid, size_limit=1024 * 1024)
        digest = hashlib.sha256(original).hexdigest()
        if (info.st_dev, info.st_ino, digest) != (item.device, item.inode, item.sha256):
            raise EnrollmentUnavailable("approved original source/configuration differs: " + item.purpose)
        originals.append((item.purpose, digest))
        original_identities.append(_file_identity(info))
    prior, prior_identity = _protected(Path(enrollment.original_prior_evidence_path), uid=expected_uid, size_limit=_MAX_BYTES)
    prior_digest = hashlib.sha256(prior).hexdigest()
    if prior_digest != enrollment.original_prior_evidence_sha256:
        raise EnrollmentUnavailable("approved original Docker/lifetime evidence differs")
    try:
        management, proxy, decoded_digest = _evidence(Path(enrollment.original_prior_evidence_path))
    except (ValueError, OSError, TypeError) as exc:
        raise EnrollmentUnavailable("approved original Docker/lifetime evidence is unsupported") from exc
    if (
        decoded_digest != prior_digest
        or management.engine_id != enrollment.engine_id
        or proxy.engine_id != enrollment.engine_id
        or management.container_id != enrollment.management_id
        or proxy.container_id != enrollment.proxy_id
        or management.image_id != enrollment.management_image_id
        or proxy.image_id != enrollment.proxy_image_id
        or proxy.network_mode != "container:" + management.container_id
        or proxy.net_namespace != management.net_namespace
        or not management.instance_id
        or not management.bootstrap_sha256
        or management.owner_lock_inode <= 0
    ):
        raise EnrollmentUnavailable("approved original Docker/lifetime identity differs")
    # Recheck approval and originals after the other reads. A caller still
    # needs separate live Docker/controller/kernel closure and owner fences.
    final_enrollment, final_enrollment_identity = _protected(enrollment_file, uid=expected_uid, size_limit=_MAX_BYTES)
    if final_enrollment != content or _file_identity(final_enrollment_identity) != _file_identity(identity):
        raise EnrollmentUnavailable("approved enrollment changed during observation")
    for item, initial_identity in zip(enrollment.original_files, original_identities):
        final_content, final_identity = _protected(Path(item.path), uid=expected_uid, size_limit=1024 * 1024)
        if hashlib.sha256(final_content).hexdigest() != item.sha256 or _file_identity(final_identity) != initial_identity:
            raise EnrollmentUnavailable("approved original source/configuration changed during observation")
    final_prior, final_prior_identity = _protected(Path(enrollment.original_prior_evidence_path), uid=expected_uid, size_limit=_MAX_BYTES)
    if hashlib.sha256(final_prior).hexdigest() != prior_digest or _file_identity(final_prior_identity) != _file_identity(prior_identity):
        raise EnrollmentUnavailable("approved original Docker/lifetime evidence changed during observation")
    return EnrollmentObservation(
        enrollment,
        hashlib.sha256(content).hexdigest(),
        identity.st_dev,
        identity.st_ino,
        identity.st_ctime_ns,
        tuple(originals),
        prior_digest,
    )
