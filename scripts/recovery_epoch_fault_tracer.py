"""Real Linux syscall barriers for the disposable recovery acceptance fixture.

This observer never changes syscall arguments/results or supplies production
observations. It stops the actual issuer at an exclusive publication open or
at the successful directory fsync following its real file fsync. The caller
then kills the pinned process or resumes it after an external fixture mutation.
Linux ptrace ABI: https://man7.org/linux/man-pages/man2/ptrace.2.html
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from auto_coder.logger_config import get_logger

logger = get_logger(__name__)
PHASES = ("reserved", "intent", "consumed", "receipt")
PUBLICATIONS = tuple(phase + suffix for phase in PHASES for suffix in (".json", "-head.json"))
BOUNDARY_SCENARIOS = tuple(f"{when}-{name.removesuffix('.json')}" for name in PUBLICATIONS for when in ("before", "after"))

_PTRACE_SEIZE = 0x4206
_PTRACE_INTERRUPT = 0x4207
_PTRACE_GET_SYSCALL_INFO = 0x420E
_PTRACE_SYSCALL = 24
_PTRACE_DETACH = 17
_PTRACE_O_TRACESYSGOOD = 1
_PTRACE_O_EXITKILL = 1 << 20
_WAIT_ALL = 0x40000000
_AUDIT_ARCH_X86_64 = 0xC000003E
_SYS_OPENAT = 257
_SYS_FSYNC = 74
_BARRIER_ACTION_TIMEOUT = 1800


class _Entry(ctypes.Structure):
    _fields_ = [("number", ctypes.c_uint64), ("arguments", ctypes.c_uint64 * 6)]


class _Exit(ctypes.Structure):
    _fields_ = [("result", ctypes.c_int64), ("is_error", ctypes.c_uint8)]


class _Call(ctypes.Union):
    _fields_ = [("entry", _Entry), ("exit", _Exit), ("space", ctypes.c_uint8 * 64)]


class _Information(ctypes.Structure):
    _fields_ = [("operation", ctypes.c_uint8), ("padding", ctypes.c_uint8 * 3), ("architecture", ctypes.c_uint32), ("instruction", ctypes.c_uint64), ("stack", ctypes.c_uint64), ("call", _Call)]


def parse_boundary(scenario: str) -> tuple[str, str]:
    if scenario not in BOUNDARY_SCENARIOS:
        raise ValueError("a reviewed publication boundary scenario is required")
    when, name = scenario.split("-", 1)
    return name + ".json", when


def _token(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


@dataclass(frozen=True)
class PublicationObservation:
    process_id: int
    start_token: str
    boot_id: str
    name: str
    when: str
    directory_device: int
    directory_inode: int
    file_device: int | None
    file_inode: int | None
    file_sha256: str | None
    file_fsync_observed: bool
    directory_fsync_observed: bool


class TraceProcess(Protocol):
    """Actual process identity with a caller-owned termination observer."""

    pid: int
    returncode: int | None

    def wait(self, timeout: float | None = None) -> int: ...


class KernelPublicationBarrier:
    """Attach to one actual child and retain its exact syscall stop.

    Do not call Popen.poll/wait between start and completion: this dedicated
    tracer thread exclusively consumes waitpid notifications during that time.
    start returns with the issuer stopped. resume_initial releases that initial
    stop. wait returns with the selected native publication still stopped.
    kill or release finishes tracing; close kills an unfinished tracee.
    """

    def __init__(self, issuer: TraceProcess, *, root: Path, name: str, when: str):
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("the native publication fixture requires Linux x86_64")
        if name not in PUBLICATIONS or when not in {"before", "after"}:
            raise ValueError("unsupported publication boundary")
        if not root.is_absolute() or root.resolve(strict=True) != root or not root.is_dir():
            raise ValueError("the publication directory must be original and canonical")
        self.issuer = issuer
        self.root = root
        self.name = name
        self.when = when
        self.root_identity = (root.stat().st_dev, root.stat().st_ino)
        self.start_token = _token(issuer.pid)
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self._pidfd = os.pidfd_open(issuer.pid)
        self._ready = threading.Event()
        self._resume = threading.Event()
        self._hit = threading.Event()
        self._action = threading.Event()
        self._done = threading.Event()
        self._decision = "kill"
        self._error: BaseException | None = None
        self._observation: object | None = None
        self._thread: threading.Thread | None = None
        self._file_identity: tuple[int, int] | None = None
        self._file_synced = False
        self._pending: str | None = None
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.ptrace.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self._libc.ptrace.restype = ctypes.c_long

    def _ptrace(self, request: int, address: int = 0, data: int | ctypes.c_void_p = 0) -> int:
        result = self._libc.ptrace(request, self.issuer.pid, ctypes.c_void_p(address), ctypes.c_void_p(data) if isinstance(data, int) else data)
        if result == -1:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    def _require_lifetime(self) -> None:
        if _token(self.issuer.pid) != self.start_token or Path("/proc/sys/kernel/random/boot_id").read_text().strip() != self.boot_id:
            raise AssertionError("the traced issuer lifetime changed")
        if (self.root.stat().st_dev, self.root.stat().st_ino) != self.root_identity:
            raise AssertionError("the traced publication directory changed")

    def _descriptor_identity(self, descriptor: int) -> tuple[int, int]:
        info = Path(f"/proc/{self.issuer.pid}/fd/{descriptor}").stat()
        return info.st_dev, info.st_ino

    def _cstring(self, address: int) -> str:
        with Path(f"/proc/{self.issuer.pid}/mem").open("rb", buffering=0) as memory:
            value = bytearray()
            for offset in range(4096):
                byte = os.pread(memory.fileno(), 1, address + offset)
                if byte == b"\0":
                    return value.decode("utf-8", "strict")
                if len(byte) != 1:
                    break
                value.extend(byte)
        raise AssertionError("the native publication pathname was not bounded")

    def _observe_call(self) -> bool:
        info = _Information()
        length = self._ptrace(_PTRACE_GET_SYSCALL_INFO, ctypes.sizeof(info), ctypes.cast(ctypes.byref(info), ctypes.c_void_p))
        if length < 24 or info.architecture != _AUDIT_ARCH_X86_64:
            raise AssertionError("the native syscall information is unavailable")
        if info.operation == 1:
            self._pending = None
            call = info.call.entry
            if call.number == _SYS_OPENAT and call.arguments[2] & (os.O_CREAT | os.O_EXCL) == os.O_CREAT | os.O_EXCL:
                directory = ctypes.c_int(call.arguments[0]).value
                if directory >= 0 and self._descriptor_identity(directory) == self.root_identity and self._cstring(call.arguments[1]) == self.name:
                    if self._file_identity is not None:
                        raise AssertionError("the same native publication was attempted twice")
                    if (self.root / self.name).exists():
                        raise AssertionError("the selected publication already existed before exclusive creation")
                    self._pending = "open"
                    return self.when == "before"
            elif call.number == _SYS_FSYNC and self._file_identity is not None:
                identity = self._descriptor_identity(call.arguments[0])
                if identity == self._file_identity:
                    self._pending = "file-fsync"
                elif identity == self.root_identity and self._file_synced:
                    self._pending = "directory-fsync"
        elif info.operation == 2 and self._pending is not None:
            pending, self._pending = self._pending, None
            if info.call.exit.is_error or info.call.exit.result < 0:
                raise AssertionError("the selected real publication syscall failed")
            if pending == "open":
                self._file_identity = self._descriptor_identity(info.call.exit.result)
                actual = (self.root / self.name).stat()
                if self._file_identity != (actual.st_dev, actual.st_ino):
                    raise AssertionError("the created publication differs from its actual descriptor")
            elif pending == "file-fsync":
                self._file_synced = True
            elif pending == "directory-fsync":
                return self.when == "after"
        return False

    def _record(self) -> None:
        self._require_lifetime()
        path = self.root / self.name
        device = inode = digest = None
        if self.when == "after":
            actual = path.stat()
            device, inode = actual.st_dev, actual.st_ino
            if self._file_identity != (device, inode) or not self._file_synced:
                raise AssertionError("actual publication/file fsync identity is missing")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.exists():
            raise AssertionError("the before-publication barrier was crossed")
        self._observation = PublicationObservation(self.issuer.pid, self.start_token, self.boot_id, self.name, self.when, *self.root_identity, device, inode, digest, self._file_synced, self.when == "after")
        logger.bind(native_publication=self.name, native_boundary=self.when, issuer_pid=self.issuer.pid).info("Actual issuer stopped at native publication boundary")
        self._hit.set()

    def _wait_status(self) -> int:
        pid, status = os.waitpid(self.issuer.pid, _WAIT_ALL)
        if pid != self.issuer.pid:
            raise AssertionError("a different process supplied the native trace status")
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            self.issuer.returncode = os.waitstatus_to_exitcode(status)
        return status

    def _run(self) -> None:
        attached = False
        try:
            self._require_lifetime()
            self._ptrace(_PTRACE_SEIZE, data=_PTRACE_O_TRACESYSGOOD | _PTRACE_O_EXITKILL)
            attached = True
            self._ptrace(_PTRACE_INTERRUPT)
            status = self._wait_status()
            if not os.WIFSTOPPED(status):
                raise AssertionError("actual issuer exited before the initial trace barrier")
            self._ready.set()
            if not self._resume.wait(_BARRIER_ACTION_TIMEOUT):
                raise TimeoutError("the actual target probe did not release the initial barrier")
            self._ptrace(_PTRACE_SYSCALL)
            while True:
                status = self._wait_status()
                if not os.WIFSTOPPED(status):
                    raise AssertionError("actual issuer exited before the selected publication boundary")
                delivered = os.WSTOPSIG(status)
                if delivered == signal.SIGTRAP | 0x80:
                    if self._observe_call():
                        self._record()
                        if not self._action.wait(_BARRIER_ACTION_TIMEOUT):
                            raise TimeoutError("the observed publication was not explicitly released or killed")
                        if self._decision == "release":
                            self._ptrace(_PTRACE_DETACH)
                            attached = False
                            return
                        signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                        while self.issuer.returncode is None:
                            self._wait_status()
                        return
                    delivered = 0
                elif delivered == signal.SIGTRAP and status >> 16:
                    delivered = 0
                self._ptrace(_PTRACE_SYSCALL, data=delivered)
        except BaseException as error:
            self._error = error
        finally:
            if attached and self.issuer.returncode is None:
                try:
                    signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
                    while self.issuer.returncode is None:
                        self._wait_status()
                except (OSError, ChildProcessError):
                    pass
            self._ready.set()
            self._hit.set()
            self._done.set()

    def _raise_error(self) -> None:
        if self._error is not None:
            raise self._error

    def start(self) -> None:
        if self._thread is not None:
            raise ValueError("a native publication tracer cannot be restarted")
        self._thread = threading.Thread(target=self._run, name="recovery-native-publication", daemon=True)
        self._thread.start()
        if not self._ready.wait(15):
            self.close()
            raise TimeoutError("the native initial trace barrier was not observed")
        self._raise_error()

    def resume_initial(self) -> None:
        if not self._ready.is_set() or self._done.is_set():
            self._raise_error()
            raise ValueError("no active initial publication barrier")
        self._resume.set()

    def wait_observation(self, timeout: float = 900) -> object:
        if not self._hit.wait(timeout):
            self.close()
            raise TimeoutError("the actual issuer never reached the selected publication")
        self._raise_error()
        if self._observation is None:
            raise AssertionError("the native publication observation is missing")
        return self._observation

    def wait(self, timeout: float = 900) -> PublicationObservation:
        observation = self.wait_observation(timeout)
        if not isinstance(observation, PublicationObservation):
            raise AssertionError("the native stop is not a publication observation")
        return observation

    def _finish(self, action: str) -> None:
        self._raise_error()
        if self._observation is None or self._done.is_set():
            raise ValueError("the selected native publication is not currently stopped")
        self._decision = action
        self._action.set()
        if not self._done.wait(15):
            self.close()
            raise TimeoutError("the native publication trace did not finish")
        self._raise_error()

    def kill(self) -> None:
        self._finish("kill")

    def release(self) -> None:
        self._finish("release")

    def close(self) -> None:
        if self._pidfd < 0:
            return
        if self._thread is not None and not self._done.is_set():
            self._decision = "kill"
            self._resume.set()
            self._action.set()
            try:
                signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not self._done.wait(15):
                raise TimeoutError("the actual traced issuer did not exit during cleanup")
        if self._thread is not None and self._done.is_set() and self._error is not None and self.issuer.returncode is None:
            # A refused initial SEIZE never transfers waitpid ownership to the
            # tracer. End the caller's exact attempted issuer here as well.
            try:
                signal.pidfd_send_signal(self._pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.issuer.wait(timeout=15)
        os.close(self._pidfd)
        self._pidfd = -1
