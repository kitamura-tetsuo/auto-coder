"""Read the finite retained watcher unit behind its actual systemd mask.

Masked effective units have no executable command to inspect. This recovery-only
profile independently binds the retained fragment selected from the real system
manager's load paths. It never changes a unit, mask, controller or approval.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from .containment_enrollment import _trusted_ancestors
from .containment_route_manifest import SystemdRouteFact, _command
from .recovery_containment_model import RecoveryContainmentUnavailable, RecoveryFile, RecoveryWatcher
from .recovery_containment_profile import read_bound_file

_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.service")
_LITERAL_PATH = re.compile(r"/[A-Za-z0-9_./:+@=-]+")
_MAX_ENTRIES = 16384


@dataclass(frozen=True)
class RecoveryWatcherUnitFact:
    source: RecoveryFile
    interpreter: str
    load_paths: tuple[str, ...]
    load_roots: tuple[tuple[str, int, int, int, int, int], ...]
    mask_path: str
    mask_identity: tuple[int, ...]


def _load_paths() -> tuple[str, ...]:
    raw = _command("systemctl", "show", "--property=UnitPath", "--value", max_bytes=16384)
    paths = tuple(raw.strip().split())
    if not 1 <= len(paths) <= 32 or len(set(paths)) != len(paths) or any(not _LITERAL_PATH.fullmatch(path) or ".." in Path(path).parts or str(Path(path)) != path for path in paths):
        raise RecoveryContainmentUnavailable("recovery watcher system unit load paths are unsupported")
    return paths


def _drop_in_names(unit: str) -> tuple[str, ...]:
    stem = unit.removesuffix(".service")
    prefixes = [stem[: index + 1] + ".service.d" for index, character in enumerate(stem) if character == "-"]
    return tuple(sorted({"service.d", unit + ".d", *prefixes}))


def _literal_command(content: bytes, selected: RecoveryWatcher) -> None:
    if len(content) > 16384 or any(value < 32 and value not in (9, 10) or value >= 127 for value in content) or not _UNIT.fullmatch(selected.unit):
        raise RecoveryContainmentUnavailable("recovery watcher retained unit is outside the finite profile")
    expected = sys.executable + " -I -B -m auto_coder.management.proxy_watch_cli --config " + selected.configuration.path
    if any(not _LITERAL_PATH.fullmatch(path) or ".." in Path(path).parts for path in (sys.executable, selected.configuration.path)):
        raise RecoveryContainmentUnavailable("recovery watcher literal interpreter/configuration path is unsupported")
    sections: dict[str, dict[str, str]] = {}
    section = ""
    for raw in content.decode("ascii").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[Unit]", "[Service]"):
            section = line[1:-1]
            if section in sections:
                raise RecoveryContainmentUnavailable("recovery watcher retained unit repeats a section")
            sections[section] = {}
            continue
        key, separator, value = line.partition("=")
        allowed = {"Description"} if section == "Unit" else {"Type", "ExecStart", "Restart", "StandardOutput", "StandardError"} if section == "Service" else set()
        if not separator or key not in allowed or key in sections[section] or not value or any(character in value for character in ("\\", "%", "\r", "\x00")):
            raise RecoveryContainmentUnavailable("recovery watcher retained unit has unsupported directives")
        sections[section][key] = value
    service = sections.get("Service", {})
    if service.get("Type") != "simple" or service.get("Restart") != "no" or service.get("ExecStart") != expected:
        raise RecoveryContainmentUnavailable("recovery watcher command does not bind this interpreter/module/configuration")
    for name in ("StandardOutput", "StandardError"):
        value = service.get(name, "journal")
        if value not in ("journal", "null") and (not value.startswith("append:") or not _LITERAL_PATH.fullmatch(value.removeprefix("append:")) or ".." in Path(value.removeprefix("append:")).parts):
            raise RecoveryContainmentUnavailable("recovery watcher retained output selection is unsupported")


def _scope(selected: RecoveryWatcher, *, expected_uid: int) -> RecoveryWatcherUnitFact:
    paths = _load_paths()
    source = Path(selected.unit_source.path)
    if source.name != selected.unit or str(source.parent) not in paths:
        raise RecoveryContainmentUnavailable("recovery watcher retained source is outside the actual unit load path")
    roots = []
    masks: list[tuple[int, str, tuple[int, ...]]] = []
    fragments: list[tuple[int, str]] = []
    aliases: list[tuple[str, str]] = []
    entries = 0
    for priority, name in enumerate(paths):
        root = Path(name)
        if root.resolve(strict=False) != root:
            raise RecoveryContainmentUnavailable("recovery watcher unit load path is redirected")
        try:
            info = root.lstat()
        except FileNotFoundError:
            ancestor = root.parent
            while not ancestor.exists():
                ancestor = ancestor.parent
            _trusted_ancestors(ancestor, uid=expected_uid)
            roots.append((name, 0, 0, 0, 0, 0))
            continue
        _trusted_ancestors(root, uid=expected_uid)
        roots.append((name, info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid))
        for drop_in in _drop_in_names(selected.unit):
            try:
                (root / drop_in).lstat()
            except FileNotFoundError:
                continue
            raise RecoveryContainmentUnavailable("recovery watcher retained unit has unsupported drop-ins")
        candidate = root / selected.unit
        try:
            candidate_info = candidate.lstat()
        except FileNotFoundError:
            candidate_info = None
        if candidate_info is not None:
            if candidate_info.st_uid != expected_uid or candidate_info.st_nlink != 1:
                raise RecoveryContainmentUnavailable("recovery watcher fragment/mask is not independently protected")
            if stat.S_ISLNK(candidate_info.st_mode) and os.readlink(candidate) == "/dev/null":
                identity = (candidate_info.st_dev, candidate_info.st_ino, candidate_info.st_ctime_ns, candidate_info.st_mode, candidate_info.st_uid, candidate_info.st_gid)
                masks.append((priority, str(candidate), identity))
            elif stat.S_ISREG(candidate_info.st_mode) and candidate == source:
                fragments.append((priority, str(candidate)))
            else:
                raise RecoveryContainmentUnavailable("recovery watcher retained fragment selection is ambiguous")
        with os.scandir(root) as listing:
            for entry in listing:
                entries += 1
                if entries > _MAX_ENTRIES:
                    raise RecoveryContainmentUnavailable("recovery watcher unit alias inventory exceeds its bound")
                if entry.name == selected.unit or not entry.name.endswith(".service") or not entry.is_symlink():
                    continue
                target = os.readlink(entry.path)
                if not target or len(target) > 4096:
                    raise RecoveryContainmentUnavailable("recovery watcher unit alias target is unsupported")
                # systemd resolves aliases inside UnitPath by unit name even
                # when their literal target file is absent at that load level.
                aliases.append((entry.name, Path(target).name))
                try:
                    resolved = Path(entry.path).resolve(strict=False)
                except RuntimeError as error:
                    raise RecoveryContainmentUnavailable("recovery watcher unit alias inventory contains a loop") from error
                if resolved == source:
                    raise RecoveryContainmentUnavailable("recovery watcher retained fragment has an unsupported alias")
    names = {selected.unit}
    for _ in range(len(aliases) + 1):
        related = {name for name, target in aliases if target in names}
        if related <= names:
            break
        names.update(related)
    if names != {selected.unit}:
        raise RecoveryContainmentUnavailable("recovery watcher retained unit name has an unsupported alias")
    if len(masks) != 1 or len(fragments) != 1 or masks[0][0] >= fragments[0][0]:
        raise RecoveryContainmentUnavailable("recovery watcher requires one real mask above its retained fragment")
    return RecoveryWatcherUnitFact(selected.unit_source, sys.executable, paths, tuple(roots), masks[0][1], masks[0][2])


def observe_recovery_watcher_unit(selected: RecoveryWatcher, fact: SystemdRouteFact, *, expected_uid: int = 0) -> RecoveryWatcherUnitFact:
    """Repeat actual load-path/source reads; effective inhibition is separate."""
    if fact.unit != selected.unit or not fact.inhibited or fact.main_pid != 0 or fact.needs_daemon_reload:
        raise RecoveryContainmentUnavailable("recovery watcher effective unit is not technically inhibited")
    first = _scope(selected, expected_uid=expected_uid)
    content = read_bound_file(selected.unit_source, source=True, expected_uid=expected_uid)
    _literal_command(content, selected)
    if _scope(selected, expected_uid=expected_uid) != first or read_bound_file(selected.unit_source, source=True, expected_uid=expected_uid) != content:
        raise RecoveryContainmentUnavailable("recovery watcher retained unit source changed during observation")
    return first
