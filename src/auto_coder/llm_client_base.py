"""
Base class for LLM clients.
"""

from abc import ABC, abstractmethod
from typing import Optional


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

    def close(self) -> None:
        """Close the backend manager and clean up resources.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass
