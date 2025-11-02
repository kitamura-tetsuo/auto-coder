"""
Base class for LLM clients.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import Lock
from typing import Any, Callable, Dict, List, Optional
import os
import inspect


class LLMClientBase(ABC):
    """Base class for all LLM clients.

    All LLM clients must implement the _run_llm_cli method and MCP configuration methods.
    """

    @abstractmethod
    def _run_llm_cli(self, prompt: str) -> str:
        """Execute LLM with the given prompt.

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            The LLM's response as a string
        """
        pass

    @abstractmethod
    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for this LLM client.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        pass

    @abstractmethod
    def add_mcp_server_config(
        self, server_name: str, command: str, args: list[str]
    ) -> bool:
        """Add MCP server configuration for this LLM client.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        pass

    def ensure_mcp_server_configured(
        self, server_name: str, command: str, args: list[str]
    ) -> bool:
        """Ensure a specific MCP server is configured, adding it if necessary.

        This is a convenience method that checks if the server is configured,
        and if not, adds the configuration.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if the MCP server is configured (or was successfully added), False otherwise
        """
        # Check if already configured
        if self.check_mcp_server_configured(server_name):
            return True

        # Try to add configuration
        if self.add_mcp_server_config(server_name, command, args):
            # Verify configuration was added
            return self.check_mcp_server_configured(server_name)

        return False

    def switch_to_default_model(self) -> None:
        """Switch to the default model.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass

    def close(self) -> None:
        """Close the client and clean up resources.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass


class LLMBackendManagerBase(LLMClientBase):
    """Base class for LLM backend managers.

    Backend managers must implement additional methods for managing backends.
    """

    @abstractmethod
    def run_test_fix_prompt(
        self, prompt: str, current_test_file: Optional[str] = None
    ) -> str:
        """Execute LLM for test fix with optional test file tracking.

        Args:
            prompt: The prompt to send to the LLM
            current_test_file: Optional test file being fixed

        Returns:
            The LLM's response as a string
        """
        pass

    def close(self) -> None:
        """Close the backend manager and clean up resources.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass


class LLMBackendManager:
    """
    Singleton manager for message-specific LLM backend.

    This manager is dedicated to commit messages and uses lightweight models
    to reduce costs while maintaining quality for message generation.

    Thread-safe singleton implementation using double-checked locking.
    """

    _instance: Optional[LLMBackendManager] = None
    _lock: Lock = Lock()

    def __new__(cls) -> LLMBackendManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self) -> None:
        """Initialize the singleton message backend manager.

        Only initializes once due to singleton pattern.
        Uses environment variables or defaults to lightweight models.
        """
        # Prevent re-initialization in singleton
        if hasattr(self, '_initialized'):
            return

        self._initialized = True
        self._backend_manager: Optional[Any] = None
        self._config_lock = Lock()

    @classmethod
    def get_llm_for_message_instance(cls) -> Any:
        """
        Get or create the singleton message backend manager instance.

        This is a class method that returns the singleton instance of the
        message backend manager, which is dedicated to generating commit messages
        and uses lightweight models for cost efficiency.

        The returned backend manager can be used directly to generate commit messages
        without needing to pass it around as a parameter.

        Returns:
            BackendManager instance configured for commit messages

        Raises:
            RuntimeError: If the backend manager is not initialized
        """
        instance = cls()
        with instance._config_lock:
            if instance._backend_manager is None:
                # Import here to avoid circular dependency
                from .backend_manager import BackendManager

                # Create message backend manager with lightweight model
                # This uses environment variables for configuration
                instance._backend_manager = cls._create_message_backend_manager()

        return instance._backend_manager

    @classmethod
    def _create_message_backend_manager(cls) -> Any:
        """
        Create a backend manager instance configured for commit messages.

        Uses lightweight models appropriate for commit message generation.
        Configuration is read from environment variables or uses sensible defaults.

        Returns:
            BackendManager instance configured for message generation
        """
        from .backend_manager import BackendManager

        # Determine the lightweight model to use for messages
        # Priority: explicit env var, then auto-detect from main config, then default
        message_model = os.environ.get("AUTO_CODER_MESSAGE_MODEL")
        message_backend = os.environ.get("AUTO_CODER_MESSAGE_BACKEND", "qwen")

        # Factory function to create message client
        def create_message_client() -> Any:
            """Create a lightweight LLM client for message generation."""
            # Import the appropriate client factory
            # This will be determined by the backend type
            try:
                if message_backend == "qwen":
                    from .llm_client_qwen import QwenClient
                    return QwenClient(
                        model=message_model or "qwen-turbo",
                        system_prompt="",
                        api_key_env="QWEN_API_KEY",
                    )
                elif message_backend == "gemini":
                    from .llm_client_gemini import GeminiClient
                    return GeminiClient(
                        model=message_model or "gemini-1.5-flash",
                        api_key_env="GEMINI_API_KEY",
                    )
                else:
                    # Default to qwen-turbo for lightweight operation
                    from .llm_client_qwen import QwenClient
                    return QwenClient(
                        model=message_model or "qwen-turbo",
                        system_prompt="",
                        api_key_env="QWEN_API_KEY",
                    )
            except Exception:
                # If specific backend fails, try to create a generic client
                # This is a fallback to ensure the system can still work
                return None

        # Create the backend manager with message-specific configuration
        try:
            message_client = create_message_client()
            if message_client is None:
                raise RuntimeError("Failed to create message client")

            # Create and return the backend manager
            return BackendManager(
                default_backend=message_backend,
                default_client=message_client,
                factories={message_backend: create_message_client},
                order=[message_backend],
            )
        except Exception as e:
            # Log error and raise with context
            raise RuntimeError(
                f"Failed to initialize message backend manager: {e}"
            ) from e

    def close(self) -> None:
        """Close the message backend manager and clean up resources."""
        if self._backend_manager is not None:
            try:
                self._backend_manager.close()
            except Exception:
                pass

        with self._config_lock:
            self._backend_manager = None

    def is_initialized(self) -> bool:
        """Check if the singleton instance is initialized."""
        return hasattr(self, '_initialized') and self._backend_manager is not None
