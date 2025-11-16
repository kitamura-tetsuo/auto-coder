"""
Auto-Coder: Automated application development using Gemini CLI and GitHub integration.
"""

__version__ = "2025.11.16+gd095ca5"
__author__ = "Auto-Coder Team"
__description__ = "Automated application development using Gemini CLI and GitHub integration"

# Export LLM backend configuration classes
from .llm_backend_config import (
    BackendConfig,
    LLMBackendConfiguration,
    get_llm_config,
)

__all__ = [
    "LLMBackendConfiguration",
    "BackendConfig",
    "get_llm_config",
]
