"""
Backend Provider Manager: Manages provider metadata for LLM backends.

This module provides the foundational provider-management layer that allows the
backend manager to understand provider lists without changing runtime behavior yet.

Provider Configuration Schema:
------------------------------
Providers are configured in the LLM config TOML file under a `[providers]` section:

```toml
[providers.qwen-open-router]
command = "uvx"
description = "Qwen via OpenRouter"
settings = { API_BASE = "https://openrouter.ai/api/v1", TIMEOUT = "30" }

[providers.qwen-local]
command = "python"
description = "Local Qwen deployment"
settings = { API_BASE = "http://localhost:8000", MAX_RETRIES = "5" }
```

Backend-Provider Association:
----------------------------
Backends reference providers by name in their configuration:

```toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
providers = ["qwen-open-router", "qwen-local"]
```

The provider manager exposes provider metadata to the backend manager, allowing
future issues to implement provider rotation logic without changing the current
runtime behavior.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .llm_backend_config import LLMBackendConfiguration, ProviderConfig


class BackendProviderManager:
    """Manages provider metadata for backends.

    This manager provides a lightweight interface to access provider information
    without enforcing environment handling or changing backend behavior yet.

    TODO: Future issues should use this manager to implement:
    - Provider rotation logic
    - Provider health checking
    - Automatic failover between providers
    - Load balancing across providers
    """

    def __init__(self, config: LLMBackendConfiguration) -> None:
        """Initialize the provider manager with configuration.

        Args:
            config: The LLM backend configuration containing provider definitions
        """
        self._config = config
        self._provider_configs: Dict[str, ProviderConfig] = config.providers

    def get_providers_for_backend(self, backend_name: str) -> List[ProviderConfig]:
        """Get list of provider configurations for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            List of ProviderConfig objects for the backend's providers.
            Returns empty list if backend has no providers or doesn't exist.
        """
        backend_config = self._config.get_backend_config(backend_name)
        if not backend_config or not backend_config.providers:
            return []

        provider_configs = []
        for provider_name in backend_config.providers:
            provider_config = self._config.get_provider_config(provider_name)
            if provider_config:
                provider_configs.append(provider_config)

        return provider_configs

    def get_provider_config(self, provider_name: str) -> Optional[ProviderConfig]:
        """Get provider configuration by name.

        Args:
            provider_name: Name of the provider

        Returns:
            ProviderConfig object if found, None otherwise
        """
        return self._provider_configs.get(provider_name)

    def get_all_providers(self) -> Dict[str, ProviderConfig]:
        """Get all provider configurations.

        Returns:
            Dictionary mapping provider names to ProviderConfig objects
        """
        return self._provider_configs.copy()

    def has_provider(self, provider_name: str) -> bool:
        """Check if a provider exists in the configuration.

        Args:
            provider_name: Name of the provider

        Returns:
            True if provider exists, False otherwise
        """
        return provider_name in self._provider_configs

    def get_provider_names_for_backend(self, backend_name: str) -> List[str]:
        """Get list of provider names for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            List of provider names. Returns empty list if backend has no providers.
        """
        backend_config = self._config.get_backend_config(backend_name)
        if not backend_config:
            return []
        return backend_config.providers.copy()

    def get_provider_count_for_backend(self, backend_name: str) -> int:
        """Get count of providers for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            Number of providers configured for the backend
        """
        return len(self.get_provider_names_for_backend(backend_name))

    def is_provider_usable(self, provider_name: str) -> bool:
        """Check if a provider is considered usable (has minimum required config).

        For now, this is a simple check. Future implementations might check:
        - Command availability
        - Environment variable presence
        - Health check status
        - API connectivity

        Args:
            provider_name: Name of the provider

        Returns:
            True if provider has at least a name (always true for configured providers)
        """
        return self.has_provider(provider_name)
