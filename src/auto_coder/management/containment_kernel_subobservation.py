"""Bounded original-lifetime kernel checks with an optional external pin.

This component independently checks old Docker/cgroup lifetimes, the full host
task namespace scan and the original published listener. A trusted-root v2
enrollment can select an externally retained namespace FD that the producer
and separate verifier independently reopen and pin during their own reads.
Neither result retains its own FD or establishes a full closed-profile verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import sqlite3
import stat
import subprocess
import tarfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .containment_enrollment import EnrollmentObservation, OriginalNetworkPin, OriginalOwnerFencePin, _file_identity, _protected
from .docker_inspection import canonical_legacy_inspection
from .fresh_host import _evidence
from .fresh_host_observation import (
    HostObservationUnavailable,
    PriorContainer,
    _assert_pid_flock,
    _capture_prior_kernel,
    _capture_supervisor_identity,
    _engine_id,
    _inspect,
    _validate_host_proc_profile,
    assert_host_listener_absent,
    assert_known_prior_stack_stopped,
    assert_prior_network_pin,
    assert_prior_network_unreferenced,
    assert_prior_processes_absent,
)
from .installed_bootstrap_mount_profile import InstalledBootstrapMountProfile
from .installed_bootstrap_mount_profile import _digest as _profile_digest
from .installed_bootstrap_mount_profile import _inspect as _profile_inspect
from .installed_bootstrap_mount_profile import _mounts as _profile_mounts
from .installed_bootstrap_mount_profile import _unshadowed_bootstrap_execution
from .installed_bootstrap_source import _REVIEWED_QUARANTINED_SOURCE_SHA256, _copy_source


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
    installed_profile: str = ""
    management_inspection_sha256: str = ""
    proxy_inspection_sha256: str = ""


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


def _require_capture_host() -> None:
    if os.geteuid() != 0 or any(os.readlink("/proc/self/ns/" + name) != os.readlink("/proc/1/ns/" + name) for name in ("pid", "user", "mnt", "net")):
        raise ValueError("initial-namespace host-root capture is required")
    _validate_host_proc_profile(os.readlink("/proc/self/ns/pid"), Path("/proc/self/uid_map").read_text(encoding="ascii"), Path("/proc/self/mountinfo").read_text(encoding="ascii"))


def _assert_live_docker_readback(container_id: str, current: dict[str, object], values: list[object], engine: str, *, label: str) -> None:
    final = _profile_inspect(container_id)
    if final != current:
        keys = sorted(key for key in set(current) | set(final) if current.get(key) != final.get(key))
        raise ValueError(label + " full Docker inspection changed at keys " + ",".join(keys))
    final_values = canonical_legacy_inspection(_inspect(container_id))
    if final_values != values:
        changed = [str(index) for index, (before, after) in enumerate(zip(values, final_values)) if before != after]
        raise ValueError(label + " legacy Docker fields changed at indices " + ",".join(changed))
    if _engine_id() != engine:
        raise ValueError(label + " Docker engine identity changed during live capture")


def capture_original_caddy_container(container_id: str, image_id: str, *, expected_mounts: tuple[tuple[str, str, bool], ...]) -> PriorContainer:
    """Capture the actual original Caddy, without broadening fresh admission."""
    try:
        _require_capture_host()
        engine = _engine_id()
        current = _profile_inspect(container_id)
        state, host, config = current.get("State"), current.get("HostConfig"), current.get("Config")
        if current.get("Image") != image_id or not isinstance(state, dict) or state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 0 or state.get("Status") != "running" or not isinstance(host, dict) or not isinstance(config, dict):
            raise ValueError("original Caddy is not the exact live instance")
        network = host.get("NetworkMode")
        if (
            host.get("Privileged") is not False
            or host.get("PidMode") not in ("", None)
            or host.get("CapAdd") not in (None, [])
            or host.get("SecurityOpt") not in (None, [])
            or host.get("Devices") not in (None, [])
            or not isinstance(network, str)
            or not network.startswith("container:")
            or len(network.removeprefix("container:")) != 64
            or any(char not in "0123456789abcdef" for char in network.removeprefix("container:"))
            or config.get("Entrypoint") != ["caddy"]
            or config.get("Cmd") != ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
            or config.get("WorkingDir") != "/srv"
            or config.get("User") != ""
            or config.get("Healthcheck") is not None
        ):
            raise ValueError("original Caddy live command or security profile differs")
        mounts = _profile_mounts(current)
        if mounts != tuple(sorted(expected_mounts)) or {destination: writable for _, destination, writable in mounts} != {"/etc/caddy/Caddyfile": False, "/data": True, "/config": True}:
            raise ValueError("original Caddy config or writable data mount effects differ")
        for source, _, _ in mounts:
            path = Path(source)
            if path != path.resolve(strict=True) or source in {"/", "/proc", "/sys", "/dev", "/run", "/var/run"} or source.startswith(("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")):
                raise ValueError("original Caddy mount source is unsupported")
        config_path = Path(next(source for source, destination, _ in mounts if destination == "/etc/caddy/Caddyfile"))
        config_bytes, config_identity = _protected(config_path, uid=0, size_limit=1024 * 1024)
        values = canonical_legacy_inspection(_inspect(container_id))
        if values[1:5] != [image_id, True, state["Pid"], "running"] or values[9] != network or values[13:15] != [state.get("StartedAt"), current.get("RestartCount")]:
            raise ValueError("original Caddy live Docker lifetime changed")
        prior = _capture_prior_kernel(container_id, image_id, engine, values, mounts, require_owner_lock=False)
        prior.validate()
        final_config, final_identity = _protected(config_path, uid=0, size_limit=1024 * 1024)
        if final_config != config_bytes:
            raise ValueError("original Caddy configuration bytes changed during live capture")
        if _file_identity(final_identity) != _file_identity(config_identity):
            raise ValueError("original Caddy configuration file identity changed during live capture")
        _assert_live_docker_readback(container_id, current, values, engine, label="original Caddy")
        return prior
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, subprocess.SubprocessError) as exc:
        raise HostObservationUnavailable("original Caddy live capture unavailable: " + str(exc)) from exc


def capture_quarantined_original_container(container_id: str, image_id: str, *, expected_mounts: tuple[tuple[str, str, bool], ...], bootstrap_config: Path) -> PriorContainer:
    """Capture actual live original identities for a later, separate enrollment.

    This observation-only reader admits the reviewed original's individual
    bootstrap file mounts and no-new-privileges setting. It neither enrolls nor
    contains the deployment, and its output is never replacement admission.
    The full observer must subsequently validate the approved source/config
    and support-mount profile against this independently captured lifetime.
    """
    try:
        _require_capture_host()
        engine = _engine_id()
        current = _profile_inspect(container_id)
        state, host, config = current.get("State"), current.get("HostConfig"), current.get("Config")
        if current.get("Image") != image_id or not isinstance(state, dict) or state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 0 or state.get("Status") != "running" or not isinstance(host, dict) or not isinstance(config, dict):
            raise ValueError("quarantined original is not the exact live instance")
        if (
            host.get("Privileged") is not False
            or host.get("PidMode") not in ("", None)
            or host.get("CapAdd") not in (None, [])
            or host.get("SecurityOpt") != ["no-new-privileges"]
            or host.get("ReadonlyRootfs") is not True
            or host.get("Devices") not in (None, [])
            or host.get("NetworkMode") in ("host", None, "")
            or config.get("Entrypoint") != ["python"]
            or config.get("Cmd") != ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--fresh-initialization"]
            or config.get("WorkingDir") != "/workspace"
            or config.get("User") != ""
            or config.get("Healthcheck") is not None
            or "/management-source" in json.dumps(config, sort_keys=True)
        ):
            raise ValueError("quarantined original live command or security profile differs")
        mounts = _profile_mounts(current)
        _unshadowed_bootstrap_execution(config, mounts)
        if mounts != tuple(sorted(expected_mounts)) or any(source in {"/", "/proc", "/sys", "/dev", "/run", "/var/run"} or source.startswith(("/proc/", "/sys/", "/dev/", "/run/", "/var/run/")) for source, _, _ in mounts):
            raise ValueError("quarantined original live mounts differ from the selected profile")
        mapped = {destination: (source, writable) for source, destination, writable in mounts}
        effects = {"/bootstrap/config.json": False, "/bootstrap/management.yml": False, "/management-source": False, "/management-versions": False, "/management-child-state": True, "/management-oauth": True}
        if any(destination not in mapped or mapped[destination][1] != writable for destination, writable in effects.items()) or mapped["/bootstrap/config.json"][0] != str(bootstrap_config):
            raise ValueError("quarantined original live file/state/OAuth mount effect differs")
        if any(destination == "/bootstrap" or destination.startswith(("/bootstrap/", "/management-source/", "/management-versions/", "/management-child-state/", "/management-oauth/")) and destination not in effects for _, destination, _ in mounts):
            raise ValueError("quarantined original live mount paths are shadowed")
        config_bytes, config_identity = _protected(bootstrap_config, uid=0, size_limit=65536)
        source = _copy_source(container_id)
        if hashlib.sha256(source).hexdigest() != _REVIEWED_QUARANTINED_SOURCE_SHA256:
            raise ValueError("quarantined original bootstrap source is not reviewed")
        values = canonical_legacy_inspection(_inspect(container_id))
        if values[1:5] != [image_id, True, state["Pid"], "running"] or values[9] != host["NetworkMode"] or values[13:15] != [state.get("StartedAt"), current.get("RestartCount")]:
            raise ValueError("quarantined original live Docker lifetime changed")
        prior = _capture_prior_kernel(container_id, image_id, engine, values, mounts, require_owner_lock=True)
        prior = _capture_supervisor_identity(prior, values, mounts, bootstrap_config)
        prior.validate()
        final_config, final_identity = _protected(bootstrap_config, uid=0, size_limit=65536)
        if final_config != config_bytes or _file_identity(final_identity) != _file_identity(config_identity) or hashlib.sha256(config_bytes).hexdigest() != prior.bootstrap_sha256:
            raise ValueError("quarantined original config changed during live capture")
        if _copy_source(container_id) != source:
            raise ValueError("quarantined original source bytes changed during live capture")
        _assert_live_docker_readback(container_id, current, values, engine, label="quarantined original")
        return prior
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, sqlite3.Error, subprocess.SubprocessError, tarfile.TarError) as exc:
        raise HostObservationUnavailable("quarantined original live capture unavailable: " + str(exc)) from exc


def _assert_profile_stack_stopped(enrolled: EnrollmentObservation, profile: InstalledBootstrapMountProfile, management: PriorContainer, proxy: PriorContainer) -> None:
    """Recheck an observation-only original profile without replacement admission.

    The caller has independently validated the installed source and effective
    mount/configuration profile. Bind that exact complete Docker snapshot to
    each captured original lifetime before sharing the kernel absence checks.
    Neither this reader nor the mount profile changes fresh replacement rules.
    """
    approval = enrolled.enrollment
    if (
        profile.profile != approval.profile
        or profile.profile not in ("ordinary-v1", "quarantined-original-v1")
        or (profile.management_id, profile.proxy_id) != (approval.management_id, approval.proxy_id)
        or (management.container_id, proxy.container_id) != (profile.management_id, profile.proxy_id)
        or management.engine_id != approval.engine_id
        or proxy.engine_id != approval.engine_id
        or proxy.network_mode != "container:" + management.container_id
        or proxy.net_namespace != management.net_namespace
        or (profile.management_original_mounts_sha256, profile.proxy_original_mounts_sha256) != (management.mounts_sha256, proxy.mounts_sha256)
        or (profile.original_owner_lock_device, profile.original_owner_lock_inode) != (management.owner_lock_device, management.owner_lock_inode)
        or profile.bootstrap_config_sha256 != management.bootstrap_sha256
    ):
        raise HostObservationUnavailable("installed original profile and captured lifetime scope differ")
    if _engine_id() != approval.engine_id:
        raise HostObservationUnavailable("Docker engine identity changed")
    for prior, digest, enrolled_image in (
        (management, profile.management_inspection_sha256, approval.management_image_id),
        (proxy, profile.proxy_inspection_sha256, approval.proxy_image_id),
    ):
        try:
            prior.validate()
        except (ValueError, TypeError) as exc:
            raise HostObservationUnavailable("prior containment identity is invalid") from exc
        current = _profile_inspect(prior.container_id)
        if _profile_digest([current]) != digest:
            raise HostObservationUnavailable("installed original Docker profile changed during kernel observation")
        state, host = current.get("State"), current.get("HostConfig")
        if (
            not isinstance(state, dict)
            or not isinstance(host, dict)
            or current.get("Image") != prior.image_id
            or prior.image_id != enrolled_image
            or state.get("Running") is not False
            or type(state.get("Pid")) is not int
            or state["Pid"] != 0
            or state.get("Status") != "exited"
            or state.get("StartedAt") != prior.started_at
            or current.get("RestartCount") != prior.restart_count
            or type(current.get("RestartCount")) is not int
            or host.get("NetworkMode") != prior.network_mode
        ):
            raise HostObservationUnavailable("original container is not the exact stopped lifetime")
        assert_prior_processes_absent(prior)
    if _engine_id() != approval.engine_id:
        raise HostObservationUnavailable("Docker engine changed during original lifetime observation")


def observe_kernel_subcomponent(enrolled: EnrollmentObservation, *, installed_profile: InstalledBootstrapMountProfile | None = None) -> KernelSubObservation:
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
        if installed_profile is None:
            assert_known_prior_stack_stopped(management, proxy)
        else:
            _assert_profile_stack_stopped(enrolled, installed_profile, management, proxy)
        assert_prior_network_unreferenced(management.net_namespace)
        if management.published_port <= 0:
            raise HostObservationUnavailable("original published listener is unavailable")
        assert_host_listener_absent(management.published_port)
        if installed_profile is None:
            assert_known_prior_stack_stopped(management, proxy)
        else:
            _assert_profile_stack_stopped(enrolled, installed_profile, management, proxy)
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
            installed_profile=installed_profile.profile if installed_profile is not None else "",
            management_inspection_sha256=installed_profile.management_inspection_sha256 if installed_profile is not None else "",
            proxy_inspection_sha256=installed_profile.proxy_inspection_sha256 if installed_profile is not None else "",
        )
