"""Tests for check_backend_prerequisites function."""

from unittest.mock import patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import check_backend_prerequisites
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestCheckBackendPrerequisites:
    """Test cases for check_backend_prerequisites function."""

    def test_known_backend_codex(self):
        """Test that known backend 'codex' triggers codex CLI check."""
        with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_check:
            check_backend_prerequisites(["codex"])
            assert mock_check.call_count == 1

    def test_known_backend_codex_mcp(self):
        """Test that known backend 'codex-mcp' triggers codex CLI check."""
        with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_check:
            check_backend_prerequisites(["codex-mcp"])
            assert mock_check.call_count == 1

    def test_known_backend_gemini(self):
        """Test that known backend 'gemini' triggers gemini CLI check."""
        with patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_check:
            check_backend_prerequisites(["gemini"])
            assert mock_check.call_count == 1

    def test_known_backend_qwen(self):
        """Test that known backend 'qwen' triggers qwen CLI check."""
        with patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_check:
            check_backend_prerequisites(["qwen"])
            assert mock_check.call_count == 1

    def test_known_backend_auggie(self):
        """Test that known backend 'auggie' triggers auggie CLI check."""
        with patch("src.auto_coder.cli_helpers.check_auggie_cli_or_fail") as mock_check:
            check_backend_prerequisites(["auggie"])
            assert mock_check.call_count == 1

    def test_known_backend_claude(self):
        """Test that known backend 'claude' triggers claude CLI check."""
        with patch("src.auto_coder.cli_helpers.check_claude_cli_or_fail") as mock_check:
            check_backend_prerequisites(["claude"])
            assert mock_check.call_count == 1

    def test_multiple_known_backends(self):
        """Test multiple known backends trigger respective CLI checks."""
        with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex, patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_gemini, patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_qwen:
            check_backend_prerequisites(["codex", "gemini", "qwen"])
            assert mock_codex.call_count == 1
            assert mock_gemini.call_count == 1
            assert mock_qwen.call_count == 1

    def test_custom_backend_with_valid_backend_type(self):
        """Test that custom backend with valid backend_type triggers appropriate CLI check."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with custom backend
            config = LLMBackendConfiguration()
            config.backends["my-custom-qwen"] = BackendConfig(
                name="my-custom-qwen",
                backend_type="qwen",
                model="qwen3-coder-plus",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_check:
                check_backend_prerequisites(["my-custom-qwen"])
                # Should recursively check the backend_type
                assert mock_check.call_count == 1

    def test_custom_backend_with_codex_backend_type(self):
        """Test custom backend with codex backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            config.backends["my-openrouter"] = BackendConfig(
                name="my-openrouter",
                backend_type="codex",
                model="openrouter/grok-4.1",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_check:
                check_backend_prerequisites(["my-openrouter"])
                assert mock_check.call_count == 1

    def test_custom_backend_with_gemini_backend_type(self):
        """Test custom backend with gemini backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            config.backends["my-gemini-alias"] = BackendConfig(
                name="my-gemini-alias",
                backend_type="gemini",
                model="gemini-2.5-flash",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_check:
                check_backend_prerequisites(["my-gemini-alias"])
                assert mock_check.call_count == 1

    def test_custom_backend_without_backend_type_raises_error(self):
        """Test that custom backend without backend_type raises ClickException."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            config.backends["my-invalid-backend"] = BackendConfig(
                name="my-invalid-backend",
                backend_type=None,  # No backend_type specified
                model="some-model",
            )
            mock_get_config.return_value = config

            with pytest.raises(ClickException) as exc_info:
                check_backend_prerequisites(["my-invalid-backend"])

            assert "Unsupported backend specified: my-invalid-backend" in str(exc_info.value)
            assert "configure backend_type in llm_config.toml" in str(exc_info.value)

    def test_unknown_backend_not_in_config_raises_error(self):
        """Test that completely unknown backend raises ClickException."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            mock_get_config.return_value = config

            with pytest.raises(ClickException) as exc_info:
                check_backend_prerequisites(["totally-unknown-backend"])

            assert "Unsupported backend specified: totally-unknown-backend" in str(exc_info.value)
            assert "configure backend_type in llm_config.toml" in str(exc_info.value)

    def test_mixed_known_and_custom_backends(self):
        """Test mixture of known backends and custom backends with backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            config.backends["my-qwen"] = BackendConfig(
                name="my-qwen",
                backend_type="qwen",
                model="qwen3-coder-plus",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex, patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_qwen:
                check_backend_prerequisites(["codex", "my-qwen"])
                assert mock_codex.call_count == 1
                assert mock_qwen.call_count == 1

    def test_empty_backends_list(self):
        """Test that empty backends list doesn't raise error."""
        check_backend_prerequisites([])
        # Should complete without error

    def test_recursive_check_does_not_cause_infinite_loop(self):
        """Test that recursive checking doesn't cause infinite loops."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            # Create a custom backend pointing to a known backend
            config.backends["alias1"] = BackendConfig(
                name="alias1",
                backend_type="qwen",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_check:
                check_backend_prerequisites(["alias1"])
                # Should resolve to qwen and check once
                assert mock_check.call_count == 1

    def test_custom_backend_with_claude_backend_type(self):
        """Test custom backend with claude backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            config.backends["my-claude-alias"] = BackendConfig(
                name="my-claude-alias",
                backend_type="claude",
                model="sonnet",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_claude_cli_or_fail") as mock_check:
                check_backend_prerequisites(["my-claude-alias"])
                assert mock_check.call_count == 1

    def test_custom_backend_with_auggie_backend_type(self):
        """Test custom backend with auggie backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            config.backends["my-auggie-alias"] = BackendConfig(
                name="my-auggie-alias",
                backend_type="auggie",
                model="GPT-5",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.check_auggie_cli_or_fail") as mock_check:
                check_backend_prerequisites(["my-auggie-alias"])
                assert mock_check.call_count == 1
