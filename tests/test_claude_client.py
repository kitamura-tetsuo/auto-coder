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
        """ClaudeClient should extract session ID from 'Session ID:' label with UUID format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "Some output\nSession ID: 550e8400-e29b-41d4-a716-446655440000\nMore output"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-e29b-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_session_label(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from 'Session:' label with UUID format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "Session: a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_url_parameter(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from URL parameters with UUID format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "https://example.com/page?session_id=12345678-1234-5678-1234-567812345678"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "12345678-1234-5678-1234-567812345678"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_from_path(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from URL paths with UUID format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "Visit https://example.com/sessions/abcdef12-3456-7890-abcd-ef1234567890 for details"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "abcdef12-3456-7890-abcd-ef1234567890"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_case_insensitive(self, mock_run, mock_get_config):
        """ClaudeClient should extract session ID case-insensitively (both label and UUID)."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with uppercase UUID and label
        output = "SESSION ID: ABCDEF12-3456-7890-ABCD-EF1234567890"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "ABCDEF12-3456-7890-ABCD-EF1234567890"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_session_id_rejects_invalid_format(self, mock_run, mock_get_config):
        """ClaudeClient should NOT extract session ID that is not a valid UUID."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test that invalid format is NOT extracted
        output = "Session ID: abc123def"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

        # Test another invalid format
        output = "Session ID: session-abc_123-def"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

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
        mock_result.stdout = "Test output\nSession ID: 11111111-2222-3333-4444-555555555555"
        mock_result.stderr = ""
        mock_cmd_exec.return_value = mock_result

        client = ClaudeClient()

        # Set extra args
        client.set_extra_args(["--resume", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"])

        # Run LLM
        client._run_llm_cli("test prompt")

        # Check that extra args were used in command
        called_cmd = mock_cmd_exec.call_args[0][0]
        assert "--resume" in called_cmd
        assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in called_cmd

        # Check that extra args were cleared
        assert client._extra_args == []

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.claude_client.CommandExecutor.run_command")
    def test_session_id_extracted_from_output(self, mock_cmd_exec, mock_run, mock_get_config):
        """ClaudeClient should extract session ID from command output (UUID format)."""
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
        mock_result.stdout = "Response output\nSession ID: 99999999-8888-7777-6666-555544443333"
        mock_result.stderr = ""
        mock_cmd_exec.return_value = mock_result

        client = ClaudeClient()

        # Run LLM
        client._run_llm_cli("test prompt")

        # Check that session ID was extracted
        assert client.get_last_session_id() == "99999999-8888-7777-6666-555544443333"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_options_loaded_from_config(self, mock_run, mock_get_config):
        """ClaudeClient should load options from backend config."""
        mock_run.return_value.returncode = 0

        # Mock config with options
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_backend.options = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        mock_backend.options_for_noedit = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Should have loaded options from config
        assert client.options == ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        assert client.options_for_noedit == ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_options_fallback_to_empty_when_not_configured(self, mock_run, mock_get_config):
        """ClaudeClient should fall back to empty options when not configured."""
        mock_run.return_value.returncode = 0

        # Mock config without options
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_backend.options = []
        mock_backend.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Should have empty options when not configured
        assert client.options == []
        assert client.options_for_noedit == []

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    @patch("src.auto_coder.claude_client.CommandExecutor.run_command")
    def test_options_used_in_run_llm_cli(self, mock_cmd_exec, mock_run, mock_get_config):
        """ClaudeClient should use configured options in _run_llm_cli."""
        mock_run.return_value.returncode = 0

        # Mock config with options
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_backend.options = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        mock_backend.options_for_noedit = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        # Mock command executor
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Test output"
        mock_result.stderr = ""
        mock_cmd_exec.return_value = mock_result

        client = ClaudeClient()

        # Run LLM
        client._run_llm_cli("test prompt")

        # Check that configured options were used in command
        called_cmd = mock_cmd_exec.call_args[0][0]
        assert "--dangerously-skip-permissions" in called_cmd
        assert "--allow-dangerously-skip-permissions" in called_cmd
        assert "--model" in called_cmd
        assert "sonnet" in called_cmd


class TestClaudeClientSessionExtraction:
    """Test cases for session ID extraction in ClaudeClient."""

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_valid_uuid_from_session_id_label(self, mock_run, mock_get_config):
        """Test extraction from 'Session ID: <uuid>' format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "Session ID: 550e8400-e29b-41d4-a716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-e29b-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_valid_uuid_from_session_label(self, mock_run, mock_get_config):
        """Test extraction from 'Session: <uuid>' format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "Session: 550e8400-e29b-41d4-a716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-e29b-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_valid_uuid_from_url_parameter(self, mock_run, mock_get_config):
        """Test extraction from 'session_id=<uuid>' format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "session_id=550e8400-e29b-41d4-a716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-e29b-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_valid_uuid_from_path(self, mock_run, mock_get_config):
        """Test extraction from '/sessions/<uuid>' format."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with valid UUID
        output = "/sessions/550e8400-e29b-41d4-a716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-e29b-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_reject_invalid_session_id_abc123(self, mock_run, mock_get_config):
        """Test that 'abc123' is NOT extracted."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test that invalid format is NOT extracted
        output = "Session ID: abc123"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_reject_invalid_session_id_short(self, mock_run, mock_get_config):
        """Test that short strings are NOT extracted."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test that short strings are NOT extracted
        output = "Session ID: 12345678"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

        # Test another short string
        output = "Session ID: a1b2c3d4"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_reject_invalid_session_id_malformed_uuid(self, mock_run, mock_get_config):
        """Test that malformed UUIDs are NOT extracted."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test malformed UUID - wrong segment lengths
        output = "Session ID: 550e8400-e29b-41d4-a716-44665544000"  # 31 chars, should be 32
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

        # Test malformed UUID - invalid characters
        output = "Session ID: 550e8400-e29b-41d4-a716-44665544000g"  # contains 'g'
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

        # Test malformed UUID - wrong dashes
        output = "Session ID: 550e8400-e29b-41d4-a716-44665544000"  # missing dash
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_uppercase_uuid(self, mock_run, mock_get_config):
        """Test that uppercase UUIDs are extracted correctly."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with uppercase UUID
        output = "Session ID: 550E8400-E29B-41D4-A716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550E8400-E29B-41D4-A716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_lowercase_uuid(self, mock_run, mock_get_config):
        """Test that lowercase UUIDs are extracted correctly."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with lowercase UUID
        output = "Session ID: 550e8400-e29b-41d4-a716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-e29b-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_extract_mixed_case_uuid(self, mock_run, mock_get_config):
        """Test that mixed-case UUIDs are extracted correctly."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test extraction with mixed-case UUID
        output = "Session ID: 550e8400-E29B-41d4-a716-446655440000"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() == "550e8400-E29B-41d4-a716-446655440000"

    @patch("src.auto_coder.claude_client.get_llm_config")
    @patch("subprocess.run")
    def test_no_session_id_in_output(self, mock_run, mock_get_config):
        """Test that _last_session_id remains None when no UUID is present."""
        mock_run.return_value.returncode = 0

        # Mock config
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "sonnet"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        client = ClaudeClient()

        # Test with empty output
        client._extract_and_store_session_id("")
        assert client.get_last_session_id() is None

        # Test with no session ID in output
        output = "This is just regular output with no session information"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None

        # Test with text that looks similar but isn't a valid UUID
        output = "Session ID: not-a-uuid-format"
        client._extract_and_store_session_id(output)
        assert client.get_last_session_id() is None
