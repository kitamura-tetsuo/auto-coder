"""
Configuration management for Jules.

This module provides JulesConfig to load and manage Jules-specific
configuration from ~/.auto-coder/config.toml.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import toml

logger = None  # Will be set in __init__.py


@dataclass
class JulesConfig:
    """Jules configuration settings.

    Loads configuration from the [jules] section in ~/.auto-coder/config.toml.
    """

    # Path to config file
    config_file_path: str = field(default="~/.auto-coder/config.toml")

    # Jules-specific settings
    enabled: bool = field(default=True)

    # Additional configuration options from [jules] section
    extra_config: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load_from_file(cls, config_path: Optional[str] = None) -> "JulesConfig":
        """Load Jules configuration from TOML file.

        Args:
            config_path: Optional path to config file. If None, uses default path.

        Returns:
            JulesConfig instance loaded from the config file
        """
        if config_path is None:
            config_path = cls().config_file_path

        config_path = os.path.expanduser(config_path)

        if not os.path.exists(config_path):
            # Return default config if file doesn't exist
            return cls(config_file_path=config_path)

        try:
            with open(config_path, "r") as f:
                data = toml.load(f)

            # Parse jules section
            jules_data = data.get("jules", {})

            # Extract known fields with defaults
            enabled = jules_data.get("enabled", True)
            extra_config = {k: v for k, v in jules_data.items() if k not in {"enabled"}}

            return cls(config_file_path=config_path, enabled=enabled, extra_config=extra_config)
        except Exception as e:
            # Return default config on error
            return cls(config_file_path=config_path)

    def get_jules_section(self) -> Dict[str, str]:
        """Get the raw [jules] section from config as a dictionary.

        Returns:
            Dictionary containing the [jules] section configuration
        """
        import toml

        config_file_path = os.path.expanduser(self.config_file_path)

        if not os.path.exists(config_file_path):
            return {}

        try:
            with open(config_file_path, "r") as f:
                config = toml.load(f)
                return config.get("jules", {})
        except Exception:
            return {}
