"""Read-only current containment observations, never admission or launch grants.

Only independently verified enrollment, reviewed source/configuration, complete
installed routes and current kernel facts can produce an observed result.
Every consumer repeats those observations; a saved result has no authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .archived_exclusions import unique_pairs
from .containment_enrollment import EnrollmentObservation, EnrollmentUnavailable, observe_approved_enrollment
from .containment_kernel_subobservation import KernelSubObservation, observe_kernel_subcomponent
from .containment_route_manifest import RouteManifestObservation, RouteManifestUnavailable, observe_approved_route_manifest
from .docker_inspection import canonical_inspection, management_candidate_roles, unrelated_inspection, validate_container_facts
from .fresh_host_observation import _validate_host_proc_profile
from .installed_bootstrap_mount_profile import InstalledBootstrapMountProfile, inspect_installed_bootstrap_mount_profile
from .installed_bootstrap_source import InstalledBootstrapSource, inspect_installed_bootstrap_source
from .proc_observation import ProcessObservation, observe_process
from .systemd_watcher_observation import WatcherSystemdObservation, observe_watcher_unit

PROFILES = ("ordinary-v1", "quarantined-original-v1")
REPORT_SCHEMA = "current-containment-observation-v11"
OBSERVED = "observed-current-containment"
_LIMIT = 1024 * 1024
_MANAGEMENT_MOUNT_DESTINATIONS = frozenset(("/bootstrap", "/management-child-state", "/management-source", "/management-versions"))


@dataclass(frozen=True)
class ProposedScope:
    """Selection only; none of these strings constitute enrollment authority."""

    deployment_id: str
    enrollment_id: str
    authorization_id: str
    profile: str
    management_id: str
    proxy_id: str
    files: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: object) -> ProposedScope:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("proposed scope fields differ")
        for name in ("deployment_id", "enrollment_id", "authorization_id"):
            if not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value[name]):
                raise ValueError("invalid proposed " + name)
        if not isinstance(value["profile"], str) or value["profile"] not in PROFILES:
            raise ValueError("unsupported observation profile")
        for name in ("management_id", "proxy_id"):
            if not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{64}", value[name]):
                raise ValueError("exact proposed container ID required")
        if value["management_id"] == value["proxy_id"]:
            raise ValueError("distinct proposed container IDs required")
        files = value["files"]
        if not isinstance(files, list) or len(files) > 32 or any(not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute() for path in files):
            raise ValueError("bounded absolute proposed file paths required")
        if len(set(files)) != len(files) or files != sorted(files):
            raise ValueError("proposed file paths must be unique and sorted")
        return cls(value["deployment_id"], value["enrollment_id"], value["authorization_id"], value["profile"], value["management_id"], value["proxy_id"], tuple(files))


@dataclass(frozen=True)
class FileObservation:
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True)
class DockerObservation:
    engine_id: str
    container_id: str
    image_id: str
    inspection_sha256: str
    restart_policy: str
    maximum_retry_count: int
    running: bool
    status: str
    pid: int
    started_at: str
    finished_at: str
    restart_count: int
    mounts_sha256: str
    management_route_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unavailable:
    authority: str
    reason: str


@dataclass(frozen=True)
class PrerequisiteReport:
    schema: str
    status: str
    scope: ProposedScope
    scope_sha256: str
    observation_id: str
    boot_id: str
    observer_pid: int
    observer_start_token: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    files: tuple[FileObservation, ...]
    docker: tuple[DockerObservation, ...]
    unavailable: tuple[Unavailable, ...]
    processes: tuple[ProcessObservation, ...] = ()
    resource_limit: str = (
        "Current observations only; no historical continuity or launch authority. Observer-owned descriptors are released before return. "
        "Separately approved namespace and exclusive owner-lock holders must remain alive and be independently reopened by every consumer; "
        "this report retains neither resource and is never a grant."
    )
    approved_enrollment: EnrollmentObservation | None = None
    watcher_systemd: WatcherSystemdObservation | None = None
    installed_bootstrap_source: InstalledBootstrapSource | None = None
    installed_mount_profile: InstalledBootstrapMountProfile | None = None
    kernel_subobservation: KernelSubObservation | None = None
    route_manifest: RouteManifestObservation | None = None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file(path: Path) -> tuple[bytes, FileObservation]:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("file path is not canonical or has a symlink component")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("observation source is not a regular file")
        content = stream.read(_LIMIT + 1)
        after = os.fstat(stream.fileno())
    if len(content) > _LIMIT:
        raise ValueError("observation source exceeds size limit")

    def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    if identity(before) != identity(after) or identity(after) != identity(path.lstat()) or path != path.resolve(strict=True):
        raise ValueError("observation source changed during read")
    return content, FileObservation(str(path), *identity(after), hashlib.sha256(content).hexdigest())


def read_scope(path: Path) -> ProposedScope:
    content, _ = _file(path)
    return ProposedScope.parse(json.loads(content, object_pairs_hook=unique_pairs))


def _docker_read(arguments: tuple[str, ...]) -> object:
    result = subprocess.run(("docker", *arguments), capture_output=True, text=True, timeout=10, check=True)
    if len(result.stdout) > 8 * _LIMIT:
        raise ValueError("Docker observation exceeds size limit")
    return json.loads(result.stdout, object_pairs_hook=unique_pairs)


def _docker(scope: ProposedScope) -> tuple[DockerObservation, ...]:
    return _docker_selected((scope.management_id,), (scope.proxy_id,), all_bind_sources=False)


def observe_recovery_docker(management_ids: tuple[str, ...], proxy_ids: tuple[str, ...]) -> tuple[DockerObservation, ...]:
    """Enumerate the whole engine against the explicitly selected recovery union.

    This only discovers candidates. The recovery observer separately validates
    the protected selection, every object profile and all installed routes.
    """
    return _docker_selected(management_ids, proxy_ids, all_bind_sources=True)


def _docker_selected(management_ids: tuple[str, ...], proxy_ids: tuple[str, ...], *, all_bind_sources: bool) -> tuple[DockerObservation, ...]:
    selected = management_ids + proxy_ids
    if not management_ids or not proxy_ids or len(selected) > 64 or len(set(selected)) != len(selected) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in selected):
        raise ValueError("selected Docker identities are absent, duplicate or ambiguous")
    # A remote context cannot supply this host's kernel/Docker correlation.
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise ValueError("Docker endpoint override is unsupported for local observation")
    endpoint = _docker_read(("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"))
    if endpoint != "unix:///var/run/docker.sock":
        raise ValueError("Docker context is not the supported local engine socket")
    engine = _docker_read(("info", "--format", "{{json .ID}}"))
    if not isinstance(engine, str) or not engine or len(engine) > 128:
        raise ValueError("Docker engine identity unavailable")
    # Enumerate the engine, not an operator/label-filtered selection. Unknown
    # objects remain visible; enumeration does not establish their enrollment.
    containers = _docker_ids()
    if not set(selected) <= set(containers):
        raise ValueError("Selected Docker objects are absent from the complete engine inventory")
    observations = []
    inspected: dict[str, dict[str, object]] = {}
    for container in containers:
        raw = _docker_read(("inspect", "--type", "container", container))
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict) or raw[0].get("Id") != container:
            raise ValueError("Docker container identity differs")
        raw = [canonical_inspection(raw[0])]
        inspected[container] = raw[0]
        image, state, host, mounts, policy, restart_count = validate_container_facts(raw[0])
        # Bind the complete inspection, including command, security, mounts,
        # restart policy, and lifetime, without exposing environment secrets.
        observations.append(DockerObservation(engine, container, image, _digest(raw), policy["Name"], policy["MaximumRetryCount"], state["Running"], state["Status"], state["Pid"], state["StartedAt"], state["FinishedAt"], restart_count, _digest(mounts)))
    # A proposed pair cannot exclude other current Docker objects that share
    # its exact images, an installed management command or its network namespace.
    # This is candidate discovery, not proof that all ordinary controllers are
    # known; the separate installed-route inventory is still required.
    management_images = {inspected[identifier]["Image"] for identifier in management_ids}
    proxy_images = {inspected[identifier]["Image"] for identifier in proxy_ids}
    management_mounts: list[object] = []
    for identifier in management_ids:
        selected_mounts = inspected[identifier]["Mounts"]
        if not isinstance(selected_mounts, list):
            raise ValueError("Selected management mount inventory is unavailable")
        management_mounts.extend(selected_mounts)
    management_sources = tuple(source for mount in management_mounts if isinstance(mount, dict) and mount.get("Type") == "bind" and (all_bind_sources or mount.get("Destination") in _MANAGEMENT_MOUNT_DESTINATIONS) and isinstance(source := mount.get("Source"), str) and os.path.isabs(source))

    classified = []
    for item in observations:
        obj = inspected[item.container_id]
        roles = management_candidate_roles(
            obj,
            item.container_id,
            management_ids,
            proxy_ids,
            tuple(management_images),
            tuple(proxy_images),
            management_sources,
        )
        if roles or "ExecIDs" not in obj:
            classified.append(replace(item, management_route_roles=tuple(roles)))
        else:
            # An unrelated container's short-lived `docker exec` identifiers
            # are not installed controller configuration or a management route.
            # Keep the complete Docker inspection pinned for any selected or
            # candidate controller; normalize only this unrelated volatile key.
            normalized = unrelated_inspection(obj)
            classified.append(replace(item, inspection_sha256=_digest([normalized]), management_route_roles=()))
    if _docker_ids() != containers:
        raise ValueError("Docker engine inventory changed during observation")
    if _docker_read(("info", "--format", "{{json .ID}}")) != engine:
        raise ValueError("Docker engine changed during observation")
    if _docker_read(("context", "inspect", "--format", "{{json .Endpoints.docker.Host}}")) != endpoint:
        raise ValueError("Docker context changed during observation")
    return tuple(classified)


def _docker_ids() -> tuple[str, ...]:
    result = subprocess.run(("docker", "container", "ls", "--all", "--quiet", "--no-trunc"), capture_output=True, text=True, timeout=10, check=True)
    # Keep the serialized evidence readable within the verifier's 1 MiB bound.
    if len(result.stdout) > 512 * 65:
        raise ValueError("Docker inventory exceeds size limit")
    ids = result.stdout.splitlines()
    if len(ids) > 512 or len(set(ids)) != len(ids) or any(not re.fullmatch(r"[0-9a-f]{64}", container) for container in ids):
        raise ValueError("Docker inventory contains ambiguous container identities")
    return tuple(sorted(ids))


def observe(scope: ProposedScope) -> PrerequisiteReport:
    """Observe available facts without converting partial evidence into closure."""
    scope = ProposedScope.parse(json.loads(json.dumps(asdict(scope))))
    started = time.monotonic_ns()
    unavailable = [
        Unavailable("ordinary-route-closure", "No authoritative installed-route inventory is available for watcher/reconcile, services/timers, socket activation, Docker restart, configured local/remote scripts, repair/recreate and alternative roots; none are excluded from scope."),
        Unavailable("original-source-profile", "The approved current original source, exact effective configuration and reviewed lock-before-launch mapping are not independently established for " + scope.profile + "."),
        Unavailable("kernel-containment", "No original task/boot lifetime capture with retained namespace object pins and exact owner-file fence is supplied; stopped Docker state and saved namespace inode strings cannot establish absence."),
    ]
    approved_enrollment = None
    try:
        # Root approval selects a present-time deployment, not a historical
        # execution claim. Every technical predicate is inspected separately.
        approved_enrollment = observe_approved_enrollment(scope)
    except (EnrollmentUnavailable, OSError, ValueError) as exc:
        unavailable.append(Unavailable("approved-enrollment-component", str(exc)))
    if approved_enrollment is None:
        unavailable.insert(
            0,
            Unavailable(
                "deployment-enrollment",
                "No protected approved present-time enrollment or authoritative original deployment provenance was verified; supplied identities and file lists select scope only.",
            ),
        )
    else:
        unavailable.insert(
            0,
            Unavailable(
                "deployment-enrollment",
                "The protected present-time enrollment was read, but complete installed controller membership and current technical closure still require independent source, route and kernel verification.",
            ),
        )
    installed_bootstrap_source = None
    installed_mount_profile = None
    if scope.profile == "quarantined-original-v1":
        unavailable.append(
            Unavailable(
                "quarantined-original-profile",
                "The current original bootstrap file mounts, no-new-privileges, support-directory execution mapping and original stopped-lifetime profile are not completely verified; replacement admission is unchanged.",
            )
        )
    watcher_systemd = None
    try:
        watcher_systemd = observe_watcher_unit()
        if not watcher_systemd.inhibited:
            unavailable.append(Unavailable("watcher-systemd", "The installed management proxy watcher unit is not independently confirmed masked, inactive and free of pending daemon-reload changes."))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        unavailable.append(Unavailable("watcher-systemd", "The fixed installed management proxy watcher unit cannot be independently observed (" + type(exc).__name__ + ")."))
    boot_id, start_token = "", ""
    try:
        boot_id = str(uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
        fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        start_token = fields[19]
        if not start_token.isdecimal() or int(start_token) <= 0:
            raise ValueError("observer start token unavailable")
        if os.geteuid() != 0:
            raise ValueError("initial-namespace host root is required")
        _validate_host_proc_profile(os.readlink("/proc/self/ns/pid"), Path("/proc/self/uid_map").read_text(), Path("/proc/self/mountinfo").read_text())
    except (OSError, ValueError, IndexError) as exc:
        unavailable.append(Unavailable("host-observation", str(exc)))
    files = []
    for path in scope.files:
        try:
            files.append(_file(Path(path))[1])
        except (OSError, ValueError) as exc:
            unavailable.append(Unavailable("file:" + path, str(exc)))
    docker: tuple[DockerObservation, ...] = ()
    try:
        before_docker = _docker(scope)
        if _docker(scope) != before_docker:
            raise ValueError("Docker configuration or lifetime changed during observation")
        docker = before_docker
        for item in docker:
            if item.restart_policy != "no" and item.management_route_roles:
                unavailable.append(Unavailable("docker-restart:" + item.container_id, "A selected or candidate management/proxy Docker object has a configured " + item.restart_policy + " restart route; stopped state does not prove inhibition or authorize exclusion from deployment scope."))
            if item.container_id not in (scope.management_id, scope.proxy_id) and item.management_route_roles:
                unavailable.append(
                    Unavailable(
                        "docker-alternative-management:" + item.container_id,
                        "An unselected Docker object matches the proposed management/proxy image, command or network route (" + ",".join(item.management_route_roles) + "); its independently approved installed-controller membership and inhibition remain unproven. Current state: " + item.status + ".",
                    )
                )
        if approved_enrollment is not None and isinstance(approved_enrollment, EnrollmentObservation):
            enrolled = approved_enrollment.enrollment
            selected = {item.container_id: item for item in docker}
            management = selected[scope.management_id]
            proxy = selected[scope.proxy_id]
            if management.engine_id != enrolled.engine_id or proxy.engine_id != enrolled.engine_id or management.image_id != enrolled.management_image_id or proxy.image_id != enrolled.proxy_image_id:
                unavailable.append(Unavailable("enrollment-live-docker", "The independently inspected Docker engine or selected current image identities differ from the protected present-time enrollment."))
            else:
                try:
                    installed_bootstrap_source = inspect_installed_bootstrap_source(approved_enrollment)
                except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
                    unavailable.append(Unavailable("installed-bootstrap-source", "Current approved " + scope.profile + " source cannot be matched to the reviewed Docker image-file bytes (" + type(exc).__name__ + ")."))
                if installed_bootstrap_source is not None:
                    try:
                        installed_mount_profile = inspect_installed_bootstrap_mount_profile(approved_enrollment, {item.container_id: item.inspection_sha256 for item in docker})
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        unavailable.append(Unavailable("installed-bootstrap-mount-profile", "Original effective bootstrap command, protected configuration and bind-mount readback is unavailable (" + type(exc).__name__ + ")."))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Do not print raw Docker stderr, environment or configuration bytes.
        reason = "Docker read-only inspection unavailable (" + type(exc).__name__ + ")"
        if isinstance(exc, ValueError):
            reason = str(exc)
        elif isinstance(exc, subprocess.CalledProcessError):
            diagnostic = (exc.stderr or "").lower()
            if "permission denied" in diagnostic:
                reason = "Local Docker inspection permission denied; access to the authoritative engine is required."
            elif "cannot connect" in diagnostic or "is the docker daemon running" in diagnostic:
                reason = "Local Docker engine is unavailable; an inspectable authoritative engine is required."
            elif "no such" in diagnostic:
                reason = "Selected Docker context or container object is unavailable; exact inspectable original objects are required."
        unavailable.append(Unavailable("docker", reason))
    for observed in files:
        try:
            if _file(Path(observed.path))[1] != observed:
                raise ValueError("file identity or content changed during observation")
        except (OSError, ValueError) as exc:
            unavailable.append(Unavailable("file:" + observed.path, str(exc)))
    # Docker PIDs select current facts only; they do not identify escaped tasks
    # or establish correspondence with an original manager/proxy lifetime.
    processes = tuple(observe_process(pid) for pid in sorted({item.pid for item in docker if item.pid > 0}))
    process_by_pid = {item.pid: item for item in processes}
    for container in docker:
        if not container.running:
            continue
        process = process_by_pid[container.pid]
        if process.unavailable or not process.start_token or not process.boot_id or (boot_id and process.boot_id != boot_id):
            unavailable.append(Unavailable("docker-process:" + container.container_id, "Docker-selected live process could not be independently correlated to an inspectable current Linux lifetime."))
    route_manifest = None
    if isinstance(approved_enrollment, EnrollmentObservation) and docker:
        try:
            route_manifest = observe_approved_route_manifest(approved_enrollment, docker)
            unavailable = [
                (
                    Unavailable("ordinary-route-closure", "The protected exhaustive-route component was reobserved, but user-session controllers, opaque alternate roots and full original-profile route authority remain unverified; a manifest alone cannot prove these technical exclusions.")
                    if item.authority == "ordinary-route-closure"
                    else item
                )
                for item in unavailable
            ]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            unavailable.append(
                Unavailable(
                    "approved-route-manifest",
                    (
                        "The installed exhaustive route authority or its independently observed inhibition is unavailable (" + type(exc).__name__ + "): " + str(exc)
                        if isinstance(exc, RouteManifestUnavailable)
                        else "The installed exhaustive route authority or independent route readback is unavailable (" + type(exc).__name__ + ")."
                    ),
                )
            )
    kernel_subobservation = None
    if approved_enrollment is not None and isinstance(approved_enrollment, EnrollmentObservation) and docker:
        try:
            kernel_subobservation = observe_kernel_subcomponent(approved_enrollment, installed_profile=installed_mount_profile)
            if not kernel_subobservation.external_namespace_pin_verified:
                unavailable.append(Unavailable("kernel-subcomponent-unpinned", "Current old-cgroup, task-namespace and host-listener checks passed, but the original namespace object is not retained by an independently re-openable pin. These facts cannot establish kernel containment."))
            if not kernel_subobservation.external_owner_fence_verified:
                unavailable.append(Unavailable("kernel-subcomponent-unfenced", "The original owner-lock inode was read, but no separately authorized external holder maintained an exclusive kernel flock throughout the read. File identity alone cannot prevent a new owner from launching."))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            unavailable.append(Unavailable("kernel-subcomponent", "Original lifetime, all-task namespace or listener reobservation failed (" + type(exc).__name__ + ")."))
    # Compose actual predicates rather than treating an approval record or
    # several successful components as an independently closed deployment.
    source_closed = installed_bootstrap_source is not None and installed_bootstrap_source.reviewed_lock_before_launch and installed_mount_profile is not None
    routes_closed = route_manifest is not None and getattr(route_manifest, "closed_profile", False) is True
    kernel_closed = (
        kernel_subobservation is not None
        and kernel_subobservation.original_cgroups_empty
        and kernel_subobservation.unpinned_network_task_scan_absent
        and kernel_subobservation.host_listener_absent
        and kernel_subobservation.external_namespace_pin_verified
        and kernel_subobservation.external_owner_fence_verified
        and installed_mount_profile is not None
    )
    established = set()
    if source_closed:
        established.add("original-source-profile")
    if routes_closed:
        established.add("ordinary-route-closure")
    if kernel_closed:
        established.add("kernel-containment")
    if source_closed and kernel_closed:
        established.add("quarantined-original-profile")
    if isinstance(approved_enrollment, EnrollmentObservation) and source_closed and routes_closed and kernel_closed:
        established.add("deployment-enrollment")
    unavailable = [item for item in unavailable if item.authority not in established]
    if not unavailable:
        # Close the observation window with fresh reads of every authority.
        # The external holders are checked last; they are not owned by us and
        # must be checked again by a later consumer even if this read succeeds.
        assert isinstance(approved_enrollment, EnrollmentObservation)
        assert kernel_subobservation is not None
        try:
            repeated_enrollment = observe_approved_enrollment(scope)
            repeated_docker = _docker(scope)
            if repeated_enrollment != approved_enrollment or repeated_docker != docker:
                raise ValueError("deployment enrollment or Docker scope changed during complete observation")
            if observe_watcher_unit() != watcher_systemd:
                raise ValueError("installed watcher changed during complete observation")
            if inspect_installed_bootstrap_source(repeated_enrollment) != installed_bootstrap_source:
                raise ValueError("installed original source changed during complete observation")
            repeated_profile = inspect_installed_bootstrap_mount_profile(repeated_enrollment, {item.container_id: item.inspection_sha256 for item in repeated_docker})
            if repeated_profile != installed_mount_profile or observe_approved_route_manifest(repeated_enrollment, repeated_docker) != route_manifest:
                raise ValueError("installed original profile or ordinary routes changed during complete observation")
            repeated_kernel = observe_kernel_subcomponent(repeated_enrollment, installed_profile=repeated_profile)
            if _stable_kernel(asdict(repeated_kernel)) != _stable_kernel(asdict(kernel_subobservation)):
                raise ValueError("original kernel scope or retained holders changed during complete observation")
            if any(_file(Path(item.path))[1] != item for item in files):
                raise ValueError("selected original file changed during complete observation")
        except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
            unavailable.append(Unavailable("complete-observation-race", "Final independent scope readback failed (" + type(exc).__name__ + "): " + (str(exc) if isinstance(exc, ValueError) else "underlying authority unavailable")))
    return PrerequisiteReport(
        REPORT_SCHEMA,
        "unavailable" if unavailable else OBSERVED,
        scope,
        _digest(asdict(scope)),
        str(uuid.uuid4()),
        boot_id,
        os.getpid(),
        start_token,
        started,
        time.monotonic_ns(),
        tuple(files),
        docker,
        tuple(unavailable),
        processes,
        approved_enrollment=approved_enrollment,
        watcher_systemd=watcher_systemd,
        installed_bootstrap_source=installed_bootstrap_source,
        installed_mount_profile=installed_mount_profile,
        kernel_subobservation=kernel_subobservation,
        route_manifest=route_manifest,
    )


def _stable_kernel(value: object) -> object:
    """Compare kernel identities, independently validating each sample window."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(KernelSubObservation.__dataclass_fields__):
        raise ValueError("saved kernel observation schema differs")
    start, finish = value.get("started_monotonic_ns"), value.get("finished_monotonic_ns")
    if type(start) is not int or type(finish) is not int or not 0 < start <= finish:
        raise ValueError("kernel observation interval is malformed")
    return {key: item for key, item in value.items() if key not in ("started_monotonic_ns", "finished_monotonic_ns")}


