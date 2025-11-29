"""
Jules client for Auto-Coder.

This module provides JulesClient, a client for interacting with Jules,
which adds the 'jules' label to issues in Jules mode.
"""

from typing import Optional

from .llm_client_base import LLMClientBase
from .logger_config import get_logger

logger = get_logger(__name__)


class JulesClient(LLMClientBase):
    """Jules client for handling Jules mode operations.

    Jules adds the 'jules' label to issues and handles them with specific
    processing logic.
    """

    def __init__(self, backend_name: Optional[str] = None) -> None:
        """Initialize Jules client.

        Args:
            backend_name: Backend name for configuration lookup (optional).
        """
        self.backend_name = backend_name

    def _run_llm_cli(self, prompt: str) -> str:
        """Execute LLM with the given prompt.

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            The LLM's response as a string

        Raises:
            NotImplementedError: This method is not implemented for JulesClient
        """
        # Jules client doesn't run LLM directly
        # It adds labels and delegates to other backends
        raise NotImplementedError("JulesClient does not run LLM directly")

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for this LLM client.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        # Jules client doesn't use MCP servers
        # This is a placeholder implementation
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration for this LLM client.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        # Jules client doesn't use MCP servers
        # This is a placeholder implementation
        return False
