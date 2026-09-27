"""Linux cgroup-v2 ownership for finite local backend invocations."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

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


@dataclass(frozen=True)
class InstallationContext:
    invocation_id: str
    backend_type: str
    effective_mode: str
    result_root: Path
    runtime_paths: tuple[Path, ...]
    ownership_path: Path


class ExecutionPolicyInstaller(Protocol):
    def install(self, context: InstallationContext) -> PolicyInstallation: ...


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
    confirmation_hook: Optional[Callable[[Path], None]] = None

    def prepare(self, invocation_id: str) -> Path:
        if not invocation_id or invocation_id in {".", ".."} or "/" in invocation_id or "\x00" in invocation_id:
            raise CgroupV2Unavailable("invocation identity is not a safe cgroup component")
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file() and self.root == Path("/sys/fs/cgroup/auto-coder"):
            raise CgroupV2Unavailable("Linux cgroup v2 is unavailable")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            group = self.root / invocation_id
            group.mkdir()
            # Real cgroupfs creates these control files. Their absence also keeps
            # ordinary directories from being mistaken for enforcement.
            for required in ("cgroup.procs", "cgroup.events"):
                if not (group / required).is_file():
                    raise CgroupV2Unavailable(f"delegated cgroup lacks {required}")
            return group
        except OSError as exc:
            raise CgroupV2Unavailable(f"cannot create delegated invocation cgroup: {exc}") from exc

    @staticmethod
    def child_joiner(group: Path) -> Callable[[], None]:
        def join() -> None:
            fd = os.open(group / "cgroup.procs", os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)

        return join

    def stop_and_confirm(self, group: Path, deadline: float) -> None:
        kill_file = group / "cgroup.kill"
        try:
            if kill_file.exists():
                kill_file.write_text("1")
            else:
                # cgroup.kill is Linux 5.14+. A delegated v2 hierarchy on older
                # kernels is still supportable by repeatedly killing every member.
                while True:
                    pids = [int(value) for value in (group / "cgroup.procs").read_text().split()]
                    if not pids:
                        break
                    for pid in pids:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("owned cgroup did not empty")
                    time.sleep(0.01)
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
    settlement_timeout: float = 5.0
    _states: dict[str, WriterState] = field(default_factory=dict, init=False)
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
        policies: Sequence[ExecutionPolicyInstaller] = (),
        boundary: Optional[LocalExecutionBoundary] = None,
    ) -> SupervisedInvocationResult:
        if self.state(request.invocation_id) is not WriterState.NOT_STARTED:
            return self._unavailable(request, "invocation identity has already been used")
        try:
            group = self.owner.prepare(request.invocation_id)
        except CgroupV2Unavailable as exc:
            return self._unavailable(request, str(exc))

        context = InstallationContext(
            request.invocation_id,
            request.backend_type,
            request.effective_mode,
            request.result_root,
            request.runtime_paths,
            group,
        )
        installations: list[PolicyInstallation] = []
        for policy in policies:
            try:
                installed = policy.install(context)
            except Exception as exc:
                installed = PolicyInstallation(False, f"policy installation raised: {exc}")
            installations.append(installed)
            if not installed.installed:
                self.owner.discard(group)
                return self._unavailable(request, installed.detail or "policy installation failed", tuple(installations))

        stdin = subprocess.PIPE if request.prompt_transport is PromptTransport.STDIN else None
        try:
            process = subprocess.Popen(
                [request.executable, *request.arguments],
                cwd=request.cwd,
                env=request.environment,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=self.owner.child_joiner(group),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.owner.discard(group)
            return self._unavailable(request, f"owned launch failed: {exc}", tuple(installations))

        self._set_state(request.invocation_id, WriterState.ACTIVE)
        if process.stdin is not None:
            try:
                process.stdin.write(request.prompt or "")
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        start = time.monotonic()
        outcome = InvocationOutcome.FAILED
        while process.poll() is None:
            if request.cancellation is not None and request.cancellation.is_set():
                outcome = InvocationOutcome.CANCELLED
                break
            if request.timeout_seconds is not None and time.monotonic() - start >= request.timeout_seconds:
                outcome = InvocationOutcome.TIMED_OUT
                break
            time.sleep(0.01)
        else:
            outcome = InvocationOutcome.SUCCEEDED if process.returncode == 0 else InvocationOutcome.FAILED

        self._set_state(request.invocation_id, WriterState.STOPPING)
        detail = ""
        try:
            self.owner.stop_and_confirm(group, time.monotonic() + self.settlement_timeout)
            process.wait(timeout=self.settlement_timeout)
            writer_state = WriterState.POSITIVELY_STOPPED
            self.owner.discard(group)
            if boundary is not None:
                boundary.record_writer_completion(request.invocation_id)
        except (CgroupV2Unavailable, subprocess.TimeoutExpired) as exc:
            writer_state = WriterState.TERMINATION_UNKNOWN
            detail = str(exc)
        self._set_state(request.invocation_id, writer_state)
        # communicate() tries to flush a closed stdin; detach it after the finite
        # prompt has been delivered so output collection remains well-defined.
        process.stdin = None
        stdout, stderr = process.communicate()
        return SupervisedInvocationResult(
            request.invocation_id,
            outcome,
            writer_state,
            process.returncode,
            stdout,
            stderr,
            detail,
            tuple(installations),
        )

    def authorize_replacement(self, predecessor: SupervisedInvocationResult, *, controller_decision: bool) -> bool:
        return controller_decision and predecessor.writer_complete and self.state(predecessor.invocation_id) is WriterState.POSITIVELY_STOPPED

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
