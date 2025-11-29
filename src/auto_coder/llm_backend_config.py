"""
Configuration management for LLM backends using TOML files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import toml


def resolve_config_path(config_path: Optional[str] = None) -> str:
    """Resolve the configuration file path with priority rules.

    Priority (highest to lowest):
    1. Explicitly provided config_path argument
    2. .auto-coder/llm_config.toml in current directory
    3. ~/.auto-coder/llm_config.toml in home directory

    Args:
        config_path: Optional explicit path to configuration file

    Returns:
        Absolute path to the configuration file to use
    """
    # Priority 1: Explicitly provided config_path
    if config_path is not None:
        expanded_path = os.path.expanduser(config_path)
        return os.path.abspath(expanded_path)

    # Priority 2: Local .auto-coder/llm_config.toml
    local_config = os.path.join(os.getcwd(), ".auto-coder", "llm_config.toml")
    if os.path.exists(local_config):
        return os.path.abspath(local_config)

    # Priority 3: Home directory ~/.auto-coder/llm_config.toml
    home_config = os.path.expanduser("~/.auto-coder/llm_config.toml")
    return os.path.abspath(home_config)


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
    # For custom configurations
    extra_args: Dict[str, str] = field(default_factory=dict)
    # List of provider names available for this backend
    # Schema: [backends.qwen].providers = ["qwen-open-router", ...]
    providers: List[str] = field(default_factory=list)
    # Retry configuration for usage limit handling
    usage_limit_retry_count: int = 0
    usage_limit_retry_wait_seconds: int = 0
    # Additional options for the backend
    options: List[str] = field(default_factory=list)
    # Type of backend
    backend_type: Optional[str] = None
    # Model provider (e.g., "openrouter", "anthropic", etc.)
    model_provider: Optional[str] = None
    # Always switch to next backend after execution
    always_switch_after_execution: bool = False
    # Path to settings file (for Claude backend)
    settings: Optional[str] = None
    # Usage markers for tracking usage patterns
    usage_markers: List[str] = field(default_factory=list)


@dataclass
class LLMBackendConfiguration:
    """Main configuration class for LLM backends."""

    # General backend settings
    backend_order: List[str] = field(default_factory=list)
    default_backend: str = "codex"
    # Individual backend configurations
    backends: Dict[str, BackendConfig] = field(default_factory=dict)
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
            default_backends = ["codex", "gemini", "qwen", "auggie", "claude", "jules", "codex-mcp"]
            for backend_name in default_backends:
                self.backends[backend_name] = BackendConfig(name=backend_name)

    @classmethod
    def load_from_file(cls, config_path: Optional[str] = None) -> "LLMBackendConfiguration":
        """Load configuration from TOML file."""
        config_path = resolve_config_path(config_path)

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

            # Helper to parse a backend config dict
            def parse_backend_config(name: str, config_data: dict) -> BackendConfig:
                return BackendConfig(
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
                    extra_args=config_data.get("extra_args", {}),
                    providers=config_data.get("providers", []),
                    usage_limit_retry_count=config_data.get("usage_limit_retry_count", 0),
                    usage_limit_retry_wait_seconds=config_data.get("usage_limit_retry_wait_seconds", 0),
                    options=config_data.get("options", []),
                    backend_type=config_data.get("backend_type"),
                    model_provider=config_data.get("model_provider"),
                    always_switch_after_execution=config_data.get("always_switch_after_execution", False),
                    settings=config_data.get("settings"),
                    usage_markers=config_data.get("usage_markers", []),
                )

            # 1. Parse explicit [backends] section
            for name, config_data in backends_data.items():
                backends[name] = parse_backend_config(name, config_data)

            # 2. Parse top-level backend definitions (e.g. [grok-4.1-fast])
            # This handles cases where TOML parses dotted keys as nested dictionaries
            # e.g. [grok-4.1-fast] -> {'grok-4': {'1-fast': {...}}}

            def is_potential_backend_config(d: dict) -> bool:
                # Heuristic: if it has specific backend keys, it's likely a config
                # We check for keys that are commonly used in backend definitions
                common_keys = {"backend_type", "model", "api_key", "base_url", "openai_api_key", "openai_base_url", "providers", "model_provider", "always_switch_after_execution", "settings"}
                # Also check if 'enabled' is present, but it's very common so we combine it
                # with the fact that we are looking for backends.
                # If a dict has 'enabled' and is in the top-level (or nested from top-level),
                # it's a strong candidate.
                if "enabled" in d:
                    return True
                return any(k in d for k in common_keys)

            def find_backends_recursive(current_data: dict, prefix: str = ""):
                for key, value in current_data.items():
                    if not isinstance(value, dict):
                        continue

                    # Skip known non-backend dict fields to avoid false positives
                    if key in {"extra_args"}:
                        continue

                    full_key = f"{prefix}.{key}" if prefix else key

                    # Check if this node itself is a backend config
                    if is_potential_backend_config(value):
                        if full_key not in backends:
                            backends[full_key] = parse_backend_config(full_key, value)

                    # Recurse to find nested backends (e.g. grok-4.1-fast)
                    find_backends_recursive(value, full_key)

            # Exclude reserved top-level keys from recursion
            reserved_keys = {"backend", "message_backend", "backends"}

            # Create a dict of potential top-level backends to recurse
            potential_roots = {k: v for k, v in data.items() if k not in reserved_keys and isinstance(v, dict)}

            find_backends_recursive(potential_roots)

            # Parse message backend settings
            message_backend_order = data.get("message_backend", {}).get("order", [])
            message_default_backend = data.get("message_backend", {}).get("default")

            config = cls(backend_order=backend_order, default_backend=default_backend, backends=backends, message_backend_order=message_backend_order, message_default_backend=message_default_backend, config_file_path=config_path)

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
                "extra_args": config.extra_args,
                "providers": config.providers,
                "usage_limit_retry_count": config.usage_limit_retry_count,
                "usage_limit_retry_wait_seconds": config.usage_limit_retry_wait_seconds,
                "options": config.options,
                "backend_type": config.backend_type,
                "model_provider": config.model_provider,
                "always_switch_after_execution": config.always_switch_after_execution,
                "settings": config.settings,
                "usage_markers": config.usage_markers,
            }

        data = {"backend": {"order": self.backend_order, "default": self.default_backend}, "message_backend": {"order": self.message_backend_order, "default": self.message_default_backend or self.default_backend}, "backends": backend_data}

        # Write TOML file
        with open(config_path, "w") as f:
            toml.dump(data, f)

    def get_backend_config(self, backend_name: str) -> Optional[BackendConfig]:
        """Get configuration for a specific backend."""
        return self.backends.get(backend_name)

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
        default_models = {"gemini": "gemini-2.5-pro", "qwen": "qwen3-coder-plus", "auggie": "GPT-5", "claude": "sonnet", "codex": "codex", "jules": "jules", "codex-mcp": "codex-mcp"}
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
