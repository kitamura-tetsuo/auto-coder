"""Independently reobserve a trusted-root approved ordinary-route manifest.

The manifest is a separately installed, exhaustive *selection attestation*.
It cannot prove its own completeness: the trusted host operator must review
every exclusion and remote origin before installing it. This reader compares
the approved entire local systemd, cron/at and Docker universes with fresh
observations, verifies each restarter's actual inhibition, and never changes
an installed route. This component alone is not a containment verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from .archived_exclusions import unique_pairs
from .containment_enrollment import (
    ENROLLMENT_ROOT,
    EnrollmentObservation,
    EnrollmentUnavailable,
    _approved_directory,
    _file_identity,
    _protected,
    _trusted_ancestors,
)

_UNIT_NAME = re.compile(r"(?:[A-Za-z0-9_.@:-]|\\x[0-9a-fA-F]{2})+\.(?:service|timer|socket|path|target|mount|automount|swap)")
_ACTIVATION_UNIT_TYPES = ("service", "timer", "socket", "path", "target", "mount", "automount", "swap")
_HASH = re.compile(r"[0-9a-f]{64}")
_CONTAINER = re.compile(r"[0-9a-f]{64}")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_UNIT_BYTES = 128 * 1024
_MAX_UNITS = 1024
_MAX_CRON_ITEMS = 2048
_MAX_CRON_BYTES = 1024 * 1024
_CRON_ROOTS = (
    Path("/etc/crontab"),
    Path("/etc/anacrontab"),
    Path("/etc/cron.d"),
    Path("/etc/cron.hourly"),
    Path("/etc/cron.daily"),
    Path("/etc/cron.weekly"),
    Path("/etc/cron.monthly"),
    Path("/var/spool/cron"),
    Path("/var/spool/at"),
    Path("/var/spool/atjobs"),
)
_CRON_SCHEDULERS = frozenset(("cron.service", "crond.service", "anacron.service", "atd.service"))
_MANAGEMENT_CUES = (
    "auto_coder.management.bootstrap_supervisor",
    "auto_coder.management.proxy_watch_cli",
    "auto-coder-management-",
    "auto-coder-proxy-",
)
_UNIT_PROPERTIES = (
    "Id",
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NeedDaemonReload",
    "FragmentPath",
    "SourcePath",
    "DropInPaths",
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecStopPost",
    "Triggers",
    "TriggeredBy",
    "Wants",
    "Requires",
)
_REQUIRED_UNIT_PROPERTIES = (
    "Id",
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NeedDaemonReload",
    "FragmentPath",
)
_REPEATABLE_UNIT_PROPERTIES = frozenset(("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStopPost"))
# systemd serializes dependency sets in nondeterministic order. Source and
# command precedence remain ordered and must never be normalized as sets.
_UNORDERED_UNIT_PROPERTIES = frozenset(("Triggers", "TriggeredBy", "Wants", "Requires"))


class RouteManifestUnavailable(ValueError):
    """An approved, complete and currently inhibited route set is unavailable."""


@dataclass(frozen=True)
class SystemdRoute:
    unit: str
    sha256: str
    role: str

    @classmethod
    def parse(cls, value: object) -> SystemdRoute:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RouteManifestUnavailable("approved systemd route schema differs")
        if not isinstance(value["unit"], str) or not _UNIT_NAME.fullmatch(value["unit"]):
            raise RouteManifestUnavailable("approved systemd route name differs")
        if not isinstance(value["sha256"], str) or not _HASH.fullmatch(value["sha256"]):
            raise RouteManifestUnavailable("approved systemd route source differs")
        if value["role"] not in ("restarter", "unrelated"):
            raise RouteManifestUnavailable("approved systemd route classification differs")
        return cls(value["unit"], value["sha256"], value["role"])


@dataclass(frozen=True)
class CronEntry:
    path: str
    kind: str
    device: int
    inode: int
    ctime_ns: int
    mode: int
    uid: int
    sha256: str
    management_candidate: bool = False
    link_target: str = ""
    target_device: int = 0
    target_inode: int = 0
    target_ctime_ns: int = 0

    @classmethod
    def parse(cls, value: object) -> CronEntry:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RouteManifestUnavailable("approved cron entry schema differs")
        if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute() or len(value["path"]) > 4096:
            raise RouteManifestUnavailable("approved cron entry path differs")
        if value["kind"] not in ("missing", "directory", "regular", "symlink"):
            raise RouteManifestUnavailable("approved cron entry kind differs")
        if any(type(value[key]) is not int or value[key] < 0 for key in ("device", "inode", "ctime_ns", "mode", "uid", "target_device", "target_inode", "target_ctime_ns")):
            raise RouteManifestUnavailable("approved cron entry identity differs")
        digest = value["sha256"]
        if not isinstance(digest, str) or (value["kind"] in ("regular", "symlink") and not _HASH.fullmatch(digest)) or (value["kind"] not in ("regular", "symlink") and digest):
            raise RouteManifestUnavailable("approved cron entry digest differs")
        if value["kind"] == "missing" and any(value[key] for key in ("device", "inode", "ctime_ns", "mode", "uid")):
            raise RouteManifestUnavailable("approved absent cron entry has an identity")
        if value["kind"] != "missing" and (value["device"] == 0 or value["inode"] == 0):
            raise RouteManifestUnavailable("approved cron entry identity is missing")
        if type(value["management_candidate"]) is not bool or (value["kind"] not in ("regular", "symlink") and value["management_candidate"]):
            raise RouteManifestUnavailable("approved cron route classification differs")
        if not isinstance(value["link_target"], str) or len(value["link_target"]) > 4096:
            raise RouteManifestUnavailable("approved cron symbolic link target differs")
        if value["kind"] == "symlink":
            if not value["link_target"] or any(value[key] <= 0 for key in ("target_device", "target_inode", "target_ctime_ns")):
                raise RouteManifestUnavailable("approved cron target identity is incomplete")
        elif value["link_target"] or any(value[key] for key in ("target_device", "target_inode", "target_ctime_ns")):
            raise RouteManifestUnavailable("approved nonlink cron entry has a target identity")
        return cls(**value)


@dataclass(frozen=True)
class LocalScriptRoute:
    path: str
    sha256: str
    device: int
    inode: int
    inhibited_by: str

    @classmethod
    def parse(cls, value: object) -> LocalScriptRoute:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RouteManifestUnavailable("approved local script route schema differs")
        if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute() or len(value["path"]) > 4096:
            raise RouteManifestUnavailable("approved local script path differs")
        if not isinstance(value["sha256"], str) or not _HASH.fullmatch(value["sha256"]):
            raise RouteManifestUnavailable("approved local script digest differs")
        if any(type(value[key]) is not int or value[key] <= 0 for key in ("device", "inode")):
            raise RouteManifestUnavailable("approved local script identity differs")
        if not isinstance(value["inhibited_by"], str) or not _UNIT_NAME.fullmatch(value["inhibited_by"]):
            raise RouteManifestUnavailable("approved local script inhibitor differs")
        return cls(**value)


@dataclass(frozen=True)
class ApprovedRouteManifest:
    schema: str
    deployment_id: str
    enrollment_id: str
    authorization_id: str
    profile: str
    enrollment_sha256: str
    approved_observation_only: bool
    complete_ordinary_routes_attested: bool
    systemd_routes: tuple[SystemdRoute, ...]
    docker_container_ids: tuple[str, ...]
    cron_entries: tuple[CronEntry, ...]
    cron_restarter_paths: tuple[str, ...]
    local_script_routes: tuple[LocalScriptRoute, ...]
    remote_script_origins: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> ApprovedRouteManifest:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__) or value["schema"] != "approved-management-ordinary-routes-v1":
            raise RouteManifestUnavailable("approved ordinary-route manifest schema differs")
        if value["approved_observation_only"] is not True or value["complete_ordinary_routes_attested"] is not True:
            raise RouteManifestUnavailable("approved ordinary routes lack explicit host-root completeness attestation")
        for name in ("deployment_id", "enrollment_id", "authorization_id"):
            if not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value[name]):
                raise RouteManifestUnavailable("approved ordinary-route deployment differs")
        if value["profile"] not in ("ordinary-v1", "quarantined-original-v1"):
            raise RouteManifestUnavailable("approved ordinary-route profile differs")
        if not isinstance(value["enrollment_sha256"], str) or not _HASH.fullmatch(value["enrollment_sha256"]):
            raise RouteManifestUnavailable("approved ordinary-route enrollment binding differs")
        routes = value["systemd_routes"]
        if not isinstance(routes, list) or not 1 <= len(routes) <= _MAX_UNITS:
            raise RouteManifestUnavailable("approved systemd catalog is absent or oversized")
        systemd = tuple(SystemdRoute.parse(item) for item in routes)
        if tuple(item.unit for item in systemd) != tuple(sorted({item.unit for item in systemd})):
            raise RouteManifestUnavailable("approved systemd catalog is duplicate or unsorted")
        containers = value["docker_container_ids"]
        if not isinstance(containers, list) or not 2 <= len(containers) <= 512 or any(not isinstance(item, str) or not _CONTAINER.fullmatch(item) for item in containers) or containers != sorted(set(containers)):
            raise RouteManifestUnavailable("approved full Docker inventory differs")
        cron = value["cron_entries"]
        if not isinstance(cron, list) or len(cron) > _MAX_CRON_ITEMS:
            raise RouteManifestUnavailable("approved cron catalog is oversized")
        cron_entries = tuple(CronEntry.parse(item) for item in cron)
        if tuple(item.path for item in cron_entries) != tuple(sorted({item.path for item in cron_entries})):
            raise RouteManifestUnavailable("approved cron catalog is duplicate or unsorted")
        crons = value["cron_restarter_paths"]
        if not isinstance(crons, list) or crons != sorted(set(crons)) or any(not isinstance(item, str) or item not in {entry.path for entry in cron_entries if entry.kind in ("regular", "symlink")} for item in crons):
            raise RouteManifestUnavailable("approved cron restarter set differs")
        local = value["local_script_routes"]
        if not isinstance(local, list) or len(local) > 128:
            raise RouteManifestUnavailable("approved local script route set is oversized")
        scripts = tuple(LocalScriptRoute.parse(item) for item in local)
        if tuple(item.path for item in scripts) != tuple(sorted({item.path for item in scripts})):
            raise RouteManifestUnavailable("approved local script routes are duplicate or unsorted")
        remote = value["remote_script_origins"]
        if not isinstance(remote, list) or len(remote) > 128 or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,256}", item) for item in remote) or remote != sorted(set(remote)):
            raise RouteManifestUnavailable("approved remote origins are ambiguous")
        # Until an authenticated remote execution/scheduler reader exists, no
        # remote origin can be silently treated as inhibited by a local claim.
        if remote:
            raise RouteManifestUnavailable("approved remote script origins need independent authenticated readback")
        restarters = {item.unit for item in systemd if item.role == "restarter"}
        if "auto-coder-management-proxy-watch.service" not in restarters:
            raise RouteManifestUnavailable("the known management watcher is omitted from approved restarters")
        if any(item.unit.startswith(("auto-coder-management", "auto_coder.management")) and item.role != "restarter" for item in systemd):
            raise RouteManifestUnavailable("known management unit classified as unrelated")
        if any(item.inhibited_by not in restarters for item in scripts):
            raise RouteManifestUnavailable("approved local restarter script has no enrolled inhibited controller")
        if crons:
            scheduler_units = {item.unit for item in systemd if item.unit in _CRON_SCHEDULERS}
            if not scheduler_units or not scheduler_units <= restarters:
                raise RouteManifestUnavailable("approved cron restarter lacks inhibited installed schedulers")
        return cls(
            value["schema"],
            value["deployment_id"],
            value["enrollment_id"],
            value["authorization_id"],
            value["profile"],
            value["enrollment_sha256"],
            True,
            True,
            systemd,
            tuple(containers),
            cron_entries,
            tuple(crons),
            scripts,
            tuple(remote),
        )


@dataclass(frozen=True)
class SystemdRouteFact:
    unit: str
    sha256: str
    unit_file_state: str
    active_state: str
    main_pid: int
    inhibited: bool
    management_candidate: bool = False


@dataclass(frozen=True)
class RouteManifestObservation:
    """Current readback only; not a durable fence or success grant."""

    manifest: ApprovedRouteManifest
    manifest_sha256: str
    manifest_device: int
    manifest_inode: int
    manifest_ctime_ns: int
    systemd: tuple[SystemdRouteFact, ...]
    cron_entries: tuple[CronEntry, ...]
    local_script_sha256: tuple[tuple[str, str], ...]
    docker_container_ids: tuple[str, ...]


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _command(*args: str, max_bytes: int = _MAX_UNIT_BYTES) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=15)
    if len(result.stdout.encode()) > max_bytes or result.stderr and len(result.stderr) > 4096:
        raise RouteManifestUnavailable("installed route command output exceeds bounds")
    return result.stdout


def _systemd_catalog() -> tuple[str, ...]:
    names: set[str] = set()
    for operation, field in (("list-unit-files", "unit_file"), ("list-units", "unit")):
        raw = _command(
            "systemctl",
            operation,
            "--all",
            *("--type=" + unit_type for unit_type in _ACTIVATION_UNIT_TYPES),
            "--output=json",
            "--no-pager",
            max_bytes=_MAX_MANIFEST_BYTES,
        )
        entries = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(entries, list) or len(entries) > _MAX_UNITS:
            raise RouteManifestUnavailable("systemd unit catalog is oversized or ambiguous")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get(field), str) or not _UNIT_NAME.fullmatch(entry[field]):
                raise RouteManifestUnavailable("systemd unit catalog contains an unsupported unit")
            names.add(entry[field])
    if not names or len(names) > _MAX_UNITS:
        raise RouteManifestUnavailable("systemd unit catalog is incomplete or oversized")
    return tuple(sorted(names))


def _unit_enablement(unit: str) -> str:
    result = subprocess.run(("systemctl", "is-enabled", unit), capture_output=True, text=True, timeout=15)
    state = result.stdout.strip()
    if result.returncode not in (0, 1, 3) or len(result.stderr) > 4096 or not re.fullmatch(r"[a-z-]{1,64}", state):
        raise RouteManifestUnavailable("installed systemd enablement cannot be independently verified")
    return state


def _systemd_unit(unit: str, *, trusted_uid: int = 0) -> SystemdRouteFact:
    if not _UNIT_NAME.fullmatch(unit):
        raise RouteManifestUnavailable("installed systemd route name differs")
    is_template = "@." in unit
    alias_state = ""
    if is_template:
        # Templates are installed files but not directly addressable with
        # `systemctl show`. Every instantiated unit is also separately found
        # by the unfiltered loaded-unit catalog. An installed template alone
        # cannot establish a positive restarter inhibition claim.
        state = _unit_enablement(unit)
        shown = {"Id": unit, "UnitFileState": state, "LoadState": "template", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "NeedDaemonReload": "unknown", "FragmentPath": ""}
        properties = ""
        repeatable: dict[str, list[str]] = {}
    else:
        properties = _command("systemctl", "show", unit, "--property=" + ",".join(_UNIT_PROPERTIES))
        shown = {}
        repeatable = {}
        for line in properties.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in _UNIT_PROPERTIES or len(value) > 32768:
                raise RouteManifestUnavailable("effective systemd unit properties are ambiguous")
            if key in _REPEATABLE_UNIT_PROPERTIES:
                repeatable.setdefault(key, []).append(value)
                if len(repeatable[key]) > 128:
                    raise RouteManifestUnavailable("effective systemd command list exceeds bound")
                continue
            if key in shown:
                raise RouteManifestUnavailable("duplicate singleton systemd unit property")
            shown[key] = value
        required = set(_REQUIRED_UNIT_PROPERTIES)
        if not unit.endswith(".service"):
            required.remove("MainPID")
            shown.setdefault("MainPID", "0")
        if not required <= set(shown) or not _UNIT_NAME.fullmatch(shown["Id"]) or shown["NeedDaemonReload"] not in ("yes", "no") or not shown["MainPID"].isdecimal():
            raise RouteManifestUnavailable("effective systemd unit properties are incomplete")
        if shown["Id"] != unit:
            alias_state = _unit_enablement(unit)
            if alias_state != "alias":
                raise RouteManifestUnavailable("systemd unit identity redirected without approved alias state")
        if any(not re.fullmatch(r"[a-z-]+", shown[key]) for key in ("LoadState", "ActiveState", "SubState")):
            raise RouteManifestUnavailable("effective systemd state is unsupported")
        if shown["UnitFileState"] and not re.fullmatch(r"[a-z-]+", shown["UnitFileState"]):
            raise RouteManifestUnavailable("effective systemd unit-file state is unsupported")
        if not shown["UnitFileState"] and (shown["LoadState"] != "not-found" or shown["FragmentPath"]):
            raise RouteManifestUnavailable("effective systemd unit-file state is unavailable")
    try:
        unit_source = _command("systemctl", "cat", unit, "--no-pager")
    except subprocess.CalledProcessError:
        if not is_template and shown["LoadState"] == "not-found" and not shown["FragmentPath"] and not shown["UnitFileState"] and shown["ActiveState"] == "inactive":
            unit_source = "<no installed unit source>"
        else:
            raise
    identity: tuple[int, int, int, int, str] | None = None
    if shown["UnitFileState"] == "masked":
        if is_template:
            candidates = (Path(root) / unit for root in ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"))
            path = next((item for item in candidates if item.is_symlink()), Path("/dev/null"))
        else:
            path = Path(shown["FragmentPath"])
        if not path.is_absolute() or len(str(path)) > 4096 or path == Path("/dev/null"):
            raise RouteManifestUnavailable("masked systemd unit fragment is ambiguous")
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != trusted_uid or os.readlink(path) != "/dev/null":
            raise RouteManifestUnavailable("masked systemd unit is not backed by a protected /dev/null link")
        identity = (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_uid, str(path))
    inhibited = not is_template and shown["LoadState"] == "masked" and shown["UnitFileState"] == "masked" and shown["ActiveState"] == "inactive" and shown["SubState"] == "dead" and shown["MainPID"] == "0" and shown["NeedDaemonReload"] == "no" and identity is not None
    normalized = {name: (repeatable.get(name, []) if name in _REPEATABLE_UNIT_PROPERTIES else sorted(shown.get(name, "").split()) if name in _UNORDERED_UNIT_PROPERTIES else shown.get(name, "")) for name in _UNIT_PROPERTIES}
    candidate = any(term in (unit + "\n" + properties + "\n" + unit_source) for term in _MANAGEMENT_CUES)
    return SystemdRouteFact(unit, _digest((normalized, hashlib.sha256(unit_source.encode()).hexdigest(), identity, alias_state)), shown["UnitFileState"], shown["ActiveState"], int(shown["MainPID"]), inhibited, candidate)


def _cron_catalog(*, roots: tuple[Path, ...], trusted_uid: int = 0) -> tuple[CronEntry, ...]:
    """Scan a fixed whole-cron/at surface; missing roots remain in the digest."""
    pending = list(roots)
    observed: dict[str, CronEntry] = {}
    aggregate_bytes = 0
    while pending:
        path = pending.pop()
        name = str(path)
        if name in observed or len(observed) >= _MAX_CRON_ITEMS:
            if name in observed:
                continue
            raise RouteManifestUnavailable("cron catalog exceeds bounded whole-root inventory")
        try:
            info = path.lstat()
        except FileNotFoundError:
            observed[name] = CronEntry(name, "missing", 0, 0, 0, 0, 0, "")
            continue
        if stat.S_ISDIR(info.st_mode):
            observed[name] = CronEntry(name, "directory", info.st_dev, info.st_ino, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, "")
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                data = stream.read(_MAX_CRON_BYTES + 1)
                after = os.fstat(stream.fileno())
            aggregate_bytes += len(data)
            if len(data) > _MAX_CRON_BYTES or aggregate_bytes > 16 * _MAX_CRON_BYTES or _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(path.lstat()):
                raise RouteManifestUnavailable("cron source changed or exceeded bounded read")
            observed[name] = CronEntry(name, "regular", info.st_dev, info.st_ino, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, hashlib.sha256(data).hexdigest(), any(term.encode() in data for term in _MANAGEMENT_CUES))
        elif stat.S_ISLNK(info.st_mode):
            target_text = os.readlink(path)
            if not target_text or len(target_text) > 4096 or info.st_uid != trusted_uid:
                raise RouteManifestUnavailable("installed cron symbolic link is not protected")
            resolved = path.resolve(strict=True)
            if resolved == path or not resolved.is_absolute():
                raise RouteManifestUnavailable("installed cron symbolic link target is ambiguous")
            _trusted_ancestors(resolved.parent, uid=trusted_uid)
            descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_uid != trusted_uid or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise RouteManifestUnavailable("installed cron symbolic link target is not root-protected")
                data = stream.read(_MAX_CRON_BYTES + 1)
                after = os.fstat(stream.fileno())
            aggregate_bytes += len(data)
            if len(data) > _MAX_CRON_BYTES or aggregate_bytes > 16 * _MAX_CRON_BYTES or _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(resolved.lstat()) or _file_identity(path.lstat()) != _file_identity(info) or os.readlink(path) != target_text:
                raise RouteManifestUnavailable("installed cron symbolic link or its target changed during observation")
            observed[name] = CronEntry(name, "symlink", info.st_dev, info.st_ino, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, hashlib.sha256(data).hexdigest(), any(term.encode() in data for term in _MANAGEMENT_CUES), target_text, after.st_dev, after.st_ino, after.st_ctime_ns)
        else:
            raise RouteManifestUnavailable("symlink or unsupported installed cron route requires explicit refusal")
    return tuple(observed[path] for path in sorted(observed))


def _local_script(path: Path, *, expected_uid: int) -> tuple[str, int, int]:
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise RouteManifestUnavailable("installed local script path is not canonical")
    _trusted_ancestors(path.parent, uid=expected_uid)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid != expected_uid or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RouteManifestUnavailable("installed local script is not root-protected")
        data = stream.read(_MAX_CRON_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > _MAX_CRON_BYTES or _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(path.lstat()):
        raise RouteManifestUnavailable("installed local script source changed during observation")
    return hashlib.sha256(data).hexdigest(), after.st_dev, after.st_ino


def _observe_approved_route_manifest(
    approval: EnrollmentObservation,
    docker: tuple[object, ...],
    *,
    root: Path,
    expected_uid: int,
    cron_roots: tuple[Path, ...] = _CRON_ROOTS,
) -> RouteManifestObservation:
    """Internal root/cron injection exists only for independently protected fixtures."""
    if os.geteuid() != expected_uid:
        raise RouteManifestUnavailable("route manifest requires trusted host owner")
    _approved_directory(root, uid=expected_uid)
    path = root / (approval.enrollment.enrollment_id + ".routes.json")
    content, info = _protected(path, uid=expected_uid, size_limit=_MAX_MANIFEST_BYTES)
    try:
        manifest = ApprovedRouteManifest.parse(json.loads(content, object_pairs_hook=unique_pairs))
    except (ValueError, TypeError, UnicodeDecodeError, EnrollmentUnavailable) as exc:
        raise RouteManifestUnavailable("approved ordinary routes cannot be decoded: " + str(exc)) from exc
    for key in ("deployment_id", "enrollment_id", "authorization_id", "profile"):
        if getattr(manifest, key) != getattr(approval.enrollment, key):
            raise RouteManifestUnavailable("approved routes and protected enrollment differ")
    if manifest.enrollment_sha256 != approval.enrollment_sha256:
        raise RouteManifestUnavailable("approved route manifest is not bound to this exact enrollment")
    ids = tuple(sorted(getattr(item, "container_id", "") for item in docker))
    if ids != manifest.docker_container_ids:
        raise RouteManifestUnavailable("whole-engine Docker inventory differs from approved routes")
    first_catalog = _systemd_catalog()
    if first_catalog != tuple(item.unit for item in manifest.systemd_routes):
        raise RouteManifestUnavailable("unknown, omitted or newly installed systemd route")
    observed_systemd = tuple(_systemd_unit(unit, trusted_uid=expected_uid) for unit in first_catalog)
    restarters = {item.unit for item in manifest.systemd_routes if item.role == "restarter"}
    for approved, actual in zip(manifest.systemd_routes, observed_systemd):
        if approved.sha256 != actual.sha256:
            raise RouteManifestUnavailable("effective systemd route changed: " + approved.unit)
        if actual.management_candidate and approved.role != "restarter":
            raise RouteManifestUnavailable("detected management systemd route is excluded: " + approved.unit)
        if approved.role == "restarter" and not actual.inhibited:
            raise RouteManifestUnavailable("ordinary systemd restarter remains enabled: " + approved.unit)
    if manifest.cron_restarter_paths and not _CRON_SCHEDULERS.intersection(restarters):
        raise RouteManifestUnavailable("ordinary cron restarter lacks scheduler inhibition")
    cron = _cron_catalog(roots=cron_roots, trusted_uid=expected_uid)
    if cron != manifest.cron_entries:
        raise RouteManifestUnavailable("unknown, omitted or changed cron/at route")
    if any(entry.management_candidate and entry.path not in manifest.cron_restarter_paths for entry in cron):
        raise RouteManifestUnavailable("detected management cron/at route is excluded")
    local = []
    for route in manifest.local_script_routes:
        try:
            digest, device, inode = _local_script(Path(route.path), expected_uid=expected_uid)
        except (OSError, EnrollmentUnavailable, RouteManifestUnavailable) as exc:
            raise RouteManifestUnavailable("installed local script route cannot be verified") from exc
        if (digest, device, inode) != (route.sha256, route.device, route.inode):
            raise RouteManifestUnavailable("installed local script source changed: " + route.path)
        if route.inhibited_by not in restarters:
            raise RouteManifestUnavailable("local script ordinary restarter is not inhibited: " + route.path)
        local.append((route.path, digest))
    if _systemd_catalog() != first_catalog or tuple(_systemd_unit(unit, trusted_uid=expected_uid) for unit in first_catalog) != observed_systemd or _cron_catalog(roots=cron_roots, trusted_uid=expected_uid) != cron:
        raise RouteManifestUnavailable("installed ordinary routes changed during observation")
    if any(_local_script(Path(route.path), expected_uid=expected_uid) != (route.sha256, route.device, route.inode) for route in manifest.local_script_routes):
        raise RouteManifestUnavailable("installed local script source changed during observation")
    last_content, last_info = _protected(path, uid=expected_uid, size_limit=_MAX_MANIFEST_BYTES)
    if last_content != content or _file_identity(last_info) != _file_identity(info):
        raise RouteManifestUnavailable("approved route manifest changed during observation")
    return RouteManifestObservation(manifest, hashlib.sha256(content).hexdigest(), info.st_dev, info.st_ino, info.st_ctime_ns, observed_systemd, cron, tuple(local), ids)


def observe_approved_route_manifest(approval: EnrollmentObservation, docker: tuple[object, ...]) -> RouteManifestObservation:
    """Production reader has no caller-controlled approval or scan roots."""
    return _observe_approved_route_manifest(approval, docker, root=ENROLLMENT_ROOT, expected_uid=0)


def propose_unapproved_route_draft(approval: EnrollmentObservation, docker: tuple[object, ...]) -> dict[str, object]:
    """Provide inspectable values for a human review; never attest completeness.

    All exclusions and absent local/remote sources are unreviewed. This draft
    cannot be installed as usable approval until a trusted root explicitly
    audits and replaces the false completeness declaration.
    """
    catalog = _systemd_catalog()
    systemd = tuple(_systemd_unit(unit) for unit in catalog)
    cron = _cron_catalog(roots=_CRON_ROOTS)
    return {
        "schema": "approved-management-ordinary-routes-v1",
        "deployment_id": approval.enrollment.deployment_id,
        "enrollment_id": approval.enrollment.enrollment_id,
        "authorization_id": approval.enrollment.authorization_id,
        "profile": approval.enrollment.profile,
        "enrollment_sha256": approval.enrollment_sha256,
        "approved_observation_only": True,
        "complete_ordinary_routes_attested": False,
        "systemd_routes": [asdict(SystemdRoute(item.unit, item.sha256, "restarter" if item.management_candidate or item.unit == "auto-coder-management-proxy-watch.service" else "unrelated")) for item in systemd],
        "docker_container_ids": sorted(getattr(item, "container_id", "") for item in docker),
        "cron_entries": [asdict(item) for item in cron],
        "cron_restarter_paths": [item.path for item in cron if item.management_candidate],
        "local_script_routes": [],
        "remote_script_origins": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce a read-only, explicitly unapproved route manifest draft")
    parser.add_argument("propose", choices=("propose",))
    parser.add_argument("--scope", required=True, type=Path)
    args = parser.parse_args()
    try:
        # Import here to avoid a module cycle between the observer and its
        # optional manifest component. Production enrollment guards apply.
        from .containment_enrollment import observe_approved_enrollment
        from .containment_observation import _docker, read_scope

        scope = read_scope(args.scope)
        approved = observe_approved_enrollment(scope)
        docker = _docker(scope)
        draft = propose_unapproved_route_draft(approved, docker)
        print(json.dumps({"status": "unapproved-root-review-required", "draft": draft}, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "unavailable", "reason": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
