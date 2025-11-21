"""
Backend Provider Manager: Manages provider metadata for LLM backends.

This module provides the foundational provider-management layer that allows the
backend manager to understand provider lists without changing runtime behavior yet.

Schema Example:
--------------
[backends.qwen]
model = "qwen3-coder-plus"
providers = ["qwen-open-router", "qwen-azure", "qwen-direct"]

Provider Definition File (provider_metadata.toml):
-------------------------------------------------
[qwen-open-router]
command = "uvx"
args = ["qwen-openai-proxy"]
description = "Qwen via OpenRouter API"

[qwen-azure]
command = "uvx"
args = ["qwen-azure-proxy"]
description = "Qwen via Azure OpenAI"
AZURE_ENDPOINT = "https://your-endpoint.openai.azure.com"
AZURE_API_VERSION = "2024-02-15-preview"

[qwen-direct]
command = "uvx"
args = ["qwen-direct"]
description = "Direct Qwen API access"
QWEN_API_KEY = "your-api-key"
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import toml


@dataclass
class ProviderMetadata:
    """Metadata for a single provider.

    This dataclass stores provider information including command, description,
    and arbitrary uppercase settings. The provider metadata is loaded from
    provider definition files and provides a way to configure different
    provider implementations for a backend.

    Note: Environment variable handling is not yet implemented. This is
    intentionally deferred to a future issue that will focus on rotation logic.
    """

    name: str
    command: str
    args: List[str] = field(default_factory=list)
    description: Optional[str] = None
    # Arbitrary uppercase settings for provider-specific configuration
    # Examples: AZURE_ENDPOINT, QWEN_API_KEY, OPENROUTER_API_KEY, etc.
    uppercase_settings: Dict[str, str] = field(default_factory=dict)


@dataclass
class BackendProviderMetadata:
    """Provider metadata for a specific backend.

    This dataclass groups all providers available for a specific backend
    and provides convenient access methods.
    """

    backend_name: str
    providers: List[ProviderMetadata] = field(default_factory=list)

    def get_provider(self, provider_name: str) -> Optional[ProviderMetadata]:
        """Get a specific provider by name.

        Args:
            provider_name: Name of the provider to retrieve

        Returns:
            ProviderMetadata if found, None otherwise
        """
        for provider in self.providers:
            if provider.name == provider_name:
                return provider
        return None

    def get_provider_names(self) -> List[str]:
        """Get list of all provider names for this backend.

        Returns:
            List of provider names
        """
        return [provider.name for provider in self.providers]


class BackendProviderManager:
    """
    Manager for backend provider metadata.

    This class provides a lightweight manager that loads and exposes provider
    metadata to the backend manager. It supports loading provider definitions
    from configuration files and gracefully degrades when no providers are
    declared.

    The manager now includes provider rotation logic, allowing automatic failover
    between multiple providers for the same backend when usage limits are reached.
    """

    def __init__(self, provider_metadata_path: Optional[str] = None):
        """Initialize the provider manager.

        Args:
            provider_metadata_path: Optional path to provider metadata file.
                If not provided, will look for ~/.auto-coder/provider_metadata.toml
                or use empty defaults.
        """
        if provider_metadata_path is None:
            default_path = Path.home() / ".auto-coder" / "provider_metadata.toml"
            self.metadata_path = str(default_path)
        else:
            self.metadata_path = provider_metadata_path

        # Cache for loaded provider metadata: backend_name -> BackendProviderMetadata
        self._provider_cache: Dict[str, BackendProviderMetadata] = {}
        self._metadata_cache: Optional[Dict[str, Dict]] = None

        # Provider rotation state tracking (thread-safe)
        # backend_name -> current provider index
        self._current_provider_index: Dict[str, int] = {}
        self._lock = threading.Lock()

        # Last used provider tracking: (backend_name, provider_name)
        self._last_used_provider: Optional[Tuple[str, str]] = None

    def load_provider_metadata(self) -> None:
        """Load provider metadata from file.

        This method loads provider definitions from the metadata file and caches
        them for later access. If the file doesn't exist, the cache remains empty
        and the manager degrades gracefully.
        """
        metadata_file = Path(self.metadata_path)

        if not metadata_file.exists():
            # Graceful degradation: no metadata file, cache remains empty
            self._metadata_cache = {}
            return

        try:
            with open(metadata_file, "r") as f:
                self._metadata_cache = toml.load(f)
        except Exception as e:
            # If there's an error loading, treat as empty
            # This allows the system to degrade gracefully
            self._metadata_cache = {}

    def _ensure_metadata_loaded(self) -> None:
        """Ensure provider metadata is loaded.

        This is a lazy loading mechanism to avoid file I/O until needed.
        """
        if self._metadata_cache is None:
            self.load_provider_metadata()

    def get_backend_providers(self, backend_name: str) -> BackendProviderMetadata:
        """
        Get provider metadata for a specific backend.

        Args:
            backend_name: Name of the backend (e.g., "qwen", "gemini", "claude")

        Returns:
            BackendProviderMetadata with all providers for the backend.
            Returns empty BackendProviderMetadata if no providers are configured.
        """
        # Check cache first
        if backend_name in self._provider_cache:
            return self._provider_cache[backend_name]

        self._ensure_metadata_loaded()

        if not self._metadata_cache:
            # No metadata loaded, return empty
            metadata = BackendProviderMetadata(backend_name=backend_name)
            self._provider_cache[backend_name] = metadata
            return metadata

        # Build provider list from metadata
        providers = []
        backend_section = self._metadata_cache.get(backend_name, {})

        for provider_name, provider_config in backend_section.items():
            # Parse uppercase settings (any keys that are uppercase)
            uppercase_settings = {}
            for key, value in provider_config.items():
                if key.isupper():
                    uppercase_settings[key] = str(value)

            # Extract command and args
            command = provider_config.get("command", "")
            args = provider_config.get("args", [])

            # Get description
            description = provider_config.get("description")

            provider = ProviderMetadata(
                name=provider_name,
                command=command,
                args=args,
                description=description,
                uppercase_settings=uppercase_settings,
            )
            providers.append(provider)

        metadata = BackendProviderMetadata(backend_name=backend_name, providers=providers)
        self._provider_cache[backend_name] = metadata
        return metadata

    def get_all_provider_names(self, backend_name: str) -> List[str]:
        """
        Get list of all provider names for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            List of provider names, empty list if none configured
        """
        backend_metadata = self.get_backend_providers(backend_name)
        return backend_metadata.get_provider_names()

    def has_providers(self, backend_name: str) -> bool:
        """
        Check if a backend has any providers configured.

        Args:
            backend_name: Name of the backend

        Returns:
            True if providers exist, False otherwise
        """
        backend_metadata = self.get_backend_providers(backend_name)
        return len(backend_metadata.providers) > 0

    def get_next_provider(self, backend_name: str) -> Optional[ProviderMetadata]:
        """
        Get the next provider for a backend (cycling rotation).

        This method implements provider rotation by returning the next provider
        in the list for the specified backend. If the end of the list is reached,
        it wraps around to the beginning.

        Args:
            backend_name: Name of the backend

        Returns:
            ProviderMetadata for the next provider, or None if no providers exist
        """
        backend_metadata = self.get_backend_providers(backend_name)

        if not backend_metadata.providers:
            return None

        with self._lock:
            # Get current index or initialize to 0
            current_idx = self._current_provider_index.get(backend_name, 0)
            current_idx %= len(backend_metadata.providers)

            # Get the provider at current index
            provider = backend_metadata.providers[current_idx]

            # Update index for next call (cycling rotation)
            self._current_provider_index[backend_name] = (current_idx + 1) % len(backend_metadata.providers)

            # Update last used provider tracking
            self._last_used_provider = (backend_name, provider.name)

            return provider

    def get_current_provider(self, backend_name: str) -> Optional[ProviderMetadata]:
        """
        Get the current provider for a backend without advancing the rotation.

        Args:
            backend_name: Name of the backend

        Returns:
            ProviderMetadata for the current provider, or None if no providers exist
        """
        backend_metadata = self.get_backend_providers(backend_name)

        if not backend_metadata.providers:
            return None

        with self._lock:
            # Get current index or initialize to 0
            current_idx = self._current_provider_index.get(backend_name, 0)
            current_idx %= len(backend_metadata.providers)

            # Get the provider at current index
            provider = backend_metadata.providers[current_idx]

            return provider

    def get_provider_env_vars(self, backend_name: str, provider_name: Optional[str] = None) -> Dict[str, str]:
        """
        Get environment variables for a specific provider.

        Extracts uppercase settings from the provider metadata and returns them
        as environment variables.

        Args:
            backend_name: Name of the backend
            provider_name: Optional specific provider name. If None, uses current provider.

        Returns:
            Dict of environment variable names to values (uppercase_settings)
        """
        backend_metadata = self.get_backend_providers(backend_name)

        if not backend_metadata.providers:
            return {}

        # Find the provider
        provider = None
        if provider_name:
            provider = backend_metadata.get_provider(provider_name)
        else:
            provider = self.get_current_provider(backend_name)

        if provider is None:
            return {}

        # Return uppercase settings as env vars
        return provider.uppercase_settings.copy()

    def get_last_used_provider(self) -> Optional[Tuple[str, str]]:
        """
        Get the last used backend and provider names.

        Returns:
            Tuple of (backend_name, provider_name) for the most recently used provider,
            or None if no provider has been used yet.
        """
        with self._lock:
            return self._last_used_provider

    def reset_provider_rotation(self, backend_name: str) -> None:
        """
        Reset the provider rotation for a specific backend back to the first provider.

        Args:
            backend_name: Name of the backend to reset
        """
        with self._lock:
            if backend_name in self._current_provider_index:
                self._current_provider_index[backend_name] = 0

    def clear_cache(self) -> None:
        """
        Clear the provider metadata cache.

        This is useful for testing or when the metadata file changes.
        """
        self._provider_cache.clear()
        self._metadata_cache = None

    @staticmethod
    def get_default_manager() -> BackendProviderManager:
        """
        Get a default provider manager instance.

        Returns:
            BackendProviderManager with default configuration
        """
        return BackendProviderManager()
