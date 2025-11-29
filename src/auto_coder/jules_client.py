"""
Jules client for Auto-Coder.

This module provides JulesClient, a client for interacting with Jules,
which adds the 'jules' label to issues in Jules mode.
"""

import os
from typing import Optional

import requests

from .jules_config import JulesConfig
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
        self.config = JulesConfig.load_from_file()
        self.base_url = os.environ.get("JULES_API_URL", "http://localhost:8000")

    def start_session(self, prompt: str) -> str:
        """Start a new Jules session.

        Args:
            prompt: The prompt to send to Jules

        Returns:
            The session ID as a string

        Raises:
            requests.RequestException: If the API request fails
        """
        url = f"{self.base_url}/sessions"
        payload = {"prompt": prompt, "AUTO_CREATE_PR": True}

        logger.debug(f"Starting Jules session at {url}")
        logger.debug(f"Sending payload: {payload}")

        try:
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()

            # Extract session ID from response
            data = response.json()
            session_id = data.get("session_id") or data.get("id")

            if not session_id:
                raise ValueError("No session_id in response")

            logger.info(f"Started Jules session with ID: {session_id}")
            return session_id

        except requests.RequestException as e:
            logger.error(f"Failed to start Jules session: {e}")
            raise
        except (ValueError, KeyError) as e:
            logger.error(f"Invalid response from Jules API: {e}")
            raise

    def send_message(self, session_id: str, message: str) -> str:
        """Send a message to an existing Jules session.

        Args:
            session_id: The session ID from start_session
            message: The message to send

        Returns:
            The response from Jules

        Raises:
            requests.RequestException: If the API request fails
        """
        url = f"{self.base_url}/sessions/{session_id}/messages"
        payload = {"message": message}

        logger.debug(f"Sending message to Jules session {session_id} at {url}")
        logger.debug(f"Sending payload: {payload}")

        try:
            response = requests.post(url, json=payload, timeout=30)
            response.raise_for_status()

            data = response.json()
            logger.info(f"Received response from Jules session {session_id}")

            return data

        except requests.RequestException as e:
            logger.error(f"Failed to send message to Jules session {session_id}: {e}")
            raise

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
