"""
LLM Backend Configuration Management Module

This module provides the core configuration infrastructure for LLM backend settings,
migrating from CLI options to TOML configuration file.
"""

import copy
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import toml

# Import for validation
try:
    import pydantic
    from pydantic import BaseModel, Field, validator

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False

    # Define basic stubs when pydantic is not available
    class BaseModel:  # type: ignore
        pass

    def Field(**kwargs):  # type: ignore
        return lambda x: x

    validator = lambda *args, **kwargs: lambda x: x


# Define configuration models
@dataclass
class BackendConfig:
    """Base configuration for an LLM backend."""

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    timeout: Optional[int] = None
    max_retries: Optional[int] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CodexBackendConfig(BackendConfig):
    """Configuration for Codex backend."""

    pass  # Codex-specific config options if needed


@dataclass
class CodexMCPBackendConfig(BackendConfig):
    """Configuration for Codex MCP backend."""

    pass  # Codex-MCP specific config options if needed


@dataclass
class GeminiBackendConfig(BackendConfig):
    """Configuration for Gemini backend."""

    # Gemini-specific settings
    pass


@dataclass
class QwenBackendConfig(BackendConfig):
    """Configuration for Qwen backend."""

    # Qwen-specific settings
    pass


@dataclass
class ClaudeBackendConfig(BackendConfig):
    """Configuration for Claude backend."""

    # Claude-specific settings
    pass


@dataclass
class AuggieBackendConfig(BackendConfig):
    """Configuration for Auggie backend."""

    # Auggie-specific settings
    pass


