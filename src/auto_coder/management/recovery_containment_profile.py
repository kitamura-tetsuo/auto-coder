"""Actual source, configuration and bind profiles for ordinary recovery only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .archived_exclusions import unique_pairs
from .containment_enrollment import _file_identity, _protected, _trusted_ancestors
from .containment_route_manifest import DeploymentRootBinding
from .installed_bootstrap_mount_profile import _mounts, _unshadowed_bootstrap_execution
from .original_attempts import source_identity
from .recovery_containment_model import RecoveryContainmentUnavailable, RecoveryEnrollment, RecoveryFile, RecoveryLock, RecoveryOriginal, RecoveryTarget
from .recovery_source_profile import read_image_source_identity

_COMMAND = ["-m", "auto_coder.management.bootstrap_supervisor", "--config", "/bootstrap/config.json"]


@dataclass(frozen=True)
class RecoveryManagerFact:
    generation_id: str
    instance_id: str
    container_id: str
    image_id: str
    state: str
    source_identity: str
    inspection_sha256: str
    bootstrap_config_sha256: str
    management_config_sha256: str
    state_root: str
    journal_root: str
    oauth_root: str
    owner: RecoveryLock | None
    published_port: int
    mounts: tuple[DeploymentRootBinding, ...]


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_bound_file(item: RecoveryFile, *, source: bool = False, expected_uid: int = 0) -> bytes:
    path = Path(item.path)
    if not source:
        data, info = _protected(path, uid=expected_uid, size_limit=1024 * 1024)
    else:
        if path != path.resolve(strict=True):
            raise RecoveryContainmentUnavailable("recovery controller source path is not canonical")
        _trusted_ancestors(path.parent, uid=expected_uid)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or info.st_nlink != 1 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RecoveryContainmentUnavailable("recovery controller source is not protected")
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or _file_identity(os.fstat(fd)) != _file_identity(info) or _file_identity(path.lstat()) != _file_identity(info):
                raise RecoveryContainmentUnavailable("recovery source changed during readback")
        finally:
            os.close(fd)
    if info.st_nlink != 1 or (info.st_dev, info.st_ino, hashlib.sha256(data).hexdigest()) != (item.device, item.inode, item.sha256):
        raise RecoveryContainmentUnavailable("protected recovery source/configuration changed")
    return data


def _security(inspected: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    host, config = inspected.get("HostConfig"), inspected.get("Config")
    if not isinstance(host, dict) or not isinstance(config, dict):
        raise RecoveryContainmentUnavailable("recovery Docker command/security observation is absent")
    if (
        host.get("Privileged") is not False
        or host.get("PidMode") not in ("", None)
        or host.get("CapAdd") not in (None, [])
        or host.get("Devices") not in (None, [])
        or host.get("SecurityOpt") not in (None, [])
        or host.get("NetworkMode") in ("host", "none", None, "")
        or config.get("User") != ""
        or config.get("Healthcheck") is not None
    ):
        raise RecoveryContainmentUnavailable("recovery ordinary Docker security profile differs")
    return host, config


def bind_mounts(container: dict[str, object], selected: tuple[DeploymentRootBinding, ...], *, expected_uid: int = 0) -> tuple[DeploymentRootBinding, ...]:
    identifier = container.get("Id")
    observed = _mounts(container)
    expected = tuple(item for item in selected if item.container_id == identifier)
    if tuple(sorted((item.source, item.destination, item.writable) for item in expected)) != observed:
        raise RecoveryContainmentUnavailable("recovery actual and protected bind inventories differ")
    for item in expected:
        path = Path(item.source)
        if path != path.resolve(strict=True) or path == Path("/") or path == Path("/proc") or path.is_relative_to("/proc") or path == Path("/sys") or path.is_relative_to("/sys") or path == Path("/dev") or path.is_relative_to("/dev"):
            raise RecoveryContainmentUnavailable("recovery bind source is noncanonical or unsupported")
        _trusted_ancestors(path.parent, uid=expected_uid)
        info = path.lstat()
        if info.st_uid != expected_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or (info.st_dev, info.st_ino, info.st_mode) != (item.device, item.inode, item.mode):
            raise RecoveryContainmentUnavailable("recovery bind root was replaced or is unprotected")
    return expected


def _mapped_file(path: str, mounts: tuple[DeploymentRootBinding, ...], *, writable: bool) -> str:
    destination = Path(path)
    matched = [item for item in mounts if destination == Path(item.destination) or Path(item.destination) in destination.parents]
    if len(matched) != 1 or matched[0].writable != writable:
        raise RecoveryContainmentUnavailable("recovery effective configuration is absent, shadowed or writable")
    binding = matched[0]
    return str(Path(binding.source) / destination.relative_to(binding.destination))


def _root(path: object, mounts: tuple[DeploymentRootBinding, ...], *, writable: bool) -> str:
    if not isinstance(path, str) or not Path(path).is_absolute() or ".." in Path(path).parts:
        raise RecoveryContainmentUnavailable("recovery configured root differs")
    found = [item for item in mounts if item.destination == path]
    if len(found) != 1 or found[0].writable != writable or not stat.S_ISDIR(found[0].mode):
        raise RecoveryContainmentUnavailable("recovery effective root mapping differs")
    if any(item.destination != path and (Path(path) in Path(item.destination).parents or Path(item.destination) in Path(path).parents) for item in mounts):
        raise RecoveryContainmentUnavailable("recovery effective root is shadowed")
    return found[0].source


def _ordinary_mount_effects(config: dict[str, object], mounts: tuple[DeploymentRootBinding, ...], *, target: bool) -> None:
    """Keep extra mounts within the explicit ordinary data/custody profile."""
    effects = {"/bootstrap": False, "/bootstrap/config.json": False, "/bootstrap/management.yml": False}
    for name in ("state_root", "journal_root", "oauth_root"):
        value = config.get(name)
        if isinstance(value, str):
            effects[value] = True
    for name in ("management_version_root", "management_config", "original_genesis", "recovery_epoch_config"):
        value = config.get(name)
        if isinstance(value, str):
            effects[value] = False
    runtimes = config.get("runtimes", [])
    exclusions = config.get("archived_exclusions", [])
    if not isinstance(runtimes, list) or not isinstance(exclusions, list):
        raise RecoveryContainmentUnavailable("recovery runtime/exclusion profile is ambiguous")
    for runtime in runtimes:
        if isinstance(runtime, dict) and isinstance(runtime.get("arguments"), list):
            arguments = runtime["arguments"]
            if "--config" in arguments and arguments.index("--config") + 1 < len(arguments) and isinstance(arguments[arguments.index("--config") + 1], str):
                effects[arguments[arguments.index("--config") + 1]] = False
    if target:
        effects.update({"/management-recovery": False, "/management-recovery-history/history.lock": False})
    else:
        if config.get("original_genesis") is not None:
            effects["/management-original-history"] = False
        if config.get("fresh_guard_path") == "/management-initialization/fresh-guard.sock":
            effects["/management-initialization"] = False
        if config.get("fresh_prior_lock_path") == "/management-prior-owner.lock":
            effects.update({"/management-prior-owner.lock": False, "/management-prior-journal": False, "/management-prior-operations": False})
        for exclusion in exclusions:
            if isinstance(exclusion, dict):
                for name in ("journal_root", "fence"):
                    if isinstance(exclusion.get(name), str):
                        effects[exclusion[name]] = False
    for item in mounts:
        declared_owner = target and re.fullmatch(r"/management-recovery-originals/(?:0|[1-9][0-9]?)/owner\.lock", item.destination) is not None
        worker_secret = re.fullmatch(r"/run/secrets/(?:beta|release)_worker_bridge", item.destination) is not None
        if worker_secret and not item.writable and stat.S_ISREG(item.mode):
            continue
        if declared_owner:
            if item.writable or not stat.S_ISREG(item.mode):
                raise RecoveryContainmentUnavailable("recovery original custody mount is not a read-only file")
        elif effects.get(item.destination) is not item.writable:
            raise RecoveryContainmentUnavailable("recovery ordinary profile has an undeclared mount effect")


def validate_manager_profile(selected: RecoveryOriginal | RecoveryTarget, inspected: dict[str, object], *, source: str, expected_uid: int = 0) -> RecoveryManagerFact:
    """Inspect current source/config shape without reading or promoting outcomes."""
    target = isinstance(selected, RecoveryTarget)
    if inspected.get("Id") != selected.management_id or inspected.get("Image") != selected.management_image_id:
        raise RecoveryContainmentUnavailable("recovery manager image or identity differs")
    host, command = _security(inspected)
    expected_commands = [_COMMAND + ["--recovery-epoch"]] if target else [_COMMAND, _COMMAND + ["--fresh-initialization"]]
    if command.get("Entrypoint") != ["python"] or command.get("Cmd") not in expected_commands or command.get("WorkingDir") != "/workspace" or str(host.get("NetworkMode")).startswith("container:"):
        raise RecoveryContainmentUnavailable("recovery ordinary bootstrap command differs")
    mounts = bind_mounts(inspected, selected.mounts, expected_uid=expected_uid)
    _unshadowed_bootstrap_execution(command, _mounts(inspected))
    environment = dict(item.split("=", 1) for item in cast_environment(command.get("Env")))
    supported_python = {"PYTHON_VERSION", "PYTHON_SHA256", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}
    if environment.get("PYTHONDONTWRITEBYTECODE") != "1" or any(name.startswith("LD_") or name.startswith("PYTHON") and name not in supported_python for name in environment):
        raise RecoveryContainmentUnavailable("recovery source execution environment is unsupported")
    config_bytes = read_bound_file(selected.bootstrap_config, expected_uid=expected_uid)
    from .bootstrap_supervisor import BootstrapConfig

    BootstrapConfig.read(Path(selected.bootstrap_config.path))
    read_bound_file(selected.management_config, expected_uid=expected_uid)
    config = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    if (
        not isinstance(config, dict)
        or config.get("instance_id") != selected.instance_id
        or config.get("state_root") != "/management-child-state"
        or config.get("oauth_root") != "/management-oauth"
        or config.get("new_instance_initialization", False) is not False
        or config.get("new_docker_network") is not None
        or config.get("new_docker_host_port") is not None
    ):
        raise RecoveryContainmentUnavailable("recovery ordinary effective bootstrap identity differs")
    _ordinary_mount_effects(config, mounts, target=target)
    if _mapped_file("/bootstrap/config.json", mounts, writable=False) != selected.bootstrap_config.path:
        raise RecoveryContainmentUnavailable("recovery bootstrap configuration is not actually mounted")
    state_root = _root(config.get("state_root"), mounts, writable=True)
    journal_root = _root(config.get("journal_root"), mounts, writable=True)
    oauth_root = _root(config.get("oauth_root"), mounts, writable=True)
    roots = (state_root, journal_root, oauth_root)
    if len(set(roots)) != 3 or any(Path(left) == Path(right) or Path(left) in Path(right).parents or Path(right) in Path(left).parents for index, left in enumerate(roots) for right in roots[index + 1 :]):
        raise RecoveryContainmentUnavailable("recovery mutable roots overlap")
    if selected.owner is not None and selected.owner.path != str(Path(state_root) / ".supervisor-owner.lock"):
        raise RecoveryContainmentUnavailable("recovery owner is not the effective supervisor owner file")
    if isinstance(selected, RecoveryOriginal) and selected.state == "never-started" and (Path(state_root) / ".supervisor-owner.lock").exists():
        raise RecoveryContainmentUnavailable("never-started original already has an owner artifact")
    runtimes = config.get("runtimes")
    if not isinstance(runtimes, list) or not 1 <= len(runtimes) <= 64 or any(not isinstance(item, dict) for item in runtimes):
        raise RecoveryContainmentUnavailable("recovery runtime configuration is absent or ambiguous")
    configured = config.get("management_config")
    version_root = config.get("management_version_root")
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            raise RecoveryContainmentUnavailable("recovery runtime configuration differs")
        arguments = runtime.get("arguments")
        if runtime.get("execution_kind") == "management-installed":
            if not isinstance(configured, str) or not isinstance(version_root, str) or _root(version_root, mounts, writable=False) != version_root or arguments != ["-I", "-B", "-m", "auto_coder.cli", "management", "serve", "--config", configured]:
                raise RecoveryContainmentUnavailable("recovery installed runtime mapping differs")
            executable, source_root = runtime.get("executable"), runtime.get("source_root")
            if not isinstance(executable, str) or not isinstance(source_root, str) or not Path(executable).is_relative_to(source_root) or not Path(source_root).is_relative_to(version_root) or source_root == version_root or any(".." in Path(item).parts for item in (executable, source_root)):
                raise RecoveryContainmentUnavailable("recovery installed source path is unsupported")
            management_path = configured
        else:
            if target or runtime.get("executable") != "/opt/auto-coder/.venv/bin/auto-coder" or not isinstance(arguments, list) or len(arguments) != 4 or arguments[:3] != ["management", "serve", "--config"] or not isinstance(arguments[3], str):
                raise RecoveryContainmentUnavailable("recovery ordinary runtime command differs")
            management_path = arguments[3]
        if _mapped_file(management_path, mounts, writable=False) != selected.management_config.path:
            raise RecoveryContainmentUnavailable("recovery runtime uses another management configuration")
    if target:
        if config.get("recovery_epoch_config") != "/bootstrap/recovery-epoch.json" or config.get("original_genesis") is not None or any(config.get(name) is not None for name in ("fresh_guard_path", "fresh_prior_lock_path", "fresh_runtime_binding")):
            raise RecoveryContainmentUnavailable("recovery target lacks its separate prelaunch profile")
    else:
        if config.get("recovery_epoch_config") is not None:
            raise RecoveryContainmentUnavailable("an original cannot be relabeled as the recovery target")
        genesis = config.get("original_genesis")
        if genesis is not None:
            if genesis != "/bootstrap/original-genesis.json" or command.get("Cmd") != _COMMAND:
                raise RecoveryContainmentUnavailable("ordinary original genesis configuration differs")
            _mapped_file(genesis, mounts, writable=False)
            _root("/management-original-history", mounts, writable=False)
        elif command.get("Cmd") != _COMMAND + ["--fresh-initialization"]:
            raise RecoveryContainmentUnavailable("ordinary original lacks genesis/history-capable entrypoint")
    binding = host.get("PortBindings")
    if not isinstance(binding, dict) or set(binding) != {"8080/tcp"} or not isinstance(binding["8080/tcp"], list) or len(binding["8080/tcp"]) != 1:
        raise RecoveryContainmentUnavailable("recovery published endpoint is ambiguous")
    endpoint = binding["8080/tcp"][0]
    if not isinstance(endpoint, dict) or set(endpoint) != {"HostIp", "HostPort"} or endpoint["HostIp"] != "127.0.0.1" or not isinstance(endpoint["HostPort"], str) or not endpoint["HostPort"].isdecimal() or not 0 < int(endpoint["HostPort"]) <= 65535:
        raise RecoveryContainmentUnavailable("recovery published endpoint is not exact loopback TCP")
    port = int(endpoint["HostPort"])
    if isinstance(selected, RecoveryTarget) and port != selected.published_port:
        raise RecoveryContainmentUnavailable("recovery target published endpoint differs")
    if source != source_identity() or read_image_source_identity(selected.management_id) != source:
        raise RecoveryContainmentUnavailable("recovery actual complete package source differs")
    generation = "target" if isinstance(selected, RecoveryTarget) else selected.generation_id
    state = "prelaunch-target" if isinstance(selected, RecoveryTarget) else selected.state
    return RecoveryManagerFact(generation, selected.instance_id, selected.management_id, selected.management_image_id, state, source, digest([inspected]), selected.bootstrap_config.sha256, selected.management_config.sha256, state_root, journal_root, oauth_root, selected.owner, port, mounts)


def cast_environment(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or "=" not in item for item in value):
        raise RecoveryContainmentUnavailable("recovery process environment is ambiguous")
    return tuple(value)


def validate_proxy_profile(selected: RecoveryOriginal, inspected: dict[str, object], *, expected_uid: int = 0) -> str:
    if inspected.get("Id") != selected.proxy_id or inspected.get("Image") != selected.proxy_image_id:
        raise RecoveryContainmentUnavailable("original recovery proxy identity differs")
    host, config = _security(inspected)
    if host.get("NetworkMode") != "container:" + selected.management_id or config.get("Entrypoint") != ["caddy"] or config.get("Cmd") != ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"] or config.get("WorkingDir") != "/srv":
        raise RecoveryContainmentUnavailable("original recovery proxy command or network differs")
    mounts = bind_mounts(inspected, selected.mounts, expected_uid=expected_uid)
    if _mapped_file("/etc/caddy/Caddyfile", mounts, writable=False) != selected.proxy_config.path:
        raise RecoveryContainmentUnavailable("original recovery proxy configuration is not actually mounted")
    for item in mounts:
        destination = Path(item.destination)
        if any(destination == Path(root) or destination in Path(root).parents or Path(root) in destination.parents for root in ("/usr/bin", "/usr/local/bin", "/bin", "/lib", "/usr/lib", "/srv")):
            raise RecoveryContainmentUnavailable("original recovery Caddy executable is shadowed")
    read_bound_file(selected.proxy_config, expected_uid=expected_uid)
    return digest([inspected])


def assert_disjoint_target(enrollment: RecoveryEnrollment, facts: tuple[RecoveryManagerFact, ...]) -> None:
    target = facts[-1]
    if target.container_id != enrollment.target.management_id:
        raise RecoveryContainmentUnavailable("recovery target fact is not exact")
    for old in facts[:-1]:
        if target.published_port == old.published_port:
            raise RecoveryContainmentUnavailable("recovery target cannot hide an old published listener")
        if target.oauth_root != old.oauth_root:
            raise RecoveryContainmentUnavailable("recovery target does not retain the original OAuth root")
        for left in (target.state_root, target.journal_root):
            for right in (old.state_root, old.journal_root, old.oauth_root):
                if Path(left) == Path(right) or Path(left) in Path(right).parents or Path(right) in Path(left).parents:
                    raise RecoveryContainmentUnavailable("recovery target mutable root overlaps an original")
    old_root_ids = {(item.device, item.inode) for original in enrollment.originals for item in original.mounts if item.writable and item.source != target.oauth_root}
    if any((item.device, item.inode) in old_root_ids for item in enrollment.target.mounts if item.writable):
        raise RecoveryContainmentUnavailable("recovery target aliases an original mutable root")
