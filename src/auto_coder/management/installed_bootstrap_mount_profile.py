"""Read back the original bootstrap command, configuration and bind mounts.

This verifies a narrow, separately enrolled original manager configuration.
It does not establish a complete original-source profile, controller closure,
kernel containment or a replacement launch grant.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .archived_exclusions import unique_pairs
from .containment_enrollment import EnrollmentObservation, _file_identity, _protected, _trusted_ancestors
from .docker_inspection import canonical_inspection
from .fresh_host import _evidence
from .fresh_host_observation import PriorContainer, _supported_prior_command
from .fresh_initialization import load_fresh_initialization, read_fresh_status

_MAX_DOCKER_BYTES = 8 * 1024 * 1024
_FRESH_COMMAND = ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json", "--fresh-initialization"]


@dataclass(frozen=True)
class CompletedFreshInitialization:
    """Completed first-launch records, distinct from the current container."""

    request_id: str
    initialized_container_id: str
    owner_incarnation: str
    observation_id: str
    records_sha256: str


@dataclass(frozen=True)
class InstalledBootstrapMountProfile:
    """Current selected original command/config/mount bindings, not closure."""

    management_id: str
    proxy_id: str
    profile: str
    management_inspection_sha256: str
    proxy_inspection_sha256: str
    management_original_mounts_sha256: str
    proxy_original_mounts_sha256: str
    bootstrap_config_sha256: str
    management_config_sha256: str
    bootstrap_config_in_container: str
    management_config_in_container: str
    original_owner_lock_device: int
    original_owner_lock_inode: int
    support_mount_manifest_sha256: str = ""
    support_mount_directory_device: int = 0
    support_mount_directory_inode: int = 0
    proxy_config_sha256: str = ""
    completed_fresh_initialization: CompletedFreshInitialization | None = None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _inspect(container_id: str) -> dict[str, object]:
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise ValueError("remote Docker context is unsupported for original profile readback")
    result = subprocess.run(("docker", "inspect", "--type", "container", container_id), capture_output=True, text=True, check=True, timeout=10)
    if len(result.stdout) > _MAX_DOCKER_BYTES:
        raise ValueError("original profile Docker inspection exceeds size bound")
    decoded = json.loads(result.stdout, object_pairs_hook=unique_pairs)
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict) or decoded[0].get("Id") != container_id:
        raise ValueError("original profile Docker identity differs")
    return canonical_inspection(decoded[0])


def _mounts(inspect: dict[str, object]) -> tuple[tuple[str, str, bool], ...]:
    raw = inspect.get("Mounts")
    if not isinstance(raw, list) or len(raw) > 64:
        raise ValueError("original profile mounts are unavailable")
    mounts: list[tuple[str, str, bool]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("Type") != "bind" or not isinstance(item.get("Source"), str) or not isinstance(item.get("Destination"), str) or type(item.get("RW")) is not bool:
            raise ValueError("original profile bind mount is unsupported")
        source, destination = item["Source"], item["Destination"]
        if not source.startswith("/") or not destination.startswith("/") or ".." in Path(destination).parts or ".." in Path(source).parts:
            raise ValueError("original profile mount path is ambiguous")
        mounts.append((source, destination, item["RW"]))
    if len({destination for _, destination, _ in mounts}) != len(mounts):
        raise ValueError("original profile mount destination is ambiguous")
    return tuple(sorted(mounts))


def _original_config(record: EnrollmentObservation, purpose: str) -> tuple[Path, bytes, str]:
    items = [item for item in record.enrollment.original_files if item.purpose == purpose]
    if len(items) != 1:
        raise ValueError("approved original configuration binding is missing: " + purpose)
    item = items[0]
    content, info = _protected(Path(item.path), uid=0, size_limit=1024 * 1024)
    digest = hashlib.sha256(content).hexdigest()
    if (digest, info.st_dev, info.st_ino) != (item.sha256, item.device, item.inode) or dict(record.original_file_sha256).get(purpose) != digest:
        raise ValueError("original configuration changed since protected enrollment: " + purpose)
    return Path(item.path), content, digest


def _support_mount_directory(content: bytes, expected_source: str, *, uid: int = 0) -> tuple[int, int]:
    """Check an independently installed protected support-mount descriptor."""
    try:
        value = json.loads(content, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("approved quarantine support mount descriptor is unreadable") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "purpose", "source", "destination", "read_only", "device", "inode"}:
        raise ValueError("approved quarantine support mount descriptor differs")
    if value["schema"] != "approved-quarantine-support-directory-v1" or value["purpose"] != "non-executed-bootstrap-reference" or value["destination"] != "/management-source" or value["read_only"] is not True or value["source"] != expected_source:
        raise ValueError("approved quarantine support mount role or effect differs")
    if any(type(value[key]) is not int or value[key] <= 0 for key in ("device", "inode")):
        raise ValueError("approved quarantine support mount directory identity differs")
    path = Path(expected_source)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("approved quarantine support directory is not canonical")
    _trusted_ancestors(path.parent, uid=uid)
    first = path.lstat()
    if not stat.S_ISDIR(first.st_mode) or first.st_uid != uid or first.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or (first.st_dev, first.st_ino) != (value["device"], value["inode"]):
        raise ValueError("approved quarantine support directory identity or protection differs")
    last = path.lstat()
    if _file_identity(first) != _file_identity(last) or path != path.resolve(strict=True):
        raise ValueError("approved quarantine support directory changed during readback")
    return first.st_dev, first.st_ino


def _unshadowed_bootstrap_execution(config: dict[str, object], mounts: tuple[tuple[str, str, bool], ...]) -> None:
    """Keep the module command bound to the reviewed image's Python package."""
    environment = config.get("Env", [])
    if not isinstance(environment, list) or len(environment) > 256 or any(not isinstance(item, str) or "=" not in item for item in environment):
        raise ValueError("original bootstrap environment is ambiguous")
    names = [item.split("=", 1)[0] for item in environment]
    if len(set(names)) != len(names):
        raise ValueError("original bootstrap environment has duplicate names")
    for item in environment:
        name, value = item.split("=", 1)
        if name in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE") and value or name == "HOME" and value != "/root" or name == "PATH" and value.split(":")[0] != "/opt/auto-coder/.venv/bin":
            raise ValueError("original bootstrap Python resolution differs from the reviewed image")
    protected = tuple(Path(path) for path in ("/opt/auto-coder", "/usr/local", "/usr/lib", "/lib", "/lib64", "/workspace", "/root/.local"))
    for _, destination, _ in mounts:
        target = Path(destination)
        if any(target == root or target in root.parents or root in target.parents for root in protected):
            raise ValueError("original bootstrap interpreter, package or working directory is shadowed")