@dataclass
class LLMBackendConfig:
    """
    Configuration management class for LLM backends.

    Manages configuration for different LLM backends including:
    - Codex, Codex-MCP, Gemini, Qwen, Claude, Auggie
    - API keys, model names, endpoints, etc.
    - Configuration validation and persistence
    """

    # Configuration version for future updates
    CONFIG_VERSION: str = "1.0.0"

    # Configuration file paths
    DEFAULT_CONFIG_DIR = Path.home() / ".auto-coder"
    DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "llm_backend.toml"

    # Backend configuration instances
    codex: CodexBackendConfig = field(default_factory=CodexBackendConfig)
    codex_mcp: CodexMCPBackendConfig = field(default_factory=CodexMCPBackendConfig)
    gemini: GeminiBackendConfig = field(default_factory=GeminiBackendConfig)
    qwen: QwenBackendConfig = field(default_factory=QwenBackendConfig)
    claude: ClaudeBackendConfig = field(default_factory=ClaudeBackendConfig)
    auggie: AuggieBackendConfig = field(default_factory=AuggieBackendConfig)

    # General configuration settings
    version: str = CONFIG_VERSION
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # Configuration management settings
    _config_file_path: Optional[Path] = field(default=None, init=False)
    _original_config_hash: Optional[str] = field(default=None, init=False)
    _is_loaded: bool = field(default=False, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def __post_init__(self) -> None:
        """Initialize configuration after dataclass creation."""
        if self._config_file_path is None:
            self._config_file_path = self.DEFAULT_CONFIG_FILE
        if not self._is_loaded:
            self._original_config_hash = self._calculate_config_hash()

    def __getstate__(self) -> Dict[str, Any]:
        """Custom pickling method to handle the threading lock."""
        state = self.__dict__.copy()
        # Remove the unpickleable _lock attribute
        del state["_lock"]
        return state

    def __setstate__(self, state: Dict[str, Any]) -> None:
        """Custom unpickling method to restore the threading lock."""
        # Restore the object's state
        self.__dict__.update(state)
        # Restore the lock
        self._lock = threading.RLock()

    def _calculate_config_hash(self) -> str:
        """Calculate a hash of the current configuration for change detection."""
        # Convert config to a representation that can be serialized
        config_dict = self.to_dict()
        # Remove timestamps to avoid false changes
        config_dict.pop("updated_at", None)
        config_dict.pop("created_at", None)
        config_str = json.dumps(config_dict, sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()

    def has_changes(self) -> bool:
        """Check if the configuration has been modified since last load/save."""
        with self._lock:
            current_hash = self._calculate_config_hash()
            return current_hash != self._original_config_hash

    def to_dict(self) -> Dict[str, Any]:
        """Convert the configuration to a dictionary."""
        result = {
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "backends": {
                "codex": asdict(self.codex),
                "codex_mcp": asdict(self.codex_mcp),
                "gemini": asdict(self.gemini),
                "qwen": asdict(self.qwen),
                "claude": asdict(self.claude),
                "auggie": asdict(self.auggie),
            },
        }
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LLMBackendConfig":
        """Create a configuration instance from a dictionary."""
        # Create a new instance with default values
        instance = cls()

        # Update with provided data
        instance.version = data.get("version", cls.CONFIG_VERSION)
        instance.created_at = data.get("created_at", datetime.now().isoformat())
        instance.updated_at = data.get("updated_at", datetime.now().isoformat())

        # Load backend configurations
        backends = data.get("backends", {})

        if "codex" in backends:
            codex_data = backends["codex"]
            for key, value in codex_data.items():
                setattr(instance.codex, key, value)

        if "codex_mcp" in backends:
            codex_mcp_data = backends["codex_mcp"]
            for key, value in codex_mcp_data.items():
                setattr(instance.codex_mcp, key, value)

        if "gemini" in backends:
            gemini_data = backends["gemini"]
            for key, value in gemini_data.items():
                setattr(instance.gemini, key, value)

        if "qwen" in backends:
            qwen_data = backends["qwen"]
            for key, value in qwen_data.items():
                setattr(instance.qwen, key, value)

        if "claude" in backends:
            claude_data = backends["claude"]
            for key, value in claude_data.items():
                setattr(instance.claude, key, value)

        if "auggie" in backends:
            auggie_data = backends["auggie"]
            for key, value in auggie_data.items():
                setattr(instance.auggie, key, value)

        return instance

    def save_to_file(self, file_path: Optional[Path] = None) -> None:
        """Save configuration to a TOML file with atomic write."""
        with self._lock:
            target_path = file_path or self._config_file_path

            if target_path is None:
                raise ValueError("No target path specified for save operation")

            # Update the stored config file path if a new path was provided
            if file_path is not None:
                self._config_file_path = file_path

            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)

            # Create a temporary file in the same directory for atomic write
            with tempfile.NamedTemporaryFile(mode="w", dir=target_path.parent, delete=False, suffix=".tmp") as tmp_file:
                temp_path = Path(tmp_file.name)
                # Update the timestamp
                self.updated_at = datetime.now().isoformat()
                # Write the configuration as TOML
                toml_data = self.to_dict()
                toml.dump(toml_data, tmp_file)

            # Atomically replace the target file
            shutil.move(str(temp_path), str(target_path))

            # Update the original config hash
            self._original_config_hash = self._calculate_config_hash()
            self._is_loaded = True

    @classmethod
    def load_from_file(cls, file_path: Optional[Path] = None) -> "LLMBackendConfig":
        """Load configuration from a TOML file."""
        target_path = file_path or cls.DEFAULT_CONFIG_FILE

        if target_path is None:
            raise ValueError("No target path specified for load operation")

        if not target_path.exists():
            # Return default configuration if file doesn't exist
            config = cls()
            config._config_file_path = target_path
            config._is_loaded = True
            return config

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                toml_data = toml.load(f)

            config = cls.from_dict(toml_data)
            config._config_file_path = target_path
            config._original_config_hash = config._calculate_config_hash()
            config._is_loaded = True
            return config
        except Exception as e:
            logging.error(f"Failed to load configuration from {target_path}: {e}")
            # Return default configuration if loading fails
            config = cls()
            config._config_file_path = target_path
            config._is_loaded = True
            return config

    def generate_default_config(self) -> Dict[str, Any]:
        """Generate a default configuration template."""
        default_config = {
            "version": self.CONFIG_VERSION,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "backends": {
                "codex": asdict(CodexBackendConfig()),
                "codex_mcp": asdict(CodexMCPBackendConfig()),
                "gemini": asdict(GeminiBackendConfig()),
                "qwen": asdict(QwenBackendConfig()),
                "claude": asdict(ClaudeBackendConfig()),
                "auggie": asdict(AuggieBackendConfig()),
            },
            "defaults": {"backend": "codex", "fallback_order": ["codex", "gemini", "qwen", "auggie", "claude", "codex-mcp"]},  # Default backend to use
        }
        return default_config

    def save_default_config(self, file_path: Optional[Path] = None) -> None:
        """Save the default configuration template to file."""
        target_path = file_path or self._config_file_path

        if target_path is None:
            raise ValueError("No target path specified for save operation")

        default_config = self.generate_default_config()

        # Ensure parent directory exists
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Write the default configuration
        with open(target_path, "w", encoding="utf-8") as f:
            # Add comment about TOML format for syntax highlighting in editors
            f.write("# Auto-Coder LLM Backend Configuration (TOML format)\n")
            f.write("# For syntax highlighting, most editors will recognize .toml extension\n\n")
            toml.dump(default_config, f)

    def validate_config(self) -> List[str]:
        """Validate the configuration and return a list of validation errors."""
        errors = []

        # Validate API key formats if provided
        all_backends = [
            ("codex", self.codex),
            ("codex_mcp", self.codex_mcp),
            ("gemini", self.gemini),
            ("qwen", self.qwen),
            ("claude", self.claude),
            ("auggie", self.auggie),
        ]

        for backend_name, backend_config in all_backends:
            if backend_config.api_key is not None and len(backend_config.api_key.strip()) > 0:
                # Basic API key validation - just check if it's a reasonable length
                # Only validate length if API key is not None or empty after stripping
                # Minimum 8 characters to accommodate common test values while still being reasonably secure
                if len(backend_config.api_key.strip()) < 8:
                    errors.append(f"API key for {backend_name} appears to be too short")

            # Validate model name format if provided
            if backend_config.model and len(backend_config.model.strip()) > 0:
                # Basic model name validation
                if not re.match(r"^[\w\-.]+$", backend_config.model):
                    errors.append(f"Model name '{backend_config.model}' for {backend_name} contains invalid characters")

        return errors

    def validate_and_save(self, file_path: Optional[Path] = None) -> bool:
        """Validate configuration and save if valid."""
        validation_errors = self.validate_config()
        if validation_errors:
            logging.error(f"Configuration validation failed: {', '.join(validation_errors)}")
            return False

        self.save_to_file(file_path)
        return True

    def create_backup(self) -> Path:
        """Create a backup of the current configuration file as it exists on disk."""
        if self._config_file_path is None:
            raise ValueError("Cannot create backup: no config file path specified")

        # If the config file doesn't exist on disk, save it first
        if not self._config_file_path.exists():
            self.save_to_file()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = self._config_file_path.with_suffix(f".{timestamp}.bak")

        shutil.copy2(self._config_file_path, backup_path)
        return backup_path

    def import_config(self, import_path: Path) -> bool:
        """Import configuration from another file."""
        try:
            imported_config = self.load_from_file(import_path)

            # Copy the imported configuration to this instance
            self.codex = imported_config.codex
            self.codex_mcp = imported_config.codex_mcp
            self.gemini = imported_config.gemini
            self.qwen = imported_config.qwen
            self.claude = imported_config.claude
            self.auggie = imported_config.auggie

            self.version = imported_config.version
            self.created_at = imported_config.created_at
            self.updated_at = imported_config.updated_at

            # Update the original config hash to match the imported config
            self._original_config_hash = imported_config._original_config_hash

            return True
        except Exception as e:
            logging.error(f"Failed to import configuration from {import_path}: {e}")
            return False

    def export_config(self, export_path: Path) -> bool:
        """Export current configuration to another file."""
        try:
            # Create a new config instance from current data to avoid pickling issues
            export_data = self.to_dict()
            export_config = LLMBackendConfig.from_dict(export_data)
            export_config._config_file_path = export_path
            export_config.save_to_file(export_path)
            return True
        except Exception as e:
            logging.error(f"Failed to export configuration to {export_path}: {e}")
            return False

    def get_backend_config(self, backend_name: str) -> Optional[BackendConfig]:
        """Get configuration for a specific backend."""
        backend_map = {
            "codex": self.codex,
            "codex_mcp": self.codex_mcp,
            "gemini": self.gemini,
            "qwen": self.qwen,
            "claude": self.claude,
            "auggie": self.auggie,
        }

        return backend_map.get(backend_name)

    def set_backend_config(self, backend_name: str, config: BackendConfig) -> bool:
        """Set configuration for a specific backend."""
        backend_map = {
            "codex": "codex",
            "codex_mcp": "codex_mcp",
            "gemini": "gemini",
            "qwen": "qwen",
            "claude": "claude",
            "auggie": "auggie",
        }

        attr_name = backend_map.get(backend_name)
        if attr_name:
            setattr(self, attr_name, config)
            return True
        return False

    def apply_environment_overrides(self) -> None:
        """Apply environment variable overrides to the configuration."""
        # Define mapping of environment variables to configuration paths
        env_mapping = {
            "AUTO_CODER_CODEX_API_KEY": ("codex", "api_key"),
            "AUTO_CODER_CODEX_MCP_API_KEY": ("codex_mcp", "api_key"),
            "GEMINI_API_KEY": ("gemini", "api_key"),
            "QWEN_API_KEY": ("qwen", "api_key"),
            "CLAUDE_API_KEY": ("claude", "api_key"),
            "AUGGIE_API_KEY": ("auggie", "api_key"),
            "AUTO_CODER_CODEX_MODEL": ("codex", "model"),
            "AUTO_CODER_CODEX_MCP_MODEL": ("codex_mcp", "model"),
            "GEMINI_MODEL": ("gemini", "model"),
            "QWEN_MODEL": ("qwen", "model"),
            "CLAUDE_MODEL": ("claude", "model"),
            "AUGGIE_MODEL": ("auggie", "model"),
            "AUTO_CODER_CODEX_BASE_URL": ("codex", "base_url"),
            "AUTO_CODER_CODEX_MCP_BASE_URL": ("codex_mcp", "base_url"),
            "GEMINI_BASE_URL": ("gemini", "base_url"),
            "QWEN_BASE_URL": ("qwen", "base_url"),
            "CLAUDE_BASE_URL": ("claude", "base_url"),
            "AUGGIE_BASE_URL": ("auggie", "base_url"),
        }

        for env_var, (backend_attr, config_attr) in env_mapping.items():
            env_value = os.getenv(env_var)
            if env_value is not None:
                backend_config = getattr(self, backend_attr)
                setattr(backend_config, config_attr, env_value)

    def get_diff(self, other_config: "LLMBackendConfig") -> Dict[str, Any]:
        """Get the difference between this config and another config."""
        current_dict = self.to_dict()  # This is the "self" config
        other_dict = other_config.to_dict()  # This is the "other" config

        diff = {}
        for key in set(current_dict.keys()) | set(other_dict.keys()):
            if key == "updated_at" or key == "created_at":
                continue  # Skip timestamps for diff

            if key not in other_dict:
                diff[key] = {"added": current_dict[key]}
            elif key not in current_dict:
                diff[key] = {"removed": other_dict[key]}
            elif current_dict[key] != other_dict[key]:
                # The "old" value is the one in the current config (self),
                # the "new" value is the one in the other config (parameter)
                diff[key] = {"old": current_dict[key], "new": other_dict[key]}

        return diff


# Singleton pattern for global configuration management
class LLMBackendConfigManager:
    """
    Singleton manager for LLM backend configuration.

    Provides a single point of access for configuration throughout the application,
    with thread-safe operations and change detection.
    """

    _instance = None
    _lock = threading.Lock()
    config: Optional["LLMBackendConfig"]
    config_path: Optional[Path]
    _config_lock: threading.RLock

    def __init__(self) -> None:
        # Initialize instance attributes if not already done
        if not hasattr(self, "config"):
            self.config = None
        if not hasattr(self, "config_path"):
            self.config_path = None
        if not hasattr(self, "_config_lock"):
            self._config_lock = threading.RLock()

    def __new__(cls) -> "LLMBackendConfigManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    # Initialize instance attributes
                    cls._instance.config = None
                    cls._instance.config_path = None
                    cls._instance._config_lock = threading.RLock()
        return cls._instance

    def initialize(self, config_path: Optional[Path] = None) -> None:
        """Initialize the configuration manager with a specific config file."""
        with self._config_lock:
            self.config_path = config_path or LLMBackendConfig.DEFAULT_CONFIG_FILE
            self.config = LLMBackendConfig.load_from_file(self.config_path)

            # Apply environment variable overrides
            self.config.apply_environment_overrides()

    def get_config(self) -> LLMBackendConfig:
        """Get the current configuration instance."""
        if self.config is None:
            self.initialize()
        # After initialization, self.config should not be None
        assert self.config is not None
        return self.config

    def save_config(self) -> bool:
        """Save the current configuration to file."""
        if self.config is None:
            return False

        return self.config.validate_and_save()

    def has_config_changes(self) -> bool:
        """Check if the configuration has been modified since last save."""
        if self.config is None:
            return False
        return self.config.has_changes()

    def reload_config(self) -> None:
        """Reload the configuration from file."""
        with self._config_lock:
            if self.config_path and self.config is not None:
                # Load fresh config from file
                fresh_config = LLMBackendConfig.load_from_file(self.config_path)
                fresh_config.apply_environment_overrides()

                # Update the current config in place to maintain references
                self.config.codex = fresh_config.codex
                self.config.codex_mcp = fresh_config.codex_mcp
                self.config.gemini = fresh_config.gemini
                self.config.qwen = fresh_config.qwen
                self.config.claude = fresh_config.claude
                self.config.auggie = fresh_config.auggie

                self.config.version = fresh_config.version
                self.config.created_at = fresh_config.created_at
                self.config.updated_at = fresh_config.updated_at
                self.config._config_file_path = fresh_config._config_file_path
                self.config._original_config_hash = fresh_config._original_config_hash
                self.config._is_loaded = fresh_config._is_loaded


# Global function for easy access to the configuration
def get_llm_backend_config() -> LLMBackendConfig:
    """Get the global LLM backend configuration."""
    manager = LLMBackendConfigManager()
    return manager.get_config()


def initialize_llm_backend_config(config_path: Optional[Path] = None) -> None:
    """Initialize the LLM backend configuration manager."""
    manager = LLMBackendConfigManager()
    manager.initialize(config_path)


# Function to ensure the configuration directory exists
def ensure_config_directory() -> None:
    """Ensure that the configuration directory exists."""
    config_dir = LLMBackendConfig.DEFAULT_CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
