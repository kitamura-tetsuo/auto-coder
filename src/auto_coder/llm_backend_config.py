"""
Configuration management for LLM backends using TOML files.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import toml

from .logger_config import get_logger

# Define required options for each backend type
# These are options that must be present in the options list for the backend to work
REQUIRED_OPTIONS_BY_BACKEND = {
    "codex": ["--dangerously-bypass-approvals-and-sandbox"],
    "claude": ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"],
    "gemini": ["--yolo"],
    "auggie": ["--print"],
    "qwen": ["-y"],
    "jules": [],  # Session-based, no required flags
    "codex-mcp": [],  # MCP-based, options flexible
}


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
    # Options for no-edit functionality (used in Claude CLI)
    options_for_noedit: List[str] = field(default_factory=list)
    # Options for resume functionality
    options_for_resume: List[str] = field(default_factory=list)
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
    # Flags to track if options were explicitly set in configuration
    options_explicitly_set: bool = False
    options_for_noedit_explicitly_set: bool = False

    def validate_required_options(self) -> List[str]:
        """Validate that required options are configured.

        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        # Use backend_type if available, otherwise fall back to name
        backend_type = self.backend_type or self.name
        required = REQUIRED_OPTIONS_BY_BACKEND.get(backend_type, [])

        for req_opt in required:
            if req_opt not in self.options:
                errors.append(f"Backend '{self.name}' missing required option: {req_opt}. " f"Add to [backends.{self.name}].options in llm_config.toml")

        return errors

    def replace_placeholders(
        self,
        model_name: Optional[str] = None,
        session_id: Optional[str] = None,
        settings: Optional[str] = None,
    ) -> Dict[str, List[str]]:
        """Replace placeholders in option lists with provided values.

        Supports placeholders:
        - [model_name] - replaced with the model_name parameter
        - [sessionId] - replaced with the session_id parameter
        - [settings] - replaced with the settings parameter

        Args:
            model_name: Model name to replace [model_name] placeholders
            session_id: Session ID to replace [sessionId] placeholders
            settings: Settings file path to replace [settings] placeholders

        Returns:
            Dictionary with processed option lists:
            - 'options': processed options list
            - 'options_for_noedit': processed options_for_noedit list
            - 'options_for_resume': processed options_for_resume list
        """
        # Create a mapping of placeholders to their values
        placeholder_map = {}
        if model_name is not None:
            placeholder_map["[model_name]"] = model_name
        if session_id is not None:
            placeholder_map["[sessionId]"] = session_id
        if settings is not None:
            placeholder_map["[settings]"] = settings

        def replace_in_list(option_list: List[str]) -> List[str]:
            """Replace placeholders in a single option list."""
            if not placeholder_map:
                # No placeholders to replace, return a copy of the original list
                return list(option_list)

            result = []
            for option in option_list:
                # Replace all placeholders in this option string
                replaced_option = option
                for placeholder, value in placeholder_map.items():
                    replaced_option = replaced_option.replace(placeholder, value)
                result.append(replaced_option)
            return result

        # Process all three option lists
        return {
            "options": replace_in_list(self.options),
            "options_for_noedit": replace_in_list(self.options_for_noedit),
            "options_for_resume": replace_in_list(self.options_for_resume),
        }


