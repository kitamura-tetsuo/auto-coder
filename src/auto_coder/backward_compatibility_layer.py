"""
Backward Compatibility Layer for GraphRAG MCP Integration.

This module provides backward compatibility for the GraphRAG MCP tools by:
1. Supporting optional session_id parameters in all MCP tool functions
2. Providing deprecation warnings for legacy usage
3. Managing compatibility mode configuration
"""

import hashlib
import os
import warnings
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = __import__("logging").getLogger(__name__)


class CompatibilityMode(Enum):
    """Compatibility mode configuration."""

    STRICT = "strict"  # Only new API with session_id
    COMPATIBLE = "compatible"  # Both old and new API (default)
    LEGACY = "legacy"  # Only old API, no isolation


@dataclass
class CompatibilityConfig:
    """Configuration for backward compatibility."""

    mode: CompatibilityMode = CompatibilityMode.COMPATIBLE
    warn_on_legacy: bool = True
    warn_on_missing_session_id: bool = False
    default_session_id: Optional[str] = None


class BackwardCompatibilityLayer:
    """
    Manages backward compatibility for GraphRAG MCP tools.

    This class provides utilities to:
    - Extract and validate session_id parameters
    - Generate repository hashes for isolation
    - Emit deprecation warnings
    - Manage compatibility modes
    """

    def __init__(self, config: Optional[CompatibilityConfig] = None):
        """Initialize the compatibility layer.

        Args:
            config: Compatibility configuration. If None, uses defaults.
        """
        self.config = config or CompatibilityConfig()
        self._deprecated_warnings_shown: set[str] = set()

    def extract_session_id(
        self,
        session_id: Optional[str] = None,
        repo_path: Optional[str] = None,
        warn: bool = True,
    ) -> tuple[str, bool]:
        """
        Extract and validate session_id parameter.

        Args:
            session_id: Optional session ID provided by caller
            repo_path: Optional repository path
            warn: Whether to emit warnings

        Returns:
            Tuple of (session_id, is_legacy) where is_legacy indicates
            if the session_id was auto-generated (legacy mode)
        """
        is_legacy = False

        # If session_id explicitly provided, use it
        if session_id:
            return session_id, False

        # If in legacy mode, allow missing session_id
        if self.config.mode == CompatibilityMode.LEGACY:
            return "default", True

        # If no session_id provided, auto-generate from repo_path
        if repo_path:
            session_id = self.generate_session_id(repo_path)
            is_legacy = True

            if warn and self.config.warn_on_missing_session_id:
                self._warn(
                    f"No session_id provided. Auto-generated session_id: {session_id}. "
                    f"Consider explicitly passing session_id for better isolation.",
                    category=UserWarning,
                )
        else:
            session_id = "default"

        return session_id, is_legacy

    def generate_session_id(self, repo_path: str) -> str:
        """
        Generate a consistent session_id from repository path.

        Args:
            repo_path: Repository path

        Returns:
            Session ID in format 'repo_<hash>' where hash is MD5 of path
        """
        repo_path_str = str(Path(repo_path).resolve())
        repo_hash = hashlib.md5(repo_path_str.encode()).hexdigest()[:8]
        return f"repo_{repo_hash}"

    def validate_session_id(self, session_id: str) -> bool:
        """
        Validate session_id format.

        Args:
            session_id: Session ID to validate

        Returns:
            True if valid, False otherwise
        """
        # Session IDs should be alphanumeric with underscores and hyphens
        return all(c.isalnum() or c in "_-" for c in session_id)

    def _warn(
        self, message: str, category: type = DeprecationWarning, stacklevel: int = 2
    ) -> None:
        """
        Emit a deprecation warning.

        Args:
            message: Warning message
            category: Warning category
            stacklevel: Stack level for warning location
        """
        # Avoid duplicate warnings
        if message not in self._deprecated_warnings_shown:
            warnings.warn(message, category=category, stacklevel=stacklevel)
            self._deprecated_warnings_shown.add(message)

        # Also log the warning
        logger.warning(message)

    def should_emit_deprecation_warning(self, context: str) -> bool:
        """
        Check if deprecation warning should be emitted for this context.

        Args:
            context: Context identifier for the warning

        Returns:
            True if warning should be emitted
        """
        if not self.config.warn_on_legacy:
            return False

        # Only warn once per context
        return context not in self._deprecated_warnings_shown

    def mark_warning_shown(self, context: str) -> None:
        """
        Mark that a warning has been shown for a context.

        Args:
            context: Context identifier
        """
        self._deprecated_warnings_shown.add(context)

    def get_repo_label(self, session_id: str) -> str:
        """
        Get repository-specific label for Neo4j queries.

        Args:
            session_id: Session ID

        Returns:
            Label in format 'Session_{HASH}' where HASH is from session_id
        """
        # Generate hash from session_id (not repo path)
        session_hash = hashlib.md5(session_id.encode()).hexdigest()[:8]
        return f"Session_{session_hash}"

    def get_config(self) -> CompatibilityConfig:
        """
        Get current compatibility configuration.

        Returns:
            Current configuration
        """
        return self.config

    def set_mode(self, mode: CompatibilityMode) -> None:
        """
        Set compatibility mode.

        Args:
            mode: New compatibility mode
        """
        self.config.mode = mode
        logger.info(f"Compatibility mode set to: {mode.value}")

    def set_warn_on_legacy(self, warn: bool) -> None:
        """
        Enable or disable legacy warnings.

        Args:
            warn: Whether to warn on legacy usage
        """
        self.config.warn_on_legacy = warn
        logger.info(f"Legacy warnings {'enabled' if warn else 'disabled'}")

    @staticmethod
    def from_environment() -> "BackwardCompatibilityLayer":
        """
        Create compatibility layer from environment variables.

        Reads:
        - GRAPHRAG_COMPATIBILITY_MODE: Compatibility mode
        - GRAPHRAG_WARN_ON_LEGACY: Whether to warn on legacy usage
        - GRAPHRAG_DEFAULT_SESSION_ID: Default session ID

        Returns:
            Configured compatibility layer
        """
        config = CompatibilityConfig()

        # Read compatibility mode
        mode_str = os.environ.get("GRAPHRAG_COMPATIBILITY_MODE", "").lower()
        if mode_str in ["strict", "compatible", "legacy"]:
            config.mode = CompatibilityMode(mode_str)

        # Read warning settings
        warn_str = os.environ.get("GRAPHRAG_WARN_ON_LEGACY", "true").lower()
        config.warn_on_legacy = warn_str in ["true", "1", "yes"]

        warn_missing_str = os.environ.get(
            "GRAPHRAG_WARN_ON_MISSING_SESSION_ID", "false"
        ).lower()
        config.warn_on_missing_session_id = warn_missing_str in ["true", "1", "yes"]

        # Read default session ID
        default_session_id = os.environ.get("GRAPHRAG_DEFAULT_SESSION_ID")
        if default_session_id:
            config.default_session_id = default_session_id

        return BackwardCompatibilityLayer(config)


# Global compatibility layer instance
_default_compat_layer = None


def get_compatibility_layer() -> BackwardCompatibilityLayer:
    """
    Get the global backward compatibility layer instance.

    Returns:
        Global compatibility layer
    """
    global _default_compat_layer
    if _default_compat_layer is None:
        _default_compat_layer = BackwardCompatibilityLayer.from_environment()
    return _default_compat_layer


def set_compatibility_layer(layer: BackwardCompatibilityLayer) -> None:
    """
    Set the global backward compatibility layer instance.

    Args:
        layer: Compatibility layer to set as global instance
    """
    global _default_compat_layer
    _default_compat_layer = layer
