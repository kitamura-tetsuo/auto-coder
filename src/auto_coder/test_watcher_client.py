"""
Test Watcher MCP Client for querying test results.
"""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .logger_config import get_logger

logger = get_logger(__name__)


class TestWatcherClient:
    """Client for querying test_watcher MCP server."""

    def __init__(self, mcp_server_path: Optional[str] = None, project_root: Optional[str] = None):
        """
        Initialize Test Watcher Client.

        Args:
            mcp_server_path: Path to test_watcher MCP server
            project_root: Project root directory
        """
        self.mcp_server_path = mcp_server_path or str(Path.home() / "mcp_servers" / "test_watcher")
        self.project_root = project_root or str(Path.cwd())
        self.process: Optional[subprocess.Popen] = None

    def start_server(self) -> bool:
        """
        Start the MCP server if not already running.

        Returns:
            True if server started successfully
        """
        if self.process and self.process.poll() is None:
            logger.debug("MCP server already running")
            return True

        try:
            server_path = Path(self.mcp_server_path)
            run_script = server_path / "run_server.sh"

            if run_script.exists():
                cmd = [str(run_script)]
            else:
                cmd = ["uv", "run", str(server_path / "server.py")]

            import os

            env = os.environ.copy()
            env["TEST_WATCHER_PROJECT_ROOT"] = self.project_root

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )

            logger.info(f"Started test_watcher MCP server (PID: {self.process.pid})")
            return True

        except Exception as e:
            logger.error(f"Failed to start MCP server: {e}")
            return False

    def stop_server(self):
        """Stop the MCP server."""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            logger.info("Stopped test_watcher MCP server")

    def _call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Call an MCP tool.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool result
        """
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("MCP server is not running")

        # Build JSON-RPC request
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }

        try:
            # Send request
            request_json = json.dumps(request) + "\n"
            self.process.stdin.write(request_json)
            self.process.stdin.flush()

            # Read response
            response_line = self.process.stdout.readline()
            response = json.loads(response_line)

            if "error" in response:
                raise RuntimeError(f"MCP error: {response['error']}")

            return response.get("result", {})

        except Exception as e:
            logger.error(f"Failed to call MCP tool {tool_name}: {e}")
            raise

    def start_watching(self) -> Dict[str, Any]:
        """
        Start file watching.

        Returns:
            Status of the watcher startup
        """
        return self._call_tool("start_watching")

    def stop_watching(self) -> Dict[str, Any]:
        """
        Stop file watching.

        Returns:
            Status of the watcher shutdown
        """
        return self._call_tool("stop_watching")

    def query_test_results(self, test_type: str = "all") -> Dict[str, Any]:
        """
        Query test results.

        Args:
            test_type: Type of tests to query (unit/integration/e2e/all)

        Returns:
            Test results
        """
        return self._call_tool("query_test_results", {"test_type": test_type})

    def get_status(self) -> Dict[str, Any]:
        """
        Get overall status.

        Returns:
            Status information
        """
        return self._call_tool("get_status")

    def __enter__(self):
        """Context manager entry."""
        self.start_server()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop_server()
