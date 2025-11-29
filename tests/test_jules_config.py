"""
Unit tests for Jules configuration in LLMBackendConfiguration.

This test file verifies the Jules configuration functionality:
1. Jules config with enabled = true
2. Jules config with enabled = false
3. Default behavior when Jules config is not explicitly defined
"""

import os
import tempfile

from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


def test_jules_config_enabled_true():
    """Test Jules configuration with enabled = true."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config with Jules explicitly enabled
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-v1"
api_key = "test-key-123"
temperature = 0.7
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify Jules config exists and is enabled
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist"
        assert jules_config.enabled is True, "Jules should be enabled"
        assert jules_config.model == "jules-v1", "Model should match"
        assert jules_config.api_key == "test-key-123", "API key should match"
        assert jules_config.temperature == 0.7, "Temperature should match"

        # Verify Jules is in active backends
        active_backends = config.get_active_backends()
        assert "jules" in active_backends, "Jules should be in active backends when enabled"


def test_jules_config_enabled_false():
    """Test Jules configuration with enabled = false."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config with Jules explicitly disabled
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = false
model = "jules-v1"
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify Jules config exists but is disabled
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist"
        assert jules_config.enabled is False, "Jules should be disabled"
        assert jules_config.model == "jules-v1", "Model should still be accessible"

        # Verify Jules is not in active backends
        active_backends = config.get_active_backends()
        assert "jules" not in active_backends, "Jules should not be in active backends when disabled"


def test_jules_config_default_behavior():
    """Test default Jules configuration behavior when not explicitly defined."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config without Jules section
        with open(config_path, "w") as f:
            f.write(
                """
[backends.codex]
enabled = true
model = "codex-model"
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify Jules has default configuration
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist with defaults"
        assert jules_config.name == "jules", "Jules config name should be 'jules'"
        assert jules_config.enabled is True, "Jules should be enabled by default"
        assert jules_config.model is None, "Model should be None by default"

        # Verify Jules is in active backends (default behavior)
        active_backends = config.get_active_backends()
        assert "jules" in active_backends, "Jules should be in active backends by default"


def test_jules_config_default_behavior_with_empty_file():
    """Test Jules default configuration with completely empty config file."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create empty config file
        with open(config_path, "w") as f:
            f.write("")

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify Jules has default configuration
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist with defaults"
        assert jules_config.enabled is True, "Jules should be enabled by default"
        assert jules_config.name == "jules", "Jules config name should be 'jules'"


def test_jules_config_get_model_for_backend():
    """Test get_model_for_backend returns correct model for Jules."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Test with explicit model
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-specific-model"
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        model = config.get_model_for_backend("jules")
        assert model == "jules-specific-model", "Should return configured model"

        # Test with default model (no model specified)
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        model = config.get_model_for_backend("jules")
        assert model == "jules", "Should return default model 'jules' when not specified"


def test_jules_config_with_backend_order():
    """Test Jules config works correctly with backend order configuration."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config with backend order including Jules
        with open(config_path, "w") as f:
            f.write(
                """
[backend]
order = ["codex", "jules", "gemini"]
default = "codex"

[backends.codex]
enabled = true
model = "codex"

[backends.jules]
enabled = true
model = "jules"

[backends.gemini]
enabled = false
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify Jules is in backend order
        assert "jules" in config.backend_order, "Jules should be in backend order"
        assert config.backend_order.index("jules") == 1, "Jules should be second in order"

        # Verify active backends only include enabled backends in order
        active_backends = config.get_active_backends()
        assert "codex" in active_backends, "Codex should be active"
        assert "jules" in active_backends, "Jules should be active"
        assert "gemini" not in active_backends, "Gemini should not be active (disabled)"


def test_jules_config_full_parameters():
    """Test Jules config with all supported parameters."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config with full Jules parameters
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-v2"
api_key = "jules-api-key"
base_url = "https://jules.example.com"
temperature = 0.9
timeout = 500
max_retries = 10
backend_type = "jules"
options = ["option1", "option2"]
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify all parameters are correctly loaded
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules config should exist"
        assert jules_config.enabled is True
        assert jules_config.model == "jules-v2"
        assert jules_config.api_key == "jules-api-key"
        assert jules_config.base_url == "https://jules.example.com"
        assert jules_config.temperature == 0.9
        assert jules_config.timeout == 500
        assert jules_config.max_retries == 10
        assert jules_config.backend_type == "jules"
        assert jules_config.options == ["option1", "option2"]


def test_jules_config_multiple_backends():
    """Test Jules config works alongside other backends."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config with multiple backends
        with open(config_path, "w") as f:
            f.write(
                """
[backends.codex]
enabled = true
model = "codex-model"

[backends.gemini]
enabled = true
model = "gemini-model"

[backends.jules]
enabled = false
model = "jules-model"

[backends.qwen]
enabled = true
model = "qwen-model"
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify all backends are loaded
        assert config.get_backend_config("codex") is not None
        assert config.get_backend_config("gemini") is not None
        assert config.get_backend_config("jules") is not None
        assert config.get_backend_config("qwen") is not None

        # Verify Jules is disabled while others are enabled
        jules_config = config.get_backend_config("jules")
        assert jules_config.enabled is False, "Jules should be disabled"

        # Verify active backends
        active_backends = config.get_active_backends()
        assert "codex" in active_backends
        assert "gemini" in active_backends
        assert "jules" not in active_backends, "Jules should not be active"
        assert "qwen" in active_backends


def test_jules_config_backend_type_is_jules():
    """Test that Jules backend can have backend_type set."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create Jules config with backend_type set
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
backend_type = "jules"
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify Jules backend type
        jules_config = config.get_backend_config("jules")
        assert jules_config.backend_type == "jules", "Backend type should be 'jules'"
