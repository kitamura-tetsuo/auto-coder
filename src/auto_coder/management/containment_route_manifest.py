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
from .containment_cron_inhibition import CRON_ROUTE_CUES, STOCK_INPUTS, CronInhibitedSource, CronScopeRetainsTasks, exact_stock_source, observe_inhibited_stock_cron_scope
from .containment_enrollment import (
    ENROLLMENT_ROOT,
    EnrollmentObservation,
    EnrollmentUnavailable,
    _approved_directory,
    _file_identity,
    _protected,
    _trusted_ancestors,
)
from .containment_route_sources import CommandSourceFact, CommandSourceUnavailable, FsckBlockOperandFact, fsck_block_operand, observe_command_sources, observe_fsck_block_operand, observe_reviewed_fsck_fragment
from .containment_systemd_exec import SYSTEMD_EXEC_CONTEXT_PROPERTIES, SystemdExecSearchFact, reobserve_systemd_exec_searches, resolve_direct_systemd_commands
from .containment_user_routes import UserControllerInventory, observe_user_controller_inventory

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
_CRON_SOURCE_FORMATS = tuple(zip(_CRON_ROOTS, ("system-crontab", "anacrontab", "system-crontab", "shell", "shell", "shell", "shell", "user-crontab", "shell", "shell")))
_CRON_SCHEDULERS = frozenset(("cron.service", "crond.service", "anacron.service", "atd.service"))
_MANAGEMENT_CUES = (
    "auto_coder.management.bootstrap_supervisor",
    "auto_coder.management.proxy_watch_cli",
    "auto-coder-management-",
    "auto-coder-proxy-",
)
_UNIT_PROPERTIES: tuple[str, ...] = (
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
    "ExecCondition",
    "ExecReload",
    "ExecStop",
    "ExecStopPost",
    "WorkingDirectory",
    "RootDirectory",
    "RootImage",
    "Environment",
    "EnvironmentFiles",
    "Triggers",
    "TriggeredBy",
    "Wants",
    "Requires",
)
_UNIT_PROPERTIES += tuple(name for name in SYSTEMD_EXEC_CONTEXT_PROPERTIES if name not in _UNIT_PROPERTIES)
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
_REPEATABLE_UNIT_PROPERTIES = frozenset(("ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition", "ExecReload", "ExecStop", "ExecStopPost", "EnvironmentFiles"))
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
    command_sources: tuple[CommandSourceFact, ...] = ()
    inhibited_source: CronInhibitedSource | None = None

    @classmethod
    def parse(cls, value: object) -> CronEntry:
        if not isinstance(value, dict) or set(value) not in (set(cls.__dataclass_fields__), set(cls.__dataclass_fields__) - {"inhibited_source"}, set(cls.__dataclass_fields__) - {"command_sources", "inhibited_source"}):
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
        sources = value.get("command_sources", [])
        if not isinstance(sources, list) or len(sources) > 128:
            raise RouteManifestUnavailable("approved cron command-source inventory differs")
        parsed_sources = tuple(CommandSourceFact.parse(item) for item in sources)
        if tuple(item.path for item in parsed_sources) != tuple(sorted({item.path for item in parsed_sources})):
            raise RouteManifestUnavailable("approved cron command sources are duplicate or unsorted")
        inhibited = CronInhibitedSource.parse(value["inhibited_source"]) if value.get("inhibited_source") is not None else None
        if inhibited is not None and (value["kind"] != "regular" or not value["management_candidate"] or parsed_sources or value["path"] not in STOCK_INPUTS or value["sha256"] != STOCK_INPUTS[value["path"]][1]):
            raise RouteManifestUnavailable("inhibited stock cron source identity or classification differs")
        return cls(**{**value, "command_sources": parsed_sources, "inhibited_source": inhibited})


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
class DeploymentRootBinding:
    """A current exact Docker bind source, independently reopened on this host."""

    container_id: str
    destination: str
    source: str
    writable: bool
    device: int
    inode: int
    mode: int

    @classmethod
    def parse(cls, value: object) -> DeploymentRootBinding:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise RouteManifestUnavailable("approved deployment root schema differs")
        if not isinstance(value["container_id"], str) or not _CONTAINER.fullmatch(value["container_id"]):
            raise RouteManifestUnavailable("approved deployment root container differs")
        if any(not isinstance(value[key], str) or not Path(value[key]).is_absolute() or ".." in Path(value[key]).parts or len(value[key]) > 4096 for key in ("source", "destination")):
            raise RouteManifestUnavailable("approved deployment root path differs")
        if type(value["writable"]) is not bool or any(type(value[key]) is not int or value[key] <= 0 for key in ("device", "inode", "mode")):
            raise RouteManifestUnavailable("approved deployment root identity differs")
        return cls(**value)


