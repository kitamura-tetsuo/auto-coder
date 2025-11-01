"""
GraphRAG MCP Integration for Auto-Coder.

Integrates graphrag_mcp server with LLM clients to provide
Neo4j and Qdrant context during LLM invocations.
"""

import json
import os
import shlex
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from .graphrag_docker_manager import GraphRAGDockerManager
from .graphrag_index_manager import GraphRAGIndexManager
from .logger_config import get_logger

try:
    from .mcp_servers.graphrag_mcp.graphrag_mcp.session import GraphRAGMCPSession
except ImportError:
    # Fallback if session module is not available
    GraphRAGMCPSession = None

logger = get_logger(__name__)


class GraphRAGMCPIntegration:
    """Integrates graphrag_mcp with LLM clients."""

    def __init__(
        self,
        docker_manager: Optional[GraphRAGDockerManager] = None,
        index_manager: Optional[GraphRAGIndexManager] = None,
        mcp_server_path: Optional[str] = None,
    ):
        """Initialize GraphRAG MCP Integration.

        Args:
            docker_manager: Docker manager instance. If None, creates new instance.
            index_manager: Index manager instance. If None, creates new instance.
            mcp_server_path: Path to graphrag_mcp server. If None, uses environment variable.
        """
        self.docker_manager = docker_manager or GraphRAGDockerManager()
        self.index_manager = index_manager or GraphRAGIndexManager()

        # Get MCP server path from environment or use default
        if mcp_server_path is None:
            mcp_server_path = os.environ.get("GRAPHRAG_MCP_SERVER_PATH")

        self.mcp_server_path = mcp_server_path
        self.mcp_process: Optional[subprocess.Popen] = None

        # Session management
        if GraphRAGMCPSession is not None:
            self.active_sessions: Dict[str, GraphRAGMCPSession] = {}
            self.session_lock = threading.RLock()
        else:
            self.active_sessions = {}
            self.session_lock = threading.RLock()

    def ensure_ready(self, max_retries: int = 2, force_reindex: bool = False) -> bool:
        """Ensure GraphRAG environment is ready for use.

        This includes:
        1. Starting Docker containers if not running
        2. Updating index if needed (or forcing update if force_reindex=True)
        3. Starting MCP server if configured

        Args:
            max_retries: Maximum number of retries for Docker container startup
            force_reindex: Force reindexing even if index is up to date

        Returns:
            True if environment is ready, False otherwise
        """
        logger.info("Ensuring GraphRAG environment is ready...")

        # 1. Ensure Docker containers are running with retry
        if not self.docker_manager.is_running():
            logger.info("Docker containers not running, starting them...")
            for attempt in range(max_retries):
                try:
                    if self.docker_manager.start(wait_for_health=True):
                        logger.info("Docker containers started successfully")
                        break
                    else:
                        logger.warning(
                            f"Failed to start Docker containers (attempt {attempt + 1}/{max_retries})"
                        )
                        if attempt < max_retries - 1:
                            logger.info("Retrying after cleanup...")
                            # Try to stop containers before retry
                            self.docker_manager.stop()
                            import time

                            time.sleep(2)
                except Exception as e:
                    logger.error(
                        f"Error starting Docker containers (attempt {attempt + 1}/{max_retries}): {e}"
                    )
                    if attempt < max_retries - 1:
                        logger.info("Retrying after cleanup...")
                        try:
                            self.docker_manager.stop()
                        except Exception:
                            pass
                        import time

                        time.sleep(2)
            else:
                logger.error(
                    "Failed to start Docker containers after all retries. "
                    "Please check Docker installation and docker-compose.graphrag.yml configuration."
                )
                return False
        else:
            logger.info("Docker containers are already running")
            # Ensure current container is connected to GraphRAG network
            try:
                self.docker_manager._connect_to_graphrag_network()
            except Exception as e:
                logger.warning(f"Failed to connect to GraphRAG network: {e}")

        # 2. Check if indexed path matches current path or force reindex
        try:
            path_matches, indexed_path = self.index_manager.check_indexed_path()
            if force_reindex:
                logger.info("Force reindex requested, updating index...")
                if not self.index_manager.update_index(force=True):
                    logger.error("Failed to force update index.")
                    return False
            elif indexed_path is not None and not path_matches:
                logger.warning(
                    f"Indexed path mismatch: indexed={indexed_path}, "
                    f"current={self.index_manager.repo_path.resolve()}"
                )
                logger.info("Updating index for current directory...")
                # Force update when path changes
                if not self.index_manager.update_index(force=True):
                    logger.error("Failed to update index for current directory.")
                    return False
            else:
                # 3. Ensure index is up to date (strict error handling)
                if not self.index_manager.ensure_index_up_to_date():
                    logger.error("Failed to ensure index is up to date.")
                    return False
        except Exception as e:
            logger.error(f"Error updating index: {e}")
            return False

        # 4. Start MCP server if configured (strict error handling)
        if self.mcp_server_path and not self.is_mcp_server_running():
            logger.info("Starting GraphRAG MCP server...")
            try:
                if not self.start_mcp_server():
                    logger.error(
                        "Failed to start MCP server. "
                        "Check GRAPHRAG_MCP_SERVER_PATH environment variable."
                    )
                    return False
            except Exception as e:
                logger.error(f"Error starting MCP server: {e}")
                return False

        logger.info("GraphRAG environment is ready")
        return True

    def start_mcp_server(self) -> bool:
        """Start graphrag_mcp server.

        Returns:
            True if server started successfully, False otherwise
        """
        if not self.mcp_server_path:
            logger.warning("MCP server path not configured")
            return False

        try:
            # Start MCP server as subprocess
            cmd = shlex.split(self.mcp_server_path)
            self.mcp_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            logger.info(f"Started MCP server with PID {self.mcp_process.pid}")

            # Start stderr pump for diagnostics
            if self.mcp_process.stderr:
                threading.Thread(
                    target=self._pump_stderr,
                    args=(self.mcp_process.stderr,),
                    daemon=True,
                ).start()

            return True
        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            return False

    def _pump_stderr(self, stderr) -> None:
        """Pump stderr from MCP server to logger.

        Args:
            stderr: Stderr stream from MCP server
        """
        try:
            for line in iter(stderr.readline, b""):
                if line:
                    logger.debug(f"MCP server stderr: {line.decode().strip()}")
        except Exception as e:
            logger.debug(f"Error pumping MCP server stderr: {e}")

    def is_mcp_server_running(self) -> bool:
        """Check if MCP server is running.

        Returns:
            True if server is running, False otherwise
        """
        # First check if we have a process we started
        if self.mcp_process is not None:
            if self.mcp_process.poll() is None:
                return True

        # Check if any MCP server process is running (started by another terminal)
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5
            )
            # Look for graphrag_mcp main.py process
            for line in result.stdout.splitlines():
                if "graphrag_mcp" in line and "main.py" in line and "grep" not in line:
                    return True
        except Exception as e:
            logger.debug(f"Error checking for MCP server process: {e}")

        return False

    def stop_mcp_server(self) -> None:
        """Stop graphrag_mcp server."""
        if self.mcp_process is not None:
            logger.info("Stopping MCP server...")
            try:
                self.mcp_process.terminate()
                self.mcp_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("MCP server did not terminate, killing it")
                self.mcp_process.kill()
            except Exception as e:
                logger.error(f"Error stopping MCP server: {e}")
            finally:
                self.mcp_process = None

    def create_session(self, repo_path: str) -> str:
        """Create new session for repository.

        Args:
            repo_path: Path to repository

        Returns:
            session_id: Unique session identifier

        Raises:
            RuntimeError: If GraphRAGMCPSession is not available
        """
        if GraphRAGMCPSession is None:
            raise RuntimeError("Session management not available. GraphRAGMCPSession class not found.")

        repo_path = str(Path(repo_path).resolve())

        with self.session_lock:
            session_id = str(uuid.uuid4())[:8]
            session = GraphRAGMCPSession(session_id, repo_path)
            self.active_sessions[session_id] = session
            logger.info(f"Created session {session_id} for {repo_path}")
            return session_id

    def get_session(self, session_id: str) -> Optional[GraphRAGMCPSession]:
        """Get session by ID with access tracking.

        Args:
            session_id: Session identifier

        Returns:
            Session object or None if not found
        """
        with self.session_lock:
            session = self.active_sessions.get(session_id)
            if session:
                session.update_access()
            return session

    def cleanup_expired_sessions(self, max_age_hours: int = 24) -> int:
        """Clean up expired sessions to prevent memory leaks.

        Args:
            max_age_hours: Maximum age of sessions in hours

        Returns:
            Number of sessions cleaned up
        """
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)

        with self.session_lock:
            expired_sessions = [
                sid for sid, session in self.active_sessions.items()
                if session.last_accessed < cutoff_time
            ]

            for sid in expired_sessions:
                del self.active_sessions[sid]
                logger.info(f"Cleaned up expired session {sid}")

            return len(expired_sessions)

    def list_sessions(self) -> list:
        """List all active sessions.

        Returns:
            List of session information dictionaries
        """
        with self.session_lock:
            return [session.to_dict() for session in self.active_sessions.values()]

    def cleanup(self) -> None:
        """Cleanup resources."""
        self.stop_mcp_server()
        # Note: We don't clean up sessions here as they might be used by other instances
        # Session cleanup is handled by cleanup_expired_sessions

    def get_mcp_config_for_llm(self) -> Optional[dict]:
        """Get MCP configuration to pass to LLM client.

        Returns:
            Dictionary with MCP configuration, or None if not available
        """
        if not self.is_mcp_server_running():
            return None

        # MCP server provides tool definitions dynamically
        # LLM client will discover tools via MCP protocol
        return {
            "mcp_server": "graphrag",
            "mcp_resources": [
                "https://graphrag.db/schema/neo4j",
                "https://graphrag.db/collection/qdrant",
            ],
        }

