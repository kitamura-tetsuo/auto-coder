"""Independently repeatable current ordinary-recovery containment observations.

This read-only producer neither enrolls a scope, inhibits a controller, acquires
an owner fence, repairs history, creates admission nor grants recovery authority.
The fixed protected selection and every current technical observation are joined
here; the epoch issuer separately establishes complete original history and READY.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from auto_coder.logger_config import get_logger, setup_logger

from .archived_exclusions import unique_pairs
from .containment_enrollment import OriginalFile, _approved_directory, _file_identity, _protected, _trusted_ancestors
from .containment_kernel_subobservation import _holder_start_token, _require_capture_host, capture_original_caddy_container
from .containment_observation import DockerObservation, observe_recovery_docker
from .containment_route_manifest import RecoveryRouteScope, RecoveryRouteSelection, RouteManifestObservation, observe_recovery_route_manifest
from .docker_inspection import canonical_legacy_inspection
from .fresh_host import _evidence
from .fresh_host_observation import (
    PriorContainer,
    _assert_pid_flock,
    _capture_prior_kernel,
    _capture_supervisor_identity,
    _cgroup_tasks,
    _engine_id,
    _inspect,
    assert_container_published_listener,
    assert_host_listener_absent,
    assert_prior_network_unreferenced,
    assert_prior_processes_absent,
    capture_prior_container,
)
from .installed_bootstrap_mount_profile import _inspect as _full_inspect
from .installed_bootstrap_mount_profile import _mounts
from .original_attempts import source_identity
from .proxy_reconnect import ProxyBinding
from .recovery_containment_model import (
    RECOVERY_ENROLLMENT_ROOT,
    NamedDescriptor,
    RecoveryContainmentUnavailable,
    RecoveryEnrollment,
    RecoveryEnrollmentObservation,
    RecoveryFile,
    RecoveryOriginal,
    RecoveryResources,
    RecoveryTarget,
)
from .recovery_containment_profile import RecoveryManagerFact, _mapped_file, assert_disjoint_target, digest, read_bound_file, validate_manager_profile, validate_proxy_profile
from .recovery_containment_resources import RecoveryResourceObservation, _private_lock, hold_observed_resources
from .recovery_diagnostics import refusal_causes
from .recovery_watcher_source import RecoveryWatcherUnitFact, observe_recovery_watcher_unit

logger = get_logger(__name__)
_REPORT_SCHEMA = "current-recovery-containment-v1"
_STATUS = "observed-current-recovery-containment"
_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RecoveryKernelFact:
    generation_id: str
    current_state: str
    boot_id: str
    manager_pid: int
    manager_start_token: str
    proxy_pid: int
    proxy_start_token: str
    original_namespace: str
    owner_device: int
    owner_inode: int
    published_port: int
    original_cgroups_empty: bool
    original_namespace_unoccupied: bool
    original_listener_absent: bool


@dataclass(frozen=True)
class RecoveryTargetFact:
    container_id: str
    pid: int
    start_token: str
    boot_id: str
    network_namespace: str
    pid_namespace: str
    cgroup_path: str
    cgroup_device: int
    cgroup_inode: int
    owner_device: int
    owner_inode: int
    published_port: int


@dataclass(frozen=True)
class RecoveryWatcherFact:
    unit: str
    manager_id: str
    proxy_id: str
    configuration: RecoveryFile
    source: RecoveryFile
    installed_unit: RecoveryWatcherUnitFact
    lock_device: int
    lock_inode: int


@dataclass(frozen=True)
class RecoveryContainmentFacts:
    enrollment: RecoveryEnrollmentObservation
    source_identity: str
    docker: tuple[DockerObservation, ...]
    managers: tuple[RecoveryManagerFact, ...]
    proxy_inspections: tuple[tuple[str, str], ...]
    kernel: tuple[RecoveryKernelFact, ...]
    target: RecoveryTargetFact
    resources: RecoveryResourceObservation
    routes: RouteManifestObservation
    watchers: tuple[RecoveryWatcherFact, ...]
    effective_files: tuple[RecoveryFile, ...]


@dataclass(frozen=True)
class RecoveryContainmentReport:
    schema: str
    status: str
    observation_id: str
    observer_pid: int
    observer_start_token: str
    boot_id: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    facts: RecoveryContainmentFacts
    evidence_sha256: str
    grant_issued: bool = False
    history_asserted: bool = False
    admission_created: bool = False

    @property
    def managers(self) -> tuple[RecoveryManagerFact, ...]:
        return self.facts.managers

    @property
    def docker_ids(self) -> tuple[str, ...]:
        return tuple(item.container_id for item in self.facts.docker)

    @property
    def target_pid(self) -> int:
        return self.facts.target.pid

    @property
    def target_start_token(self) -> str:
        return self.facts.target.start_token

    @property
    def enrollment(self) -> RecoveryEnrollmentObservation:
        return self.facts.enrollment


def _load_enrollment(enrollment_id: str, *, root: Path, expected_uid: int) -> RecoveryEnrollmentObservation:
    """Controlled protected fixture seam; the production factory fixes both."""
    from .recovery_containment_model import _IDENTIFIER

    if not isinstance(enrollment_id, str) or not _IDENTIFIER.fullmatch(enrollment_id) or os.geteuid() != expected_uid:
        raise RecoveryContainmentUnavailable("invalid protected recovery enrollment selector")
    _approved_directory(root, uid=expected_uid)
    path = root / (enrollment_id + ".json")
    content, info = _protected(path, uid=expected_uid, size_limit=1024 * 1024)
    if info.st_nlink != 1:
        raise RecoveryContainmentUnavailable("recovery enrollment has an ambiguous file identity")
    selected = RecoveryEnrollment.parse(json.loads(content, object_pairs_hook=unique_pairs))
    if selected.enrollment_id != enrollment_id:
        raise RecoveryContainmentUnavailable("protected recovery enrollment selector differs")
    return RecoveryEnrollmentObservation(selected, hashlib.sha256(content).hexdigest(), info.st_dev, info.st_ino, info.st_ctime_ns)


def load_enrollment(enrollment_id: str) -> RecoveryEnrollmentObservation:
    """Load only the fixed trusted-host scope; no request/environment override."""
    _require_capture_host()
    return _load_enrollment(enrollment_id, root=RECOVERY_ENROLLMENT_ROOT, expected_uid=0)


def route_selection(approved: RecoveryEnrollmentObservation) -> RecoveryRouteSelection:
    """Pass the full independently protected recovery scope to shared readers."""
    selected = approved.enrollment
    originals: list[OriginalFile] = []
    for item in selected.originals:
        originals.extend((OriginalFile("bootstrap-config", **asdict(item.bootstrap_config)), OriginalFile("management-config", **asdict(item.management_config)), OriginalFile("proxy-config", **asdict(item.proxy_config))))
    originals.extend((OriginalFile("bootstrap-config", **asdict(selected.target.bootstrap_config)), OriginalFile("management-config", **asdict(selected.target.management_config))))
    for watcher in selected.watchers:
        originals.extend((OriginalFile("watcher-source", **asdict(watcher.source)), OriginalFile("watcher-config", **asdict(watcher.configuration)), OriginalFile("support-source", **asdict(watcher.unit_source))))
    scope = RecoveryRouteScope(selected.deployment_id, selected.enrollment_id, selected.authorization_id, selected.profile, selected.container_ids, tuple(originals))
    return RecoveryRouteSelection(scope, approved.sha256)


def _engine(enrollment: RecoveryEnrollment) -> tuple[DockerObservation, ...]:
    managers = tuple(item.management_id for item in enrollment.originals) + (enrollment.target.management_id,)
    proxies = tuple(item.proxy_id for item in enrollment.originals)
    docker = observe_recovery_docker(managers, proxies)
    selected = set(enrollment.container_ids)
    if any(item.engine_id != enrollment.engine_id or item.management_route_roles and item.container_id not in selected for item in docker):
        raise RecoveryContainmentUnavailable("foreign, unknown or omitted recovery management candidate")
    if any(item.container_id in selected and (item.restart_policy != "no" or item.maximum_retry_count != 0) for item in docker):
        raise RecoveryContainmentUnavailable("an ordinary recovery Docker restart route remains enabled")
    return docker


def _original_kernel(original: RecoveryOriginal, manager: RecoveryManagerFact, current: dict[str, DockerObservation]) -> RecoveryKernelFact:
    if original.state == "never-started":
        for identifier in (original.management_id, original.proxy_id):
            item = current[identifier]
            if item.running or item.status != "created" or item.pid != 0 or item.restart_count != 0 or item.started_at != "0001-01-01T00:00:00Z" or item.finished_at != "0001-01-01T00:00:00Z":
                raise RecoveryContainmentUnavailable("selected never-started Docker object has a lifetime")
        assert_host_listener_absent(manager.published_port)
        return RecoveryKernelFact(original.generation_id, "currently-created-never-started", "", 0, "", 0, "", "", 0, 0, manager.published_port, False, False, True)
    if original.prior_evidence is None or original.owner is None or original.namespace_pin is None:
        raise RecoveryContainmentUnavailable("original lifetime evidence or resource identity is missing")
    read_bound_file(original.prior_evidence)
    prior, proxy, prior_sha = _evidence(Path(original.prior_evidence.path))
    if prior_sha != original.prior_evidence.sha256 or original.boot_id != Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip():
        raise RecoveryContainmentUnavailable("original evidence differs or came from another boot")
    if (
        prior.engine_id != proxy.engine_id
        or prior.engine_id != current[original.management_id].engine_id
        or (prior.container_id, prior.image_id, prior.instance_id) != (original.management_id, original.management_image_id, original.instance_id)
        or (proxy.container_id, proxy.image_id, proxy.network_mode) != (original.proxy_id, original.proxy_image_id, "container:" + original.management_id)
        or prior.net_namespace != proxy.net_namespace
        or prior.net_namespace != original.namespace_pin.namespace
        or (prior.owner_lock_device, prior.owner_lock_inode) != (original.owner.device, original.owner.inode)
        or (prior.bootstrap_source, prior.bootstrap_sha256, prior.published_port) != (original.bootstrap_config.path, original.bootstrap_config.sha256, manager.published_port)
        or prior.oauth_source != manager.oauth_root
    ):
        raise RecoveryContainmentUnavailable("recovery captured original scope differs")
    oauth = Path(manager.oauth_root).stat()
    if (oauth.st_dev, oauth.st_ino) != (prior.oauth_device, prior.oauth_inode):
        raise RecoveryContainmentUnavailable("recovery captured OAuth object was replaced")
    for lifetime in (prior, proxy):
        observed = current[lifetime.container_id]
        mounts = tuple(sorted((item.source, item.destination, item.writable) for item in original.mounts if item.container_id == lifetime.container_id))
        mount_sha = hashlib.sha256(json.dumps(mounts, separators=(",", ":")).encode()).hexdigest()
        if observed.running or observed.pid != 0 or observed.status != "exited" or observed.image_id != lifetime.image_id or observed.started_at != lifetime.started_at or observed.restart_count != lifetime.restart_count or mount_sha != lifetime.mounts_sha256:
            raise RecoveryContainmentUnavailable("recovery original Docker lifetime/configuration changed")
        assert_prior_processes_absent(lifetime)
    assert_prior_network_unreferenced(prior.net_namespace)
    assert_host_listener_absent(prior.published_port)
    read_bound_file(original.prior_evidence)
    return RecoveryKernelFact(original.generation_id, "original-lifetimes-absent", original.boot_id, prior.pid, prior.start_token, proxy.pid, proxy.start_token, prior.net_namespace, original.owner.device, original.owner.inode, prior.published_port, True, True, True)


def _target_kernel(enrollment: RecoveryEnrollment, current: dict[str, DockerObservation], kernels: tuple[RecoveryKernelFact, ...]) -> RecoveryTargetFact:
    target = enrollment.target
    observed = current[target.management_id]
    if not observed.running or observed.status != "running" or observed.pid <= 1 or observed.image_id != target.management_image_id:
        raise RecoveryContainmentUnavailable("exact recovery target is not running its custody entrypoint")
    proc = Path("/proc") / str(observed.pid)
    with ExitStack() as stack:
        lifetime = os.pidfd_open(observed.pid, 0)
        stack.callback(os.close, lifetime)
        poller = select.poll()
        poller.register(lifetime, select.POLLIN)
        token = _holder_start_token(observed.pid)
        network, pid_namespace = os.readlink(proc / "ns/net"), os.readlink(proc / "ns/pid")
        if network in {item.original_namespace for item in kernels} or network == os.readlink("/proc/1/ns/net") or pid_namespace == os.readlink("/proc/1/ns/pid"):
            raise RecoveryContainmentUnavailable("recovery target namespace aliases an original or the host")
        command = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        if not command or command[0] not in (b"python", b"/opt/auto-coder/.venv/bin/python") or command[1:] != [b"-m", b"auto_coder.management.bootstrap_supervisor", b"--config", b"/bootstrap/config.json", b"--recovery-epoch"]:
            raise RecoveryContainmentUnavailable("target process is not the exact prelaunch consumer")
        expected_cgroup = "/system.slice/docker-" + target.management_id + ".scope"
        if (proc / "cgroup").read_text(encoding="ascii").splitlines() != ["0::" + expected_cgroup]:
            raise RecoveryContainmentUnavailable("recovery target cgroup differs")
        cgroup = Path("/sys/fs/cgroup") / expected_cgroup.lstrip("/")
        info = cgroup.stat()
        if _cgroup_tasks(cgroup) != {observed.pid}:
            raise RecoveryContainmentUnavailable("recovery prelaunch target has unexpected child tasks")
        _private_lock(Path(target.owner.path), target.owner, uid=0)
        owner = os.open(target.owner.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        stack.callback(os.close, owner)
        if (os.fstat(owner).st_dev, os.fstat(owner).st_ino) != (target.owner.device, target.owner.inode):
            raise RecoveryContainmentUnavailable("recovery target owner changed during open")
        _assert_pid_flock(observed.pid, owner, "WRITE")
        assert_container_published_listener(target.management_id, target.management_image_id, target.published_port)
        if _holder_start_token(observed.pid) != token or poller.poll(0) or os.readlink(proc / "ns/net") != network or _cgroup_tasks(cgroup) != {observed.pid} or (cgroup.stat().st_dev, cgroup.stat().st_ino) != (info.st_dev, info.st_ino):
            raise RecoveryContainmentUnavailable("recovery target lifetime changed during observation")
        return RecoveryTargetFact(target.management_id, observed.pid, token, Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(), network, pid_namespace, expected_cgroup, info.st_dev, info.st_ino, target.owner.device, target.owner.inode, target.published_port)


def _watchers(enrollment: RecoveryEnrollment, routes: RouteManifestObservation) -> tuple[RecoveryWatcherFact, ...]:
    from . import proxy_watch_cli

    actual_source = str(Path(cast(str, proxy_watch_cli.__file__)).resolve(strict=True))
    configs = {item.configuration.path for item in enrollment.watchers}
    selected_units = {item.unit for item in enrollment.watchers}
    routes_by_name = {item.unit: item for item in routes.systemd}
    result = []
    pairs = {(item.management_id, item.management_image_id, item.proxy_id, item.proxy_image_id): item for item in enrollment.originals}
    for selected in enrollment.watchers:
        if selected.source.path != actual_source:
            raise RecoveryContainmentUnavailable("watcher source is not the actual current installed module")
        read_bound_file(selected.source, source=True)
        content = read_bound_file(selected.configuration)
        raw = json.loads(content, object_pairs_hook=unique_pairs)
        if not isinstance(raw, dict) or set(raw) != {"state_root", "manager_id", "manager_image", "proxy_id", "proxy_image", "config_sha256"} or any(not isinstance(value, str) for value in raw.values()):
            raise RecoveryContainmentUnavailable("watcher effective configuration differs")
        binding = ProxyBinding(raw["manager_id"], raw["manager_image"], raw["proxy_id"], raw["proxy_image"], raw["config_sha256"])
        binding.validate()
        pair = pairs.get((binding.manager_id, binding.manager_image, binding.proxy_id, binding.proxy_image))
        if pair is None or pair.proxy_config.sha256 != binding.config_sha256 or selected.lock.path != str(Path(raw["state_root"]) / "proxy-watch.lock"):
            raise RecoveryContainmentUnavailable("watcher source/configuration does not use the exact enrolled controller lock")
        root = Path(raw["state_root"])
        _approved_directory(root, uid=0)
        fact = routes_by_name.get(selected.unit)
        if fact is None:
            raise RecoveryContainmentUnavailable("watcher installed unit is outside the observed routes")
        installed_unit = observe_recovery_watcher_unit(selected, fact)
        result.append(RecoveryWatcherFact(selected.unit, binding.manager_id, binding.proxy_id, selected.configuration, selected.source, installed_unit, selected.lock.device, selected.lock.inode))
    source_sets = [(item.unit, item.command_sources) for item in routes.systemd]
    source_sets += [(str(item.uid) + ":" + item.fact.unit, item.fact.command_sources) for item in routes.user_systemd]
    source_sets += [(item.path, item.command_sources) for item in routes.cron_entries]
    for name, sources in source_sets:
        paths = {item.path for item in sources}
        if actual_source in paths and (len(paths & configs) != 1 or name.endswith(".service") and name not in selected_units):
            raise RecoveryContainmentUnavailable("unknown watcher invocation/configuration is outside the actual controller fences")
    return tuple(result)


def _effective_files(enrollment: RecoveryEnrollment, managers: tuple[RecoveryManagerFact, ...]) -> tuple[RecoveryFile, ...]:
    result: dict[str, RecoveryFile] = {}
    members: tuple[RecoveryOriginal | RecoveryTarget, ...] = (*enrollment.originals, enrollment.target)
    for selected, manager in zip(members, managers):
        raw = json.loads(read_bound_file(selected.bootstrap_config), object_pairs_hook=unique_pairs)
        for name in ("original_genesis", "recovery_epoch_config"):
            configured = raw.get(name)
            if configured is None:
                continue
            path = Path(_mapped_file(configured, manager.mounts, writable=False))
            data, info = _protected(path, uid=0, size_limit=1024 * 1024)
            if info.st_nlink != 1:
                raise RecoveryContainmentUnavailable("effective original/consumer input is aliased")
            result[str(path)] = RecoveryFile(str(path), hashlib.sha256(data).hexdigest(), info.st_dev, info.st_ino)
            if name == "recovery_epoch_config":
                _consumer_custody_profile(enrollment, data)
    return tuple(result[key] for key in sorted(result))


def _consumer_custody_profile(enrollment: RecoveryEnrollment, encoded: bytes) -> None:
    """Bind the target's actual prelaunch inputs to this exact selected scope.

    History completeness, epoch EX and grant admission remain issuer duties.
    Here those input roles must still name their real mapped objects rather
    than an alternate socket, a different original owner or missing resources.
    """
    from .recovery_epoch_consumer import parse_consumer_configuration

    selected = parse_consumer_configuration(encoded)
    target = enrollment.target
    mounts = {item.destination: item for item in target.mounts}
    socket_directory = mounts.get("/management-recovery")
    if (
        selected.authorization_id != enrollment.authorization_id
        or selected.issuer_uid != 0
        or socket_directory is None
        or socket_directory.source != "/var/lib/auto-coder/recovery-epoch-peer"
        or socket_directory.writable
        or not stat.S_ISDIR(socket_directory.mode)
        or (selected.socket_directory_device, selected.socket_directory_inode) != (socket_directory.device, socket_directory.inode)
        or (selected.target_owner.name, selected.target_owner.device, selected.target_owner.inode) != ("target-owner", target.owner.device, target.owner.inode)
    ):
        raise RecoveryContainmentUnavailable("recovery consumer's actual issuer socket or target owner differs")
    owners = {item.name: item for item in selected.original_fences}
    expected = {"history"}
    allowed_owner_destinations = set()
    transferred = {item.name: item for item in selected.transferred_resources}
    expected_transfers = {"epoch"}
    for index, original in enumerate(enrollment.originals):
        if original.owner is None:
            continue
        name, destination = f"owner-{index}", f"/management-recovery-originals/{index}/owner.lock"
        expected.add(name)
        allowed_owner_destinations.add(destination)
        chosen, mount = owners.get(name), mounts.get(destination)
        if (
            chosen is None
            or mount is None
            or mount.writable
            or not stat.S_ISREG(mount.mode)
            or chosen.path != destination
            or mount.source != original.owner.path
            or (chosen.device, chosen.inode) != (original.owner.device, original.owner.inode)
            or (mount.device, mount.inode) != (chosen.device, chosen.inode)
        ):
            raise RecoveryContainmentUnavailable("recovery consumer original SH input is missing or bound to another owner")
        namespace = transferred.get(f"namespace-{index}")
        pin = original.namespace_pin
        if namespace is None or pin is None or namespace.kind != "network-namespace" or (namespace.device, namespace.inode) != (pin.device, pin.inode):
            raise RecoveryContainmentUnavailable("recovery consumer original namespace input differs")
        expected_transfers.add(namespace.name)
    if {item.destination for item in target.mounts if item.destination.startswith("/management-recovery-originals/")} != allowed_owner_destinations:
        raise RecoveryContainmentUnavailable("recovery target has undeclared original owner mounts")
    history = owners.get("history")
    history_mount = mounts.get("/management-recovery-history/history.lock")
    if set(owners) != expected or history is None or history_mount is None or history.path != history_mount.destination or history_mount.writable or not stat.S_ISREG(history_mount.mode) or (history.device, history.inode) != (history_mount.device, history_mount.inode):
        raise RecoveryContainmentUnavailable("recovery consumer history SH input or original set differs")
    for index, watcher in enumerate(enrollment.watchers):
        controller = transferred.get(f"controller-{index}")
        if controller is None or controller.kind != "transferred-ex" or (controller.device, controller.inode) != (watcher.lock.device, watcher.lock.inode):
            raise RecoveryContainmentUnavailable("recovery consumer controller custody input differs")
        expected_transfers.add(controller.name)
    epoch = transferred.get("epoch")
    if set(transferred) != expected_transfers or epoch is None or epoch.kind != "transferred-ex":
        raise RecoveryContainmentUnavailable("recovery consumer deployment custody input is incomplete")


def _facts(approved: RecoveryEnrollmentObservation, resources: RecoveryResourceObservation) -> RecoveryContainmentFacts:
    enrollment = approved.enrollment
    docker = _engine(enrollment)
    current = {item.container_id: item for item in docker}
    full = {identifier: _full_inspect(identifier) for identifier in enrollment.container_ids}
    if any(digest([full[identifier]]) != current[identifier].inspection_sha256 for identifier in full):
        raise RecoveryContainmentUnavailable("recovery Docker objects changed after whole-engine enumeration")
    members: tuple[RecoveryOriginal | RecoveryTarget, ...] = (*enrollment.originals, enrollment.target)
    managers = tuple(validate_manager_profile(item, full[item.management_id], source=enrollment.source_identity) for item in members)
    assert_disjoint_target(enrollment, managers)
    proxies = tuple((item.generation_id, validate_proxy_profile(item, full[item.proxy_id])) for item in enrollment.originals)
    kernels = tuple(_original_kernel(item, manager, current) for item, manager in zip(enrollment.originals, managers))
    target = _target_kernel(enrollment, current, kernels)
    routes = observe_recovery_route_manifest(route_selection(approved), docker)
    watchers = _watchers(enrollment, routes)
    effective = _effective_files(enrollment, managers)
    if not routes.closed_profile or routes.deployment_roots != tuple(sorted((item for original in members for item in original.mounts), key=lambda item: (item.container_id, item.destination))):
        raise RecoveryContainmentUnavailable("recovery route closure omitted or changed an actual selected root")
    # The complete route reader already repeats its system/user/cron catalog.
    # Repeat the other actual predicates around that reader while custody is
    # retained, without redundantly multiplying the full systemd scan count.
    if (
        tuple(validate_manager_profile(item, full[item.management_id], source=enrollment.source_identity) for item in members) != managers
        or tuple((item.generation_id, validate_proxy_profile(item, full[item.proxy_id])) for item in enrollment.originals) != proxies
        or tuple(_original_kernel(item, manager, current) for item, manager in zip(enrollment.originals, managers)) != kernels
        or _target_kernel(enrollment, current, kernels) != target
        or _watchers(enrollment, routes) != watchers
        or _effective_files(enrollment, managers) != effective
    ):
        raise RecoveryContainmentUnavailable("recovery source, kernel or effective configuration changed during observation")
    if _engine(enrollment) != docker or any(_full_inspect(identifier) != full[identifier] for identifier in full):
        raise RecoveryContainmentUnavailable("recovery complete engine/source/configuration changed during observation")
    return RecoveryContainmentFacts(approved, enrollment.source_identity, docker, managers, proxies, kernels, target, resources, routes, watchers, effective)


def observe(enrollment_id: str, resources: RecoveryResources) -> RecoveryContainmentReport:
    """Produce complete current facts or raise; partial evidence is never success."""
    try:
        started = time.monotonic_ns()
        approved = load_enrollment(enrollment_id)
        resources = RecoveryResources.parse(json.loads(json.dumps(asdict(resources))))
        if approved.enrollment.source_identity != source_identity():
            raise RecoveryContainmentUnavailable("enrolled recovery package source is not the current observer source")
        with hold_observed_resources(approved.enrollment, resources) as held:
            last = _facts(approved, held)
            if load_enrollment(enrollment_id) != approved:
                raise RecoveryContainmentUnavailable("recovery protected enrollment changed during observations")
            payload = digest(asdict(last))
            report = RecoveryContainmentReport(_REPORT_SCHEMA, _STATUS, str(uuid.uuid4()), os.getpid(), _holder_start_token(os.getpid()), resources.holder_boot_id, started, time.monotonic_ns(), last, payload)
        logger.bind(observation_only=True, recovery_grant=False, admission_created=False).info("Current recovery containment observed: enrollment={} evidence={}", enrollment_id, payload)
        return report
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, sqlite3.Error, subprocess.SubprocessError, tarfile.TarError) as error:
        if isinstance(error, RecoveryContainmentUnavailable):
            raise
        raise RecoveryContainmentUnavailable("current recovery containment unavailable: " + str(error)) from error


def verify(enrollment_id: str, previous: RecoveryContainmentReport | object, resources: RecoveryResources) -> RecoveryContainmentReport:
    """A separate consumer repeats current reads; a saved status is no authority."""
    prior = asdict(previous) if isinstance(previous, RecoveryContainmentReport) else previous
    if not isinstance(prior, dict) or set(prior) != set(RecoveryContainmentReport.__dataclass_fields__) or prior.get("schema") != _REPORT_SCHEMA or prior.get("status") != _STATUS or any(prior.get(name) is not False for name in ("grant_issued", "history_asserted", "admission_created")):
        raise RecoveryContainmentUnavailable("previous recovery report is forged, partial or authorizing")
    if any(type(prior.get(name)) is not int or cast(int, prior[name]) <= 0 for name in ("observer_pid", "started_monotonic_ns", "finished_monotonic_ns")) or not isinstance(prior.get("observer_start_token"), str):
        raise RecoveryContainmentUnavailable("previous recovery observer lifetime differs")
    if not isinstance(prior.get("facts"), dict) or prior.get("evidence_sha256") != digest(prior["facts"]):
        raise RecoveryContainmentUnavailable("previous recovery fact digest differs")
    if not isinstance(prior.get("observation_id"), str) or str(uuid.UUID(prior["observation_id"])) != prior["observation_id"]:
        raise RecoveryContainmentUnavailable("previous recovery observation identity differs")
    # The sampling command may have exited. Its PID is attribution only; the
    # actual resource producers and holders are independently pinned and checked
    # again by observe(), and a saved positive status supplies no authority.
    current = observe(enrollment_id, resources)
    if prior["boot_id"] != current.boot_id or not cast(int, prior["started_monotonic_ns"]) <= cast(int, prior["finished_monotonic_ns"]) <= current.started_monotonic_ns or current.started_monotonic_ns - cast(int, prior["finished_monotonic_ns"]) > 600 * 1_000_000_000:
        raise RecoveryContainmentUnavailable("previous recovery sample is stale, future or from another boot")
    if prior["evidence_sha256"] != current.evidence_sha256 or json.loads(json.dumps(prior["facts"])) != json.loads(json.dumps(asdict(current.facts))):
        raise RecoveryContainmentUnavailable("previous recovery scope/source/resources/current facts differ")
    return current


def capture_recovery_original_pair(original: RecoveryOriginal, *, source: str) -> tuple[str, PriorContainer, PriorContainer]:
    """Capture real live original lifetimes without claiming a branch succeeded.

    This reads the separately selected command/configuration and the native
    supervisor owner/presence. The caller must pin its real network namespace
    while alive and install approval separately; this function does neither.
    """
    _require_capture_host()
    engine = _engine_id()
    full = _full_inspect(original.management_id)
    validate_manager_profile(original, full, source=source)
    proxy_full = _full_inspect(original.proxy_id)
    validate_proxy_profile(original, proxy_full)
    values = canonical_legacy_inspection(_inspect(original.management_id))
    if values[1:3] != [original.management_image_id, True] or values[4] != "running":
        raise RecoveryContainmentUnavailable("recovery original is not the exact live manager")
    mounts = _mounts(full)
    manager = _capture_prior_kernel(original.management_id, original.management_image_id, engine, values, mounts, require_owner_lock=True)
    manager = _capture_supervisor_identity(manager, values, mounts, Path(original.bootstrap_config.path))
    proxy_mounts = _mounts(proxy_full)
    if tuple(destination for _, destination, _ in proxy_mounts) == ("/etc/caddy/Caddyfile",):
        # Ordinary cutover and original stock Caddy retain their separate
        # capture contracts. Recovery composes them without widening either.
        proxy = capture_prior_container(original.proxy_id, original.proxy_image_id, expected_mounts=proxy_mounts)
    else:
        proxy = capture_original_caddy_container(original.proxy_id, original.proxy_image_id, expected_mounts=proxy_mounts)
    if _full_inspect(original.management_id) != full or _full_inspect(original.proxy_id) != proxy_full or _engine_id() != engine or manager.net_namespace != proxy.net_namespace:
        raise RecoveryContainmentUnavailable("recovery original changed during live capture")
    validate_manager_profile(original, full, source=source)
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip(), manager, proxy


def _input(path: Path) -> object:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise RecoveryContainmentUnavailable("recovery observation input is not canonical")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        first = os.fstat(stream.fileno())
        if not stat.S_ISREG(first.st_mode):
            raise RecoveryContainmentUnavailable("recovery observation input is not regular")
        data = stream.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES or _file_identity(first) != _file_identity(os.fstat(stream.fileno())) or _file_identity(first) != _file_identity(path.lstat()):
            raise RecoveryContainmentUnavailable("recovery observation input changed or exceeds its bound")
    return json.loads(data, object_pairs_hook=unique_pairs)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read current ordinary recovery containment; no admission or grant")
    parser.add_argument("action", choices=("observe", "verify"))
    parser.add_argument("--enrollment", required=True)
    parser.add_argument("--resources", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    setup_logger(stream=sys.stdout, log_file="/var/log/auto-coder/recovery-containment.log")
    try:
        resources = RecoveryResources.parse(_input(args.resources))
        if (args.action == "verify") != (args.previous is not None):
            raise RecoveryContainmentUnavailable("separate verification requires exactly one previous report")
        report = verify(args.enrollment, _input(args.previous), resources) if args.previous is not None else observe(args.enrollment, resources)
        logger.complete()
        print(json.dumps(asdict(report), sort_keys=True, separators=(",", ":")), flush=True)
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, subprocess.SubprocessError) as error:
        logger.bind(observation_only=True, recovery_grant=False, admission_created=False).warning("Current recovery containment unavailable: {}", str(error))
        causes = refusal_causes(error)
        for index, cause in enumerate(causes, start=1):
            logger.bind(observation_only=True, recovery_grant=False, admission_created=False).warning("Current recovery containment refusal cause {}: {}", index, json.dumps(cause, sort_keys=True, separators=(",", ":")))
        logger.complete()
        print(json.dumps({"schema": _REPORT_SCHEMA, "status": "unavailable", "grant_issued": False, "history_asserted": False, "admission_created": False, "reason": str(error), "causes": causes}, sort_keys=True), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
