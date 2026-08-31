"""
Configuration management for LLM backends using TOML files.
"""

import contextvars
import copy
import os
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import tomli_w

from .logger_config import get_logger

# Define implementation-required options for each backend type. Codex execution
# policies are validated by mode in ``BackendConfig.validate_required_options``
# and must never be injected as defaults.
REQUIRED_OPTIONS_BY_BACKEND = {
    "codex": [],
    "claude": ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"],
    "antigravity": ["--dangerously-skip-permissions"],
    "auggie": ["--print"],
    "qwen": ["-y"],
    "jules": [],  # Session-based, no required flags
    "codex-mcp": [],  # MCP-based, options flexible
    "aider": [],  # Aider-based, options flexible
}


def resolve_config_path(config_path: Optional[str] = None) -> str:
    """Resolve the configuration file path with priority rules.

    Priority (highest to lowest):
    1. Explicitly provided config_path argument
    2. Environment variable AUTO_CODER_CONFIG_PATH or AUTO_CODER_LLM_CONFIG_PATH
    3. .auto-coder/llm_config.toml in current directory
    4. ~/.auto-coder/llm_config.toml in home directory

    Args:
        config_path: Optional explicit path to configuration file

    Returns:
        Absolute path to the configuration file to use
    """
    # Priority 1: Explicitly provided config_path
    if config_path is not None:
        expanded_path = os.path.expanduser(config_path)
        return os.path.abspath(expanded_path)

    # Priority 2: Environment variable override
    env_config = os.environ.get("AUTO_CODER_CONFIG_PATH") or os.environ.get("AUTO_CODER_LLM_CONFIG_PATH")
    if env_config:
        return os.path.abspath(os.path.expanduser(env_config))

    # Priority 3: Local .auto-coder/llm_config.toml
    local_config = os.path.join(os.getcwd(), ".auto-coder", "llm_config.toml")
    if os.path.exists(local_config):
        return os.path.abspath(local_config)

    # Priority 4: Home directory ~/.auto-coder/llm_config.toml
    home_config = os.path.expanduser("~/.auto-coder/llm_config.toml")
    return os.path.abspath(home_config)


def resolve_repo_override_path(
    repo_name: Optional[str],
    override_root_dir: Optional[str] = None,
    filename: str = "llm_config.toml",
) -> Optional[str]:
    """Resolve the repository-specific configuration override file path.

    The path convention is:
        ~/.auto-coder/<owner>/<repo>/<filename>

    Repository overrides must be selected using the GitHub 'owner/name' identity
    and mapped directly to the directory hierarchy under ~/.auto-coder.
    The override location is independent of where the base configuration was loaded from.

    Args:
        repo_name: GitHub repository in 'owner/repo' format. If None, empty, or
                   not in 'owner/repo' format, returns None.
        override_root_dir: Optional override root directory (defaults to ~/.auto-coder).
        filename: Name of the config file to resolve (defaults to 'llm_config.toml').

    Returns:
        Absolute path to the repository override configuration file, or None if invalid repo_name.
    """
    if not repo_name or not isinstance(repo_name, str):
        return None

    cleaned_repo = repo_name.strip().strip("/")
    if "/" not in cleaned_repo:
        # Repository matching must not depend only on the short repository name
        return None

    parts = cleaned_repo.split("/")
    if len(parts) != 2:
        return None

    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    if not owner or not repo:
        return None

    # Validate that owner and repo are valid GitHub identifiers (no path traversal, no . or ..)
    import re

    valid_identifier_pattern = re.compile(r"^[a-zA-Z0-9_.-]+$")
    if not valid_identifier_pattern.match(owner) or not valid_identifier_pattern.match(repo):
        return None
    if owner in (".", "..") or repo in (".", ".."):
        return None

    if override_root_dir is not None:
        base_dir = os.path.expanduser(override_root_dir)
    else:
        base_dir = os.path.expanduser("~/.auto-coder")

    base_dir = os.path.abspath(base_dir)
    override_path = os.path.abspath(os.path.join(base_dir, owner, repo, filename))

    # Ensure resolved path is strictly inside base_dir
    try:
        common = os.path.commonpath([base_dir, override_path])
        if common != base_dir:
            return None
    except ValueError:
        return None

    return override_path


