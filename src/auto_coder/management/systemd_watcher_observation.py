"""Read-only evidence for the installed management proxy watcher systemd route.

This is one ordinary restarter component, not a complete installed-route
inventory or a deployment-containment verdict. A masked unit is only evidence
for this exact unit at the time of the two systemd and filesystem reads.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

WATCHER_UNIT = "auto-coder-management-proxy-watch.service"
WATCHER_UNIT_PATH = Path("/etc/systemd/system") / WATCHER_UNIT
_PROPERTIES = ("Id", "LoadState", "UnitFileState", "ActiveState", "SubState", "MainPID", "NeedDaemonReload", "FragmentPath")
_OUTPUT_LIMIT = 8192


@dataclass(frozen=True)
class WatcherSystemdObservation:
    """A bounded, independently reread watcher unit and mask-file identity."""

    unit: str
    fragment_path: str
    load_state: str
    unit_file_state: str
    active_state: str
    sub_state: str
    main_pid: int
    need_daemon_reload: bool
    unit_file_device: int
    unit_file_inode: int
    unit_file_ctime_ns: int
    unit_file_uid: int
    unit_file_mode: int
    unit_file_target: str
    inhibited: bool


def _show(unit: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.service", unit):
        raise ValueError("invalid fixed watcher unit name")
    result = subprocess.run(("systemctl", "show", unit, "--property=" + ",".join(_PROPERTIES)), capture_output=True, text=True, timeout=10, check=True)
    if len(result.stdout) > _OUTPUT_LIMIT:
        raise ValueError("systemd watcher observation exceeds size limit")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in _PROPERTIES or key in fields or len(value) > 4096:
            raise ValueError("ambiguous watcher systemd property")
        fields[key] = value
    if set(fields) != set(_PROPERTIES) or fields["Id"] != unit or not fields["MainPID"].isdecimal() or fields["NeedDaemonReload"] not in ("yes", "no"):
        raise ValueError("watcher systemd properties are incomplete or contradictory")
    if any(not fields[key] or not re.fullmatch(r"[a-z-]+", fields[key]) for key in ("LoadState", "UnitFileState", "ActiveState", "SubState")):
        raise ValueError("watcher systemd states are unsupported")
    return fields


def _link(path: Path) -> tuple[int, int, int, int, int, str]:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return (0, 0, 0, 0, 0, "")
    target = os.readlink(path) if stat.S_ISLNK(entry.st_mode) else ""
    if len(target) > 4096:
        raise ValueError("watcher systemd unit link exceeds size bound")
    return (entry.st_dev, entry.st_ino, entry.st_ctime_ns, entry.st_uid, stat.S_IMODE(entry.st_mode), target)


def _observe(unit: str, path: Path) -> WatcherSystemdObservation:
    first = _show(unit)
    first_link = _link(path)
    second_link = _link(path)
    second = _show(unit)
    if first != second or first_link != second_link:
        raise ValueError("watcher systemd state or unit identity changed during observation")
    inhibited = (
        first["LoadState"] == "masked"
        and first["UnitFileState"] == "masked"
        and first["ActiveState"] == "inactive"
        and first["SubState"] == "dead"
        and first["MainPID"] == "0"
        and first["NeedDaemonReload"] == "no"
        and first["FragmentPath"] == str(path)
        and first_link[0] > 0
        and first_link[1] > 0
        and first_link[3] == 0
        and first_link[5] == "/dev/null"
    )
    return WatcherSystemdObservation(
        unit,
        first["FragmentPath"],
        first["LoadState"],
        first["UnitFileState"],
        first["ActiveState"],
        first["SubState"],
        int(first["MainPID"]),
        first["NeedDaemonReload"] == "yes",
        *first_link,
        inhibited,
    )


def observe_watcher_unit() -> WatcherSystemdObservation:
    """Inspect the actual installed unit; callers cannot select an alternate unit."""
    return _observe(WATCHER_UNIT, WATCHER_UNIT_PATH)
