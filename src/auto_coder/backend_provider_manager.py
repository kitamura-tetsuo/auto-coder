"""
Backend Provider Manager: Manages provider metadata and rotation for LLM backends.

This module provides the provider-management layer that enables automatic provider
rotation within backends, environment variable handling, and usage tracking.

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

Features:
---------
- Provider rotation with automatic failover on usage limits
- Environment variable export for provider-specific configuration
- Tracking of last used providers for debugging and telemetry
- Graceful degradation when no providers are configured
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ProviderMetadata:
    """Metadata for a single provider.

    This dataclass stores provider information including command, description,
    and arbitrary uppercase settings. The provider metadata is loaded from
    provider definition files and provides a way to configure different
    provider implementations for a backend.

    Uppercase settings are automatically exported as environment variables
    during provider execution via the create_env_context() method.
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

    This class provides a manager that loads and exposes provider metadata to the
    backend manager. It supports loading provider definitions from configuration
    files and gracefully degrades when no providers are declared.

    The manager implements provider rotation logic, environment variable handling,
    and tracks last used providers for telemetry. Uppercase settings from provider
    metadata are exported as environment variables during execution.
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

        # Track current provider index for each backend (for rotation). Index is persisted
        # across calls so we keep retry ordering consistent when limits are hit.
        self._current_provider_idx: Dict[str, int] = {}

        # Track last used provider for each backend
        self._last_used_provider: Dict[str, Optional[str]] = {}

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
            with open(metadata_file, "rb") as f:
                self._metadata_cache = tomllib.load(f)
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

    def get_provider_count(self, backend_name: str) -> int:
        """
        Get the total number of providers configured for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            Number of providers configured for the backend (0 if none)
        """
        backend_metadata = self.get_backend_providers(backend_name)
        return len(backend_metadata.providers)

    def get_current_provider_name(self, backend_name: str) -> Optional[str]:
        """
        Get the current provider name for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            Current provider name, or None if no providers configured
        """
        provider = self.get_current_provider(backend_name)
        return provider.name if provider else None

    def get_next_provider(self, backend_name: str) -> Optional[ProviderMetadata]:
        """
        Get the next provider for a backend and advance the rotation index.

        Args:
            backend_name: Name of the backend

        Returns:
            Next ProviderMetadata, or None if no providers configured
        """
        backend_metadata = self.get_backend_providers(backend_name)
        if not backend_metadata.providers:
            return None

        # Get current index or initialize to 0
        current = self.get_current_provider(backend_name)
        if current is None:
            return None

        # Rotate to the next provider index in a circular fashion
        total = len(backend_metadata.providers)
        idx = self._current_provider_idx.get(backend_name, 0)
        idx = (idx + 1) % total
        self._current_provider_idx[backend_name] = idx
        provider = backend_metadata.providers[idx]
        return provider

    def get_current_provider(self, backend_name: str) -> Optional[ProviderMetadata]:
        """
        Get the current provider for a backend without advancing the index.

        Args:
            backend_name: Name of the backend

        Returns:
            Current ProviderMetadata, or None if no providers configured
        """
        backend_metadata = self.get_backend_providers(backend_name)
        if not backend_metadata.providers:
            return None

        # Get current index or initialize to the first provider
        idx = self._current_provider_idx.get(backend_name)
        if idx is None:
            idx = 0
            self._current_provider_idx[backend_name] = idx
        else:
            idx = idx % len(backend_metadata.providers)
            self._current_provider_idx[backend_name] = idx

        return backend_metadata.providers[idx]

    def advance_to_next_provider(self, backend_name: str) -> bool:
        """
        Advance to the next provider for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            True if rotation occurred, False if no providers are configured.
        """
        provider = self.get_next_provider(backend_name)
        return provider is not None

    def get_last_used_provider_name(self, backend_name: str) -> Optional[str]:
        """
        Get the name of the last used provider for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            Last used provider name, or None if no provider has been used
        """
        return self._last_used_provider.get(backend_name)

    def create_env_context(self, backend_name: str) -> Dict[str, str]:
        """
        Create environment variable context from current provider's uppercase settings.

        This method extracts all uppercase settings from the current provider
        and returns them as a dictionary that can be merged with environment variables.

        Args:
            backend_name: Name of the backend

        Returns:
            Dictionary of environment variables from the current provider
        """
        provider = self.get_current_provider(backend_name)
        if provider is None:
            return {}

        env_vars = {}
        for key, value in provider.uppercase_settings.items():
            env_vars[key] = value

        return env_vars

    def mark_provider_used(self, backend_name: str, provider_name: Optional[str]) -> None:
        """
        Record the provider that produced a successful response.

        Args:
            backend_name: Name of the backend
            provider_name: Provider identifier to record
        """
        if provider_name:
            self._last_used_provider[backend_name] = provider_name

    def reset_provider_rotation(self, backend_name: str) -> None:
        """
        Reset provider rotation to the beginning for a specific backend.

        Args:
            backend_name: Name of the backend
        """
        if backend_name in self._current_provider_idx:
            self._current_provider_idx[backend_name] = 0

    def reset_all_provider_rotations(self) -> None:
        """
        Reset provider rotation for all backends.
        """
        self._current_provider_idx.clear()

    def clear_cache(self) -> None:
        """
        Clear the provider metadata cache.

        This is useful for testing or when the metadata file changes.
        """
        self._provider_cache.clear()
        self._metadata_cache = None
        # Reset rotation state when cache is cleared
        self._current_provider_idx.clear()
        self._last_used_provider.clear()

    @staticmethod
    def get_default_manager() -> BackendProviderManager:
        """
        Get a default provider manager instance.

        Returns:
            BackendProviderManager with default configuration
        """
        return BackendProviderManager()
