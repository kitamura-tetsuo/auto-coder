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