def verify(scope: ProposedScope, previous: object) -> PrerequisiteReport:
    """Reobserve independently; a saved report is never a current verdict."""
    current = observe(scope)
    reasons = list(current.unavailable)
    if not isinstance(previous, dict) or set(previous) != set(PrerequisiteReport.__dataclass_fields__) or previous.get("schema") != current.schema or previous.get("status") not in ("unavailable", OBSERVED):
        reasons.append(Unavailable("saved-report", "Unsupported or authorizing saved report; an observation is never a launch grant."))
    else:
        current_value = json.loads(json.dumps(asdict(current)))
        for field in ("scope", "scope_sha256", "boot_id", "files", "docker", "processes", "approved_enrollment", "watcher_systemd", "installed_bootstrap_source", "installed_mount_profile", "route_manifest", "unavailable", "resource_limit"):
            if previous[field] != current_value[field]:
                reasons.append(Unavailable("saved-report:" + field, "Independent current observation differs from saved report."))
        try:
            if _stable_kernel(previous["kernel_subobservation"]) != _stable_kernel(current_value["kernel_subobservation"]):
                reasons.append(Unavailable("saved-report:kernel_subobservation", "Independent current observation differs from saved report."))
        except ValueError as exc:
            reasons.append(Unavailable("saved-report:kernel_subobservation", str(exc)))
        start, finish = previous.get("started_monotonic_ns"), previous.get("finished_monotonic_ns")
        if type(start) is not int or type(finish) is not int or not 0 < start <= finish <= current.started_monotonic_ns:
            reasons.append(Unavailable("saved-report:interval", "Saved observation interval is malformed or is not earlier than the consumer."))
        if (previous["status"] == OBSERVED) != (previous["unavailable"] == []):
            reasons.append(Unavailable("saved-report:status", "Saved observation status contradicts its unavailable predicates."))
    return replace(current, status="unavailable" if reasons else OBSERVED, unavailable=tuple(reasons))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only current containment observations; never a launch grant or historical continuity")
    parser.add_argument("command", choices=("observe", "verify"))
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    if (args.command == "verify") != (args.previous is not None):
        parser.error("verify requires --previous; observe does not accept it")
    try:
        scope = read_scope(args.scope)
        if args.command == "verify":
            previous = json.loads(_file(args.previous)[0], object_pairs_hook=unique_pairs)
            result = verify(scope, previous)
        else:
            result = observe(scope)
        print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
        return 0 if result.status == OBSERVED else 2
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
