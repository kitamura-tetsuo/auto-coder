import errno
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
    def __init__(self, sentinel: Path, installed: bool, establishes: bool = False) -> None:
        self.sentinel = sentinel
        self.installed = installed
        self.establishes = establishes
        self.context: InstallationContext | None = None

    def install(self, context: InstallationContext) -> PolicyInstallation:
        assert not self.sentinel.exists()
        self.context = context
        return PolicyInstallation(
            self.installed,
            "probe rejected execution",
            establishes_filesystem_enforcement=self.establishes,
        )

    def close(self) -> None:
        pass


class TestFilesystemPolicy:
    def install(self, context: InstallationContext) -> PolicyInstallation:
        return PolicyInstallation(True, establishes_filesystem_enforcement=True)

    def close(self) -> None:
        pass


def make_supervisor(owner: ProcessGroupOwner, **kwargs) -> InvocationProcessSupervisor:
    return InvocationProcessSupervisor(
        owner=owner,  # type: ignore[arg-type]
        filesystem_policy_factory=TestFilesystemPolicy,
        **kwargs,
    )


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
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))

    result = supervisor.run(request(tmp_path, f"open({str(sentinel)!r}, 'w').close()"), policies=(policy,))

    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert result.writer_state is WriterState.NOT_STARTED
    assert result.policy_installations == (PolicyInstallation(False, "probe rejected execution"),)
    assert policy.context is not None
    assert policy.context.invocation_id == "invocation-one"
    assert not sentinel.exists()


@pytest.mark.parametrize("policies", [(), (ProbePolicy(Path("/nonexistent"), installed=True),)])
def test_launch_requires_a_filesystem_enforcement_installation(tmp_path: Path, policies) -> None:
    sentinel = tmp_path / "outside"
    launch = request(tmp_path, f"open({str(sentinel)!r}, 'w').close()")
    runner = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))

    result = runner.run(launch, policies=policies)

    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert result.writer_state is WriterState.NOT_STARTED
    assert "filesystem enforcement was not installed" in result.detail
    assert not sentinel.exists()


def test_prompt_and_failure_survive_positive_settlement(tmp_path: Path) -> None:
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))
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
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))
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
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))
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
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))
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
    supervisor = make_supervisor(owner, settlement_timeout=0.1)

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
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))
    cancel = threading.Event()
    first_result = tmp_path / "first-result"
    second_ready = tmp_path / "second-ready"
    second_release = tmp_path / "second-release"
    second_heartbeat = tmp_path / "second-heartbeat"
    first_result.mkdir()
    results = []

    first = request(tmp_path, "import time; time.sleep(30)", invocation_id="first", cancellation=cancel)
    second = request(
        tmp_path,
        f"""import pathlib, time
pathlib.Path({str(second_ready)!r}).touch()
while not pathlib.Path({str(second_release)!r}).exists():
    time.sleep(.01)
pathlib.Path({str(second_heartbeat)!r}).write_text('alive')
""",
        invocation_id="second",
    )
    one = threading.Thread(target=lambda: results.append(supervisor.run(first)))
    two = threading.Thread(target=lambda: results.append(supervisor.run(second)))
    try:
        one.start()
        first_deadline = time.monotonic() + 15
        while supervisor.state("first") is not WriterState.ACTIVE:
            assert one.is_alive(), "first invocation exited before becoming active"
            assert time.monotonic() < first_deadline
            time.sleep(0.01)
        # Serialize only the fork/pre-exec setup. The two owned provider processes
        # still overlap, which is the boundary this test exercises, while avoiding
        # a test-only concurrent preexec_fn deadlock under coverage instrumentation.
        two.start()
        second_deadline = time.monotonic() + 15
        while not second_ready.exists() or supervisor.state("second") is not WriterState.ACTIVE:
            assert two.is_alive(), "peer invocation exited before becoming active"
            assert time.monotonic() < second_deadline
            time.sleep(0.01)
        cancel.set()
        one.join(3)

        assert not one.is_alive()
        assert two.is_alive()
        assert supervisor.state("second") is WriterState.ACTIVE
        assert not second_heartbeat.exists()
        second_release.touch()
        two.join(3)
        assert not two.is_alive()
    finally:
        cancel.set()
        second_release.touch()
        for worker in (one, two):
            if worker.ident is not None:
                worker.join(3)

    by_id = {item.invocation_id: item for item in results}
    assert by_id["first"].outcome is InvocationOutcome.CANCELLED
    assert by_id["first"].writer_complete
    assert by_id["second"].outcome is InvocationOutcome.SUCCEEDED
    assert second_heartbeat.read_text() == "alive"
    assert first_result.is_dir()


def test_invocation_identity_cannot_be_reused(tmp_path: Path) -> None:
    supervisor = make_supervisor(ProcessGroupOwner(tmp_path / "owners"))
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


