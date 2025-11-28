"""Tests for jules_config module."""

import tempfile
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest
import toml

from src.auto_coder.jules_config import JulesConfig


class TestJulesConfig:
    """Test cases for JulesConfig class."""

    def test_jules_config_default_values(self):
        """Test creating JulesConfig with default values."""
        config = JulesConfig()
        assert config.config_file_path == "~/.auto-coder/config.toml"
        assert config.enabled is True
        assert config.extra_config == {}

    def test_jules_config_custom_values(self):
        """Test creating JulesConfig with custom values."""
        config = JulesConfig(config_file_path="/custom/path/config.toml", enabled=False, extra_config={"key1": "value1"})
        assert config.config_file_path == "/custom/path/config.toml"
        assert config.enabled is False
        assert config.extra_config == {"key1": "value1"}

    def test_jules_config_load_from_file_default_path(self):
        """Test loading JulesConfig from default config file."""
        with patch("os.path.exists", return_value=False):
            config = JulesConfig.load_from_file()

            assert config.enabled is True
            # The path should be expanded
            assert ".auto-coder/config.toml" in config.config_file_path

    def test_jules_config_load_from_file_custom_path(self):
        """Test loading JulesConfig from a specific config file."""
        custom_path = "/custom/config.toml"

        with patch("os.path.exists", return_value=False):
            config = JulesConfig.load_from_file(custom_path)

            assert config.config_file_path == custom_path
            assert config.enabled is True

    def test_jules_config_load_from_file_nonexistent(self):
        """Test loading JulesConfig from a non-existent config file."""
        with patch("os.path.exists", return_value=False):
            with patch("builtins.open", mock_open(read_data="")) as mock_file:
                config = JulesConfig.load_from_file("/nonexistent/config.toml")

                # Should return default config without error
                assert config.enabled is True
                assert config.config_file_path == "/nonexistent/config.toml"
                mock_file.assert_not_called()

    def test_jules_config_load_from_file_with_jules_section(self):
        """Test loading JulesConfig with [jules] section in TOML."""
        toml_content = """
[jules]
enabled = false
option1 = "value1"
option2 = "value2"
"""
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=toml_content)):
                with patch("toml.load") as mock_toml:
                    mock_toml.return_value = {"jules": {"enabled": False, "option1": "value1", "option2": "value2"}}
                    config = JulesConfig.load_from_file("/test/config.toml")

                    assert config.enabled is False
                    assert config.extra_config == {"option1": "value1", "option2": "value2"}

    def test_jules_config_load_from_file_no_jules_section(self):
        """Test loading JulesConfig without [jules] section."""
        toml_content = """
[backend]
default = "codex"
"""
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=toml_content)):
                with patch("toml.load") as mock_toml:
                    mock_toml.return_value = {"backend": {"default": "codex"}}
                    config = JulesConfig.load_from_file("/test/config.toml")

                    # Should use defaults
                    assert config.enabled is True
                    assert config.extra_config == {}

    def test_jules_config_load_from_file_toml_error(self):
        """Test loading JulesConfig when TOML loading fails."""
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="invalid")):
                with patch("toml.load", side_effect=Exception("Parse error")):
                    config = JulesConfig.load_from_file("/test/config.toml")

                    # Should return default config on error
                    assert config.enabled is True
                    assert config.config_file_path == "/test/config.toml"

    def test_get_jules_section_returns_dict(self):
        """Test that get_jules_section returns a dictionary."""
        toml_content = """
[jules]
enabled = true
option1 = "value1"
"""
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=toml_content)):
                with patch("toml.load") as mock_toml:
                    mock_toml.return_value = {"jules": {"enabled": True, "option1": "value1"}}
                    config = JulesConfig(config_file_path="/test/config.toml")

                    result = config.get_jules_section()

                    assert result == {"enabled": True, "option1": "value1"}

    def test_get_jules_section_no_jules_section(self):
        """Test get_jules_section when [jules] section doesn't exist."""
        toml_content = """
[backend]
default = "codex"
"""
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data=toml_content)):
                with patch("toml.load") as mock_toml:
                    mock_toml.return_value = {"backend": {"default": "codex"}}
                    config = JulesConfig(config_file_path="/test/config.toml")

                    result = config.get_jules_section()

                    assert result == {}

    def test_get_jules_section_file_not_exists(self):
        """Test get_jules_section when config file doesn't exist."""
        with patch("os.path.exists", return_value=False):
            with patch("builtins.open") as mock_file:
                config = JulesConfig(config_file_path="/test/config.toml")

                result = config.get_jules_section()

                assert result == {}
                mock_file.assert_not_called()

    def test_get_jules_section_load_error(self):
        """Test get_jules_section when TOML loading fails."""
        with patch("os.path.exists", return_value=True):
            with patch("builtins.open", mock_open(read_data="invalid")):
                with patch("toml.load", side_effect=Exception("Parse error")):
                    config = JulesConfig(config_file_path="/test/config.toml")

                    result = config.get_jules_section()

                    # Should return empty dict on error
                    assert result == {}
