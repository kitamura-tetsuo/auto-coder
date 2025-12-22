"""
Auto-Coder: Automated application development using Gemini CLI and GitHub integration.
"""

__version__ = "2025.12.22.1+gf76eb638"
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

# Provide a lightweight stub for sentence_transformers when the optional
# dependency (or its transitive requirements like torch) is unavailable.
# This keeps patch-based tests and optional integration paths from failing
# at import time.
try:
    import sentence_transformers  # noqa: F401
except Exception as exc:  # pragma: no cover - defensive fallback for optional dep
    import sys
    import types

    stub_module = types.ModuleType("sentence_transformers")
    _missing_exc = exc

    class _MissingSentenceTransformer:  # pragma: no cover - simple stub
        def __init__(self, *args, **kwargs) -> None:
            raise ImportError("sentence_transformers dependency is not available") from _missing_exc

    stub_module.SentenceTransformer = _MissingSentenceTransformer  # type: ignore
    sys.modules["sentence_transformers"] = stub_module

__all__ = [
    "LLMBackendConfiguration",
    "BackendConfig",
    "get_llm_config",
    "llm_backend_config",
]
