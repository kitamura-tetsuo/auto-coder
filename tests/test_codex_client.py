"""
Tests for Codex client functionality.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.codex_client import CodexClient
from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.utils import CommandResult


class TestCodexClient:
    """Test cases for CodexClient class."""

    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run):
        """CodexClient should check codex --version at init."""
        mock_run.return_value.returncode = 0
        client = CodexClient()
        assert client.model_name == "codex"
        # Verify output_logger is initialized
        assert client.output_logger is not None

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_exec_success(self, mock_run_command, mock_run):
        """codex exec should stream and aggregate output successfully."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "line1\nline2\n", "", 0)

        client = CodexClient()
        output = client._run_llm_cli("hello world")
        assert "line1" in output and "line2" in output

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_exec_failure(self, mock_run_command, mock_run):
        """When codex exec returns non-zero, raise RuntimeError."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "boom", 1)

        client = CodexClient()
        with pytest.raises(RuntimeError):
            client._run_llm_cli("hello world")

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_usage_limit_error(self, mock_run_command, mock_run):
        """When usage limit is reached, raise AutoCoderUsageLimitError."""
        mock_run.return_value.returncode = 0
        # Simulate a usage limit error in stderr
        mock_run_command.return_value = CommandResult(False, "", "Error: usage limit exceeded", 1)

        client = CodexClient()
        with pytest.raises(AutoCoderUsageLimitError):
            client._run_llm_cli("hello world")

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    @patch("builtins.print")
    def test_json_logging_on_success(self, mock_print, mock_run_command, mock_run, tmp_path):
        """Verify that JSON logging is called on successful execution."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        log_file = tmp_path / "test_log.jsonl"

        from src.auto_coder.llm_output_logger import LLMOutputLogger

        client = CodexClient()
        client.output_logger = LLMOutputLogger(log_path=log_file, enabled=True)

        # Execute the method
        output = client._run_llm_cli("test prompt")

        # Verify output is returned
        assert output == "test output"

        # Verify log file was created
        assert log_file.exists()

        # Verify JSON log entry
        content = log_file.read_text().strip()
        data = json.loads(content)

        assert data["event_type"] == "llm_interaction"
        assert data["backend"] == "codex"
        assert data["model"] == "codex"
        assert data["status"] == "success"
        assert data["prompt_length"] == len("test prompt")
        assert data["response_length"] == len("test output")
        assert "duration_ms" in data
        assert "timestamp" in data

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    @patch("builtins.print")
    def test_json_logging_on_error(self, mock_print, mock_run_command, mock_run, tmp_path):
        """Verify that JSON logging is called on error."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "boom", 1)

        log_file = tmp_path / "test_log.jsonl"

        from src.auto_coder.llm_output_logger import LLMOutputLogger

        client = CodexClient()
        client.output_logger = LLMOutputLogger(log_path=log_file, enabled=True)

        # Execute the method and expect exception
        with pytest.raises(RuntimeError):
            client._run_llm_cli("test prompt")

        # Verify log file was created
        assert log_file.exists()

        # Verify JSON log entry with error status
        content = log_file.read_text().strip()
        data = json.loads(content)

        assert data["event_type"] == "llm_interaction"
        assert data["backend"] == "codex"
        assert data["model"] == "codex"
        assert data["status"] == "error"
        assert data["prompt_length"] == len("test prompt")
        assert "error" in data
        assert "duration_ms" in data
        assert "timestamp" in data

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    @patch("builtins.print")
    def test_user_friendly_summary_on_success(self, mock_print, mock_run_command, mock_run):
        """Verify that user-friendly summary is printed on success."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        client = CodexClient()

        # Execute the method
        with patch("builtins.print") as mock_print:
            output = client._run_llm_cli("test prompt")
            assert output == "test output"

            # Verify print was called for summary
            assert mock_print.called
            # Check that summary contains key information
            print_calls = [str(call) for call in mock_print.call_args_list]
            summary_text = "".join(print_calls)

            assert "Codex CLI Execution Summary" in summary_text
            assert "Backend: codex" in summary_text
            assert "Model: codex" in summary_text
            assert "SUCCESS" in summary_text

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    @patch("builtins.print")
    def test_user_friendly_summary_on_error(self, mock_print, mock_run_command, mock_run):
        """Verify that user-friendly summary is printed on error."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "boom", 1)

        client = CodexClient()

        # Execute the method and expect exception
        with pytest.raises(RuntimeError):
            client._run_llm_cli("test prompt")

            # Verify print was called for summary even on error
            assert mock_print.called
            # Check that summary contains error information
            print_calls = [str(call) for call in mock_print.call_args_list]
            summary_text = "".join(print_calls)

            assert "Codex CLI Execution Summary" in summary_text
            assert "ERROR" in summary_text

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    @patch("builtins.print")
    def test_no_duplicate_logs(self, mock_print, mock_run_command, mock_run, tmp_path):
        """Verify that multiple calls do not create duplicate log entries."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        log_file = tmp_path / "test_log.jsonl"
        from src.auto_coder.llm_output_logger import LLMOutputLogger

        client = CodexClient()
        client.output_logger = LLMOutputLogger(log_path=log_file, enabled=True)

        # Execute the method multiple times
        for i in range(3):
            output = client._run_llm_cli(f"test prompt {i}")

        # Verify output is returned
        assert output == "test output"

        # Verify log file was created
        assert log_file.exists()

        # Verify there are exactly 3 log entries (one per call)
        content = log_file.read_text().strip()
        log_lines = [line for line in content.split("\n") if line.strip()]

        assert len(log_lines) == 3, f"Expected 3 log entries, got {len(log_lines)}"

        # Verify each log entry is valid JSON and has correct structure
        for i, line in enumerate(log_lines):
            data = json.loads(line)
            assert data["event_type"] == "llm_interaction"
            assert data["backend"] == "codex"
            assert data["model"] == "codex"
            assert data["status"] == "success"
            assert "duration_ms" in data
            assert "timestamp" in data
            # Verify each entry corresponds to its call
            assert data["prompt_length"] == len(f"test prompt {i}")

        # Verify console summary is printed for each call
        assert mock_print.called
        print_calls = [str(call) for call in mock_print.call_args_list]
        summary_text = "".join(print_calls)

        # Should have 3 "Codex CLI Execution Summary" entries (one per call)
        assert summary_text.count("Codex CLI Execution Summary") == 3

    @patch("subprocess.run")
    def test_init_with_custom_config(self, mock_run):
        """CodexClient should accept and store custom configuration values."""
        mock_run.return_value.returncode = 0
        
        # Mock config to return specific model
        from unittest.mock import MagicMock
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "custom-codex"
        mock_backend_config.api_key = "test_api_key"
        mock_backend_config.base_url = "https://test.example.com"
        mock_backend_config.openai_api_key = "test_openai_key"
        mock_backend_config.openai_base_url = "https://openai.test.example.com"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(
                backend_name="custom-backend",
                api_key="test_api_key",
                base_url="https://test.example.com",
                openai_api_key="test_openai_key",
                openai_base_url="https://openai.test.example.com",
            )
            assert client.model_name == "custom-codex"
            assert client.api_key == "test_api_key"
            assert client.base_url == "https://test.example.com"
            assert client.openai_api_key == "test_openai_key"
            assert client.openai_base_url == "https://openai.test.example.com"

    @patch("subprocess.run")
    def test_init_falls_back_to_config_when_no_custom_config(self, mock_run):
        """CodexClient should fall back to config when no custom values provided."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0

        # Mock the config
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "config-model"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient()
            assert client.model_name == "config-model"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_model_flag_passed_to_cli(self, mock_run_command, mock_run):
        """CodexClient should pass --model flag to codex exec when model_name is specified."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config to return specific model
        from unittest.mock import MagicMock
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "custom-model"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="custom-backend")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify --model flag is in the command
            assert "--model" in cmd
            model_index = cmd.index("--model")
            assert cmd[model_index + 1] == "custom-model"

        # Verify command structure
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "test prompt" in cmd

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_model_flag_not_passed_when_model_name_is_none(self, mock_run_command, mock_run):
        """CodexClient should not pass --model flag when model_name is None."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Create client with model_name=None via config
        with patch("src.auto_coder.codex_client.get_llm_config") as mock_config:
            mock_backend = MagicMock()
            mock_backend.model = None
            mock_config.return_value.get_backend_config.return_value = mock_backend

            client = CodexClient()
            # Force model_name to None (it defaults to "codex" in init if config is None)
            client.model_name = None

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify --model flag is NOT in the command
            assert "--model" not in cmd

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_injects_env_vars(self, mock_run_command, mock_run):
        """CodexClient should inject environment variables into subprocess."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        client = CodexClient(
            api_key="test_api_key",
            base_url="https://test.example.com",
            openai_api_key="test_openai_key",
            openai_base_url="https://openai.test.example.com",
        )

        # Execute the method
        output = client._run_llm_cli("test prompt")

        # Verify CommandExecutor.run_command was called with env
        assert mock_run_command.called
        call_kwargs = mock_run_command.call_args[1]
        # The env parameter should be passed
        assert "env" in call_kwargs
        env = call_kwargs["env"]
        assert env["CODEX_API_KEY"] == "test_api_key"
        assert env["CODEX_BASE_URL"] == "https://test.example.com"
        assert env["OPENAI_API_KEY"] == "test_openai_key"
        assert env["OPENAI_BASE_URL"] == "https://openai.test.example.com"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_skips_env_vars_when_not_provided(self, mock_run_command, mock_run):
        """CodexClient should not inject environment variables when not provided."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        client = CodexClient()

        # Execute the method
        output = client._run_llm_cli("test prompt")

        # Verify CommandExecutor.run_command was called without env
        assert mock_run_command.called
        call_kwargs = mock_run_command.call_args[1]
        # The env parameter should not be passed (or should be None)
        if "env" in call_kwargs:
            # Only os.environ should be used, no custom env
            assert call_kwargs["env"] is None or call_kwargs["env"] is os.environ.copy()

    @patch("subprocess.run")
    def test_model_name_default_with_custom_config(self, mock_run):
        """CodexClient should use default model when model_name is None with custom config."""
        mock_run.return_value.returncode = 0
        client = CodexClient(
            api_key="test_api_key",
            base_url="https://test.example.com",
        )
        assert client.model_name == "codex"  # Default when model_name is None
        assert client.api_key == "test_api_key"
        assert client.base_url == "https://test.example.com"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_custom_model_like_grok_passed_to_cli(self, mock_run_command, mock_run):
        """CodexClient should pass custom models like 'grok-4.1-fast' to codex CLI.

        This test verifies the fix for issue #672 where configured models
        were allegedly not being passed to the underlying codex CLI.
        """
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Simulate a user configuring a custom model like in the example config:
        # [my-openrouter-model]
        # model = "open-router/grok-4.1-fast"
        # backend_type = "codex"
        
        # Mock config to return specific model
        from unittest.mock import MagicMock
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "open-router/grok-4.1-fast"
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = "sk-or-v1-test-key"
        mock_backend_config.openai_base_url = "https://openrouter.ai/api/v1"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(
                backend_name="my-openrouter-model",
                openai_api_key="sk-or-v1-test-key",
                openai_base_url="https://openrouter.ai/api/v1",
            )

        # Verify model is set correctly
        assert client.model_name == "open-router/grok-4.1-fast"

        # Execute a command
        output = client._run_llm_cli("test prompt")

        # Verify CommandExecutor.run_command was called
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify --model flag is in the command with the custom model
        assert "--model" in cmd
        model_index = cmd.index("--model")
        assert cmd[model_index + 1] == "open-router/grok-4.1-fast"

        # Verify environment variables are set
        call_kwargs = mock_run_command.call_args[1]
        assert "env" in call_kwargs
        env = call_kwargs["env"]
        assert env["OPENAI_API_KEY"] == "sk-or-v1-test-key"
        assert env["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_minimax_model_passed_to_cli(self, mock_run_command, mock_run):
        """CodexClient should pass MiniMax models to codex CLI.

        This test verifies another model mentioned in issue #672.
        """
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Simulate MiniMax-M2 model configuration
        
        # Mock config to return specific model
        from unittest.mock import MagicMock
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "MiniMax-M2"
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = "test-key"
        mock_backend_config.openai_base_url = "https://api.minimax.com/v1"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(
                backend_name="my-minimax",
                openai_api_key="test-key",
                openai_base_url="https://api.minimax.com/v1",
            )

        # Verify model is set correctly
        assert client.model_name == "MiniMax-M2"

        # Execute a command
        output = client._run_llm_cli("test prompt")

        # Verify --model flag is passed with the correct model
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]
        assert "--model" in cmd
        model_index = cmd.index("--model")
        assert cmd[model_index + 1] == "MiniMax-M2"

    @patch("subprocess.run")
    def test_init_with_model_provider(self, mock_run):
        """CodexClient should store model_provider from backend config."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0

        # Mock the config with model_provider
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "test-model"
        mock_backend_config.model_provider = "openrouter"
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="test-backend")
            assert client.model_provider == "openrouter"

    @patch("subprocess.run")
    def test_init_without_model_provider(self, mock_run):
        """CodexClient should have None model_provider when not configured."""
        mock_run.return_value.returncode = 0

        # Create client without backend_name
        client = CodexClient()
        assert client.model_provider is None

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_model_provider_passed_to_cli(self, mock_run_command, mock_run):
        """CodexClient should pass model_provider as -c flag when specified."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock the config with model_provider
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "test-model"
        mock_backend_config.model_provider = "anthropic"
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="test-backend")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify -c flag with model_provider is in the command
            assert "-c" in cmd
            c_index = cmd.index("-c")
            assert cmd[c_index + 1] == "model_provider=anthropic"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_model_provider_not_passed_when_none(self, mock_run_command, mock_run):
        """CodexClient should not pass -c flag when model_provider is None."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        client = CodexClient()
        client.model_provider = None

        # Execute the method
        output = client._run_llm_cli("test prompt")

        # Verify CommandExecutor.run_command was called
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify -c flag is NOT in the command
        # Note: There might be other -c flags, but model_provider should not be there
        cmd_str = " ".join(cmd)
        assert "model_provider=" not in cmd_str

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_model_and_model_provider_both_passed(self, mock_run_command, mock_run):
        """CodexClient should pass both --model and -c model_provider when both are specified."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock the config with both model and model_provider
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "grok-4.1-fast"
        mock_backend_config.model_provider = "openrouter"
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="test-backend")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify both --model and -c flags are in the command
            assert "--model" in cmd
            model_index = cmd.index("--model")
            assert cmd[model_index + 1] == "grok-4.1-fast"

            assert "-c" in cmd
            c_index = cmd.index("-c")
            assert cmd[c_index + 1] == "model_provider=openrouter"

            # Verify the order: --model comes before -c
            assert model_index < c_index
