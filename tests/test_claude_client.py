"""
Tests for Claude client functionality.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.claude_client import ClaudeClient


class TestClaudeClient:
    """Test cases for ClaudeClient class."""

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run, mock_get_config):
        """ClaudeClient should check claude --version at init."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()
        assert client.model_name == "sonnet"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_init_with_model_name(self, mock_run, mock_get_config):
        """ClaudeClient should use provided model name from config."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "opus"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()
        assert client.model_name == "opus"
        assert client.conflict_model == "sonnet"

    @patch("subprocess.run")
    def test_init_with_backend_name(self, mock_run):
        """ClaudeClient should use config for provided backend name."""
        mock_run.return_value.returncode = 0

        # Mock the config
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "custom-sonnet"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.claude_client.get_llm_config", return_value=mock_config):
            client = ClaudeClient(backend_name="custom-claude")
            assert client.model_name == "custom-sonnet"

    @patch("subprocess.run")
    def test_init_falls_back_to_default_claude_config(self, mock_run):
        """ClaudeClient should fall back to default claude config when no backend_name."""
        mock_run.return_value.returncode = 0

        # Mock the config
        mock_config = MagicMock()
        mock_backend_config = MagicMock()
        mock_backend_config.model = "default-model"
        mock_config.get_backend_config.return_value = mock_backend_config

        with patch("src.auto_coder.claude_client.get_llm_config", return_value=mock_config):
            client = ClaudeClient()
            # Should use "claude" backend config
            mock_config.get_backend_config.assert_called_with("claude")
            assert client.model_name == "default-model"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_switch_to_conflict_model(self, mock_run, mock_get_config):
        """ClaudeClient should switch to conflict model."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "opus"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Default should be opus
        assert client.model_name == "opus"

        # Switch to conflict model (sonnet)
        client.switch_to_conflict_model()
        assert client.model_name == "sonnet"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_switch_back_to_default_model(self, mock_run, mock_get_config):
        """ClaudeClient should switch back to default model."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "opus"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Switch to conflict model
        client.switch_to_conflict_model()
        assert client.model_name == "sonnet"

        # Switch back to default
        client.switch_to_default_model()
        assert client.model_name == "opus"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_escape_prompt(self, mock_run, mock_get_config):
        """ClaudeClient should escape @ in prompts."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        escaped = client._escape_prompt("Hello @world")
        assert escaped == "Hello \\@world"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_escape_prompt_trims_whitespace(self, mock_run, mock_get_config):
        """ClaudeClient should trim whitespace from prompts."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        escaped = client._escape_prompt("  Hello world  ")
        assert escaped == "Hello world"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_usage_markers_from_config(self, mock_run, mock_get_config):
        """ClaudeClient should use configured usage_markers from backend config."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_backend.usage_markers = ["custom marker 1", "custom marker 2"]
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Should have loaded usage_markers from config
        assert client.usage_markers == ["custom marker 1", "custom marker 2"]

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_usage_markers_fallback_to_empty(self, mock_run, mock_get_config):
        """ClaudeClient should fall back to empty usage_markers when not configured."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_backend.usage_markers = []
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Should have empty usage_markers when not configured
        assert client.usage_markers == []

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_set_extra_args(self, mock_run, mock_get_config):
        """ClaudeClient should store extra args for next execution."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Set extra args
        client.set_extra_args(["--resume", "session123"])
        assert client._extra_args == ["--resume", "session123"]

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_get_last_session_id_returns_none_initially(self, mock_run, mock_get_config):
        """ClaudeClient should return None for session ID initially."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Initially should be None
        assert client.get_last_session_id() is None

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_session_id_label(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from 'Session ID:' label."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction
        output = "Some output\nSession ID: abc123def\nMore output"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "abc123def"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_session_label(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from 'Session:' label."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction
        output = "Session: xyz789"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "xyz789"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_url_parameter(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from URL parameters."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction
        output = "https://example.com/page?session_id=param123"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "param123"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_path(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from URL paths."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction
        output = "Visit https://example.com/sessions/path456 for details"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "path456"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_case_insensitive(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID case-insensitively."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with uppercase
        output = "SESSION ID: CASE123"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "CASE123"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_with_dashes_and_underscores(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID with dashes and underscores."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction
        output = "Session ID: session-abc_123-def"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "session-abc_123-def"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.claude_client.CommandExecutor.run_command")
    def test_extra_args_used_in_run_llm_cli(self, mock_cmd_exec, mock_run, mock_get_config):
        """ClaudeClient should use and clear extra args in _run_llm_cli."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        # Mock command executor
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Test output\nSession ID: test123"
        mock_result.stderr = ""
        mock_cmd_exec.return_value = mock_result

        client = ClaudeClient()

        # Set extra args
        client.set_extra_args(["--resume", "session999"])

        # Run LLM
        client._run_llm_cli("test prompt")

        # Check that extra args were used in command
        called_cmd = mock_cmd_exec.call_args[0][0]
        assert "--resume" in called_cmd
        assert "session999" in called_cmd

        # Check that extra args were cleared
        assert client._extra_args == []

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.claude_client.CommandExecutor.run_command")
    def test_session_id_extracted_from_output(self, mock_cmd_exec, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from command output."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        # Mock command executor
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Response output\nSession ID: extracted789"
        mock_result.stderr = ""
        mock_cmd_exec.return_value = mock_result

        client = ClaudeClient()

        # Run LLM
        client._run_llm_cli("test prompt")

        # Check that session ID was extracted
        assert client.get_last_session_id() == "extracted789"