def deep_merge_config_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge repository override dictionary into base configuration dictionary.

    Merge semantics:
    - Scalar values: a repository override replaces the base value.
    - Tables/objects: merge recursively so that sibling values not mentioned by the
      override remain inherited from the base configuration.
    - Arrays/lists: if an override specifies the value, replace the entire base array
      rather than concatenating or partially merging it.
    - Missing override keys: inherit from the base configuration.
    - If incompatible override data is supplied (e.g., dict vs non-dict), fails with ValueError.

    Args:
        base: Base configuration dictionary.
        override: Repository override configuration dictionary.

    Returns:
        New merged configuration dictionary without mutating the original inputs.

    Raises:
        ValueError: If invalid or incompatible override data is supplied.
    """
    if not isinstance(base, dict):
        raise ValueError(f"Base configuration must be a dictionary, got {type(base).__name__}")
    if not isinstance(override, dict):
        raise ValueError(f"Override configuration must be a dictionary, got {type(override).__name__}")

    merged: Dict[str, Any] = copy.deepcopy(base)

    for key, override_val in override.items():
        if key not in merged:
            merged[key] = copy.deepcopy(override_val)
        else:
            base_val = merged[key]
            if isinstance(base_val, dict) and isinstance(override_val, dict):
                merged[key] = deep_merge_config_dict(base_val, override_val)
            elif isinstance(base_val, dict) != isinstance(override_val, dict):
                raise ValueError(f"Incompatible override type for key '{key}': base is {type(base_val).__name__}, " f"override is {type(override_val).__name__}")
            elif isinstance(base_val, list) and not isinstance(override_val, list) and override_val is not None:
                raise ValueError(f"Incompatible override type for key '{key}': base is list, " f"override is {type(override_val).__name__}")
            elif not isinstance(base_val, list) and isinstance(override_val, list):
                raise ValueError(f"Incompatible override type for key '{key}': base is {type(base_val).__name__}, " f"override is list")
            else:
                # Replace scalars or arrays as whole values
                merged[key] = copy.deepcopy(override_val)

    return merged


def _normalize_backend_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize environment / env_id alias keys inside a backend configuration dictionary."""
    if not isinstance(data, dict):
        return data
    norm = dict(data)
    if "environment" in norm:
        norm["environment_id"] = norm.pop("environment")
    if "env_id" in norm:
        norm["environment_id"] = norm.pop("env_id")
    return norm


