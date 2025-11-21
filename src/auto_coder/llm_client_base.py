"""
Base classes for LLM clients and provider-aware helpers.
"""

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from .backend_provider_manager import BackendProviderManager, ProviderChoice, ProviderOutcome


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
    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration for this LLM client.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        pass

    def ensure_mcp_server_configured(self, server_name: str, command: str, args: list[str]) -> bool:
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
    def run_test_fix_prompt(self, prompt: str, current_test_file: Optional[str] = None) -> str:
        """Execute LLM for test fix with optional test file tracking.

        Args:
            prompt: The prompt to send to the LLM
            current_test_file: Optional test file being fixed

        Returns:
            The LLM's response as a string
        """
        pass


class ProviderAwareLLMClient(LLMClientBase):
    """Base implementation for clients that depend on provider metadata."""

    def __init__(self, backend_name: str, provider_manager: Optional[BackendProviderManager] = None) -> None:
        self._backend_name = backend_name
        self._provider_manager: BackendProviderManager = provider_manager or BackendProviderManager.get_default_manager()

    @property
    def backend_name(self) -> str:
        """Return the backend name tied to this client."""
        return self._backend_name

    @property
    def provider_manager(self) -> BackendProviderManager:
        """Expose the shared provider manager instance."""
        return self._provider_manager

    def iter_provider_choices(self) -> Sequence[ProviderChoice]:
        """Retrieve ordered provider choices for this backend."""
        return self._provider_manager.iterate_provider_choices(self._backend_name)

    def report_provider_result(self, choice: ProviderChoice, outcome: ProviderOutcome) -> None:
        """Report provider invocation results back to the manager."""
        self._provider_manager.report_provider_result(self._backend_name, choice, outcome)

    def close(self) -> None:
        """Close the backend manager and clean up resources.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass
