"""
Tests for Qwen client functionality.
"""

import os
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.exceptions import AutoCoderUsageLimitError
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
        out = client._run_llm_cli("hello")
        assert "ok line 1" in out and "ok line 2" in out

        # Verify qwen CLI is used
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_run_prompt_failure_nonzero(self, mock_run_command, mock_run):
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "error", 2)

        client = QwenClient()
        with pytest.raises(RuntimeError):
            client._run_llm_cli("oops")

        # Verify qwen CLI is used
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_cli_invocation_with_oauth_provider(self, mock_run_command, mock_run):
        """Test CLI invocation details when using OAuth provider (no API key)."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Client with no API key should use OAuth
        client = QwenClient(model_name="qwen3-coder-plus")

        client._run_llm_cli("test prompt")

        # Verify qwen CLI is used for OAuth
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"
        assert "-y" in args
        assert "-p" in args

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_model_name_tracking(self, mock_run_command, mock_run):
        """Test that the client tracks which model is configured."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        client = QwenClient(model_name="qwen3-coder-plus")

        # Initial model should be set
        assert client.model_name == "qwen3-coder-plus"

        # After execution, model should remain the same
        client._run_llm_cli("test")
        assert client.model_name == "qwen3-coder-plus"

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_usage_limit_detection_variations(self, mock_run_command, mock_run):
        """Test various usage limit error message formats."""
        mock_run.return_value.returncode = 0
        client = QwenClient(model_name="qwen3-coder-plus")

        # Test various rate limit messages
        test_cases = [
            ("Rate limit exceeded", True),
            ("quota exceeded", True),
            ("429 Too Many Requests", True),
            ("openai api streaming error: 429 free allocated quota exceeded.", True),
            ("openai api streaming error: 429 provider returned error", True),
            ("error: 400 model access denied.", True),
            ("normal error message", False),
        ]

        for message, expected_limit in test_cases:
            assert client._is_usage_limit(message, 429 if expected_limit else 1) == expected_limit

    @patch("subprocess.run")
    def test_init_without_cli_raises_error(self, mock_run):
        """Test that initialization fails when required CLI is not available."""

        # Mock subprocess.run to simulate qwen CLI not available
        def side_effect(cmd, **kwargs):
            if cmd == ["qwen", "--version"]:
                raise Exception("qwen CLI not found")
            return mock.Mock(returncode=0)

        mock_run.side_effect = side_effect

        with pytest.raises(RuntimeError, match="qwen CLI not available"):
            QwenClient()

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_custom_model_parameter(self, mock_run_command, mock_run):
        """Test that custom model parameter is passed through correctly."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        client = QwenClient(model_name="custom-qwen-model")
        client._run_llm_cli("test")

        # Verify custom model is used in command
        args = mock_run_command.call_args[0][0]
        # Should have model flag
        assert "-m" in args
        assert "custom-qwen-model" in args