def _normalize_config_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize aliases in configuration dictionary only for known backend tables."""
    if not isinstance(data, dict):
        return data

    result: Dict[str, Any] = {}
    backend_section_keys = {
        "backend_cloud",
        "backend_with_high_score",
        "backend_with_high_score_cloud",
        "backend_adversarial_validation",
    }

    for key, value in data.items():
        if key == "backends" and isinstance(value, dict):
            norm_backends: Dict[str, Any] = {}
            for b_name, b_val in value.items():
                if isinstance(b_val, dict):
                    norm_backends[b_name] = _normalize_backend_dict(b_val)
                else:
                    norm_backends[b_name] = b_val
            result[key] = norm_backends
        elif key in backend_section_keys and isinstance(value, dict):
            result[key] = _normalize_backend_dict(value)
        else:
            result[key] = value
    return result


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
    # For OpenRouter-compatible backends
    openrouter_api_key: Optional[str] = None
    openrouter_base_url: Optional[str] = None
    # For Claude Code CLI authentication
    claude_code_oauth_token: Optional[str] = None
    # For Claude Routine authentication and URL
    claude_code_routine_token: Optional[str] = None
    url: Optional[str] = None
    # Codex Cloud environment and best-of-N task settings
    environment_id: Optional[str] = None
    attempts: int = 1
    # For custom configurations
    extra_args: Dict[str, Any] = field(default_factory=dict)
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

    @property
    def environment(self) -> Optional[str]:
        """Alias for environment_id."""
        return self.environment_id

    @environment.setter
    def environment(self, value: Optional[str]) -> None:
        self.environment_id = value

    def __getattr__(self, name: str) -> Any:
        """Allow accessing unrecognized table keys from extra_args."""
        if name in ("extra_args", "__dataclass_fields__"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        extra = self.__dict__.get("extra_args")
        if isinstance(extra, dict) and name in extra:
            return extra[name]
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def validate_required_options(self, is_noedit: bool = False) -> List[str]:
        """Validate required options for the requested execution mode.

        No-edit Codex invocations do not rely on configured permission flags:
        ``CodexClient`` removes unsafe options and enforces its read-only,
        non-interactive policy. Editable Codex runs, however, need a configured
        unattended execution policy.

        Returns:
            List of error messages (empty if valid)
        """
        errors: List[str] = []
        # Use backend_type if available, otherwise fall back to name
        backend_type = self.backend_type or self.name

        if backend_type == "codex":
            if is_noedit:
                return errors

            options = set(self.options)
            has_unattended_policy = bool(
                options
                & {
                    "--dangerously-bypass-approvals-and-sandbox",
                    "--full-auto",
                    "--yolo",
                }
            )
            has_never_approval = any(option in {"--ask-for-approval=never", "-a=never", "-anever"} or (option in {"--ask-for-approval", "-a"} and index + 1 < len(self.options) and self.options[index + 1] == "never") for index, option in enumerate(self.options))
            has_writable_sandbox = any(option in {"--sandbox=workspace-write", "-s=workspace-write", "-sworkspace-write"} or (option in {"--sandbox", "-s"} and index + 1 < len(self.options) and self.options[index + 1] == "workspace-write") for index, option in enumerate(self.options))
            if has_unattended_policy or (has_never_approval and has_writable_sandbox):
                return errors

            errors.append(f"Backend '{self.name}' missing an unattended editable Codex execution policy. " "Configure --dangerously-bypass-approvals-and-sandbox, --full-auto, or " "--sandbox workspace-write with --ask-for-approval never.")
            return errors

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
    backend_with_high_score: Optional[BackendConfig] = None
    backend_with_high_score_order: List[str] = field(default_factory=list)
    # Cloud backend configuration (e.g., for standard/non-difficult cloud tasks)
    backend_cloud: Optional[BackendConfig] = None
    backend_cloud_order: List[str] = field(default_factory=list)
    # Cloud backend configuration with high score (e.g., for difficult issues)
    backend_with_high_score_cloud: Optional[BackendConfig] = None
    backend_with_high_score_cloud_order: List[str] = field(default_factory=list)
    # Backend configuration for strong-model adversarial validation
    backend_adversarial_validation: Optional[BackendConfig] = None
    backend_adversarial_validation_order: List[str] = field(default_factory=list)
    backend_adversarial_validation_default: Optional[str] = None
    # Environment variable overrides
    env_prefix: str = "AUTO_CODER_"
    # Configuration file path - relative to user's home directory
    config_file_path: str = "~/.auto-coder/llm_config.toml"

    def __post_init__(self) -> None:
        """Initialize default backends if none are configured."""
        if not self.backends:
            # Add default configurations for known backends
            default_backends = ["codex", "antigravity", "qwen", "auggie", "claude", "jules", "codex-mcp", "aider"]
            for backend_name in default_backends:
                self.backends[backend_name] = BackendConfig(name=backend_name)

            # Set default options for backends that require them
            # All backends here are newly created, so set required options for those that need them
            for backend_name, required_options in REQUIRED_OPTIONS_BY_BACKEND.items():
                if backend_name in self.backends and required_options:
                    # Set required options since these backends were just created
                    self.backends[backend_name].options = list(required_options)

    @classmethod
    def load_from_file(
        cls,
        config_path: Optional[str] = None,
        repo_name: Optional[str] = None,
    ) -> "LLMBackendConfiguration":
        """Load configuration from TOML file, optionally merging a repository override."""
        config_path = resolve_config_path(config_path)

        if not os.path.exists(config_path):
            if repo_name is None:
                # Create a default configuration file if none exists
                config = cls()
                config.save_to_file(config_path)
                return config
            base_data: Dict[str, Any] = {}
        else:
            try:
                with open(config_path, "rb") as f:
                    base_data = tomllib.load(f)
            except Exception as e:
                raise ValueError(f"Error loading configuration from {config_path}: {e}")

        base_data = _normalize_config_dict(base_data)
        effective_data = base_data

        # If repo_name is provided, check for repository-specific override
        if repo_name:
            override_path = resolve_repo_override_path(repo_name)
            if override_path and os.path.exists(override_path):
                try:
                    with open(override_path, "rb") as f:
                        override_data = tomllib.load(f)
                except Exception as e:
                    raise ValueError(f"Error loading repository configuration override from {override_path}: {e}")

                if not isinstance(override_data, dict):
                    raise ValueError(f"Repository configuration override at {override_path} must be a TOML table")

                override_data = _normalize_config_dict(override_data)
                effective_data = deep_merge_config_dict(base_data, override_data)

        try:
            return cls._load_from_data(effective_data, config_path=config_path)
        except Exception as e:
            if isinstance(e, ValueError):
                raise
            raise ValueError(f"Error loading configuration: {e}")

    @classmethod
    def load_from_dict(cls, data: Dict[str, Any]) -> "LLMBackendConfiguration":
        """Load configuration directly from a dictionary of configuration data."""

        try:
            return cls._load_from_data(_normalize_config_dict(data), config_path="<dict>")
        except Exception as e:
            raise ValueError(f"Error loading configuration from dict: {e}")

    @classmethod
    def _load_from_data(cls, data: Dict[str, Any], config_path: Optional[str] = None) -> "LLMBackendConfiguration":
        data = _normalize_config_dict(data)

        # Handle legacy "gemini" backend name translation
        def _translate_backend(val: Any) -> Any:
            if isinstance(val, str) and val == "gemini":
                return "antigravity"
            if isinstance(val, list):
                return ["antigravity" if v == "gemini" else v for v in val]
            return val

        # Parse general backend settings
        backend_order = _translate_backend(data.get("backend", {}).get("order", []))

        # Determine default backend - prioritize explicit "default" field, then order[0], then fallback to "codex"
        default_backend = _translate_backend(data.get("backend", {}).get("default"))
        if not default_backend:
            if backend_order:
                default_backend = backend_order[0]
            else:
                default_backend = "codex"

        # Parse backends
        backends_data = data.get("backends", {})
        if "gemini" in backends_data and "antigravity" not in backends_data:
            backends_data["antigravity"] = backends_data.pop("gemini")

        backends: Dict[str, BackendConfig] = {}

        # Helper to parse a backend config dict
        def parse_backend_config(name: str, config_data: dict) -> BackendConfig:
            # Set explicit flags based on whether options were actually specified in config
            options_explicitly_set = "options" in config_data
            options_for_noedit_explicitly_set = "options_for_noedit" in config_data

            known_keys = {
                "name",
                "enabled",
                "model",
                "api_key",
                "base_url",
                "temperature",
                "timeout",
                "max_retries",
                "openai_api_key",
                "openai_base_url",
                "openrouter_api_key",
                "openrouter_base_url",
                "claude_code_oauth_token",
                "claude_code_routine_token",
                "url",
                "environment_id",
                "environment",
                "env_id",
                "attempts",
                "extra_args",
                "providers",
                "usage_limit_retry_count",
                "usage_limit_retry_wait_seconds",
                "options",
                "backend_type",
                "model_provider",
                "always_switch_after_execution",
                "settings",
                "usage_markers",
                "options_for_noedit",
                "options_for_resume",
                "options_explicitly_set",
                "options_for_noedit_explicitly_set",
            }

            raw_extra_args = config_data.get("extra_args", {})
            extra_args = dict(raw_extra_args) if isinstance(raw_extra_args, dict) else {}
            for k, v in config_data.items():
                if k not in known_keys:
                    extra_args[k] = v

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
                openrouter_api_key=config_data.get("openrouter_api_key"),
                openrouter_base_url=config_data.get("openrouter_base_url"),
                claude_code_oauth_token=config_data.get("claude_code_oauth_token"),
                claude_code_routine_token=config_data.get("claude_code_routine_token"),
                url=config_data.get("url"),
                environment_id=config_data.get("environment_id") or config_data.get("environment") or config_data.get("env_id"),
                attempts=config_data.get("attempts", 1),
                extra_args=extra_args,
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
            if isinstance(config_data, dict):
                backends[name] = parse_backend_config(name, config_data)
                explicitly_configured_backends.add(name)

        # 2. Parse top-level backend definitions (e.g. [grok-4.1-fast])
        # This handles cases where TOML parses dotted keys as nested dictionaries
        # e.g. [grok-4.1-fast] -> {'grok-4': {'1-fast': {...}}}

        def is_potential_backend_config(d: dict) -> bool:
            # Heuristic: if it has specific backend keys, it's likely a config
            # We check for keys that are commonly used in backend definitions
            common_keys = {
                "backend_type",
                "model",
                "api_key",
                "base_url",
                "url",
                "claude_code_routine_token",
                "openai_api_key",
                "openai_base_url",
                "openrouter_api_key",
                "openrouter_base_url",
                "providers",
                "model_provider",
                "always_switch_after_execution",
                "settings",
                "options",
                "options_for_noedit",
                "options_for_resume",
            }
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
        reserved_keys = {"backend", "backend_cloud", "message_backend", "backend_for_noedit", "backends", "backend_with_high_score", "backend_with_high_score_cloud", "backend_adversarial_validation"}
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
        default_backends = ["codex", "antigravity", "qwen", "auggie", "claude", "jules", "codex-mcp", "aider"]
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
        backend_for_noedit_order = _translate_backend(data.get("backend_for_noedit", {}).get("order", []))

        # Determine default for noedit - prioritize explicit "default" field, then order[0]
        backend_for_noedit_default = _translate_backend(data.get("backend_for_noedit", {}).get("default"))
        if not backend_for_noedit_default and backend_for_noedit_order:
            backend_for_noedit_default = backend_for_noedit_order[0]

        # Backward compatibility: check old key if new key not found
        if not backend_for_noedit_order:
            old_order = _translate_backend(data.get("message_backend", {}).get("order", []))
            if old_order:
                logger = get_logger(__name__)
                logger.warning("Configuration uses deprecated 'message_backend' key. " "Please update to 'backend_for_noedit' in your config file.")
                backend_for_noedit_order = old_order
                if backend_for_noedit_order:
                    backend_for_noedit_default = backend_for_noedit_order[0]

        # If no specific default for noedit, fallback to general default
        if not backend_for_noedit_default:
            backend_for_noedit_default = default_backend

        # Parse backend_with_high_score section
        backend_with_high_score_data = data.get("backend_with_high_score", {})
        backend_with_high_score = None
        backend_with_high_score_order = []

        if backend_with_high_score_data:
            # Check for order
            backend_with_high_score_order = backend_with_high_score_data.get("order", [])

            # Use "backend_with_high_score" as default name if not specified in data
            # Only parse as a backend config if it has backend-like fields or if order is empty
            # If it has order, it might still have backend fields, but we prioritize order for the manager
            # We still parse it as a config in case it's used as a backend definition too
            fallback_name = backend_with_high_score_data.get("name", "backend_with_high_score")
            backend_with_high_score = parse_backend_config(fallback_name, backend_with_high_score_data)

        # Parse backend_cloud section
        backend_cloud_data = data.get("backend_cloud", {})
        backend_cloud = None
        backend_cloud_order = []

        if backend_cloud_data:
            backend_cloud_order = backend_cloud_data.get("order", [])
            fallback_name = backend_cloud_data.get("name", "backend_cloud")
            backend_cloud = parse_backend_config(fallback_name, backend_cloud_data)

        # Parse backend_with_high_score_cloud section
        backend_with_high_score_cloud_data = data.get("backend_with_high_score_cloud", {})
        backend_with_high_score_cloud = None
        backend_with_high_score_cloud_order = []

        if backend_with_high_score_cloud_data:
            backend_with_high_score_cloud_order = backend_with_high_score_cloud_data.get("order", [])
            fallback_name = backend_with_high_score_cloud_data.get("name", "backend_with_high_score_cloud")
            backend_with_high_score_cloud = parse_backend_config(fallback_name, backend_with_high_score_cloud_data)

        # Parse backend_adversarial_validation section
        backend_adversarial_validation_data = data.get("backend_adversarial_validation", {})
        backend_adversarial_validation = None
        backend_adversarial_validation_order = []
        backend_adversarial_validation_default = None

        if backend_adversarial_validation_data:
            backend_adversarial_validation_order = backend_adversarial_validation_data.get("order", [])
            backend_adversarial_validation_default = backend_adversarial_validation_data.get("default")
            fallback_name = backend_adversarial_validation_data.get("name", "backend_adversarial_validation")
            backend_adversarial_validation = parse_backend_config(fallback_name, backend_adversarial_validation_data)

        config = cls(
            backend_order=backend_order,
            default_backend=default_backend,
            backends=backends,
            backend_for_noedit_order=backend_for_noedit_order,
            backend_for_noedit_default=backend_for_noedit_default,
            backend_with_high_score=backend_with_high_score,
            backend_with_high_score_order=backend_with_high_score_order,
            backend_cloud=backend_cloud,
            backend_cloud_order=backend_cloud_order,
            backend_with_high_score_cloud=backend_with_high_score_cloud,
            backend_with_high_score_cloud_order=backend_with_high_score_cloud_order,
            backend_adversarial_validation=backend_adversarial_validation,
            backend_adversarial_validation_order=backend_adversarial_validation_order,
            backend_adversarial_validation_default=backend_adversarial_validation_default,
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
            raw_config = {
                "enabled": config.enabled,
                "model": config.model,
                "api_key": config.api_key,
                "base_url": config.base_url,
                "temperature": config.temperature,
                "timeout": config.timeout,
                "max_retries": config.max_retries,
                "openai_api_key": config.openai_api_key,
                "openai_base_url": config.openai_base_url,
                "openrouter_api_key": config.openrouter_api_key,
                "openrouter_base_url": config.openrouter_base_url,
                "claude_code_oauth_token": config.claude_code_oauth_token,
                "claude_code_routine_token": config.claude_code_routine_token,
                "url": config.url,
                "environment_id": config.environment_id,
                "attempts": config.attempts,
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
            backend_data[name] = {k: v for k, v in raw_config.items() if v is not None}

        # Prepare backend_with_high_score data
        backend_with_high_score_data = {}
        if self.backend_with_high_score:
            config = self.backend_with_high_score
            raw_config = {
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
                "openrouter_api_key": config.openrouter_api_key,
                "openrouter_base_url": config.openrouter_base_url,
                "claude_code_oauth_token": config.claude_code_oauth_token,
                "claude_code_routine_token": config.claude_code_routine_token,
                "url": config.url,
                "environment_id": config.environment_id,
                "attempts": config.attempts,
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
            backend_with_high_score_data = {k: v for k, v in raw_config.items() if v is not None}

        # Prepare backend_cloud data
        backend_cloud_data = {}
        if self.backend_cloud:
            config = self.backend_cloud
            raw_config = {
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
                "openrouter_api_key": config.openrouter_api_key,
                "openrouter_base_url": config.openrouter_base_url,
                "claude_code_oauth_token": config.claude_code_oauth_token,
                "claude_code_routine_token": config.claude_code_routine_token,
                "url": config.url,
                "environment_id": config.environment_id,
                "attempts": config.attempts,
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
            backend_cloud_data = {k: v for k, v in raw_config.items() if v is not None}
        elif self.backend_cloud_order:
            backend_cloud_data = {"order": self.backend_cloud_order}

        # Prepare backend_with_high_score_cloud data
        backend_with_high_score_cloud_data = {}
        if self.backend_with_high_score_cloud:
            config = self.backend_with_high_score_cloud
            raw_config = {
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
                "openrouter_api_key": config.openrouter_api_key,
                "openrouter_base_url": config.openrouter_base_url,
                "claude_code_oauth_token": config.claude_code_oauth_token,
                "claude_code_routine_token": config.claude_code_routine_token,
                "url": config.url,
                "environment_id": config.environment_id,
                "attempts": config.attempts,
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
            backend_with_high_score_cloud_data = {k: v for k, v in raw_config.items() if v is not None}
        elif self.backend_with_high_score_cloud_order:
            backend_with_high_score_cloud_data = {"order": self.backend_with_high_score_cloud_order}

        # Prepare backend_adversarial_validation data
        backend_adversarial_validation_data = {}
        if self.backend_adversarial_validation:
            config = self.backend_adversarial_validation
            raw_config = {
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
                "openrouter_api_key": config.openrouter_api_key,
                "openrouter_base_url": config.openrouter_base_url,
                "claude_code_oauth_token": config.claude_code_oauth_token,
                "claude_code_routine_token": config.claude_code_routine_token,
                "url": config.url,
                "environment_id": config.environment_id,
                "attempts": config.attempts,
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
            backend_adversarial_validation_data = {k: v for k, v in raw_config.items() if v is not None}
        elif self.backend_adversarial_validation_order:
            backend_adversarial_validation_data = {"order": self.backend_adversarial_validation_order}
            if self.backend_adversarial_validation_default:
                backend_adversarial_validation_data["default"] = self.backend_adversarial_validation_default

        data = {"backend": {"order": self.backend_order, "default": self.default_backend}, "backend_for_noedit": {"order": self.backend_for_noedit_order, "default": self.backend_for_noedit_default or self.default_backend}, "backends": backend_data}

        # Add backend_with_high_score section if configured
        if backend_with_high_score_data:
            data["backend_with_high_score"] = backend_with_high_score_data

        # Add backend_cloud section if configured
        if backend_cloud_data:
            data["backend_cloud"] = backend_cloud_data

        # Add backend_with_high_score_cloud section if configured
        if backend_with_high_score_cloud_data:
            data["backend_with_high_score_cloud"] = backend_with_high_score_cloud_data

        # Add backend_adversarial_validation section if configured
        if backend_adversarial_validation_data:
            data["backend_adversarial_validation"] = backend_adversarial_validation_data
        # Use os.open to ensure file is created with 600 permissions
        try:
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            # Fallback to standard open if os.open fails (e.g. some file systems)
            with open(config_path, "wb") as f:
                try:
                    os.chmod(config_path, 0o600)
                except OSError:
                    pass  # Ignore permission errors on systems that don't support it
                tomli_w.dump(data, f)
            return

        # File opened successfully with os.open
        try:
            f = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise

        with f:
            # Ensure permissions are correct even if file already existed
            try:
                os.chmod(config_path, 0o600)
            except OSError:
                pass  # Ignore permission errors on systems that don't support it
            tomli_w.dump(data, f)

    def get_backend_config(self, backend_name: str) -> Optional[BackendConfig]:
        """Get configuration for a specific backend."""
        config = self.backends.get(backend_name)
        if config:
            return config
        if self.backend_cloud and self.backend_cloud.name == backend_name:
            return self.backend_cloud
        if self.backend_with_high_score and self.backend_with_high_score.name == backend_name:
            return self.backend_with_high_score
        if self.backend_with_high_score_cloud and self.backend_with_high_score_cloud.name == backend_name:
            return self.backend_with_high_score_cloud
        if self.backend_adversarial_validation and self.backend_adversarial_validation.name == backend_name:
            return self.backend_adversarial_validation
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

    def get_backend_with_high_score(self) -> Optional[BackendConfig]:
        """Get the fallback backend configuration for failed PRs.

        Returns the backend_with_high_score configuration if configured, None otherwise.
        """
        return self.backend_with_high_score

    def get_model_for_backend_with_high_score(self) -> Optional[str]:
        """Get the model for the fallback backend for high-scoring PRs.

        Returns the model name if a fallback backend is configured and has a model,
        None otherwise.
        """
        if self.backend_with_high_score and self.backend_with_high_score.model:
            return self.backend_with_high_score.model
        return None

    def get_backend_cloud(self) -> Optional[BackendConfig]:
        """Get the cloud backend configuration.

        Returns the backend_cloud configuration if configured, None otherwise.
        """
        return self.backend_cloud

    def get_model_for_backend_cloud(self) -> Optional[str]:
        """Get the model for the cloud backend.

        Returns the model name if configured, None otherwise.
        """
        if self.backend_cloud and self.backend_cloud.model:
            return self.backend_cloud.model
        return None

    def get_backend_with_high_score_cloud(self) -> Optional[BackendConfig]:
        """Get the cloud backend configuration with high score.

        Returns the backend_with_high_score_cloud configuration if configured, None otherwise.
        """
        return self.backend_with_high_score_cloud

    def get_model_for_backend_with_high_score_cloud(self) -> Optional[str]:
        """Get the model for the high-scoring cloud backend.

        Returns the model name if configured, None otherwise.
        """
        if self.backend_with_high_score_cloud and self.backend_with_high_score_cloud.model:
            return self.backend_with_high_score_cloud.model
        return None

    def get_backend_adversarial_validation(self) -> Optional[BackendConfig]:
        """Get the adversarial validation backend configuration."""
        return self.backend_adversarial_validation

    def get_adversarial_validation_backend_order(self) -> List[str]:
        """Get the adversarial validation backend order."""
        return list(self.backend_adversarial_validation_order)

    def get_adversarial_validation_default_backend(self) -> Optional[str]:
        """Get the default backend for adversarial validation."""
        if self.backend_adversarial_validation_default:
            return self.backend_adversarial_validation_default
        if self.backend_adversarial_validation_order:
            return self.backend_adversarial_validation_order[0]
        if self.backend_adversarial_validation:
            return self.backend_adversarial_validation.name
        return None

    def get_model_for_backend_adversarial_validation(self) -> Optional[str]:
        """Get the model for the adversarial validation backend."""
        if self.backend_adversarial_validation and self.backend_adversarial_validation.model:
            return self.backend_adversarial_validation.model
        return None

    def get_model_for_backend(self, backend_name: str) -> Optional[str]:
        """Get the model for a specific backend, with fallback to backend defaults."""
        config = self.get_backend_config(backend_name)
        if config and config.model:
            return config.model

        # Default models for known backends
        default_models = {"antigravity": "gemini-2.5-pro", "qwen": "qwen3-coder-plus", "auggie": "GPT-5", "claude": "sonnet", "codex": "codex", "jules": "jules", "codex-mcp": "codex-mcp", "aider": "aider"}
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

            claude_code_oauth_token_env = os.environ.get(f"AUTO_CODER_{backend_name.upper()}_CLAUDE_CODE_OAUTH_TOKEN")
            if claude_code_oauth_token_env:
                backend_config.claude_code_oauth_token = claude_code_oauth_token_env

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


_active_repo_name: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("active_repo_name", default=None)


def set_active_repo_name(repo_name: Optional[str]) -> None:
    """Set the active repository name in the current context."""
    _active_repo_name.set(repo_name)


def get_active_repo_name() -> Optional[str]:
    """Get the active repository name in the current context."""
    return _active_repo_name.get()


@contextmanager
def active_repo_context(repo_name: Optional[str]):
    """Context manager to set the active repository for LLM configuration resolution."""
    token = _active_repo_name.set(repo_name)
    try:
        yield
    finally:
        _active_repo_name.reset(token)


# Global base instance to be used throughout the application
_llm_config: Optional[LLMBackendConfiguration] = None


def get_llm_config(
    repo_name: Optional[str] = None,
    config_path: Optional[str] = None,
) -> LLMBackendConfiguration:
    """Get the effective LLM backend configuration.

    If repo_name is provided or set via active_repo_context, resolves the effective
    configuration by loading the base configuration and merging the repository-specific
    override at ~/.auto-coder/<owner>/<repo>/llm_config.toml.

    Args:
        repo_name: Optional GitHub repository ('owner/repo')
        config_path: Optional explicit configuration file path

    Returns:
        LLMBackendConfiguration instance with effective configuration
    """
    effective_repo = repo_name if repo_name is not None else get_active_repo_name()

    if effective_repo:
        config = LLMBackendConfiguration.load_from_file(config_path=config_path, repo_name=effective_repo)
        config.apply_env_overrides()
        return config

    global _llm_config
    if config_path is not None:
        config = LLMBackendConfiguration.load_from_file(config_path=config_path)
        config.apply_env_overrides()
        return config

    if _llm_config is None:
        _llm_config = LLMBackendConfiguration.load_from_file()
        _llm_config.apply_env_overrides()
    return _llm_config


def reset_llm_config() -> None:
    """Reset the global configuration instance (for testing)."""
    global _llm_config
    _llm_config = None
    _active_repo_name.set(None)


def load_app_config_data(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Load configuration from config.toml, optionally merging a repository override.

    Args:
        config_path: Optional explicit path to base config.toml file.
        repo_name: Optional GitHub repository ('owner/repo'). If not provided,
                   checks get_active_repo_name().

    Returns:
        Merged configuration dictionary.
    """
    base_data: Dict[str, Any] = {}

    if config_path:
        if os.path.exists(config_path):
            try:
                with open(config_path, "rb") as f:
                    base_data = tomllib.load(f)
            except Exception as e:
                logger = get_logger(__name__)
                logger.warning(f"Failed to read config.toml from {config_path}: {e}")
    else:
        # Check standard locations (local first, then home)
        config_paths = [
            os.path.join(os.getcwd(), ".auto-coder", "config.toml"),
            os.path.expanduser("~/.auto-coder/config.toml"),
        ]
        for path in config_paths:
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        base_data = tomllib.load(f)
                    break
                except Exception as e:
                    logger = get_logger(__name__)
                    logger.warning(f"Failed to read config.toml from {path}: {e}")
                    continue

    if not isinstance(base_data, dict):
        base_data = {}

    effective_repo = repo_name if repo_name is not None else get_active_repo_name()
    if effective_repo:
        override_path = resolve_repo_override_path(effective_repo, filename="config.toml")
        if override_path and os.path.exists(override_path):
            try:
                with open(override_path, "rb") as f:
                    override_data = tomllib.load(f)
            except Exception as e:
                raise ValueError(f"Error loading repository configuration override from {override_path}: {e}")

            if not isinstance(override_data, dict):
                raise ValueError(f"Repository configuration override at {override_path} must be a TOML table")

            base_data = deep_merge_config_dict(base_data, override_data)

    return base_data


