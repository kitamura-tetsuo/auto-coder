"""
Configuration management for Auto-Coder.
"""

from typing import Optional

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    # GitHub settings
    github_token: Optional[str] = Field(default=None)
    github_api_url: str = Field(default="https://api.github.com")

    # Gemini settings
    gemini_api_key: Optional[str] = Field(default=None)
    gemini_model: str = Field(default="gemini-pro")

    # Application settings
    max_issues_per_run: int = Field(default=-1)  # -1 means unlimited
    max_prs_per_run: int = Field(default=-1)  # -1 means unlimited
    dry_run: bool = Field(default=False)

    # Logging settings
    log_level: str = Field(default="INFO")
    log_format: str = Field(
        default="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # 予期しない環境変数を無視
    )


# Global settings instance
settings = Settings()
