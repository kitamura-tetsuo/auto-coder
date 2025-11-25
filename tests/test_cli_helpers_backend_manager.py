"""Tests for cli_helpers backend manager functions."""

from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.cli_helpers import build_backend_manager, build_backend_manager_from_config
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


class TestBuildBackendManager:
    """Test cases for build_backend_manager function."""

    def test_build_backend_manager_with_qwen_options(self):
        """Test that QwenClient receives options from config."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config
            config = LLMBackendConfiguration()
            config.backends["qwen"] = BackendConfig(
                name="qwen",
                model="qwen3-coder-plus",
                openai_api_key="test_key",
                openai_base_url="https://api.example.com",
                options=["-o", "stream", "false", "--debug"],
            )
            config.backends["gemini"] = BackendConfig(
                name="gemini",
                model="gemini-2.5-pro",
                api_key="gemini_key",
            )
            mock_get_config.return_value = config

            # Mock QwenClient to capture the options passed to it
            with patch("src.auto_coder.cli_helpers.QwenClient") as mock_qwen_class:
                mock_qwen_instance = MagicMock()
                mock_qwen_class.return_value = mock_qwen_instance

                # Mock GeminiClient
                with patch("src.auto_coder.cli_helpers.GeminiClient") as mock_gemini_class:
                    mock_gemini_instance = MagicMock()
                    mock_gemini_class.return_value = mock_gemini_instance

                    # Call build_backend_manager with only qwen backend
                    manager = build_backend_manager(
                        selected_backends=["qwen"],
                        primary_backend="qwen",
                        models={"qwen": "qwen3-coder-plus"},
                        enable_graphrag=False,
                    )

                    # Verify QwenClient was called with the options from config
                    assert mock_qwen_class.call_count == 1
                    call_kwargs = mock_qwen_class.call_args.kwargs

                    # Check that options were passed
                    assert "options" in call_kwargs
                    assert call_kwargs["options"] == ["-o", "stream", "false", "--debug"]

                    # Verify other parameters
                    assert call_kwargs["model_name"] == "qwen3-coder-plus"

    def test_build_backend_manager_with_alias_and_backend_type(self):
        """Test that custom aliases with backend_type are handled correctly."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with a custom alias
            config = LLMBackendConfiguration()
            # Create a custom backend that uses qwen as its backend_type
            config.backends["my-qwen-alias"] = BackendConfig(
                name="my-qwen-alias",
                model="qwen3-coder-plus",
                backend_type="qwen",  # This alias points to qwen
                options=["-o", "yolo", "true"],
            )
            # Also set up the base qwen config for API keys
            config.backends["qwen"] = BackendConfig(
                name="qwen",
                openai_api_key="test_key",
                openai_base_url="https://api.example.com",
            )
            mock_get_config.return_value = config

            # Mock QwenClient
            with patch("src.auto_coder.cli_helpers.QwenClient") as mock_qwen_class:
                mock_qwen_instance = MagicMock()
                mock_qwen_class.return_value = mock_qwen_instance

                # Mock GeminiClient
                with patch("src.auto_coder.cli_helpers.GeminiClient") as mock_gemini_class:
                    mock_gemini_instance = MagicMock()
                    mock_gemini_class.return_value = mock_gemini_instance

                    # Call build_backend_manager with the alias
                    manager = build_backend_manager(
                        selected_backends=["my-qwen-alias"],
                        primary_backend="my-qwen-alias",
                        models={"my-qwen-alias": "qwen3-coder-plus"},
                        enable_graphrag=False,
                    )

                    # Verify QwenClient was called with the options from the alias config
                    assert mock_qwen_class.call_count == 1
                    call_kwargs = mock_qwen_class.call_args.kwargs

                    # Check that options were passed from the alias
                    assert "options" in call_kwargs
                    assert call_kwargs["options"] == ["-o", "yolo", "true"]

                    # Verify the model
                    assert call_kwargs["model_name"] == "qwen3-coder-plus"

    def test_build_backend_manager_without_options(self):
        """Test that QwenClient works without options (empty list or None)."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config without options
            config = LLMBackendConfiguration()
            config.backends["qwen"] = BackendConfig(
                name="qwen",
                model="qwen3-coder-plus",
                # No options specified
            )
            mock_get_config.return_value = config

            # Mock QwenClient
            with patch("src.auto_coder.cli_helpers.QwenClient") as mock_qwen_class:
                mock_qwen_instance = MagicMock()
                mock_qwen_class.return_value = mock_qwen_instance

                # Mock GeminiClient
                with patch("src.auto_coder.cli_helpers.GeminiClient") as mock_gemini_class:
                    mock_gemini_instance = MagicMock()
                    mock_gemini_class.return_value = mock_gemini_instance

                    # Call build_backend_manager
                    manager = build_backend_manager(
                        selected_backends=["qwen"],
                        primary_backend="qwen",
                        models={"qwen": "qwen3-coder-plus"},
                        enable_graphrag=False,
                    )

                    # Verify QwenClient was called
                    assert mock_qwen_class.call_count == 1
                    call_kwargs = mock_qwen_class.call_args.kwargs

                    # Check that options is either None or empty list
                    assert call_kwargs["options"] is None or call_kwargs["options"] == []

    def test_build_backend_manager_with_codex_alias(self):
        """Test that CodexClient receives api_key and base_url from an alias config."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with a custom alias for codex
            config = LLMBackendConfiguration()
            config.backends["my-openrouter-model"] = BackendConfig(
                name="my-openrouter-model",
                model="open-router/grok-4.1-fast",
                backend_type="codex",
                api_key="or-key",
                base_url="https://openrouter.ai/api/v1",
            )
            mock_get_config.return_value = config

            # Mock CodexClient
            with patch("src.auto_coder.cli_helpers.CodexClient") as mock_codex_class:
                mock_codex_instance = MagicMock()
                mock_codex_class.return_value = mock_codex_instance

                # Call build_backend_manager with the alias
                manager = build_backend_manager(
                    selected_backends=["my-openrouter-model"],
                    primary_backend="my-openrouter-model",
                    models={"my-openrouter-model": "open-router/grok-4.1-fast"},
                    enable_graphrag=False,
                )

                # Verify CodexClient was called with the options from the alias config
                assert mock_codex_class.call_count == 1
                call_kwargs = mock_codex_class.call_args.kwargs

                # Check that api_key and base_url were passed from the alias
                assert "api_key" in call_kwargs
                assert call_kwargs["api_key"] == "or-key"
                assert "base_url" in call_kwargs
                assert call_kwargs["base_url"] == "https://openrouter.ai/api/v1"
                assert call_kwargs["model_name"] == "open-router/grok-4.1-fast"
                assert call_kwargs["backend_name"] == "my-openrouter-model"

    def test_build_backend_manager_with_default_codex_config(self):
        """Test that CodexClient receives config for the default 'codex' backend."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config for the default codex backend
            config = LLMBackendConfiguration()
            config.backends["codex"] = BackendConfig(
                name="codex",
                model="some-codex-model",
                backend_type="codex",
                api_key="default-codex-key",
                base_url="https://default.codex.com",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.CodexClient") as mock_codex_class:
                mock_codex_instance = MagicMock()
                mock_codex_class.return_value = mock_codex_instance

                # Call build_backend_manager with the default codex backend
                build_backend_manager(
                    selected_backends=["codex"],
                    primary_backend="codex",
                    models={"codex": "some-codex-model"},
                    enable_graphrag=False,
                )

                assert mock_codex_class.call_count == 1
                call_kwargs = mock_codex_class.call_args.kwargs

                # Verify api_key and base_url were passed
                assert call_kwargs["api_key"] == "default-codex-key"
                assert call_kwargs["base_url"] == "https://default.codex.com"
                assert call_kwargs["backend_name"] == "codex"

    def test_build_backend_manager_with_default_qwen_config(self):
        """Test that QwenClient receives config for the default 'qwen' backend."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config for the default qwen backend
            config = LLMBackendConfiguration()
            config.backends["qwen"] = BackendConfig(
                name="qwen",
                model="qwen-model",
                backend_type="qwen",
                api_key="default-qwen-key",
                base_url="https://default.qwen.com",
            )
            mock_get_config.return_value = config

            with patch("src.auto_coder.cli_helpers.QwenClient") as mock_qwen_class:
                mock_qwen_instance = MagicMock()
                mock_qwen_class.return_value = mock_qwen_instance

                # Call build_backend_manager with the default qwen backend
                build_backend_manager(
                    selected_backends=["qwen"],
                    primary_backend="qwen",
                    models={"qwen": "qwen-model"},
                    enable_graphrag=False,
                )

                assert mock_qwen_class.call_count == 1
                call_kwargs = mock_qwen_class.call_args.kwargs

                # Verify api_key and base_url were passed
                assert call_kwargs["api_key"] == "default-qwen-key"
                assert call_kwargs["base_url"] == "https://default.qwen.com"
                assert call_kwargs["backend_name"] == "qwen"


