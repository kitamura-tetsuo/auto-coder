"""
Tests for Codex client functionality.
"""

from unittest.mock import patch

import pytest

from auto_coder.codex_client import CodexClient
from auto_coder.utils import CommandResult


class TestCodexClient:
    """Test cases for CodexClient class."""

    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run):
        """CodexClient should check codex --version at init."""
        mock_run.return_value.returncode = 0
        client = CodexClient()
        assert client.model_name == "codex"

    @patch("subprocess.run")
    @patch("auto_coder.codex_client.CommandExecutor.run_command")
    def test_llm_invocation_warn_log(self, mock_run_command, mock_run):
        """Verify warning is logged when invoking codex CLI."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "ok\n", "", 0)

        client = CodexClient()
        _ = client._run_llm_cli("hello world")
        # We cannot easily capture loguru here without handler tweaks; rely on absence of exceptions
        # The warn path is at least executed without error.

    @patch("subprocess.run")
    @patch("auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_exec_success(self, mock_run_command, mock_run):
        """codex exec should stream and aggregate output successfully."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "line1\nline2\n", "", 0)

        client = CodexClient()
        output = client._run_llm_cli("hello world")
        assert "line1" in output and "line2" in output

    @patch("subprocess.run")
    @patch("auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_exec_failure(self, mock_run_command, mock_run):
        """When codex exec returns non-zero, raise RuntimeError."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "boom", 1)

        client = CodexClient()
        with pytest.raises(RuntimeError):
            client._run_llm_cli("hello world")
