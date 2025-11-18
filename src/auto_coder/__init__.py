"""
Auto-Coder: Automated application development using Gemini CLI and GitHub integration.
"""

__version__ = "2025.11.15+g697f91e"
__author__ = "Auto-Coder Team"
__description__ = "Automated application development using Gemini CLI and GitHub integration"

# Make the module available as a submodule
from . import llm_backend_config

# Export LLM backend configuration classes
from .llm_backend_config import (
    AuggieBackendConfig,
    BackendConfig,
    ClaudeBackendConfig,
    CodexBackendConfig,
    CodexMCPBackendConfig,
    GeminiBackendConfig,
    LLMBackendConfig,
    LLMBackendConfigManager,
    QwenBackendConfig,
    ensure_config_directory,
    get_llm_backend_config,
    initialize_llm_backend_config,
)

__all__ = [
    "LLMBackendConfig",
    "BackendConfig",
    "CodexBackendConfig",
    "CodexMCPBackendConfig",
    "GeminiBackendConfig",
    "QwenBackendConfig",
    "ClaudeBackendConfig",
    "AuggieBackendConfig",
    "LLMBackendConfigManager",
    "get_llm_backend_config",
    "initialize_llm_backend_config",
    "ensure_config_directory",
]
