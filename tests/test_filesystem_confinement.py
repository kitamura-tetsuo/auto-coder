import os
import signal
import sys
from pathlib import Path

import pytest

from src.auto_coder import filesystem_confinement
from src.auto_coder.filesystem_confinement import LandlockFilesystemPolicy, PtraceDenialMonitor
from src.auto_coder.invocation_process_supervisor import InvocationLaunch, InvocationOutcome, InvocationProcessSupervisor
from src.auto_coder.local_execution_boundary import EvidenceStatus, LocalExecutionBoundary
from src.auto_coder.worktree_utils import LocalWorkspaceBinding, LocalWorkspaceOwnership


class ProcessOwner:
    def __init__(self, root: Path) -> None:
        self.root = root

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

    @staticmethod
    def stop_and_confirm(group: Path, deadline: float) -> None:
        try:
            os.killpg(int((group / "pid").read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def discard(group: Path) -> None:
        for child in group.iterdir():
            child.unlink()
        group.rmdir()


def test_denial_monitor_does_not_consume_unrelated_controller_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = PtraceDenialMonitor(())
    monitor._tracees.add(1234)
    waited: list[int] = []

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        waited.append(pid)
        return 0, 0

    monkeypatch.setattr(os, "waitpid", waitpid)

    assert monitor.pump() == ()
    assert waited == [1234]


def test_denial_monitor_does_not_redeliver_synthetic_syscall_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = PtraceDenialMonitor(())
    resumed: list[tuple[int, int]] = []
    info = filesystem_confinement._SyscallInfo()

    monkeypatch.setattr(filesystem_confinement, "_syscall_info", lambda pid: info)
    monkeypatch.setattr(
        filesystem_confinement,
        "_ptrace",
        lambda request, pid, address, data: resumed.append((request, int(data))) or 0,
    )

    syscall_stop_status = ((signal.SIGTRAP | 0x80) << 8) | 0x7F
    assert monitor._handle_stop(1234, syscall_stop_status) == ()
    assert resumed == [(filesystem_confinement._PTRACE_SYSCALL, 0)]


def _launch(tmp_path: Path, code: str, *, mode: str = "editable", runtime_inputs: tuple[Path, ...] = ()) -> InvocationLaunch:
    private = tmp_path / "private"
    runtime = tmp_path / "runtime"
    protected = tmp_path / "caller"
    for path in (private, runtime, protected):
        path.mkdir(exist_ok=True)
    return InvocationLaunch(
        invocation_id=f"landlock-{mode}",
        backend_type="test-provider",
        effective_mode=mode,
        result_root=private,
        runtime_paths=(runtime,),
        protected_paths=(protected,),
        runtime_inputs=runtime_inputs,
        executable=sys.executable,
        arguments=("-c", code),
        cwd=private,
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_editable_policy_allows_private_git_style_writes_and_denies_absolute_escape(tmp_path: Path) -> None:
    private = tmp_path / "private"
    protected = tmp_path / "caller"
    code = f"""from pathlib import Path
Path('edit.txt').write_text('private')
try:
    Path({str(protected / 'escaped.txt')!r}).write_text('escape')
except PermissionError:
    print('escape-denied')
"""
    request = _launch(tmp_path, code)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),))

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "escape-denied\n"
    assert (private / "edit.txt").read_text() == "private"
    assert not (protected / "escaped.txt").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_supervisor_default_policy_denies_outside_write(tmp_path: Path) -> None:
    protected = tmp_path / "caller"
    request = _launch(
        tmp_path,
        f"from pathlib import Path\ntry: Path({str(protected / 'escape')!r}).write_text('bad')\nexcept PermissionError: pass",
    )
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request)

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert not (protected / "escape").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_no_edit_policy_denies_repository_write_before_effect(tmp_path: Path) -> None:
    private = tmp_path / "private"
    code = """from pathlib import Path
print(Path('.').is_dir())
try:
    Path('edit.txt').write_text('forbidden')
except PermissionError:
    print('write-denied')
"""
    request = _launch(tmp_path, code, mode="no-edit")
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),))

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "True\nwrite-denied\n"
    assert not (private / "edit.txt").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_no_edit_policy_allows_private_runtime_but_denies_repository_write(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    code = f"""from pathlib import Path
Path({str(runtime / 'provider-state')!r}).write_text('started')
try:
    Path('edit.txt').write_text('forbidden')
except PermissionError:
    print('repository-denied')
"""
    request = _launch(tmp_path, code, mode="no-edit")
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),))

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "repository-denied\n"
    assert (runtime / "provider-state").read_text() == "started"
    assert not (request.result_root / "edit.txt").exists()