class TestBuildBackendManagerFromConfig:
    """Test cases for build_backend_manager_from_config function."""

    def test_build_backend_manager_from_config_with_options(self):
        """Test that config options are passed to QwenClient through build_backend_manager."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with options
            config = LLMBackendConfiguration()
            config.default_backend = "qwen"
            config.get_backend_config("qwen").model = "qwen3-coder-plus"
            config.get_backend_config("qwen").options = ["-o", "stream", "true"]
            config.get_backend_config("qwen").backend_type = "qwen"
            mock_get_config.return_value = config

            # Mock build_backend_manager to verify it's called correctly
            with patch("src.auto_coder.cli_helpers.build_backend_manager") as mock_build:
                mock_manager = MagicMock()
                mock_build.return_value = mock_manager

                # Call build_backend_manager_from_config
                manager = build_backend_manager_from_config(
                    enable_graphrag=False,
                    cli_backends=["qwen"],
                )

                # Verify build_backend_manager was called
                assert mock_build.call_count == 1
                call_args = mock_build.call_args

                # Check the parameters
                assert call_args.kwargs["selected_backends"] == ["qwen"]
                assert call_args.kwargs["primary_backend"] == "qwen"
                assert "qwen" in call_args.kwargs["models"]
                assert call_args.kwargs["enable_graphrag"] is False

    def test_build_backend_manager_from_config_with_alias(self):
        """Test that config aliases are properly handled."""
        with patch("src.auto_coder.cli_helpers.get_llm_config") as mock_get_config:
            # Setup mock config with an alias
            config = LLMBackendConfiguration()
            config.default_backend = "my-alias"
            # Add custom backend as alias
            config.backends["my-alias"] = BackendConfig(
                name="my-alias",
                model="qwen3-coder-plus",
                openai_api_key="test_key",
                options=["-o", "debug", "true"],
                backend_type="qwen",
            )
            mock_get_config.return_value = config

            # Mock build_backend_manager
            with patch("src.auto_coder.cli_helpers.build_backend_manager") as mock_build:
                mock_manager = MagicMock()
                mock_build.return_value = mock_manager

                # Call build_backend_manager_from_config with the alias
                manager = build_backend_manager_from_config(
                    enable_graphrag=False,
                    cli_backends=["my-alias"],
                )

                # Verify build_backend_manager was called with the alias
                assert mock_build.call_count == 1
                call_args = mock_build.call_args

                assert call_args.kwargs["selected_backends"] == ["my-alias"]
                assert call_args.kwargs["primary_backend"] == "my-alias"
                assert call_args.kwargs["models"]["my-alias"] == "qwen3-coder-plus"
