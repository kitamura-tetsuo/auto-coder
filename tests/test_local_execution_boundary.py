import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.exceptions import SessionWorkspaceCompatibilityError
from src.auto_coder.invocation_process_supervisor import InvocationOutcome, SupervisedInvocationResult, WriterState
from src.auto_coder.local_execution_boundary import (
    BackendOutcome,
    EvidenceStatus,
    LocalBoundaryError,
    LocalExecutionBoundary,
    bind_local_execution_boundary,
    get_current_local_execution_boundary,
)
from src.auto_coder.utils import CommandExecutor, bind_command_execution_cwd, bind_supervised_command_execution, reset_command_execution_cwd
from src.auto_coder.worktree_utils import LocalWorkspaceBinding, LocalWorkspaceOwnership


def _binding(tmp_path: Path) -> LocalWorkspaceBinding:
    caller = tmp_path / "caller"
    workspace = tmp_path / "private" / "repository"
    git_dir = caller / ".git"
    workspace.mkdir(parents=True)
    git_dir.mkdir(parents=True)
    return LocalWorkspaceBinding(
        invocation_id="invocation-1",
        caller_root=caller,
        caller_git_dir=git_dir,
        caller_common_dir=git_dir,
        initial_head="refs/heads/main",
        initial_commit="a" * 40,
        index_checksum="index",
        file_snapshot_checksum="files",
        workspace=workspace,
        ownership=LocalWorkspaceOwnership(),
    )


def test_incomplete_evidence_cannot_authorize_confined_result(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "opencode", editable=True)
    invocation_id = boundary.binding.invocation_id
    boundary.record_backend_success(invocation_id)

    with pytest.raises(LocalBoundaryError, match="lacks complete"):
        boundary.require_promotable()

    evidence = boundary.evidence()
    assert evidence.backend_outcome is BackendOutcome.SUCCEEDED
    assert evidence.filesystem_enforcement is EvidenceStatus.UNKNOWN
    assert evidence.publication_enforcement is EvidenceStatus.UNKNOWN
    assert evidence.writer_completion is EvidenceStatus.UNKNOWN
    assert evidence.violation_observation is EvidenceStatus.UNKNOWN
    assert evidence.confined_result_authorized is False


def test_complete_matching_evidence_authorizes_but_violation_is_sticky(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "muse", editable=True)
    invocation_id = boundary.binding.invocation_id
    boundary.record_backend_success(invocation_id)
    boundary.record_enforcement(
        invocation_id,
        filesystem=EvidenceStatus.ESTABLISHED,
        publication=EvidenceStatus.ESTABLISHED,
        writers=EvidenceStatus.ESTABLISHED,
        violations_observed=EvidenceStatus.ESTABLISHED,
    )
    assert boundary.require_promotable().confined_result_authorized is True
    boundary.report_policy_violation(invocation_id, "external publication denied")
    boundary.record_backend_success(invocation_id)

    with pytest.raises(LocalBoundaryError, match="lacks complete"):
        boundary.require_promotable()
    assert boundary.evidence().policy_violation is True
    assert boundary.evidence().promotable is False


def test_operational_success_does_not_require_retired_publication_certificate(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "codex", editable=True)
    invocation_id = boundary.binding.invocation_id
    boundary.record_backend_success(invocation_id)
    boundary.record_enforcement(
        invocation_id,
        filesystem=EvidenceStatus.ESTABLISHED,
        publication=EvidenceStatus.UNKNOWN,
        writers=EvidenceStatus.ESTABLISHED,
        violations_observed=EvidenceStatus.ESTABLISHED,
    )

    evidence = boundary.require_confined_result(invocation_id)
    assert evidence.confined_result_authorized is True
    assert evidence.publication_enforcement is EvidenceStatus.UNKNOWN


def test_provider_session_is_turn_scoped_metadata_not_edit_authority(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "codex", editable=False)
    invocation_id = boundary.binding.invocation_id
    boundary.record_provider_session(invocation_id, " session-1 ")
    boundary.record_backend_success(invocation_id)
    boundary.record_enforcement(
        invocation_id,
        filesystem=EvidenceStatus.ESTABLISHED,
        publication=EvidenceStatus.UNKNOWN,
        writers=EvidenceStatus.ESTABLISHED,
        violations_observed=EvidenceStatus.ESTABLISHED,
    )

    evidence = boundary.require_confined_result(invocation_id)
    assert evidence.provider_session_id == "session-1"
    assert evidence.turn_id != evidence.invocation_id
    with pytest.raises(LocalBoundaryError, match="no-edit evidence"):
        boundary.require_confined_result(invocation_id, for_edit_promotion=True)
    with pytest.raises(LocalBoundaryError, match="session identity changed"):
        boundary.record_provider_session(invocation_id, "session-2")


