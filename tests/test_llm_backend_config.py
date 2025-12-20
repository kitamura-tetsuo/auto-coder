"""Tests for llm_backend_config module."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import toml

from src.auto_coder.llm_backend_config import (
    BackendConfig,
    LLMBackendConfiguration,
    get_llm_config,
    reset_llm_config,
    resolve_config_path,
)


class TestBackendConfig:
    """Test cases for BackendConfig class."""

    def test_backend_config_creation(self):
        """Test creating a BackendConfig with default values."""
        config = BackendConfig(name="gemini")
        assert config.name == "gemini"
        assert config.enabled is True
        assert config.model is None
        assert config.api_key is None
        assert config.base_url is None
        assert config.temperature is None
        assert config.timeout is None
        assert config.max_retries is None
        assert config.openai_api_key is None
        assert config.openai_base_url is None
        assert config.extra_args == {}
        assert config.providers == []
        assert config.usage_limit_retry_count == 0
        assert config.usage_limit_retry_wait_seconds == 0
        assert config.options == []
        assert config.options_for_resume == []
        assert config.backend_type is None
        assert config.model_provider is None
        assert config.always_switch_after_execution is False
        assert config.usage_markers == []
        assert config.options_for_noedit == []
        assert config.options_explicitly_set is False
        assert config.options_for_noedit_explicitly_set is False

    def test_backend_config_with_custom_values(self):
        """Test creating a BackendConfig with custom values."""
        config = BackendConfig(
            name="gemini",
            enabled=False,
            model="gemini-pro",
            api_key="test_key",
            base_url="https://api.example.com",
            temperature=0.7,
            timeout=30,
            max_retries=3,
            openai_api_key="openai_key",
            openai_base_url="https://openai.example.com",
            extra_args={"arg1": "value1"},
            providers=["provider1", "provider2"],
            usage_limit_retry_count=5,
            usage_limit_retry_wait_seconds=30,
            options=["option1", "option2"],
            options_for_resume=["resume_option1", "resume_option2"],
            backend_type="custom_type",
            model_provider="openrouter",
            always_switch_after_execution=True,
            usage_markers=["marker1", "marker2"],
            options_for_noedit=["noedit_option1", "noedit_option2"],
            options_explicitly_set=True,
            options_for_noedit_explicitly_set=True,
        )
        assert config.name == "gemini"
        assert config.enabled is False
        assert config.model == "gemini-pro"
        assert config.api_key == "test_key"
        assert config.base_url == "https://api.example.com"
        assert config.temperature == 0.7
        assert config.timeout == 30
        assert config.max_retries == 3
        assert config.openai_api_key == "openai_key"
        assert config.openai_base_url == "https://openai.example.com"
        assert config.extra_args == {"arg1": "value1"}
        assert config.providers == ["provider1", "provider2"]
        assert config.usage_limit_retry_count == 5
        assert config.usage_limit_retry_wait_seconds == 30
        assert config.options == ["option1", "option2"]
        assert config.options_for_resume == ["resume_option1", "resume_option2"]
        assert config.backend_type == "custom_type"
        assert config.model_provider == "openrouter"
        assert config.always_switch_after_execution is True
        assert config.usage_markers == ["marker1", "marker2"]
        assert config.options_for_noedit == ["noedit_option1", "noedit_option2"]
        assert config.options_explicitly_set is True
        assert config.options_for_noedit_explicitly_set is True

    def test_backend_config_extra_args_default(self):
        """Test that extra_args has a proper default factory."""
        config1 = BackendConfig(name="test1")
        config2 = BackendConfig(name="test2")

        # Modifying one shouldn't affect the other
        config1.extra_args["test"] = "value"
        assert "test" not in config2.extra_args

    def test_replace_placeholders_with_model_name(self):
        """Test replacing [model_name] placeholder."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]", "--json"],
            options_for_noedit=["--model", "[model_name]"],
            options_for_resume=["--model", "[model_name]", "--resume"],
        )

        result = config.replace_placeholders(model_name="gpt-5.1-codex-max")

        assert result["options"] == ["--model", "gpt-5.1-codex-max", "--json"]
        assert result["options_for_noedit"] == ["--model", "gpt-5.1-codex-max"]
        assert result["options_for_resume"] == ["--model", "gpt-5.1-codex-max", "--resume"]

    def test_replace_placeholders_with_session_id(self):
        """Test replacing [sessionId] placeholder."""
        config = BackendConfig(
            name="codex",
            options=["--session", "[sessionId]", "--flag"],
            options_for_noedit=["--session", "[sessionId]"],
            options_for_resume=["--session", "[sessionId]"],
        )

        result = config.replace_placeholders(session_id="abc123xyz")

        assert result["options"] == ["--session", "abc123xyz", "--flag"]
        assert result["options_for_noedit"] == ["--session", "abc123xyz"]
        assert result["options_for_resume"] == ["--session", "abc123xyz"]

    def test_replace_placeholders_with_both_placeholders(self):
        """Test replacing both [model_name] and [sessionId] placeholders."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]", "--session", "[sessionId]"],
            options_for_noedit=["--model", "[model_name]"],
            options_for_resume=["--session", "[sessionId]"],
        )

        result = config.replace_placeholders(model_name="gpt-5.1-codex-max", session_id="abc123xyz")

        assert result["options"] == ["--model", "gpt-5.1-codex-max", "--session", "abc123xyz"]
        assert result["options_for_noedit"] == ["--model", "gpt-5.1-codex-max"]
        assert result["options_for_resume"] == ["--session", "abc123xyz"]

    def test_replace_placeholders_multiple_occurrences(self):
        """Test replacing multiple occurrences of the same placeholder."""
        config = BackendConfig(
            name="codex",
            options=["[model_name]", "--model", "[model_name]", "--flag"],
            options_for_noedit=["[model_name]", "[model_name]"],
        )

        result = config.replace_placeholders(model_name="gpt-5.1-codex-max")

        assert result["options"] == ["gpt-5.1-codex-max", "--model", "gpt-5.1-codex-max", "--flag"]
        assert result["options_for_noedit"] == ["gpt-5.1-codex-max", "gpt-5.1-codex-max"]

    def test_replace_placeholders_no_placeholders(self):
        """Test that options without placeholders are returned unchanged."""
        config = BackendConfig(
            name="codex",
            options=["--model", "gpt-4", "--flag"],
            options_for_noedit=["--json", "--verbose"],
            options_for_resume=["--resume"],
        )

        result = config.replace_placeholders(model_name="gpt-5")

        assert result["options"] == ["--model", "gpt-4", "--flag"]
        assert result["options_for_noedit"] == ["--json", "--verbose"]
        assert result["options_for_resume"] == ["--resume"]

    def test_replace_placeholders_empty_option_lists(self):
        """Test with empty option lists."""
        config = BackendConfig(name="codex")

        result = config.replace_placeholders(model_name="gpt-5")

        assert result["options"] == []
        assert result["options_for_noedit"] == []
        assert result["options_for_resume"] == []

    def test_replace_placeholders_no_parameters(self):
        """Test when no placeholder values are provided."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]", "--flag"],
            options_for_noedit=["--json"],
        )

        result = config.replace_placeholders()

        # Without providing model_name, placeholders should remain unchanged
        assert result["options"] == ["--model", "[model_name]", "--flag"]
        assert result["options_for_noedit"] == ["--json"]
        assert result["options_for_resume"] == []

    def test_replace_placeholders_returns_new_lists(self):
        """Test that replace_placeholders returns new lists, not modifies originals."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]"],
            options_for_noedit=["--json"],
            options_for_resume=["--resume"],
        )

        original_options = list(config.options)
        original_noedit = list(config.options_for_noedit)
        original_resume = list(config.options_for_resume)

        result = config.replace_placeholders(model_name="gpt-5")

        # Verify the original lists were not modified
        assert config.options == original_options
        assert config.options_for_noedit == original_noedit
        assert config.options_for_resume == original_resume

        # Verify the returned lists are different objects
        assert result["options"] is not config.options
        assert result["options_for_noedit"] is not config.options_for_noedit
        assert result["options_for_resume"] is not config.options_for_resume

        # Verify the returned lists have the expected values
        assert result["options"] == ["--model", "gpt-5"]
        assert result["options_for_noedit"] == ["--json"]
        assert result["options_for_resume"] == ["--resume"]

    def test_replace_placeholders_missing_placeholder(self):
        """Test that missing placeholder values leave placeholders unchanged."""
        config = BackendConfig(
            name="codex",
            options=["[model_name]", "[sessionId]", "--flag"],
        )

        # Only provide model_name, sessionId should remain
        result = config.replace_placeholders(model_name="gpt-5")

        assert result["options"] == ["gpt-5", "[sessionId]", "--flag"]
        assert result["options_for_noedit"] == []
        assert result["options_for_resume"] == []

    def test_replace_placeholders_with_none_values(self):
        """Test with None values for placeholders."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]", "--session", "[sessionId]"],
        )

        result = config.replace_placeholders(model_name=None, session_id=None)

        # With None values, placeholders should remain unchanged
        assert result["options"] == ["--model", "[model_name]", "--session", "[sessionId]"]

    def test_replace_placeholders_with_empty_string_values(self):
        """Test with empty string values for placeholders."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]", "--flag"],
            options_for_noedit=["--json", "[sessionId]"],
        )

        result = config.replace_placeholders(model_name="", session_id="")

        assert result["options"] == ["--model", "", "--flag"]
        assert result["options_for_noedit"] == ["--json", ""]
        assert result["options_for_resume"] == []

    def test_replace_placeholders_special_characters_in_values(self):
        """Test replacing placeholders with values containing special characters."""
        config = BackendConfig(
            name="codex",
            options=["--model", "[model_name]"],
            options_for_noedit=["--session", "[sessionId]"],
        )

        result = config.replace_placeholders(model_name="gpt-4.1-pro/model:special", session_id="session-123_abc.xyz")

        assert result["options"] == ["--model", "gpt-4.1-pro/model:special"]
        assert result["options_for_noedit"] == ["--session", "session-123_abc.xyz"]

    def test_replace_placeholders_real_world_example(self):
        """Test with a real-world configuration example."""
        config = BackendConfig(
            name="codex",
            model="gpt-5.1-codex-max",
            options=["--model", "[model_name]", "--json", "--dangerously-bypass-approvals-and-sandbox"],
            options_for_noedit=["--model", "[model_name]", "--json"],
            options_for_resume=["--model", "[model_name]"],
        )

        result = config.replace_placeholders(model_name="gpt-5.1-codex-max", session_id="sess_20241204")

        assert result["options"] == ["--model", "gpt-5.1-codex-max", "--json", "--dangerously-bypass-approvals-and-sandbox"]
        assert result["options_for_noedit"] == ["--model", "gpt-5.1-codex-max", "--json"]
        assert result["options_for_resume"] == ["--model", "gpt-5.1-codex-max"]


class TestLLMBackendConfiguration:
    """Test cases for LLMBackendConfiguration class."""

    def test_default_initialization(self):
        """Test LLMBackendConfiguration with default values."""
        config = LLMBackendConfiguration()

        # Check default backends are created
        assert "codex" in config.backends
        assert "gemini" in config.backends
        assert "qwen" in config.backends
        assert "auggie" in config.backends
        assert "claude" in config.backends
        assert "codex-mcp" in config.backends

        # Check default settings
        assert config.default_backend == "codex"
        assert config.backend_order == []
        assert config.backend_for_noedit_order == []
        assert config.backend_for_noedit_default is None
        assert config.env_prefix == "AUTO_CODER_"
        assert config.config_file_path == "~/.auto-coder/llm_config.toml"

    def test_custom_initialization(self):
        """Test LLMBackendConfiguration with custom values."""
        backends = {
            "gemini": BackendConfig(name="gemini", model="gemini-pro"),
            "codex": BackendConfig(name="codex", enabled=False),
        }
        config = LLMBackendConfiguration(
            backend_order=["gemini", "codex"],
            default_backend="gemini",
            backends=backends,
            backend_for_noedit_order=["codex"],
            backend_for_noedit_default="codex",
        )

        assert config.default_backend == "gemini"
        assert config.backend_order == ["gemini", "codex"]
        assert config.backend_for_noedit_order == ["codex"]
        assert config.backend_for_noedit_default == "codex"
        assert config.backends["gemini"].model == "gemini-pro"
        assert config.backends["codex"].enabled is False

    def test_options_for_noedit_field(self):
        """Test that options_for_noedit is parsed and stored."""
        config_data = {"backends": {"codex": {"options": ["--flag1"], "options_for_noedit": ["--flag2"]}}}

        config = LLMBackendConfiguration.load_from_dict(config_data)

        assert config.backends["codex"].options == ["--flag1"]
        assert config.backends["codex"].options_for_noedit == ["--flag2"]

    def test_options_for_noedit_default_empty(self):
        """options_for_noedit should default to an empty list when missing."""
        config_data = {"backends": {"codex": {"options": ["--flag1"]}}}

        config = LLMBackendConfiguration.load_from_dict(config_data)

        assert config.backends["codex"].options == ["--flag1"]
        assert config.backends["codex"].options_for_noedit == []

    def test_save_and_load_from_file(self):
        """Test saving and loading configuration from a TOML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_config.toml"

            # Create a configuration with custom values
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").model = "gemini-pro-custom"
            config.get_backend_config("gemini").api_key = "test_key_123"
            config.get_backend_config("gemini").temperature = 0.8
            config.get_backend_config("qwen").providers = ["qwen-open-router", "qwen-azure"]
            config.default_backend = "gemini"
            config.backend_order = ["gemini", "codex", "qwen"]

            # Save to file
            config.save_to_file(str(config_file))

            # Verify file was created
            assert config_file.exists()

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify loaded configuration matches saved configuration
            assert loaded_config.default_backend == config.default_backend
            assert loaded_config.backend_order == config.backend_order
            assert loaded_config.get_backend_config("gemini").model == "gemini-pro-custom"
            assert loaded_config.get_backend_config("gemini").api_key == "test_key_123"
            assert loaded_config.get_backend_config("gemini").temperature == 0.8
            assert loaded_config.get_backend_config("qwen").providers == ["qwen-open-router", "qwen-azure"]

    def test_load_from_file_parses_provider_lists_and_uppercase_fields(self):
        """Provider lists and uppercase env-style fields should be preserved from the TOML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "llm_config.toml"
            data = {
                "backend": {"default": "qwen", "order": ["qwen"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router", "qwen-azure"],
                        "extra_args": {
                            "AZURE_ENDPOINT": "https://llm.example.net",
                            "QWEN_API_KEY": "example-key",
                        },
                    }
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))
            qwen_config = config.get_backend_config("qwen")

            assert qwen_config is not None
            assert qwen_config.providers == ["qwen-open-router", "qwen-azure"]
            assert qwen_config.extra_args["AZURE_ENDPOINT"] == "https://llm.example.net"
            assert qwen_config.extra_args["QWEN_API_KEY"] == "example-key"

    def test_load_from_file_parses_dotted_keys_recursive(self):
        """Test that top-level dotted keys (which TOML parses as nested dicts) are correctly identified as backends."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "dotted_keys.toml"
            # This structure mimics [grok-4.1-fast] which TOML parses as nested dicts
            data = {"grok-4": {"1-fast": {"enabled": True, "model": "x-ai/grok-4.1-fast:free", "backend_type": "codex", "openai_base_url": "https://openrouter.ai/api/v1"}}, "deepseek": {"coder": {"v2": {"enabled": True, "backend_type": "codex"}}}}
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify grok-4.1-fast was found
            grok_config = config.get_backend_config("grok-4.1-fast")
            assert grok_config is not None
            assert grok_config.backend_type == "codex"
            assert grok_config.model == "x-ai/grok-4.1-fast:free"

            # Verify deepseek.coder.v2 was found
            deepseek_config = config.get_backend_config("deepseek.coder.v2")
            assert deepseek_config is not None
            assert deepseek_config.backend_type == "codex"

    def test_load_from_nonexistent_file_creates_default(self):
        """Test that loading a nonexistent file creates a default configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "nonexistent.toml"

            # File should not exist initially
            assert not config_file.exists()

            # Load configuration (should create default)
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify default configuration was created
            assert config.default_backend == "codex"
            assert "gemini" in config.backends
            assert "codex" in config.backends

            # Verify file was created
            assert config_file.exists()

    def test_get_backend_config(self):
        """Test getting configuration for a specific backend."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").model = "custom-model"

        backend_config = config.get_backend_config("gemini")
        assert backend_config is not None
        assert backend_config.name == "gemini"
        assert backend_config.model == "custom-model"

        # Test nonexistent backend
        nonexistent = config.get_backend_config("nonexistent")
        assert nonexistent is None

    def test_get_active_backends(self):
        """Test getting list of enabled backends."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = False
        config.get_backend_config("codex").enabled = True
        config.get_backend_config("qwen").enabled = False
        config.backend_order = ["gemini", "codex", "qwen"]

        active = config.get_active_backends()
        assert "codex" in active
        assert "gemini" not in active
        assert "qwen" not in active

    def test_get_active_backends_no_order(self):
        """Test getting active backends when no order is specified."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = True
        config.get_backend_config("codex").enabled = False
        config.get_backend_config("qwen").enabled = True

        active = config.get_active_backends()
        assert "gemini" in active
        assert "codex" not in active
        assert "qwen" in active

    def test_get_active_noedit_backends(self):
        """Test getting active noedit backends."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = False
        config.get_backend_config("codex").enabled = True
        config.get_backend_config("qwen").enabled = False
        config.backend_for_noedit_order = ["codex", "gemini", "qwen"]

        active = config.get_active_noedit_backends()
        assert "codex" in active
        assert "gemini" not in active
        assert "qwen" not in active

    def test_get_active_noedit_backends_fallback(self):
        """Test noedit backends fall back to general backends when not configured."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = False
        config.get_backend_config("codex").enabled = True
        config.backend_order = ["codex", "gemini", "qwen"]
        # No backend_for_noedit_order set

        active = config.get_active_noedit_backends()
        assert "codex" in active
        # Should fall back to general backends

    def test_get_noedit_default_backend(self):
        """Test getting default noedit backend."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").enabled = True
        config.backend_for_noedit_default = "codex"
        config.default_backend = "gemini"

        assert config.get_noedit_default_backend() == "codex"

    def test_get_noedit_default_backend_fallback(self):
        """Test noedit default falls back to general default."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = True
        config.default_backend = "gemini"

        # No backend_for_noedit_default set
        assert config.get_noedit_default_backend() == "gemini"

    def test_get_noedit_default_backend_disabled(self):
        """Test that disabled noedit default falls back to general default."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").enabled = False
        config.backend_for_noedit_default = "codex"
        config.default_backend = "gemini"

        # backend_for_noedit_default is disabled, should fall back
        assert config.get_noedit_default_backend() == "gemini"

    # Test backward compatibility with deprecated method names
    def test_get_active_message_backends_deprecated(self):
        """Test deprecated get_active_message_backends() still works."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").enabled = True
        config.backend_for_noedit_order = ["codex"]

        with patch("src.auto_coder.llm_backend_config.get_logger") as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger

            # Deprecated method should still work
            active = config.get_active_message_backends()

        assert "codex" in active
        mock_logger.warning.assert_called_once_with("get_active_message_backends() is deprecated. Use get_active_noedit_backends() instead.")

    def test_get_message_default_backend_deprecated(self):
        """Test deprecated get_message_default_backend() still works."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").enabled = True
        config.backend_for_noedit_default = "codex"

        with patch("src.auto_coder.llm_backend_config.get_logger") as mock_get_logger:
            mock_logger = MagicMock()
            mock_get_logger.return_value = mock_logger

            # Deprecated method should still work
            assert config.get_message_default_backend() == "codex"

        mock_logger.warning.assert_called_once_with("get_message_default_backend() is deprecated. Use get_noedit_default_backend() instead.")

    def test_has_dual_configuration(self):
        """Test detection of dual backend configuration."""
        config1 = LLMBackendConfiguration()
        config1.backend_for_noedit_order = ["codex"]
        assert config1.has_dual_configuration() is True

        config2 = LLMBackendConfiguration()
        config2.backend_order = ["gemini", "codex"]
        config2.backend_for_noedit_order = ["codex"]
        assert config2.has_dual_configuration() is True

        config3 = LLMBackendConfiguration()
        # Only general config
        assert config3.has_dual_configuration() is False

        config4 = LLMBackendConfiguration()
        # Only noedit config
        config4.backend_for_noedit_order = ["codex"]
        config4.backend_for_noedit_default = "codex"
        assert config4.has_dual_configuration() is True

    def test_get_model_for_backend(self):
        """Test getting model for a specific backend."""
        config = LLMBackendConfiguration()

        # Test with custom model
        config.get_backend_config("gemini").model = "custom-gemini-model"
        assert config.get_model_for_backend("gemini") == "custom-gemini-model"

        # Test with default models (use a fresh config instance)
        config2 = LLMBackendConfiguration()
        assert config2.get_model_for_backend("gemini") == "gemini-2.5-pro"
        assert config2.get_model_for_backend("qwen") == "qwen3-coder-plus"
        assert config2.get_model_for_backend("auggie") == "GPT-5"
        assert config2.get_model_for_backend("claude") == "sonnet"
        assert config2.get_model_for_backend("codex") == "codex"
        assert config2.get_model_for_backend("codex-mcp") == "codex-mcp"

        # Test with unknown backend (no default)
        assert config.get_model_for_backend("unknown") is None

    def test_apply_env_overrides(self):
        """Test applying environment variable overrides."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").api_key = "original_key"

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_GEMINI_API_KEY": "env_gemini_key",
                "AUTO_CODER_OPENAI_API_KEY": "env_openai_key",
                "AUTO_CODER_GEMINI_OPENAI_API_KEY": "env_gemini_openai_key",
                "AUTO_CODER_DEFAULT_BACKEND": "qwen",
                "AUTO_CODER_NOEDIT_DEFAULT_BACKEND": "claude",
            },
        ):
            config.apply_env_overrides()

        # Check that environment overrides were applied
        assert config.get_backend_config("gemini").api_key == "env_gemini_key"
        assert config.default_backend == "qwen"
        assert config.backend_for_noedit_default == "claude"

    def test_apply_env_overrides_openai(self):
        """Test applying OpenAI environment variable overrides."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").openai_api_key = "original_openai_key"
        config.get_backend_config("gemini").openai_base_url = "https://original.openai.com"

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_OPENAI_API_KEY": "global_openai_key",
                "AUTO_CODER_OPENAI_BASE_URL": "https://global.openai.com",
            },
        ):
            config.apply_env_overrides()

        # Check that environment overrides were applied
        assert config.get_backend_config("gemini").openai_api_key == "global_openai_key"
        assert config.get_backend_config("gemini").openai_base_url == "https://global.openai.com"

    def test_apply_env_overrides_backend_specific(self):
        """Test that global OpenAI variables take precedence over backend-specific ones."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").openai_api_key = "original_openai_key"

        with patch.dict(
            os.environ,
            {
                "AUTO_CODER_OPENAI_API_KEY": "global_openai_key",
                "AUTO_CODER_GEMINI_OPENAI_API_KEY": "gemini_openai_key",
            },
        ):
            config.apply_env_overrides()

        # Global should take precedence (checked first in the implementation)
        assert config.get_backend_config("gemini").openai_api_key == "global_openai_key"

    def test_save_creates_directory(self):
        """Test that save creates parent directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path as PathClass

            config_file = PathClass(tmpdir) / "subdir" / "test_config.toml"

            config = LLMBackendConfiguration()
            config.save_to_file(str(config_file))

            # Verify directory was created
            assert (PathClass(tmpdir) / "subdir").exists()
            # Verify file was created
            assert config_file.exists()

    def test_toml_file_structure(self):
        """Test that TOML file has correct structure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_config.toml"

            # Create and save configuration
            config = LLMBackendConfiguration()
            config.default_backend = "gemini"
            config.backend_order = ["gemini", "codex"]
            config.get_backend_config("gemini").model = "custom-model"
            config.get_backend_config("gemini").temperature = 0.9
            config.get_backend_config("qwen").providers = ["qwen-open-router", "qwen-azure"]
            config.save_to_file(str(config_file))

            # Read and parse TOML directly
            with open(config_file, "r") as f:
                data = toml.load(f)

            # Verify structure
            assert "backend" in data
            assert "backends" in data
            assert data["backend"]["default"] == "gemini"
            assert data["backend"]["order"] == ["gemini", "codex"]
            assert data["backends"]["gemini"]["model"] == "custom-model"
            assert data["backends"]["gemini"]["temperature"] == 0.9
            assert data["backends"]["qwen"]["providers"] == ["qwen-open-router", "qwen-azure"]

    def test_toml_save_and_load_retry_config(self):
        """Test that retry configuration fields are properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_retry_config.toml"

            # Create configuration with retry settings
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").usage_limit_retry_count = 5
            config.get_backend_config("gemini").usage_limit_retry_wait_seconds = 30
            config.get_backend_config("qwen").usage_limit_retry_count = 3
            config.get_backend_config("qwen").usage_limit_retry_wait_seconds = 60

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify retry configuration was persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.usage_limit_retry_count == 5
            assert gemini_config.usage_limit_retry_wait_seconds == 30

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.usage_limit_retry_count == 3
            assert qwen_config.usage_limit_retry_wait_seconds == 60

    def test_toml_save_and_load_new_fields(self):
        """Test that new options and backend_type fields are properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_new_fields.toml"

            # Create configuration with new fields
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").options = ["option1", "option2"]
            config.get_backend_config("gemini").backend_type = "custom_type"
            config.get_backend_config("qwen").options = ["option3"]
            config.get_backend_config("qwen").backend_type = "another_type"

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify new fields were persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.options == ["option1", "option2"]
            assert gemini_config.backend_type == "custom_type"

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.options == ["option3"]
            assert qwen_config.backend_type == "another_type"

    def test_toml_save_and_load_options_for_resume(self):
        """Test that options_for_resume field is properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_options_for_resume.toml"

            # Create configuration with options_for_resume settings
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").options_for_resume = ["resume_opt1", "resume_opt2"]
            config.get_backend_config("qwen").options_for_resume = ["resume_opt3"]
            config.get_backend_config("codex").options_for_resume = []

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify options_for_resume was persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.options_for_resume == ["resume_opt1", "resume_opt2"]

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.options_for_resume == ["resume_opt3"]

            codex_config = loaded_config.get_backend_config("codex")
            assert codex_config.options_for_resume == []

    def test_toml_save_and_load_always_switch_after_execution(self):
        """Test that always_switch_after_execution field is properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_always_switch.toml"

            # Create configuration with always_switch_after_execution settings
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").always_switch_after_execution = True
            config.get_backend_config("qwen").always_switch_after_execution = False

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify always_switch_after_execution field was persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.always_switch_after_execution is True

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.always_switch_after_execution is False

    def test_toml_save_and_load_options_for_noedit(self):
        """Test that options_for_noedit field is properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_options_for_noedit.toml"

            # Create configuration with options_for_noedit settings
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").options_for_noedit = ["noedit1", "noedit2"]
            config.get_backend_config("qwen").options_for_noedit = ["noedit3"]

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify options_for_noedit was persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.options_for_noedit == ["noedit1", "noedit2"]

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.options_for_noedit == ["noedit3"]

    def test_backward_compatibility_old_toml_without_retry_fields(self):
        """Test loading old TOML files that don't have retry configuration fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config.toml"

            # Create a TOML file with the old structure (without retry fields)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router"],
                        "temperature": 0.7,
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify retry fields have default values when not present in old TOML
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.usage_limit_retry_count == 0
            assert qwen_config.usage_limit_retry_wait_seconds == 0
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.usage_limit_retry_count == 0
            assert gemini_config.usage_limit_retry_wait_seconds == 0
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30

    def test_backward_compatibility_old_toml_without_new_fields(self):
        """Test loading old TOML files that don't have options and backend_type fields."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config_new_fields.toml"

            # Create a TOML file with the old structure (without new fields)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router"],
                        "temperature": 0.7,
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify new fields have default values when not present in old TOML
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.options == []
            assert qwen_config.options_for_resume == []
            assert qwen_config.backend_type is None
            assert qwen_config.always_switch_after_execution is False
            assert qwen_config.options_for_noedit == []
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.options == []
            assert gemini_config.options_for_resume == []
            assert gemini_config.backend_type is None
            assert gemini_config.always_switch_after_execution is False
            assert gemini_config.options_for_noedit == []
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30

    def test_backward_compatibility_old_toml_without_options_for_noedit(self):
        """Test loading old TOML files that don't have options_for_noedit field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config_options_for_noedit.toml"

            # Create a TOML file with the old structure (without options_for_noedit field)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router"],
                        "temperature": 0.7,
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify options_for_noedit has default empty list when not present in old TOML
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.options_for_noedit == []
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.options_for_noedit == []
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30

    def test_backward_compatibility_old_toml_without_options_for_resume(self):
        """Test loading old TOML files that don't have options_for_resume field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config_options_for_resume.toml"

            # Create a TOML file with the old structure (without options_for_resume field)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router"],
                        "temperature": 0.7,
                        "options": ["option1", "option2"],
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                        "backend_type": "custom_type",
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify options_for_resume has default empty list when not present in old TOML
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.options_for_resume == []
            assert qwen_config.options == ["option1", "option2"]
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.options_for_resume == []
            assert gemini_config.backend_type == "custom_type"
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30

    def test_configuration_persistence_across_instances(self):
        """Test that configuration persists correctly across multiple instances."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_config.toml"

            # Create first instance and save
            config1 = LLMBackendConfiguration()
            config1.default_backend = "gemini"
            config1.get_backend_config("gemini").model = "test-model"
            config1.save_to_file(str(config_file))

            # Create second instance and load
            config2 = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify values persisted
            assert config2.default_backend == "gemini"
            assert config2.get_backend_config("gemini").model == "test-model"

    def test_load_invalid_toml_file(self):
        """Test that loading an invalid TOML file raises an error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "invalid.toml"

            # Write invalid TOML content
            with open(config_file, "w") as f:
                f.write("invalid toml syntax [[[")

            # Should raise ValueError
            with pytest.raises(ValueError, match="Error loading configuration"):
                LLMBackendConfiguration.load_from_file(str(config_file))

    def test_toml_save_and_load_options_explicitly_set_flags(self):
        """Test that options_explicitly_set flags are determined by presence of options key in TOML.

        Note: The options_explicitly_set flags are not stored/loaded from TOML.
        They are computed dynamically based on whether the 'options' or 'options_for_noedit'
        keys are present in the loaded configuration data.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_explicit_flags.toml"

            # Create configuration with options set for gemini but not for qwen
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").options = ["--test-opt"]
            config.get_backend_config("gemini").options_for_noedit = ["--noedit-opt"]
            # qwen has no options set (empty list from default)

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Gemini should have flags set because options keys are present in saved TOML
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.options_explicitly_set is True
            assert gemini_config.options_for_noedit_explicitly_set is True
            assert gemini_config.options == ["--test-opt"]
            assert gemini_config.options_for_noedit == ["--noedit-opt"]

            # Qwen should also have flags set because save_to_file writes all fields
            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.options_explicitly_set is True
            assert qwen_config.options_for_noedit_explicitly_set is True

    def test_backward_compatibility_old_toml_without_explicitly_set_flags(self):
        """Test loading TOML files with options - flags are determined by presence of options key.

        Note: When options/options_for_noedit keys exist in TOML, the corresponding
        explicitly_set flags will be True. This is the correct behavior for option
        inheritance - if a config explicitly sets options (even to a value), it should
        not inherit from parent.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config_no_explicit_flags.toml"

            # Create a TOML file with the old structure (without explicit set flags)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router"],
                        "temperature": 0.7,
                        "options": ["option1"],
                        "options_for_noedit": ["noedit1"],
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # When options/options_for_noedit keys exist in TOML, flags should be True
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.options_explicitly_set is True
            assert qwen_config.options_for_noedit_explicitly_set is True
            assert qwen_config.options == ["option1"]
            assert qwen_config.options_for_noedit == ["noedit1"]
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            # Gemini doesn't have options key in TOML, so flags should be False
            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.options_explicitly_set is False
            assert gemini_config.options_for_noedit_explicitly_set is False
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30


class TestGlobalConfigInstance:
    """Test cases for global configuration instance."""

    def test_get_llm_config_creates_instance(self):
        """Test that get_llm_config creates a global instance."""
        reset_llm_config()
        config = get_llm_config()
        assert config is not None
        assert isinstance(config, LLMBackendConfiguration)

    def test_get_llm_config_returns_same_instance(self):
        """Test that get_llm_config returns the same instance."""
        reset_llm_config()
        config1 = get_llm_config()
        config2 = get_llm_config()
        assert config1 is config2

    def test_get_llm_config_applies_env_overrides(self):
        """Test that get_llm_config applies environment overrides."""
        reset_llm_config()

        with patch.dict(os.environ, {"AUTO_CODER_GEMINI_API_KEY": "test_key"}):
            config = get_llm_config()

        assert config.get_backend_config("gemini").api_key == "test_key"

    def test_reset_llm_config(self):
        """Test that reset_llm_config clears the global instance."""
        config1 = get_llm_config()
        assert config1 is not None

        reset_llm_config()
        config2 = get_llm_config()

        # Should be a new instance
        assert config2 is not config1

    @patch.dict(os.environ, {"AUTO_CODER_GEMINI_API_KEY": "test_key"}, clear=True)
    def test_get_llm_config_with_env(self):
        """Test get_llm_config with environment variables."""
        reset_llm_config()
        config = get_llm_config()
        assert config.get_backend_config("gemini").api_key == "test_key"


class TestConfigErrorHandling:
    """Test error handling and edge cases."""

    def test_load_from_file_permission_error(self):
        """Test handling of permission errors when loading configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path as PathClass

            config_file = PathClass(tmpdir) / "test_config.toml"

            # Create a valid config file first
            config = LLMBackendConfiguration()
            config.save_to_file(str(config_file))

            # Now mock toml.load to raise PermissionError during loading
            with patch("toml.load", side_effect=PermissionError("Permission denied")):
                with pytest.raises(ValueError, match="Error loading configuration"):
                    LLMBackendConfiguration.load_from_file(str(config_file))

    def test_save_to_file_with_invalid_path(self):
        """Test that save handles invalid paths gracefully."""
        config = LLMBackendConfiguration()

        # This should raise an error for an invalid path
        with pytest.raises((OSError, FileNotFoundError)):
            config.save_to_file("/nonexistent/path/that/does/not/exist/config.toml")

    def test_backend_config_edge_cases(self):
        """Test edge cases in BackendConfig."""
        # Test with all None values
        config = BackendConfig(
            name="test",
            model=None,
            api_key=None,
            base_url=None,
            temperature=None,
            timeout=None,
            max_retries=None,
            openai_api_key=None,
            openai_base_url=None,
        )
        assert config.model is None
        assert config.api_key is None

    def test_configuration_with_empty_order(self):
        """Test configuration with empty backend order."""
        config = LLMBackendConfiguration()
        config.backend_order = []

        # Should still return enabled backends
        active = config.get_active_backends()
        # All backends should be enabled by default
        assert len(active) > 0

    def test_configuration_all_backends_disabled(self):
        """Test configuration when all backends are disabled."""
        config = LLMBackendConfiguration()
        for backend in config.backends.values():
            backend.enabled = False

        active = config.get_active_backends()
        assert len(active) == 0