@dataclass(frozen=True)
class UserSystemdRoute:
    uid: int
    unit: str
    sha256: str
    role: str

    @classmethod
    def parse(cls, value: object) -> UserSystemdRoute:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__) or type(value["uid"]) is not int or not 0 <= value["uid"] <= 2147483647:
            raise RouteManifestUnavailable("approved user systemd scope differs")
        route = SystemdRoute.parse({key: value[key] for key in ("unit", "sha256", "role")})
        return cls(value["uid"], route.unit, route.sha256, route.role)


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
    deployment_roots: tuple[DeploymentRootBinding, ...] = ()
    user_controller_catalog_sha256: str = ""
    user_systemd_routes: tuple[UserSystemdRoute, ...] = ()

    @classmethod
    def parse(cls, value: object) -> ApprovedRouteManifest:
        if not isinstance(value, dict):
            raise RouteManifestUnavailable("approved ordinary-route manifest schema differs")
        extra = {"deployment_roots", "user_controller_catalog_sha256", "user_systemd_routes"}
        if value.get("schema") == "approved-management-ordinary-routes-v1":
            if set(value) not in (set(cls.__dataclass_fields__), set(cls.__dataclass_fields__) - extra) or any(value.get(key) for key in extra):
                raise RouteManifestUnavailable("legacy ordinary-route manifest cannot assert closed user/root scope")
            bindings: tuple[DeploymentRootBinding, ...] = ()
            user_digest = ""
            user_routes: tuple[UserSystemdRoute, ...] = ()
        elif value.get("schema") == "approved-management-ordinary-routes-v2" and set(value) == set(cls.__dataclass_fields__):
            raw_bindings = value["deployment_roots"]
            if not isinstance(raw_bindings, list) or not 1 <= len(raw_bindings) <= 128:
                raise RouteManifestUnavailable("approved deployment root inventory is absent or oversized")
            bindings = tuple(DeploymentRootBinding.parse(item) for item in raw_bindings)
            binding_keys = tuple((item.container_id, item.destination) for item in bindings)
            if binding_keys != tuple(sorted(set(binding_keys))):
                raise RouteManifestUnavailable("approved deployment roots are duplicate or unsorted")
            user_digest = value["user_controller_catalog_sha256"]
            if not isinstance(user_digest, str) or not _HASH.fullmatch(user_digest):
                raise RouteManifestUnavailable("approved user controller inventory digest is absent")
            raw_users = value["user_systemd_routes"]
            if not isinstance(raw_users, list) or len(raw_users) > 4096:
                raise RouteManifestUnavailable("approved user unit inventory is oversized")
            user_routes = tuple(UserSystemdRoute.parse(item) for item in raw_users)
            user_keys = tuple((item.uid, item.unit) for item in user_routes)
            if user_keys != tuple(sorted(set(user_keys))):
                raise RouteManifestUnavailable("approved user units are duplicate or unsorted")
        else:
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
            bindings,
            user_digest,
            user_routes,
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
    needs_daemon_reload: bool = False
    command_sources: tuple[CommandSourceFact, ...] = ()
    fsck_block_operands: tuple[FsckBlockOperandFact, ...] = ()
    systemd_exec_searches: tuple[SystemdExecSearchFact, ...] = ()


@dataclass(frozen=True)
class UserSystemdRouteFact:
    uid: int
    account_name: str
    fact: SystemdRouteFact


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
    closed_profile: bool = False
    deployment_roots: tuple[DeploymentRootBinding, ...] = ()
    user_controller_inventory: UserControllerInventory | None = None
    user_systemd: tuple[UserSystemdRouteFact, ...] = ()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _command(*args: str, max_bytes: int = _MAX_UNIT_BYTES) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=15)
    if len(result.stdout.encode()) > max_bytes or result.stderr and len(result.stderr) > 4096:
        raise RouteManifestUnavailable("installed route command output exceeds bounds")
    return result.stdout


def _systemctl_prefix(user: str | None = None) -> tuple[str, ...]:
    if user is None:
        return ("systemctl",)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\$?", user):
        raise RouteManifestUnavailable("user systemd account name is unsupported")
    return ("systemctl", "--user", "--machine=" + user + "@.host")


