import subprocess
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.muse_client import MuseClient
from src.auto_coder.utils import CommandResult


@pytest.fixture
def mock_subprocess_popen():
    with patch("subprocess.Popen") as mock_popen:
        mock_process = MagicMock()
        mock_process.stdout = ["output line 1\n", "output line 2\n"]
        mock_process.wait.return_value = 0
        mock_popen.return_value = mock_process
        yield mock_popen


@pytest.fixture
def mock_git():
    with patch("src.auto_coder.muse_client.CommandExecutor") as mock_executor, patch("src.auto_coder.muse_client.get_current_branch") as mock_branch:

        mock_branch.return_value = "main"

        mock_cmd = MagicMock()
        mock_cmd.run_command.return_value = CommandResult(success=True, stdout="abcdef123456\n", stderr="", returncode=0)
        mock_executor.return_value = mock_cmd

        yield {"executor": mock_executor, "cmd": mock_cmd, "branch": mock_branch}


def test_muse_client_success(mock_subprocess_popen, mock_git):
    """Test successful muse client execution without git mutation."""
    client = MuseClient()

    result = client._run_llm_cli("test prompt")

    assert result == "output line 1\noutput line 2"

    # Assert muse exec is called properly
    mock_subprocess_popen.assert_called_once()
    called_args = mock_subprocess_popen.call_args[0][0]
    assert called_args[0] == "muse"
    assert called_args[1] == "exec"
    assert called_args[-1] == "test prompt"

    # Assert reset was not called
    mock_git["cmd"].run_command.assert_called()
    for call in mock_git["cmd"].run_command.call_args_list:
        args = call[0][0]
        if args and len(args) > 1:
            assert args[1] != "reset"


def test_muse_client_noedit_leaves_state_unchanged(mock_subprocess_popen, mock_git):
    """Test no-edit operations perform reset and clean."""
    client = MuseClient()

    client._run_llm_cli("test prompt", is_noedit=True)

    # Assert reset and clean were called
    calls = mock_git["cmd"].run_command.call_args_list
    assert any(c[0][0] == ["git", "reset", "--hard", "abcdef123456"] for c in calls)
    assert any(c[0][0] == ["git", "clean", "-fd"] for c in calls)


def test_muse_client_git_mutation_rejected(mock_subprocess_popen, mock_git):
    """Test standard run raises RuntimeError if Git mutates."""
    client = MuseClient()

    # Simulate a mutation by altering what run_command returns on the second call (post_run_head)
    original_run_command = mock_git["cmd"].run_command

    def side_effect(*args, **kwargs):
        if args[0] == ["git", "rev-parse", "HEAD"]:
            if side_effect.calls == 0:
                side_effect.calls += 1
                return CommandResult(success=True, stdout="abcdef123456\n", stderr="", returncode=0)
            else:
                return CommandResult(success=True, stdout="mutated987654\n", stderr="", returncode=0)
        return original_run_command(*args, **kwargs)

    side_effect.calls = 0
    mock_git["cmd"].run_command = MagicMock(side_effect=side_effect)

    with pytest.raises(RuntimeError, match="Muse CLI violated Git invariants"):
        client._run_llm_cli("test prompt")

    # Check that it tried to revert
    calls = mock_git["cmd"].run_command.call_args_list
    assert any(c[0][0] == ["git", "reset", "--hard", "abcdef123456"] for c in calls)


def test_muse_client_usage_limit(mock_subprocess_popen, mock_git):
    """Test usage limit detection."""
    mock_process = mock_subprocess_popen.return_value
    mock_process.stdout = ["rate limit reached\n"]
    mock_process.wait.return_value = 1

    client = MuseClient()

    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("test prompt")
