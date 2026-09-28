"""Linux cgroup-v2 ownership for finite local backend invocations."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import IO, Callable, Optional, Protocol, Sequence

from .local_execution_boundary import EvidenceStatus, LocalExecutionBoundary


class WriterState(str, Enum):
    NOT_STARTED = "not-started"
    ACTIVE = "active"
    STOPPING = "stopping"
    POSITIVELY_STOPPED = "positively-stopped"
    TERMINATION_UNKNOWN = "termination-unknown"


class InvocationOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed-out"
    CANCELLED = "cancelled"
    PRESTART_UNAVAILABLE = "pre-start-unavailable"


class PromptTransport(str, Enum):
    INHERIT = "inherit"
    STDIN = "stdin"


@dataclass(frozen=True)
class PolicyInstallation:
    installed: bool
    detail: str = ""
    establishes_filesystem_enforcement: bool = False
    child_setup: Optional[Callable[[], None]] = field(default=None, compare=False, repr=False)
    denial_monitor: Optional["PolicyDenialMonitor"] = field(default=None, compare=False, repr=False)


class PolicyDenialMonitor(Protocol):
    def attach(self, process: subprocess.Popen[bytes]) -> None: ...

    def pump(self) -> tuple[str, ...]: ...

    @property
    def root_returncode(self) -> Optional[int]: ...


@dataclass(frozen=True)
class InstallationContext:
    invocation_id: str
    backend_type: str
    effective_mode: str
    result_root: Path
    runtime_paths: tuple[Path, ...]
    ownership_path: Path
    protected_paths: tuple[Path, ...] = ()
    runtime_inputs: tuple[Path, ...] = ()


class ExecutionPolicyInstaller(Protocol):
    def install(self, context: InstallationContext) -> PolicyInstallation: ...

    def close(self) -> None: ...


def _default_filesystem_policy() -> ExecutionPolicyInstaller:
    # Keep the platform-specific implementation lazy and avoid a module cycle.
    from .filesystem_confinement import LandlockFilesystemPolicy

    return LandlockFilesystemPolicy()


@dataclass(frozen=True)
class InvocationLaunch:
    invocation_id: str
    backend_type: str
    effective_mode: str
    result_root: Path
    runtime_paths: tuple[Path, ...]
    executable: str
    arguments: tuple[str, ...] = ()
    prompt_transport: PromptTransport = PromptTransport.INHERIT
    prompt: Optional[str] = None
    timeout_seconds: Optional[float] = None
    cancellation: Optional[threading.Event] = None
    cwd: Optional[Path] = None
    environment: Optional[dict[str, str]] = None
    protected_paths: tuple[Path, ...] = ()
    runtime_inputs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SupervisedInvocationResult:
    invocation_id: str
    outcome: InvocationOutcome
    writer_state: WriterState
    returncode: Optional[int]
    stdout: str
    stderr: str
    detail: str = ""
    policy_installations: tuple[PolicyInstallation, ...] = ()

    @property
    def writer_complete(self) -> bool:
        return self.writer_state is WriterState.POSITIVELY_STOPPED

    @property
    def replacement_authorized(self) -> bool:
        """Settlement is necessary but never itself a replacement decision."""
        return False


class CgroupV2Unavailable(RuntimeError):
    pass


@dataclass
class CgroupV2Owner:
    """A delegated cgroup-v2 subtree used as a non-PID ownership identity."""

    root: Path = Path("/sys/fs/cgroup/auto-coder")
    worker_uid: Optional[int] = None
    worker_gid: Optional[int] = None
    confirmation_hook: Optional[Callable[[Path], None]] = None

    def prepare(self, invocation_id: str) -> Path:
        if not invocation_id or invocation_id in {".", ".."} or "/" in invocation_id or "\x00" in invocation_id:
            raise CgroupV2Unavailable("invocation identity is not a safe cgroup component")
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file() and self.root == Path("/sys/fs/cgroup/auto-coder"):
            raise CgroupV2Unavailable("Linux cgroup v2 is unavailable")
        group: Optional[Path] = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            group = self.root / invocation_id
            group.mkdir()
            # Real cgroupfs creates these control files. Their absence also keeps
            # ordinary directories from being mistaken for enforcement.
            for required in ("cgroup.procs", "cgroup.events", "cgroup.kill"):
                if not (group / required).is_file():
                    raise CgroupV2Unavailable(f"delegated cgroup lacks {required}")
            if os.geteuid() != 0 or self.worker_uid in (None, 0) or self.worker_gid is None:
                raise CgroupV2Unavailable("cgroup containment requires a root supervisor and an explicit non-root worker identity")
            root_stat = self.root.stat()
            group_stat = group.stat()
            if root_stat.st_uid != 0 or group_stat.st_uid != 0 or (root_stat.st_mode & 0o022):
                raise CgroupV2Unavailable("cgroup subtree must be root-owned and not writable by worker credentials")
            assert self.worker_uid is not None
            assert self.worker_gid is not None
            membership_controls = (path / "cgroup.procs" for path in self.root.iterdir() if path.is_dir())
            for candidate in (self.root, *membership_controls):
                candidate_stat = candidate.stat()
                worker_writable = bool(candidate_stat.st_mode & 0o002)
                worker_writable |= candidate_stat.st_uid == self.worker_uid and bool(candidate_stat.st_mode & 0o200)
                worker_writable |= candidate_stat.st_gid == self.worker_gid and bool(candidate_stat.st_mode & 0o020)
                if worker_writable:
                    raise CgroupV2Unavailable("cgroup hierarchy permits worker-controlled membership migration")
            return group
        except CgroupV2Unavailable:
            if group is not None:
                self.discard(group)
            raise
        except (OSError, ValueError) as exc:
            if group is not None:
                self.discard(group)
            raise CgroupV2Unavailable(f"cannot create delegated invocation cgroup: {exc}") from exc

    def child_joiner(self, group: Path) -> Callable[[], None]:
        def join() -> None:
            fd = os.open(group / "cgroup.procs", os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            assert self.worker_uid is not None
            assert self.worker_gid is not None
            os.setgroups([])
            os.setgid(self.worker_gid)
            os.setuid(self.worker_uid)

        return join

    def stop_and_confirm(self, group: Path, deadline: float) -> None:
        try:
            # cgroup.kill is the only supported termination primitive. Numeric
            # PIDs are reusable and therefore cannot be an ownership identity.
            (group / "cgroup.kill").write_text("1")
            while self._populated(group):
                if time.monotonic() >= deadline:
                    raise TimeoutError("owned cgroup termination was not confirmed")
                time.sleep(0.01)
            if self.confirmation_hook is not None:
                self.confirmation_hook(group)
        except (OSError, ValueError, TimeoutError) as exc:
            raise CgroupV2Unavailable(f"authoritative termination confirmation failed: {exc}") from exc

    @staticmethod
    def _populated(group: Path) -> bool:
        values = dict(line.split(maxsplit=1) for line in (group / "cgroup.events").read_text().splitlines())
        if "populated" not in values:
            raise ValueError("cgroup.events has no populated field")
        return values["populated"] != "0"

    @staticmethod
    def discard(group: Path) -> None:
        try:
            group.rmdir()
        except OSError:
            pass


@dataclass
class InvocationProcessSupervisor:
    owner: CgroupV2Owner = field(default_factory=CgroupV2Owner)
    filesystem_policy_factory: Callable[[], ExecutionPolicyInstaller] = _default_filesystem_policy
    settlement_timeout: float = 5.0
    _states: dict[str, WriterState] = field(default_factory=dict, init=False)
    _retained: dict[str, tuple[subprocess.Popen[bytes], Path]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def state(self, invocation_id: str) -> WriterState:
        with self._lock:
            return self._states.get(invocation_id, WriterState.NOT_STARTED)

    def _set_state(self, invocation_id: str, state: WriterState) -> None:
        with self._lock:
            self._states[invocation_id] = state

    def run(
        self,
        request: InvocationLaunch,
        *,
        policies: Optional[Sequence[ExecutionPolicyInstaller]] = None,
        boundary: Optional[LocalExecutionBoundary] = None,
    ) -> SupervisedInvocationResult:
        if self.state(request.invocation_id) is not WriterState.NOT_STARTED:
            return self._unavailable(request, "invocation identity has already been used")
        try:
            group = self.owner.prepare(request.invocation_id)
        except CgroupV2Unavailable as exc:
            return self._unavailable(request, str(exc))

        selected_policies: Sequence[ExecutionPolicyInstaller] = (self.filesystem_policy_factory(),) if policies is None else policies
        context = InstallationContext(
            request.invocation_id,
            request.backend_type,
            request.effective_mode,
            request.result_root,
            request.runtime_paths,
            group,
            request.protected_paths,
            request.runtime_inputs,
        )
        installations: list[PolicyInstallation] = []
        for policy in selected_policies:
            try:
                installed = policy.install(context)
            except Exception as exc:
                installed = PolicyInstallation(False, f"policy installation raised: {exc}")
            installations.append(installed)
            if not installed.installed:
                for prepared_policy in selected_policies:
                    close = getattr(prepared_policy, "close", None)
                    if close is not None:
                        close()
                self.owner.discard(group)
                return self._unavailable(request, installed.detail or "policy installation failed", tuple(installations))
        if not any(item.establishes_filesystem_enforcement for item in installations):
            for prepared_policy in selected_policies:
                close = getattr(prepared_policy, "close", None)
                if close is not None:
                    close()
            self.owner.discard(group)
            return self._unavailable(request, "filesystem enforcement was not installed", tuple(installations))

        stdin = subprocess.PIPE if request.prompt_transport is PromptTransport.STDIN else None
        child_setups = tuple(item.child_setup for item in installations if item.child_setup is not None)

        def prepare_child() -> None:
            self.owner.child_joiner(group)()
            for setup in child_setups:
                setup()

        try:
            process = subprocess.Popen(
                [request.executable, *request.arguments],
                cwd=request.cwd,
                env=request.environment,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=prepare_child,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            for policy in selected_policies:
                close = getattr(policy, "close", None)
                if close is not None:
                    close()
            self.owner.discard(group)
            return self._unavailable(request, f"owned launch failed: {exc}", tuple(installations))
        for policy in selected_policies:
            close = getattr(policy, "close", None)
            if close is not None:
                close()
        if boundary is not None and any(item.establishes_filesystem_enforcement for item in installations):
            boundary.record_filesystem_enforcement(request.invocation_id, EvidenceStatus.ESTABLISHED)

        monitors = tuple(item.denial_monitor for item in installations if item.denial_monitor is not None)
        try:
            for monitor in monitors:
                monitor.attach(process)
        except (OSError, RuntimeError) as exc:
            self.owner.stop_and_confirm(group, time.monotonic() + self.settlement_timeout)
            self.owner.discard(group)
            if boundary is not None:
                boundary.record_filesystem_enforcement(request.invocation_id, EvidenceStatus.FAILED)
            return self._unavailable(request, f"denial observation unavailable: {exc}", tuple(installations))

        self._set_state(request.invocation_id, WriterState.ACTIVE)
        start = time.monotonic()
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        readers = [
            self._start_reader(process.stdout, stdout_chunks),
            self._start_reader(process.stderr, stderr_chunks),
        ]
        input_writer = self._start_writer(process.stdin, (request.prompt or "").encode("utf-8"))
        outcome = InvocationOutcome.FAILED
        monitor_failures: list[str] = []

        def provider_running() -> bool:
            if monitors:
                try:
                    for monitor in monitors:
                        for denial in monitor.pump():
                            if boundary is not None:
                                boundary.report_policy_violation(request.invocation_id, denial)
                except (OSError, RuntimeError) as exc:
                    monitor_failures.append(str(exc))
                    if boundary is not None:
                        boundary.record_filesystem_enforcement(request.invocation_id, EvidenceStatus.FAILED)
                    return False
                return any(monitor.root_returncode is None for monitor in monitors)
            return process.poll() is None

        while provider_running():
            if request.cancellation is not None and request.cancellation.is_set():
                outcome = InvocationOutcome.CANCELLED
                break
            if request.timeout_seconds is not None and time.monotonic() - start >= request.timeout_seconds:
                outcome = InvocationOutcome.TIMED_OUT
                break
            time.sleep(0.01)
        else:
            monitored_returncode = next((monitor.root_returncode for monitor in monitors if monitor.root_returncode is not None), None)
            if monitored_returncode is not None:
                process.returncode = monitored_returncode
            outcome = InvocationOutcome.SUCCEEDED if process.returncode == 0 else InvocationOutcome.FAILED
        if monitor_failures:
            outcome = InvocationOutcome.FAILED

        self._set_state(request.invocation_id, WriterState.STOPPING)
        detail = f"denial observation failed: {monitor_failures[0]}" if monitor_failures else ""
        try:
            self.owner.stop_and_confirm(group, time.monotonic() + self.settlement_timeout)
            if monitors:
                monitor_deadline = time.monotonic() + self.settlement_timeout
                while any(monitor.root_returncode is None for monitor in monitors) and time.monotonic() < monitor_deadline:
                    for monitor in monitors:
                        for denial in monitor.pump():
                            if boundary is not None:
                                boundary.report_policy_violation(request.invocation_id, denial)
                    time.sleep(0.01)
                monitored_returncode = next((monitor.root_returncode for monitor in monitors if monitor.root_returncode is not None), None)
                if monitored_returncode is not None:
                    process.returncode = monitored_returncode
            process.wait(timeout=self.settlement_timeout)
            writer_state = WriterState.POSITIVELY_STOPPED
            self.owner.discard(group)
            if boundary is not None:
                boundary.record_writer_completion(request.invocation_id)
        except (CgroupV2Unavailable, subprocess.TimeoutExpired) as exc:
            writer_state = WriterState.TERMINATION_UNKNOWN
            detail = str(exc)
            with self._lock:
                self._retained[request.invocation_id] = (process, group)
        self._set_state(request.invocation_id, writer_state)
        if boundary is not None:
            if outcome is InvocationOutcome.SUCCEEDED and writer_state is WriterState.POSITIVELY_STOPPED:
                boundary.record_backend_success(request.invocation_id)
                boundary.record_violation_observation(request.invocation_id)
            else:
                boundary.record_backend_failure(request.invocation_id, detail or outcome.value)
        if writer_state is WriterState.POSITIVELY_STOPPED:
            for thread in (*readers, input_writer):
                if thread is not None:
                    thread.join(timeout=self.settlement_timeout)
        return SupervisedInvocationResult(
            request.invocation_id,
            outcome,
            writer_state,
            process.returncode,
            b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            detail,
            tuple(installations),
        )

    def authorize_replacement(self, predecessor: SupervisedInvocationResult, *, controller_decision: bool) -> bool:
        return controller_decision and predecessor.writer_complete and self.state(predecessor.invocation_id) is WriterState.POSITIVELY_STOPPED

    def settle_retained(self, invocation_id: str) -> bool:
        """Retry authoritative cleanup without converting the original outcome."""
        with self._lock:
            retained = self._retained.get(invocation_id)
        if retained is None:
            return self.state(invocation_id) is WriterState.POSITIVELY_STOPPED
        process, group = retained
        try:
            self.owner.stop_and_confirm(group, time.monotonic() + self.settlement_timeout)
            process.wait(timeout=self.settlement_timeout)
        except (CgroupV2Unavailable, subprocess.TimeoutExpired):
            return False
        self.owner.discard(group)
        with self._lock:
            self._retained.pop(invocation_id, None)
            self._states[invocation_id] = WriterState.POSITIVELY_STOPPED
        return True

    @staticmethod
    def _start_reader(stream: Optional[IO[bytes]], chunks: list[bytes]) -> Optional[threading.Thread]:
        if stream is None:
            return None

        def read() -> None:
            try:
                while chunk := os.read(stream.fileno(), 65536):
                    chunks.append(chunk)
            except OSError:
                pass
            finally:
                stream.close()

        thread = threading.Thread(target=read, name="invocation-output-reader", daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _start_writer(stream: Optional[IO[bytes]], prompt: bytes) -> Optional[threading.Thread]:
        if stream is None:
            return None

        def write() -> None:
            try:
                stream.write(prompt)
                stream.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        thread = threading.Thread(target=write, name="invocation-prompt-writer", daemon=True)
        thread.start()
        return thread

    def _unavailable(
        self,
        request: InvocationLaunch,
        detail: str,
        installations: tuple[PolicyInstallation, ...] = (),
    ) -> SupervisedInvocationResult:
        return SupervisedInvocationResult(
            request.invocation_id,
            InvocationOutcome.PRESTART_UNAVAILABLE,
            WriterState.NOT_STARTED,
            None,
            "",
            "",
            detail,
            installations,
        )
