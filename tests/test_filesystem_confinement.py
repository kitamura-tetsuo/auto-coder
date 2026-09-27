import os
import signal
import sys
from pathlib import Path

import pytest

from src.auto_coder.filesystem_confinement import LandlockFilesystemPolicy
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
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "escape-denied\n"
    assert (private / "edit.txt").read_text() == "private"
    assert not (protected / "escaped.txt").exists()


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
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.stdout == "True\nwrite-denied\n"
    assert not (private / "edit.txt").exists()


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
def test_ordinary_permitted_command_failure_is_not_a_policy_violation(tmp_path: Path) -> None:
    request = _launch(tmp_path, "from pathlib import Path\ntry: Path('missing').read_text()\nexcept FileNotFoundError: pass")
    boundary = _boundary(request, tmp_path)
    supervisor = InvocationProcessSupervisor(owner=ProcessOwner(tmp_path / "owners"))  # type: ignore[arg-type]

    result = supervisor.run(request, policies=(LandlockFilesystemPolicy(),), boundary=boundary)

    if result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE:
        pytest.skip(result.detail)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert not boundary.evidence().policy_violation
