import os
import tempfile
import tomllib
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.llm_backend_config import get_jules_wait_timeout_hours_from_config


class TestJulesTimeoutConfig:
    """Tests for Jules wait timeout configuration."""

    def test_get_timeout_default(self):
        """Test default timeout value when no config exists."""
        with patch("os.path.exists", return_value=False):
            timeout = get_jules_wait_timeout_hours_from_config()
            assert timeout == 240

    def test_get_timeout_from_file_explicit(self):
        """Test reading timeout from explicit config file path."""
        config_content = b"""
        [jules]
        wait_timeout_hours = 5
        """
        with tempfile.NamedTemporaryFile(suffix=".toml", delete=False) as f:
            f.write(config_content)
            config_path = f.name

        try:
            timeout = get_jules_wait_timeout_hours_from_config(config_path)
            assert timeout == 5
        finally:
            if os.path.exists(config_path):
                os.remove(config_path)

    def test_get_timeout_from_default_locations(self):
        """Test reading timeout from standard locations."""
        config_content = b"""
        [jules]
        wait_timeout_hours = 3
        """

        # We mock open() to simulate reading from a file without actually creating one in restricted paths
        # handling the tomllib.load call
        with patch("builtins.open", MagicMock()) as mock_open:
            mock_file = MagicMock()
            mock_file.__enter__.return_value = mock_file
            # tomllib.load expects a binary file interface
            # We can mock tomllib.load directly instead to avoid complex file mocking
            pass

        with patch("src.auto_coder.llm_backend_config.tomllib.load") as mock_load:
            mock_load.return_value = {"jules": {"wait_timeout_hours": 3}}

            # Mock os.path.exists to true for the first default path
            with patch("os.path.exists", side_effect=lambda p: p.endswith(".auto-coder/config.toml")):
                # We also need to mock open to return something valid-ish context manager so code doesn't crash
                with patch("builtins.open", MagicMock()):
                    timeout = get_jules_wait_timeout_hours_from_config()
                    assert timeout == 3

    def test_automation_config_integration(self):
        """Test that AutomationConfig initializes with the value from config."""
        # Mock the helper function to return a specific value
        # The function is imported from llm_backend_config inside __init__, so we patch the source
        with patch("src.auto_coder.llm_backend_config.get_jules_wait_timeout_hours_from_config", return_value=8):
            config = AutomationConfig()
            assert config.JULES_WAIT_TIMEOUT_HOURS == 8