def test_policy_rejects_protected_alias_before_task_submission(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    request = InvocationLaunch(
        invocation_id="alias",
        backend_type="test-provider",
        effective_mode="editable",
        result_root=private,
        runtime_paths=(runtime,),
        protected_paths=(private,),
        executable=sys.executable,
        arguments=("-c", f"open({str(tmp_path / 'started')!r}, 'w').close()"),
    )
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),))

    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert "aliases protected data" in result.detail
    assert not (tmp_path / "started").exists()


def test_policy_rejects_landlock_without_truncation_support_before_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _launch(tmp_path, f"open({str(tmp_path / 'started')!r}, 'w').close()")
    monkeypatch.setattr(filesystem_confinement, "_landlock_abi", lambda: 2)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),))

    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert "ABI 3 or newer" in result.detail
    assert not (tmp_path / "started").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_runtime_input_does_not_block_provider_execution_or_private_reads(tmp_path: Path) -> None:
    runtime_input = tmp_path / "approved-input"
    runtime_input.mkdir()
    (runtime_input / "model.txt").write_text("approved")
    request = _launch(
        tmp_path,
        f"from pathlib import Path; print(Path({str(runtime_input / 'model.txt')!r}).read_text()); Path('edit').write_text('ok')",
        runtime_inputs=(runtime_input,),
    )
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(read_visibility=(request.result_root,)),))

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "approved\n"
    assert (request.result_root / "edit").read_text() == "ok"


def _boundary(request: InvocationLaunch, tmp_path: Path) -> LocalExecutionBoundary:
    protected = request.protected_paths[0]
    binding = LocalWorkspaceBinding(
        invocation_id=request.invocation_id,
        caller_root=protected,
        caller_git_dir=protected / ".git",
        caller_common_dir=protected / ".git",
        initial_head="refs/heads/main",
        initial_commit="a" * 40,
        index_checksum="index",
        file_snapshot_checksum="files",
        workspace=request.result_root,
        ownership=LocalWorkspaceOwnership(),
    )
    return LocalExecutionBoundary(binding=binding, backend_type=request.backend_type, editable=request.effective_mode == "editable")


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_denied_escape_is_a_sticky_controller_owned_violation(tmp_path: Path) -> None:
    protected = tmp_path / "caller"
    request = _launch(
        tmp_path,
        f"from pathlib import Path\ntry: Path({str(protected / 'escape')!r}).write_text('bad')\nexcept PermissionError: pass\nprint('success')",
    )
    boundary = _boundary(request, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),), boundary=boundary)

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    evidence = boundary.evidence()
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "success\n"
    assert evidence.filesystem_enforcement is EvidenceStatus.ESTABLISHED
    assert evidence.violation_observation is EvidenceStatus.FAILED
    assert evidence.policy_violation
    assert not evidence.confined_result_authorized
    boundary.record_backend_success(request.invocation_id)
    assert boundary.evidence().policy_violation


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
@pytest.mark.parametrize("escape_kind", ["openat2", "leaf-symlink", "chmod", "utime", "setxattr"])
def test_denied_nonstandard_escape_is_a_sticky_violation(tmp_path: Path, escape_kind: str) -> None:
    protected = tmp_path / "caller"
    target = protected / "protected.txt"
    private = tmp_path / "private"
    if escape_kind == "openat2":
        code = f"""import ctypes, os
class OpenHow(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_ulonglong), ('mode', ctypes.c_ulonglong), ('resolve', ctypes.c_ulonglong)]
how = OpenHow(os.O_WRONLY, 0, 0)
libc = ctypes.CDLL(None, use_errno=True)
assert libc.syscall(437, -100, {os.fsencode(target)!r}, ctypes.byref(how), ctypes.sizeof(how)) == -1
assert ctypes.get_errno() == 13
"""
    elif escape_kind == "leaf-symlink":
        code = f"""import os
os.symlink({str(target)!r}, 'escape-link')
try:
    open('escape-link', 'w').write('bad')
except PermissionError:
    pass
"""
    elif escape_kind == "chmod":
        code = f"""import os
try:
    os.chmod({str(private / 'tracked.sh')!r}, 0o755)
except PermissionError:
    pass
"""
    elif escape_kind == "utime":
        code = f"""import os
try:
    os.utime({str(private / 'tracked.sh')!r}, (1, 1))
except PermissionError:
    pass
"""
    else:
        code = f"""import os
try:
    os.setxattr({str(private / 'tracked.sh')!r}, b'user.auto-coder-test', b'bad')
except PermissionError:
    pass
"""
    request = _launch(
        tmp_path,
        code,
        mode="no-edit" if escape_kind in {"chmod", "utime", "setxattr"} else "editable",
    )
    target.write_text("protected")
    tracked = request.result_root / "tracked.sh"
    tracked.write_text("echo safe\n")
    tracked.chmod(0o644)
    original_mtime_ns = tracked.stat().st_mtime_ns
    boundary = _boundary(request, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),), boundary=boundary)

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert target.read_text() == "protected"
    assert tracked.stat().st_mode & 0o777 == 0o644
    assert tracked.stat().st_mtime_ns == original_mtime_ns
    assert "user.auto-coder-test" not in os.listxattr(tracked)
    evidence = boundary.evidence()
    assert evidence.policy_violation
    assert evidence.violation_observation is EvidenceStatus.FAILED
    boundary.record_backend_success(request.invocation_id)
    assert boundary.evidence().policy_violation


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_confined_forked_child_can_edit_private_root(tmp_path: Path) -> None:
    request = _launch(
        tmp_path,
        "import os\npid=os.fork()\nif pid == 0:\n open('child-edit', 'w').write('ok'); os._exit(0)\nos.waitpid(pid, 0)",
    )
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),))

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert (request.result_root / "child-edit").read_text() == "ok"


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock is Linux-specific")
def test_ordinary_permitted_command_failure_is_not_a_policy_violation(tmp_path: Path) -> None:
    request = _launch(tmp_path, "from pathlib import Path\ntry: Path('missing').read_text()\nexcept FileNotFoundError: pass")
    boundary = _boundary(request, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),), boundary=boundary)

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail(f"supported CI confinement profile unavailable: {result.detail}")
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert not boundary.evidence().policy_violation


