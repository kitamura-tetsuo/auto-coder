import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from src.auto_coder.invocation_process_supervisor import (
    CgroupV2Owner,
    CgroupV2Unavailable,
    InstallationContext,
    InvocationLaunch,
    InvocationOutcome,
    InvocationProcessSupervisor,
    PolicyInstallation,
    PromptTransport,
    WriterState,
)


class ProcessGroupOwner:
    """Deterministic unit-test owner; production conformance uses cgroup v2."""

    def __init__(self, root: Path, fail_confirmation: bool = False) -> None:
        self.root = root
        self.fail_confirmation = fail_confirmation

    def prepare(self, invocation_id: str) -> Path:
        group = self.root / invocation_id
        group.mkdir(parents=True)
        return group

    @staticmethod
    def child_joiner(group: Path):
        def join() -> None:
            os.setsid()
            (group / "pid").write_text(str(os.getpid()))

        return join

    def stop_and_confirm(self, group: Path, deadline: float) -> None:
        pid = int((group / "pid").read_text())
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if self.fail_confirmation:
            raise CgroupV2Unavailable("injected confirmation failure")

    @staticmethod
    def discard(group: Path) -> None:
        for child in group.iterdir():
            child.unlink()
        group.rmdir()


class ProbePolicy:
    def __init__(self, sentinel: Path, installed: bool) -> None:
        self.sentinel = sentinel
        self.installed = installed
        self.context: InstallationContext | None = None

    def install(self, context: InstallationContext) -> PolicyInstallation:
        assert not self.sentinel.exists()
        self.context = context
        return PolicyInstallation(self.installed, "probe rejected execution")


def request(tmp_path: Path, code: str, **kwargs) -> InvocationLaunch:
    return InvocationLaunch(
        invocation_id=kwargs.pop("invocation_id", "invocation-one"),
        backend_type="opencode",
        effective_mode="editable",
        result_root=tmp_path / "result",
        runtime_paths=(tmp_path / "runtime",),
        executable=sys.executable,
        arguments=("-c", code),
        cwd=tmp_path,
        **kwargs,
    )


def test_policy_failure_prevents_task_start(tmp_path: Path) -> None:
    sentinel = tmp_path / "started"
    policy = ProbePolicy(sentinel, installed=False)
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request(tmp_path, f"open({str(sentinel)!r}, 'w').close()"), policies=(policy,))

    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert result.writer_state is WriterState.NOT_STARTED
    assert result.policy_installations == (PolicyInstallation(False, "probe rejected execution"),)
    assert policy.context is not None
    assert policy.context.invocation_id == "invocation-one"
    assert not sentinel.exists()


def test_prompt_and_failure_survive_positive_settlement(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    launch = request(
        tmp_path,
        "import sys; data=sys.stdin.read(); print(data); raise SystemExit(7)",
        prompt_transport=PromptTransport.STDIN,
        prompt="finite prompt",
    )

    result = supervisor.run(launch)

    assert result.outcome is InvocationOutcome.FAILED
    assert result.writer_state is WriterState.POSITIVELY_STOPPED
    assert result.returncode == 7
    assert result.stdout == "finite prompt\n"
    assert not result.replacement_authorized
    assert not supervisor.authorize_replacement(result, controller_decision=False)
    assert supervisor.authorize_replacement(result, controller_decision=True)


def test_timeout_and_confirmation_uncertainty_remain_distinct(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners", fail_confirmation=True))  # type: ignore[arg-type]

    result = supervisor.run(request(tmp_path, "import time; time.sleep(30)", timeout_seconds=0.05))

    assert result.outcome is InvocationOutcome.TIMED_OUT
    assert result.writer_state is WriterState.TERMINATION_UNKNOWN
    assert result.returncode == -signal.SIGKILL
    assert "injected confirmation failure" in result.detail
    assert not supervisor.authorize_replacement(result, controller_decision=True)


def test_cancelling_one_owner_does_not_stop_peer_or_delete_results(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    cancel = threading.Event()
    first_result = tmp_path / "first-result"
    second_heartbeat = tmp_path / "second-heartbeat"
    first_result.mkdir()
    results = []

    first = request(tmp_path, "import time; time.sleep(30)", invocation_id="first", cancellation=cancel)
    second = request(
        tmp_path,
        f"import pathlib,time; time.sleep(.15); pathlib.Path({str(second_heartbeat)!r}).write_text('alive')",
        invocation_id="second",
    )
    one = threading.Thread(target=lambda: results.append(supervisor.run(first)))
    two = threading.Thread(target=lambda: results.append(supervisor.run(second)))
    one.start()
    two.start()
    deadline = time.monotonic() + 3
    while supervisor.state("first") is not WriterState.ACTIVE or supervisor.state("second") is not WriterState.ACTIVE:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    cancel.set()
    one.join(3)
    two.join(3)

    by_id = {item.invocation_id: item for item in results}
    assert by_id["first"].outcome is InvocationOutcome.CANCELLED
    assert by_id["first"].writer_complete
    assert by_id["second"].outcome is InvocationOutcome.SUCCEEDED
    assert second_heartbeat.read_text() == "alive"
    assert first_result.is_dir()


def test_invocation_identity_cannot_be_reused(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    first = supervisor.run(request(tmp_path, "pass"))
    second = supervisor.run(request(tmp_path, "pass"))
    assert first.writer_complete
    assert second.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert second.writer_state is WriterState.NOT_STARTED
    assert "already been used" in second.detail


@pytest.mark.skipif(sys.platform != "linux", reason="cgroup-v2 conformance is Linux-specific")
def test_linux_cgroup_stops_double_forked_writer_before_release(tmp_path: Path) -> None:
    """Positive runtime check for detach/reparent ownership on delegated hosts."""
    owner = CgroupV2Owner()
    probe_id = f"conformance-probe-{os.getpid()}-{time.time_ns()}"
    try:
        probe = owner.prepare(probe_id)
    except CgroupV2Unavailable as exc:
        pytest.skip(str(exc))
    else:
        owner.discard(probe)

    ready = tmp_path / "ready"
    late = tmp_path / "late-write"
    code = "import os,pathlib,time; child=os.fork(); " f"(os.setsid(), pathlib.Path({str(ready)!r}).write_text('ready'), " f"time.sleep(.5), pathlib.Path({str(late)!r}).write_text('escaped')) if child == 0 else os._exit(0)"
    supervisor = InvocationProcessSupervisor(owner=owner)

    result = supervisor.run(request(tmp_path, code, invocation_id=f"conformance-{time.time_ns()}"))

    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.writer_state is WriterState.POSITIVELY_STOPPED
    assert ready.read_text() == "ready"
    time.sleep(0.6)
    assert not late.exists()