@dataclass
class LLMBackendConfiguration:
    """Main configuration class for LLM backends."""

    # General backend settings
    backend_order: List[str] = field(default_factory=list)
    default_backend: str = "codex"
    # Individual backend configurations
    backends: Dict[str, BackendConfig] = field(default_factory=dict)
    # Backend configuration for non-editing operations (message generation, etc.)
    backend_for_noedit_order: List[str] = field(default_factory=list)
    backend_for_noedit_default: Optional[str] = None
    # Fallback backend configuration for failed PRs
    backend_for_failed_pr: Optional[BackendConfig] = None
    backend_for_failed_pr_order: List[str] = field(default_factory=list)
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

            # Set default options for backends that require them
            # All backends here are newly created, so set required options for those that need them
            for backend_name, required_options in REQUIRED_OPTIONS_BY_BACKEND.items():
                if backend_name in self.backends and required_options:
                    # Set required options since these backends were just created
                    self.backends[backend_name].options = list(required_options)

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
            return cls._load_from_data(data, config_path=config_path)
        except Exception as e:
            raise ValueError(f"Error loading configuration from {config_path}: {e}")

    @classmethod
    def load_from_dict(cls, data: Dict[str, Any]) -> "LLMBackendConfiguration":
        """Load configuration directly from a dictionary of configuration data."""

        try:
            return cls._load_from_data(data, config_path="<dict>")
        except Exception as e:
            raise ValueError(f"Error loading configuration from dict: {e}")

    @classmethod
    def _load_from_data(cls, data: Dict[str, Any], config_path: Optional[str] = None) -> "LLMBackendConfiguration":
        # Parse general backend settings
        backend_order = data.get("backend", {}).get("order", [])

        # Determine default backend - prioritize explicit "default" field, then order[0], then fallback to "codex"
        default_backend = data.get("backend", {}).get("default")
        if not default_backend:
            if backend_order:
                default_backend = backend_order[0]
            else:
                default_backend = "codex"

        # Parse backends
        backends_data = data.get("backends", {})
        backends: Dict[str, BackendConfig] = {}

        # Helper to parse a backend config dict
        def parse_backend_config(name: str, config_data: dict) -> BackendConfig:
            # Set explicit flags based on whether options were actually specified in config
            options_explicitly_set = "options" in config_data
            options_for_noedit_explicitly_set = "options_for_noedit" in config_data

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
                options_for_noedit=config_data.get("options_for_noedit", []),
                options_for_resume=config_data.get("options_for_resume", []),
                options_explicitly_set=options_explicitly_set,
                options_for_noedit_explicitly_set=options_for_noedit_explicitly_set,
            )

        # 1. Parse explicit [backends] section
        # Track which backends were explicitly configured
        explicitly_configured_backends = set()
        for name, config_data in backends_data.items():
            backends[name] = parse_backend_config(name, config_data)
            explicitly_configured_backends.add(name)

        # 2. Parse top-level backend definitions (e.g. [grok-4.1-fast])
        # This handles cases where TOML parses dotted keys as nested dictionaries
        # e.g. [grok-4.1-fast] -> {'grok-4': {'1-fast': {...}}}

        def is_potential_backend_config(d: dict) -> bool:
            # Heuristic: if it has specific backend keys, it's likely a config
            # We check for keys that are commonly used in backend definitions
            common_keys = {"backend_type", "model", "api_key", "base_url", "openai_api_key", "openai_base_url", "providers", "model_provider", "always_switch_after_execution", "settings", "options", "options_for_noedit", "options_for_resume"}
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
                        explicitly_configured_backends.add(full_key)

                # Recurse to find nested backends (e.g. grok-4.1-fast)
                find_backends_recursive(value, full_key)

        # Exclude reserved top-level keys from recursion
        reserved_keys = {"backend", "message_backend", "backend_for_noedit", "backends", "backend_for_failed_pr"}

        # Create a dict of potential top-level backends to recurse
        potential_roots = {k: v for k, v in data.items() if k not in reserved_keys and isinstance(v, dict)}

        find_backends_recursive(potential_roots)

        # Post-processing: inherit options from parent backend if backend_type matches
        # and options were not explicitly set in the child configuration
        for backend_name, backend_config in backends.items():
            if backend_config.backend_type and backend_config.backend_type in backends:
                parent_config = backends[backend_config.backend_type]
                # Inherit options if not explicitly set
                if not backend_config.options_explicitly_set:
                    backend_config.options = list(parent_config.options)
                # Inherit options_for_noedit if not explicitly set
                if not backend_config.options_for_noedit_explicitly_set:
                    backend_config.options_for_noedit = list(parent_config.options_for_noedit)

        # Add default backends if they are not already in the configuration
        # This ensures that backends like 'jules' are available even if not explicitly defined in the file
        default_backends = ["codex", "gemini", "qwen", "auggie", "claude", "jules", "codex-mcp"]
        for backend_name in default_backends:
            if backend_name not in backends:
                backends[backend_name] = BackendConfig(name=backend_name)

        # Set default options for backends that require them
        # Only add required options to backends that were NOT explicitly configured in the dict
        # This preserves backward compatibility with old config dicts that don't have options field
        for backend_name, required_options in REQUIRED_OPTIONS_BY_BACKEND.items():
            if backend_name in backends and required_options:
                backend = backends[backend_name]
                # Only set required options if the backend was NOT explicitly configured
                if backend_name not in explicitly_configured_backends:
                    backend.options = list(required_options)

        # Parse backend for non-editing operations settings
        # Try new key first, then fall back to old key for backward compatibility
        backend_for_noedit_order = data.get("backend_for_noedit", {}).get("order", [])

        # Determine default for noedit - prioritize explicit "default" field, then order[0]
        backend_for_noedit_default = data.get("backend_for_noedit", {}).get("default")
        if not backend_for_noedit_default and backend_for_noedit_order:
            backend_for_noedit_default = backend_for_noedit_order[0]

        # Backward compatibility: check old key if new key not found
        if not backend_for_noedit_order:
            old_order = data.get("message_backend", {}).get("order", [])
            if old_order:
                logger = get_logger(__name__)
                logger.warning("Configuration uses deprecated 'message_backend' key. " "Please update to 'backend_for_noedit' in your config file.")
                backend_for_noedit_order = old_order
                if backend_for_noedit_order:
                    backend_for_noedit_default = backend_for_noedit_order[0]

        # If no specific default for noedit, fallback to general default
        if not backend_for_noedit_default:
            backend_for_noedit_default = default_backend

        # Parse backend_for_failed_pr section
        backend_for_failed_pr_data = data.get("backend_for_failed_pr", {})
        backend_for_failed_pr = None
        backend_for_failed_pr_order = []

        if backend_for_failed_pr_data:
            # Check for order
            backend_for_failed_pr_order = backend_for_failed_pr_data.get("order", [])

            # Use "backend_for_failed_pr" as default name if not specified in data
            # Only parse as a backend config if it has backend-like fields or if order is empty
            # If it has order, it might still have backend fields, but we prioritize order for the manager
            # We still parse it as a config in case it's used as a backend definition too
            fallback_name = backend_for_failed_pr_data.get("name", "backend_for_failed_pr")
            backend_for_failed_pr = parse_backend_config(fallback_name, backend_for_failed_pr_data)

        config = cls(
            backend_order=backend_order,
            default_backend=default_backend,
            backends=backends,
            backend_for_noedit_order=backend_for_noedit_order,
            backend_for_noedit_default=backend_for_noedit_default,
            backend_for_failed_pr=backend_for_failed_pr,
            backend_for_failed_pr_order=backend_for_failed_pr_order,
            config_file_path=config_path or "~/.auto-coder/llm_config.toml",
        )

        return config

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
                "options_for_noedit": config.options_for_noedit,
                "options_for_resume": config.options_for_resume,
                "backend_type": config.backend_type,
                "model_provider": config.model_provider,
                "always_switch_after_execution": config.always_switch_after_execution,
                "settings": config.settings,
                "usage_markers": config.usage_markers,
                "options_explicitly_set": config.options_explicitly_set,
                "options_for_noedit_explicitly_set": config.options_for_noedit_explicitly_set,
            }

        # Prepare backend_for_failed_pr data
        backend_for_failed_pr_data = {}
        if self.backend_for_failed_pr:
            config = self.backend_for_failed_pr
            backend_for_failed_pr_data = {
                "name": config.name,
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
                "options_for_noedit": config.options_for_noedit,
                "options_for_resume": config.options_for_resume,
                "backend_type": config.backend_type,
                "model_provider": config.model_provider,
                "always_switch_after_execution": config.always_switch_after_execution,
                "settings": config.settings,
                "usage_markers": config.usage_markers,
                "options_explicitly_set": config.options_explicitly_set,
                "options_for_noedit_explicitly_set": config.options_for_noedit_explicitly_set,
            }

        data = {"backend": {"order": self.backend_order, "default": self.default_backend}, "backend_for_noedit": {"order": self.backend_for_noedit_order, "default": self.backend_for_noedit_default or self.default_backend}, "backends": backend_data}

        # Add backend_for_failed_pr section if configured
        if backend_for_failed_pr_data:
            data["backend_for_failed_pr"] = backend_for_failed_pr_data

        # Write TOML file
        with open(config_path, "w") as f:
            toml.dump(data, f)

    def get_backend_config(self, backend_name: str) -> Optional[BackendConfig]:
        """Get configuration for a specific backend."""
        config = self.backends.get(backend_name)
        if config:
            return config
        if self.backend_for_failed_pr and self.backend_for_failed_pr.name == backend_name:
            return self.backend_for_failed_pr
        return None

    def get_active_backends(self) -> List[str]:
        """Get list of enabled backends in the configured order."""
        if self.backend_order:
            # Filter to only include enabled backends that are in order
            return [name for name in self.backend_order if self.backends.get(name, BackendConfig(name=name)).enabled]
        else:
            # If no order is specified, return all enabled backends
            return [name for name, config in self.backends.items() if config.enabled]

    def get_active_noedit_backends(self) -> List[str]:
        """Get list of enabled backends for non-editing operations in the configured order.

        Returns backend_for_noedit order if specifically configured, otherwise falls back to general backends.
        """
        if self.backend_for_noedit_order:
            # Filter to only include enabled backends that are in noedit order
            return [name for name in self.backend_for_noedit_order if self.backends.get(name, BackendConfig(name=name)).enabled]
        else:
            # Fall back to using the general backend order for non-editing operations
            return self.get_active_backends()

    # Deprecated alias for backward compatibility
    def get_active_message_backends(self) -> List[str]:
        """Deprecated: Use get_active_noedit_backends() instead."""
        logger = get_logger(__name__)
        logger.warning("get_active_message_backends() is deprecated. Use get_active_noedit_backends() instead.")
        return self.get_active_noedit_backends()

    def get_noedit_default_backend(self) -> str:
        """Get the default backend for non-editing operations.

        Returns backend_for_noedit default if specifically configured, otherwise falls back to general default.
        """
        if self.backend_for_noedit_default and self.backends.get(self.backend_for_noedit_default, BackendConfig(name=self.backend_for_noedit_default)).enabled:
            return self.backend_for_noedit_default
        return self.default_backend

    # Deprecated alias for backward compatibility
    def get_message_default_backend(self) -> str:
        """Deprecated: Use get_noedit_default_backend() instead."""
        logger = get_logger(__name__)
        logger.warning("get_message_default_backend() is deprecated. Use get_noedit_default_backend() instead.")
        return self.get_noedit_default_backend()

    def has_dual_configuration(self) -> bool:
        """Check if both general backend and non-editing backend configurations are explicitly set."""
        # Check if noedit-specific configurations exist
        has_noedit_config = bool(self.backend_for_noedit_order or self.backend_for_noedit_default)
        has_general_config = bool(self.backend_order or self.default_backend)
        return has_noedit_config and has_general_config

    def get_backend_for_failed_pr(self) -> Optional[BackendConfig]:
        """Get the fallback backend configuration for failed PRs.

        Returns the backend_for_failed_pr configuration if configured, None otherwise.
        """
        return self.backend_for_failed_pr

    def get_model_for_failed_pr_backend(self) -> Optional[str]:
        """Get the model for the fallback backend for failed PRs.

        Returns the model name if a fallback backend is configured and has a model,
        None otherwise.
        """
        if self.backend_for_failed_pr and self.backend_for_failed_pr.model:
            return self.backend_for_failed_pr.model
        return None

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

        # Check new environment variable first, fall back to old one
        noedit_default_backend_env = os.environ.get("AUTO_CODER_NOEDIT_DEFAULT_BACKEND") or os.environ.get("AUTO_CODER_MESSAGE_DEFAULT_BACKEND")
        if noedit_default_backend_env:
            self.backend_for_noedit_default = noedit_default_backend_env
            # Warn if using old environment variable
            if os.environ.get("AUTO_CODER_MESSAGE_DEFAULT_BACKEND") and not os.environ.get("AUTO_CODER_NOEDIT_DEFAULT_BACKEND"):
                logger = get_logger(__name__)
                logger.warning("Environment variable AUTO_CODER_MESSAGE_DEFAULT_BACKEND is deprecated. " "Use AUTO_CODER_NOEDIT_DEFAULT_BACKEND instead.")


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


