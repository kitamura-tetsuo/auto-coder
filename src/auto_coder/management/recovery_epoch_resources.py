"""Acquire and retain the actual fences used by one recovery issuer lifetime."""

from __future__ import annotations

import fcntl
import os
import select
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .containment_enrollment import OriginalNetworkPin, _approved_directory
from .original_attempt_evidence import observe as observe_original_history
from .recovery_containment_model import NamedDescriptor, RecoveryEnrollment, RecoveryLock, RecoveryResources
from .recovery_epoch_authorization import EpochAuthorization
from .recovery_epoch_peer import HeldFence, HeldResource, inspect_held_resource
from .recovery_epoch_state import EpochUnavailable, RecoveryEpochState, issuer_lifetime


@dataclass(frozen=True)
class EpochResources:
    history_descriptor: int
    containment: RecoveryResources
    fences: tuple[HeldFence, ...]
    namespaces: tuple[HeldResource, ...]

    @property
    def original_descriptors(self) -> tuple[int, ...]:
        return tuple(item.descriptor for item in self.containment.original_owner_fds)

    def check(self) -> None:
        if (self.containment.holder_boot_id, self.containment.holder_pid, self.containment.holder_start_token) != issuer_lifetime():
            raise EpochUnavailable("recovery resources escaped their original issuer lifetime")
        held: tuple[HeldFence | HeldResource, ...] = (*self.fences, *self.namespaces)
        for resource in held:
            inspect_held_resource(resource)


def _open_bound_lock(selected: RecoveryLock, *, mode: int | None) -> int:
    path = Path(selected.path)
    _approved_directory(path.parent, uid=os.geteuid())
    if path != path.resolve(strict=True):
        raise EpochUnavailable("recovery owner/controller path is not canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info, named = os.fstat(descriptor), path.lstat()
        expected = (selected.device, selected.inode)
        if (info.st_dev, info.st_ino) != expected or (named.st_dev, named.st_ino) != expected or info.st_mode != named.st_mode or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise EpochUnavailable("recovery original fence identity or protection differs")
        if mode is not None:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _pin_current_namespace(selected: OriginalNetworkPin) -> int:
    """Duplicate a currently held nsfs object after pinning its exact holder."""
    boot = issuer_lifetime()[0]
    if selected.holder_boot_id != boot:
        raise EpochUnavailable("original namespace holder belongs to another boot")
    pidfd = os.pidfd_open(selected.holder_pid, 0)
    descriptor = -1
    try:
        poll = select.poll()
        poll.register(pidfd, select.POLLIN)
        holder = Path("/proc") / str(selected.holder_pid)

        def check_holder() -> None:
            fields = (holder / "stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            if len(fields) < 20 or fields[0] in ("Z", "X", "x") or fields[19] != selected.holder_start_token or poll.poll(0):
                raise EpochUnavailable("original namespace holder exited or changed")

        check_holder()
        path = holder / "fd" / str(selected.holder_fd)
        before = path.stat()
        if os.readlink(path) != selected.namespace or selected.namespace != f"net:[{selected.inode}]" or (before.st_dev, before.st_ino) != (selected.device, selected.inode):
            raise EpochUnavailable("original namespace pin no longer names its observed object")
        # Following the authenticated process's descriptor is intentional;
        # ordinary pathname symlinks never select an original namespace.
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        actual, after = os.fstat(descriptor), path.stat()
        if any((item.st_dev, item.st_ino, item.st_mode) != (before.st_dev, before.st_ino, before.st_mode) for item in (actual, after)) or os.readlink(path) != selected.namespace:
            raise EpochUnavailable("original namespace object changed during acquisition")
        check_holder()
        inspect_held_resource(HeldResource("acquired-namespace", descriptor))
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        os.close(pidfd)


@contextmanager
def hold_epoch_resources(authorization: EpochAuthorization, enrollment: RecoveryEnrollment, state: RecoveryEpochState) -> Iterator[EpochResources]:
    """Acquire independent original SH and real controller EX; create nothing."""
    with ExitStack() as held:
        history = held.enter_context(observe_original_history(authorization.history))
        fences = [HeldFence("history", history, "original-sh")]
        owners: list[NamedDescriptor] = []
        controllers: list[NamedDescriptor] = []
        namespaces: list[NamedDescriptor] = []
        transfers: list[HeldResource] = []
        for index, original in enumerate(enrollment.originals):
            if original.owner is not None:
                owner = _open_bound_lock(original.owner, mode=fcntl.LOCK_SH)
                held.callback(os.close, owner)
                owners.append(NamedDescriptor(original.generation_id, owner))
                fences.append(HeldFence(f"owner-{index}", owner, "original-sh"))
            if original.namespace_pin is not None:
                namespace = _pin_current_namespace(original.namespace_pin)
                held.callback(os.close, namespace)
                namespaces.append(NamedDescriptor(original.generation_id, namespace))
                transfers.append(HeldResource(f"namespace-{index}", namespace))
        for index, watcher in enumerate(enrollment.watchers):
            controller = _open_bound_lock(watcher.lock, mode=fcntl.LOCK_EX)
            held.callback(os.close, controller)
            controllers.append(NamedDescriptor(watcher.unit, controller))
            fences.append(HeldFence(f"controller-{index}", controller, "transferred-ex"))
        target = _open_bound_lock(enrollment.target.owner, mode=None)
        held.callback(os.close, target)
        fences.extend((HeldFence("target-owner", target, "target-ex"), HeldFence("epoch", state.descriptor, "transferred-ex")))
        boot, pid, start = issuer_lifetime()
        result = EpochResources(history, RecoveryResources(pid, start, boot, tuple(owners), tuple(controllers), tuple(namespaces)), tuple(fences), tuple(transfers))
        result.check()
        yield result
        result.check()
