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
from typing import Optional

from .graphrag_docker_manager import GraphRAGDockerManager
from .graphrag_index_manager import GraphRAGIndexManager
from .logger_config import get_logger

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

    def ensure_ready(self, max_retries: int = 2) -> bool:
        """Ensure GraphRAG environment is ready for use.

        This includes:
        1. Starting Docker containers if not running
        2. Updating index if needed
        3. Starting MCP server if configured

        Args:
            max_retries: Maximum number of retries for Docker container startup

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

        # 2. Check if indexed path matches current path
        try:
            path_matches, indexed_path = self.index_manager.check_indexed_path()
            if indexed_path is not None and not path_matches:
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

    def cleanup(self) -> None:
        """Cleanup resources."""
        self.stop_mcp_server()

    def get_mcp_config_for_llm(self) -> Optional[dict]:
        """Get MCP configuration to pass to LLM client.

        Returns:
            Dictionary with MCP configuration, or None if not available
        """
        if not self.is_mcp_server_running():
            return None

        return {
            "mcp_server": "graphrag",
            "mcp_tools": ["search_documentation", "hybrid_search"],
            "mcp_resources": [
                "https://graphrag.db/schema/neo4j",
                "https://graphrag.db/collection/qdrant",
            ],
        }

