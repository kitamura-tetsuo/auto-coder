"""Bounded original-lifetime kernel checks with an optional external pin.

This component independently checks old Docker/cgroup lifetimes, the full host
task namespace scan and the original published listener. A trusted-root v2
enrollment can select an externally retained namespace FD that the producer
and separate verifier independently reopen and pin during their own reads.
Neither result retains its own FD or establishes a full closed-profile verdict.
"""

from __future__ import annotations

import os
import select
import stat
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .containment_enrollment import EnrollmentObservation, OriginalNetworkPin, OriginalOwnerFencePin, _file_identity, _protected
from .fresh_host import _evidence
from .fresh_host_observation import (
    HostObservationUnavailable,
    PriorContainer,
    _assert_pid_flock,
    _inspect,
    assert_host_listener_absent,
    assert_known_prior_stack_stopped,
    assert_prior_network_pin,
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
    external_namespace_pin_verified: bool = False
    namespace_pin_holder_pid: int = 0
    namespace_pin_holder_start_token: str = ""
    namespace_pin_holder_fd: int = -1
    external_owner_fence_verified: bool = False
    owner_fence_holder_pid: int = 0
    owner_fence_holder_start_token: str = ""
    owner_fence_holder_fd: int = -1


def _holder_start_token(pid: int) -> str:
    value = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    prefix, separator, tail = value.rpartition(")")
    fields = tail.split()
    if not separator or prefix.partition(" (")[0] != str(pid) or len(fields) < 20 or fields[0] in ("Z", "X", "x") or not fields[19].isdecimal() or int(fields[19]) <= 0:
        raise HostObservationUnavailable("namespace pin holder lifetime is unavailable")
    return fields[19]


@contextmanager
def _external_namespace_pin(pin: OriginalNetworkPin, original_namespace: str) -> Iterator[None]:
    """Hold the referenced nsfs object throughout this observer's kernel scan.

    The trusted operator must have acquired the external FD while the original
    namespace was still alive. Current observations alone cannot reconstruct
    that earlier event or survive the external holder's termination.
    """
    descriptor = None
    lifetime = None
    try:
        if pin.namespace != original_namespace or str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip())) != pin.holder_boot_id:
            raise HostObservationUnavailable("approved external namespace pin does not match the original boot/namespace")
        lifetime = os.pidfd_open(pin.holder_pid, 0)
        selector = select.poll()
        selector.register(lifetime, select.POLLIN)
        target = f"/proc/{pin.holder_pid}/fd/{pin.holder_fd}"

        def check_lifetime() -> None:
            if _holder_start_token(pin.holder_pid) != pin.holder_start_token or selector.poll(0):
                raise HostObservationUnavailable("approved namespace pin holder lifetime differs or exited")

        check_lifetime()
        if os.readlink(target) != original_namespace:
            raise HostObservationUnavailable("approved namespace pin FD no longer names the original namespace")
        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (pin.device, pin.inode):
            raise HostObservationUnavailable("approved namespace pin kernel object differs")
        assert_prior_network_pin(descriptor, original_namespace)
        check_lifetime()
        if os.readlink(target) != original_namespace:
            raise HostObservationUnavailable("approved namespace pin FD changed during open")
        yield
        assert_prior_network_pin(descriptor, original_namespace)
        check_lifetime()
        if os.readlink(target) != original_namespace or (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != (pin.device, pin.inode):
            raise HostObservationUnavailable("approved namespace pin changed during kernel observation")
    except HostObservationUnavailable:
        raise
    except (OSError, ValueError, IndexError, UnicodeError) as exc:
        raise HostObservationUnavailable("approved external namespace pin cannot be independently retained") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lifetime is not None:
            os.close(lifetime)


@contextmanager
def _external_owner_fence(pin: OriginalOwnerFencePin, prior: PriorContainer) -> Iterator[None]:
    """Read back another process's exclusive flock without taking any lock.

    An approved operator must acquire the fence outside the read-only observer
    after the original manager has stopped. Both producer and verifier require
    the external process, exact open file and exclusive kernel flock to survive.
    """
    descriptor = None
    lifetime = None
    try:
        if pin.holder_pid == os.getpid() or (pin.device, pin.inode) != (prior.owner_lock_device, prior.owner_lock_inode):
            raise HostObservationUnavailable("original owner fence is not an independent exact-file pin")
        if str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip())) != pin.holder_boot_id:
            raise HostObservationUnavailable("approved owner fence holder boot differs")
        lifetime = os.pidfd_open(pin.holder_pid, 0)
        selector = select.poll()
        selector.register(lifetime, select.POLLIN)
        target = f"/proc/{pin.holder_pid}/fd/{pin.holder_fd}"

        def check_holder() -> None:
            if _holder_start_token(pin.holder_pid) != pin.holder_start_token or selector.poll(0):
                raise HostObservationUnavailable("approved owner fence holder lifetime differs or exited")
            if descriptor is None:
                raise HostObservationUnavailable("approved owner fence descriptor is not open")
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (pin.device, pin.inode):
                raise HostObservationUnavailable("approved owner fence file identity differs")
            _assert_pid_flock(pin.holder_pid, descriptor, "WRITE")
            if (os.stat(target).st_dev, os.stat(target).st_ino) != (pin.device, pin.inode):
                raise HostObservationUnavailable("approved owner fence holder descriptor changed")

        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        check_holder()
        yield
        check_holder()
    except HostObservationUnavailable:
        raise
    except (OSError, ValueError, IndexError, UnicodeError) as exc:
        raise HostObservationUnavailable("approved external owner fence cannot be independently verified") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if lifetime is not None:
            os.close(lifetime)


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
    pin = approval.original_network_pin
    owner_pin = approval.original_owner_fence
    with _external_owner_fence(owner_pin, management) if owner_pin is not None else nullcontext(), _external_namespace_pin(pin, management.net_namespace) if pin is not None else nullcontext():
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
            external_namespace_pin_verified=pin is not None,
            namespace_pin_holder_pid=pin.holder_pid if pin is not None else 0,
            namespace_pin_holder_start_token=pin.holder_start_token if pin is not None else "",
            namespace_pin_holder_fd=pin.holder_fd if pin is not None else -1,
            external_owner_fence_verified=owner_pin is not None,
            owner_fence_holder_pid=owner_pin.holder_pid if owner_pin is not None else 0,
            owner_fence_holder_start_token=owner_pin.holder_start_token if owner_pin is not None else "",
            owner_fence_holder_fd=owner_pin.holder_fd if owner_pin is not None else -1,
        )
