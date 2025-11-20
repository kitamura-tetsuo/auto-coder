"""
Configuration management for LLM backends using TOML files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import toml


@dataclass
class ProviderConfig:
    """Configuration for a provider.

    A provider represents a specific deployment or endpoint of a backend.
    Providers can be used for rotation, failover, or geographic distribution.
    """

    name: str
    # Command to invoke the provider (e.g., "uvx", "python", path to binary)
    command: Optional[str] = None
    # Human-readable description of the provider
    description: Optional[str] = None
    # Arbitrary uppercase settings for provider-specific configuration
    # These will be converted to environment variables or passed as config
    settings: Dict[str, str] = field(default_factory=dict)


@dataclass
class BackendConfig:
    """Configuration for a single backend."""

    name: str
    enabled: bool = True
    # Model name can be provided directly or will use backend default if None
    model: Optional[str] = None
    # Additional backend-specific settings
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    # LLM parameters
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    max_retries: Optional[int] = None
    # For OpenAI-compatible backends
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    # List of provider names for this backend
    providers: List[str] = field(default_factory=list)
    # For custom configurations
    extra_args: Dict[str, str] = field(default_factory=dict)


@dataclass
class LLMBackendConfiguration:
    """Main configuration class for LLM backends."""

    # General backend settings
    backend_order: List[str] = field(default_factory=list)
    default_backend: str = "codex"
    # Individual backend configurations
    backends: Dict[str, BackendConfig] = field(default_factory=dict)
    # Provider configurations (by provider name)
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    # Message backend configuration (separate from general LLM operations)
    message_backend_order: List[str] = field(default_factory=list)
    message_default_backend: Optional[str] = None
    # Environment variable overrides
    env_prefix: str = "AUTO_CODER_"
    # Configuration file path - relative to user's home directory
    config_file_path: str = "~/.auto-coder/llm_config.toml"

    def __post_init__(self) -> None:
        """Initialize default backends if none are configured."""
        if not self.backends:
            # Add default configurations for known backends
            default_backends = ["codex", "gemini", "qwen", "auggie", "claude", "codex-mcp"]
            for backend_name in default_backends:
                self.backends[backend_name] = BackendConfig(name=backend_name)

    @classmethod
    def load_from_file(cls, config_path: Optional[str] = None) -> "LLMBackendConfiguration":
        """Load configuration from TOML file."""
        if config_path is None:
            config_path = os.path.expanduser("~/.auto-coder/llm_config.toml")
        else:
            config_path = os.path.expanduser(config_path)

        if not os.path.exists(config_path):
            # Create a default configuration file if none exists
            config = cls()
            config.save_to_file(config_path)
            return config

        try:
            with open(config_path, "r") as f:
                data = toml.load(f)

            # Parse general backend settings
            backend_order = data.get("backend", {}).get("order", [])
            default_backend = data.get("backend", {}).get("default", "codex")

            # Parse backends
            backends_data = data.get("backends", {})
            backends = {}
            for name, config_data in backends_data.items():
                backend_config = BackendConfig(
                    name=name,
                    enabled=config_data.get("enabled", True),
                    model=config_data.get("model"),
                    api_key=config_data.get("api_key"),
                    base_url=config_data.get("base_url"),
                    temperature=config_data.get("temperature"),
                    timeout=config_data.get("timeout"),
                    max_retries=config_data.get("max_retries"),
                    openai_api_key=config_data.get("openai_api_key"),
                    openai_base_url=config_data.get("openai_base_url"),
                    providers=config_data.get("providers", []),
                    extra_args=config_data.get("extra_args", {}),
                )
                backends[name] = backend_config

            # Parse providers
            providers_data = data.get("providers", {})
            providers = {}
            for name, config_data in providers_data.items():
                provider_config = ProviderConfig(
                    name=name,
                    command=config_data.get("command"),
                    description=config_data.get("description"),
                    settings=config_data.get("settings", {}),
                )
                providers[name] = provider_config

            # Parse message backend settings
            message_backend_order = data.get("message_backend", {}).get("order", [])
            message_default_backend = data.get("message_backend", {}).get("default")

            config = cls(backend_order=backend_order, default_backend=default_backend, backends=backends, providers=providers, message_backend_order=message_backend_order, message_default_backend=message_default_backend, config_file_path=config_path)

            return config
        except Exception as e:
            raise ValueError(f"Error loading configuration from {config_path}: {e}")

    def save_to_file(self, config_path: Optional[str] = None) -> None:
        """Save configuration to TOML file."""
        if config_path is None:
            config_path = self.config_file_path
        config_path = os.path.expanduser(config_path)

        # Create directory if it doesn't exist
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)

        # Prepare data for TOML
        backend_data = {}
        for name, config in self.backends.items():
            backend_data[name] = {
                "enabled": config.enabled,
                "model": config.model,
                "api_key": config.api_key,
                "base_url": config.base_url,
                "temperature": config.temperature,
                "timeout": config.timeout,
                "max_retries": config.max_retries,
                "openai_api_key": config.openai_api_key,
                "openai_base_url": config.openai_base_url,
                "providers": config.providers,
                "extra_args": config.extra_args,
            }

        # Prepare provider data for TOML
        provider_data = {}
        for name, provider_config in self.providers.items():
            provider_data[name] = {
                "command": provider_config.command,
                "description": provider_config.description,
                "settings": provider_config.settings,
            }

        data = {"backend": {"order": self.backend_order, "default": self.default_backend}, "message_backend": {"order": self.message_backend_order, "default": self.message_default_backend or self.default_backend}, "backends": backend_data, "providers": provider_data}

        # Write TOML file
        with open(config_path, "w") as f:
            toml.dump(data, f)

    def get_backend_config(self, backend_name: str) -> Optional[BackendConfig]:
        """Get configuration for a specific backend."""
        return self.backends.get(backend_name)

    def get_provider_config(self, provider_name: str) -> Optional[ProviderConfig]:
        """Get configuration for a specific provider."""
        return self.providers.get(provider_name)

    def get_active_backends(self) -> List[str]:
        """Get list of enabled backends in the configured order."""
        if self.backend_order:
            # Filter to only include enabled backends that are in order
            return [name for name in self.backend_order if self.backends.get(name, BackendConfig(name=name)).enabled]
        else:
            # If no order is specified, return all enabled backends
            return [name for name, config in self.backends.items() if config.enabled]

    def get_active_message_backends(self) -> List[str]:
        """Get list of enabled message backends in the configured order.

        Returns message backend order if specifically configured, otherwise falls back to general backends.
        """
        if self.message_backend_order:
            # Filter to only include enabled backends that are in message order
            return [name for name in self.message_backend_order if self.backends.get(name, BackendConfig(name=name)).enabled]
        else:
            # Fall back to using the general backend order for messages
            return self.get_active_backends()

    def get_message_default_backend(self) -> str:
        """Get the default backend for message generation.

        Returns message backend default if specifically configured, otherwise falls back to general default.
        """
        if self.message_default_backend and self.backends.get(self.message_default_backend, BackendConfig(name=self.message_default_backend)).enabled:
            return self.message_default_backend
        return self.default_backend

    def has_dual_configuration(self) -> bool:
        """Check if both general backend and message backend configurations are explicitly set."""
        # Check if message-specific configurations exist
        has_message_config = bool(self.message_backend_order or self.message_default_backend)
        has_general_config = bool(self.backend_order or self.default_backend)
        return has_message_config and has_general_config

    def get_model_for_backend(self, backend_name: str) -> Optional[str]:
        """Get the model for a specific backend, with fallback to backend defaults."""
        config = self.get_backend_config(backend_name)
        if config and config.model:
            return config.model

        # Default models for known backends
        default_models = {"gemini": "gemini-2.5-pro", "qwen": "qwen3-coder-plus", "auggie": "GPT-5", "claude": "sonnet", "codex": "codex", "codex-mcp": "codex-mcp"}
        return default_models.get(backend_name)

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides to the configuration."""
        # Map environment variable names to backend settings
        for backend_name, backend_config in self.backends.items():
            # Check for specific backend env overrides
            api_key_env = os.environ.get(f"AUTO_CODER_{backend_name.upper()}_API_KEY")
            if api_key_env:
                backend_config.api_key = api_key_env

            openai_api_key_env = os.environ.get(f"AUTO_CODER_OPENAI_API_KEY") or os.environ.get(f"AUTO_CODER_{backend_name.upper()}_OPENAI_API_KEY")
            if openai_api_key_env:
                backend_config.openai_api_key = openai_api_key_env

            openai_base_url_env = os.environ.get(f"AUTO_CODER_OPENAI_BASE_URL") or os.environ.get(f"AUTO_CODER_{backend_name.upper()}_OPENAI_BASE_URL")
            if openai_base_url_env:
                backend_config.openai_base_url = openai_base_url_env

        # Also check for general environment overrides
        default_backend_env = os.environ.get("AUTO_CODER_DEFAULT_BACKEND")
        if default_backend_env:
            self.default_backend = default_backend_env

        message_default_backend_env = os.environ.get("AUTO_CODER_MESSAGE_DEFAULT_BACKEND")
        if message_default_backend_env:
            self.message_default_backend = message_default_backend_env


# Global instance to be used throughout the application
_llm_config: Optional[LLMBackendConfiguration] = None


def get_llm_config() -> LLMBackendConfiguration:
    """Get the global LLM backend configuration instance."""
    global _llm_config
    if _llm_config is None:
        _llm_config = LLMBackendConfiguration.load_from_file()
        _llm_config.apply_env_overrides()
    return _llm_config


def reset_llm_config() -> None:
    """Reset the global configuration instance (for testing)."""
    global _llm_config
    _llm_config = None