def _systemd_catalog(*, user: str | None = None) -> tuple[str, ...]:
    names: set[str] = set()
    for operation, field in (("list-unit-files", "unit_file"), ("list-units", "unit")):
        raw = _command(
            *_systemctl_prefix(user),
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


def _unit_enablement(unit: str, *, user: str | None = None) -> str:
    result = subprocess.run((*_systemctl_prefix(user), "is-enabled", "--", unit), capture_output=True, text=True, timeout=15)
    state = result.stdout.strip()
    if result.returncode not in (0, 1, 3) or len(result.stderr) > 4096 or not re.fullmatch(r"[a-z-]{1,64}", state):
        raise RouteManifestUnavailable("installed systemd enablement cannot be independently verified")
    return state


def _fsck_data_inputs(unit: str, shown: dict[str, str], repeatable: dict[str, list[str]], unit_source: str, *, user: str | None) -> tuple[list[str], tuple[CommandSourceFact, ...], tuple[FsckBlockOperandFact, ...]]:
    rows = [value for values in repeatable.values() for value in values]
    starts = repeatable.get("ExecStart", [])
    empty = ("Environment", "WorkingDirectory", "RootDirectory", "RootImage", "SourcePath", "DropInPaths")
    if (
        user is not None
        or not unit.startswith("systemd-fsck@")
        or "@." in unit
        or shown.get("Id") != unit
        or shown.get("LoadState") != "loaded"
        or shown.get("UnitFileState") != "static"
        or shown.get("NeedDaemonReload") != "no"
        or shown.get("FragmentPath") != "/usr/lib/systemd/system/systemd-fsck@.service"
        or any(shown.get(key) != "" for key in empty)
        or any(repeatable.get("EnvironmentFiles", []))
        or len(starts) != 1
        or any(value for key, values in repeatable.items() if key not in ("ExecStart", "EnvironmentFiles") for value in values)
    ):
        return rows, (), ()
    selected = fsck_block_operand(starts[0])
    if selected is None:
        return rows, (), ()
    fragment = observe_reviewed_fsck_fragment(unit_source)
    operand, remaining = selected
    block = observe_fsck_block_operand(operand)
    if observe_reviewed_fsck_fragment(unit_source) != fragment:
        raise CommandSourceUnavailable("reviewed fsck fragment changed during block operand readback")
    # Consume only this operand occurrence. The executable and every other
    # property still pass through ordinary strict source inspection.
    rows = [remaining if key == "ExecStart" else value for key, values in repeatable.items() for value in values]
    return rows, (fragment,), (block,)


def _systemd_unit(unit: str, *, trusted_uid: int = 0, user: str | None = None, extra_cues: tuple[str, ...] = (), inspect_local_inputs: bool = False, module_sources: tuple[tuple[str, str], ...] = ()) -> SystemdRouteFact:
    if not _UNIT_NAME.fullmatch(unit):
        raise RouteManifestUnavailable("installed systemd route name differs")
    is_template = "@." in unit
    alias_state = ""
    if is_template:
        # Templates are installed files but not directly addressable with
        # `systemctl show`. Every instantiated unit is also separately found
        # by the unfiltered loaded-unit catalog. An installed template alone
        # cannot establish a positive restarter inhibition claim.
        state = _unit_enablement(unit, user=user) if user is not None else _unit_enablement(unit)
        shown = {"Id": unit, "UnitFileState": state, "LoadState": "template", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "NeedDaemonReload": "unknown", "FragmentPath": ""}
        properties = ""
        repeatable: dict[str, list[str]] = {}
    else:
        properties = _command(*_systemctl_prefix(user), "show", "--all", "--property=" + ",".join(_UNIT_PROPERTIES), "--", unit)
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
                raise RouteManifestUnavailable("duplicate singleton systemd unit property: unit=" + unit[:256] + " property=" + key)
            shown[key] = value
        required = set(_REQUIRED_UNIT_PROPERTIES)
        if not unit.endswith(".service"):
            required.remove("MainPID")
            shown.setdefault("MainPID", "0")
        if not required <= set(shown) or not _UNIT_NAME.fullmatch(shown["Id"]) or shown["NeedDaemonReload"] not in ("yes", "no") or not shown["MainPID"].isdecimal():
            raise RouteManifestUnavailable("effective systemd unit properties are incomplete")
        if shown["Id"] != unit:
            alias_state = _unit_enablement(unit, user=user) if user is not None else _unit_enablement(unit)
            if alias_state != "alias":
                raise RouteManifestUnavailable("systemd unit identity redirected without approved alias state")
        if any(not re.fullmatch(r"[a-z-]+", shown[key]) for key in ("LoadState", "ActiveState", "SubState")):
            raise RouteManifestUnavailable("effective systemd state is unsupported")
        if shown["UnitFileState"] and not re.fullmatch(r"[a-z-]+", shown["UnitFileState"]):
            raise RouteManifestUnavailable("effective systemd unit-file state is unsupported")
        kernel_mount = unit.endswith(".mount") and shown["LoadState"] == "loaded" and shown["UnitFileState"] == "" and shown["FragmentPath"] == "" and shown.get("SourcePath") == "/proc/self/mountinfo"
        if not shown["UnitFileState"] and not kernel_mount and (shown["LoadState"] != "not-found" or shown["FragmentPath"]):
            raise RouteManifestUnavailable("effective systemd unit-file state is unavailable")
    try:
        unit_source = _command(*_systemctl_prefix(user), "cat", "--no-pager", "--", unit)
    except subprocess.CalledProcessError:
        if not is_template and shown["LoadState"] == "loaded" and shown["UnitFileState"] == "" and not shown["FragmentPath"] and shown.get("SourcePath") == "/proc/self/mountinfo" and unit.endswith(".mount"):
            # Passive mount units reflected from the kernel have no installed
            # systemd fragment. They remain in the full inventory and can
            # never satisfy the inhibited-restarter condition.
            unit_source = "<kernel mount reflected from /proc/self/mountinfo>"
        elif not is_template and shown["LoadState"] == "not-found" and not shown["FragmentPath"] and not shown["UnitFileState"] and shown["ActiveState"] == "inactive":
            unit_source = "<no installed unit source>"
        else:
            raise
    identity: tuple[int, int, int, int, str] | None = None
    if shown["UnitFileState"] == "masked":
        if is_template:
            roots = _user_unit_paths(user) if user is not None else ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system")
            candidates = (Path(root) / unit for root in roots)
            path = next((item for item in candidates if item.is_symlink()), Path("/dev/null"))
        else:
            path = Path(shown["FragmentPath"])
        if not path.is_absolute() or len(str(path)) > 4096 or path == Path("/dev/null"):
            raise RouteManifestUnavailable("masked systemd unit fragment is ambiguous")
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, trusted_uid) or os.readlink(path) != "/dev/null":
            raise RouteManifestUnavailable("masked systemd unit is not backed by a protected /dev/null link")
        identity = (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_uid, str(path))
    inhibited = not is_template and shown["LoadState"] == "masked" and shown["UnitFileState"] == "masked" and shown["ActiveState"] == "inactive" and shown["SubState"] == "dead" and shown["MainPID"] == "0" and shown["NeedDaemonReload"] == "no" and identity is not None
    normalized = {name: (repeatable.get(name, []) if name in _REPEATABLE_UNIT_PROPERTIES else sorted(shown.get(name, "").split()) if name in _UNORDERED_UNIT_PROPERTIES else shown.get(name, "")) for name in _UNIT_PROPERTIES}
    candidate = any(term in (unit + "\n" + properties + "\n" + unit_source) for term in _MANAGEMENT_CUES + extra_cues)
    local_sources: tuple[CommandSourceFact, ...] = ()
    block_operands: tuple[FsckBlockOperandFact, ...] = ()
    exec_searches: tuple[SystemdExecSearchFact, ...] = ()
    if inspect_local_inputs:
        rows, fragment_sources, block_operands = _fsck_data_inputs(unit, shown, repeatable, unit_source, user=user)
        mapped_rows, exec_searches = resolve_direct_systemd_commands(tuple(rows), unit=unit, shown=shown, environment_files=tuple(repeatable.get("EnvironmentFiles", [])), user=user, uid=trusted_uid, read_command=_command)
        commands = "\n".join(mapped_rows)
        commands += "\n" + "\n".join(shown.get(key, "") for key in ("Environment", "WorkingDirectory", "RootDirectory", "RootImage"))
        local_sources, local_candidate = observe_command_sources(commands, uid=trusted_uid, cues=_MANAGEMENT_CUES + extra_cues, known_modules=module_sources)
        reobserve_systemd_exec_searches(exec_searches, uid=trusted_uid, read_command=_command)
        local_sources = tuple(sorted((*local_sources, *fragment_sources), key=lambda item: item.path))
        candidate = candidate or local_candidate
    fingerprint: tuple[object, ...] = (normalized, hashlib.sha256(unit_source.encode()).hexdigest(), identity, alias_state)
    if block_operands:
        fingerprint = (*fingerprint, [asdict(item) for item in block_operands])
    if exec_searches:
        fingerprint = (*fingerprint, [asdict(item) for item in exec_searches])
    digest = _digest((*fingerprint, [asdict(item) for item in local_sources])) if inspect_local_inputs else _digest(fingerprint)
    return SystemdRouteFact(unit, digest, shown["UnitFileState"], shown["ActiveState"], int(shown["MainPID"]), inhibited, candidate, shown["NeedDaemonReload"] == "yes", local_sources, block_operands, exec_searches)


