"""
Tests for Codex client functionality.
"""

import json
from pathlib import Path
from unittest.mock import patch

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
        with patch.object(CodexClient, "__init__", lambda self, model_name=None: None):
            client = CodexClient()
            from src.auto_coder.llm_output_logger import LLMOutputLogger

            client.output_logger = LLMOutputLogger(log_path=log_file, enabled=True)
            client.model_name = "codex"  # Set model_name since we mocked __init__

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
        with patch.object(CodexClient, "__init__", lambda self, model_name=None: None):
            client = CodexClient()
            from src.auto_coder.llm_output_logger import LLMOutputLogger

            client.output_logger = LLMOutputLogger(log_path=log_file, enabled=True)
            client.model_name = "codex"

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
