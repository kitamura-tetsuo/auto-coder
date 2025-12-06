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
    @patch("src.auto_coder.codex_client.get_llm_config")
    def test_init_checks_cli(self, mock_get_config, mock_run):
        """CodexClient should check codex --version at init."""
        mock_run.return_value.returncode = 0

        # Mock config to return default codex model
        mock_backend = MagicMock()
        mock_backend.model = "codex"
        mock_backend.options_for_noedit = []
        mock_backend.options = []
        mock_get_config.return_value.get_backend_config.return_value = mock_backend

        client = CodexClient()
        assert client.model_name == "codex"
        # Verify output_logger is initialized
        assert client.output_logger is not None

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_exec_success(self, mock_run_command, mock_run):
        """codex should stream and aggregate output successfully."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "line1\nline2\n", "", 0)

        client = CodexClient()
        output = client._run_llm_cli("hello world")
        assert "line1" in output and "line2" in output

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_run_exec_includes_extra_args(self, mock_run_command, mock_run):
        """Extra args (e.g., resume flags) should be passed to codex CLI."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "ok", "", 0)

        client = CodexClient()
        client.set_extra_args(["--resume", "abc123"])

        _ = client._run_llm_cli("hello world")

        called_cmd = mock_run_command.call_args[0][0]
        assert called_cmd[-1] == "hello world"
        assert called_cmd[-3:-1] == ["--resume", "abc123"]

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
    def test_usage_limit_error_with_json_marker(self, mock_run_command, mock_run):
        """Usage limit detection should handle JSON markers with partial matches."""
        mock_run.return_value.returncode = 0
        json_log_line = "2025-12-07 00:06:46.125 | INFO | auto_coder/claude_client.py:161 in _run_llm_cli - " '{"type":"result","subtype":"error","is_error":true,"result":"Limit reached - resets soon"}'
        mock_run_command.return_value = CommandResult(False, json_log_line, "", 1)

        client = CodexClient()
        client.usage_markers = [{"type": "result", "is_error": True, "result": "Limit reached"}]

        with pytest.raises(AutoCoderUsageLimitError):
            client._run_llm_cli("hello world")

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    @patch("builtins.print")
    @patch("src.auto_coder.codex_client.get_llm_config")
    def test_json_logging_on_success(self, mock_get_config, mock_print, mock_run_command, mock_run, tmp_path):
        """Verify that JSON logging is called on successful execution."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config
        mock_backend = MagicMock()
        mock_backend.model = "codex"
        mock_backend.options_for_noedit = []
        mock_backend.options = []
        mock_get_config.return_value.get_backend_config.return_value = mock_backend

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
    @patch("src.auto_coder.codex_client.get_llm_config")
    def test_json_logging_on_error(self, mock_get_config, mock_print, mock_run_command, mock_run, tmp_path):
        """Verify that JSON logging is called on error."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(False, "", "boom", 1)

        # Mock config
        mock_backend = MagicMock()
        mock_backend.model = "codex"
        mock_backend.options_for_noedit = []
        mock_backend.options = []
        mock_get_config.return_value.get_backend_config.return_value = mock_backend

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
    @patch("src.auto_coder.codex_client.get_llm_config")
    def test_user_friendly_summary_on_success(self, mock_get_config, mock_print, mock_run_command, mock_run):
        """Verify that user-friendly summary is printed on success."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config
        mock_backend = MagicMock()
        mock_backend.model = "codex"
        mock_backend.options_for_noedit = []
        mock_backend.options = []
        mock_get_config.return_value.get_backend_config.return_value = mock_backend

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
    @patch("src.auto_coder.codex_client.get_llm_config")
    def test_no_duplicate_logs(self, mock_get_config, mock_print, mock_run_command, mock_run, tmp_path):
        """Verify that multiple calls do not create duplicate log entries."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config
        mock_backend = MagicMock()
        mock_backend.model = "codex"
        mock_backend.options_for_noedit = []
        mock_backend.options = []
        mock_get_config.return_value.get_backend_config.return_value = mock_backend

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
    def test_config_options_passed_to_cli(self, mock_run_command, mock_run):
        """CodexClient should pass configured options to codex CLI."""
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

            # Verify command structure
            assert cmd[0] == "codex"
            assert "test prompt" in cmd

        # Verify command does not contain "exec" subcommand
        assert "exec" not in cmd

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
    @patch("src.auto_coder.codex_client.get_llm_config")
    def test_model_name_default_with_custom_config(self, mock_get_config, mock_run):
        """CodexClient should use default model when model_name is None with custom config."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_backend = MagicMock()
        mock_backend.model = "codex"
        mock_backend.options_for_noedit = []
        mock_backend.options = []
        mock_get_config.return_value.get_backend_config.return_value = mock_backend

        client = CodexClient(
            api_key="test_api_key",
            base_url="https://test.example.com",
        )
        assert client.model_name == "codex"  # Default when model_name is None
        assert client.api_key == "test_api_key"
        assert client.base_url == "https://test.example.com"

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
    def test_init_with_options(self, mock_run):
        """CodexClient should load options from backend config."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0

        # Mock the config with options
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "codex"
        mock_backend_config.options = ["--dangerously-bypass-approvals-and-sandbox", "--custom-flag"]
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")
            assert client.options == ["--dangerously-bypass-approvals-and-sandbox", "--custom-flag"]

    @patch("subprocess.run")
    def test_init_without_options(self, mock_run):
        """CodexClient should have empty options when not configured."""
        mock_run.return_value.returncode = 0

        # Mock the config without options
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "codex"
        mock_backend_config.options = []
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")
            assert client.options == []

    @patch("subprocess.run")
    def test_init_falls_back_to_codex_config_options(self, mock_run):
        """CodexClient should fall back to codex backend config for options."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0

        # Mock the config
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "codex"
        mock_backend_config.options = ["--dangerously-bypass-approvals-and-sandbox"]
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient()  # No backend_name specified
            assert client.options == ["--dangerously-bypass-approvals-and-sandbox"]

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_options_passed_to_cli(self, mock_run_command, mock_run):
        """Configured options should be passed to codex CLI."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config with options
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "codex"
        mock_backend_config.options = ["--dangerously-bypass-approvals-and-sandbox", "--custom-flag"]
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        # Mock replace_placeholders to return the options
        mock_backend_config.replace_placeholders.return_value = {"options": ["--dangerously-bypass-approvals-and-sandbox", "--custom-flag"], "options_for_noedit": [], "options_for_resume": []}
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify options are in the command
            assert "--dangerously-bypass-approvals-and-sandbox" in cmd
            assert "--custom-flag" in cmd

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_empty_options_not_passed_to_cli(self, mock_run_command, mock_run):
        """Empty options list should not affect the command."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        client = CodexClient()
        # Ensure options is empty list
        client.options = []

        # Execute the method
        output = client._run_llm_cli("test prompt")

        # Verify CommandExecutor.run_command was called
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify command structure is correct (no extra elements)
        assert cmd[0] == "codex"
        # Next should be either an option or the prompt
        assert cmd[-1] == "test prompt"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_options_order_in_command(self, mock_run_command, mock_run):
        """Options should appear in the correct position in the command."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config with options
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "custom-model"
        mock_backend_config.options = ["--dangerously-bypass-approvals-and-sandbox", "--model", "custom-model"]
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        # Mock replace_placeholders to return the options
        mock_backend_config.replace_placeholders.return_value = {"options": ["--dangerously-bypass-approvals-and-sandbox", "--model", "custom-model"], "options_for_noedit": [], "options_for_resume": []}
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify command structure: codex -> options -> prompt
            assert cmd[0] == "codex"
            # Options should be in the middle
            assert "--dangerously-bypass-approvals-and-sandbox" in cmd
            # Prompt should be at the end
            assert cmd[-1] == "test prompt"
            # Command should not contain "exec"
            assert "exec" not in cmd

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_placeholder_replacement_with_model_name(self, mock_run_command, mock_run):
        """CodexClient should replace [model_name] placeholders in options."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config with placeholders
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "gpt-5.1-codex-max"
        mock_backend_config.options = ["--model", "[model_name]", "--json"]
        mock_backend_config.options_for_noedit = ["--model", "[model_name]"]
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        # Mock replace_placeholders to replace placeholders
        mock_backend_config.replace_placeholders.return_value = {"options": ["--model", "gpt-5.1-codex-max", "--json"], "options_for_noedit": ["--model", "gpt-5.1-codex-max"], "options_for_resume": []}
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify placeholders were replaced
            assert "gpt-5.1-codex-max" in cmd
            assert "[model_name]" not in " ".join(cmd)

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_placeholder_replacement_in_noedit_options(self, mock_run_command, mock_run):
        """CodexClient should replace placeholders in options_for_noedit."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config with placeholders
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "gpt-5.1-codex-max"
        mock_backend_config.options = ["--dangerously-bypass-approvals-and-sandbox"]
        mock_backend_config.options_for_noedit = ["--model", "[model_name]", "--json"]
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        # Mock replace_placeholders to replace placeholders
        mock_backend_config.replace_placeholders.return_value = {"options": ["--dangerously-bypass-approvals-and-sandbox"], "options_for_noedit": ["--model", "gpt-5.1-codex-max", "--json"], "options_for_resume": []}
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")

            # Execute the method with is_noedit=True
            output = client._run_llm_cli("test prompt", is_noedit=True)

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify placeholders in noedit options were replaced
            assert "gpt-5.1-codex-max" in cmd
            assert "[model_name]" not in " ".join(cmd)
            # Should use noedit options, not regular options
            assert "--json" in cmd

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_no_placeholder_replacement_when_no_config_backend(self, mock_run_command, mock_run):
        """CodexClient should handle missing config_backend gracefully."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Create client with no config_backend
        client = CodexClient()
        client.config_backend = None
        client.options = ["--json", "--dangerously-bypass-approvals-and-sandbox"]

        # Execute the method
        output = client._run_llm_cli("test prompt")

        # Verify CommandExecutor.run_command was called
        assert mock_run_command.called
        cmd = mock_run_command.call_args[0][0]

        # Verify command structure is correct
        assert cmd[0] == "codex"
        assert "--json" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd[-1] == "test prompt"

    @patch("subprocess.run")
    @patch("src.auto_coder.codex_client.CommandExecutor.run_command")
    def test_all_options_from_config_no_hardcoded_flags(self, mock_run_command, mock_run):
        """CodexClient should use only config-based options without hardcoded flags."""
        from unittest.mock import MagicMock

        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "test output\n", "", 0)

        # Mock config with complete options
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "custom-model"
        mock_backend_config.model_provider = "openrouter"
        mock_backend_config.options = ["--model", "custom-model", "-c", "model_provider=openrouter", "--json", "--dangerously-bypass-approvals-and-sandbox"]
        mock_backend_config.api_key = None
        mock_backend_config.base_url = None
        mock_backend_config.openai_api_key = None
        mock_backend_config.openai_base_url = None
        # Mock replace_placeholders to return the options
        mock_backend_config.replace_placeholders.return_value = {"options": ["--model", "custom-model", "-c", "model_provider=openrouter", "--json", "--dangerously-bypass-approvals-and-sandbox"], "options_for_noedit": [], "options_for_resume": []}
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.codex_client.get_llm_config", return_value=mock_config):
            client = CodexClient(backend_name="codex")

            # Execute the method
            output = client._run_llm_cli("test prompt")

            # Verify CommandExecutor.run_command was called
            assert mock_run_command.called
            cmd = mock_run_command.call_args[0][0]

            # Verify command structure
            assert cmd[0] == "codex"
            # All options should come from config (including model and model_provider)
            assert "custom-model" in cmd
            assert "model_provider=openrouter" in cmd
            assert "--json" in cmd
            assert "--dangerously-bypass-approvals-and-sandbox" in cmd
            # Command should not contain "exec" subcommand
            assert "exec" not in cmd
