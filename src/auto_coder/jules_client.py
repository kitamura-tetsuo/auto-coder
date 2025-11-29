"""
Jules CLI client for Auto-Coder.

Jules is a session-based AI assistant that can be used for issue processing.
"""

import subprocess
import time
from typing import Any, Dict, List, Optional

from .llm_client_base import LLMClientBase
from .logger_config import get_logger
from .utils import CommandExecutor

logger = get_logger(__name__)


class JulesClient(LLMClientBase):
    """Jules client that manages session-based AI interactions."""

    def __init__(self, backend_name: Optional[str] = None) -> None:
        """Initialize Jules client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
        """
        self.backend_name = backend_name or "jules"
        self.timeout = None  # No timeout - let Jules CLI run as needed
        self.active_sessions: Dict[str, str] = {}  # Track active sessions

        # Check if Jules CLI is available
        try:
            result = subprocess.run(["jules", "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                raise RuntimeError("Jules CLI not available or not working")
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            FileNotFoundError,
        ) as e:
            raise RuntimeError(f"Jules CLI not available: {e}")

    def start_session(self, prompt: str) -> str:
        """Start a new Jules session with the given prompt.

        Args:
            prompt: The prompt to send to Jules

        Returns:
            Session ID for the started session
        """
        try:
            cmd = ["jules", "session", "start"]

            logger.info("Starting Jules session")
            logger.info(f"🤖 Running: jules session start [prompt]")
            logger.info("=" * 60)

            result = CommandExecutor.run_command(
                cmd,
                input_text=prompt,
                stream_output=True,
            )

            logger.info("=" * 60)

            # Parse session ID from output
            # Expected format: "Session started: <session_id>"
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            output = stdout or stderr

            # Try to extract session ID from the output
            session_id = self._extract_session_id(output)

            if not session_id:
                # Fallback: generate a session ID based on timestamp
                session_id = f"session_{int(time.time())}"
                logger.warning(f"Could not extract session ID from output, using generated ID: {session_id}")

            # Track the session
            self.active_sessions[session_id] = prompt

            logger.info(f"Started Jules session: {session_id}")
            return session_id

        except Exception as e:
            logger.error(f"Failed to start Jules session: {e}")
            raise RuntimeError(f"Failed to start Jules session: {e}")

    def _extract_session_id(self, output: str) -> Optional[str]:
        """Extract session ID from Jules CLI output.

        Args:
            output: The output from Jules CLI

        Returns:
            Session ID if found, None otherwise
        """
        # Handle None or empty output
        if not output:
            return None

        # Try to find session ID in various formats
        # Pattern 1: "Session started: <id>"
        if "session started:" in output.lower():
            parts = output.lower().split("session started:")
            if len(parts) > 1:
                session_id = parts[1].strip().split()[0].strip()
                return session_id

        # Pattern 2: "session_id: <id>"
        if "session_id:" in output.lower():
            parts = output.lower().split("session_id:")
            if len(parts) > 1:
                session_id = parts[1].strip().split()[0].strip()
                return session_id

        # Pattern 3: Extract any alphanumeric ID
        import re

        match = re.search(r"\b([a-zA-Z0-9_-]{8,})\b", output)
        if match:
            # Return the matched alphanumeric string
            return match.group(1)

        return None

    def send_message(self, session_id: str, message: str) -> str:
        """Send a message to an existing Jules session.

        Args:
            session_id: The session ID to send the message to
            message: The message to send

        Returns:
            Response from Jules
        """
        try:
            cmd = ["jules", "session", "send", "--session", session_id]

            logger.info(f"Sending message to Jules session: {session_id}")

            result = CommandExecutor.run_command(
                cmd,
                input_text=message,
                stream_output=True,
            )

            # Check if command succeeded
            if result.returncode != 0:
                error_msg = result.stderr or "Unknown error"
                logger.error(f"Failed to send message to Jules session {session_id}: {error_msg}")
                raise RuntimeError(f"Failed to send message to Jules session {session_id}: {error_msg}")

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            response = stdout or stderr

            return response

        except Exception as e:
            logger.error(f"Failed to send message to Jules session {session_id}: {e}")
            raise RuntimeError(f"Failed to send message to Jules session {session_id}: {e}")

    def end_session(self, session_id: str) -> bool:
        """End a Jules session.

        Args:
            session_id: The session ID to end

        Returns:
            True if session was ended successfully, False otherwise
        """
        try:
            cmd = ["jules", "session", "end", "--session", session_id]

            logger.info(f"Ending Jules session: {session_id}")

            result = CommandExecutor.run_command(cmd)

            if result.returncode == 0:
                # Remove from active sessions
                if session_id in self.active_sessions:
                    del self.active_sessions[session_id]

                logger.info(f"Ended Jules session: {session_id}")
                return True
            else:
                logger.error(f"Failed to end Jules session {session_id}: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to end Jules session {session_id}: {e}")
            return False

    def _run_llm_cli(self, prompt: str) -> str:
        """Run Jules CLI with the given prompt and return response.

        This method is kept for compatibility with LLMClientBase interface,
        but Jules uses sessions rather than single-run commands.

        Args:
            prompt: The prompt to send

        Returns:
            Response from Jules
        """
        # Start a new session for this prompt
        session_id = self.start_session(prompt)

        try:
            # Send the prompt and get response
            response = self.send_message(session_id, prompt)
            return response
        finally:
            # End the session
            self.end_session(session_id)

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Jules CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        # Jules doesn't support MCP servers like Claude/Gemini
        # Return False for all MCP server checks
        logger.debug(f"Jules does not support MCP servers. Check for '{server_name}' returned False")
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration for Jules CLI.

        Jules doesn't currently support MCP server configuration.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            False - Jules does not support MCP server configuration
        """
        # Jules doesn't support MCP servers
        logger.debug(f"Jules does not support MCP server configuration. Cannot add '{server_name}'")
        return False
