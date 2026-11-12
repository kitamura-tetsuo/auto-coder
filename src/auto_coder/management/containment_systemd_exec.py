"""Bind the measured systemd 255 direct-executable search without a shell.

The supported system manager uses its compiled search path, not the observer's
PATH. This deliberately excludes alternate execution roots, search overrides,
interpreters, wrappers and environment expansion. It is source readback, not a
new permission to execute an observed command.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .containment_enrollment import EnrollmentUnavailable, _trusted_ancestors
from .containment_route_sources import CommandSourceUnavailable, SourceNodeIdentity, _source_node_identity

_SYSTEMD_PATH = Path("/usr/bin/systemd-path")
_SUPPORTED_SEARCH_PATH = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin")
_MAX_EXECUTABLE_BYTES = 8 * 1024 * 1024
_MAX_EXECUTABLES = 128
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}")
_ARGUMENT = re.compile(r"[A-Za-z0-9_./:=+@,-]{1,4096}")
_DISPATCHER = re.compile(r"(?:sh|dash|bash|zsh|ksh|csh|tcsh|fish|busybox|toybox|nice|nohup|setsid|timeout|flock|stdbuf|ionice|chrt|taskset|env|python[0-9.]*|node|nodejs|perl|ruby)")
_EXEC_ROW = re.compile(
    r"\{ path=(?P<executable>[A-Za-z0-9_][A-Za-z0-9_.+-]{0,127}) ; argv\[\]=(?P<argv>[^\r\n]{1,32768}) ; ignore_errors=(?:yes|no) ; "
    r"start_time=\[[^\]\r\n]{1,128}\] ; stop_time=\[[^\]\r\n]{1,128}\] ; "
    r"pid=[0-9]{1,20} ; code=(?:\(null\)|[a-z-]{1,32}) ; status=[0-9]{1,10}(?:/(?:[0-9]{1,10}|[A-Z0-9]{1,32}))? \}"
)
_EMPTY_CONTEXT = (
    "WorkingDirectory",
    "RootDirectory",
    "RootImage",
    "Environment",
    "ExecSearchPath",
    "PassEnvironment",
    "UnsetEnvironment",
    "User",
    "Group",
    "PAMName",
    "BindPaths",
    "BindReadOnlyPaths",
    "MountImages",
    "ExtensionImages",
    "ExtensionDirectories",
    "TemporaryFileSystem",
    "InaccessiblePaths",
    "NoExecPaths",
    "ExecPaths",
    "ReadOnlyPaths",
    "ReadWritePaths",
)
_FALSE_CONTEXT = (
    "DynamicUser",
    "RootDirectoryStartOnly",
    "RootEphemeral",
    "PrivateUsers",
    "PrivateMounts",
    "PrivateTmp",
    "PrivateDevices",
    "ProtectHome",
    "ProtectSystem",
)
SYSTEMD_EXEC_CONTEXT_PROPERTIES = _EMPTY_CONTEXT + _FALSE_CONTEXT


@dataclass(frozen=True)
class ExecutableDirectoryFact:
    path: str
    identity: SourceNodeIdentity


@dataclass(frozen=True)
class ExecutableLookupFact:
    path: str
    resolved: str
    link_target: str
    entry: SourceNodeIdentity | None
    target: SourceNodeIdentity | None
    sha256: str
    ancestors: tuple[ExecutableDirectoryFact, ...]


@dataclass(frozen=True)
class SystemdSearchAuthority:
    manager_version: str
    utility_version: str
    search_path: tuple[str, ...]
    utility: ExecutableLookupFact


@dataclass(frozen=True)
class SystemdExecSearchFact:
    executable: str
    selected_path: str
    authority: SystemdSearchAuthority
    candidates: tuple[ExecutableLookupFact, ...]


def _directories(directory: Path, *, uid: int) -> tuple[ExecutableDirectoryFact, ...]:
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise CommandSourceUnavailable("systemd executable directory is not canonical")
    _trusted_ancestors(directory, uid=uid)
    return tuple(ExecutableDirectoryFact(str(item), _source_node_identity(item.lstat())) for item in (directory, *directory.parents))


def _executable_bytes(path: Path, *, uid: int) -> tuple[str, SourceNodeIdentity]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, uid) or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not before.st_mode & stat.S_IXUSR or not os.access(path, os.X_OK):
            raise CommandSourceUnavailable("systemd executable candidate is not protected and executable")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(_MAX_EXECUTABLE_BYTES + 1)
        after = os.fstat(descriptor)
        identity = _source_node_identity(after)
        if len(data) > _MAX_EXECUTABLE_BYTES or _source_node_identity(before) != identity or identity != _source_node_identity(path.lstat()):
            raise CommandSourceUnavailable("systemd executable changed during readback or exceeded its bound")
        return hashlib.sha256(data).hexdigest(), identity
    finally:
        os.close(descriptor)


def _lookup(path: Path, *, uid: int) -> ExecutableLookupFact:
    """Read one concrete candidate, including absence before any later match."""
    ancestors = _directories(path.parent, uid=uid)
    try:
        before = path.lstat()
    except FileNotFoundError:
        if _directories(path.parent, uid=uid) != ancestors:
            raise CommandSourceUnavailable("systemd absent executable prefix changed during readback")
        return ExecutableLookupFact(str(path), str(path), "", None, None, "", ancestors)
    entry = _source_node_identity(before)
    if before.st_uid not in (0, uid):
        raise CommandSourceUnavailable("systemd executable entry has an unapproved owner")
    link = ""
    resolved = path
    if stat.S_ISLNK(before.st_mode):
        link = os.readlink(path)
        target = Path(link)
        if ".." in target.parts or not link or len(link) > 4096:
            raise CommandSourceUnavailable("systemd executable symbolic link is unsupported")
        resolved = target if target.is_absolute() else path.parent / target
        if resolved.resolve(strict=True) != resolved or stat.S_ISLNK(resolved.lstat().st_mode):
            raise CommandSourceUnavailable("systemd executable symbolic-link chain is unsupported")
    elif not stat.S_ISREG(before.st_mode):
        raise CommandSourceUnavailable("systemd executable candidate is not a regular file")
    target_ancestors = _directories(resolved.parent, uid=uid)
    digest, target_identity = _executable_bytes(resolved, uid=uid)
    if _source_node_identity(path.lstat()) != entry or path.resolve(strict=True) != resolved or stat.S_ISLNK(before.st_mode) and os.readlink(path) != link or _directories(path.parent, uid=uid) != ancestors or _directories(resolved.parent, uid=uid) != target_ancestors:
        raise CommandSourceUnavailable("systemd executable lookup changed during readback")
    merged = {item.path: item for item in (*ancestors, *target_ancestors)}
    return ExecutableLookupFact(str(path), str(resolved), link, entry, target_identity, digest, tuple(merged[name] for name in sorted(merged)))


def _authority(read_command: Callable[..., str], *, uid: int) -> SystemdSearchAuthority:
    utility = _lookup(_SYSTEMD_PATH, uid=uid)
    if utility.target is None:
        raise CommandSourceUnavailable("native systemd search authority is absent")
    version = read_command(str(_SYSTEMD_PATH), "--version").splitlines()
    matched = re.fullmatch(r"systemd 255 \((255\.[0-9]+-1ubuntu[0-9.]+)\)", version[0] if version else "")
    manager = read_command("/usr/bin/systemctl", "show", "--property=Version", "--value").strip()
    search = read_command(str(_SYSTEMD_PATH), "search-binaries-default").strip()
    if matched is None or manager != matched.group(1) or search != ":".join(_SUPPORTED_SEARCH_PATH):
        raise CommandSourceUnavailable("native systemd executable search authority is outside the measured profile")
    if _lookup(_SYSTEMD_PATH, uid=uid) != utility:
        raise CommandSourceUnavailable("native systemd search utility changed during readback")
    return SystemdSearchAuthority(manager, version[0], _SUPPORTED_SEARCH_PATH, utility)


def _search(name: str, authority: SystemdSearchAuthority, *, uid: int) -> SystemdExecSearchFact:
    candidates = []
    for directory in authority.search_path:
        fact = _lookup(Path(directory) / name, uid=uid)
        candidates.append(fact)
        if fact.target is not None:
            if _DISPATCHER.fullmatch(Path(fact.resolved).name):
                raise CommandSourceUnavailable("bare systemd dispatcher target is unsupported")
            return SystemdExecSearchFact(name, fact.path, authority, tuple(candidates))
    raise CommandSourceUnavailable("bare systemd executable is absent from its native search path")


def resolve_direct_systemd_commands(
    rows: tuple[str, ...],
    *,
    unit: str,
    shown: Mapping[str, str],
    environment_files: tuple[str, ...],
    user: str | None,
    uid: int,
    read_command: Callable[..., str],
) -> tuple[tuple[str, ...], tuple[SystemdExecSearchFact, ...]]:
    """Map direct command paths for source inspection, retaining original argv."""
    bare = []
    for index, row in enumerate(rows):
        prefix = re.match(r"\{ path=([^;]+) ;", row)
        if prefix is not None and not prefix.group(1).startswith("/"):
            matched = _EXEC_ROW.fullmatch(row)
            if matched is None:
                raise CommandSourceUnavailable("bare systemd command structure is unsupported")
            name, argv = matched.group("executable", "argv")
            words = argv.split(" ")
            if not _NAME.fullmatch(name) or _DISPATCHER.fullmatch(name) or not words or words[0] != name or any(not _ARGUMENT.fullmatch(word) or ".." in Path(word).parts for word in words):
                raise CommandSourceUnavailable("bare systemd dispatcher, argv override or expansion is unsupported")
            bare.append((index, matched))
    if not bare:
        return rows, ()
    if len(bare) > _MAX_EXECUTABLES:
        raise CommandSourceUnavailable("bare systemd executable inventory exceeds bound")
    if user is not None or not unit.endswith(".service") or "@." in unit or shown.get("LoadState") != "loaded" or shown.get("NeedDaemonReload") != "no":
        raise CommandSourceUnavailable("bare systemd command lacks the supported system-manager context")
    if any(shown.get(name) != "" for name in _EMPTY_CONTEXT) or any(shown.get(name) != "no" for name in _FALSE_CONTEXT) or any(environment_files):
        raise CommandSourceUnavailable("bare systemd command has unsupported or missing execution context")
    try:
        authority = _authority(read_command, uid=uid)
        searches: dict[str, SystemdExecSearchFact] = {}
        mapped = list(rows)
        for index, match in bare:
            name = match.group("executable")
            if name not in searches:
                searches[name] = _search(name, authority, uid=uid)
            start, end = match.span("executable")
            # Only the source-inspection path changes. argv[0] stays exactly
            # what systemd passes, and the original rows remain fingerprinted.
            mapped[index] = rows[index][:start] + searches[name].selected_path + rows[index][end:]
        return tuple(mapped), tuple(searches.values())
    except (OSError, EnrollmentUnavailable) as exc:
        raise CommandSourceUnavailable("native systemd executable search cannot be independently read") from exc


def reobserve_systemd_exec_searches(searches: tuple[SystemdExecSearchFact, ...], *, uid: int, read_command: Callable[..., str]) -> None:
    """Check the original mapping again after all selected sources were read."""
    if not searches:
        return
    try:
        authority = _authority(read_command, uid=uid)
        current = tuple(_search(item.executable, authority, uid=uid) for item in searches)
    except (OSError, EnrollmentUnavailable) as exc:
        raise CommandSourceUnavailable("native systemd executable search cannot be reobserved") from exc
    if current != searches:
        raise CommandSourceUnavailable("native systemd executable search or source changed during observation")
