"""Tests for check_backend_prerequisites function."""

from unittest.mock import MagicMock, patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import check_backend_prerequisites
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestCheckBackendPrerequisites:
    """Test cases for check_backend_prerequisites function."""

    def test_check_backend_prerequisites_with_valid_backend_type(self):
        """Test that custom backend names work when backend_type is configured."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with custom backend that has backend_type
            config = LLMBackendConfiguration()
            config.backends["grok-4.1-fast"] = BackendConfig(
                name="grok-4.1-fast",
                model="open-router/grok-4.1-fast",
                backend_type="codex",  # Points to codex backend type
                api_key="test_key",
                base_url="https://openrouter.ai/api/v1",
            )
            mock_get_config.return_value = config

            # Mock check_codex_cli_or_fail to succeed
            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_check:
                # Call check_backend_prerequisites with custom backend
                check_backend_prerequisites(["grok-4.1-fast"])

                # Assert check_codex_cli_or_fail was called (since backend_type is codex)
                assert mock_check.call_count == 1

    def test_check_backend_prerequisites_without_backend_type(self):
        """Test that custom backend names fail when backend_type is not configured."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config without backend_type for custom backend
            config = LLMBackendConfiguration()
            config.backends["custom-backend"] = BackendConfig(
                name="custom-backend",
                model="some-model",
                # No backend_type specified
            )
            mock_get_config.return_value = config

            # Assert ClickException is raised with helpful error message
            with pytest.raises(ClickException) as exc_info:
                check_backend_prerequisites(["custom-backend"])

            error_message = str(exc_info.value.message)
            assert "Unsupported backend specified: custom-backend" in error_message
            assert "backend_type in llm_config.toml" in error_message

    def test_check_backend_prerequisites_with_invalid_backend_type(self):
        """Test that custom backend with invalid backend_type fails."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with invalid backend_type
            config = LLMBackendConfiguration()
            config.backends["my-backend"] = BackendConfig(
                name="my-backend",
                model="some-model",
                backend_type="invalid-type",  # Invalid backend type
            )
            mock_get_config.return_value = config

            # Mock check functions to ensure they're NOT called
            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex:
                with patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_gemini:
                    # Assert ClickException is raised (because invalid-type is not known)
                    with pytest.raises(ClickException) as exc_info:
                        check_backend_prerequisites(["my-backend"])

                    error_message = str(exc_info.value.message)
                    assert "Unsupported backend specified: invalid-type" in error_message

                    # Ensure no CLI check functions were called
                    assert mock_codex.call_count == 0
                    assert mock_gemini.call_count == 0

    def test_check_backend_prerequisites_with_known_backends(self):
        """Test that hardcoded backend names continue to work."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            mock_get_config.return_value = config

            # Mock all CLI check functions
            with (
                patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex,
                patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_gemini,
                patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_qwen,
                patch("src.auto_coder.cli_helpers.check_auggie_cli_or_fail") as mock_auggie,
                patch("src.auto_coder.cli_helpers.check_claude_cli_or_fail") as mock_claude,
                patch("src.auto_coder.cli_helpers.check_jules_cli_or_fail") as mock_jules,
            ):

                # Test codex
                check_backend_prerequisites(["codex"])
                assert mock_codex.call_count == 1
                mock_codex.reset_mock()

                # Test codex-mcp
                check_backend_prerequisites(["codex-mcp"])
                assert mock_codex.call_count == 1
                mock_codex.reset_mock()

                # Test gemini
                check_backend_prerequisites(["gemini"])
                assert mock_gemini.call_count == 1

                # Test qwen
                check_backend_prerequisites(["qwen"])
                assert mock_qwen.call_count == 1

                # Test auggie
                check_backend_prerequisites(["auggie"])
                assert mock_auggie.call_count == 1

                # Test claude
                check_backend_prerequisites(["claude"])
                assert mock_claude.call_count == 1

                # Test jules
                check_backend_prerequisites(["jules"])
                assert mock_jules.call_count == 1

    def test_check_backend_prerequisites_mixed_backends(self):
        """Test that mixing known and custom backends works."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with custom backend
            config = LLMBackendConfiguration()
            config.backends["my-qwen-custom"] = BackendConfig(
                name="my-qwen-custom",
                model="qwen-special",
                backend_type="qwen",  # Points to qwen backend type
            )
            mock_get_config.return_value = config

            # Mock CLI check functions
            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex, patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_qwen:

                # Test mixed backends: known codex + custom qwen-based backend
                check_backend_prerequisites(["codex", "my-qwen-custom"])

                # Assert both check functions were called
                assert mock_codex.call_count == 1
                assert mock_qwen.call_count == 1

    def test_check_backend_prerequisites_backend_not_in_config(self):
        """Test that unknown backend name without config fails appropriately."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config without the requested backend
            config = LLMBackendConfiguration()
            mock_get_config.return_value = config

            # Assert ClickException is raised
            with pytest.raises(ClickException) as exc_info:
                check_backend_prerequisites(["nonexistent-backend"])

            error_message = str(exc_info.value.message)
            assert "Unsupported backend specified: nonexistent-backend" in error_message

    def test_check_backend_prerequisites_recursive_backend_type_resolution(self):
        """Test that backend_type resolution works recursively."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with custom backend pointing to gemini
            config = LLMBackendConfiguration()
            config.backends["my-gemini-alias"] = BackendConfig(
                name="my-gemini-alias",
                model="gemini-2.5-flash",
                backend_type="gemini",
            )
            mock_get_config.return_value = config

            # Mock gemini CLI check
            with patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_gemini:
                # Call with custom backend
                check_backend_prerequisites(["my-gemini-alias"])

                # Assert gemini check was called
                assert mock_gemini.call_count == 1

    def test_check_backend_prerequisites_multiple_custom_backends(self):
        """Test multiple custom backends with different backend_types."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with multiple custom backends
            config = LLMBackendConfiguration()
            config.backends["grok-fast"] = BackendConfig(
                name="grok-fast",
                backend_type="codex",
            )
            config.backends["gemini-experimental"] = BackendConfig(
                name="gemini-experimental",
                backend_type="gemini",
            )
            config.backends["qwen-custom"] = BackendConfig(
                name="qwen-custom",
                backend_type="qwen",
            )
            mock_get_config.return_value = config

            # Mock all relevant CLI checks
            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex, patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail") as mock_gemini, patch("src.auto_coder.cli_helpers.check_qwen_cli_or_fail") as mock_qwen:

                # Call with all custom backends
                check_backend_prerequisites(["grok-fast", "gemini-experimental", "qwen-custom"])

                # Assert all relevant checks were called
                assert mock_codex.call_count == 1
                assert mock_gemini.call_count == 1
                assert mock_qwen.call_count == 1

    def test_check_backend_prerequisites_custom_claude_backend(self):
        """Test custom backend with claude backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with custom claude backend
            config = LLMBackendConfiguration()
            config.backends["my-claude"] = BackendConfig(
                name="my-claude",
                model="claude-sonnet-4",
                backend_type="claude",
            )
            mock_get_config.return_value = config

            # Mock claude CLI check
            with patch("src.auto_coder.cli_helpers.check_claude_cli_or_fail") as mock_claude:
                # Call with custom backend
                check_backend_prerequisites(["my-claude"])

                # Assert claude check was called
                assert mock_claude.call_count == 1

    def test_check_backend_prerequisites_custom_auggie_backend(self):
        """Test custom backend with auggie backend_type."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with custom auggie backend
            config = LLMBackendConfiguration()
            config.backends["my-auggie"] = BackendConfig(
                name="my-auggie",
                model="GPT-6",
                backend_type="auggie",
            )
            mock_get_config.return_value = config

            # Mock auggie CLI check
            with patch("src.auto_coder.cli_helpers.check_auggie_cli_or_fail") as mock_auggie:
                # Call with custom backend
                check_backend_prerequisites(["my-auggie"])

                # Assert auggie check was called
                assert mock_auggie.call_count == 1

    def test_check_backend_prerequisites_empty_list(self):
        """Test that empty backend list doesn't raise errors."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            mock_get_config.return_value = config

            # Should not raise any errors
            check_backend_prerequisites([])

    def test_check_backend_prerequisites_deduplication(self):
        """Test that duplicate backends in list are handled correctly."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            config = LLMBackendConfiguration()
            mock_get_config.return_value = config

            # Mock codex CLI check
            with patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail") as mock_codex:
                # Call with duplicate backends
                check_backend_prerequisites(["codex", "codex", "codex"])

                # Should be called for each occurrence (no deduplication in the function)
                assert mock_codex.call_count == 3
