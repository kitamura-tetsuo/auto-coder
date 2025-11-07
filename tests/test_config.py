"""Tests for configuration functionality."""

import os
from unittest.mock import patch

from src.auto_coder.config import Settings


class TestSettings:
    """Test cases for Settings class."""

    def test_default_settings(self):
        """Test default settings values."""
        settings = Settings()

        assert settings.github_token is None
        assert settings.github_api_url == "https://api.github.com"
        assert settings.gemini_api_key is None
        assert settings.gemini_model == "gemini-pro"
        assert settings.max_issues_per_run == -1
        assert settings.max_prs_per_run == -1
        assert settings.log_level == "INFO"
        assert "%(asctime)s" in settings.log_format

    @patch.dict(
        os.environ,
        {
            "GITHUB_TOKEN": "test_github_token",
            "GITHUB_API_URL": "https://custom.github.com",
            "GEMINI_API_KEY": "test_gemini_key",
            "GEMINI_MODEL": "gemini-pro-vision",
            "MAX_ISSUES_PER_RUN": "20",
            "MAX_PRS_PER_RUN": "10",
            "LOG_LEVEL": "DEBUG",
            "LOG_FORMAT": "custom format",
        },
    )
    def test_settings_from_environment(self):
        """Test settings loaded from environment variables."""
        settings = Settings()

        assert settings.github_token == "test_github_token"
        assert settings.github_api_url == "https://custom.github.com"
        assert settings.gemini_api_key == "test_gemini_key"
        assert settings.gemini_model == "gemini-pro-vision"
        assert settings.max_issues_per_run == 20
        assert settings.max_prs_per_run == 10
        assert settings.log_level == "DEBUG"
        assert settings.log_format == "custom format"

    @patch.dict(os.environ, {"GITHUB_TOKEN": "env_token"})
    def test_settings_partial_environment(self):
        """Test settings with partial environment variables."""
        settings = Settings()

        # Environment variable should override default
        assert settings.github_token == "env_token"

        # Other settings should use defaults
        assert settings.github_api_url == "https://api.github.com"
        assert settings.gemini_api_key is None
        assert settings.max_issues_per_run == -1
        assert settings.max_prs_per_run == -1

    def test_settings_validation(self):
        """Test settings validation."""
        # Test with valid values
        settings = Settings(
            max_issues_per_run=5,
            max_prs_per_run=3,
        )

        assert settings.max_issues_per_run == 5
        assert settings.max_prs_per_run == 3

    def test_settings_immutability(self):
        """Test that settings can be modified after creation."""
        settings = Settings()

        # Should be able to modify settings
        original_token = settings.github_token
        settings.github_token = "new_token"
        assert settings.github_token == "new_token"
        assert settings.github_token != original_token

    def test_config_class_attributes(self):
        """Test model_config attributes."""
        settings = Settings()

        # Check that model_config is properly set
        assert hasattr(Settings, "model_config")
        assert Settings.model_config["env_file"] == ".env"
        assert Settings.model_config["env_file_encoding"] == "utf-8"