def _user_unit_paths(user: str) -> tuple[str, ...]:
    raw = _command(*_systemctl_prefix(user), "show", "--property=UnitPath", "--value")
    paths = tuple(raw.strip().split())
    if not paths or len(paths) > 128 or len(set(paths)) != len(paths) or any(not Path(path).is_absolute() or ".." in Path(path).parts or "\\" in path or len(path) > 4096 for path in paths):
        raise RouteManifestUnavailable("user manager load paths are unavailable or ambiguous")
    return paths


def _user_controllers(systemd: tuple[SystemdRouteFact, ...], cues: tuple[str, ...], module_sources: tuple[tuple[str, str], ...] = ()) -> tuple[UserControllerInventory, tuple[UserSystemdRouteFact, ...]]:
    pids = {}
    for fact in systemd:
        match = re.fullmatch(r"user@([0-9]+)\.service", fact.unit)
        if match:
            uid = int(match.group(1))
            if str(uid) != match.group(1) or uid in pids:
                raise RouteManifestUnavailable("installed user manager identity is ambiguous")
            pids[uid] = fact.main_pid
    try:
        inventory = observe_user_controller_inventory(pids, _user_unit_paths, cues=_MANAGEMENT_CUES + cues)
    except (OSError, ValueError) as exc:
        raise RouteManifestUnavailable("user controller inventory unavailable: " + str(exc)) from exc
    units = []
    for account in inventory.accounts:
        if not account.manager_pid:
            continue
        for unit in _systemd_catalog(user=account.name):
            units.append(UserSystemdRouteFact(account.uid, account.name, _systemd_unit(unit, trusted_uid=account.uid, user=account.name, extra_cues=cues, inspect_local_inputs=True, module_sources=module_sources)))
    return inventory, tuple(sorted(units, key=lambda item: (item.uid, item.fact.unit)))