def _validated_proxy_configuration(record: EnrollmentObservation, proxy: dict[str, object]) -> str:
    """Bind the actual original Caddy command to its protected file mount."""
    config = proxy.get("Config")
    if not isinstance(config, dict) or config.get("Entrypoint") != ["caddy"] or config.get("Cmd") != ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"] or config.get("WorkingDir") != "/srv" or config.get("User") != "":
        raise ValueError("original proxy command/configuration profile is unsupported")
    path, _, digest = _original_config(record, "proxy-config")
    mounts = _mounts(proxy)
    expected = (str(path), "/etc/caddy/Caddyfile", False)
    if expected not in mounts:
        raise ValueError("original proxy configuration is not its protected read-only file mount")
    selected = Path(expected[1])
    for mount in mounts:
        target = Path(mount[1])
        if mount != expected and (target == selected or target in selected.parents or selected in target.parents):
            raise ValueError("original proxy configuration mount is shadowed")
    return digest


def _completed_fresh_initialization(
    config: dict[str, object],
    mounted: dict[str, tuple[str, bool]],
    bootstrap_digest: str,
    management_digest: str,
    *,
    uid: int = 0,
) -> CompletedFreshInitialization:
    """Read completed initialization without equating it to a later lifetime."""
    state = Path(mounted["/management-child-state"][0])
    names = ("fresh-initialization.json", "fresh-launch-claimed.json", "fresh-launched.json", "fresh-outcome.json")
    before = tuple(_protected(state / name, uid=uid, size_limit=65536) for name in names)
    intent, _ = load_fresh_initialization(state, host=True)
    status = read_fresh_status(state, host=True)
    if status.get("status") != "succeeded":
        raise ValueError("original fresh initialization has no completed authenticated outcome")
    if (intent.new_instance, intent.state_root, intent.journal_root, intent.oauth_root, intent.runtime_reference) != (config.get("instance_id"), config.get("state_root"), config.get("journal_root"), config.get("oauth_root"), config.get("initial_runtime")):
        raise ValueError("completed original initialization/configuration identity differs")
    if intent.bootstrap_config_sha256 != bootstrap_digest or intent.management_config_sha256 != management_digest:
        raise ValueError("completed original initialization configuration bytes differ")
    roots = (
        (intent.state_root, intent.host_state_root, intent.state_device, intent.state_inode),
        (intent.journal_root, intent.host_journal_root, intent.journal_device, intent.journal_inode),
        (intent.oauth_root, intent.host_oauth_root, intent.oauth_device, intent.oauth_inode),
    )
    for destination, source, device, inode in roots:
        if mounted.get(destination) != (source, True):
            raise ValueError("completed original initialization mount mapping differs")
        path = Path(source)
        info = path.lstat()
        if path != path.resolve(strict=True) or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or (info.st_dev, info.st_ino) != (device, inode):
            raise ValueError("completed original initialization mounted root identity differs")
        if any(target.startswith(destination + "/") for target in mounted):
            raise ValueError("completed original initialization mounted root is shadowed")
    if config.get("fresh_guard_path") != "/management-initialization/fresh-guard.sock" or config.get("fresh_prior_lock_path") != "/management-prior-owner.lock":
        raise ValueError("completed original initialization guard/owner mapping differs")
    for destination, device, inode, directory in (("/management-initialization", intent.guard_device, intent.guard_inode, True), ("/management-prior-owner.lock", intent.owner_lock_device, intent.owner_lock_inode, False)):
        source, writable = mounted.get(destination, ("", True))
        if writable or not source or any(target.startswith(destination + "/") for target in mounted):
            raise ValueError("completed original initialization guard/owner mount differs")
        path = Path(source)
        info = path.lstat()
        if path != path.resolve(strict=True) or info.st_uid != uid or (info.st_dev, info.st_ino) != (device, inode) or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)):
            raise ValueError("completed original initialization guard/owner identity differs")
    after = tuple(_protected(state / name, uid=uid, size_limit=65536) for name in names)
    if tuple((content, _file_identity(info)) for content, info in before) != tuple((content, _file_identity(info)) for content, info in after):
        raise ValueError("completed original initialization records changed during readback")
    incarnation, observation = status.get("owner_incarnation"), status.get("observation_id")
    if not isinstance(incarnation, str) or not isinstance(observation, str):
        raise ValueError("completed original initialization launch identity is unavailable")
    records = tuple((name, hashlib.sha256(content).hexdigest(), _file_identity(info)) for name, (content, info) in zip(names, before))
    return CompletedFreshInitialization(intent.request_id, intent.new_container_id, incarnation, observation, _digest(records))


