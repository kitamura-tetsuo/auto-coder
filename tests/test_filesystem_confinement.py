import os
import signal
import sys
from pathlib import Path

import pytest

from src.auto_coder.filesystem_confinement import LandlockFilesystemPolicy
from src.auto_coder.invocation_process_supervisor import InvocationLaunch, InvocationOutcome, InvocationProcessSupervisor


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


def _launch(tmp_path: Path, code: str, *, mode: str = "editable") -> InvocationLaunch:
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