def test_supervised_command_uses_bound_root_and_removes_git_overrides(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "codex", editable=True)

    class RecordingSupervisor:
        def __init__(self) -> None:
            self.request = None

        def run(self, request, *, boundary):
            self.request = request
            boundary.record_filesystem_enforcement(request.invocation_id, EvidenceStatus.ESTABLISHED)
            boundary.record_writer_completion(request.invocation_id)
            boundary.record_violation_observation(request.invocation_id)
            boundary.record_backend_success(request.invocation_id)
            return SupervisedInvocationResult(
                request.invocation_id,
                InvocationOutcome.SUCCEEDED,
                WriterState.POSITIVELY_STOPPED,
                0,
                "provider output",
                "",
            )

    supervisor = RecordingSupervisor()
    environment = {"PATH": "/bin", "GIT_DIR": str(boundary.binding.caller_git_dir), "GIT_WORK_TREE": str(boundary.binding.caller_root)}
    with bind_supervised_command_execution(supervisor, boundary):  # type: ignore[arg-type]
        result = CommandExecutor.run_command(["provider", "--flag"], cwd=str(boundary.binding.workspace), env=environment, stdin_text="full prompt")

    assert result.success is True
    assert result.stdout == "provider output"
    assert supervisor.request is not None
    assert supervisor.request.cwd == boundary.binding.workspace
    assert supervisor.request.prompt == "full prompt"
    assert supervisor.request.arguments == ("--flag",)
    assert "GIT_DIR" not in supervisor.request.environment
    assert "GIT_WORK_TREE" not in supervisor.request.environment
    assert supervisor.request.protected_paths == (
        boundary.binding.caller_root,
        boundary.binding.caller_git_dir,
        boundary.binding.caller_common_dir,
    )


def test_cross_invocation_and_late_evidence_are_rejected(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "opencode", editable=True)
    with pytest.raises(LocalBoundaryError, match="different local invocation"):
        boundary.record_backend_success("another-invocation")
    boundary.close()
    with pytest.raises(LocalBoundaryError, match="evidence is closed"):
        boundary.record_backend_success(boundary.binding.invocation_id)


