"""
Tests for Qwen client functionality.
"""

from unittest.mock import patch

import pytest

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


class TestQwenClient:
    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run):
        mock_run.return_value.returncode = 0
        client = QwenClient()
        assert client.model_name.startswith("qwen")

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_run_prompt_success(self, mock_run_command, mock_run):
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "ok line 1\nok line 2\n", "", 0)

        client = QwenClient(model_name="qwen3-coder-plus")
        out = client._run_qwen_cli("hello")
        assert "ok line 1" in out and "ok line 2" in out

        # Verify qwen CLI is used (OAuth, no API key)
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_run_prompt_failure_nonzero(self, mock_run_command, mock_run):
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "error", 2)

        client = QwenClient()
        with pytest.raises(RuntimeError):
            client._run_qwen_cli("oops")

        # Verify qwen CLI is used (OAuth, no API key)
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"