@pytest.fixture
def emulated_cgroup_owner(tmp_path: Path, monkeypatch) -> CgroupV2Owner:
    """Emulate cgroupfs metadata for unit interleavings, not kernel conformance."""
    root = tmp_path / "cgroups"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    original_mkdir = Path.mkdir
    original_rmdir = Path.rmdir
    original_stat = Path.stat
    controls = ("cgroup.procs", "cgroup.events", "cgroup.kill")

    def mkdir(path: Path, *args, **kwargs) -> None:
        original_mkdir(path, *args, **kwargs)
        if path.parent == root:
            path.chmod(0o755)
            for name in controls:
                control = path / name
                control.write_text("populated 0\n" if name == "cgroup.events" else "")
                control.chmod(0o644)

    def rmdir(path: Path) -> None:
        if path.parent == root:
            for name in controls:
                (path / name).unlink(missing_ok=True)
        original_rmdir(path)

    def root_owned_stat(path: Path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path == root or root in path.parents:
            values = list(metadata)
            values[4:6] = [0, 0]
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    monkeypatch.setattr(Path, "stat", root_owned_stat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    return CgroupV2Owner(root=root, worker_uid=65534, worker_gid=65534)


@pytest.mark.parametrize(
    "change,expected_error",
    [
        ("retired-peer", None),
        ("retired-peer-unsafe-survivor", "membership migration"),
        ("missing-peer-control", "No such file or directory"),
        ("peer-permission", "Permission denied"),
        ("peer-parent-permission", "Permission denied"),
        ("missing-own-control", "No such file or directory"),
        ("missing-own-group", "No such file or directory"),
        ("missing-root", "No such file or directory"),
    ],
)
def test_production_owner_membership_checks_handle_peer_cleanup(tmp_path: Path, monkeypatch, emulated_cgroup_owner: CgroupV2Owner, change: str, expected_error: str | None) -> None:
    owner = emulated_cgroup_owner
    peer = owner.prepare("retiring-peer")
    survivor = owner.prepare("surviving-peer")
    current = owner.root / "invocation-one"
    peer_control = peer / "cgroup.procs"
    original_stat = Path.stat
    original_iterdir = Path.iterdir
    peer_check_started = False
    survivor_checks: list[Path] = []

    if change == "retired-peer-unsafe-survivor":
        (survivor / "cgroup.procs").chmod(0o666)

    def ordered_inventory(path: Path):
        # All three groups exist during enumeration. Cleanup happens at the later stat.
        return iter((current, peer, survivor)) if path == owner.root else original_iterdir(path)

    def stat_with_interleaving(path: Path, *args, **kwargs):
        nonlocal peer_check_started
        if path == peer_control and not peer_check_started:
            peer_check_started = True
            if change == "peer-permission":
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            if change == "missing-peer-control":
                path.unlink()
            else:
                owner.discard(peer)
                if change == "missing-own-control":
                    (current / "cgroup.procs").unlink()
                elif change == "missing-own-group":
                    owner.discard(current)
                elif change == "missing-root":
                    owner.discard(current)
                    owner.discard(survivor)
                    owner.root.rmdir()
        if path == peer and peer_check_started and change == "peer-parent-permission":
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        if path == survivor / "cgroup.procs":
            survivor_checks.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", ordered_inventory)
    monkeypatch.setattr(Path, "stat", stat_with_interleaving)

    if expected_error is None:
        assert owner.prepare("invocation-one") == current
        assert current.is_dir()
        assert not peer.exists()
        assert survivor.is_dir()
    else:

        def unexpected_policy_setup():
            pytest.fail("unsafe cgroup preparation reached policy setup")

        provider_started = tmp_path / "provider-started"
        supervisor = InvocationProcessSupervisor(owner=owner, filesystem_policy_factory=unexpected_policy_setup)
        result = supervisor.run(request(tmp_path, f"from pathlib import Path; Path({str(provider_started)!r}).touch()"))

        assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
        assert result.writer_state is WriterState.NOT_STARTED
        assert result.returncode is None
        assert expected_error in result.detail
        assert not provider_started.exists()

    assert peer_check_started
    assert survivor_checks == ([survivor / "cgroup.procs"] if change in {"retired-peer", "retired-peer-unsafe-survivor"} else [])


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
    (runtime / "result").mkdir()
    (runtime / "runtime").mkdir()
    os.chown(runtime / "runtime", 65534, 65534)
    ready = runtime / "runtime" / "ready"
    late = runtime / "runtime" / "late-write"
    migrated = runtime / "runtime" / "migrated"
    sibling = owner.root / f"escape-{time.time_ns()}"
    code = f"""import os, pathlib, time
child = os.fork()
if child:
    while not pathlib.Path({str(ready)!r}).exists() or pathlib.Path({str(ready)!r}).read_text() != "ready":
        time.sleep(.01)
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


@pytest.mark.parametrize("mismatch", ["invocation_id", "backend_type", "result_root", "effective_mode"])
def test_launch_mismatch_refuses_before_owner_or_provider(tmp_path: Path, mismatch: str) -> None:
    from dataclasses import replace

    from src.auto_coder.local_execution_boundary import LocalExecutionBoundary
    from tests.test_local_execution_boundary import _binding

    binding = _binding(tmp_path)
    boundary = LocalExecutionBoundary(binding, "codex", editable=False)
    marker = tmp_path / "provider-started"
    launch = InvocationLaunch(
        invocation_id=binding.invocation_id,
        backend_type="codex",
        effective_mode="no-edit",
        result_root=binding.workspace,
        runtime_paths=(),
        executable=sys.executable,
        arguments=("-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
    )
    changes = {"invocation_id": "different", "backend_type": "opencode", "result_root": tmp_path, "effective_mode": "editable"}
    supervisor = InvocationProcessSupervisor()
    result = supervisor.run(replace(launch, **{mismatch: changes[mismatch]}), boundary=boundary)
    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert result.detail == "launch does not match its invocation boundary"
    assert not marker.exists()