def _deployment_root_bindings(approval: EnrollmentObservation, *, trusted_uid: int = 0) -> tuple[DeploymentRootBinding, ...]:
    """Derive scope roots from the exact enrolled pair, never a caller root list."""
    if os.environ.get("DOCKER_HOST") or os.environ.get("DOCKER_CONTEXT"):
        raise RouteManifestUnavailable("remote Docker root discovery is unsupported")
    selected = (approval.enrollment.management_id, approval.enrollment.proxy_id)
    raw = _command("docker", "inspect", "--type", "container", *selected, max_bytes=8 * _MAX_MANIFEST_BYTES)
    decoded = json.loads(raw, object_pairs_hook=unique_pairs)
    if not isinstance(decoded, list) or len(decoded) != 2 or {item.get("Id") for item in decoded if isinstance(item, dict)} != set(selected):
        raise RouteManifestUnavailable("deployment root Docker scope differs")
    result = []
    for container in decoded:
        if not isinstance(container, dict) or not isinstance(container.get("Mounts"), list) or len(container["Mounts"]) > 64:
            raise RouteManifestUnavailable("deployment root mount inventory is unsupported")
        for mount in container["Mounts"]:
            if not isinstance(mount, dict) or mount.get("Type") != "bind" or not isinstance(mount.get("Source"), str) or not isinstance(mount.get("Destination"), str) or type(mount.get("RW")) is not bool:
                raise RouteManifestUnavailable("deployment root mount is unsupported")
            path = Path(mount["Source"])
            if not path.is_absolute() or path == Path("/") or path != path.resolve(strict=True) or ".." in Path(mount["Destination"]).parts:
                raise RouteManifestUnavailable("deployment root source is noncanonical or ambiguous")
            _trusted_ancestors(path.parent, uid=trusted_uid)
            info = path.lstat()
            if info.st_uid != trusted_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
                raise RouteManifestUnavailable("deployment root source is mutable outside the trusted owner")
            result.append(DeploymentRootBinding(container["Id"], mount["Destination"], str(path), mount["RW"], info.st_dev, info.st_ino, info.st_mode))
    bindings = tuple(sorted(result, key=lambda item: (item.container_id, item.destination)))
    if not bindings or len({(item.container_id, item.destination) for item in bindings}) != len(bindings):
        raise RouteManifestUnavailable("deployment root inventory is absent or ambiguous")
    return bindings


def _cron_source_format(path: Path, formats: tuple[tuple[Path, str], ...]) -> str:
    """Use controller-owned path roles, never command/username token guesses."""
    matches = [(len(root.parts), kind) for root, kind in formats if path == root or path.is_relative_to(root)]
    if not matches:
        raise RouteManifestUnavailable("cron/at source lacks a supported installed path format")
    longest = max(length for length, _ in matches)
    selected = [kind for length, kind in matches if length == longest]
    if len(selected) != 1 or selected[0] not in ("system-crontab", "user-crontab", "anacrontab", "shell"):
        raise RouteManifestUnavailable("cron/at source format is ambiguous or unsupported")
    return selected[0]


def _cron_shell_text(text: str, kind: str) -> str:
    """Remove only the fixed schedule fields before existing shell inspection."""
    if kind == "shell":
        return text
    commands = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assignment = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*)", line)
        if assignment:
            name, value = assignment.groups()
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            if not re.fullmatch(r"[A-Za-z0-9_./:+@=-]*", value):
                raise RouteManifestUnavailable("cron environment assignment is outside the literal source profile")
            if name == "SHELL":
                if value not in ("/bin/sh", "/bin/dash", "/usr/bin/sh", "/usr/bin/dash"):
                    raise RouteManifestUnavailable("cron shell selection is outside the supported nonlogin profile")
                # This selects the fixed interpreter; it is not a command
                # invoking a shell with missing arguments. Raw bytes remain
                # bound in the enclosing cron entry's digest.
                continue
            commands.append(name + "=" + value)
            continue
        if kind == "anacrontab":
            fields = line.split(None, 3)
            if len(fields) != 4 or not re.fullmatch(r"(?:[0-9]{1,10}|@monthly)", fields[0]) or not re.fullmatch(r"[0-9]{1,10}", fields[1]) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", fields[2]):
                raise RouteManifestUnavailable("anacron schedule source is outside the supported format")
            command = fields[3]
        else:
            system = kind == "system-crontab"
            if line.startswith("@"):
                fields = line.split(None, 2 if system else 1)
                if len(fields) != (3 if system else 2) or fields[0] not in ("@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly"):
                    raise RouteManifestUnavailable("cron macro source is outside the supported format")
                user_index = 1
            else:
                fields = line.split(None, 6 if system else 5)
                if len(fields) != (7 if system else 6) or any(not re.fullmatch(r"[A-Za-z0-9*,/-]{1,128}", value) for value in fields[:5]):
                    raise RouteManifestUnavailable("cron schedule source is outside the supported format")
                user_index = 5
            if system and not re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_.-]{0,63}\$?|[0-9]{1,20})", fields[user_index]):
                raise RouteManifestUnavailable("system cron account field is outside the supported format")
            command = fields[-1]
        if "%" in command:
            raise RouteManifestUnavailable("cron percent/stdin dispatch is outside the supported format")
        commands.append(command)
    return "\n".join(commands)


