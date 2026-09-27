"""Kernel-enforced, invocation-local filesystem policy for Linux launchers."""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import signal
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
_PTRACE_TRACEME = 0
_PTRACE_PEEKDATA = 2
_PTRACE_SYSCALL = 24
_PTRACE_SETOPTIONS = 0x4200
_PTRACE_O_TRACESYSGOOD = 1
_PTRACE_O_TRACEFORK = 2
_PTRACE_O_TRACEVFORK = 4
_PTRACE_O_TRACECLONE = 8
_WAIT_ALL = 0x40000000

_WRITE_FILE = 1 << 1
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
    _monitor: Optional["PtraceDenialMonitor"] = field(default=None, init=False, repr=False)

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
            # Landlock allow-lists are monotonic and cannot express "all normal
            # runtime reads except these protected inputs". This producer therefore
            # handles mutation rights only; approved inputs remain readable and
            # cannot become writable aliases. A composed publication stage may
            # tighten visibility independently.
            for path in self.read_visibility:
                _canonical_existing(path, "visible runtime input")
            rules: list[tuple[int, int]] = []
            for root in dict.fromkeys(writable_roots):
                flags = os.O_PATH | os.O_CLOEXEC
                fd = os.open(root, flags)
                is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                if root in writable_roots and not is_directory:
                    os.close(fd)
                    raise FilesystemConfinementUnavailable("writable roots must be directories")
                rules.append((fd, _write_rights(abi)))
            self._rules = rules
            self._abi = abi
            self._monitor = PtraceDenialMonitor(writable_roots)
            return PolicyInstallation(
                True,
                f"Landlock ABI {abi} filesystem policy prepared",
                establishes_filesystem_enforcement=True,
                child_setup=self._restrict_child,
                denial_monitor=self._monitor,
            )
        except (FilesystemConfinementUnavailable, OSError) as exc:
            self.close()
            return PolicyInstallation(False, str(exc))

    def _restrict_child(self) -> None:
        if self._abi is None:
            raise FilesystemConfinementUnavailable("filesystem policy was not prepared")
        # The controller traces denied mutation syscalls. TRACEME guarantees an
        # exec-stop before provider code can run, so observation has no start race.
        _ptrace(_PTRACE_TRACEME, 0, 0, 0)
        handled = _write_rights(self._abi)
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


class _UserRegsStruct(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "r15",
            "r14",
            "r13",
            "r12",
            "rbp",
            "rbx",
            "r11",
            "r10",
            "r9",
            "r8",
            "rax",
            "rcx",
            "rdx",
            "rsi",
            "rdi",
            "orig_rax",
            "rip",
            "cs",
            "eflags",
            "rsp",
            "ss",
            "fs_base",
            "gs_base",
            "ds",
            "es",
            "fs",
            "gs",
        )
    ]


def _ptrace(request: int, pid: int, address: object, data: object) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    libc.ptrace.restype = ctypes.c_long
    address_value = address if not isinstance(address, int) else ctypes.c_void_p(address)
    data_value = data if not isinstance(data, int) else ctypes.c_void_p(data)
    result = int(libc.ptrace(request, pid, address_value, data_value))
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


