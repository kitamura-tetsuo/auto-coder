"""Tests for llm_backend_config module."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

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
        assert config.backend_type is None
        assert config.model_provider is None
        assert config.always_switch_after_execution is False

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
            backend_type="custom_type",
            model_provider="openrouter",
            always_switch_after_execution=True,
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
        assert config.backend_type == "custom_type"
        assert config.model_provider == "openrouter"
        assert config.always_switch_after_execution is True

    def test_backend_config_extra_args_default(self):
        """Test that extra_args has a proper default factory."""
        config1 = BackendConfig(name="test1")
        config2 = BackendConfig(name="test2")

        # Modifying one shouldn't affect the other
        config1.extra_args["test"] = "value"
        assert "test" not in config2.extra_args


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
        assert config.message_backend_order == []
        assert config.message_default_backend is None
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
            message_backend_order=["codex"],
            message_default_backend="codex",
        )

        assert config.default_backend == "gemini"
        assert config.backend_order == ["gemini", "codex"]
        assert config.message_backend_order == ["codex"]
        assert config.message_default_backend == "codex"
        assert config.backends["gemini"].model == "gemini-pro"
        assert config.backends["codex"].enabled is False

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

    def test_get_active_message_backends(self):
        """Test getting active message backends."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = False
        config.get_backend_config("codex").enabled = True
        config.get_backend_config("qwen").enabled = False
        config.message_backend_order = ["codex", "gemini", "qwen"]

        active = config.get_active_message_backends()
        assert "codex" in active
        assert "gemini" not in active
        assert "qwen" not in active

    def test_get_active_message_backends_fallback(self):
        """Test message backends fall back to general backends when not configured."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = False
        config.get_backend_config("codex").enabled = True
        config.backend_order = ["codex", "gemini", "qwen"]
        # No message_backend_order set

        active = config.get_active_message_backends()
        assert "codex" in active
        # Should fall back to general backends

    def test_get_message_default_backend(self):
        """Test getting default message backend."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").enabled = True
        config.message_default_backend = "codex"
        config.default_backend = "gemini"

        assert config.get_message_default_backend() == "codex"

    def test_get_message_default_backend_fallback(self):
        """Test message default falls back to general default."""
        config = LLMBackendConfiguration()
        config.get_backend_config("gemini").enabled = True
        config.default_backend = "gemini"

        # No message_default_backend set
        assert config.get_message_default_backend() == "gemini"

    def test_get_message_default_backend_disabled(self):
        """Test that disabled message default falls back to general default."""
        config = LLMBackendConfiguration()
        config.get_backend_config("codex").enabled = False
        config.message_default_backend = "codex"
        config.default_backend = "gemini"

        # message_default_backend is disabled, should fall back
        assert config.get_message_default_backend() == "gemini"

    def test_has_dual_configuration(self):
        """Test detection of dual backend configuration."""
        config1 = LLMBackendConfiguration()
        config1.message_backend_order = ["codex"]
        assert config1.has_dual_configuration() is True

        config2 = LLMBackendConfiguration()
        config2.backend_order = ["gemini", "codex"]
        config2.message_backend_order = ["codex"]
        assert config2.has_dual_configuration() is True

        config3 = LLMBackendConfiguration()
        # Only general config
        assert config3.has_dual_configuration() is False

        config4 = LLMBackendConfiguration()
        # Only message config
        config4.message_backend_order = ["codex"]
        config4.message_default_backend = "codex"
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
                "AUTO_CODER_MESSAGE_DEFAULT_BACKEND": "claude",
            },
        ):
            config.apply_env_overrides()

        # Check that environment overrides were applied
        assert config.get_backend_config("gemini").api_key == "env_gemini_key"
        assert config.default_backend == "qwen"
        assert config.message_default_backend == "claude"

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
            assert qwen_config.backend_type is None
            assert qwen_config.always_switch_after_execution is False
            assert qwen_config.model == "qwen3-coder-plus"
            assert qwen_config.temperature == 0.7

            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.options == []
            assert gemini_config.backend_type is None
            assert gemini_config.always_switch_after_execution is False
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

    def test_toml_save_and_load_model_provider(self):
        """Test that model_provider field is properly saved and loaded from TOML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_model_provider.toml"

            # Create configuration with model_provider settings
            config = LLMBackendConfiguration()
            # Use existing backends and add model_provider to them
            config.get_backend_config("codex").model_provider = "openrouter"
            config.get_backend_config("codex").model = "x-ai/grok-4.1-fast:free"
            config.get_backend_config("gemini").model_provider = "anthropic"
            config.get_backend_config("gemini").model = "claude-3-sonnet"

            # Save to file
            config.save_to_file(str(config_file))

            # Load from file
            loaded_config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify model_provider was persisted correctly
            codex_config = loaded_config.get_backend_config("codex")
            assert codex_config is not None
            assert codex_config.model_provider == "openrouter"
            assert codex_config.model == "x-ai/grok-4.1-fast:free"

            gemini_config = loaded_config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.model_provider == "anthropic"
            assert gemini_config.model == "claude-3-sonnet"

    def test_load_from_file_parses_model_provider(self):
        """Test that model_provider is correctly parsed from TOML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "test_parse_model_provider.toml"
            # Create TOML file with model_provider field
            data = {
                "backend": {"default": "openrouter-backend", "order": ["openrouter-backend"]},
                "backends": {
                    "openrouter-backend": {
                        "enabled": True,
                        "model": "x-ai/grok-4.1-fast:free",
                        "backend_type": "codex",
                        "model_provider": "openrouter",
                        "openai_base_url": "https://openrouter.ai/api/v1",
                    }
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            config = LLMBackendConfiguration.load_from_file(str(config_file))
            backend_config = config.get_backend_config("openrouter-backend")

            assert backend_config is not None
            assert backend_config.model_provider == "openrouter"
            assert backend_config.model == "x-ai/grok-4.1-fast:free"
            assert backend_config.backend_type == "codex"

    def test_backward_compatibility_old_toml_without_model_provider(self):
        """Test loading old TOML files that don't have model_provider field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_file = Path(tmpdir) / "old_config_no_model_provider.toml"

            # Create a TOML file with old structure (without model_provider)
            data = {
                "backend": {"default": "gemini", "order": ["gemini", "codex"]},
                "backends": {
                    "gemini": {
                        "enabled": True,
                        "model": "gemini-pro",
                        "temperature": 0.7,
                    },
                    "codex": {
                        "enabled": True,
                        "model": "codex",
                        "timeout": 30,
                    },
                },
            }
            with open(config_file, "w", encoding="utf-8") as fh:
                toml.dump(data, fh)

            # Load the configuration
            config = LLMBackendConfiguration.load_from_file(str(config_file))

            # Verify model_provider has default value when not present in old TOML
            gemini_config = config.get_backend_config("gemini")
            assert gemini_config is not None
            assert gemini_config.model_provider is None
            assert gemini_config.model == "gemini-pro"
            assert gemini_config.temperature == 0.7

            codex_config = config.get_backend_config("codex")
            assert codex_config is not None
            assert codex_config.model_provider is None
            assert codex_config.model == "codex"
            assert codex_config.timeout == 30


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
