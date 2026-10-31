"""Bounded observations of installed specification-consumer source files.

These measurements do not attest Python's already-loaded objects or prove that
files remained unchanged between samples. No caller chooses filesystem paths.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

_FILES = (
    "requirement_contract.py",
    "specification_analyzer.py",
    "specification_validation_lifecycle.py",
    "issue_review_service.py",
    "prompts.yaml",
)
_PACKAGE = Path(__file__).resolve().parent
_MAX_FILE_BYTES = 16 * 1024 * 1024


def _measure() -> dict[str, str] | None:
    hashes: dict[str, str] = {}
    try:
        for name in _FILES:
            path = _PACKAGE / name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_FILE_BYTES:
                    return None
                digest = hashlib.sha256()
                total = 0
                while chunk := stream.read(64 * 1024):
                    total += len(chunk)
                    if total > _MAX_FILE_BYTES:
                        return None
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
                named = path.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return None
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns, named.st_ctime_ns):
                return None
            hashes[name] = digest.hexdigest()
    except OSError:
        return None
    return hashes


class ConsumerIdentity:
    """Retain a bridge-start sample, then compare two current samples."""

    def __init__(self) -> None:
        first = _measure()
        second = _measure()
        self.initial = first if first is not None and first == second else None

    def observe(self) -> dict[str, object]:
        first = _measure()
        second = _measure()
        if self.initial is None or first is None or second is None:
            return {"status": "UNAVAILABLE", "reason": "file_measurement_unavailable"}
        if first != second or first != self.initial:
            return {"status": "CONFLICT", "reason": "file_samples_differ"}
        return {
            "status": "OK",
            "origin": "worker_package_disk_sha256",
            "semantics": "Bridge-start and two collection samples agree; intervening changes and already-loaded Python code are not attested. prompts.yaml is the package default, not proof of the prompt used by a particular operation.",
            "sha256": first,
        }