def _cron_command_sources(data: bytes, path: Path, *, uid: int, cues: tuple[str, ...], module_sources: tuple[tuple[str, str], ...], source_formats: tuple[tuple[Path, str], ...]) -> tuple[tuple[CommandSourceFact, ...], bool]:
    source = data.decode("utf-8")
    kind = _cron_source_format(path, source_formats)
    sources, candidate = observe_command_sources(_cron_shell_text(source, kind), uid=uid, cues=cues, known_modules=module_sources, shell_source=True)
    return sources, candidate or any(cue in source for cue in cues)


def _cron_catalog(
    *, roots: tuple[Path, ...], trusted_uid: int = 0, extra_cues: tuple[str, ...] = (), inspect_local_inputs: bool = False, module_sources: tuple[tuple[str, str], ...] = (), source_formats: tuple[tuple[Path, str], ...] | None = None, systemd_facts: tuple[SystemdRouteFact, ...] = ()
) -> tuple[CronEntry, ...]:
    """Scan a fixed whole-cron/at surface; missing roots remain in the digest."""
    pending = list(roots)
    observed: dict[str, CronEntry] = {}
    aggregate_bytes = 0
    stock_inhibition: CronInhibitedSource | None = None
    local_sources: tuple[CommandSourceFact, ...] = ()
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
            inhibited_source = None
            formats = _CRON_SOURCE_FORMATS if source_formats is None else source_formats
            if inspect_local_inputs and name in STOCK_INPUTS and exact_stock_source(path, data, _cron_source_format(path, formats)):
                if info.st_uid != trusted_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise RouteManifestUnavailable("inhibited stock cron source is not owner-protected")
                if stock_inhibition is None:
                    try:
                        stock_inhibition = observe_inhibited_stock_cron_scope(systemd_facts)
                    except CronScopeRetainsTasks as exc:
                        # Only a typed current-task predicate exposes its bounded
                        # scope. Raw OS, command and diagnostic text stays private.
                        raise RouteManifestUnavailable(exc.public_reason) from exc
                inhibited_source = stock_inhibition
                local_sources, candidate = (), True
            else:
                local_sources, candidate = _cron_command_sources(data, path, uid=info.st_uid, cues=_MANAGEMENT_CUES + extra_cues, module_sources=module_sources, source_formats=formats) if inspect_local_inputs else ((), any(term.encode() in data for term in _MANAGEMENT_CUES + extra_cues))
            observed[name] = CronEntry(name, "regular", info.st_dev, info.st_ino, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, hashlib.sha256(data).hexdigest(), candidate, command_sources=local_sources, inhibited_source=inhibited_source)
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
            local_sources, candidate = (
                _cron_command_sources(data, path, uid=after.st_uid, cues=_MANAGEMENT_CUES + extra_cues, module_sources=module_sources, source_formats=_CRON_SOURCE_FORMATS if source_formats is None else source_formats)
                if inspect_local_inputs
                else ((), any(term.encode() in data for term in _MANAGEMENT_CUES + extra_cues))
            )
            observed[name] = CronEntry(name, "symlink", info.st_dev, info.st_ino, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, hashlib.sha256(data).hexdigest(), candidate, target_text, after.st_dev, after.st_ino, after.st_ctime_ns, local_sources)
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


def _deployment_cues(approval: EnrollmentObservation, bindings: tuple[DeploymentRootBinding, ...]) -> tuple[str, ...]:
    return tuple(sorted({item.source for item in bindings} | {item.path for item in approval.enrollment.original_files} | {approval.enrollment.management_id, approval.enrollment.proxy_id} | set(CRON_ROUTE_CUES)))


def _deployment_modules(approval: EnrollmentObservation) -> tuple[tuple[str, str], ...]:
    # This known original controller is supported only through its separately
    # approved source binding and isolated Python command, not arbitrary -m.
    return tuple(("auto_coder.management.proxy_watch_cli", item.path) for item in approval.enrollment.original_files if getattr(item, "purpose", None) == "watcher-source")


