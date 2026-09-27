"""Kernel-enforced, invocation-local filesystem policy for Linux launchers."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .invocation_process_supervisor import InstallationContext, PolicyInstallation


class FilesystemConfinementUnavailable(RuntimeError):
    """Raised before provider submission when confinement cannot be installed."""


# Landlock's syscall numbers are allocated in the architecture-independent range
# on every Linux architecture supported by the production image.
_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446
_RULE_PATH_BENEATH = 1
_CREATE_VERSION = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14
_IOCTL_DEV = 1 << 15

_WRITE_RIGHTS_V1 = _WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKE_CHAR | _MAKE_DIR | _MAKE_REG | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM
_READ_RIGHTS = _EXECUTE | _READ_FILE | _READ_DIR


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _syscall(number: int, *arguments: object) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = int(libc.syscall(number, *arguments))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _landlock_abi() -> int:
    try:
        return _syscall(_CREATE_RULESET, 0, 0, _CREATE_VERSION)
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EINVAL}:
            raise FilesystemConfinementUnavailable("the Linux runtime does not provide usable Landlock confinement") from exc
        raise FilesystemConfinementUnavailable(f"cannot query Landlock capability: {exc}") from exc


def _write_rights(abi: int) -> int:
    rights = _WRITE_RIGHTS_V1
    if abi >= 2:
        rights |= _REFER
    if abi >= 3:
        rights |= _TRUNCATE
    if abi >= 5:
        rights |= _IOCTL_DEV
    return rights


def _canonical_existing(path: Path, description: str) -> Path:
    if not path.is_absolute():
        raise FilesystemConfinementUnavailable(f"{description} must be an absolute controller-selected path")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise FilesystemConfinementUnavailable(f"{description} is unavailable: {exc}") from exc


def _aliases(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or parent in child.parents


@dataclass
class LandlockFilesystemPolicy:
    """Prepare a fail-closed Landlock policy and install it after fork.

    Paths are resolved and checked by the controller during ``install``. Open path
    descriptors are retained until the child applies the rules, eliminating a
    resolve-then-reopen race and making symlink replacement harmless.
    """

    read_visibility: tuple[Path, ...] = ()
    _rules: list[tuple[int, int]] = field(default_factory=list, init=False, repr=False)
    _abi: Optional[int] = field(default=None, init=False, repr=False)

    def install(self, context: InstallationContext) -> PolicyInstallation:
        if platform.system() != "Linux":
            return PolicyInstallation(False, "filesystem confinement requires Linux")
        try:
            editable = context.effective_mode == "editable"
            if context.effective_mode not in {"editable", "no-edit"}:
                raise FilesystemConfinementUnavailable("effective mode is not a supported controller mode")
            # A provider and its delegated commands share one kernel domain. Giving
            # a no-edit provider a writable bookkeeping directory would therefore
            # also give model-requested shells that capability. Trusted no-edit
            # bookkeeping stays controller-side rather than becoming a child root.
            writable = (context.result_root, *context.runtime_paths) if editable else ()
            writable_roots = tuple(_canonical_existing(path, "writable root") for path in writable)
            protected = tuple(_canonical_existing(path, "protected path") for path in context.protected_paths)
            runtime_inputs = tuple(_canonical_existing(path, "runtime input") for path in context.runtime_inputs)
            ownership = _canonical_existing(context.ownership_path, "invocation ownership path")
            for root in writable_roots:
                if _contains(root, ownership) or _contains(ownership, root):
                    raise FilesystemConfinementUnavailable("writable data aliases controller ownership state")
                for denied in protected:
                    if _contains(root, denied) or _contains(denied, root) or _aliases(root, denied):
                        raise FilesystemConfinementUnavailable("writable private data aliases protected data")
                for runtime_input in runtime_inputs:
                    if _contains(root, runtime_input) or _contains(runtime_input, root) or _aliases(root, runtime_input):
                        raise FilesystemConfinementUnavailable("writable private data aliases a read-only runtime input")
            abi = _landlock_abi()
            visible_roots = tuple(_canonical_existing(path, "visible runtime input") for path in self.read_visibility) + runtime_inputs
            handled = _write_rights(abi) | (_READ_RIGHTS if visible_roots else 0)
            rules: list[tuple[int, int]] = []
            for root in dict.fromkeys((*writable_roots, *visible_roots)):
                flags = os.O_PATH | os.O_CLOEXEC
                fd = os.open(root, flags)
                is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                if root in writable_roots and not is_directory:
                    os.close(fd)
                    raise FilesystemConfinementUnavailable("writable roots must be directories")
                allowed = _READ_RIGHTS if root in visible_roots else 0
                if root in writable_roots:
                    allowed |= _write_rights(abi)
                    if visible_roots:
                        allowed |= _READ_RIGHTS
                rules.append((fd, allowed & handled))
            self._rules = rules
            self._abi = abi
            return PolicyInstallation(
                True,
                f"Landlock ABI {abi} filesystem policy prepared",
                establishes_filesystem_enforcement=True,
                child_setup=self._restrict_child,
            )
        except (FilesystemConfinementUnavailable, OSError) as exc:
            self.close()
            return PolicyInstallation(False, str(exc))

    def _restrict_child(self) -> None:
        if self._abi is None:
            raise FilesystemConfinementUnavailable("filesystem policy was not prepared")
        handled = _write_rights(self._abi) | (_READ_RIGHTS if self.read_visibility else 0)
        # runtime_inputs also cause read rules; infer that from prepared rights.
        if any(rights & _READ_RIGHTS for _, rights in self._rules):
            handled |= _READ_RIGHTS
        ruleset = _RulesetAttr(handled)
        ruleset_fd = _syscall(_CREATE_RULESET, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
        try:
            for path_fd, allowed in self._rules:
                rule = _PathBeneathAttr(allowed, path_fd)
                _syscall(_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, ctypes.byref(rule), 0)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            _syscall(_RESTRICT_SELF, ruleset_fd, 0)
        finally:
            os.close(ruleset_fd)
            self.close()

    def close(self) -> None:
        while self._rules:
            try:
                os.close(self._rules.pop()[0])
            except OSError:
                pass
