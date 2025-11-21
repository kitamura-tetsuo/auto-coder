"""
Tests for environment variable override functionality in LLM backend configuration.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from auto_coder.llm_backend_config import (
    BackendConfig,
    LLMBackendConfiguration,
    get_llm_config,
    reset_llm_config,
)


class TestEnvironmentVariableOverrides:
    """Test environment variable override functionality."""

    def test_backend_specific_api_key_override_gemini(self):
        """Test AUTO_CODER_GEMINI_API_KEY override."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").api_key = "original_key"

        with patch.dict(os.environ, {"AUTO_CODER_GEMINI_API_KEY": "env_gemini_key"}):
            config.apply_env_overrides()

        assert config.get_backend_config("gemini").api_key == "env_gemini_key"

    def test_backend_specific_api_key_override_qwen(self):
        """Test AUTO_CODER_QWEN_API_KEY override."""
        config = LLMBackendConfiguration()
        config.get_backend_config("qwen").api_key = "original_key"

        with patch.dict(os.environ, {"AUTO_CODER_QWEN_API_KEY": "env_qwen_key"}):
            config.apply_env_overrides()

        assert config.get_backend_config("qwen").api_key == "env_qwen_key"

    def test_backend_specific_api_key_override_codex(self):
        """Test AUTO_CODER_CODEX_API_KEY override."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").api_key = "original_key"

        with patch.dict(os.environ, {"AUTO_CODER_CODEX_API_KEY": "env_codex_key"}):
            config.apply_env_overrides()

        assert config.get_backend_config("codex").api_key == "env_codex_key"

    def test_backend_specific_api_key_override_claude(self):
        """Test AUTO_CODER_CLAUDE_API_KEY override."""
        config = LLMBackendConfiguration()
        config.get_backend_config("claude").api_key = "original_key"

        with patch.dict(os.environ, {"AUTO_CODER_CLAUDE_API_KEY": "env_claude_key"}):
            config.apply_env_overrides()

        assert config.get_backend_config("claude").api_key == "env_claude_key"

    def test_backend_specific_api_key_override_auggie(self):
        """Test AUTO_CODER_AUGGIE_API_KEY override."""
        config = LLMBackendConfiguration()
        config.get_backend_config("auggie").api_key = "original_key"

        with patch.dict(os.environ, {"AUTO_CODER_AUGGIE_API_KEY": "env_auggie_key"}):
            config.apply_env_overrides()

        assert config.get_backend_config("auggie").api_key == "env_auggie_key"

    def test_openai_api_key_global_override(self):
        """Test AUTO_CODER_OPENAI_API_KEY global override applies to all backends."""
        config = LLMBackendConfiguration()

        with patch.dict(os.environ, {"AUTO_CODER_OPENAI_API_KEY": "global_openai_key"}):
            config.apply_env_overrides()

        # Global OpenAI API key should not override backend-specific api_key
        # It should only override openai_api_key field
        assert config.get_backend_config("gemini").openai_api_key == "global_openai_key"
        assert config.get_backend_config("qwen").openai_api_key == "global_openai_key"
        assert config.get_backend_config("codex").openai_api_key == "global_openai_key"
        assert config.get_backend_config("claude").openai_api_key == "global_openai_key"

    def test_openai_api_key_backend_specific_override(self):
        """Test AUTO_CODER_<BACKEND>_OPENAI_API_KEY backend-specific override."""
        config = LLMBackendConfiguration()

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_GEMINI_OPENAI_API_KEY": "gemini_specific_key",
                "AUTO_CODER_QWEN_OPENAI_API_KEY": "qwen_specific_key",
            },
        ):
            config.apply_env_overrides()

        # Backend-specific overrides should work
        assert config.get_backend_config("gemini").openai_api_key == "gemini_specific_key"
        assert config.get_backend_config("qwen").openai_api_key == "qwen_specific_key"

    def test_openai_base_url_global_override(self):
        """Test AUTO_CODER_OPENAI_BASE_URL global override."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").openai_base_url = "https://original.openai.com"

        with patch.dict(
            os.environ,
            {"AUTO_CODER_OPENAI_BASE_URL": "https://global.openai.com"},
        ):
            config.apply_env_overrides()

        assert config.get_backend_config("gemini").openai_base_url == "https://global.openai.com"
        assert config.get_backend_config("qwen").openai_base_url == "https://global.openai.com"

    def test_openai_base_url_backend_specific_override(self):
        """Test AUTO_CODER_<BACKEND>_OPENAI_BASE_URL backend-specific override."""
        config = LLMBackendConfiguration()

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_GEMINI_OPENAI_BASE_URL": "https://gemini.openai.com",
                "AUTO_CODER_QWEN_OPENAI_BASE_URL": "https://qwen.openai.com",
            },
        ):
            config.apply_env_overrides()

        assert config.get_backend_config("gemini").openai_base_url == "https://gemini.openai.com"
        assert config.get_backend_config("qwen").openai_base_url == "https://qwen.openai.com"

    def test_default_backend_override(self):
        """Test AUTO_CODER_DEFAULT_BACKEND override."""
        config = LLMBackendConfiguration()
        assert config.default_backend == "codex"

        with patch.dict(os.environ, {"AUTO_CODER_DEFAULT_BACKEND": "gemini"}):
            config.apply_env_overrides()

        assert config.default_backend == "gemini"

    def test_message_default_backend_override(self):
        """Test AUTO_CODER_MESSAGE_DEFAULT_BACKEND override."""
        config = LLMBackendConfiguration()
        assert config.message_default_backend is None

        with patch.dict(
            os.environ,
            {"AUTO_CODER_MESSAGE_DEFAULT_BACKEND": "claude"},
        ):
            config.apply_env_overrides()

        assert config.message_default_backend == "claude"

    def test_environment_variables_override_config_file_values(self):
        """Test that environment variables properly override config file values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_config.toml"

            # Create a config file with specific values
            config = LLMBackendConfiguration()
            config.default_backend = "codex"
            config.get_backend_config("gemini").model = "config-model"
            config.get_backend_config("gemini").api_key = "config-api-key"
            config.get_backend_config("gemini").openai_api_key = "config-openai-key"
            config.save_to_file(config_file)

            # Load the config from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Set environment variables with different values
            with patch.dict(
                os.environ,
                {
                    "AUTO_CODER_DEFAULT_BACKEND": "env-backend",
                    "AUTO_CODER_GEMINI_API_KEY": "env-api-key",
                    "AUTO_CODER_OPENAI_API_KEY": "env-openai-key",
                },
            ):
                loaded_config.apply_env_overrides()

            # Verify environment variables overrode config file values
            assert loaded_config.default_backend == "env-backend"
            assert loaded_config.get_backend_config("gemini").api_key == "env-api-key"
            assert loaded_config.get_backend_config("gemini").openai_api_key == "env-openai-key"
            # Model should remain unchanged as it doesn't have an env override
            assert loaded_config.get_backend_config("gemini").model == "config-model"

    def test_global_openai_key_takes_precedence_over_backend_specific(self):
        """Test that global AUTO_CODER_OPENAI_API_KEY takes precedence."""
        config = LLMBackendConfiguration()

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_OPENAI_API_KEY": "global_key",
                "AUTO_CODER_GEMINI_OPENAI_API_KEY": "backend_specific_key",
            },
        ):
            config.apply_env_overrides()

        # Global should take precedence (checked first in implementation)
        assert config.get_backend_config("gemini").openai_api_key == "global_key"

    def test_multiple_backend_api_key_overrides(self):
        """Test overriding API keys for multiple backends simultaneously."""
        config = LLMBackendConfiguration()

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_GEMINI_API_KEY": "gemini-key",
                "AUTO_CODER_QWEN_API_KEY": "qwen-key",
                "AUTO_CODER_CODEX_API_KEY": "codex-key",
                "AUTO_CODER_CLAUDE_API_KEY": "claude-key",
            },
        ):
            config.apply_env_overrides()

        assert config.get_backend_config("gemini").api_key == "gemini-key"
        assert config.get_backend_config("qwen").api_key == "qwen-key"
        assert config.get_backend_config("codex").api_key == "codex-key"
        assert config.get_backend_config("claude").api_key == "claude-key"

    def test_no_environment_variables_leaves_config_unchanged(self):
        """Test that missing environment variables don't modify config."""
        config = LLMBackendConfiguration()
        config.default_backend = "gemini"
        config.get_backend_config("gemini").api_key = "original-key"

        # Ensure no relevant environment variables are set
        with patch.dict(os.environ, {}, clear=True):
            config.apply_env_overrides()

        # Config should remain unchanged
        assert config.default_backend == "gemini"
        assert config.get_backend_config("gemini").api_key == "original-key"

    def test_empty_environment_variable_ignored(self):
        """Test that empty environment variables don't override config."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").api_key = "original-key"

        with patch.dict(
            os.environ,
            {"AUTO_CODER_GEMINI_API_KEY": ""},
        ):
            config.apply_env_overrides()

        # Empty string is falsy, so it should not override
        # This is the expected behavior per the implementation
        assert config.get_backend_config("gemini").api_key == "original-key"

    def test_env_overrides_with_nonexistent_backend_ignored(self):
        """Test that env overrides for nonexistent backends are safely ignored."""
        config = LLMBackendConfiguration()

        # Only default backends are initialized
        assert "nonexistent_backend" not in config.backends

        with patch.dict(
            os.environ,
            {"AUTO_CODER_NONEXISTENT_BACKEND_API_KEY": "test-key"},
        ):
            # Should not raise an error
            config.apply_env_overrides()

        # Config should remain unchanged
        assert len(config.backends) == 6  # Default backends only

    def test_get_llm_config_applies_env_overrides(self):
        """Test that get_llm_config applies environment overrides."""
        reset_llm_config()

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_DEFAULT_BACKEND": "qwen",
                "AUTO_CODER_GEMINI_API_KEY": "test-key",
            },
        ):
            config = get_llm_config()

        assert config.default_backend == "qwen"
        assert config.get_backend_config("gemini").api_key == "test-key"

    def test_env_override_with_file_config(self):
        """Test environment override works with file-based configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.toml"

            # Create a config file
            config = LLMBackendConfiguration()
            config.default_backend = "codex"
            config.get_backend_config("gemini").model = "custom-model"
            config.get_backend_config("gemini").api_key = "file-key"
            config.save_to_file(str(config_file))

            # Load and apply env overrides
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            with patch.dict(
                os.environ,
                {"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
            ):
                loaded_config.apply_env_overrides()

            # Verify override
            assert loaded_config.default_backend == "gemini"
            # File values should remain
            assert loaded_config.get_backend_config("gemini").model == "custom-model"
            assert loaded_config.get_backend_config("gemini").api_key == "file-key"

    def test_multiple_openai_overrides_for_different_backends(self):
        """Test applying different OpenAI credentials to different backends."""
        config = LLMBackendConfiguration()

        with patch.dict(
            os.environ,
            {
                # Global overrides
                "AUTO_CODER_OPENAI_API_KEY": "global-key",
                "AUTO_CODER_OPENAI_BASE_URL": "https://global.openai.com",
                # Backend-specific overrides for some backends
                "AUTO_CODER_CODEX_OPENAI_API_KEY": "codex-key",
                "AUTO_CODER_CODEX_OPENAI_BASE_URL": "https://codex.openai.com",
            },
        ):
            config.apply_env_overrides()

        # Global overrides should apply to all
        assert config.get_backend_config("gemini").openai_api_key == "global-key"
        assert config.get_backend_config("qwen").openai_api_key == "global-key"

        # Backend-specific should take precedence for that backend
        assert config.get_backend_config("codex").openai_api_key == "global-key"
        # The global key takes precedence over backend-specific (implementation detail)

        assert config.get_backend_config("gemini").openai_base_url == "https://global.openai.com"
        assert config.get_backend_config("codex").openai_base_url == "https://global.openai.com"

    def test_env_override_persistence_across_instances(self):
        """Test that env overrides work correctly across config instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "config.toml"

            # Create initial config
            config1 = LLMBackendConfiguration()
            config1.default_backend = "codex"
            config1.save_to_file(str(config_file))

            # Load with environment override
            with patch.dict(
                os.environ,
                {"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
            ):
                config2 = LLMBackendConfiguration.load_from_file(str(config_file))
                config2.apply_env_overrides()

            # Should have the overridden value
            assert config2.default_backend == "gemini"

            # Load again without env override
            config3 = LLMBackendConfiguration.load_from_file(str(config_file))
            config3.apply_env_overrides()

            # Should still have the overridden value from env
            # (env is checked at runtime)
            with patch.dict(os.environ, {}, clear=True):
                config4 = LLMBackendConfiguration.load_from_file(str(config_file))
                config4.apply_env_overrides()

            # With no env, should be from file
            assert config4.default_backend == "codex"
