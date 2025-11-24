"""
Tests for Qwen client functionality.
"""

import os
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.backend_provider_manager import BackendProviderManager, BackendProviderMetadata, ProviderMetadata
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

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_provider_rotation_on_usage_limit(self, mock_run_command, mock_run):
        """Test that Qwen client rotates through providers when usage limits are hit."""
        mock_run.return_value.returncode = 0

        # First call raises usage limit, second succeeds
        mock_run_command.side_effect = [
            CommandResult(True, "Rate limit exceeded", "", 429),  # Usage limit
            CommandResult(True, "Success response", "", 0),  # Success
        ]

        client = QwenClient(model_name="qwen3-coder-plus")

        # Mock _is_usage_limit to return True for first call
        with patch.object(client, "_is_usage_limit", side_effect=[True, False]):
            # First invocation hits limit and should raise
            with pytest.raises(AutoCoderUsageLimitError):
                client._run_qwen_cli("test prompt")

            # Second invocation should succeed with next provider
            result = client._run_qwen_cli("test prompt 2")
            assert "Success response" in result

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_provider_rotation_clears_env_between_attempts(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_env_vars_cleared_after_invocation(self, mock_run_command, mock_run):
        """Test that provider-specific environment variables are cleaned up after execution."""
        mock_run.return_value.returncode = 0

        # Track environment variables that were passed
        env_captured = {}

        def capture_env(cmd, stream_output=False, env=None, **kwargs):
            if env:
                env_captured.update(env)
            return CommandResult(True, "response", "", 0)

        mock_run_command.side_effect = capture_env

        client = QwenClient(
            model_name="qwen3-coder-plus",
            openai_api_key="test-key",
            openai_base_url="https://test.openai.com",
        )

        # Run prompt with custom API key/base URL (should use codex CLI)
        result = client._run_qwen_cli("test")
        assert "response" in result

        # Verify that OPENAI_API_KEY and OPENAI_BASE_URL were set in the env
        assert "OPENAI_API_KEY" in env_captured
        assert env_captured["OPENAI_API_KEY"] == "test-key"
        assert "OPENAI_BASE_URL" in env_captured
        assert env_captured["OPENAI_BASE_URL"] == "https://test.openai.com"

        # Verify that env vars are not in the global os.environ
        # (they should only be passed to the subprocess, not persist globally)
        # Note: The client passes a copy of environment, so global env should be clean
        # We can't directly test the cleanup from within the test, but the implementation
        # ensures env vars are only set for the subprocess call via TemporaryEnvironment

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_cli_invocation_with_openai_provider(self, mock_run_command, mock_run):
        """Test CLI invocation details when using OpenAI-compatible provider."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        client = QwenClient(
            model_name="qwen3-coder-plus",
            openai_api_key="custom-key",
            openai_base_url="https://custom.openai.com",
        )

        client._run_qwen_cli("test prompt")

        # Verify codex CLI is used for OpenAI-compatible providers
        args = mock_run_command.call_args[0][0]
        assert args[0] == "codex"
        assert "exec" in args
        assert "workspace-write" in args
        assert "--dangerously-bypass-approvals-and-sandbox" in args

        # Check environment variables were set correctly
        env = mock_run_command.call_args[1]["env"]
        assert env["OPENAI_API_KEY"] == "custom-key"
        assert env["OPENAI_BASE_URL"] == "https://custom.openai.com"

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_cli_invocation_with_oauth_provider(self, mock_run_command, mock_run):
        """Test CLI invocation details when using OAuth provider (no API key)."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Client with no API key should use OAuth
        client = QwenClient(model_name="qwen3-coder-plus")

        client._run_qwen_cli("test prompt")

        # Verify qwen CLI is used for OAuth
        args = mock_run_command.call_args[0][0]
        assert args[0] == "qwen"
        assert "-y" in args
        assert "-p" in args

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_multiple_providers_exhaustion(self, mock_run_command, mock_run):
        """Test behavior when all providers exhaust their usage limits."""
        mock_run.return_value.returncode = 0
        mock_run_command.side_effect = [
            CommandResult(True, "Rate limit 1", "", 429),
            CommandResult(True, "Rate limit 2", "", 429),
            CommandResult(True, "Rate limit 3", "", 429),
        ]

        client = QwenClient(
            model_name="qwen3-coder-plus",
            openai_api_key="test-key",
            openai_base_url="https://test.openai.com",
        )

        # Mock _is_usage_limit to always return True
        with patch.object(client, "_is_usage_limit", return_value=True):
            with pytest.raises(AutoCoderUsageLimitError) as exc_info:
                client._run_qwen_cli("test prompt")

            # Should aggregate error messages from all providers
            assert "All Qwen providers reached usage limits" in str(exc_info.value)

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_preserve_existing_env_flag(self, mock_run_command, mock_run):
        """Test that preserve_existing_env flag controls environment variable handling."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        # Set some environment variables
        os.environ["OPENAI_API_KEY"] = "existing-key"
        os.environ["OPENAI_BASE_URL"] = "https://existing.com"

        try:
            # Test with preserve_existing_env=True
            client = QwenClient(
                model_name="qwen3-coder-plus",
                openai_api_key="new-key",
                openai_base_url="https://new.com",
                preserve_existing_env=True,
            )

            client._run_qwen_cli("test")

            # Environment should include existing values
            env = mock_run_command.call_args[1]["env"]
            assert env["OPENAI_API_KEY"] == "new-key"  # New value should still be set

            # Test with preserve_existing_env=False (default)
            client2 = QwenClient(
                model_name="qwen3-coder-plus",
                openai_api_key="newer-key",
                openai_base_url="https://newer.com",
                preserve_existing_env=False,
            )

            client2._run_qwen_cli("test")

            # Environment should have newer values
            env2 = mock_run_command.call_args[1]["env"]
            assert env2["OPENAI_API_KEY"] == "newer-key"
        finally:
            # Clean up
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)

    @patch("subprocess.run")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_model_name_tracking(self, mock_run_command, mock_run):
        """Test that the client tracks which model was actually used."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "response", "", 0)

        client = QwenClient(model_name="qwen3-coder-plus")

        # Initial model should be set
        assert client.model_name == "qwen3-coder-plus"
        assert client._last_used_model == "qwen3-coder-plus"

        # After execution, model should still be tracked
        client._run_qwen_cli("test")
        assert client._last_used_model == "qwen3-coder-plus"

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
            result = CommandResult(True, message, "", 429 if expected_limit else 1)
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
        client._run_qwen_cli("test")

        # Verify custom model is used in command
        args = mock_run_command.call_args[0][0]
        # Should have model flag
        assert any("-m" in str(arg) or "model=" in str(arg) for arg in args)


class TestQwenClientWithProviderManager:
    """Test Qwen client integration with BackendProviderManager."""

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_provider_manager_uppercase_settings_as_env_vars(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_provider_rotation_hits_usage_limit_triggers_fallback(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_env_vars_cleared_between_provider_invocations(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_cli_invocation_with_provider_manager_settings(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_oauth_provider_invocation_after_openai_provider(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_provider_manager_all_providers_exhausted(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_env_vars_remain_clean_after_error(self, mock_run_command, mock_run):
        pass

    @pytest.mark.skip(reason="Tests deprecated QwenClient internal provider rotation - provider rotation now handled by BackendManager")
    @patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
    def test_preserve_existing_env_with_provider_manager(self, mock_run_command, mock_run):
        pass
