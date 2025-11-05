"""
Test Watcher MCP Integration for Auto-Coder.

Integrates test_watcher MCP server with LLM clients to provide
continuous test monitoring during LLM invocations.
"""

import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

from .logger_config import get_logger

logger = get_logger(__name__)


class TestWatcherIntegration:
    """Integrates test_watcher MCP server with LLM clients."""

    def __init__(
        self,
        mcp_server_path: Optional[str] = None,
        project_root: Optional[str] = None,
    ):
        """Initialize Test Watcher MCP Integration.

        Args:
            mcp_server_path: Path to test_watcher server. If None, uses environment variable or default.
            project_root: Project root directory to watch. If None, uses current directory.
        """
        # Get MCP server path from environment or use default
        if mcp_server_path is None:
            mcp_server_path = os.environ.get("TEST_WATCHER_MCP_SERVER_PATH")

        # If still None, try to find it in default location
        if mcp_server_path is None:
            default_path = Path.home() / "mcp_servers" / "test_watcher"
            if default_path.exists():
                mcp_server_path = str(default_path)

        self.mcp_server_path = mcp_server_path
        self.project_root = project_root or str(Path.cwd())
        self.mcp_process: Optional[subprocess.Popen] = None

    def ensure_ready(self) -> bool:
        """Ensure Test Watcher environment is ready for use.

        This includes:
        1. Checking if MCP server is installed
        2. Starting MCP server if configured

        Returns:
            True if environment is ready, False otherwise
        """
        logger.info("Ensuring Test Watcher environment is ready...")

        # 1. Check if MCP server is installed
        if not self.is_mcp_server_installed():
            logger.warning("Test Watcher MCP server not installed")
            logger.info("Run 'auto-coder mcp setup test-watcher' to install")
            return False

        # 2. Start MCP server if configured
        if self.mcp_server_path and not self.is_mcp_server_running():
            logger.info("Starting Test Watcher MCP server...")
            if not self.start_mcp_server():
                logger.error("Failed to start Test Watcher MCP server")
                return False

        logger.info("✅ Test Watcher environment is ready")
        return True

    def is_mcp_server_installed(self) -> bool:
        """Check if test_watcher MCP server is installed.

        Returns:
            True if server is installed, False otherwise
        """
        if not self.mcp_server_path:
            return False

        server_path = Path(self.mcp_server_path)
        if not server_path.exists():
            return False

        # Check for main.py or server.py
        if not (server_path / "main.py").exists() and not (server_path / "server.py").exists():
            return False

        return True

    def is_mcp_server_running(self) -> bool:
        """Check if test_watcher MCP server is running.

        Returns:
            True if server is running, False otherwise
        """
        if self.mcp_process is None:
            return False

        # Check if process is still alive
        if self.mcp_process.poll() is not None:
            self.mcp_process = None
            return False

        return True

    def start_mcp_server(self) -> bool:
        """Start test_watcher MCP server.

        Returns:
            True if server started successfully, False otherwise
        """
        if not self.mcp_server_path:
            logger.warning("MCP server path not configured")
            return False

        try:
            # Prepare environment variables
            env = os.environ.copy()
            env["TEST_WATCHER_PROJECT_ROOT"] = self.project_root

            # Use run_server.sh if it exists, otherwise use uv
            server_path = Path(self.mcp_server_path)
            run_script = server_path / "run_server.sh"

            if run_script.exists():
                cmd = [str(run_script)]
            else:
                cmd = ["uv", "run", str(server_path / "main.py")]

            # Start MCP server as subprocess
            self.mcp_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=env,
            )

            logger.info(f"Started Test Watcher MCP server with PID {self.mcp_process.pid}")

            # Start stderr pump for diagnostics
            if self.mcp_process.stderr:
                threading.Thread(
                    target=self._pump_stderr,
                    args=(self.mcp_process.stderr,),
                    daemon=True,
                ).start()

            return True
        except Exception as e:
            logger.error(f"Failed to start Test Watcher MCP server: {e}")
            return False

    def stop_mcp_server(self):
        """Stop test_watcher MCP server."""
        if self.mcp_process is not None:
            try:
                self.mcp_process.terminate()
                self.mcp_process.wait(timeout=5)
                logger.info("Stopped Test Watcher MCP server")
            except subprocess.TimeoutExpired:
                self.mcp_process.kill()
                logger.warning("Killed Test Watcher MCP server after timeout")
            except Exception as e:
                logger.error(f"Error stopping Test Watcher MCP server: {e}")
            finally:
                self.mcp_process = None

    def _pump_stderr(self, stderr):
        """Pump stderr from MCP server process for diagnostics."""
        try:
            for line in stderr:
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.debug(f"[Test Watcher MCP] {line_str}")
        except Exception as e:
            logger.debug(f"Error pumping stderr: {e}")

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
            "mcp_server": "test-watcher",
            "mcp_resources": [
                "test-watcher://status",
                "test-watcher://help",
            ],
        }

    def cleanup(self):
        """Cleanup resources."""
        self.stop_mcp_server()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.cleanup()