def _validated_ordinary_profile(
    record: EnrollmentObservation,
    manager: dict[str, object],
    proxy: dict[str, object],
    prior_manager: PriorContainer,
    prior_proxy: PriorContainer,
) -> tuple[str, str, CompletedFreshInitialization | None]:
    """Bind actual Docker paths to original root-owned config and prior mounts."""
    if record.enrollment.profile != "ordinary-v1":
        raise ValueError("this exact ordinary mount/command profile does not cover the quarantined original")
    manager_config, manager_host = manager.get("Config"), manager.get("HostConfig")
    proxy_host = proxy.get("HostConfig")
    if not isinstance(manager_config, dict) or not isinstance(manager_host, dict) or not isinstance(proxy_host, dict):
        raise ValueError("original effective Docker configuration is unavailable")
    completed_command = manager_config.get("Entrypoint") == ["python"] and manager_config.get("Cmd") == _FRESH_COMMAND
    if not completed_command and not _supported_prior_command(manager_config.get("Entrypoint"), manager_config.get("Cmd")):
        raise ValueError("original bootstrap effective command is not reviewed")
    if manager_config.get("WorkingDir") != "/workspace":
        raise ValueError("original bootstrap working directory differs from the reviewed image")
    for host in (manager_host, proxy_host):
        if host.get("Privileged") is not False or host.get("PidMode") not in ("", None) or host.get("CapAdd") not in ([], None) or host.get("Devices") not in ([], None) or host.get("SecurityOpt") not in ([], None):
            raise ValueError("original ordinary Docker security configuration differs")
    if proxy_host.get("NetworkMode") != "container:" + record.enrollment.management_id:
        raise ValueError("original proxy network binding differs")
    manager_mounts, proxy_mounts = _mounts(manager), _mounts(proxy)
    _unshadowed_bootstrap_execution(manager_config, manager_mounts)
    for inspected, prior in ((manager_mounts, prior_manager), (proxy_mounts, prior_proxy)):
        digest = hashlib.sha256(json.dumps(inspected, separators=(",", ":")).encode()).hexdigest()
        if digest != prior.mounts_sha256:
            raise ValueError("current mounts differ from captured original Docker mounts")
    expected = {"/bootstrap": False, "/management-child-state": True, "/management-oauth": True}
    mapped = {destination: (source, writable) for source, destination, writable in manager_mounts}
    if any(mapped.get(destination, ("", not writable))[1] != writable for destination, writable in expected.items()):
        raise ValueError("original bootstrap/state/OAuth mount effect differs")
    # No mounted subdirectory may obscure the protected config or owner-fence.
    if any(destination.startswith(("/bootstrap/", "/management-child-state/", "/management-oauth/")) for _, destination, _ in manager_mounts):
        raise ValueError("original bootstrap/state/OAuth path is shadowed")
    bootstrap_path, bootstrap_bytes, bootstrap_digest = _original_config(record, "bootstrap-config")
    management_path, _, management_digest = _original_config(record, "management-config")
    source = mapped["/bootstrap"][0]
    if bootstrap_path != Path(source) / "config.json" or management_path.parent != Path(source):
        raise ValueError("protected original configuration is not mounted at its effective path")
    if bootstrap_path != Path(prior_manager.bootstrap_source) or bootstrap_digest != prior_manager.bootstrap_sha256:
        raise ValueError("original bootstrap config differs from captured live producer")
    if prior_manager.owner_lock_device <= 0 or prior_manager.owner_lock_inode <= 0:
        raise ValueError("captured original producer owner-lock identity is unavailable")
    try:
        config = json.loads(bootstrap_bytes, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("original bootstrap configuration cannot be decoded") from exc
    if not isinstance(config, dict) or config.get("instance_id") != prior_manager.instance_id or config.get("state_root") != "/management-child-state" or config.get("oauth_root") != "/management-oauth":
        raise ValueError("original bootstrap effective state/instance mapping differs")
    runtimes = config.get("runtimes")
    if not isinstance(runtimes, list) or not 1 <= len(runtimes) <= 64:
        raise ValueError("original bootstrap runtime configuration is unsupported")
    management_config_path = "/bootstrap/" + management_path.name
    bound = False
    for runtime in runtimes:
        if not isinstance(runtime, dict) or runtime.get("executable") != "/opt/auto-coder/.venv/bin/auto-coder" or not isinstance(runtime.get("arguments"), list) or any(not isinstance(arg, str) for arg in runtime["arguments"]):
            raise ValueError("original bootstrap runtime command is unsupported")
        args = runtime["arguments"]
        if args.count("--config") != 1 or args.index("--config") + 1 >= len(args) or args[args.index("--config") + 1] != management_config_path or args[:2] != ["management", "serve"]:
            raise ValueError("original runtime command/config binding is unsupported")
        bound = True
    if not bound:
        raise ValueError("original runtime management configuration is not referenced")
    completed = _completed_fresh_initialization(config, mapped, bootstrap_digest, management_digest) if completed_command else None
    return bootstrap_digest, management_digest, completed


def _validated_quarantined_profile(
    record: EnrollmentObservation,
    manager: dict[str, object],
    proxy: dict[str, object],
    prior_manager: PriorContainer,
    prior_proxy: PriorContainer,
) -> tuple[str, str, str, int, int]:
    """Validate the captured quarantined original, never replacement admission."""
    if record.enrollment.profile != "quarantined-original-v1":
        raise ValueError("the quarantined original reader requires its own reviewed profile")
    config, host, proxy_host = manager.get("Config"), manager.get("HostConfig"), proxy.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host, dict) or not isinstance(proxy_host, dict):
        raise ValueError("quarantined original effective Docker configuration is unavailable")
    entrypoint, command = config.get("Entrypoint"), config.get("Cmd")
    if entrypoint != ["python"] or command != _FRESH_COMMAND:
        raise ValueError("quarantined original bootstrap command differs from reviewed fresh entrypoint")
    if config.get("WorkingDir") != "/workspace" or config.get("User") != "" or config.get("Healthcheck") is not None or "/management-source" in json.dumps(config, sort_keys=True):
        raise ValueError("quarantined original support mount execution configuration is unsupported")
    if host.get("SecurityOpt") != ["no-new-privileges"] or host.get("ReadonlyRootfs") is not True or host.get("Privileged") is not False or host.get("PidMode") not in ("", None) or host.get("CapAdd") not in ([], None) or host.get("Devices") not in ([], None):
        raise ValueError("quarantined original no-new-privileges or rootfs security profile differs")
    if proxy_host.get("Privileged") is not False or proxy_host.get("PidMode") not in ("", None) or proxy_host.get("CapAdd") not in ([], None) or proxy_host.get("Devices") not in ([], None) or proxy_host.get("SecurityOpt") not in ([], None):
        raise ValueError("quarantined original proxy security configuration differs")
    if host.get("NetworkMode") != prior_manager.network_mode:
        raise ValueError("quarantined original management network binding differs")
    if proxy_host.get("NetworkMode") != "container:" + record.enrollment.management_id:
        raise ValueError("quarantined original proxy network binding differs")
    if proxy_host.get("NetworkMode") != prior_proxy.network_mode:
        raise ValueError("quarantined original proxy captured network differs")
    management_mounts, proxy_mounts = _mounts(manager), _mounts(proxy)
    _unshadowed_bootstrap_execution(config, management_mounts)
    for inspected, prior in ((management_mounts, prior_manager), (proxy_mounts, prior_proxy)):
        if hashlib.sha256(json.dumps(inspected, separators=(",", ":")).encode()).hexdigest() != prior.mounts_sha256:
            raise ValueError("quarantined original mounts differ from captured Docker provenance")
    mounted = {destination: (source, writable) for source, destination, writable in management_mounts}
    exact = {
        "/bootstrap/config.json": False,
        "/bootstrap/management.yml": False,
        "/management-source": False,
        "/management-versions": False,
        "/management-child-state": True,
        "/management-oauth": True,
    }
    if any(destination not in mounted or mounted[destination][1] != writable for destination, writable in exact.items()):
        raise ValueError("quarantined original individual-file or support mount effects differ")
    if any(destination == "/bootstrap" or destination.startswith(("/bootstrap/", "/management-source/", "/management-versions/", "/management-child-state/", "/management-oauth/")) and destination not in exact for _, destination, _ in management_mounts):
        raise ValueError("quarantined original bootstrap or support mount is shadowed")
    bootstrap_path, bootstrap_bytes, bootstrap_digest = _original_config(record, "bootstrap-config")
    management_path, _, management_digest = _original_config(record, "management-config")
    if str(bootstrap_path) != mounted["/bootstrap/config.json"][0] or str(management_path) != mounted["/bootstrap/management.yml"][0]:
        raise ValueError("quarantined original protected config file mount sources differ")
    if bootstrap_path != Path(prior_manager.bootstrap_source) or bootstrap_digest != prior_manager.bootstrap_sha256 or prior_manager.owner_lock_device <= 0 or prior_manager.owner_lock_inode <= 0:
        raise ValueError("quarantined original captured source/owner provenance differs")
    _, support_descriptor, support_sha = _original_config(record, "support-source")
    support_device, support_inode = _support_mount_directory(support_descriptor, mounted["/management-source"][0])
    try:
        parsed = json.loads(bootstrap_bytes, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("quarantined original bootstrap configuration is unreadable") from exc
    if (
        not isinstance(parsed, dict)
        or parsed.get("instance_id") != prior_manager.instance_id
        or parsed.get("state_root") != "/management-child-state"
        or parsed.get("oauth_root") != "/management-oauth"
        or parsed.get("management_version_root") != "/management-versions"
        or parsed.get("management_config") != "/bootstrap/management.yml"
    ):
        raise ValueError("quarantined original effective bootstrap root/config mapping differs")
    runtimes = parsed.get("runtimes")
    if not isinstance(runtimes, list) or len(runtimes) != 1 or not isinstance(runtimes[0], dict) or runtimes[0].get("reference") != parsed.get("initial_runtime"):
        raise ValueError("quarantined original initial runtime binding is ambiguous")
    runtime = runtimes[0]
    executable, source_root, arguments = runtime.get("executable"), runtime.get("source_root"), runtime.get("arguments")
    if not isinstance(executable, str) or not isinstance(source_root, str) or not isinstance(arguments, list):
        raise ValueError("quarantined original installed executable mapping is unsupported")
    version_root = Path("/management-versions")
    executable_path, source_root_path = Path(executable), Path(source_root)
    if (
        runtime.get("execution_kind") != "management-installed"
        or not executable_path.is_relative_to(source_root_path)
        or not source_root_path.is_relative_to(version_root)
        or source_root_path == version_root
        or ".." in executable_path.parts
        or ".." in source_root_path.parts
        or arguments != ["-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config", "/bootstrap/management.yml"]
        or "/management-source" in json.dumps(runtime, sort_keys=True)
    ):
        raise ValueError("quarantined original support mount execution or runtime config differs")
    return bootstrap_digest, management_digest, support_sha, support_device, support_inode


def inspect_installed_bootstrap_mount_profile(record: EnrollmentObservation, selected_inspections: dict[str, str]) -> InstalledBootstrapMountProfile:
    """Independently re-inspect original Docker objects and protected config."""
    enrollment = record.enrollment
    if enrollment.profile not in ("ordinary-v1", "quarantined-original-v1"):
        raise ValueError("the original mount/config profile is unsupported")
    prior_content, prior_identity = _protected(Path(enrollment.original_prior_evidence_path), uid=0, size_limit=65536)
    if hashlib.sha256(prior_content).hexdigest() != record.original_prior_evidence_sha256:
        raise ValueError("original prior evidence differs from protected enrollment")
    prior_manager, prior_proxy, decoded_digest = _evidence(Path(enrollment.original_prior_evidence_path))
    if decoded_digest != record.original_prior_evidence_sha256:
        raise ValueError("original prior evidence changed during profile readback")
    first = (_inspect(enrollment.management_id), _inspect(enrollment.proxy_id))
    for inspected, identifier in zip(first, (enrollment.management_id, enrollment.proxy_id)):
        if _digest([inspected]) != selected_inspections.get(identifier):
            raise ValueError("original profile Docker inspection changed since complete inventory")
    support_sha, support_device, support_inode = "", 0, 0
    completed = None
    proxy_digest = _validated_proxy_configuration(record, first[1])
    if enrollment.profile == "ordinary-v1":
        bootstrap_digest, management_digest, completed = _validated_ordinary_profile(record, first[0], first[1], prior_manager, prior_proxy)
        management_config_in_container = "/bootstrap/" + next(item for item in enrollment.original_files if item.purpose == "management-config").path.rsplit("/", 1)[-1]
    else:
        bootstrap_digest, management_digest, support_sha, support_device, support_inode = _validated_quarantined_profile(record, first[0], first[1], prior_manager, prior_proxy)
        management_config_in_container = "/bootstrap/management.yml"
    if (_inspect(enrollment.management_id), _inspect(enrollment.proxy_id)) != first:
        raise ValueError("original profile Docker configuration changed during observation")
    if _original_config(record, "bootstrap-config")[2] != bootstrap_digest or _original_config(record, "management-config")[2] != management_digest:
        raise ValueError("original protected configuration changed during profile readback")
    if _validated_proxy_configuration(record, first[1]) != proxy_digest:
        raise ValueError("original protected proxy configuration changed during profile readback")
    if completed is not None and _validated_ordinary_profile(record, first[0], first[1], prior_manager, prior_proxy)[2] != completed:
        raise ValueError("original completed initialization changed during profile readback")
    if enrollment.profile == "quarantined-original-v1" and _original_config(record, "support-source")[2] != support_sha:
        raise ValueError("quarantined original support manifest changed during profile readback")
    final_prior, final_identity = _protected(Path(enrollment.original_prior_evidence_path), uid=0, size_limit=65536)
    if final_prior != prior_content or _file_identity(final_identity) != _file_identity(prior_identity):
        raise ValueError("original prior evidence changed during profile readback")
    return InstalledBootstrapMountProfile(
        enrollment.management_id,
        enrollment.proxy_id,
        enrollment.profile,
        selected_inspections[enrollment.management_id],
        selected_inspections[enrollment.proxy_id],
        prior_manager.mounts_sha256,
        prior_proxy.mounts_sha256,
        bootstrap_digest,
        management_digest,
        "/bootstrap/config.json",
        management_config_in_container,
        prior_manager.owner_lock_device,
        prior_manager.owner_lock_inode,
        support_sha,
        support_device,
        support_inode,
        proxy_digest,
        completed,
    )