class TestResolveConfigPath:
    """Test cases for resolve_config_path function."""

    def test_explicit_path_takes_priority(self):
        """Test that explicitly provided path takes highest priority."""
        with tempfile.TemporaryDirectory() as tmpdir:
            explicit_path = os.path.join(tmpdir, "explicit_config.toml")
            result = resolve_config_path(explicit_path)

            # Should return the absolute path of the explicit path
            assert result == os.path.abspath(explicit_path)

    def test_explicit_relative_path_converted_to_absolute(self):
        """Test that explicit relative paths are converted to absolute paths."""
        relative_path = "config/llm_config.toml"
        result = resolve_config_path(relative_path)

        # Should return an absolute path
        assert os.path.isabs(result)
        # Should end with the provided relative path
        assert result.endswith("config/llm_config.toml")

    def test_explicit_path_with_tilde_expansion(self):
        """Test that explicit paths with ~ are expanded."""
        explicit_path = "~/custom_config.toml"
        result = resolve_config_path(explicit_path)

        # Should not contain tilde
        assert "~" not in result
        # Should be absolute
        assert os.path.isabs(result)
        # Should contain expanded home directory
        assert result.startswith(os.path.expanduser("~"))

    def test_local_config_priority_over_home(self):
        """Test that local .auto-coder/llm_config.toml takes priority over home config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a local config directory and file
            local_config_dir = os.path.join(tmpdir, ".auto-coder")
            os.makedirs(local_config_dir, exist_ok=True)
            local_config_file = os.path.join(local_config_dir, "llm_config.toml")
            Path(local_config_file).touch()

            # Change to the temp directory
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                result = resolve_config_path()

                # Should return the local config path
                assert result == os.path.abspath(local_config_file)
                assert ".auto-coder" in result
                assert result.endswith("llm_config.toml")
            finally:
                os.chdir(original_cwd)

    def test_home_config_fallback_when_no_local(self):
        """Test that home config is used when no local config exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Change to a directory without local config
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                result = resolve_config_path()

                # Should return the home config path
                expected_home = os.path.abspath(os.path.expanduser("~/.auto-coder/llm_config.toml"))
                assert result == expected_home
            finally:
                os.chdir(original_cwd)

    def test_all_three_priorities_in_order(self):
        """Test all three priority levels in a single scenario."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a local config
            local_config_dir = os.path.join(tmpdir, ".auto-coder")
            os.makedirs(local_config_dir, exist_ok=True)
            local_config_file = os.path.join(local_config_dir, "llm_config.toml")
            Path(local_config_file).touch()

            # Create an explicit path
            explicit_path = os.path.join(tmpdir, "explicit.toml")

            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)

                # Priority 1: Explicit path
                result1 = resolve_config_path(explicit_path)
                assert result1 == os.path.abspath(explicit_path)

                # Priority 2: Local config (when no explicit path)
                result2 = resolve_config_path()
                assert result2 == os.path.abspath(local_config_file)

                # Priority 3: Remove local config and verify home fallback
                os.remove(local_config_file)
                os.rmdir(local_config_dir)
                result3 = resolve_config_path()
                expected_home = os.path.abspath(os.path.expanduser("~/.auto-coder/llm_config.toml"))
                assert result3 == expected_home
            finally:
                os.chdir(original_cwd)

    def test_returns_absolute_paths(self):
        """Test that function always returns absolute paths."""
        # Test with explicit relative path
        result1 = resolve_config_path("relative/path.toml")
        assert os.path.isabs(result1)

        # Test with no arguments
        result2 = resolve_config_path()
        assert os.path.isabs(result2)

        # Test with tilde path
        result3 = resolve_config_path("~/config.toml")
        assert os.path.isabs(result3)

    def test_handles_none_explicitly(self):
        """Test that None is handled correctly."""
        result = resolve_config_path(None)
        assert os.path.isabs(result)
        # Should fall back to local or home config
        assert "llm_config.toml" in result

    def test_local_config_detection_case_sensitivity(self):
        """Test that local config detection works correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Test without local config
            original_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                result_no_local = resolve_config_path()

                # Should be home config
                assert "~" not in result_no_local  # Should be expanded
                expected_home = os.path.abspath(os.path.expanduser("~/.auto-coder/llm_config.toml"))
                assert result_no_local == expected_home

                # Create local config
                local_config_dir = os.path.join(tmpdir, ".auto-coder")
                os.makedirs(local_config_dir, exist_ok=True)
                local_config_file = os.path.join(local_config_dir, "llm_config.toml")
                Path(local_config_file).touch()

                result_with_local = resolve_config_path()

                # Should now be local config
                assert result_with_local == os.path.abspath(local_config_file)
                assert tmpdir in result_with_local
            finally:
                os.chdir(original_cwd)

    def test_empty_string_treated_as_relative_path(self):
        """Test that empty string is treated as a relative path."""
        result = resolve_config_path("")
        # Empty string becomes current directory when converted to absolute
        assert os.path.isabs(result)

    def test_windows_style_path_handling(self):
        """Test that function handles Windows-style paths correctly."""
        # This test works on all platforms but validates path normalization
        if os.name == "nt":  # Windows
            result = resolve_config_path("C:\\Users\\test\\config.toml")
            assert os.path.isabs(result)
            assert result.startswith("C:")
        else:  # Unix-like
            # Just verify absolute path handling works
            result = resolve_config_path("/tmp/config.toml")
            assert os.path.isabs(result)
            assert result == "/tmp/config.toml"

    def test_nested_relative_path_explicit(self):
        """Test nested relative paths in explicit argument."""
        nested_path = "a/b/c/config.toml"
        result = resolve_config_path(nested_path)

        assert os.path.isabs(result)
        assert result.endswith("a/b/c/config.toml") or result.endswith("a\\b\\c\\config.toml")