def _get_config_value(
    section: str,
    key: str,
    default: Any,
    config_path: Optional[str] = None,
    value_type: Optional[type] = None,
    repo_name: Optional[str] = None,
) -> Any:
    """Helper to get a value from config.toml with standard lookup paths and repo overrides.

    Args:
        section: TOML section name (e.g., 'jules')
        key: Key name within section (e.g., 'enabled')
        default: Default value if not found
        config_path: Optional explicit path to config file
        value_type: Optional type to cast the value to (e.g., int, bool)
        repo_name: Optional repository name ('owner/repo') for repository-scoped override.

    Returns:
        The configured value or default
    """
    try:
        data = load_app_config_data(config_path=config_path, repo_name=repo_name)
    except ValueError:
        raise
    except Exception as e:
        logger = get_logger(__name__)
        logger.warning(f"Failed to load config data: {e}")
        return default

    section_config = data.get(section, {})
    if isinstance(section_config, dict) and key in section_config:
        val = section_config[key]
        return value_type(val) if value_type else val

    return default


def get_jules_enabled_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> bool:
    """Check if Jules is enabled via [jules].enabled in config.toml.

    This function reads from ~/.auto-coder/config.toml (or local .auto-coder/config.toml)
    and checks for a [jules] section with an 'enabled' field, applying any repo-scoped override.

    Args:
        config_path: Optional explicit path to config.toml file. If not provided,
                    will check standard locations.
        repo_name: Optional GitHub repository name in 'owner/repo' format.

    Returns:
        True if Jules is enabled (default), False if explicitly disabled.
    """
    return _get_config_value(
        section="jules",
        key="enabled",
        default=True,
        config_path=config_path,
        value_type=bool,
        repo_name=repo_name,
    )


