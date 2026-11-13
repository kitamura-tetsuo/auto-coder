"""Read existing recovery fences and retained namespaces without acquiring them."""

from __future__ import annotations

import os
import select
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .containment_enrollment import _trusted_ancestors
from .containment_kernel_subobservation import _external_namespace_pin, _holder_start_token
from .fresh_host_observation import _assert_pid_flock, assert_prior_network_pin
from .recovery_containment_model import RecoveryContainmentUnavailable, RecoveryEnrollment, RecoveryLock, RecoveryResources
from .recovery_epoch_peer import KernelLock, inspect_kernel_lock


@dataclass(frozen=True)
class RecoveryResourceFact:
    name: str
    kind: str
    holder_descriptor: int
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    kernel_lock: KernelLock | None


@dataclass(frozen=True)
class RecoveryResourceObservation:
    holder_pid: int
    holder_start_token: str
    holder_boot_id: str
    resources: tuple[RecoveryResourceFact, ...]


def _private_lock(path: Path, selected: RecoveryLock, *, uid: int) -> os.stat_result:
    if path != path.resolve(strict=True) or str(path) != selected.path:
        raise RecoveryContainmentUnavailable("recovery fence path is not canonical")
    _trusted_ancestors(path.parent, uid=uid)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (selected.device, selected.inode):
        raise RecoveryContainmentUnavailable("original recovery fence is missing, replaced or unprotected")
    return info


@contextmanager
def hold_observed_resources(enrollment: RecoveryEnrollment, resources: RecoveryResources, *, expected_uid: int = 0) -> Iterator[RecoveryResourceObservation]:
    """Retain observation references and recheck every supplied actual holder.

    This takes no flock and creates no file. Original SH attribution must name
    the issuer process itself, so an inherited shared OFD is not independent
    issuer acquisition. Controller EX must name that same current issuer.
    """
    originals = {item.generation_id: item for item in enrollment.originals if item.state == "started"}
    watchers = {item.unit: item for item in enrollment.watchers}
    owner_fds = {item.name: item.descriptor for item in resources.original_owner_fds}
    controller_fds = {item.name: item.descriptor for item in resources.controller_fds}
    namespace_fds = {item.name: item.descriptor for item in resources.namespace_fds}
    if set(owner_fds) != set(originals) or set(namespace_fds) != set(originals) or set(controller_fds) != set(watchers):
        raise RecoveryContainmentUnavailable("recovery resource membership differs from the protected scope")
    with ExitStack() as stack:
        lifetime = os.pidfd_open(resources.holder_pid, 0)
        stack.callback(os.close, lifetime)
        poller = select.poll()
        poller.register(lifetime, select.POLLIN)
        held: list[tuple[RecoveryResourceFact, int, RecoveryLock | None]] = []

        def check_holder() -> None:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            if boot != resources.holder_boot_id or _holder_start_token(resources.holder_pid) != resources.holder_start_token or poller.poll(0):
                raise RecoveryContainmentUnavailable("recovery resource holder changed or exited")
            proc = Path("/proc") / str(resources.holder_pid)
            if proc.stat().st_uid != expected_uid or any(os.readlink(proc / "ns" / name) != os.readlink("/proc/1/ns/" + name) for name in ("pid", "user", "mnt", "net")):
                raise RecoveryContainmentUnavailable("recovery resource holder is not the trusted host process")

        def check_one(fact: RecoveryResourceFact, descriptor: int, selected: RecoveryLock | None) -> None:
            target = Path("/proc") / str(resources.holder_pid) / "fd" / str(fact.holder_descriptor)
            info, remote = os.fstat(descriptor), target.stat()
            expected = (fact.device, fact.inode, fact.mode, fact.uid)
            if (info.st_dev, info.st_ino, info.st_mode, info.st_uid) != expected or (remote.st_dev, remote.st_ino, remote.st_mode, remote.st_uid) != expected or os.readlink(target) != fact.path:
                raise RecoveryContainmentUnavailable("held recovery resource changed")
            if selected is None:
                assert_prior_network_pin(descriptor, fact.path)
                return
            _private_lock(Path(fact.path), selected, uid=expected_uid)
            observed = inspect_kernel_lock(resources.holder_pid, fact.holder_descriptor, fact.device, fact.inode)
            if observed != fact.kernel_lock or observed is None or observed.pid != resources.holder_pid:
                raise RecoveryContainmentUnavailable("recovery fence was inherited, released or changed")
            _assert_pid_flock(resources.holder_pid, descriptor, observed.mode)

        check_holder()
        for kind, selected_fds in (("original-sh", owner_fds), ("controller-ex", controller_fds)):
            for name, remote_fd in sorted(selected_fds.items()):
                selected = originals[name].owner if kind == "original-sh" else watchers[name].lock
                if selected is None:
                    raise RecoveryContainmentUnavailable("original owner identity is absent")
                expected = _private_lock(Path(selected.path), selected, uid=expected_uid)
                remote_path = Path("/proc") / str(resources.holder_pid) / "fd" / str(remote_fd)
                descriptor = os.open(remote_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink) != (expected.st_dev, expected.st_ino, expected.st_mode, expected.st_uid, expected.st_nlink):
                    raise RecoveryContainmentUnavailable("recovery issuer fence names another object")
                lock = inspect_kernel_lock(resources.holder_pid, remote_fd, info.st_dev, info.st_ino)
                mode = "READ" if kind == "original-sh" else "WRITE"
                if lock is None or lock.mode != mode or lock.pid != resources.holder_pid:
                    raise RecoveryContainmentUnavailable("recovery issuer lacks independently acquired " + kind)
                fact = RecoveryResourceFact(name, kind, remote_fd, selected.path, info.st_dev, info.st_ino, info.st_mode, info.st_uid, lock)
                check_one(fact, descriptor, selected)
                held.append((fact, descriptor, selected))
        for name, remote_fd in sorted(namespace_fds.items()):
            original = originals[name]
            pin = original.namespace_pin
            if pin is None or original.boot_id != resources.holder_boot_id:
                raise RecoveryContainmentUnavailable("original namespace boot or pin is absent")
            stack.enter_context(_external_namespace_pin(pin, pin.namespace))
            target = Path("/proc") / str(resources.holder_pid) / "fd" / str(remote_fd)
            descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            stack.callback(os.close, descriptor)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != (pin.device, pin.inode):
                raise RecoveryContainmentUnavailable("recovery namespace descriptor is not the retained original object")
            fact = RecoveryResourceFact(name, "network-namespace", remote_fd, pin.namespace, info.st_dev, info.st_ino, info.st_mode, info.st_uid, None)
            check_one(fact, descriptor, None)
            held.append((fact, descriptor, None))
        check_holder()
        result = RecoveryResourceObservation(resources.holder_pid, resources.holder_start_token, resources.holder_boot_id, tuple(item[0] for item in held))
        yield result
        for fact, descriptor, selected in held:
            check_one(fact, descriptor, selected)
        check_holder()
