"""Read-only, finite inhibition proof for measured Ubuntu stock cron inputs.

Opaque script support is confined to exact installed paths and source bytes.
It depends on a real masked cron controller, its reviewed vendor/PAM profile,
and empty current inhibited service and ordinary PAM user scopes. It grants no execution
right and asserts no historical source continuity or future exclusion.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .containment_enrollment import _trusted_ancestors
from .containment_route_sources import CommandSourceFact
from .containment_user_routes import _protected_source

PROFILE = "ubuntu24-stock-cron-inhibited-v1"
STOCK_INPUTS = {
    "/etc/cron.d/e2scrub_all": ("system-crontab", "d4df081b982b23e66cac7102f9d91032f296caf13031991d90af44ee6cbf6e3b"),
    "/etc/crontab": ("system-crontab", "ffb48ad57868ed639fad049d11ef4b9bcdd3d2d3e556754ce69b4d6b016969a3"),
    "/etc/cron.daily/apport": ("shell", "ece406240beddfd8d262a9f0f2ffd5aa40cae4bf5401e7641db3ae1aca737a39"),
    "/etc/cron.daily/apt-compat": ("shell", "1983c659b042b1ec26127e7874954d83cd97eb8dcfd03238a7d2031ea0182fbe"),
    "/etc/cron.daily/dpkg": ("shell", "9f2fdd4b4e7706dda74e8e443e1e1da0fbbb19c62a58e230e90d648b69177c35"),
}
CRON_ROUTE_CUES = (
    "/etc/crontab",
    "/etc/anacrontab",
    "/etc/cron.d",
    "/etc/cron.hourly",
    "/etc/cron.daily",
    "/etc/cron.weekly",
    "/etc/cron.monthly",
    "/var/spool/cron",
    "/var/spool/at",
    "/var/spool/atjobs",
    "/usr/sbin/cron",
    "/usr/sbin/crond",
    "/usr/sbin/anacron",
    "/usr/sbin/atd",
)
_SCHEDULERS = frozenset(("cron.service", "crond.service", "anacron.service", "atd.service"))
_PROFILE_FILES = {
    "/usr/lib/systemd/system/cron.service": ("/usr/lib/systemd/system/cron.service", "bbcd656457d22e12d8983ed0c253b5bd8cc6b12308fd16d9324400040de68e6f"),
    "/etc/pam.d/cron": ("/etc/pam.d/cron", "50a7062347b76fe4a429065547f2b53ffe49ac7d896f6b238010a3ee90446a84"),
    "/etc/pam.d/common-auth": ("/etc/pam.d/common-auth", "b787b01c7e71c658fa9acb84cbe153f71786cf380af4b01ae9135210cdc0154d"),
    "/etc/pam.d/common-account": ("/etc/pam.d/common-account", "aa8a63d72e79399b6c51ebe4e9f828c954145a799eb4b8f3224724f51cbb9fac"),
    "/etc/pam.d/common-session-noninteractive": ("/etc/pam.d/common-session-noninteractive", "1bd1ef2b6241fc8b3c97bbaf57e5c6038c96c275e30c62ebee6eec15701e2e07"),
    "/etc/default/cron": ("/etc/default/cron", "6a8f0b25001017f55a227be357cd418e46f514acd58c069d17a78c3072d9d569"),
    "/etc/environment": ("/etc/environment", "3d88795b8d42b87bbbf91f8d30dae43b6db9ecd4b1eb93a883062352c902ff5d"),
    "/etc/default/locale": ("/etc/locale.conf", "89dd29db91ea608d72b5b4d3d3f5816cc2d3c1dd730741dc41b20ce12f1c2b3b"),
}
_OTHER_EXECUTABLES = ("/usr/sbin/crond", "/usr/sbin/anacron", "/usr/sbin/atd")
_CGROUP = "/system.slice/cron.service"
_USER_SCOPE = "/user.slice"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_MAX_GROUPS = 8192
_MAX_TASKS = 131072
_MAX_SERVICES = 4096
# systemd v255 cg_needs_escape also prefixes controller.* cgroup names.
_ESCAPED_SERVICE_PREFIXES = ("cgroup", "cpu", "cpuacct", "cpuset", "io", "blkio", "memory", "devices", "pids", "bpf-firewall", "bpf-devices", "bpf-foreign", "bpf-socket-bind", "bpf-restrict-network-interfaces")
_PROPERTIES = ("Id", "LoadState", "UnitFileState", "ActiveState", "SubState", "MainPID", "ControlPID", "NeedDaemonReload", "FragmentPath", "SourcePath", "DropInPaths", "ControlGroup", "Slice", "Delegate")


class CronInhibitionUnavailable(ValueError):
    """The bounded stock cron execution scope is not currently closed."""


@dataclass(frozen=True)
class CgroupDirectoryIdentity:
    path: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def parse(cls, value: object) -> CgroupDirectoryIdentity:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise CronInhibitionUnavailable("cron cgroup identity schema differs")
        if not isinstance(value["path"], str) or not value["path"].startswith("/") or ".." in Path(value["path"]).parts or len(value["path"]) > 4096:
            raise CronInhibitionUnavailable("cron cgroup identity path differs")
        if any(type(value[key]) is not int or value[key] < 0 for key in ("device", "inode", "mode", "uid", "gid")) or value["inode"] == 0:
            raise CronInhibitionUnavailable("cron cgroup object identity differs")
        return cls(**value)


@dataclass(frozen=True)
class EmptyCgroupFact:
    path: str
    processes: tuple[int, ...]
    threads: tuple[int, ...]
    populated: int
    frozen: int

    @classmethod
    def parse(cls, value: object) -> EmptyCgroupFact:
        if (
            not isinstance(value, dict)
            or set(value) != set(cls.__dataclass_fields__)
            or value.get("processes") != []
            or value.get("threads") != []
            or type(value.get("populated")) is not int
            or value["populated"] != 0
            or type(value.get("frozen")) is not int
            or value["frozen"] not in (0, 1)
            or not isinstance(value.get("path"), str)
        ):
            raise CronInhibitionUnavailable("cron empty-task evidence differs")
        return cls(value["path"], (), (), value["populated"], value["frozen"])


@dataclass(frozen=True)
class CronCgroupScope:
    mount_id: int
    mount_device: str
    directories: tuple[CgroupDirectoryIdentity, ...]
    required_empty_roots: tuple[str, ...]
    missing_roots: tuple[str, ...]
    empty_groups: tuple[EmptyCgroupFact, ...]

    @classmethod
    def parse(cls, value: object, *, inhibited_units: tuple[str, ...] = ("cron.service",)) -> CronCgroupScope:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__) or type(value.get("mount_id")) is not int or value["mount_id"] <= 0 or not isinstance(value.get("mount_device"), str) or not re.fullmatch(r"[0-9]+:[0-9]+", value["mount_device"]):
            raise CronInhibitionUnavailable("cron cgroup mount evidence differs")
        for key in ("directories", "required_empty_roots", "missing_roots", "empty_groups"):
            if not isinstance(value[key], list) or len(value[key]) > _MAX_GROUPS:
                raise CronInhibitionUnavailable("cron cgroup inventory exceeds its bound")
        directories = tuple(CgroupDirectoryIdentity.parse(item) for item in value["directories"])
        empty = tuple(EmptyCgroupFact.parse(item) for item in value["empty_groups"])
        paths = tuple(item.path for item in directories)
        roots, missing = tuple(value["required_empty_roots"]), tuple(value["missing_roots"])
        if not paths or paths[0] != "/" or paths != tuple(sorted(set(paths))) or any(not isinstance(path, str) for path in roots + missing):
            raise CronInhibitionUnavailable("cron cgroup path inventory differs")
        if roots != _required_scope_roots(paths, inhibited_units) or missing != tuple(sorted(set(missing))) or not set(missing) <= set(roots):
            raise CronInhibitionUnavailable("cron required execution scopes differ")
        expected = tuple(path for path in paths if any(path == root or path.startswith(root + "/") for root in roots))
        if tuple(item.path for item in empty) != expected or set(missing) != {root for root in roots if root not in paths}:
            raise CronInhibitionUnavailable("cron scope task coverage differs")
        return cls(value["mount_id"], value["mount_device"], directories, roots, missing, empty)


@dataclass(frozen=True)
class CronInhibitedSource:
    schema: str
    boot_id: str
    scheduler_unit: str
    scheduler_sha256: str
    controller_properties: tuple[tuple[str, str], ...]
    sources: tuple[CommandSourceFact, ...]
    mask_path: str
    mask_identity: tuple[int, ...]
    cgroups: CronCgroupScope
    inhibited_services: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, value: object) -> CronInhibitedSource:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__) or value.get("schema") != PROFILE or value.get("scheduler_unit") != "cron.service":
            raise CronInhibitionUnavailable("stock cron inhibition profile schema differs")
        if not isinstance(value.get("boot_id"), str) or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["boot_id"]) or not isinstance(value.get("scheduler_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["scheduler_sha256"]):
            raise CronInhibitionUnavailable("stock cron boot/controller binding differs")
        bindings = value.get("inhibited_services")
        if not isinstance(bindings, list) or not 0 < len(bindings) <= _MAX_SERVICES or any(not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not isinstance(row[1], str) or not re.fullmatch(r"[0-9a-f]{64}", row[1]) for row in bindings):
            raise CronInhibitionUnavailable("stock cron inhibited service bindings differ")
        services = tuple((row[0], row[1]) for row in bindings)
        units = tuple(unit for unit, _ in services)
        _validate_service_units(units)
        if ("cron.service", value["scheduler_sha256"]) not in services:
            raise CronInhibitionUnavailable("stock cron scheduler binding differs from inhibited services")
        rows = value.get("controller_properties")
        if not isinstance(rows, list) or len(rows) != len(_PROPERTIES) or any(not isinstance(row, list) or len(row) != 2 or any(not isinstance(item, str) or len(item) > 4096 for item in row) for row in rows):
            raise CronInhibitionUnavailable("stock cron effective properties differ")
        properties = tuple((row[0], row[1]) for row in rows)
        if tuple(key for key, _ in properties) != tuple(sorted(_PROPERTIES)):
            raise CronInhibitionUnavailable("stock cron effective property identity differs")
        _validate_properties(dict(properties))
        files = value.get("sources")
        if not isinstance(files, list) or len(files) > 32:
            raise CronInhibitionUnavailable("stock cron source inventory differs")
        sources = tuple(CommandSourceFact.parse(item) for item in files)
        expected = tuple(sorted((*_PROFILE_FILES, "/usr/sbin/cron", *_OTHER_EXECUTABLES)))
        if tuple(item.path for item in sources) != expected:
            raise CronInhibitionUnavailable("stock cron vendor/PAM source coverage differs")
        for item in sources:
            if item.path in _PROFILE_FILES and (item.resolved, item.sha256) != _PROFILE_FILES[item.path] or item.path in _OTHER_EXECUTABLES and item.kind != "missing":
                raise CronInhibitionUnavailable("stock cron vendor/PAM source profile differs")
        mask = value.get("mask_identity")
        if value.get("mask_path") not in ("/etc/systemd/system/cron.service", "/run/systemd/system/cron.service") or not isinstance(mask, list) or len(mask) != 6 or any(type(item) is not int or item < 0 for item in mask):
            raise CronInhibitionUnavailable("stock cron mask binding differs")
        if value["mask_path"] != dict(properties)["FragmentPath"] or mask[1] == 0 or not stat.S_ISLNK(mask[3]) or mask[4] != 0:
            raise CronInhibitionUnavailable("stock cron protected mask identity differs")
        return cls(PROFILE, value["boot_id"], "cron.service", value["scheduler_sha256"], properties, sources, value["mask_path"], tuple(mask), CronCgroupScope.parse(value["cgroups"], inhibited_units=units), services)


def _validate_service_units(units: tuple[str, ...]) -> None:
    # Literal service names map to literal cgroup components. Templates,
    # instances, escaping and reserved controller prefixes need other profiles.
    if not 0 < len(units) <= _MAX_SERVICES or units != tuple(sorted(set(units))) or "cron.service" not in units or any(len(unit) > 255 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.service", unit) or any(unit.startswith(prefix + ".") for prefix in _ESCAPED_SERVICE_PREFIXES) for unit in units):
        raise CronInhibitionUnavailable("stock cron inhibited service cgroup mapping is unsupported")


def _required_scope_roots(paths: tuple[str, ...], units: tuple[str, ...]) -> tuple[str, ...]:
    _validate_service_units(units)
    return tuple(sorted({_USER_SCOPE, *("/system.slice/" + unit for unit in units)} | {path for path in paths if Path(path).name in units}))


def _inhibited_service_bindings(systemd_facts: tuple[object, ...]) -> tuple[tuple[str, str], ...]:
    bindings = []
    for fact in systemd_facts:
        unit = getattr(fact, "unit", "")
        state = getattr(fact, "unit_file_state", "")
        if not isinstance(unit, str) or not unit.endswith(".service") or not (state in ("masked", "masked-runtime") or getattr(fact, "inhibited", False) is True):
            continue
        # A masked template is deliberately not .inhibited in the unit reader;
        # reject its unsupported mapping before that boolean can omit its scope.
        _validate_service_units(tuple(sorted({"cron.service", unit})))
        digest = getattr(fact, "sha256", "")
        if (
            getattr(fact, "inhibited", False) is not True
            or state != "masked"
            or getattr(fact, "active_state", "") != "inactive"
            or getattr(fact, "main_pid", None) != 0
            or getattr(fact, "needs_daemon_reload", True) is not False
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise CronInhibitionUnavailable("stock cron alternative service is not independently inhibited: " + unit)
        bindings.append((unit, digest))
    result = tuple(sorted(bindings))
    _validate_service_units(tuple(unit for unit, _ in result))
    return result


def exact_stock_source(path: Path, data: bytes, kind: str) -> bool:
    return STOCK_INPUTS.get(str(path)) == (kind, hashlib.sha256(data).hexdigest())


def _read(path: Path, limit: int = 1024 * 1024) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CronInhibitionUnavailable("stock cron kernel read exceeded its bound")
    return data.decode("ascii")


def _directory(path: Path, relative: str) -> CgroupDirectoryIdentity:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise CronInhibitionUnavailable("stock cron cgroup path is not a directory")
    return CgroupDirectoryIdentity(relative, info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def _mount(root: Path, mountinfo: Path) -> tuple[int, str]:
    matches = []
    for line in _read(mountinfo).splitlines():
        left, separator, right = line.partition(" - ")
        fields, fs = left.split(), right.split()
        if separator and len(fields) >= 6 and fields[4] == str(root):
            if fields[3] != "/" or not fs or fs[0] != "cgroup2" or not fields[0].isdecimal() or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
                raise CronInhibitionUnavailable("stock cron requires a complete unified cgroup2 mount")
            matches.append((int(fields[0]), fields[2]))
    if len(matches) != 1 or root.resolve(strict=True) != root:
        raise CronInhibitionUnavailable("stock cron cgroup mount is unavailable or redirected")
    info = root.stat()
    if matches[0][1] != str(os.major(info.st_dev)) + ":" + str(os.minor(info.st_dev)):
        raise CronInhibitionUnavailable("stock cron cgroup device differs from mount authority")
    return matches[0]


def _empty_group(path: Path, relative: str) -> EmptyCgroupFact:
    procs = _read(path / "cgroup.procs", 1024 * 1024).split()
    threads = _read(path / "cgroup.threads", 1024 * 1024).split()
    events = _read(path / "cgroup.events", 4096).splitlines()
    pairs = [line.split() for line in events]
    if any(len(pair) != 2 for pair in pairs) or len(pairs) != 2 or {pair[0] for pair in pairs} != {"populated", "frozen"} or any(pair[1] not in ("0", "1") for pair in pairs):
        raise CronInhibitionUnavailable("stock cron cgroup event evidence is incomplete")
    values = dict(pairs)
    if procs or threads or values["populated"] != "0":
        raise CronInhibitionUnavailable("inhibited stock cron scope retains tasks: " + relative)
    return EmptyCgroupFact(relative, (), (), 0, int(values["frozen"]))


def _proc_scopes_empty(roots: tuple[str, ...], *, proc: Path = Path("/proc"), inhibited_units: tuple[str, ...] = ("cron.service",)) -> None:
    pids = tuple(sorted(path.name for path in proc.iterdir() if path.name.isdecimal()))
    count = 0
    for pid in pids:
        task_root = proc / pid / "task"
        tids = tuple(sorted(path.name for path in task_root.iterdir() if path.name.isdecimal()))
        for tid in tids:
            count += 1
            if count > _MAX_TASKS:
                raise CronInhibitionUnavailable("stock cron task inventory exceeds its bound")
            task = task_root / tid
            before = _read(task / "stat", 16384)
            tail = before.rpartition(")")[2].split()
            if len(tail) < 20 or not tail[19].isdecimal():
                raise CronInhibitionUnavailable("stock cron task lifetime is unavailable")
            groups = _read(task / "cgroup", 16384).splitlines()
            if len(groups) != 1 or not groups[0].startswith("0::/"):
                raise CronInhibitionUnavailable("stock cron task cgroup membership is unsupported")
            group = groups[0][3:]
            if any(group == root or group.startswith(root + "/") for root in roots) or any(part in inhibited_units for part in Path(group).parts):
                raise CronInhibitionUnavailable("inhibited stock cron scope retains tasks: pid=" + pid + " tid=" + tid + " state=" + tail[0] + " start=" + tail[19])
            after = _read(task / "stat", 16384).rpartition(")")[2].split()
            if len(after) < 20 or after[19] != tail[19] or _read(task / "cgroup", 16384).splitlines() != groups:
                raise CronInhibitionUnavailable("stock cron task changed during scope inspection")
        if tids != tuple(sorted(path.name for path in task_root.iterdir() if path.name.isdecimal())):
            raise CronInhibitionUnavailable("stock cron thread inventory changed during inspection")
        # A daemon started outside its normal unit still remains an ordinary
        # scheduler, even if that service's own cgroup is empty.
        with (proc / pid / "cmdline").open("rb") as stream:
            command = stream.read(131073)
        if len(command) > 131072:
            raise CronInhibitionUnavailable("stock cron process command exceeds its bound")
        if command:
            executable = os.readlink(proc / pid / "exe")
            if executable.removesuffix(" (deleted)") in ("/usr/sbin/cron", *_OTHER_EXECUTABLES):
                raise CronInhibitionUnavailable("inhibited stock cron scope retains tasks: scheduler pid=" + pid)
    if pids != tuple(sorted(path.name for path in proc.iterdir() if path.name.isdecimal())):
        raise CronInhibitionUnavailable("stock cron process inventory changed during inspection")


def _cgroup_snapshot(*, root: Path = _CGROUP_ROOT, mountinfo: Path = Path("/proc/self/mountinfo"), proc: Path = Path("/proc"), inhibited_units: tuple[str, ...] = ("cron.service",)) -> CronCgroupScope:
    _validate_service_units(inhibited_units)
    mount_id, mount_device = _mount(root, mountinfo)
    pending = [(root, "/")]
    directories: list[CgroupDirectoryIdentity] = []
    while pending:
        path, relative = pending.pop()
        if len(directories) >= _MAX_GROUPS or len(Path(relative).parts) > 64:
            raise CronInhibitionUnavailable("stock cron cgroup inventory exceeds its bound")
        identity = _directory(path, relative)
        if identity.device != root.stat().st_dev:
            raise CronInhibitionUnavailable("stock cron cgroup inventory crosses a filesystem")
        children = []
        with os.scandir(path) as stream:
            for entry in stream:
                if entry.is_symlink():
                    raise CronInhibitionUnavailable("stock cron cgroup inventory contains a symbolic link")
                if entry.is_dir(follow_symlinks=False):
                    children.append(entry.name)
                if len(children) > _MAX_GROUPS:
                    raise CronInhibitionUnavailable("stock cron cgroup directory exceeds its bound")
        if _directory(path, relative) != identity:
            raise CronInhibitionUnavailable("stock cron cgroup changed during enumeration")
        directories.append(identity)
        pending.extend((path / name, relative.rstrip("/") + "/" + name) for name in children)
    ordered = tuple(sorted(directories, key=lambda item: item.path))
    paths = {item.path for item in ordered}
    if "/system.slice" not in paths:
        raise CronInhibitionUnavailable("stock cron system slice authority is unavailable")
    roots = _required_scope_roots(tuple(sorted(paths)), inhibited_units)
    empty = tuple(_empty_group(root / item.path.lstrip("/"), item.path) for item in ordered if any(item.path == scope or item.path.startswith(scope + "/") for scope in roots))
    _proc_scopes_empty(roots, proc=proc, inhibited_units=inhibited_units)
    for item in ordered:
        if _directory(root / item.path.lstrip("/"), item.path) != item:
            raise CronInhibitionUnavailable("stock cron cgroup identity changed during task inspection")
    if _mount(root, mountinfo) != (mount_id, mount_device):
        raise CronInhibitionUnavailable("stock cron cgroup mount changed during inspection")
    return CronCgroupScope(mount_id, mount_device, ordered, roots, tuple(scope for scope in roots if scope not in paths), empty)


def _profile_source(path: Path, *, expected: tuple[str, str] | None = None, uid: int = 0) -> CommandSourceFact:
    _trusted_ancestors(path.parent, uid=uid)
    original = path.lstat()
    resolved = path.resolve(strict=True)
    _trusted_ancestors(resolved.parent, uid=uid)
    data, target = _protected_source(resolved, owners=frozenset((uid,)))
    digest = hashlib.sha256(data).hexdigest()
    if original.st_uid != uid or (expected is not None and (str(resolved), digest) != expected) or (expected is None and (resolved != path or not target.st_mode & stat.S_IXUSR)):
        raise CronInhibitionUnavailable("stock cron vendor/PAM/executable profile differs: " + str(path))
    if path.resolve(strict=True) != resolved or (original.st_dev, original.st_ino, original.st_ctime_ns) != (path.lstat().st_dev, path.lstat().st_ino, path.lstat().st_ctime_ns):
        raise CronInhibitionUnavailable("stock cron source changed during inspection")
    return CommandSourceFact(str(path), str(resolved), "file", original.st_dev, original.st_ino, original.st_ctime_ns, original.st_mode, original.st_uid, digest, target.st_dev, target.st_ino, target.st_ctime_ns)


def _profile_sources() -> tuple[CommandSourceFact, ...]:
    sources = [_profile_source(Path(name), expected=expected) for name, expected in _PROFILE_FILES.items()]
    sources.append(_profile_source(Path("/usr/sbin/cron")))
    for name in _OTHER_EXECUTABLES:
        path = Path(name)
        try:
            path.lstat()
        except FileNotFoundError:
            sources.append(CommandSourceFact(name, name, "missing", 0, 0, 0, 0, 0, ""))
        else:
            raise CronInhibitionUnavailable("stock cron profile has an unsupported additional scheduler executable: " + name)
    return tuple(sorted(sources, key=lambda item: item.path))


def _validate_properties(properties: dict[str, str]) -> None:
    required = {"Id": "cron.service", "LoadState": "masked", "UnitFileState": "masked", "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", "ControlPID": "0", "NeedDaemonReload": "no", "SourcePath": "", "DropInPaths": "", "Delegate": "no"}
    if any(properties.get(key) != value for key, value in required.items()) or properties.get("FragmentPath") not in ("/etc/systemd/system/cron.service", "/run/systemd/system/cron.service") or properties.get("ControlGroup") not in ("", _CGROUP) or properties.get("Slice") not in ("", "system.slice"):
        raise CronInhibitionUnavailable("stock cron controller is not independently masked and inactive")


def _properties() -> tuple[tuple[str, str], ...]:
    result = subprocess.run(("systemctl", "show", "--all", "--property=" + ",".join(_PROPERTIES), "--", "cron.service"), capture_output=True, text=True, check=True, timeout=15)
    if len(result.stdout) > 65536 or len(result.stderr) > 4096:
        raise CronInhibitionUnavailable("stock cron controller readback exceeds its bound")
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in _PROPERTIES or key in values:
            raise CronInhibitionUnavailable("stock cron controller readback is ambiguous")
        values[key] = value
    if set(values) != set(_PROPERTIES):
        raise CronInhibitionUnavailable("stock cron controller readback is incomplete")
    _validate_properties(values)
    return tuple(sorted(values.items()))


def _snapshot(scheduler_sha256: str, inhibited_services: tuple[tuple[str, str], ...]) -> CronInhibitedSource:
    boot = _read(Path("/proc/sys/kernel/random/boot_id"), 128).strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", boot):
        raise CronInhibitionUnavailable("stock cron boot identity is unavailable")
    properties = _properties()
    mask = Path(dict(properties)["FragmentPath"])
    _trusted_ancestors(mask.parent, uid=0)
    info = mask.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or os.readlink(mask) != "/dev/null":
        raise CronInhibitionUnavailable("stock cron protected mask is unavailable")
    identity = (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)
    return CronInhibitedSource(PROFILE, boot, "cron.service", scheduler_sha256, properties, _profile_sources(), str(mask), identity, _cgroup_snapshot(inhibited_units=tuple(unit for unit, _ in inhibited_services)), inhibited_services)


def observe_inhibited_stock_cron_scope(systemd_facts: tuple[object, ...]) -> CronInhibitedSource:
    installed = []
    for fact in systemd_facts:
        unit = getattr(fact, "unit", None)
        state = getattr(fact, "unit_file_state", None)
        active = getattr(fact, "active_state", None)
        pid = getattr(fact, "main_pid", None)
        if unit in _SCHEDULERS and (state != "" or active != "inactive" or pid != 0):
            installed.append(fact)
    if len(installed) != 1 or getattr(installed[0], "unit", "") != "cron.service" or getattr(installed[0], "inhibited", False) is not True or getattr(installed[0], "needs_daemon_reload", True) is not False:
        raise CronInhibitionUnavailable("stock cron profile requires exactly its real inhibited installed scheduler")
    digest = getattr(installed[0], "sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CronInhibitionUnavailable("stock cron controller observation identity is unavailable")
    try:
        services = _inhibited_service_bindings(systemd_facts)
        first = _snapshot(digest, services)
        if _snapshot(digest, services) != first:
            raise CronInhibitionUnavailable("stock cron source/controller/task scope changed during observation")
        return first
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, CronInhibitionUnavailable):
            raise
        raise CronInhibitionUnavailable("stock cron source/controller/task authority unavailable: " + str(exc)) from exc
