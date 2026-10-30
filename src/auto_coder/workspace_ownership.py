"""Producer-created, replacement-resistant workspace ownership evidence."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

MARKER = ".auto-coder-owner-v1"


def create_ownership(root: Path, target: Path, job_id: str, incarnation: str, *, binding_target: Path | None = None) -> str:
    """Create an exclusive marker and external hard-link anchor for a new root."""
    key = hashlib.sha256(json.dumps([job_id, incarnation], separators=(",", ":")).encode()).hexdigest()
    anchors = root / ".ownership-v1"
    anchors.mkdir(mode=0o700, exist_ok=True)
    if anchors.is_symlink() or anchors.resolve().parent != root.resolve():
        raise OSError("invalid workspace ownership anchor directory")
    anchor = anchors / key
    marker = target / MARKER
    payload = json.dumps([1, job_id, incarnation, str(binding_target or target)], separators=(",", ":")).encode()
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    linked = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(marker, anchor, follow_symlinks=False)
        linked = True
        for directory in (target, anchors):
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        marker.unlink(missing_ok=True)
        if linked:
            anchor.unlink(missing_ok=True)
        raise
    return str(anchor)


def create_owned_workspace(root: Path, target: Path, job_id: str, incarnation: str) -> str:
    """Publish an already-marked new directory without replacing an existing path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".workspace-staging-", dir=target.parent))
    anchor = ""
    try:
        anchor = create_ownership(root, staging, job_id, incarnation, binding_target=target)
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "atomic no-replace rename unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(target), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
        if not verify_ownership(target, anchor, job_id, incarnation):
            raise OSError("workspace ownership changed during publication")
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return anchor
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        if anchor:
            Path(anchor).unlink(missing_ok=True)
        raise


def verify_ownership(target: Path, anchor: str, job_id: str, incarnation: str) -> bool:
    """Require the original marker inode, its external anchor, and exact binding."""
    try:
        marker = target / MARKER
        anchor_path = Path(anchor)
        a, b = marker.stat(follow_symlinks=False), anchor_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(a.st_mode) or not stat.S_ISREG(b.st_mode) or (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            return False
        expected = [1, job_id, incarnation, str(target)]
        return json.loads(marker.read_text(encoding="utf-8")) == expected
    except (OSError, ValueError, TypeError):
        return False
