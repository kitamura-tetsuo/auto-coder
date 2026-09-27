import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.local_execution_boundary import (
    LocalBoundaryError,
    LocalExecutionBoundary,
    bind_local_execution_boundary,
    get_current_local_execution_boundary,
)
from src.auto_coder.utils import bind_command_execution_cwd, reset_command_execution_cwd
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


def test_boundary_requires_settled_writers_before_promotion(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "opencode", editable=True)

    with pytest.raises(LocalBoundaryError, match="writer lifetime is not settled"):
        boundary.require_promotable()

    boundary.settle_writers()
    evidence = boundary.require_promotable()
    assert evidence.invocation_id == "invocation-1"
    assert evidence.backend_type == "opencode"
    assert evidence.editable is True
    assert evidence.promotable is True


def test_policy_violation_is_sticky_after_writer_settlement(tmp_path: Path) -> None:
    boundary = LocalExecutionBoundary(_binding(tmp_path), "muse", editable=True)
    boundary.report_policy_violation("external publication denied")
    boundary.settle_writers()

    with pytest.raises(LocalBoundaryError, match="external publication denied"):
        boundary.require_promotable()
    assert boundary.evidence().policy_violation is True
    assert boundary.evidence().promotable is False


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
