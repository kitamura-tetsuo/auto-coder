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

    def __init__(self, root: Path, fail_confirmation: bool = False, kill_before_failure: bool = True) -> None:
        self.root = root
        self.fail_confirmation = fail_confirmation
        self.kill_before_failure = kill_before_failure

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
        if self.kill_before_failure:
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


def test_large_output_is_drained_while_provider_runs(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    size = 2 * 1024 * 1024

    result = supervisor.run(
        request(
            tmp_path,
            f"import os; os.write(1, b'o' * {size}); os.write(2, b'e' * {size})",
            timeout_seconds=5,
        )
    )

    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.writer_state is WriterState.POSITIVELY_STOPPED
    assert result.stdout == "o" * size
    assert result.stderr == "e" * size


def test_large_unread_prompt_does_not_block_cancellation(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    cancellation = threading.Event()
    launch = request(
        tmp_path,
        "import time; time.sleep(30)",
        prompt_transport=PromptTransport.STDIN,
        prompt="p" * (4 * 1024 * 1024),
        cancellation=cancellation,
    )
    results = []
    worker = threading.Thread(target=lambda: results.append(supervisor.run(launch)))
    started = time.monotonic()
    worker.start()
    deadline = started + 3
    while supervisor.state(launch.invocation_id) is not WriterState.ACTIVE:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    cancellation.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert time.monotonic() - started < 3
    assert results[0].outcome is InvocationOutcome.CANCELLED
    assert results[0].writer_state is WriterState.POSITIVELY_STOPPED


def test_large_unread_prompt_does_not_block_timeout(tmp_path: Path) -> None:
    supervisor = InvocationProcessSupervisor(owner=ProcessGroupOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    started = time.monotonic()

    result = supervisor.run(
        request(
            tmp_path,
            "import time; time.sleep(30)",
            prompt_transport=PromptTransport.STDIN,
            prompt="p" * (4 * 1024 * 1024),
            timeout_seconds=0.05,
        )
    )

    assert time.monotonic() - started < 1
    assert result.outcome is InvocationOutcome.TIMED_OUT
    assert result.writer_state is WriterState.POSITIVELY_STOPPED


def test_timeout_and_confirmation_uncertainty_remain_distinct(tmp_path: Path) -> None:
    owner = ProcessGroupOwner(tmp_path / "owners", fail_confirmation=True, kill_before_failure=False)
    supervisor = InvocationProcessSupervisor(owner=owner, settlement_timeout=0.1)  # type: ignore[arg-type]

    started = time.monotonic()
    result = supervisor.run(request(tmp_path, "import time; time.sleep(30)", timeout_seconds=0.05))

    assert result.outcome is InvocationOutcome.TIMED_OUT
    assert result.writer_state is WriterState.TERMINATION_UNKNOWN
    assert result.returncode is None
    assert "injected confirmation failure" in result.detail
    assert time.monotonic() - started < 1
    assert not supervisor.authorize_replacement(result, controller_decision=True)
    owner.fail_confirmation = False
    owner.kill_before_failure = True
    assert supervisor.settle_retained(result.invocation_id)


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


def test_production_owner_rejects_unsafe_termination_profile_before_start(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "cgroups"
    original_mkdir = Path.mkdir

    def create_fake_cgroup(path: Path, *args, **kwargs) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.name == "unsafe":
            (path / "cgroup.procs").touch()
            (path / "cgroup.events").write_text("populated 0\n")

    monkeypatch.setattr(Path, "mkdir", create_fake_cgroup)
    owner = CgroupV2Owner(root=root, worker_uid=65534, worker_gid=65534)

    with pytest.raises(CgroupV2Unavailable, match="cgroup.kill"):
        owner.prepare("unsafe")


@pytest.mark.skipif(sys.platform != "linux", reason="cgroup-v2 conformance is Linux-specific")
def test_linux_cgroup_stops_double_forked_writer_before_release(tmp_path: Path) -> None:
    """Positive runtime check for detach/reparent ownership on delegated hosts."""
    owner = CgroupV2Owner(worker_uid=65534, worker_gid=65534)
    probe_id = f"conformance-probe-{os.getpid()}-{time.time_ns()}"
    try:
        probe = owner.prepare(probe_id)
    except CgroupV2Unavailable as exc:
        pytest.skip(str(exc))
    else:
        owner.discard(probe)

    runtime = Path("/tmp") / f"auto-coder-cgroup-test-{os.getpid()}-{time.time_ns()}"
    runtime.mkdir(mode=0o777)
    ready = runtime / "ready"
    late = runtime / "late-write"
    migrated = runtime / "migrated"
    sibling = owner.root / f"escape-{time.time_ns()}"
    code = f"""import os, pathlib, time
child = os.fork()
if child:
    os._exit(0)
os.setsid()
try:
    pathlib.Path({str(sibling)!r}).mkdir()
    pathlib.Path({str(sibling / 'cgroup.procs')!r}).write_text(str(os.getpid()))
    pathlib.Path({str(migrated)!r}).write_text('escaped ownership')
except PermissionError:
    pass
pathlib.Path({str(ready)!r}).write_text('ready')
time.sleep(.5)
pathlib.Path({str(late)!r}).write_text('escaped')
"""
    supervisor = InvocationProcessSupervisor(owner=owner)

    result = supervisor.run(request(runtime, code, invocation_id=f"conformance-{time.time_ns()}"))

    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.writer_state is WriterState.POSITIVELY_STOPPED
    assert ready.read_text() == "ready"
    assert not migrated.exists()
    time.sleep(0.6)
    assert not late.exists()