def get_jules_enabled_from_config(config_path: Optional[str] = None) -> bool:
    """Check if Jules is enabled via [jules].enabled in config.toml.

    This function reads from ~/.auto-coder/config.toml (or local .auto-coder/config.toml)
    and checks for a [jules] section with an 'enabled' field.

    Args:
        config_path: Optional explicit path to config.toml file. If not provided,
                    will check standard locations.

    Returns:
        True if Jules is enabled (default), False if explicitly disabled.
    """
    import os

    # If explicit path provided, check only that file
    if config_path:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    data = toml.load(f)

                jules_config = data.get("jules", {})
                if "enabled" in jules_config:
                    return bool(jules_config["enabled"])
            except Exception as e:
                # Log warning
                logger = get_logger(__name__)
                logger.warning(f"Failed to read config.toml from {config_path}: {e}")

        # If explicit path doesn't exist or has no [jules].enabled, return default
        return True

    # Try to find config.toml in standard locations
    config_paths = [
        os.path.join(os.getcwd(), ".auto-coder", "config.toml"),  # Local config
        os.path.expanduser("~/.auto-coder/config.toml"),  # Home config
    ]

    for config_path in config_paths:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    data = toml.load(f)

                # Check for [jules] section
                jules_config = data.get("jules", {})
                if "enabled" in jules_config:
                    return bool(jules_config["enabled"])

            except Exception as e:
                # Log warning but continue checking other paths
                logger = get_logger(__name__)
                logger.warning(f"Failed to read config.toml from {config_path}: {e}")
                continue

    # If no config.toml found or no [jules].enabled setting, return default (enabled)
    return True


def is_jules_mode_enabled() -> bool:
    """Check if Jules mode is enabled.

    Jules mode is enabled if:
    1. The 'jules' backend is enabled in llm_config.toml
    2. The [jules].enabled flag is set to true in config.toml (default: true)
    """
    # Check [backends.jules] in llm_config.toml
    jules_config = get_llm_config().get_backend_config("jules")
    jules_backend_enabled = jules_config.enabled if jules_config else False

    # Check [jules].enabled in config.toml
    jules_config_enabled = get_jules_enabled_from_config()

    return jules_backend_enabled and jules_config_enabled
