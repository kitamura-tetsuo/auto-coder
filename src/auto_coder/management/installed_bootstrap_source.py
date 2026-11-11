"""Independently compare one reviewed present-time bootstrap image source.

This is an ordinary-v1 source component, not historical execution provenance,
route closure, a kernel pin, or permission to replace a prior deployment.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from dataclasses import dataclass

from .containment_enrollment import EnrollmentObservation

# The deployed ordinary bootstrap image source was reviewed for the exclusive
# .supervisor-owner.lock acquisition before _run_owned on this exact source.
# No different source is promoted to an accepted profile by operator JSON.
_REVIEWED_ORDINARY_SOURCE_SHA256 = "66bcfefa86aa2bd88dd4a253405fc6faac65675917e8d393e587d5a62fff83bb"
_SOURCE_IN_IMAGE = "/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder/management/bootstrap_supervisor.py"
_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class InstalledBootstrapSource:
    """Current exact image-file readback; no original-lifetime assertion."""

    management_id: str
    management_image_id: str
    source_path_in_container: str
    source_sha256: str
    source_bytes: int
    reviewed_lock_before_launch: bool


def _copy_source(container_id: str) -> bytes:
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise ValueError("remote Docker endpoint is unsupported for installed source readback")
    result = subprocess.run(("docker", "cp", container_id + ":" + _SOURCE_IN_IMAGE, "-"), capture_output=True, check=True, timeout=15)
    if len(result.stdout) > _MAX_ARCHIVE_BYTES:
        raise ValueError("installed bootstrap source archive exceeds size limit")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 1 or members[0].name != "bootstrap_supervisor.py" or not members[0].isfile() or not 0 < members[0].size <= _MAX_SOURCE_BYTES:
            raise ValueError("installed bootstrap source archive is ambiguous")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError("installed bootstrap source is unavailable")
        source = stream.read(_MAX_SOURCE_BYTES + 1)
    if len(source) > _MAX_SOURCE_BYTES or len(source) != members[0].size:
        raise ValueError("installed bootstrap source length differs")
    return source


def inspect_installed_bootstrap_source(record: EnrollmentObservation) -> InstalledBootstrapSource:
    """Reread Docker's selected original container file without changing it."""
    enrollment = record.enrollment
    if enrollment.profile != "ordinary-v1":
        raise ValueError("installed bootstrap source is not reviewed for this profile")
    approved_files = {file.purpose: file.sha256 for file in enrollment.original_files}
    readback = dict(record.original_file_sha256)
    if approved_files.get("bootstrap-source") != _REVIEWED_ORDINARY_SOURCE_SHA256 or readback.get("bootstrap-source") != _REVIEWED_ORDINARY_SOURCE_SHA256:
        raise ValueError("installed bootstrap source differs from reviewed ordinary-v1 bytes")
    first = _copy_source(enrollment.management_id)
    second = _copy_source(enrollment.management_id)
    if first != second or hashlib.sha256(first).hexdigest() != _REVIEWED_ORDINARY_SOURCE_SHA256:
        raise ValueError("installed bootstrap source differs from the pinned reviewed bytes")
    return InstalledBootstrapSource(enrollment.management_id, enrollment.management_image_id, _SOURCE_IN_IMAGE, _REVIEWED_ORDINARY_SOURCE_SHA256, len(first), True)
