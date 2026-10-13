"""
Auto-Coder: Automated application development using Antigravity CLI and GitHub integration.
"""

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("auto-coder")
except PackageNotFoundError:
    __version__ = "0+unknown"
__author__ = "Auto-Coder Team"
__description__ = "Automated application development using Antigravity CLI and GitHub integration"

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
