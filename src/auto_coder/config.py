"""
Configuration management for Auto-Coder.
"""

from typing import Optional

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


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
    dry_run: bool = Field(default=False, json_schema_extra={"env": "DRY_RUN"})

    # Logging settings
    log_level: str = Field(default="INFO", json_schema_extra={"env": "LOG_LEVEL"})
    log_format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        json_schema_extra={"env": "LOG_FORMAT"},
    )

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore unexpected environment variables
    )


# Global settings instance
settings = Settings()
