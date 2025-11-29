"""
Integration tests for Jules configuration with both config.toml and llm_config.toml.

This test file verifies the combined behavior of:
1. [jules].enabled in config.toml
2. [backends.jules].enabled in llm_config.toml
3. How they work together to control Jules mode
"""

import os
import tempfile

from src.auto_coder.llm_backend_config import LLMBackendConfiguration, get_jules_enabled_from_config, get_llm_config, reset_llm_config


def test_jules_enabled_in_both_configs():
    """Test Jules enabled in both config.toml and llm_config.toml."""
    reset_llm_config()
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create llm_config.toml with Jules enabled
        llm_config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(llm_config_path), exist_ok=True)

        with open(llm_config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
"""
            )

        # Create config.toml with Jules enabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = true
"""
            )

        # Load configurations
        llm_config = LLMBackendConfiguration.load_from_file(llm_config_path)
        jules_backend_config = llm_config.get_backend_config("jules")
        jules_config_enabled = get_jules_enabled_from_config(config_path)

        # Both should be enabled
        assert jules_backend_config.enabled is True, "Jules should be enabled in llm_config.toml"
        assert jules_config_enabled is True, "Jules should be enabled in config.toml"


def test_jules_disabled_in_llm_config_but_enabled_in_config_toml():
    """Test Jules disabled in llm_config.toml but enabled in config.toml."""
    reset_llm_config()
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create llm_config.toml with Jules disabled
        llm_config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(llm_config_path), exist_ok=True)

        with open(llm_config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = false
"""
            )

        # Create config.toml with Jules enabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = true
"""
            )

        # Load configurations
        llm_config = LLMBackendConfiguration.load_from_file(llm_config_path)
        jules_backend_config = llm_config.get_backend_config("jules")
        jules_config_enabled = get_jules_enabled_from_config(config_path)

        # Backend should be disabled, config should be enabled
        assert jules_backend_config.enabled is False, "Jules should be disabled in llm_config.toml"
        assert jules_config_enabled is True, "Jules should be enabled in config.toml"


def test_jules_enabled_in_llm_config_but_disabled_in_config_toml():
    """Test Jules enabled in llm_config.toml but disabled in config.toml."""
    reset_llm_config()
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create llm_config.toml with Jules enabled
        llm_config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(llm_config_path), exist_ok=True)

        with open(llm_config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
"""
            )

        # Create config.toml with Jules disabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = false
"""
            )

        # Load configurations
        llm_config = LLMBackendConfiguration.load_from_file(llm_config_path)
        jules_backend_config = llm_config.get_backend_config("jules")
        jules_config_enabled = get_jules_enabled_from_config(config_path)

        # Backend should be enabled, config should be disabled
        assert jules_backend_config.enabled is True, "Jules should be enabled in llm_config.toml"
        assert jules_config_enabled is False, "Jules should be disabled in config.toml"


def test_jules_disabled_in_both_configs():
    """Test Jules disabled in both config.toml and llm_config.toml."""
    reset_llm_config()
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create llm_config.toml with Jules disabled
        llm_config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(llm_config_path), exist_ok=True)

        with open(llm_config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = false
"""
            )

        # Create config.toml with Jules disabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = false
"""
            )

        # Load configurations
        llm_config = LLMBackendConfiguration.load_from_file(llm_config_path)
        jules_backend_config = llm_config.get_backend_config("jules")
        jules_config_enabled = get_jules_enabled_from_config(config_path)

        # Both should be disabled
        assert jules_backend_config.enabled is False, "Jules should be disabled in llm_config.toml"
        assert jules_config_enabled is False, "Jules should be disabled in config.toml"


def test_jules_config_toml_only():
    """Test Jules configuration with only config.toml (no llm_config.toml Jules section)."""
    reset_llm_config()
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create config.toml with Jules disabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = false
"""
            )

        # Load configurations (no llm_config.toml Jules section)
        llm_config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        llm_config = LLMBackendConfiguration.load_from_file(llm_config_path)
        jules_backend_config = llm_config.get_backend_config("jules")

        # Backend should have default config (enabled), config should be disabled
        assert jules_backend_config.enabled is True, "Jules should be enabled by default in llm_config.toml"
        assert get_jules_enabled_from_config(config_path) is False, "Jules should be disabled in config.toml"
