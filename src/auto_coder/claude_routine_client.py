"""
Claude Routine HTTP API client for Auto-Coder.

Claude Routine is an asynchronous cloud coding agent that executes tasks on
Anthropic-managed cloud infrastructure via routine trigger endpoints.
Reference: https://code.claude.com/docs/en/routines
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import requests  # type: ignore
from requests.adapters import HTTPAdapter  # type: ignore
from urllib3.util.retry import Retry

from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger

logger = get_logger(__name__)


class ClaudeRoutineClient(LLMClientBase):
    """Claude Routine HTTP API client for asynchronous cloud routine execution."""

    def __init__(self, backend_name: Optional[str] = None) -> None:
        """Initialize Claude Routine HTTP API client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
        """
        self.backend_name = backend_name or "claude-routine"
        self.timeout = 30
        self.active_sessions: Dict[str, str] = {}  # session_id -> prompt
        self.token: Optional[str] = None
        self.url: Optional[str] = None

        # Load configuration for this backend
        config = get_llm_config()
        config_backend = config.get_backend_config(self.backend_name)

        self.options = (config_backend and config_backend.options) or []
        self.options_for_noedit = (config_backend and config_backend.options_for_noedit) or []
        self.options_for_resume = (config_backend and config_backend.options_for_resume) or []
        self.token = (config_backend and (config_backend.claude_code_routine_token or config_backend.claude_code_oauth_token or config_backend.api_key)) or None
        self.url = (config_backend and (config_backend.url or config_backend.base_url)) or None

        if not self.token:
            logger.warning("No token configured for Claude Routine. API calls may fail with 401 Unauthorized.")
        if not self.url:
            logger.warning("No trigger URL configured for Claude Routine. API calls may fail.")

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

        # Set default headers
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "auto-coder/1.0",
                "anthropic-beta": "experimental-cc-routine-2026-04-01",
                "anthropic-version": "2023-06-01",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def fire_routine(
        self,
        prompt: str,
        repo_name: str = "",
        base_branch: str = "",
        title: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """Fire a Claude routine via HTTP POST to its trigger endpoint.

        Args:
            prompt: The task prompt to send in the fire text payload
            repo_name: Repository name (optional)
            base_branch: Base branch name (optional)
            title: Optional title/description for logging

        Returns:
            Tuple of (session_id, session_url)
        """
        if not self.url:
            raise ValueError(f"No URL configured for Claude Routine backend '{self.backend_name}'. " "Please configure 'url' in llm_config.toml.")

        payload: Dict[str, Any] = {"text": prompt}

        logger.info(f"Firing Claude Routine on {self.url} (title={title or 'N/A'})")
        logger.info(f"🤖 POST {self.url}")

        try:
            response = self.session.post(self.url, json=payload, timeout=self.timeout)

            if response.status_code not in [200, 201, 202]:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to fire Claude Routine: {error_msg}")
                raise RuntimeError(f"Failed to fire Claude Routine: {error_msg}")

            try:
                response_data = response.json()
            except json.JSONDecodeError:
                logger.warning(f"Could not parse JSON response from Claude Routine fire: {response.text}")
                response_data = {}

            session_id = response_data.get("claude_code_session_id") or response_data.get("sessionId") or response_data.get("session_id") or response_data.get("id")
            session_url = response_data.get("claude_code_session_url") or response_data.get("sessionUrl") or response_data.get("url")

            if not session_id:
                session_id = f"session_{int(time.time())}"
                logger.warning(f"Could not extract session ID from response, using generated ID: {session_id}")

            if not session_url and session_id:
                session_url = f"https://claude.ai/code/{session_id}"

            self.active_sessions[session_id] = prompt
            logger.info(f"Successfully fired Claude Routine: session_id={session_id}, session_url={session_url}")
            return session_id, session_url

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fire Claude Routine: {e}")
            raise RuntimeError(f"Failed to fire Claude Routine: {e}")

    def start_session(
        self,
        prompt: str,
        repo_name: str,
        base_branch: str,
        is_noedit: bool = False,
        title: Optional[str] = None,
    ) -> str:
        """Start a new session by firing the Claude Routine.

        Args:
            prompt: The prompt to send
            repo_name: Repository name
            base_branch: Base branch name
            is_noedit: Whether this is a noedit run
            title: Optional session title

        Returns:
            Session ID of the routine session
        """
        session_id, _ = self.fire_routine(prompt, repo_name=repo_name, base_branch=base_branch, title=title)
        return session_id

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run Claude Routine with given prompt.

        Implements LLMClientBase interface.
        """
        session_id, session_url = self.fire_routine(prompt)
        return f"Claude Routine fired successfully. Session ID: {session_id}, URL: {session_url}"

    def close(self) -> None:
        """Close HTTP session."""
        if self.session:
            self.session.close()

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if MCP server is configured."""
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration."""
        return False
