"""
Unit tests for Jules configuration via [jules].enabled in config.toml.

This test file verifies the new functionality:
1. Reading [jules].enabled from config.toml file
2. Checking both config.toml and llm_config.toml for Jules settings
3. Default behavior when config.toml doesn't exist or has no [jules] section
"""

import os
import tempfile

from src.auto_coder.llm_backend_config import get_jules_enabled_from_config


def test_jules_enabled_via_config_toml():
    """Test Jules enabled via [jules].enabled = true in config.toml."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create config.toml with Jules enabled
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = true
"""
            )

        # Test that Jules is enabled
        assert get_jules_enabled_from_config(config_path) is True, "Jules should be enabled when config.toml has [jules].enabled = true"


def test_jules_disabled_via_config_toml():
    """Test Jules disabled via [jules].enabled = false in config.toml."""
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

        # Test that Jules is disabled
        assert get_jules_enabled_from_config(config_path) is False, "Jules should be disabled when config.toml has [jules].enabled = false"


def test_jules_config_toml_with_local_and_home():
    """Test that local config.toml takes precedence over home config."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create home config.toml with Jules disabled
        home_config_path = os.path.expanduser("~/.auto-coder/config.toml")
        home_dir = os.path.dirname(home_config_path)
        os.makedirs(home_dir, exist_ok=True)

        # Create a subdirectory for the "current working directory"
        local_cwd = os.path.join(temp_dir, "project")
        os.makedirs(local_cwd, exist_ok=True)

        # Write home config (disabled)
        with open(home_config_path, "w") as f:
            f.write(
                """
[jules]
enabled = false
"""
            )

        # Create local config (enabled) in the "project" directory
        local_config_path = os.path.join(local_cwd, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(local_config_path), exist_ok=True)

        with open(local_config_path, "w") as f:
            f.write(
                """
[jules]
enabled = true
"""
            )

        # Change to the local directory and test
        original_cwd = os.getcwd()
        try:
            os.chdir(local_cwd)
            # Local config should take precedence - Jules should be enabled
            assert get_jules_enabled_from_config() is True, "Local config.toml should take precedence"
        finally:
            os.chdir(original_cwd)

        # Clean up home config
        if os.path.exists(home_config_path):
            os.remove(home_config_path)


def test_jules_no_config_toml():
    """Test default behavior when config.toml doesn't exist."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Change to a directory with no config.toml
        original_cwd = os.getcwd()
        try:
            os.chdir(temp_dir)
            # Should return default (True) when config.toml doesn't exist
            assert get_jules_enabled_from_config() is True, "Should return default (True) when config.toml doesn't exist"
        finally:
            os.chdir(original_cwd)


def test_jules_config_toml_without_jules_section():
    """Test behavior when config.toml exists but has no [jules] section."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config.toml without [jules] section
        with open(config_path, "w") as f:
            f.write(
                """
[other_section]
setting = "value"
"""
            )

        # Should return default (True) when [jules] section is missing
        assert get_jules_enabled_from_config(config_path) is True, "Should return default (True) when [jules] section is missing"


def test_jules_config_toml_with_jules_no_enabled():
    """Test behavior when [jules] section exists but has no 'enabled' field."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config.toml with [jules] but no 'enabled' field
        with open(config_path, "w") as f:
            f.write(
                """
[jules]
model = "jules-v1"
"""
            )

        # Should return default (True) when 'enabled' field is missing
        assert get_jules_enabled_from_config(config_path) is True, "Should return default (True) when 'enabled' field is missing"


def test_jules_config_toml_with_other_fields():
    """Test that [jules] section with other fields still respects 'enabled' field."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create config.toml with [jules] section having multiple fields
        with open(config_path, "w") as f:
            f.write(
                """
[jules]
enabled = false
model = "jules-v1"
api_key = "test-key"
temperature = 0.7
"""
            )

        # Should respect enabled=false even with other fields
        assert get_jules_enabled_from_config(config_path) is False, "Should respect enabled=false with other fields present"


def test_jules_config_toml_home_directory():
    """Test reading from home directory config.toml."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create home config.toml
        home_config_path = os.path.expanduser("~/.auto-coder/config.toml")
        home_dir = os.path.dirname(home_config_path)
        os.makedirs(home_dir, exist_ok=True)

        try:
            with open(home_config_path, "w") as f:
                f.write(
                    """
[jules]
enabled = false
"""
                )

            # Should read from home directory when local doesn't exist
            original_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                assert get_jules_enabled_from_config() is False, "Should read from home directory config.toml"
            finally:
                os.chdir(original_cwd)
        finally:
            # Clean up
            if os.path.exists(home_config_path):
                os.remove(home_config_path)