@dataclass
class PtraceDenialMonitor:
    """Controller-side observation of Landlock-denied mutation syscalls."""

    writable_roots: tuple[Path, ...]
    _root_pid: Optional[int] = field(default=None, init=False)
    _root_returncode: Optional[int] = field(default=None, init=False)
    _entering: dict[int, bool] = field(default_factory=dict, init=False)
    _pending: dict[int, Optional[str]] = field(default_factory=dict, init=False)

    @property
    def root_returncode(self) -> Optional[int]:
        return self._root_returncode

    def attach(self, process: object) -> None:
        if platform.machine() not in {"x86_64", "amd64"}:
            raise RuntimeError("denial observation requires the supported x86_64 Linux runtime")
        pid = int(getattr(process, "pid"))
        waited, status = os.waitpid(pid, _WAIT_ALL)
        if waited != pid or not os.WIFSTOPPED(status) or os.WSTOPSIG(status) != signal.SIGTRAP:
            raise RuntimeError("provider did not enter its controller-owned exec stop")
        options = _PTRACE_O_TRACESYSGOOD | _PTRACE_O_TRACEFORK | _PTRACE_O_TRACEVFORK | _PTRACE_O_TRACECLONE
        _ptrace(_PTRACE_SETOPTIONS, pid, 0, options)
        self._root_pid = pid
        self._entering[pid] = True
        _ptrace(_PTRACE_SYSCALL, pid, 0, 0)

    def pump(self) -> tuple[str, ...]:
        denials: list[str] = []
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG | _WAIT_ALL)
            except ChildProcessError:
                break
            if pid == 0:
                break
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                if pid == self._root_pid:
                    self._root_returncode = os.waitstatus_to_exitcode(status)
                self._entering.pop(pid, None)
                self._pending.pop(pid, None)
                continue
            stop_signal = os.WSTOPSIG(status)
            if stop_signal == (signal.SIGTRAP | 0x80):
                entering = self._entering.get(pid, True)
                regs = _registers(pid)
                if entering:
                    operation = self._mutation_outside_roots(pid, regs)
                    self._pending[pid] = operation
                else:
                    operation = self._pending.pop(pid, None)
                    result = ctypes.c_longlong(regs.rax).value
                    if operation is not None and result in {-errno.EACCES, -errno.EPERM, -errno.EXDEV}:
                        denials.append(f"filesystem policy denied {operation} for invocation process {pid}")
                self._entering[pid] = not entering
            else:
                self._entering.setdefault(pid, True)
            try:
                delivered_signal = stop_signal if stop_signal not in {signal.SIGTRAP, signal.SIGSTOP} else 0
                _ptrace(_PTRACE_SYSCALL, pid, 0, delivered_signal)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    raise
        return tuple(denials)

    def _mutation_outside_roots(self, pid: int, regs: _UserRegsStruct) -> Optional[str]:
        syscall = regs.orig_rax
        # x86_64 pathname argument and open flags for mutation-capable calls.
        path_pointer: Optional[int] = None
        directory_fd = -100
        operation = "write"
        if syscall in {2, 85}:  # open, creat
            flags = regs.rsi if syscall == 2 else os.O_CREAT | os.O_WRONLY
            if not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                return None
            path_pointer = regs.rdi
            operation = "file open for mutation"
        elif syscall == 257:  # openat
            if not regs.rdx & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                return None
            directory_fd = ctypes.c_int(regs.rdi).value
            path_pointer = regs.rsi
            operation = "file open for mutation"
        elif syscall in {76, 83, 84, 87, 90, 92}:
            path_pointer = regs.rdi
            operation = "path mutation"
        elif syscall in {82, 86}:  # rename, link: either pathname may escape
            for pointer in (regs.rdi, regs.rsi):
                if self._path_is_outside(pid, -100, pointer):
                    return "cross-root path mutation"
            return None
        elif syscall == 88:  # symlink: only the created link path is mutated
            path_pointer = regs.rsi
        elif syscall in {258, 263, 268, 260}:
            directory_fd = ctypes.c_int(regs.rdi).value
            path_pointer = regs.rsi
            operation = "path mutation"
        elif syscall in {264, 265, 316}:  # renameat/linkat variants
            pairs = ((ctypes.c_int(regs.rdi).value, regs.rsi), (ctypes.c_int(regs.rdx).value, regs.r10))
            if any(self._path_is_outside(pid, directory, pointer) for directory, pointer in pairs):
                return "cross-root path mutation"
            return None
        elif syscall == 266:  # symlinkat(target, newdirfd, linkpath)
            directory_fd = ctypes.c_int(regs.rsi).value
            path_pointer = regs.rdx
            operation = "path mutation"
        if path_pointer is None:
            return None
        requested = _read_process_string(pid, path_pointer)
        if requested is None:
            return None
        target = _resolve_process_path(pid, directory_fd, requested)
        if target is None or any(_contains(root, target) for root in self.writable_roots):
            return None
        return operation

    def _path_is_outside(self, pid: int, directory_fd: int, pointer: int) -> bool:
        requested = _read_process_string(pid, pointer)
        if requested is None:
            return False
        target = _resolve_process_path(pid, directory_fd, requested)
        return target is not None and not any(_contains(root, target) for root in self.writable_roots)


def _registers(pid: int) -> _UserRegsStruct:
    registers = _UserRegsStruct()
    _ptrace(12, pid, 0, ctypes.byref(registers))  # PTRACE_GETREGS
    return registers


def _read_process_string(pid: int, address: int, limit: int = 4096) -> Optional[str]:
    data = bytearray()
    word_size = ctypes.sizeof(ctypes.c_long)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ptrace.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    libc.ptrace.restype = ctypes.c_long
    while len(data) < limit:
        ctypes.set_errno(0)
        word = int(libc.ptrace(_PTRACE_PEEKDATA, pid, ctypes.c_void_p(address + len(data)), None))
        error = ctypes.get_errno()
        if word == -1 and error:
            return None
        chunk = int(word & ((1 << (word_size * 8)) - 1)).to_bytes(word_size, byteorder="little")
        if b"\0" in chunk:
            data.extend(chunk.split(b"\0", 1)[0])
            break
        data.extend(chunk)
    try:
        return os.fsdecode(bytes(data))
    except UnicodeError:
        return None


def _resolve_process_path(pid: int, directory_fd: int, requested: str) -> Optional[Path]:
    try:
        if os.path.isabs(requested):
            base = Path(f"/proc/{pid}/root")
            combined = base / requested.lstrip("/")
        else:
            anchor = "cwd" if directory_fd == -100 else f"fd/{directory_fd}"
            base = Path(os.readlink(f"/proc/{pid}/{anchor}"))
            combined = base / requested
        parent = combined.parent.resolve(strict=True)
        return parent / combined.name
    except OSError:
        return None