class TestConfigurationPriorityLogic:
    """Test cases for configuration file priority logic with load_from_file()."""

    def test_load_from_file_with_no_config_files(self):
        """Test that load_from_file() creates default config when no files exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an isolated home config path
            isolated_home = os.path.join(tmpdir, "home", ".auto-coder", "llm_config.toml")

            original_cwd = os.getcwd()
            try:
                # Change to a directory without local config
                work_dir = os.path.join(tmpdir, "work")
                os.makedirs(work_dir, exist_ok=True)
                os.chdir(work_dir)

                # Load configuration (should create default at home path)
                config = LLMBackendConfiguration.load_from_file(isolated_home)

                # Verify default configuration
                assert config.default_backend == "codex"
                assert "gemini" in config.backends
                assert "codex" in config.backends
                assert "qwen" in config.backends

                # Verify file was created at the specified path
                assert os.path.exists(isolated_home)
            finally:
                os.chdir(original_cwd)

    def test_load_from_file_with_only_home_config(self):
        """Test that load_from_file() loads from home config when only home config exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create home config with specific settings
            home_config_dir = os.path.join(tmpdir, "home", ".auto-coder")
            os.makedirs(home_config_dir, exist_ok=True)
            home_config_file = os.path.join(home_config_dir, "llm_config.toml")

            # Create config with specific values
            config = LLMBackendConfiguration()
            config.default_backend = "gemini"
            config.get_backend_config("gemini").model = "home-gemini-model"
            config.get_backend_config("gemini").api_key = "home-api-key"
            config.save_to_file(home_config_file)

            original_cwd = os.getcwd()
            try:
                # Change to work directory without local config
                work_dir = os.path.join(tmpdir, "work")
                os.makedirs(work_dir, exist_ok=True)
                os.chdir(work_dir)

                # Mock the home directory path resolution
                with patch("os.path.expanduser") as mock_expand:

                    def side_effect(path):
                        if path.startswith("~"):
                            return path.replace("~", os.path.join(tmpdir, "home"))
                        return path

                    mock_expand.side_effect = side_effect

                    # Load configuration
                    loaded_config = LLMBackendConfiguration.load_from_file()

                    # Verify it loaded from home config
                    assert loaded_config.default_backend == "gemini"
                    assert loaded_config.get_backend_config("gemini").model == "home-gemini-model"
                    assert loaded_config.get_backend_config("gemini").api_key == "home-api-key"
            finally:
                os.chdir(original_cwd)

    def test_load_from_file_with_only_local_config(self):
        """Test that load_from_file() loads from local config when only local config exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create local config with specific settings
            local_config_dir = os.path.join(tmpdir, ".auto-coder")
            os.makedirs(local_config_dir, exist_ok=True)
            local_config_file = os.path.join(local_config_dir, "llm_config.toml")

            # Create config with specific values
            config = LLMBackendConfiguration()
            config.default_backend = "qwen"
            config.get_backend_config("qwen").model = "local-qwen-model"
            config.get_backend_config("qwen").api_key = "local-api-key"
            config.backend_order = ["qwen", "gemini"]
            config.save_to_file(local_config_file)

            original_cwd = os.getcwd()
            try:
                # Change to the directory with local config
                os.chdir(tmpdir)

                # Load configuration
                loaded_config = LLMBackendConfiguration.load_from_file()

                # Verify it loaded from local config
                assert loaded_config.default_backend == "qwen"
                assert loaded_config.get_backend_config("qwen").model == "local-qwen-model"
                assert loaded_config.get_backend_config("qwen").api_key == "local-api-key"
                assert loaded_config.backend_order == ["qwen", "gemini"]
            finally:
                os.chdir(original_cwd)

    def test_load_from_file_local_config_prioritized_over_home(self):
        """Test that local config takes priority over home config when both exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create home config
            home_config_dir = os.path.join(tmpdir, "home", ".auto-coder")
            os.makedirs(home_config_dir, exist_ok=True)
            home_config_file = os.path.join(home_config_dir, "llm_config.toml")

            home_config = LLMBackendConfiguration()
            home_config.default_backend = "gemini"
            home_config.get_backend_config("gemini").model = "home-model"
            home_config.get_backend_config("gemini").temperature = 0.5
            home_config.save_to_file(home_config_file)

            # Create local config with different values
            local_config_dir = os.path.join(tmpdir, "work", ".auto-coder")
            os.makedirs(local_config_dir, exist_ok=True)
            local_config_file = os.path.join(local_config_dir, "llm_config.toml")

            local_config = LLMBackendConfiguration()
            local_config.default_backend = "codex"
            local_config.get_backend_config("codex").model = "local-model"
            local_config.get_backend_config("codex").temperature = 0.8
            local_config.backend_order = ["codex", "qwen"]
            local_config.save_to_file(local_config_file)

            original_cwd = os.getcwd()
            try:
                # Change to work directory with local config
                os.chdir(os.path.join(tmpdir, "work"))

                # Mock expanduser to use our test home
                with patch("os.path.expanduser") as mock_expand:

                    def side_effect(path):
                        if path.startswith("~"):
                            return path.replace("~", os.path.join(tmpdir, "home"))
                        return path

                    mock_expand.side_effect = side_effect

                    # Load configuration
                    loaded_config = LLMBackendConfiguration.load_from_file()

                    # Verify it loaded from local config, NOT home config
                    assert loaded_config.default_backend == "codex"
                    assert loaded_config.get_backend_config("codex").model == "local-model"
                    assert loaded_config.get_backend_config("codex").temperature == 0.8
                    assert loaded_config.backend_order == ["codex", "qwen"]

                    # Verify home config values were NOT loaded
                    assert loaded_config.default_backend != "gemini"
                    gemini_config = loaded_config.get_backend_config("gemini")
                    assert gemini_config.model != "home-model"
            finally:
                os.chdir(original_cwd)

    def test_load_from_file_explicit_path_overrides_both_local_and_home(self):
        """Test that explicit path overrides both local and home configs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create home config
            home_config_dir = os.path.join(tmpdir, "home", ".auto-coder")
            os.makedirs(home_config_dir, exist_ok=True)
            home_config_file = os.path.join(home_config_dir, "llm_config.toml")

            home_config = LLMBackendConfiguration()
            home_config.default_backend = "gemini"
            home_config.get_backend_config("gemini").model = "home-model"
            home_config.save_to_file(home_config_file)

            # Create local config
            local_config_dir = os.path.join(tmpdir, "work", ".auto-coder")
            os.makedirs(local_config_dir, exist_ok=True)
            local_config_file = os.path.join(local_config_dir, "llm_config.toml")

            local_config = LLMBackendConfiguration()
            local_config.default_backend = "codex"
            local_config.get_backend_config("codex").model = "local-model"
            local_config.save_to_file(local_config_file)

            # Create explicit config with different values
            explicit_config_file = os.path.join(tmpdir, "custom", "my_config.toml")
            os.makedirs(os.path.dirname(explicit_config_file), exist_ok=True)

            explicit_config = LLMBackendConfiguration()
            explicit_config.default_backend = "qwen"
            explicit_config.get_backend_config("qwen").model = "explicit-model"
            explicit_config.get_backend_config("qwen").api_key = "explicit-key"
            explicit_config.backend_order = ["qwen"]
            explicit_config.save_to_file(explicit_config_file)

            original_cwd = os.getcwd()
            try:
                # Change to work directory (which has local config)
                os.chdir(os.path.join(tmpdir, "work"))

                # Load with explicit path
                loaded_config = LLMBackendConfiguration.load_from_file(explicit_config_file)

                # Verify it loaded from explicit path, NOT local or home
                assert loaded_config.default_backend == "qwen"
                assert loaded_config.get_backend_config("qwen").model == "explicit-model"
                assert loaded_config.get_backend_config("qwen").api_key == "explicit-key"
                assert loaded_config.backend_order == ["qwen"]

                # Verify it did NOT load local or home values
                assert loaded_config.default_backend != "codex"
                assert loaded_config.default_backend != "gemini"
            finally:
                os.chdir(original_cwd)

    def test_load_from_file_values_correctly_loaded_from_prioritized_file(self):
        """Test that configuration values are correctly loaded from the prioritized file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create local config with comprehensive settings
            local_config_dir = os.path.join(tmpdir, ".auto-coder")
            os.makedirs(local_config_dir, exist_ok=True)
            local_config_file = os.path.join(local_config_dir, "llm_config.toml")

            # Create config with various settings to test
            config = LLMBackendConfiguration()
            config.default_backend = "gemini"
            config.backend_order = ["gemini", "qwen", "codex"]
            config.backend_for_noedit_order = ["codex"]
            config.backend_for_noedit_default = "codex"

            # Set detailed backend configs
            config.get_backend_config("gemini").model = "gemini-test-model"
            config.get_backend_config("gemini").api_key = "test-gemini-key"
            config.get_backend_config("gemini").temperature = 0.7
            config.get_backend_config("gemini").timeout = 60
            config.get_backend_config("gemini").max_retries = 5
            config.get_backend_config("gemini").providers = ["gemini-provider-1", "gemini-provider-2"]
            config.get_backend_config("gemini").usage_limit_retry_count = 3
            config.get_backend_config("gemini").usage_limit_retry_wait_seconds = 30
            config.get_backend_config("gemini").options = ["option1", "option2"]
            config.get_backend_config("gemini").backend_type = "custom_gemini"
            config.get_backend_config("gemini").always_switch_after_execution = True

            config.get_backend_config("qwen").enabled = False
            config.get_backend_config("qwen").model = "qwen-test-model"
            config.get_backend_config("qwen").extra_args = {"QWEN_CUSTOM": "value123"}

            config.save_to_file(local_config_file)

            original_cwd = os.getcwd()
            try:
                # Change to the directory with local config
                os.chdir(tmpdir)

                # Load configuration
                loaded_config = LLMBackendConfiguration.load_from_file()

                # Verify all values were correctly loaded
                assert loaded_config.default_backend == "gemini"
                assert loaded_config.backend_order == ["gemini", "qwen", "codex"]
                assert loaded_config.backend_for_noedit_order == ["codex"]
                assert loaded_config.backend_for_noedit_default == "codex"

                # Verify gemini backend settings
                gemini = loaded_config.get_backend_config("gemini")
                assert gemini.model == "gemini-test-model"
                assert gemini.api_key == "test-gemini-key"
                assert gemini.temperature == 0.7
                assert gemini.timeout == 60
                assert gemini.max_retries == 5
                assert gemini.providers == ["gemini-provider-1", "gemini-provider-2"]
                assert gemini.usage_limit_retry_count == 3
                assert gemini.usage_limit_retry_wait_seconds == 30
                assert gemini.options == ["option1", "option2"]
                assert gemini.backend_type == "custom_gemini"
                assert gemini.always_switch_after_execution is True

                # Verify qwen backend settings
                qwen = loaded_config.get_backend_config("qwen")
                assert qwen.enabled is False
                assert qwen.model == "qwen-test-model"
                assert qwen.extra_args == {"QWEN_CUSTOM": "value123"}
            finally:
                os.chdir(original_cwd)

    def test_priority_logic_with_different_working_directories(self):
        """Test that priority logic works correctly when changing working directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create two different project directories with their own local configs
            project1_dir = os.path.join(tmpdir, "project1")
            project2_dir = os.path.join(tmpdir, "project2")
            os.makedirs(project1_dir, exist_ok=True)
            os.makedirs(project2_dir, exist_ok=True)

            # Project 1 config
            project1_config_dir = os.path.join(project1_dir, ".auto-coder")
            os.makedirs(project1_config_dir, exist_ok=True)
            project1_config_file = os.path.join(project1_config_dir, "llm_config.toml")

            project1_config = LLMBackendConfiguration()
            project1_config.default_backend = "gemini"
            project1_config.get_backend_config("gemini").model = "project1-model"
            project1_config.save_to_file(project1_config_file)

            # Project 2 config
            project2_config_dir = os.path.join(project2_dir, ".auto-coder")
            os.makedirs(project2_config_dir, exist_ok=True)
            project2_config_file = os.path.join(project2_config_dir, "llm_config.toml")

            project2_config = LLMBackendConfiguration()
            project2_config.default_backend = "qwen"
            project2_config.get_backend_config("qwen").model = "project2-model"
            project2_config.save_to_file(project2_config_file)

            original_cwd = os.getcwd()
            try:
                # Load from project 1
                os.chdir(project1_dir)
                config1 = LLMBackendConfiguration.load_from_file()
                assert config1.default_backend == "gemini"
                assert config1.get_backend_config("gemini").model == "project1-model"

                # Load from project 2
                os.chdir(project2_dir)
                config2 = LLMBackendConfiguration.load_from_file()
                assert config2.default_backend == "qwen"
                assert config2.get_backend_config("qwen").model == "project2-model"

                # Verify configs are different
                assert config1.default_backend != config2.default_backend
            finally:
                os.chdir(original_cwd)

    def test_backward_compatibility_existing_behavior_unchanged(self):
        """Test that existing behavior is unchanged for backward compatibility."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a config file
            config_file = os.path.join(tmpdir, "test_config.toml")

            # Create and save a config
            original_config = LLMBackendConfiguration()
            original_config.default_backend = "gemini"
            original_config.get_backend_config("gemini").model = "test-model"
            original_config.backend_order = ["gemini", "codex"]
            original_config.save_to_file(config_file)

            # Load it back with explicit path (this was always supported)
            loaded_config = LLMBackendConfiguration.load_from_file(config_file)

            # Verify it matches
            assert loaded_config.default_backend == original_config.default_backend
            assert loaded_config.backend_order == original_config.backend_order
            assert loaded_config.get_backend_config("gemini").model == original_config.get_backend_config("gemini").model

            # Test that loading non-existent file creates default (existing behavior)
            nonexistent_file = os.path.join(tmpdir, "nonexistent.toml")
            default_config = LLMBackendConfiguration.load_from_file(nonexistent_file)

            assert default_config.default_backend == "codex"
            assert os.path.exists(nonexistent_file)

    def test_toml_save_and_load_model_provider(self):
        """Test that model_provider field is properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_model_provider.toml"

            # Create configuration with model_provider settings
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").model_provider = "openrouter"
            config.get_backend_config("qwen").model_provider = "anthropic"
            config.get_backend_config("codex").model_provider = None

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify model_provider was persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.model_provider == "openrouter"

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.model_provider == "anthropic"

            codex_config = loaded_config.get_backend_config("codex")
            assert codex_config.model_provider is None

    def test_load_from_file_with_model_provider(self):
        """Test loading a TOML file with model_provider field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "model_provider_config.toml"
            data = {
                "backend": {"default": "grok-4.1-fast", "order": ["grok-4.1-fast"]},
                "backends": {
                    "grok-4.1-fast": {
                        "enabled": True,
                        "model": "x-ai/grok-4.1-fast:free",
                        "backend_type": "codex",
                        "model_provider": "openrouter",
                    }
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))
            grok_config = config.get_backend_config("grok-4.1-fast")

            assert grok_config is not None
            assert grok_config.model == "x-ai/grok-4.1-fast:free"
            assert grok_config.backend_type == "codex"
            assert grok_config.model_provider == "openrouter"

    def test_load_from_file_parses_dotted_keys_with_model_provider(self):
        """Test that dotted keys with model_provider are correctly identified as backends."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "dotted_model_provider.toml"
            # This structure mimics [grok-4.1-fast] which TOML parses as nested dicts
            data = {"grok-4": {"1-fast": {"enabled": True, "model": "x-ai/grok-4.1-fast:free", "backend_type": "codex", "model_provider": "openrouter"}}}
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify grok-4.1-fast was found and has model_provider
            grok_config = config.get_backend_config("grok-4.1-fast")
            assert grok_config is not None
            assert grok_config.backend_type == "codex"
            assert grok_config.model == "x-ai/grok-4.1-fast:free"
            assert grok_config.model_provider == "openrouter"

    def test_backward_compatibility_old_toml_without_model_provider(self):
        """Test loading old TOML files that don't have model_provider field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_without_model_provider.toml"

            # Create a TOML file with the old structure (without model_provider field)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "backend_type": "codex",
                        "temperature": 0.7,
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify model_provider has default None value when not present in old TOML
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.model_provider is None
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.model_provider is None
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30

    def test_enabled_defaults_to_true(self):
        """Test that enabled defaults to true when not specified in configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "llm_config.toml"
            config_file.write_text(
                """
