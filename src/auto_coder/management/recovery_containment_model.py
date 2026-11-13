"""Finite scope and resource types for observation-only ordinary recovery.

Protected enrollment selects the independently inspectable objects. None of these
records attests original history, technical containment, admission or a grant.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .containment_enrollment import OriginalNetworkPin
from .containment_route_manifest import DeploymentRootBinding

RECOVERY_ENROLLMENT_ROOT = Path("/var/lib/auto-coder/approved-recovery-containment-enrollments")
RECOVERY_PROFILE = "ordinary-recovery-v1"
_MAX_GENERATIONS = 16
_HASH = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class RecoveryContainmentUnavailable(ValueError):
    """A required current recovery observation is absent or unsupported."""


def _object(value: object, fields: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(cast(dict[str, object], fields)):
        raise RecoveryContainmentUnavailable(name + " schema differs")
    return value


def _text(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RecoveryContainmentUnavailable(name + " differs")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or not Path(value).is_absolute() or str(Path(value)) != value or ".." in Path(value).parts or value == "/":
        raise RecoveryContainmentUnavailable("recovery path is not canonical")
    return value


def _positive(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise RecoveryContainmentUnavailable("recovery kernel identity differs")
    return value


def _boot(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("noncanonical boot identity")
    except (ValueError, TypeError, AttributeError) as error:
        raise RecoveryContainmentUnavailable("recovery boot identity differs") from error
    return value


@dataclass(frozen=True)
class RecoveryFile:
    path: str
    sha256: str
    device: int
    inode: int

    @classmethod
    def parse(cls, value: object) -> RecoveryFile:
        raw = _object(value, cls.__dataclass_fields__, "recovery file")
        return cls(_path(raw["path"]), _text(raw["sha256"], _HASH, "file digest"), _positive(raw["device"]), _positive(raw["inode"]))


@dataclass(frozen=True)
class RecoveryLock:
    path: str
    device: int
    inode: int

    @classmethod
    def parse(cls, value: object) -> RecoveryLock:
        raw = _object(value, cls.__dataclass_fields__, "recovery lock")
        return cls(_path(raw["path"]), _positive(raw["device"]), _positive(raw["inode"]))


def _mounts(value: object, containers: tuple[str, ...]) -> tuple[DeploymentRootBinding, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise RecoveryContainmentUnavailable("recovery bind inventory is absent or oversized")
    mounts = tuple(DeploymentRootBinding.parse(item) for item in value)
    keys = tuple((item.container_id, item.destination) for item in mounts)
    if keys != tuple(sorted(set(keys))) or {item.container_id for item in mounts} != set(containers):
        raise RecoveryContainmentUnavailable("recovery bind inventory is ambiguous or foreign")
    for item in mounts:
        _path(item.source)
        _path(item.destination)
    return mounts


@dataclass(frozen=True)
class RecoveryOriginal:
    generation_id: str
    instance_id: str
    management_id: str
    management_image_id: str
    proxy_id: str
    proxy_image_id: str
    state: str
    boot_id: str
    bootstrap_config: RecoveryFile
    management_config: RecoveryFile
    proxy_config: RecoveryFile
    mounts: tuple[DeploymentRootBinding, ...]
    owner: RecoveryLock | None
    prior_evidence: RecoveryFile | None
    namespace_pin: OriginalNetworkPin | None

    @classmethod
    def parse(cls, value: object) -> RecoveryOriginal:
        raw = _object(value, cls.__dataclass_fields__, "recovery original")
        manager = _text(raw["management_id"], _HASH, "original manager")
        proxy = _text(raw["proxy_id"], _HASH, "original proxy")
        if manager == proxy:
            raise RecoveryContainmentUnavailable("original manager and proxy alias")
        state = raw["state"]
        if state not in ("started", "never-started"):
            raise RecoveryContainmentUnavailable("original current-state selection differs")
        if state == "started":
            boot = _boot(raw["boot_id"])
            owner = RecoveryLock.parse(raw["owner"])
            prior = RecoveryFile.parse(raw["prior_evidence"])
            pin = OriginalNetworkPin.parse(raw["namespace_pin"])
            if pin.holder_boot_id != boot:
                raise RecoveryContainmentUnavailable("original capture and namespace boot differ")
        else:
            if raw["boot_id"] != "" or any(raw[name] is not None for name in ("owner", "prior_evidence", "namespace_pin")):
                raise RecoveryContainmentUnavailable("never-started object cannot invent old resources")
            boot, owner, prior, pin = "", None, None, None
        return cls(
            _text(raw["generation_id"], _IDENTIFIER, "generation identity"),
            _text(raw["instance_id"], _IDENTIFIER, "original instance identity"),
            manager,
            _text(raw["management_image_id"], _IMAGE, "original manager image"),
            proxy,
            _text(raw["proxy_image_id"], _IMAGE, "original proxy image"),
            str(state),
            boot,
            RecoveryFile.parse(raw["bootstrap_config"]),
            RecoveryFile.parse(raw["management_config"]),
            RecoveryFile.parse(raw["proxy_config"]),
            _mounts(raw["mounts"], (manager, proxy)),
            owner,
            prior,
            pin,
        )


@dataclass(frozen=True)
class RecoveryTarget:
    instance_id: str
    management_id: str
    management_image_id: str
    bootstrap_config: RecoveryFile
    management_config: RecoveryFile
    mounts: tuple[DeploymentRootBinding, ...]
    owner: RecoveryLock
    published_port: int

    @classmethod
    def parse(cls, value: object) -> RecoveryTarget:
        raw = _object(value, cls.__dataclass_fields__, "recovery target")
        manager = _text(raw["management_id"], _HASH, "target manager")
        port = raw["published_port"]
        if type(port) is not int or not 0 < port <= 65535:
            raise RecoveryContainmentUnavailable("target published port differs")
        return cls(
            _text(raw["instance_id"], _IDENTIFIER, "target instance"),
            manager,
            _text(raw["management_image_id"], _IMAGE, "target image"),
            RecoveryFile.parse(raw["bootstrap_config"]),
            RecoveryFile.parse(raw["management_config"]),
            _mounts(raw["mounts"], (manager,)),
            RecoveryLock.parse(raw["owner"]),
            port,
        )


@dataclass(frozen=True)
class RecoveryWatcher:
    unit: str
    configuration: RecoveryFile
    source: RecoveryFile
    unit_source: RecoveryFile
    lock: RecoveryLock

    @classmethod
    def parse(cls, value: object) -> RecoveryWatcher:
        raw = _object(value, cls.__dataclass_fields__, "recovery watcher")
        unit = _text(raw["unit"], re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.service"), "watcher unit")
        fragment = RecoveryFile.parse(raw["unit_source"])
        if Path(fragment.path).name != unit:
            raise RecoveryContainmentUnavailable("recovery watcher retained unit source name differs")
        return cls(unit, RecoveryFile.parse(raw["configuration"]), RecoveryFile.parse(raw["source"]), fragment, RecoveryLock.parse(raw["lock"]))


@dataclass(frozen=True)
class RecoveryEnrollment:
    schema: str
    deployment_id: str
    enrollment_id: str
    authorization_id: str
    profile: str
    engine_id: str
    source_identity: str
    originals: tuple[RecoveryOriginal, ...]
    target: RecoveryTarget
    watchers: tuple[RecoveryWatcher, ...]
    approved_observation_only: bool

    @classmethod
    def parse(cls, value: object) -> RecoveryEnrollment:
        raw = _object(value, cls.__dataclass_fields__, "recovery enrollment")
        if raw["schema"] != "approved-management-recovery-containment-v1" or raw["profile"] != RECOVERY_PROFILE or raw["approved_observation_only"] is not True:
            raise RecoveryContainmentUnavailable("recovery enrollment profile or authority differs")
        values, controllers = raw["originals"], raw["watchers"]
        if not isinstance(values, list) or not 1 <= len(values) <= _MAX_GENERATIONS or not isinstance(controllers, list) or not 1 <= len(controllers) <= 32:
            raise RecoveryContainmentUnavailable("recovery original/controller set is absent or oversized")
        originals = tuple(RecoveryOriginal.parse(item) for item in values)
        target = RecoveryTarget.parse(raw["target"])
        watchers = tuple(RecoveryWatcher.parse(item) for item in controllers)
        if 3 + 2 * sum(item.state == "started" for item in originals) + len(watchers) > 64:
            raise RecoveryContainmentUnavailable("recovery scope exceeds the complete peer resource bound")
        names = tuple(item.generation_id for item in originals)
        units = tuple(item.unit for item in watchers)
        if names != tuple(sorted(set(names))) or units != tuple(sorted(set(units))):
            raise RecoveryContainmentUnavailable("recovery members or controllers are duplicate or unsorted")
        ids = [target.management_id] + [identifier for item in originals for identifier in (item.management_id, item.proxy_id)]
        instances = [target.instance_id] + [item.instance_id for item in originals]
        if len(ids) != len(set(ids)) or len(instances) != len(set(instances)):
            raise RecoveryContainmentUnavailable("recovery container or instance identities alias")
        locks = [item.owner for item in originals if item.owner is not None] + [target.owner] + [item.lock for item in watchers]
        if len({item.path for item in locks}) != len(locks) or len({(item.device, item.inode) for item in locks}) != len(locks):
            raise RecoveryContainmentUnavailable("recovery owner/controller objects alias")
        return cls(
            str(raw["schema"]),
            _text(raw["deployment_id"], _IDENTIFIER, "deployment"),
            _text(raw["enrollment_id"], _IDENTIFIER, "enrollment"),
            _text(raw["authorization_id"], _IDENTIFIER, "authorization"),
            RECOVERY_PROFILE,
            _text(raw["engine_id"], _IDENTIFIER, "engine"),
            _text(raw["source_identity"], _IMAGE, "package source"),
            originals,
            target,
            watchers,
            True,
        )

    @property
    def container_ids(self) -> tuple[str, ...]:
        return tuple(sorted((self.target.management_id, *(identifier for item in self.originals for identifier in (item.management_id, item.proxy_id)))))


@dataclass(frozen=True)
class RecoveryEnrollmentObservation:
    enrollment: RecoveryEnrollment
    sha256: str
    device: int
    inode: int
    ctime_ns: int


@dataclass(frozen=True)
class NamedDescriptor:
    name: str
    descriptor: int

    @classmethod
    def parse(cls, value: object) -> NamedDescriptor:
        raw = _object(value, cls.__dataclass_fields__, "recovery descriptor")
        descriptor = raw["descriptor"]
        if type(descriptor) is not int or not 0 <= descriptor <= 1048576:
            raise RecoveryContainmentUnavailable("recovery descriptor differs")
        return cls(_text(raw["name"], _IDENTIFIER, "resource name"), descriptor)


@dataclass(frozen=True)
class RecoveryResources:
    holder_pid: int
    holder_start_token: str
    holder_boot_id: str
    original_owner_fds: tuple[NamedDescriptor, ...]
    controller_fds: tuple[NamedDescriptor, ...]
    namespace_fds: tuple[NamedDescriptor, ...]

    @classmethod
    def parse(cls, value: object) -> RecoveryResources:
        raw = _object(value, cls.__dataclass_fields__, "recovery resources")
        pid = _positive(raw["holder_pid"])
        if pid <= 1 or pid > 2147483647:
            raise RecoveryContainmentUnavailable("recovery resource holder PID differs")
        groups = []
        for name in ("original_owner_fds", "controller_fds", "namespace_fds"):
            selected = raw[name]
            if not isinstance(selected, list) or len(selected) > 32:
                raise RecoveryContainmentUnavailable("recovery resource set is oversized")
            group = tuple(NamedDescriptor.parse(item) for item in selected)
            names = tuple(item.name for item in group)
            if names != tuple(sorted(set(names))) or len({item.descriptor for item in group}) != len(group):
                raise RecoveryContainmentUnavailable("recovery resource set is duplicate or unsorted")
            groups.append(group)
        if len({item.descriptor for group in groups for item in group}) != sum(map(len, groups)):
            raise RecoveryContainmentUnavailable("recovery resource descriptor groups alias")
        return cls(pid, _text(raw["holder_start_token"], re.compile(r"[1-9][0-9]{0,23}"), "holder lifetime"), _boot(raw["holder_boot_id"]), *groups)
