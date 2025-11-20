"""
Auto-Coder: Automated application development using Gemini CLI and GitHub integration.
"""

__version__ = "2025.11.21+g57f32bf"
__author__ = "Auto-Coder Team"
__description__ = "Automated application development using Gemini CLI and GitHub integration"

# Make the module available as a submodule
from . import llm_backend_config

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
    "llm_backend_config",
]
