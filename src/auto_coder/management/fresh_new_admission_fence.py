"""A held, non-authorizing NEW admission serialization boundary.

The fresh origin already binds a protected guard directory. A flock on that
directory avoids creating or replacing an owner-lock file while the new state
root must remain empty. Only a caller that keeps this context open can use
its exclusion; the returned observation is never first-launch authority.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .fresh_begin import inspect_new_instance_origin
from .fresh_new_profile import inspect_new_instance_profile
from .fresh_preflight import _file, _identity, _open


@dataclass(frozen=True)
class NewAdmissionFenceObservation:
    request_id: str
    origin_sha256: str
    guard_device: int
    guard_inode: int
    status: str = "admission-fence-observed-only"
    launch_authorized: bool = False


@contextmanager
def hold_new_guard_flock(request_file: Path) -> Iterator[NewAdmissionFenceObservation]:
    """Hold the original guard inode through a caller-owned effect boundary.

    The caller is responsible for post-effect content/source verification;
    this context provides only held kernel serialization and inode identity.
    """
    request = json.loads(_file(request_file, private=True, limit=65536)[1])
    guard = Path(request["guard_root"])
    with _open(guard, directory=True) as descriptor:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("NEW admission guard is not protected")
        # LOCK_NB makes a real competing admission fail instead of waiting
        # until after its peer has exited and accidentally passing a test.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Arbitrate before reading mutable guard entries: an overlapping
        # competitor must reach the actual kernel admission lock even while
        # the first contender is writing its durable launch claim.
        origin = inspect_new_instance_origin(request_file)
        profile = inspect_new_instance_profile(request_file)
        if profile.origin_sha256 != origin.record_sha256 or profile.request_id != origin.request_id:
            raise ValueError("NEW profile and origin differ")
        if inspect_new_instance_origin(request_file) != origin or inspect_new_instance_profile(request_file) != profile:
            raise ValueError("NEW admission sources changed during arbitration")
        current = _identity(guard, descriptor)
        if (current.device, current.inode, current.uid, current.mode) != (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)):
            raise ValueError("NEW admission guard identity changed")
        with _open(guard, directory=True) as reopened:
            if (os.fstat(reopened).st_dev, os.fstat(reopened).st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("NEW admission guard was detached from its path")
        yield NewAdmissionFenceObservation(origin.request_id, origin.record_sha256, info.st_dev, info.st_ino)
        # The held inode may have been detached from the path while the caller
        # held the lock. This final observation is a refusal, not launch authority.
        with _open(guard, directory=True) as reopened:
            if (os.fstat(reopened).st_dev, os.fstat(reopened).st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("NEW admission guard detached while fence held")
        current = _identity(guard, descriptor)
        if (current.device, current.inode, current.uid, current.mode) != (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)):
            raise ValueError("NEW admission guard changed while fence held")


@contextmanager
def hold_new_admission_fence(request_file: Path) -> Iterator[NewAdmissionFenceObservation]:
    """Serialize an observation only; no launch authority survives this scope."""
    with hold_new_guard_flock(request_file) as observation:
        yield observation
        if inspect_new_instance_origin(request_file).record_sha256 != observation.origin_sha256:
            raise ValueError("NEW origin changed while probe fence was held")
        if inspect_new_instance_profile(request_file).origin_sha256 != observation.origin_sha256:
            raise ValueError("NEW profile changed while probe fence was held")
