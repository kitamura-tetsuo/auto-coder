"""Non-authorizing, read-only prerequisite observations for current containment.

There is deliberately no successful containment verdict in this adapter yet.
The repository has no authoritative closed deployment enrollment reader or
reviewed original quarantine profile. Partial observations must not hide either
missing prerequisite behind operator-supplied IDs, lists, or source hashes.
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
from .fresh_host_observation import _validate_host_proc_profile
from .installed_bootstrap_source import InstalledBootstrapSource, inspect_installed_bootstrap_source
from .proc_observation import ProcessObservation, observe_process
from .systemd_watcher_observation import WatcherSystemdObservation, observe_watcher_unit

PROFILES = ("ordinary-v1", "quarantined-original-v1")
_LIMIT = 1024 * 1024


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
    resource_limit: str = "All read descriptors are released before return; no namespace pin or owner fence is retained."
    approved_enrollment: EnrollmentObservation | None = None
    watcher_systemd: WatcherSystemdObservation | None = None
    installed_bootstrap_source: InstalledBootstrapSource | None = None


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
    if not {scope.management_id, scope.proxy_id} <= set(containers):
        raise ValueError("Selected Docker objects are absent from the complete engine inventory")
    observations = []
    inspected: dict[str, dict[str, object]] = {}
    for container in containers:
        raw = _docker_read(("inspect", "--type", "container", container))
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict) or raw[0].get("Id") != container:
            raise ValueError("Docker container identity differs")
        inspected[container] = raw[0]
        image = raw[0].get("Image")
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Docker image identity unavailable")
        state, host, mounts = raw[0].get("State"), raw[0].get("HostConfig"), raw[0].get("Mounts")
        if not isinstance(state, dict) or not isinstance(host, dict) or not isinstance(mounts, list):
            raise ValueError("Docker state, host configuration or mounts unavailable")
        policy = host.get("RestartPolicy")
        if not isinstance(policy, dict) or policy.get("Name") not in ("no", "always", "unless-stopped", "on-failure") or type(policy.get("MaximumRetryCount")) is not int or policy["MaximumRetryCount"] < 0:
            raise ValueError("Docker restart policy unavailable or unsupported")
        if type(state.get("Running")) is not bool or type(state.get("Pid")) is not int or state["Pid"] < 0 or state.get("Status") not in ("created", "running", "paused", "restarting", "removing", "exited", "dead"):
            raise ValueError("Docker lifetime state unavailable or unsupported")
        if (state["Pid"] > 0) != state["Running"]:
            raise ValueError("Docker running/PID relationship is contradictory")
        if any(not isinstance(state.get(key), str) or not state[key] or len(state[key]) > 128 for key in ("StartedAt", "FinishedAt")) or type(raw[0].get("RestartCount")) is not int or raw[0]["RestartCount"] < 0:
            raise ValueError("Docker lifetime identity unavailable")
        # Bind the complete inspection, including command, security, mounts,
        # restart policy, and lifetime, without exposing environment secrets.
        observations.append(DockerObservation(engine, container, image, _digest(raw), policy["Name"], policy["MaximumRetryCount"], state["Running"], state["Status"], state["Pid"], state["StartedAt"], state["FinishedAt"], raw[0]["RestartCount"], _digest(mounts)))
    # A proposed pair cannot exclude other current Docker objects that share
    # its exact images, an installed management command or its network namespace.
    # This is candidate discovery, not proof that all ordinary controllers are
    # known; the separate installed-route inventory is still required.
    management_image = inspected[scope.management_id]["Image"]
    proxy_image = inspected[scope.proxy_id]["Image"]
    classified = []
    for item in observations:
        obj = inspected[item.container_id]
        config, host = obj.get("Config"), obj["HostConfig"]
        if not isinstance(config, dict) or not isinstance(host, dict):
            raise ValueError("Docker effective command or network configuration unavailable")
        command = config.get("Cmd")
        entrypoint = config.get("Entrypoint")
        if command is not None and (not isinstance(command, list) or len(command) > 64 or any(not isinstance(part, str) or len(part) > 4096 for part in command)):
            raise ValueError("Docker effective command is ambiguous")
        if entrypoint is not None and (not isinstance(entrypoint, list) or len(entrypoint) > 64 or any(not isinstance(part, str) or len(part) > 4096 for part in entrypoint)):
            raise ValueError("Docker entrypoint is ambiguous")
        network_mode = host.get("NetworkMode")
        if network_mode is not None and (not isinstance(network_mode, str) or len(network_mode) > 256):
            raise ValueError("Docker network mode is ambiguous")
        arguments = tuple((entrypoint or []) + (command or []))
        roles = []
        if item.container_id == scope.management_id:
            roles.append("selected-management")
        elif item.image_id == management_image:
            roles.append("same-management-image")
        if item.container_id == scope.proxy_id:
            roles.append("selected-proxy")
        elif item.image_id == proxy_image:
            roles.append("same-proxy-image")
        if "auto_coder.management.bootstrap_supervisor" in arguments or ("auto_coder.cli" in arguments and "management" in arguments and "serve" in arguments):
            roles.append("management-command")
        if network_mode == "container:" + scope.management_id:
            roles.append("selected-management-network")
        classified.append(replace(item, management_route_roles=tuple(roles)))
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
        Unavailable("original-source-profile", "No reviewed exact original source/configuration binding is registered for " + scope.profile + "; current installation bytes cannot establish original lock-before-launch evidence."),
        Unavailable("kernel-containment", "No original task/boot lifetime capture with retained namespace object pins and exact owner-file fence is supplied; stopped Docker state and saved namespace inode strings cannot establish absence."),
    ]
    approved_enrollment = None
    try:
        # A protected enrollment is evidence of a separately installed record.
        # The original installed execution, route and kernel facts still need
        # independent verification; never remove those refusal reasons here.
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
                "The protected present-time enrollment was read, but independent original execution, complete installed controller membership and current technical closure are not established by that record.",
            ),
        )
    installed_bootstrap_source = None
    if scope.profile == "quarantined-original-v1":
        unavailable.append(Unavailable("quarantined-original-profile", "Exact original bootstrap file mounts, no-new-privileges setting and support-mount purpose/source/destination/read-only execution binding are not registered; replacement admission is unchanged."))
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
            if item.restart_policy != "no":
                unavailable.append(Unavailable("docker-restart:" + item.container_id, "Engine inventory contains a configured " + item.restart_policy + " restart route; stopped state does not prove inhibition or authorize exclusion from deployment scope."))
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
                    unavailable.append(Unavailable("installed-bootstrap-source", "Current approved ordinary source cannot be matched to the reviewed Docker image-file bytes (" + type(exc).__name__ + ")."))
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
    return PrerequisiteReport(
        "current-containment-prerequisites-v6",
        "unavailable",
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
    )


def verify(scope: ProposedScope, previous: object) -> PrerequisiteReport:
    """Reobserve independently; a saved report is never a current verdict."""
    current = observe(scope)
    reasons = list(current.unavailable)
    if not isinstance(previous, dict) or set(previous) != set(PrerequisiteReport.__dataclass_fields__) or previous.get("schema") != current.schema or previous.get("status") != "unavailable":
        reasons.append(Unavailable("saved-report", "Unsupported or authorizing saved report; no containment success schema exists."))
    else:
        current_value = json.loads(json.dumps(asdict(current)))
        for field in ("scope", "scope_sha256", "boot_id", "files", "docker", "processes", "approved_enrollment", "watcher_systemd", "installed_bootstrap_source", "unavailable", "resource_limit"):
            if previous[field] != current_value[field]:
                reasons.append(Unavailable("saved-report:" + field, "Independent current observation differs from saved report."))
    return replace(current, unavailable=tuple(reasons))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only current containment prerequisites; never a launch grant or containment success")
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
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
