"""Bounded original-lifetime kernel checks; not a retained containment pin.

This component independently checks old Docker/cgroup lifetimes, the full host
task namespace scan and the original published listener. A historical namespace
inode with no live pinned namespace object is *never* a positive closed-profile
authority, even when these current sub-observations agree.
"""

from __future__ import annotations

import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .containment_enrollment import EnrollmentObservation, _file_identity, _protected
from .fresh_host import _evidence
from .fresh_host_observation import (
    HostObservationUnavailable,
    PriorContainer,
    _inspect,
    assert_host_listener_absent,
    assert_known_prior_stack_stopped,
    assert_prior_network_unreferenced,
)


@dataclass(frozen=True)
class KernelSubObservation:
    """Current observations that remain insufficient without an original pin."""

    engine_id: str
    management_id: str
    proxy_id: str
    management_pid: int
    management_start_token: str
    proxy_pid: int
    proxy_start_token: str
    prior_network_namespace: str
    owner_lock_device: int
    owner_lock_inode: int
    published_port: int
    boot_id: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    original_cgroups_empty: bool
    unpinned_network_task_scan_absent: bool
    host_listener_absent: bool
    retained_original_network_pin: bool = False


def _owner_lock(prior: PriorContainer) -> tuple[int, int, int, int]:
    """Reopen the exact owner lock by its independently inspected state mount."""
    raw = _inspect(prior.container_id)
    mounts = raw[11]
    if not isinstance(mounts, list):
        raise HostObservationUnavailable("original manager state mounts are unavailable")
    sources = [item.get("Source") for item in mounts if isinstance(item, dict) and item.get("Type") == "bind" and item.get("Destination") == "/management-child-state"]
    if len(sources) != 1 or not isinstance(sources[0], str):
        raise HostObservationUnavailable("exact original owner file mount is unavailable")
    path = Path(sources[0]) / ".supervisor-owner.lock"
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise HostObservationUnavailable("original owner file path is not canonical")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (prior.owner_lock_device, prior.owner_lock_inode):
            raise HostObservationUnavailable("original owner file identity changed")
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns
    finally:
        os.close(fd)


def observe_kernel_subcomponent(enrolled: EnrollmentObservation) -> KernelSubObservation:
    """Inspect original-current facts without treating historical netns IDs as pins."""
    approval = enrolled.enrollment
    if os.geteuid() != 0 or os.readlink("/proc/self/ns/pid") != os.readlink("/proc/1/ns/pid"):
        raise HostObservationUnavailable("host-root original-lifetime observer is required")
    prior_path = Path(approval.original_prior_evidence_path)
    original, before = _protected(prior_path, uid=0, size_limit=65536)
    management, proxy, prior_digest = _evidence(prior_path)
    if prior_digest != approval.original_prior_evidence_sha256 or prior_digest != enrolled.original_prior_evidence_sha256:
        raise HostObservationUnavailable("approved original lifetime evidence differs")
    if (management.container_id, proxy.container_id, management.engine_id, proxy.engine_id) != (
        approval.management_id,
        approval.proxy_id,
        approval.engine_id,
        approval.engine_id,
    ):
        raise HostObservationUnavailable("original lifetime scope differs")
    start = time.monotonic_ns()
    first_owner = _owner_lock(management)
    assert_known_prior_stack_stopped(management, proxy)
    assert_prior_network_unreferenced(management.net_namespace)
    if management.published_port <= 0:
        raise HostObservationUnavailable("original published listener is unavailable")
    assert_host_listener_absent(management.published_port)
    assert_known_prior_stack_stopped(management, proxy)
    if _owner_lock(management) != first_owner:
        raise HostObservationUnavailable("original owner file changed during kernel checks")
    final, after = _protected(prior_path, uid=0, size_limit=65536)
    if final != original or _file_identity(after) != _file_identity(before):
        raise HostObservationUnavailable("original lifetime evidence changed during kernel checks")
    boot = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()))
    return KernelSubObservation(
        management.engine_id,
        management.container_id,
        proxy.container_id,
        management.pid,
        management.start_token,
        proxy.pid,
        proxy.start_token,
        management.net_namespace,
        management.owner_lock_device,
        management.owner_lock_inode,
        management.published_port,
        boot,
        start,
        time.monotonic_ns(),
        True,
        True,
        True,
    )