[backend]
default = "test-backend"

[backends.test-backend]
model = "test-model"
backend_type = "codex"
"""
            )

            config = LLMBackendConfiguration.load_from_file(str(config_file))
            backend_config = config.get_backend_config("test-backend")

            assert backend_config is not None
            assert backend_config.enabled is True

    def test_explicit_enabled_false(self):
        """Test that explicit enabled = false is respected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "llm_config.toml"
            config_file.write_text(
                """
[backend]
default = "test-backend"

[backends.test-backend]
model = "test-model"
backend_type = "codex"
enabled = false
"""
            )

            config = LLMBackendConfiguration.load_from_file(str(config_file))
            backend_config = config.get_backend_config("test-backend")

            assert backend_config is not None
            assert backend_config.enabled is False

            # Verify get_active_backends excludes this backend
            active = config.get_active_backends()
            assert "test-backend" not in active

    def test_explicit_enabled_true(self):
        """Test that explicit enabled = true is respected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "llm_config.toml"
            config_file.write_text(
                """
[backend]
default = "test-backend"

[backends.test-backend]
model = "test-model"
backend_type = "codex"
enabled = true
"""
            )

            config = LLMBackendConfiguration.load_from_file(str(config_file))
            backend_config = config.get_backend_config("test-backend")

            assert backend_config is not None
            assert backend_config.enabled is True

            # Verify get_active_backends includes this backend
            active = config.get_active_backends()
            assert "test-backend" in active

    def test_toml_save_and_load_usage_markers(self):
        """Test that usage_markers field is properly saved and loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_usage_markers.toml"

            # Create configuration with usage_markers settings
            config = LLMBackendConfiguration()
            config.get_backend_config("gemini").usage_markers = ["marker1", "marker2"]
            config.get_backend_config("qwen").usage_markers = ["marker3"]

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify usage_markers was persisted
            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config.usage_markers == ["marker1", "marker2"]

            qwen_config = loaded_config.get_backend_config("qwen")
            assert qwen_config.usage_markers == ["marker3"]

    def test_backward_compatibility_old_toml_without_usage_markers(self):
        """Test loading old TOML files that don't have usage_markers field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config_usage_markers.toml"

            # Create a TOML file with the old structure (without usage_markers field)
            data = {
                "backend": {"default": "qwen", "order": ["qwen", "gemini"]},
                "backends": {
                    "qwen": {
                        "enabled": True,
                        "model": "qwen3-coder-plus",
                        "providers": ["qwen-open-router"],
                        "temperature": 0.7,
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify usage_markers has default empty list when not present in old TOML
            qwen_config = config.get_backend_config("qwen")
            assert qwen_config is not None
            assert qwen_config.usage_markers == []
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.usage_markers == []
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.timeout == 30


class TestBackendWithHighScore:
    """Test cases for backend_with_high_score configuration."""

    def test_backend_with_high_score_optional_in_initialization(self):
        """Test that backend_with_high_score is optional during initialization."""
        config = LLMBackendConfiguration()
        assert config.backend_with_high_score is None

    def test_backend_with_high_score_with_custom_values(self):
        """Test creating LLMBackendConfiguration with custom fallback backend."""
        fallback_backend = BackendConfig(
            name="fallback",
            enabled=True,
            model="fallback-model",
            api_key="fallback_key",
            temperature=0.5,
            backend_type="codex",
        )
        config = LLMBackendConfiguration(backend_with_high_score=fallback_backend)

        assert config.backend_with_high_score is not None
        assert config.backend_with_high_score.name == "fallback"
        assert config.backend_with_high_score.model == "fallback-model"
        assert config.backend_with_high_score.api_key == "fallback_key"
        assert config.backend_with_high_score.temperature == 0.5
        assert config.backend_with_high_score.backend_type == "codex"

    def test_save_and_load_backend_with_high_score(self):
        """Test saving and loading configuration with fallback backend."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_fallback_config.toml"

            # Create configuration with fallback backend
            config = LLMBackendConfiguration()
            config.backend_with_high_score = BackendConfig(
                name="gemini-fallback",
                enabled=True,
                model="gemini-2.0-flash",
                api_key="fallback_api_key",
                temperature=0.3,
                timeout=120,
                backend_type="gemini",
            )
            config.default_backend = "codex"

            # Save to file
            config.save_to_file(str(config_file))

            # Verify file was created
            assert config_file.exists()

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify fallback backend was persisted
            assert loaded_config.backend_with_high_score is not None
            fallback = loaded_config.backend_with_high_score
            assert fallback.name == "gemini-fallback"
            assert fallback.model == "gemini-2.0-flash"
            assert fallback.api_key == "fallback_api_key"
            assert fallback.temperature == 0.3
            assert fallback.timeout == 120
            assert fallback.backend_type == "gemini"
            assert fallback.enabled is True

    def test_load_toml_with_backend_with_high_score_section(self):
        """Test loading TOML file with [backend_with_high_score] section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "llm_config.toml"
            data = {
                "backend": {"default": "codex", "order": ["codex", "gemini"]},
                "backends": {
                    "codex": {
                        "enabled": True,
                        "model": "codex-default",
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                    },
                },
                "backend_with_high_score": {
                    "enabled": True,
                    "model": "gemini-2.0-flash",
                    "api_key": "fallback_key_123",
                    "temperature": 0.2,
                    "timeout": 60,
                    "backend_type": "gemini",
                    "providers": ["fallback-provider"],
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify fallback backend was loaded
            assert config.backend_with_high_score is not None
            fallback = config.backend_with_high_score
            assert fallback.model == "gemini-2.0-flash"
            assert fallback.api_key == "fallback_key_123"
            assert fallback.temperature == 0.2
            assert fallback.timeout == 60
            assert fallback.backend_type == "gemini"
            assert fallback.providers == ["fallback-provider"]
            assert fallback.enabled is True

    def test_load_toml_without_backend_with_high_score_section(self):
        """Test loading TOML file without [backend_with_high_score] section (backward compatibility)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "llm_config.toml"
            data = {
                "backend": {"default": "codex", "order": ["codex", "gemini"]},
                "backends": {
                    "codex": {
                        "enabled": True,
                        "model": "codex-default",
                    },
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify fallback backend is None when not in config
            assert config.backend_with_high_score is None

    def test_get_backend_with_high_score_method(self):
        """Test get_backend_with_high_score method."""
        config = LLMBackendConfiguration()
        assert config.get_backend_with_high_score() is None

        # Set fallback backend
        fallback = BackendConfig(name="test-fallback", model="test-model")
        config.backend_with_high_score = fallback

        # Verify method returns the fallback backend
        result = config.get_backend_with_high_score()
        assert result is fallback
        assert result.name == "test-fallback"
        assert result.model == "test-model"

    def test_get_model_for_backend_with_high_score_method(self):
        """Test get_model_for_backend_with_high_score method."""
        config = LLMBackendConfiguration()

        # No fallback backend configured
        assert config.get_model_for_backend_with_high_score() is None

        # Fallback backend without model
        config.backend_with_high_score = BackendConfig(name="test-fallback")
        assert config.get_model_for_backend_with_high_score() is None

        # Fallback backend with model
        config.backend_with_high_score = BackendConfig(name="test-fallback", model="fallback-model")
        assert config.get_model_for_backend_with_high_score() == "fallback-model"

    def test_save_to_file_without_backend_with_high_score(self):
        """Test that save_to_file works when backend_with_high_score is not configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_no_fallback.toml"

            # Create configuration without fallback backend
            config = LLMBackendConfiguration()
            config.default_backend = "gemini"
            config.get_backend_config("gemini").model = "gemini-pro"

            # Save to file
            config.save_to_file(str(config_file))

            # Verify file was created
            assert config_file.exists()

            # Load and verify
            with open(config_file, "r") as f:
                data = toml.load(f)

            # Verify no backend_with_high_score section in the file
            assert "backend_with_high_score" not in data
            assert data["backend"]["default"] == "gemini"

    def test_backend_with_high_score_all_fields(self):
        """Test that all BackendConfig fields are properly saved and loaded for fallback backend."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_all_fields_fallback.toml"

            # Create configuration with all fields set for fallback backend
            config = LLMBackendConfiguration()
            config.backend_with_high_score = BackendConfig(
                name="full-fallback",
                enabled=False,
                model="full-fallback-model",
                api_key="fallback_key",
                base_url="https://fallback.example.com",
                temperature=0.9,
                timeout=180,
                max_retries=10,
                openai_api_key="fallback_openai_key",
                openai_base_url="https://fallback.openai.example.com",
                extra_args={"FALLBACK_ARG": "value"},
                providers=["fallback-provider-1", "fallback-provider-2"],
                usage_limit_retry_count=5,
                usage_limit_retry_wait_seconds=45,
                options=["fallback-option1", "fallback-option2"],
                options_for_resume=["fallback-resume-opt1", "fallback-resume-opt2"],
                backend_type="custom_fallback",
                model_provider="fallback-provider",
                always_switch_after_execution=True,
                settings="fallback_settings.json",
                usage_markers=["fallback-marker1", "fallback-marker2"],
                options_for_noedit=["fallback-noedit1", "fallback-noedit2"],
            )

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify all fields were persisted
            fallback = loaded_config.backend_with_high_score
            assert fallback is not None
            assert fallback.enabled is False
            assert fallback.model == "full-fallback-model"
            assert fallback.api_key == "fallback_key"
            assert fallback.base_url == "https://fallback.example.com"
            assert fallback.temperature == 0.9
            assert fallback.timeout == 180
            assert fallback.max_retries == 10
            assert fallback.openai_api_key == "fallback_openai_key"
            assert fallback.openai_base_url == "https://fallback.openai.example.com"
            assert fallback.extra_args == {"FALLBACK_ARG": "value"}
            assert fallback.providers == ["fallback-provider-1", "fallback-provider-2"]
            assert fallback.usage_limit_retry_count == 5
            assert fallback.usage_limit_retry_wait_seconds == 45
            assert fallback.options == ["fallback-option1", "fallback-option2"]
            assert fallback.options_for_resume == ["fallback-resume-opt1", "fallback-resume-opt2"]
            assert fallback.backend_type == "custom_fallback"
            assert fallback.model_provider == "fallback-provider"
            assert fallback.always_switch_after_execution is True
            assert fallback.settings == "fallback_settings.json"
            assert fallback.usage_markers == ["fallback-marker1", "fallback-marker2"]
            assert fallback.options_for_noedit == ["fallback-noedit1", "fallback-noedit2"]

    def test_backend_with_high_score_minimal_config(self):
        """Test fallback backend with minimal configuration (only required fields)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_minimal_fallback.toml"

            # Create configuration with minimal fallback backend
            config = LLMBackendConfiguration()
            config.backend_with_high_score = BackendConfig(name="minimal-fallback")

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify minimal config was persisted with defaults
            fallback = loaded_config.backend_with_high_score
            assert fallback is not None
            assert fallback.name == "minimal-fallback"
            assert fallback.enabled is True  # Default value
            assert fallback.model is None  # Optional field
            assert fallback.api_key is None  # Optional field

    def test_backward_compatibility_message_backend(self):
        """Test that old message_backend config format is still supported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_format.toml"

            # Create a config file using the OLD format
            old_format_config = """
[backend]
default = "codex"
order = ["codex", "gemini"]

[message_backend]
default = "gemini"
order = ["gemini", "qwen"]

[backends.codex]
enabled = true
model = "codex"

[backends.gemini]
enabled = true
model = "gemini-2.5-pro"
"""
            config_file.write_text(old_format_config)

            with patch("src.auto_coder.llm_backend_config.get_logger") as mock_get_logger:
                mock_logger = MagicMock()
                mock_get_logger.return_value = mock_logger

                # Load the old format config
                loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify it was converted to new format
            assert loaded_config.backend_for_noedit_default == "gemini"
            assert loaded_config.backend_for_noedit_order == ["gemini", "qwen"]

            mock_logger.warning.assert_any_call("Configuration uses deprecated 'message_backend' key. Please update to 'backend_for_noedit' in your config file.")

            # Verify the deprecated methods still work
            assert loaded_config.get_noedit_default_backend() == "gemini"
            assert loaded_config.get_active_noedit_backends() == ["gemini", "qwen"]


class TestOptionInheritance:
    """Test cases for option inheritance from parent backends."""

    def test_options_inherited_from_parent_backend(self):
        """Test that options are inherited from parent backend when not explicitly set."""
        config_data = {
            "backends": {
                "codex": {
                    "enabled": True,
                    "options": ["--parent-opt1", "--parent-opt2"],
                    "options_for_noedit": ["--parent-noedit1"],
                },
                "child": {
                    "enabled": True,
                    "backend_type": "codex",
                    "model": "child-model",
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        child_config = config.get_backend_config("child")
        assert child_config is not None
        assert child_config.options == ["--parent-opt1", "--parent-opt2"]
        assert child_config.options_for_noedit == ["--parent-noedit1"]

    def test_options_not_inherited_when_explicitly_set(self):
        """Test that options are NOT inherited when explicitly set in child."""
        config_data = {
            "backends": {
                "codex": {
                    "enabled": True,
                    "options": ["--parent-opt1", "--parent-opt2"],
                    "options_for_noedit": ["--parent-noedit1"],
                },
                "child": {
                    "enabled": True,
                    "backend_type": "codex",
                    "model": "child-model",
                    "options": ["--child-opt1"],
                    "options_for_noedit": ["--child-noedit1"],
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        child_config = config.get_backend_config("child")
        assert child_config is not None
        assert child_config.options == ["--child-opt1"]
        assert child_config.options_for_noedit == ["--child-noedit1"]
        assert child_config.options_explicitly_set is True
        assert child_config.options_for_noedit_explicitly_set is True

    def test_options_explicitly_set_flag_detected(self):
        """Test that options_explicitly_set flag is correctly detected."""
        config_data = {
            "backends": {
                "parent": {
                    "enabled": True,
                    "options": ["--parent-opt"],
                },
                "child_with_options": {
                    "enabled": True,
                    "backend_type": "parent",
                    "options": [],
                },
                "child_without_options": {
                    "enabled": True,
                    "backend_type": "parent",
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        # Child with explicitly set options (even empty list)
        child_with = config.get_backend_config("child_with_options")
        assert child_with is not None
        assert child_with.options_explicitly_set is True
        assert child_with.options == []

        # Child without explicitly set options should inherit
        child_without = config.get_backend_config("child_without_options")
        assert child_without is not None
        assert child_without.options_explicitly_set is False
        assert child_without.options == ["--parent-opt"]

    def test_options_for_noedit_explicitly_set_flag_detected(self):
        """Test that options_for_noedit_explicitly_set flag is correctly detected."""
        config_data = {
            "backends": {
                "parent": {
                    "enabled": True,
                    "options_for_noedit": ["--parent-noedit"],
                },
                "child_with_noedit": {
                    "enabled": True,
                    "backend_type": "parent",
                    "options_for_noedit": [],
                },
                "child_without_noedit": {
                    "enabled": True,
                    "backend_type": "parent",
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        # Child with explicitly set options_for_noedit (even empty list)
        child_with = config.get_backend_config("child_with_noedit")
        assert child_with is not None
        assert child_with.options_for_noedit_explicitly_set is True
        assert child_with.options_for_noedit == []

        # Child without explicitly set options_for_noedit should inherit
        child_without = config.get_backend_config("child_without_noedit")
        assert child_without is not None
        assert child_without.options_for_noedit_explicitly_set is False
        assert child_without.options_for_noedit == ["--parent-noedit"]

    def test_no_inheritance_when_parent_not_found(self):
        """Test that no inheritance occurs when parent backend doesn't exist."""
        config_data = {
            "backends": {
                "child": {
                    "enabled": True,
                    "backend_type": "nonexistent_parent",
                    "model": "child-model",
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        child_config = config.get_backend_config("child")
        assert child_config is not None
        assert child_config.options == []
        assert child_config.options_for_noedit == []

    def test_inheritance_with_load_from_file(self):
        """Test that option inheritance works with load_from_file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "inheritance_config.toml"
            data = {
                "backend": {"default": "child", "order": ["child", "parent"]},
                "backends": {
                    "parent": {
                        "enabled": True,
                        "options": ["--parent-flag"],
                        "options_for_noedit": ["--parent-noedit-flag"],
                    },
                    "child": {
                        "enabled": True,
                        "backend_type": "parent",
                        "model": "child-model",
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))

            child_config = config.get_backend_config("child")
            assert child_config is not None
            assert child_config.backend_type == "parent"
            assert child_config.options == ["--parent-flag"]
            assert child_config.options_for_noedit == ["--parent-noedit-flag"]
            assert child_config.options_explicitly_set is False
            assert child_config.options_for_noedit_explicitly_set is False

    def test_partial_inheritance(self):
        """Test that only non-explicitly set options are inherited."""
        config_data = {
            "backends": {
                "parent": {
                    "enabled": True,
                    "options": ["--parent-opt"],
                    "options_for_noedit": ["--parent-noedit"],
                },
                "child": {
                    "enabled": True,
                    "backend_type": "parent",
                    "options": ["--child-opt"],
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        child_config = config.get_backend_config("child")
        assert child_config is not None
        assert child_config.options == ["--child-opt"]
        assert child_config.options_for_noedit == ["--parent-noedit"]
        assert child_config.options_explicitly_set is True
        assert child_config.options_for_noedit_explicitly_set is False

    def test_inheritance_creates_copy_not_reference(self):
        """Test that inherited options are copied, not referenced."""
        config_data = {
            "backends": {
                "parent": {
                    "enabled": True,
                    "options": ["--parent-opt"],
                },
                "child": {
                    "enabled": True,
                    "backend_type": "parent",
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        parent_config = config.get_backend_config("parent")
        child_config = config.get_backend_config("child")

        assert parent_config is not None
        assert child_config is not None

        child_config.options.append("--child-added-opt")
        assert "--child-added-opt" not in parent_config.options

    def test_nested_inheritance_not_supported(self):
        """Test that grandparent inheritance is not directly supported (only immediate parent)."""
        config_data = {
            "backends": {
                "grandparent": {
                    "enabled": True,
                    "options": ["--grandparent-opt"],
                },
                "parent": {
                    "enabled": True,
                    "backend_type": "grandparent",
                },
                "child": {
                    "enabled": True,
                    "backend_type": "parent",
                },
            }
        }

        config = LLMBackendConfiguration.load_from_dict(config_data)

        parent_config = config.get_backend_config("parent")
        child_config = config.get_backend_config("child")

        assert parent_config.options == ["--grandparent-opt"]
        assert child_config.options == ["--grandparent-opt"]