def _validate_user_scope(manifest: ApprovedRouteManifest, inventory: UserControllerInventory, facts: tuple[UserSystemdRouteFact, ...]) -> None:
    if _digest(asdict(inventory)) != manifest.user_controller_catalog_sha256:
        raise RouteManifestUnavailable("unknown, omitted or changed user controller account/source/lifetime scope")
    if tuple((item.uid, item.unit) for item in manifest.user_systemd_routes) != tuple((item.uid, item.fact.unit) for item in facts):
        raise RouteManifestUnavailable("unknown, omitted or changed user systemd unit inventory")
    approved = {(item.uid, item.unit): item for item in manifest.user_systemd_routes}
    for selected, actual in zip(manifest.user_systemd_routes, facts):
        fact = actual.fact
        if selected.sha256 != fact.sha256 or fact.needs_daemon_reload:
            raise RouteManifestUnavailable("effective user systemd route changed: " + actual.account_name + ":" + fact.unit)
        if fact.management_candidate and selected.role != "restarter":
            raise RouteManifestUnavailable("detected user management route is excluded: " + actual.account_name + ":" + fact.unit)
        if selected.role == "restarter" and not fact.inhibited:
            raise RouteManifestUnavailable("ordinary user systemd restarter remains enabled: " + actual.account_name + ":" + fact.unit)
    for account in inventory.accounts:
        for source in account.sources + inventory.global_sources:
            if not source.management_candidate:
                continue
            parts = Path(source.path).parts
            unit = next((part[:-2] if part.endswith(".d") and _UNIT_NAME.fullmatch(part[:-2]) else part for part in reversed(parts) if _UNIT_NAME.fullmatch(part) or part.endswith(".d") and _UNIT_NAME.fullmatch(part[:-2])), "")
            route = approved.get((account.uid, unit))
            if route is None or route.role != "restarter":
                raise RouteManifestUnavailable("installed user management source lacks an independently inhibited unit")


