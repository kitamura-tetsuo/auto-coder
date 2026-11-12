"""Read-only inventory of installed and live local systemd user controllers.

The returned inventory is present-time evidence, not an approval or an exclusion
policy. A separately protected route manifest must bind and classify its entire
contents. Sources owned by their account may change later; every observer and
consumer must read them again and refuse a changed inventory.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_MAX_ACCOUNTS = 1024
_MAX_SOURCES = 8192
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 32 * _MAX_FILE_BYTES
_USER_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\$?")
GLOBAL_USER_ROOTS = (
    Path("/etc/systemd/user"),
    Path("/run/systemd/user"),
    Path("/usr/local/lib/systemd/user"),
    Path("/usr/local/share/systemd/user"),
    Path("/usr/lib/systemd/user"),
    Path("/usr/share/systemd/user"),
)


class UserControllerUnavailable(ValueError):
    """The complete current user-controller scope could not be inspected."""


@dataclass(frozen=True)
class UserSourceFact:
    path: str
    kind: str
    device: int
    inode: int
    ctime_ns: int
    mode: int
    uid: int
    sha256: str
    target: str = ""
    target_device: int = 0
    target_inode: int = 0
    target_ctime_ns: int = 0
    management_candidate: bool = False
    size: int = 0


@dataclass(frozen=True)
class UserManagerLifetime:
    uid: int
    pid: int
    start_token: str


@dataclass(frozen=True)
class UserAccountObservation:
    uid: int
    gid: int
    name: str
    home: str
    shell: str
    lingering: bool
    runtime_present: bool
    manager_pid: int
    manager_start_token: str
    unit_paths: tuple[str, ...]
    sources: tuple[UserSourceFact, ...]


@dataclass(frozen=True)
class UserControllerInventory:
    accounts: tuple[UserAccountObservation, ...]
    global_sources: tuple[UserSourceFact, ...]
    linger_sources: tuple[UserSourceFact, ...]
    runtime_uids: tuple[int, ...]
    managers: tuple[UserManagerLifetime, ...]


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size, info.st_mode, info.st_uid


def _protected_source(path: Path, *, owners: frozenset[int]) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid not in owners or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise UserControllerUnavailable("user controller source is unsupported or writable by another principal")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            data = stream.read(_MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(data) > _MAX_FILE_BYTES or _identity(before) != _identity(after) or _identity(after) != _identity(path.lstat()):
        raise UserControllerUnavailable("user controller source changed during read or exceeded its bound")
    return data, after


def observe_user_sources(roots: tuple[Path, ...], *, uid: int, cues: tuple[str, ...]) -> tuple[UserSourceFact, ...]:
    """Snapshot whole installed unit trees, including missing roots and masks."""
    pending = list(roots)
    result: dict[str, UserSourceFact] = {}
    total_bytes = 0
    owners = frozenset((0, uid))
    while pending:
        path = pending.pop()
        name = str(path)
        if not path.is_absolute() or ".." in path.parts or len(name) > 4096:
            raise UserControllerUnavailable("user controller source path is unsupported")
        if name in result:
            continue
        if len(result) >= _MAX_SOURCES:
            raise UserControllerUnavailable("user controller source inventory exceeds its bound")
        try:
            info = path.lstat()
        except FileNotFoundError:
            result[name] = UserSourceFact(name, "missing", 0, 0, 0, 0, 0, "")
            continue
        if info.st_uid not in owners or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not stat.S_ISLNK(info.st_mode):
            raise UserControllerUnavailable("user controller source ownership or permissions are unsupported")
        base = (name, "", info.st_dev, info.st_ino, info.st_ctime_ns, stat.S_IMODE(info.st_mode), info.st_uid, "")
        if stat.S_ISDIR(info.st_mode):
            children = tuple(path.iterdir())
            if _identity(path.lstat()) != _identity(info):
                raise UserControllerUnavailable("user controller directory changed during enumeration")
            result[name] = UserSourceFact(base[0], "directory", *base[2:])
            pending.extend(children)
            continue
        target, target_device, target_inode, target_ctime = "", 0, 0, 0
        if stat.S_ISLNK(info.st_mode):
            link = os.readlink(path)
            if len(link) > 4096:
                raise UserControllerUnavailable("user controller symbolic link exceeds its bound")
            resolved = path.resolve(strict=True)
            if resolved == Path("/dev/null"):
                data = b""
                kind = "mask"
                target = str(resolved)
            else:
                # Linked regular unit files are supported; linked directories
                # hide another load root and require explicit profile support.
                data, target_info = _protected_source(resolved, owners=owners)
                kind = "symlink"
                target = str(resolved)
                target_device, target_inode, target_ctime = target_info.st_dev, target_info.st_ino, target_info.st_ctime_ns
            if os.readlink(path) != link or _identity(path.lstat()) != _identity(info):
                raise UserControllerUnavailable("user controller symbolic link changed during read")
        elif stat.S_ISREG(info.st_mode):
            data, info = _protected_source(path, owners=owners)
            kind = "regular"
        else:
            raise UserControllerUnavailable("unsupported object in user controller unit source tree")
        total_bytes += len(data)
        if total_bytes > _MAX_TOTAL_BYTES:
            raise UserControllerUnavailable("user controller source bytes exceed their bound")
        result[name] = UserSourceFact(
            name,
            kind,
            info.st_dev,
            info.st_ino,
            info.st_ctime_ns,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            hashlib.sha256(data).hexdigest(),
            target,
            target_device,
            target_inode,
            target_ctime,
            any(cue.encode() in data for cue in cues),
            len(data),
        )
    return tuple(result[name] for name in sorted(result))


def _process_identity(path: Path) -> tuple[str, str]:
    text = (path / "stat").read_text(encoding="ascii")
    prefix, separator, tail = text.rpartition(")")
    fields = tail.split()
    if not separator or prefix.partition(" (")[0] != path.name or len(fields) < 20 or not fields[19].isdecimal():
        raise UserControllerUnavailable("user manager process lifetime is ambiguous")
    return fields[0], fields[19]


def observe_user_manager_lifetimes(*, proc: Path = Path("/proc")) -> tuple[UserManagerLifetime, ...]:
    """Discover actual systemd --user processes independently of unit names."""
    result: list[UserManagerLifetime] = []
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            before = _process_identity(path)
            command = (path / "cmdline").read_bytes()
            if len(command) > 65536:
                raise UserControllerUnavailable("process command exceeds user-manager discovery bound")
            arguments = command.rstrip(b"\x00").split(b"\x00")
            if b"--user" not in arguments:
                continue
            executable = os.readlink(path / "exe")
            if Path(executable.removesuffix(" (deleted)")).name != "systemd":
                continue
            if executable.endswith(" (deleted)") or before[0] in ("Z", "X", "x"):
                raise UserControllerUnavailable("user manager source or lifetime is unavailable")
            identity_lines = [line.split() for line in (path / "status").read_text().splitlines() if line.startswith("Uid:")]
            if len(identity_lines) != 1 or len(identity_lines[0]) != 5 or len(set(identity_lines[0][1:])) != 1 or not identity_lines[0][1].isdecimal():
                raise UserControllerUnavailable("user manager account identity is ambiguous")
            if _process_identity(path) != before or (path / "cmdline").read_bytes() != command:
                raise UserControllerUnavailable("user manager lifetime changed during discovery")
            result.append(UserManagerLifetime(int(identity_lines[0][1]), int(path.name), before[1]))
        except FileNotFoundError:
            if path.exists():
                raise UserControllerUnavailable("live process is inaccessible during user-manager discovery")
    if len(result) > _MAX_ACCOUNTS or len({item.uid for item in result}) != len(result):
        raise UserControllerUnavailable("multiple or oversized user manager scopes are unsupported")
    return tuple(sorted(result, key=lambda item: item.uid))


def _default_private_unit_paths(uid: int, home: str, *, runtime_root: Path = Path("/run/user")) -> tuple[Path, ...]:
    root = Path(home)
    runtime = runtime_root / str(uid)
    return (
        root / ".config/systemd/user.control",
        root / ".config/systemd/user",
        root / ".local/share/systemd/user",
        runtime / "systemd/user.control",
        runtime / "systemd/transient",
        runtime / "systemd/generator.early",
        runtime / "systemd/user",
        runtime / "systemd/generator",
        runtime / "systemd/generator.late",
    )


def observe_user_controller_inventory(
    manager_pids: dict[int, int],
    unit_paths: Callable[[str], tuple[str, ...]],
    *,
    cues: tuple[str, ...],
    global_roots: tuple[Path, ...] = GLOBAL_USER_ROOTS,
    runtime_root: Path = Path("/run/user"),
    linger_root: Path = Path("/var/lib/systemd/linger"),
    trusted_uid: int = 0,
) -> UserControllerInventory:
    """Read all accounts, installed roots, live manager identities and load paths."""
    accounts = tuple(sorted(pwd.getpwall(), key=lambda item: (item.pw_uid, item.pw_name)))
    if not accounts or len(accounts) > _MAX_ACCOUNTS or len({item.pw_uid for item in accounts}) != len(accounts) or len({item.pw_name for item in accounts}) != len(accounts):
        raise UserControllerUnavailable("local account catalog is incomplete or ambiguous")
    if any(not _USER_NAME.fullmatch(item.pw_name) or not Path(item.pw_dir).is_absolute() or ".." in Path(item.pw_dir).parts or not 0 <= item.pw_uid <= 2147483647 for item in accounts):
        raise UserControllerUnavailable("local user controller account is unsupported")
    account_ids = {item.pw_uid for item in accounts}
    if not set(manager_pids) <= account_ids:
        raise UserControllerUnavailable("unknown installed user manager account scope")
    try:
        runtime_entries = tuple(runtime_root.iterdir())
    except FileNotFoundError:
        runtime_entries = ()
    if any(not item.name.isdecimal() or str(int(item.name)) != item.name or int(item.name) not in account_ids for item in runtime_entries):
        raise UserControllerUnavailable("unknown user runtime controller scope")
    runtime_ids = tuple(sorted(int(item.name) for item in runtime_entries))
    for entry in runtime_entries:
        info = entry.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, int(entry.name)) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise UserControllerUnavailable("user runtime controller root is not protected")
    linger = observe_user_sources((linger_root,), uid=trusted_uid, cues=())
    lingering_names = {Path(item.path).name for item in linger if item.kind != "missing" and Path(item.path) != linger_root}
    if not lingering_names <= {item.pw_name for item in accounts}:
        raise UserControllerUnavailable("unknown lingering user controller scope")
    live = observe_user_manager_lifetimes()
    by_uid = {item.uid: item for item in live}
    if any(item.uid not in account_ids or manager_pids.get(item.uid, 0) != item.pid for item in live) or any(pid and (uid not in by_uid or by_uid[uid].pid != pid) for uid, pid in manager_pids.items()):
        raise UserControllerUnavailable("unlisted or contradictory live user manager identity")
    global_sources = observe_user_sources(global_roots, uid=trusted_uid, cues=cues)
    globals_as_strings = {str(path) for path in global_roots}
    observed = []
    source_count = len(global_sources) + len(linger)
    source_bytes = sum(item.size for item in global_sources + linger)
    for account in accounts:
        manager = by_uid.get(account.pw_uid)
        actual_paths = unit_paths(account.pw_name) if manager is not None else tuple(str(path) for path in _default_private_unit_paths(account.pw_uid, account.pw_dir, runtime_root=runtime_root)) + tuple(str(path) for path in global_roots)
        if not actual_paths or len(actual_paths) > 128 or len(set(actual_paths)) != len(actual_paths) or any(not Path(path).is_absolute() or ".." in Path(path).parts or len(path) > 4096 for path in actual_paths):
            raise UserControllerUnavailable("user manager effective unit load roots are unsupported")
        private = tuple(Path(path) for path in actual_paths if path not in globals_as_strings)
        sources = observe_user_sources(private, uid=account.pw_uid, cues=cues)
        source_count += len(sources)
        source_bytes += sum(item.size for item in sources)
        if source_count > _MAX_SOURCES or source_bytes > _MAX_TOTAL_BYTES:
            raise UserControllerUnavailable("complete user controller source inventory exceeds its aggregate bound")
        if manager is None and any(item.management_candidate for item in sources + global_sources):
            raise UserControllerUnavailable("inactive user has a configured management route without independently observed inhibition")
        observed.append(
            UserAccountObservation(
                account.pw_uid,
                account.pw_gid,
                account.pw_name,
                account.pw_dir,
                account.pw_shell,
                account.pw_name in lingering_names,
                account.pw_uid in runtime_ids,
                manager.pid if manager is not None else 0,
                manager.start_token if manager is not None else "",
                actual_paths,
                sources,
            )
        )
    return UserControllerInventory(tuple(observed), global_sources, linger, runtime_ids, live)