def is_jules_mode_enabled(repo_name: Optional[str] = None) -> bool:
    """Check if Jules mode or Cloud mode is enabled.

    Cloud/Jules mode is enabled if:
    1. A cloud backend is configured via [backend_cloud] in llm_config.toml, OR
    2. The 'jules' backend is enabled in llm_config.toml and [jules].enabled is true in config.toml
    """
    llm_config = get_llm_config(repo_name=repo_name)
    if llm_config.backend_cloud_order or llm_config.backend_cloud:
        return True

    # Check [backends.jules] in llm_config.toml
    jules_config = llm_config.get_backend_config("jules")
    jules_backend_enabled = jules_config.enabled if jules_config else False

    # Check [jules].enabled in config.toml
    jules_config_enabled = get_jules_enabled_from_config(repo_name=repo_name)

    return jules_backend_enabled and jules_config_enabled


def is_cloud_mode_enabled(repo_name: Optional[str] = None) -> bool:
    """Check if Cloud mode (backend_cloud or Jules) is enabled."""
    return is_jules_mode_enabled(repo_name=repo_name)


def get_jules_wait_timeout_hours_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get the Jules wait timeout in hours from config.toml.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Timeout in hours (default: 2)
    """
    return _get_config_value(
        section="jules",
        key="wait_timeout_hours",
        default=2,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_jules_issue_pr_timeout_hours_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get the maximum time Jules may work on an issue without opening a PR.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Timeout in hours (default: 12)
    """
    return _get_config_value(
        section="jules",
        key="issue_pr_timeout_hours",
        default=12,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_jules_pr_ci_timeout_hours_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get the maximum time a Jules PR may stay open without passing CI.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Timeout in hours (default: 12)
    """
    return _get_config_value(
        section="jules",
        key="pr_ci_timeout_hours",
        default=12,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_github_action_log_max_length_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get the GitHub Action log max length from config.toml.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Max length (default: 50000)
    """
    return _get_config_value(
        section="github_action",
        key="max_log_length",
        default=50000,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_jules_session_expiration_days_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get the Jules session expiration in days from config.toml.

    Looks for [jules] session_expiration_days in config.toml.
    Default is 7 days.
    """
    return _get_config_value(
        section="jules",
        key="session_expiration_days",
        default=7,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_process_issues_sleep_time_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get process_issues sleep time from [process_issues].sleep_time in config.toml.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Sleep time in seconds (default: 300)
    """
    return _get_config_value(
        section="process_issues",
        key="sleep_time",
        default=300,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_process_issues_empty_sleep_time_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get process_issues empty sleep time from [process_issues].empty_sleep_time in config.toml.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Sleep time in seconds (default: 600)
    """
    return _get_config_value(
        section="process_issues",
        key="empty_sleep_time",
        default=600,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_process_issues_max_open_prs_for_issues_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> int:
    """Get max open PRs count for issue processing from [process_issues].max_open_prs_for_issues in config.toml.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Max open PRs count (default: 3)
    """
    val = _get_config_value(
        section="process_issues",
        key="max_open_prs_for_issues",
        default=None,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )
    if val is not None:
        return val
    return _get_config_value(
        section="process_issues",
        key="max_open_prs",
        default=3,
        config_path=config_path,
        value_type=int,
        repo_name=repo_name,
    )


def get_isolate_single_test_on_failure_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> bool:
    """Get isolate_single_test_on_failure setting from [test] section in config.toml.

    When enabled, if multiple tests fail, the system will extract and re-run only
    the first failed test in isolation. This can help identify flaky or unstable tests.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        True if isolation is enabled, False otherwise (default: False)
    """
    return _get_config_value(
        section="test",
        key="isolate_single_test_on_failure",
        default=False,
        config_path=config_path,
        value_type=bool,
        repo_name=repo_name,
    )


def get_issue_allowlist_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> Optional[List[int]]:
    """Get the issue author allowlist from config.toml [github].issue_allowlist.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        List of allowed GitHub user IDs (ints) or None if not configured.
    """
    raw_list = _get_config_value(
        section="github",
        key="issue_allowlist",
        default=None,
        config_path=config_path,
        repo_name=repo_name,
    )
    if raw_list is None:
        return None
    result: List[int] = []
    for item in raw_list:
        try:
            result.append(int(item))
        except (ValueError, TypeError):
            pass
    return result


def get_pr_allowlist_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> Optional[List[int]]:
    """Get the pull request author allowlist from config.toml [github].pr_allowlist.

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        List of allowed GitHub user IDs (ints) or None if not configured.
    """
    raw_list = _get_config_value(
        section="github",
        key="pr_allowlist",
        default=None,
        config_path=config_path,
        repo_name=repo_name,
    )
    if raw_list is None:
        return None
    result: List[int] = []
    for item in raw_list:
        try:
            result.append(int(item))
        except (ValueError, TypeError):
            pass
    return result


def get_adversarial_validation_max_reviews_from_config(
    config_path: Optional[str] = None,
    repo_name: Optional[str] = None,
) -> Optional[int]:
    """Get the maximum number of adversarial review executions from config.toml.

    Looks for max_reviews in [adversarial_validation] section in config.toml
    (or fallback keys: max_validations, max_attempts, max_count).

    Args:
        config_path: Optional explicit path to config.toml file.
        repo_name: Optional repository name in 'owner/repo' format.

    Returns:
        Maximum review executions (int) or None if not configured.
    """
    for key in ("max_reviews", "max_validations", "max_attempts", "max_count"):
        val = _get_config_value(
            section="adversarial_validation",
            key=key,
            default=None,
            config_path=config_path,
            value_type=int,
            repo_name=repo_name,
        )
        if val is not None:
            return val
    for key in ("max_reviews", "max_validations", "max_attempts"):
        val = _get_config_value(
            section="backend_adversarial_validation",
            key=key,
            default=None,
            config_path=config_path,
            value_type=int,
            repo_name=repo_name,
        )
        if val is not None:
            return val
    return None