def test_normal_manager_return_remains_legacy_and_uncertified(tmp_path: Path, _use_real_commands: None) -> None:
    class SuccessfulClient:
        use_noedit_options = False
        model_name = "test-model"
        config_backend = SimpleNamespace(backend_type="opencode")

        def __init__(self) -> None:
            self.boundary: LocalExecutionBoundary | None = None

        def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
            self.boundary = get_current_local_execution_boundary()
            assert self.boundary is not None
            return "plausible success"

        def get_last_session_id(self) -> None:
            return None

    repository = tmp_path / "real-repository"
    repository.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("initial\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    client = SuccessfulClient()
    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = BackendManager(default_backend="alias", default_client=client, factories={"alias": lambda: client}, order=["alias"])
    token = bind_command_execution_cwd(str(repository))
    try:
        assert manager._run_llm_cli("implement") == "plausible success"
    finally:
        reset_command_execution_cwd(token)

    assert client.boundary is not None
    evidence = client.boundary.evidence()
    assert evidence.backend_outcome is BackendOutcome.SUCCEEDED
    assert evidence.writer_completion is EvidenceStatus.UNKNOWN
    assert evidence.filesystem_enforcement is EvidenceStatus.UNKNOWN
    assert evidence.publication_enforcement is EvidenceStatus.UNKNOWN
    assert evidence.confined_result_authorized is False


def test_boundary_is_invocation_local_and_rejects_nesting(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    assert get_current_local_execution_boundary() is None

    with bind_local_execution_boundary(binding, backend_type="codex", editable=False) as boundary:
        assert get_current_local_execution_boundary() is boundary
        with pytest.raises(LocalBoundaryError, match="already active"):
            with bind_local_execution_boundary(binding, backend_type="codex", editable=False):
                pass

    assert get_current_local_execution_boundary() is None


def test_boundary_rejects_workspace_aliasing_caller_authority(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    unsafe = LocalWorkspaceBinding(
        invocation_id=binding.invocation_id,
        caller_root=binding.caller_root,
        caller_git_dir=binding.caller_git_dir,
        caller_common_dir=binding.caller_common_dir,
        initial_head=binding.initial_head,
        initial_commit=binding.initial_commit,
        index_checksum=binding.index_checksum,
        file_snapshot_checksum=binding.file_snapshot_checksum,
        workspace=binding.caller_root,
        ownership=binding.ownership,
    )

    with pytest.raises(LocalBoundaryError, match="aliases caller-owned Git state"):
        LocalExecutionBoundary(unsafe, "opencode", editable=True)


def test_noedit_constructed_client_creates_read_only_boundary(tmp_path: Path) -> None:
    class NoEditClient:
        use_noedit_options = True
        model_name = "test-model"
        config_backend = SimpleNamespace(backend_type="opencode")

        def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
            boundary = get_current_local_execution_boundary()
            assert boundary is not None
            assert boundary.editable is False
            assert is_noedit is True
            return "read-only result"

        def get_last_session_id(self) -> None:
            return None

    repository = tmp_path / "repository"
    repository.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    (repository / "tracked.txt").write_text("initial\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)

    client = NoEditClient()
    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = BackendManager(default_backend="alias", default_client=client, factories={"alias": lambda: client}, order=["alias"])
    binding = _binding(tmp_path / "binding")
    with (
        patch("src.auto_coder.backend_manager.isolated_local_llm_worktree", return_value=contextlib.nullcontext()),
        patch("src.auto_coder.backend_manager.get_current_local_workspace", return_value=binding),
    ):
        token = bind_command_execution_cwd(str(repository))
        try:
            assert manager._run_llm_cli("inspect") == "read-only result"
        finally:
            reset_command_execution_cwd(token)


def test_dynamic_client_attribute_does_not_invent_noedit_mode(tmp_path: Path) -> None:
    """Mock/proxy clients without an explicit Boolean must not change mode."""
    from unittest.mock import MagicMock

    client = MagicMock(model_name="test-model")
    client.config_backend = SimpleNamespace(backend_type="opencode")
    client._run_llm_cli.return_value = "editable result"
    client.get_last_session_id.return_value = None
    binding = _binding(tmp_path)
    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = BackendManager(default_backend="alias", default_client=client, factories={"alias": lambda: client}, order=["alias"])
    with (
        patch("src.auto_coder.backend_manager.isolated_local_llm_worktree", return_value=contextlib.nullcontext()),
        patch("src.auto_coder.backend_manager.get_current_local_workspace", return_value=binding),
    ):
        assert manager._run_llm_cli("implement") == "editable result"

    client._run_llm_cli.assert_called_once_with("implement", is_noedit=False)


def test_local_continuations_are_refused_before_rebinding_session_workspace(tmp_path: Path) -> None:
    class ConcurrentClient:
        use_noedit_options = False
        model_name = "test-model"
        config_backend = SimpleNamespace(backend_type="opencode")

        def __init__(self) -> None:
            self.entered = threading.Barrier(2)
            self.calls: dict[str, tuple[bool, bool]] = {}

        def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
            boundary = get_current_local_execution_boundary()
            assert boundary is not None
            self.entered.wait(timeout=5)
            self.calls[session_id] = (is_noedit, boundary.editable)
            return session_id

        def get_last_session_id(self) -> None:
            return None

    client = ConcurrentClient()
    binding = _binding(tmp_path)
    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = BackendManager(default_backend="alias", default_client=client, factories={"alias": lambda: client}, order=["alias"])

    errors: list[BaseException] = []

    def run(session_id: str, is_noedit: bool) -> None:
        try:
            manager.continue_session(session_id, "continue", is_noedit=is_noedit)
        except BaseException as exc:
            errors.append(exc)

    with (
        patch("src.auto_coder.backend_manager.isolated_local_llm_worktree", return_value=contextlib.nullcontext()),
        patch("src.auto_coder.backend_manager.get_current_local_workspace", return_value=binding),
    ):
        first = threading.Thread(target=run, args=("read-only", True))
        second = threading.Thread(target=run, args=("editable", False))
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

    assert len(errors) == 2
    assert all(isinstance(error, SessionWorkspaceCompatibilityError) for error in errors)
    assert client.calls == {}