def test_codex_noedit_denied_write_fails_even_after_exit_zero(tmp_path: Path) -> None:
    from dataclasses import replace

    from src.auto_coder.local_execution_boundary import BackendOutcome

    launch = replace(_launch(tmp_path, "from pathlib import Path\ntry: Path('tracked.txt').write_text('forbidden')\nexcept PermissionError: print('caught-denial')", mode="no-edit"), backend_type="codex")
    protected = launch.result_root / "tracked.txt"
    protected.write_text("preserved")
    boundary = _boundary(launch, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    result = supervisor.run(launch, boundary=boundary)
    assert result.returncode == 0
    assert result.stdout == "caught-denial\n"
    assert result.outcome is InvocationOutcome.FAILED
    assert result.writer_complete
    assert protected.read_text() == "preserved"
    assert boundary.evidence().backend_outcome is BackendOutcome.FAILED
    assert boundary.evidence().policy_violation
    assert not boundary.evidence().confined_result_authorized


def test_codex_noedit_can_remove_own_runtime_symlink_without_mutating_referent(tmp_path: Path) -> None:
    from dataclasses import replace

    launch = replace(_launch(tmp_path, "", mode="no-edit"), backend_type="codex")
    protected = tmp_path / "caller" / "codex-executable"
    protected.write_text("protected executable")
    alias = launch.runtime_paths[0] / "codex-alias"
    alias.symlink_to(protected)
    launch = replace(launch, arguments=("-c", f"from pathlib import Path; Path({str(alias)!r}).unlink(); print('alias-removed')"))
    boundary = _boundary(launch, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    result = supervisor.run(launch, boundary=boundary)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "alias-removed\n"
    assert not alias.is_symlink()
    assert protected.read_text() == "protected executable"
    assert not boundary.evidence().policy_violation
    assert boundary.evidence().confined_result_authorized


def test_codex_noedit_cannot_remove_caller_symlink_pointing_into_runtime(tmp_path: Path) -> None:
    from dataclasses import replace

    launch = replace(_launch(tmp_path, "", mode="no-edit"), backend_type="codex")
    owned_file = launch.runtime_paths[0] / "owned"
    owned_file.write_text("owned content")
    protected_alias = tmp_path / "caller" / "alias"
    protected_alias.symlink_to(owned_file)
    code = f"from pathlib import Path\ntry: Path({str(protected_alias)!r}).unlink()\nexcept PermissionError: print('caller-alias-denied')"
    launch = replace(launch, arguments=("-c", code))
    boundary = _boundary(launch, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]
    result = supervisor.run(launch, boundary=boundary)
    assert result.outcome is InvocationOutcome.FAILED
    assert result.returncode == 0
    assert result.stdout == "caller-alias-denied\n"
    assert protected_alias.is_symlink()
    assert owned_file.read_text() == "owned content"
    assert boundary.evidence().policy_violation
