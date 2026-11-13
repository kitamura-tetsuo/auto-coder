"""Real non-root Codex-policy launcher regressions, without an LLM call."""

import os
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from src.auto_coder.invocation_process_supervisor import InvocationLaunch, InvocationOutcome, InvocationProcessSupervisor, WriterState
from src.auto_coder.local_execution_boundary import EvidenceStatus, LocalExecutionBoundary
from tests.test_local_execution_boundary import _binding


def launch(tmp_path: Path, code: str, **kwargs):
    binding = _binding(tmp_path)
    binding.workspace.mkdir(exist_ok=True, parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    boundary = LocalExecutionBoundary(binding, "codex", editable=False)
    request = InvocationLaunch(binding.invocation_id, "codex", "no-edit", binding.workspace, (runtime,), sys.executable, ("-c", code), cwd=binding.workspace, **kwargs)
    return request, boundary


def test_real_nonroot_noedit_preserves_files_and_reports_direct_only(tmp_path):
    sentinel = tmp_path / "protected"
    sentinel.write_text("original")
    code = f"import pathlib; p=pathlib.Path({str(sentinel)!r});\ntry: p.write_text('corrupt')\nexcept PermissionError: print('DENIED', flush=True)\n"
    request, boundary = launch(tmp_path, code)
    runner = InvocationProcessSupervisor()
    result = runner.run(request, boundary=boundary)
    assert result.outcome is InvocationOutcome.SUCCEEDED, result
    assert result.stdout.strip() == "DENIED"
    assert sentinel.read_text() == "original"
    assert result.writer_state is WriterState.DIRECT_PROCESS_STOPPED
    assert not result.writer_complete
    assert result.direct_process_complete
    assert "unproven" in result.detail
    evidence = boundary.evidence()
    assert evidence.writer_completion is EvidenceStatus.UNKNOWN
    assert evidence.direct_process_completion is EvidenceStatus.ESTABLISHED
    assert evidence.confined_result_authorized
    assert not runner.authorize_replacement(result, controller_decision=False)
    assert runner.authorize_replacement(result, controller_decision=True)
    assert runner.run(request, boundary=boundary).outcome is InvocationOutcome.PRESTART_UNAVAILABLE


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_cancel_reaps_direct_child_without_success(tmp_path, cancel):
    cancellation = threading.Event()
    if cancel:
        cancellation.set()
    request, boundary = launch(tmp_path, "import time; time.sleep(60)", timeout_seconds=0.1, cancellation=cancellation)
    result = InvocationProcessSupervisor().run(request, boundary=boundary)
    assert result.outcome is (InvocationOutcome.CANCELLED if cancel else InvocationOutcome.TIMED_OUT)
    assert result.direct_process_complete
    assert not result.writer_complete
    assert not boundary.evidence().confined_result_authorized


def test_root_controller_rejected_before_start(tmp_path, monkeypatch):
    request, boundary = launch(tmp_path, "print(1)")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    result = InvocationProcessSupervisor().run(request, boundary=boundary)
    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert not result.direct_process_complete


def test_opencode_still_requires_cgroup_owner(tmp_path):
    request, _ = launch(tmp_path, "print(1)")
    result = InvocationProcessSupervisor().run(replace(request, backend_type="opencode"))
    assert result.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
    assert not result.direct_process_complete


def test_direct_exit_cleans_known_child_without_claiming_descendant_proof(tmp_path):
    request, boundary = launch(tmp_path, "import os,time; pid=os.fork(); print('child', pid, flush=True) if pid else time.sleep(60)")
    result = InvocationProcessSupervisor().run(request, boundary=boundary)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert result.direct_process_complete
    assert not result.writer_complete
    child_pid = int(result.stdout.strip().split()[-1])
    assert child_pid > 0
    assert not Path(f"/proc/{child_pid}").exists()
    assert boundary.evidence().writer_completion is EvidenceStatus.UNKNOWN


def test_nonroot_git_readiness_uses_current_identity(tmp_path):
    from src.auto_coder.repository_readiness import verify_worker_repository
    from tests.test_repository_readiness import _repository

    root = _repository(tmp_path / "repo")
    observed = verify_worker_repository(root, worker_uid=os.geteuid(), worker_gid=os.getegid(), environment=os.environ)
    assert observed.root == root.resolve()
    assert observed.worker_uid == os.geteuid()
    assert observed.readable_regular_files == 1


def test_concurrent_same_invocation_is_rejected_while_preparing(tmp_path):
    from src.auto_coder.invocation_process_supervisor import PolicyInstallation

    entered = threading.Event()
    release = threading.Event()

    class BlockingPolicy:
        def install(self, context):
            entered.set()
            assert release.wait(5)
            return PolicyInstallation(True, establishes_filesystem_enforcement=True)

        def close(self):
            pass

    request, _ = launch(tmp_path, "print('one-launch')")
    supervisor = InvocationProcessSupervisor(filesystem_policy_factory=BlockingPolicy)
    results = []
    thread = threading.Thread(target=lambda: results.append(supervisor.run(request)))
    thread.start()
    try:
        assert entered.wait(5)
        duplicate = supervisor.run(request)
        assert duplicate.outcome is InvocationOutcome.PRESTART_UNAVAILABLE
        assert "reserved" in duplicate.detail
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].outcome is InvocationOutcome.SUCCEEDED
    assert results[0].stdout == "one-launch\n"


def test_cleanup_only_denial_is_retained_in_boundary_and_diagnostics(tmp_path):
    from src.auto_coder.invocation_process_supervisor import PolicyInstallation

    class CleanupMonitor:
        def attach(self, process):
            self.process = process

        @property
        def root_returncode(self):
            return self.process.poll()

        def pump(self):
            return ()

        def terminate_remaining(self):
            return ("cleanup-only write denial",)

    class Policy:
        def install(self, context):
            return PolicyInstallation(True, establishes_filesystem_enforcement=True, denial_monitor=CleanupMonitor())

        def close(self):
            pass

    request, boundary = launch(tmp_path, "print('ok')")
    result = InvocationProcessSupervisor(filesystem_policy_factory=Policy).run(request, boundary=boundary)
    assert result.outcome is InvocationOutcome.SUCCEEDED
    assert "cleanup-only write denial" in result.detail
    assert boundary.evidence().policy_violation
    assert boundary.evidence().writer_completion is EvidenceStatus.UNKNOWN
