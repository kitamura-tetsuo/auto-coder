"""
Unit tests for Jules backend configuration in LLMBackendConfiguration.
"""

import os
import tempfile
from pathlib import Path

from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


def test_jules_config_loading():
    """Test that LLMBackendConfiguration correctly loads [jules] config."""
    # Create a temporary directory for our test
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a test config file with a jules section
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-test-model"
api_key = "test-key"
temperature = 0.7
"""
            )

        # Load configuration from the file
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Check that the jules backend was loaded
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist"
        assert jules_config.enabled, "Jules enabled property should be True"
        assert jules_config.model == "jules-test-model", "Jules model should match"
        assert jules_config.api_key == "test-key", "Jules API key should match"
        assert jules_config.temperature == 0.7, "Jules temperature should match"


def test_jules_config_default_values():
    """Test that jules backend has correct default values when not explicitly configured."""
    # Create a temporary directory for our test
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a minimal config file without jules section
        with open(config_path, "w") as f:
            f.write(
                """
[backends.codex]
enabled = true
"""
            )

        # Load configuration from the file
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Check that the jules backend was created with default values
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist with defaults"
        assert jules_config.name == "jules", "Jules config name should be 'jules'"
        assert jules_config.enabled, "Jules should be enabled by default"
        assert jules_config.model is None, "Jules model should be None by default"


def test_jules_config_disabled():
    """Test that jules backend can be disabled via config."""
    # Create a temporary directory for our test
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a config file with jules explicitly disabled
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = false
model = "jules-test-model"
"""
            )

        # Load configuration from the file
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Check that the jules backend was loaded but is disabled
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules backend config should exist"
        assert not jules_config.enabled, "Jules enabled property should be False"
        assert jules_config.model == "jules-test-model", "Jules model should match"

        # Check that jules is not in the active backends list
        active_backends = config.get_active_backends()
        assert "jules" not in active_backends, "Jules should not be in active backends when disabled"


def test_jules_config_get_model():
    """Test that get_model_for_backend works correctly for jules."""
    # Create a temporary directory for our test
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a config file with jules having a specific model
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-specific-model"
"""
            )

        # Load configuration from the file
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Check that get_model_for_backend returns the correct model for jules
        model = config.get_model_for_backend("jules")
        assert model == "jules-specific-model", f"Expected 'jules-specific-model', got {model}"


def test_jules_config_with_other_backends():
    """Test that jules config works alongside other backends."""
    # Create a temporary directory for our test
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a config file with multiple backends including jules
        with open(config_path, "w") as f:
            f.write(
                """
[backend]
order = ["codex", "gemini", "jules"]
default = "codex"

[backends.codex]
enabled = true
model = "codex-model"

[backends.gemini]
enabled = true
model = "gemini-model"

[backends.jules]
enabled = true
model = "jules-model"
"""
            )

        # Load configuration from the file
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Check all backends are loaded
        assert config.get_backend_config("codex") is not None
        assert config.get_backend_config("gemini") is not None
        assert config.get_backend_config("jules") is not None

        # Check jules config specifically
        jules_config = config.get_backend_config("jules")
        assert jules_config.enabled
        assert jules_config.model == "jules-model"

        # Check backend order includes jules
        active_backends = config.get_active_backends()
        assert "jules" in active_backends
