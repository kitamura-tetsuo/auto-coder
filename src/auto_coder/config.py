"""
Configuration management for Auto-Coder.
"""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # GitHub settings
    github_token: Optional[str] = Field(default=None, json_schema_extra={"env": "GITHUB_TOKEN"})
    github_api_url: str = Field(default="https://api.github.com", json_schema_extra={"env": "GITHUB_API_URL"})

    # Gemini settings
    gemini_api_key: Optional[str] = Field(default=None, json_schema_extra={"env": "GEMINI_API_KEY"})
    gemini_model: str = Field(default="gemini-pro", json_schema_extra={"env": "GEMINI_MODEL"})

    # Application settings
    max_issues_per_run: int = Field(default=-1, json_schema_extra={"env": "MAX_ISSUES_PER_RUN"})  # -1 means unlimited
    max_prs_per_run: int = Field(default=-1, json_schema_extra={"env": "MAX_PRS_PER_RUN"})  # -1 means unlimited
    hishel_cache_path: Optional[str] = Field(default=None, json_schema_extra={"env": "HISHEL_CACHE_PATH"})

    # Dependency management settings
    check_dependencies: bool = Field(default=True, json_schema_extra={"env": "CHECK_DEPENDENCIES"})

    # Logging settings
    log_level: str = Field(default="INFO", json_schema_extra={"env": "LOG_LEVEL"})
    log_format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        json_schema_extra={"env": "LOG_FORMAT"},
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore unexpected environment variables
    )


from typing import Union

# Define type for the settings instance
SettingsType = Union[Settings, "DefaultSettings"]

# Global settings instance with error handling to prevent import-time failures
try:
    _settings = Settings()
except Exception as e:
    # In case of settings loading failure, create a minimal configuration
    # This allows the CLI to at least show help without crashing
    import warnings

    warnings.warn(f"Failed to load settings: {e}. Using default configuration.")

    # Create a default settings object with basic values
    class DefaultSettings(Settings):
        log_level: str = "INFO"
        github_token: Optional[str] = None
        github_api_url: str = "https://api.github.com"
        gemini_api_key: Optional[str] = None
        gemini_model: str = "gemini-pro"
        max_issues_per_run: int = -1
        max_prs_per_run: int = -1
        check_dependencies: bool = True
        hishel_cache_path: Optional[str] = None
        log_format: str = "%(asctime)s - %(name)s - %(message)s"

    _settings = DefaultSettings()

# Export the settings instance with proper type
settings: SettingsType = _settings
