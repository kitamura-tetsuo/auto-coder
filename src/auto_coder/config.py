"""
Configuration management for Auto-Coder.
"""

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings."""

    # GitHub settings
    github_token: Optional[str] = Field(None, env="GITHUB_TOKEN")
    github_api_url: str = Field("https://api.github.com", env="GITHUB_API_URL")

    # Gemini settings
    gemini_api_key: Optional[str] = Field(None, env="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-pro", env="GEMINI_MODEL")

    # Application settings
    max_issues_per_run: int = Field(-1, env="MAX_ISSUES_PER_RUN")  # -1 means unlimited
    max_prs_per_run: int = Field(-1, env="MAX_PRS_PER_RUN")  # -1 means unlimited
    dry_run: bool = Field(False, env="DRY_RUN")

    # Logging settings
    log_level: str = Field("INFO", env="LOG_LEVEL")
    log_format: str = Field(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s", env="LOG_FORMAT"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # 予期しない環境変数を無視


# Global settings instance
settings = Settings()
