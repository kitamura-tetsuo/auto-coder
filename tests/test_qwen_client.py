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


def make_backend_mock(
    model: str = "qwen3-coder-plus",
    api_key: str | None = None,
    base_url: str | None = None,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
    options: list[str] | None = None,
    options_for_noedit: list[str] | None = None,
    usage_markers: list[str] | None = None,
):
    backend = MagicMock()
    backend.model = model
    backend.api_key = api_key
    backend.base_url = base_url
    backend.openai_api_key = openai_api_key
    backend.openai_base_url = openai_base_url
    backend.options = options or []
    backend.options_for_noedit = options_for_noedit or []
    backend.usage_markers = usage_markers or []
    return backend


class TestQwenClient:
    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run, mock_get_config):
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock()
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()
        assert client.model_name.startswith("qwen")

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_run_prompt_success(self, mock_run_command, mock_run, mock_get_config):
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "ok line 1\nok line 2\n", "", 0)

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock()
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()
        out = client._run_llm_cli("hello")
        assert "ok line 1" in out and "ok line 2" in out

        # Verify qwen CLI is used
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_run_prompt_failure_nonzero(self, mock_run_command, mock_run, mock_get_config):
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "error", 2)

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock()
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()
        with pytest.raises(RuntimeError):
            client._run_llm_cli("oops")

        # Verify qwen CLI is used
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_cli_invocation_with_oauth_provider(self, mock_run_command, mock_run, mock_get_config):
        """Test CLI invocation details when using OAuth provider (no API key)."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock(api_key=None)
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        # Client with no API key should use OAuth
        client = QwenClient()

        client._run_llm_cli("test prompt")

        # Verify qwen CLI is used for OAuth
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"
        assert "-y" in args
        assert "-p" in args

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_model_name_tracking(self, mock_run_command, mock_run, mock_get_config):
        """Test that the client tracks which model is configured."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock()
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()

        # Initial model should be set
        assert client.model_name == "qwen3-coder-plus"

        # After execution, model should remain the same
        client._run_llm_cli("test")
        assert client.model_name == "qwen3-coder-plus"

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_usage_limit_detection_variations(self, mock_run_command, mock_run, mock_get_config):
        """Test various usage limit error message formats."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock()
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()

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

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    def test_init_without_cli_raises_error(self, mock_run, mock_get_config):
        """Test that initialization fails when required CLI is not available."""

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock()
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        # Mock subprocess.run to simulate qwen CLI not available
        def side_effect(cmd, **kwargs):
            if cmd == ["qwen", "--version"]:
                raise Exception("qwen CLI not found")
            return mock.Mock(returncode=0)

        mock_run.side_effect = side_effect

        with pytest.raises(RuntimeError, match="qwen CLI not available"):
            QwenClient()

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_custom_model_parameter(self, mock_run_command, mock_run, mock_get_config):
        """Test that custom model parameter is passed through correctly."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock(model="custom-qwen-model")
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()
        client._run_llm_cli("test")

        # Verify custom model is used in command
        args = mock_run_command.call_args[0][0]
        # Should have model flag
        assert "-m" in args
        assert "custom-qwen-model" in args

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_options_for_noedit_used_when_present(self, mock_run_command, mock_run, mock_get_config):
        """Ensure options_for_noedit overrides general options for CLI invocation."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        mock_config = MagicMock()
        mock_backend = make_backend_mock(options=["--general"], options_for_noedit=["--noedit-flag"])
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()
        client._run_llm_cli("test prompt")

        args = mock_run_command.call_args[0][0]
        assert "--noedit-flag" in args
        assert "--general" not in args

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_options_for_noedit_falls_back_to_options(self, mock_run_command, mock_run, mock_get_config):
        """When options_for_noedit is empty, general options should be used."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        mock_config = MagicMock()
        mock_backend = make_backend_mock(options=["--general"], options_for_noedit=[])
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()
        client._run_llm_cli("prompt")

        args = mock_run_command.call_args[0][0]
        assert "--general" in args

    @patch("src.auto_coder.qwen_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_cli_invocation_with_openrouter_config(self, mock_run_command, mock_run, mock_get_config):
        """Test that API key and base URL are passed as environment variables."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Mock config
        mock_config = MagicMock()
        mock_backend = make_backend_mock(
            api_key="qwen-key",
            base_url="https://qwen.example.com",
            openai_api_key="openai-key",
            openai_base_url="https://openai.example.com",
        )
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = QwenClient()

        client._run_llm_cli("test prompt")

        # Verify environment variables are correctly set for the subprocess
        _, kwargs = mock_run_command.call_args
        env = kwargs.get("env", {})

        assert env.get("QWEN_API_KEY") == "qwen-key"
        assert env.get("QWEN_BASE_URL") == "https://qwen.example.com"
        assert env.get("OPENAI_API_KEY") == "openai-key"
        assert env.get("OPENAI_BASE_URL") == "https://openai.example.com"