def _observe_approved_route_manifest(
    approval: EnrollmentObservation,
    docker: tuple[object, ...],
    *,
    root: Path,
    expected_uid: int,
    cron_roots: tuple[Path, ...] = _CRON_ROOTS,
    cron_source_formats: tuple[tuple[Path, str], ...] | None = None,
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
    is_closed_version = manifest.schema == "approved-management-ordinary-routes-v2"
    bindings = _deployment_root_bindings(approval, trusted_uid=expected_uid) if is_closed_version else ()
    if bindings != manifest.deployment_roots:
        raise RouteManifestUnavailable("original deployment root/config binding differs from approved routes")
    cues = _deployment_cues(approval, bindings) if is_closed_version else ()
    modules = _deployment_modules(approval) if is_closed_version else ()
    first_catalog = _systemd_catalog()
    if first_catalog != tuple(item.unit for item in manifest.systemd_routes):
        raise RouteManifestUnavailable("unknown, omitted or newly installed systemd route")
    observed_systemd = tuple(_systemd_unit(unit, trusted_uid=expected_uid, extra_cues=cues, inspect_local_inputs=is_closed_version, module_sources=modules) for unit in first_catalog)
    restarters = {item.unit for item in manifest.systemd_routes if item.role == "restarter"}
    for approved, actual in zip(manifest.systemd_routes, observed_systemd):
        if approved.sha256 != actual.sha256 or is_closed_version and actual.needs_daemon_reload:
            raise RouteManifestUnavailable("effective systemd route changed: " + approved.unit)
        if actual.management_candidate and approved.role != "restarter":
            raise RouteManifestUnavailable("detected management systemd route is excluded: " + approved.unit)
        if approved.role == "restarter" and not actual.inhibited:
            raise RouteManifestUnavailable("ordinary systemd restarter remains enabled: " + approved.unit)
    if manifest.cron_restarter_paths and not _CRON_SCHEDULERS.intersection(restarters):
        raise RouteManifestUnavailable("ordinary cron restarter lacks scheduler inhibition")
    cron = _cron_catalog(roots=cron_roots, trusted_uid=expected_uid, extra_cues=cues, inspect_local_inputs=is_closed_version, module_sources=modules, source_formats=cron_source_formats, systemd_facts=observed_systemd)
    if cron != manifest.cron_entries:
        raise RouteManifestUnavailable("unknown, omitted or changed cron/at route")
    if any(entry.management_candidate and entry.path not in manifest.cron_restarter_paths for entry in cron):
        raise RouteManifestUnavailable("detected management cron/at route is excluded")
    users = None
    user_units: tuple[UserSystemdRouteFact, ...] = ()
    if is_closed_version:
        users, user_units = _user_controllers(observed_systemd, cues, modules)
        _validate_user_scope(manifest, users, user_units)
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
    if (
        _systemd_catalog() != first_catalog
        or tuple(_systemd_unit(unit, trusted_uid=expected_uid, extra_cues=cues, inspect_local_inputs=is_closed_version, module_sources=modules) for unit in first_catalog) != observed_systemd
        or _cron_catalog(roots=cron_roots, trusted_uid=expected_uid, extra_cues=cues, inspect_local_inputs=is_closed_version, module_sources=modules, source_formats=cron_source_formats, systemd_facts=observed_systemd) != cron
    ):
        raise RouteManifestUnavailable("installed ordinary routes changed during observation")
    if is_closed_version and (_deployment_root_bindings(approval, trusted_uid=expected_uid) != bindings or _user_controllers(observed_systemd, cues, modules) != (users, user_units)):
        raise RouteManifestUnavailable("installed user controllers or deployment roots changed during observation")
    if any(_local_script(Path(route.path), expected_uid=expected_uid) != (route.sha256, route.device, route.inode) for route in manifest.local_script_routes):
        raise RouteManifestUnavailable("installed local script source changed during observation")
    last_content, last_info = _protected(path, uid=expected_uid, size_limit=_MAX_MANIFEST_BYTES)
    if last_content != content or _file_identity(last_info) != _file_identity(info):
        raise RouteManifestUnavailable("approved route manifest changed during observation")
    return RouteManifestObservation(manifest, hashlib.sha256(content).hexdigest(), info.st_dev, info.st_ino, info.st_ctime_ns, observed_systemd, cron, tuple(local), ids, is_closed_version, bindings, users, user_units)


def observe_approved_route_manifest(approval: EnrollmentObservation, docker: tuple[object, ...]) -> RouteManifestObservation:
    """Production reader has no caller-controlled approval or scan roots."""
    return _observe_approved_route_manifest(approval, docker, root=ENROLLMENT_ROOT, expected_uid=0)


def propose_unapproved_route_draft(approval: EnrollmentObservation, docker: tuple[object, ...], *, closed_profile: bool = False) -> dict[str, object]:
    """Provide inspectable values for a human review; never attest completeness.

    All exclusions and absent local/remote sources are unreviewed. This draft
    cannot be installed as usable approval until a trusted root explicitly
    audits and replaces the false completeness declaration.
    """
    bindings = _deployment_root_bindings(approval) if closed_profile else ()
    cues = _deployment_cues(approval, bindings) if closed_profile else ()
    modules = _deployment_modules(approval) if closed_profile else ()
    catalog = _systemd_catalog()
    systemd = tuple(_systemd_unit(unit, extra_cues=cues, inspect_local_inputs=closed_profile, module_sources=modules) for unit in catalog)
    cron = _cron_catalog(roots=_CRON_ROOTS, extra_cues=cues, inspect_local_inputs=closed_profile, module_sources=modules, systemd_facts=systemd)
    users, user_units = _user_controllers(systemd, cues, modules) if closed_profile else (None, ())
    return {
        "schema": "approved-management-ordinary-routes-v2" if closed_profile else "approved-management-ordinary-routes-v1",
        "deployment_id": approval.enrollment.deployment_id,
        "enrollment_id": approval.enrollment.enrollment_id,
        "authorization_id": approval.enrollment.authorization_id,
        "profile": approval.enrollment.profile,
        "enrollment_sha256": approval.enrollment_sha256,
        "approved_observation_only": True,
        "complete_ordinary_routes_attested": False,
        "systemd_routes": [
            asdict(SystemdRoute(item.unit, item.sha256, "restarter" if item.management_candidate or item.unit == "auto-coder-management-proxy-watch.service" or any(entry.inhibited_source is not None and entry.inhibited_source.scheduler_unit == item.unit for entry in cron) else "unrelated"))
            for item in systemd
        ],
        "docker_container_ids": sorted(getattr(item, "container_id", "") for item in docker),
        "cron_entries": [asdict(item) for item in cron],
        "cron_restarter_paths": [item.path for item in cron if item.management_candidate],
        "local_script_routes": [],
        "remote_script_origins": [],
        "deployment_roots": [asdict(item) for item in bindings],
        "user_controller_catalog_sha256": _digest(asdict(users)) if users is not None else "",
        "user_systemd_routes": [asdict(UserSystemdRoute(item.uid, item.fact.unit, item.fact.sha256, "restarter" if item.fact.management_candidate else "unrelated")) for item in user_units],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce a read-only, explicitly unapproved route manifest draft")
    parser.add_argument("propose", choices=("propose",))
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--closed-profile", action="store_true", help="Include independently read user controllers and exact deployment roots; the draft still requires separate approval")
    args = parser.parse_args()
    try:
        # Import here to avoid a module cycle between the observer and its
        # optional manifest component. Production enrollment guards apply.
        from .containment_enrollment import observe_approved_enrollment
        from .containment_observation import _docker, read_scope

        scope = read_scope(args.scope)
        approved = observe_approved_enrollment(scope)
        docker = _docker(scope)
        draft = propose_unapproved_route_draft(approved, docker, closed_profile=args.closed_profile)
        print(json.dumps({"status": "unapproved-root-review-required", "draft": draft}, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "unavailable", "reason": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
