"""
Jules HTTP API client for Auto-Coder.

Jules is a session-based AI assistant that can be used for issue processing.
This client uses HTTP API instead of Jules CLI to communicate with Jules.
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import requests  # type: ignore
from requests.adapters import HTTPAdapter  # type: ignore
from urllib3.util.retry import Retry

from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger

logger = get_logger(__name__)


class JulesClient(LLMClientBase):
    """Jules HTTP API client that manages session-based AI interactions."""

    def __init__(self, backend_name: Optional[str] = None) -> None:
        """Initialize Jules HTTP API client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
        """
        self.backend_name = backend_name or "jules"
        self.timeout = None  # No timeout - let HTTP requests run as needed
        self.active_sessions: Dict[str, str] = {}  # Track active sessions
        self.api_key: Optional[str] = None
        self.base_url = "https://jules.googleapis.com/v1alpha"

        # Load configuration for this backend
        config = get_llm_config()
        config_backend = config.get_backend_config(self.backend_name)
        self.options = (config_backend and config_backend.options) or []
        self.options_for_noedit = (config_backend and config_backend.options_for_noedit) or []
        self.api_key = (config_backend and config_backend.api_key) or None

        # Check API connectivity
        if not self.api_key:
            logger.warning("No API key configured for Jules. API calls may fail.")

        # Create HTTP session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set headers
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "auto-coder/1.0"})
        if self.api_key:
            # Use X-Goog-Api-Key header instead of Authorization: Bearer
            # This is required for Jules API when using API keys
            self.session.headers["X-Goog-Api-Key"] = self.api_key

    def start_session(self, prompt: str, repo_name: str, base_branch: str, is_noedit: bool = False, title: Optional[str] = None) -> str:
        """Start a new Jules session with the given prompt.

        Args:
            prompt: The prompt to send to Jules
            repo_name: Repository name (e.g., 'owner/repo')
            base_branch: Base branch name (e.g., 'main')
            is_noedit: Whether this is a no-edit operation (uses options_for_noedit)
            title: Optional title for the session

        Returns:
            Session ID for the started session
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions"
            payload = {"prompt": prompt, "automationMode": "AUTO_CREATE_PR", "sourceContext": {"source": f"sources/github/{repo_name}", "githubRepoContext": {"startingBranch": base_branch}}}

            if title:
                payload["title"] = title

            logger.info("Starting Jules session")
            logger.info(f"🤖 POST {url}")

            response = self.session.post(url, json=payload, timeout=self.timeout)

            # Check if request was successful
            if response.status_code not in [200, 201]:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to start Jules session: {error_msg}")
                raise RuntimeError(f"Failed to start Jules session: {error_msg}")

            # Parse the response to get the session ID
            try:
                response_data = response.json()
                # Extract the session ID from the response
                # The exact field name depends on the API response format
                session_id = response_data.get("sessionId") or response_data.get("session_id") or response_data.get("id")
                if not session_id:
                    # Fallback: generate a session ID based on timestamp
                    session_id = f"session_{int(time.time())}"
                    logger.warning(f"Could not extract session ID from response, using generated ID: {session_id}")
            except json.JSONDecodeError:
                # Fallback: generate a session ID
                session_id = f"session_{int(time.time())}"
                logger.warning(f"Could not parse response JSON, using generated ID: {session_id}")

            # Track the session
            self.active_sessions[session_id] = prompt

            logger.info(f"Started Jules session: {session_id}")
            return session_id

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to start Jules session: {e}")
            raise RuntimeError(f"Failed to start Jules session: {e}")
        except Exception as e:
            logger.error(f"Failed to start Jules session: {e}")
            raise RuntimeError(f"Failed to start Jules session: {e}")

    def list_sessions(self, page_size: int = 20) -> List[Dict[str, Any]]:
        """List recent Jules sessions.

        Args:
            page_size: Number of sessions to return per page (default: 20)

        Returns:
            List of session dictionaries
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions"

            # Note: The API does not support server-side filtering for 'state'.
            # We must fetch sessions and filter them client-side.
            base_params = {
                "pageSize": page_size,
            }

            logger.info(f"Listing Jules sessions (pageSize={page_size})")

            all_sessions = []
            page_token = None

            while True:
                params = base_params.copy()
                if page_token:
                    params["pageToken"] = page_token

                logger.info(f"🤖 GET {url} (pageToken={page_token if page_token else 'None'})")

                response = self.session.get(url, params=params, timeout=self.timeout)

                # Check if request was successful
                if response.status_code != 200:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    logger.error(f"Failed to list Jules sessions: {error_msg}")
                    raise RuntimeError(f"Failed to list Jules sessions: {error_msg}")

                # Parse the response
                try:
                    response_data = response.json()
                    sessions = response_data.get("sessions", [])
                    all_sessions.extend(sessions)

                    # Check for next page
                    page_token = response_data.get("nextPageToken")
                    if not page_token:
                        break

                except json.JSONDecodeError:
                    logger.error("Failed to parse Jules sessions response as JSON")
                    break

            # Filter out archived sessions client-side
            active_sessions = [s for s in all_sessions if s.get("state") != "ARCHIVED"]

            logger.info(f"Total sessions retrieved: {len(all_sessions)}, Active: {len(active_sessions)}")
            logger.info("=" * 60)
            return active_sessions

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to list Jules sessions: {e}")
            raise RuntimeError(f"Failed to list Jules sessions: {e}")
        except Exception as e:
            logger.error(f"Failed to list Jules sessions: {e}")
            raise RuntimeError(f"Failed to list Jules sessions: {e}")

    def send_message(self, session_id: str, message: str) -> str:
        """Send a message to an existing Jules session.

        Args:
            session_id: The session ID to send the message to
            message: The message to send

        Returns:
            Response from Jules
        """
        # Escape < / > to &lt; / &gt;
        message = message.replace("<", "&lt;").replace(">", "&gt;")

        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}:sendMessage"
            payload = {
                "prompt": message,
            }

            logger.info(f"Sending message to Jules session: {session_id}")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json=payload, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to send message to Jules session {session_id}: {error_msg}")
                raise RuntimeError(f"Failed to send message to Jules session {session_id}: {error_msg}")

            # Parse the response
            try:
                response_data = response.json()
                # Extract the response message
                # The exact field name depends on the API response format
                response_text = response_data.get("response") or response_data.get("message") or response_data.get("result") or str(response_data)
                return response_text
            except json.JSONDecodeError:
                # If response is not JSON, return the raw text
                return response.text

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send message to Jules session {session_id}: {e}")
            raise RuntimeError(f"Failed to send message to Jules session {session_id}: {e}")
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
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}"

            logger.info(f"Ending Jules session: {session_id}")
            logger.info(f"🤖 DELETE {url}")
            logger.info("=" * 60)

            response = self.session.delete(url, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code == 200:
                # Remove from active sessions
                if session_id in self.active_sessions:
                    del self.active_sessions[session_id]

                logger.info(f"Ended Jules session: {session_id}")
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to end Jules session {session_id}: {error_msg}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to end Jules session {session_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to end Jules session {session_id}: {e}")
            return False

    def archive_session(self, session_id: str) -> bool:
        """Archive a Jules session.

        Args:
            session_id: The session ID to archive

        Returns:
            True if session was archived successfully, False otherwise
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}:archive"

            logger.info(f"Archiving Jules session: {session_id}")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json={}, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code == 200:
                # Remove from active sessions
                if session_id in self.active_sessions:
                    del self.active_sessions[session_id]

                logger.info(f"Archived Jules session: {session_id}")
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to archive Jules session {session_id}: {error_msg}")
                return False

            return False
        except Exception as e:
            logger.error(f"Failed to archive Jules session {session_id}: {e}")
            return False

    def approve_plan(self, session_id: str) -> bool:
        """Approve a plan for a Jules session.

        Args:
            session_id: The session ID to approve the plan for

        Returns:
            True if plan was approved successfully, False otherwise
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}:approvePlan"

            logger.info(f"Approving plan for Jules session: {session_id}")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json={}, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code == 200:
                logger.info(f"Approved plan for Jules session: {session_id}")
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to approve plan for Jules session {session_id}: {error_msg}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to approve plan for Jules session {session_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to approve plan for Jules session {session_id}: {e}")
            return False

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run Jules HTTP API with the given prompt and return response.

        This method is kept for compatibility with LLMClientBase interface,
        but Jules uses sessions rather than single-run commands.

        Args:
            prompt: The prompt to send
            is_noedit: Whether this is a no-edit operation (uses options_for_noedit)

        Returns:
            Response from Jules
        """
        # Start a new session for this prompt
        # Note: This fallback method doesn't have access to repo context, so it might fail
        # if the API strictly requires it. We'll use placeholders or try without it.
        # For now, we'll try to extract repo info from prompt or use defaults if possible,
        # but since this is a fallback, we might need to update the interface or accept failure.
        # Ideally, _run_llm_cli shouldn't be used for Jules in this context.
        logger.warning("_run_llm_cli called for JulesClient. This may fail due to missing repo context.")
        session_id = self.start_session(prompt, "unknown/repo", "main", is_noedit)

        try:
            # Send the prompt and get response
            response = self.send_message(session_id, prompt)
            return response
        finally:
            # End the session
            self.end_session(session_id)

    def close(self) -> None:
        """Close the HTTP session and clean up resources."""
        if self.session:
            self.session.close()

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
