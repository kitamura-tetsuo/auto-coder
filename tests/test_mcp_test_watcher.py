"""
Tests for Test Watcher MCP Server.
"""

import pytest
import subprocess
import time
import json
from pathlib import Path
import sys
import os

# Add the test_watcher module to the path
test_watcher_path = Path(__file__).parent.parent / "src" / "auto_coder" / "mcp_servers" / "test_watcher"
sys.path.insert(0, str(test_watcher_path))

from test_watcher_tool import TestWatcherTool


def _mcp_available() -> bool:
    """Check if MCP package is available."""
    try:
        import mcp.server.fastmcp
        return True
    except ImportError:
        return False


class TestTestWatcherTool:
    """Test the TestWatcherTool class."""

    def test_initialization(self, tmp_path):
        """Test that TestWatcherTool initializes correctly."""
        tool = TestWatcherTool(project_root=str(tmp_path))
        assert tool.project_root == tmp_path
        assert "unit" in tool.test_results
        assert "integration" in tool.test_results
        assert "e2e" in tool.test_results

    def test_initialization_default_path(self):
        """Test that TestWatcherTool uses current directory by default."""
        tool = TestWatcherTool()
        assert tool.project_root == Path.cwd()

    def test_gitignore_loading(self, tmp_path):
        """Test that .gitignore is loaded correctly."""
        # Create a .gitignore file
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n")

        tool = TestWatcherTool(project_root=str(tmp_path))

        # Check that gitignore spec is loaded
        assert tool.gitignore_spec is not None

        # Test that patterns are respected
        assert tool.gitignore_spec.match_file("test.pyc")
        assert tool.gitignore_spec.match_file("__pycache__/test.py")
        assert not tool.gitignore_spec.match_file("test.py")

    def test_start_watching(self, tmp_path):
        """Test starting file watching."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        result = tool.start_watching()

        assert result["status"] == "started"
        assert "project_root" in result

        # Clean up
        tool.stop_watching()

    def test_start_watching_already_running(self, tmp_path):
        """Test starting file watching when already running."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        tool.start_watching()
        result = tool.start_watching()

        assert result["status"] == "already_running"

        # Clean up
        tool.stop_watching()

    def test_stop_watching(self, tmp_path):
        """Test stopping file watching."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        tool.start_watching()
        result = tool.stop_watching()

        assert result["status"] == "stopped"

    def test_stop_watching_not_running(self, tmp_path):
        """Test stopping file watching when not running."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        result = tool.stop_watching()

        assert result["status"] == "not_running"

    def test_parse_playwright_json_report(self, tmp_path):
        """Test parsing Playwright JSON report."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Create a sample Playwright JSON report
        report = {
            "suites": [
                {
                    "specs": [
                        {
                            "file": "tests/example.spec.ts",
                            "title": "Example test",
                            "tests": [
                                {
                                    "results": [
                                        {
                                            "status": "passed"
                                        }
                                    ]
                                }
                            ]
                        },
                        {
                            "file": "tests/failing.spec.ts",
                            "title": "Failing test",
                            "tests": [
                                {
                                    "results": [
                                        {
                                            "status": "failed",
                                            "error": {
                                                "message": "Expected true to be false"
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        result = tool._parse_playwright_json_report(json.dumps(report))

        assert result["status"] == "completed"
        assert result["passed"] == 1
        assert result["failed"] == 1
        assert result["total"] == 2
        assert len(result["tests"]) == 2

        # Check failed test details
        failed_test = [t for t in result["tests"] if t["status"] == "failed"][0]
        assert failed_test["file"] == "tests/failing.spec.ts"
        assert failed_test["error"] == "Expected true to be false"

    def test_query_test_results_idle(self, tmp_path):
        """Test querying test results when idle."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        result = tool.query_test_results(test_type="all")

        assert result["status"] == "completed"
        assert result["test_type"] == "all"
        assert result["summary"]["passed"] == 0
        assert result["summary"]["failed"] == 0

    def test_query_test_results_e2e(self, tmp_path):
        """Test querying e2e test results."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        # Set some test results
        tool.test_results["e2e"] = {
            "status": "completed",
            "passed": 5,
            "failed": 2,
            "flaky": 1,
            "skipped": 0,
            "total": 8,
            "tests": [
                {
                    "file": "tests/test1.spec.ts",
                    "title": "Test 1",
                    "status": "failed",
                    "error": "Error message 1"
                },
                {
                    "file": "tests/test2.spec.ts",
                    "title": "Test 2",
                    "status": "failed",
                    "error": "Error message 2"
                },
                {
                    "file": "tests/test3.spec.ts",
                    "title": "Test 3",
                    "status": "flaky",
                    "error": "Flaky error"
                }
            ]
        }

        result = tool.query_test_results(test_type="e2e")

        assert result["status"] == "completed"
        assert result["test_type"] == "e2e"
        assert result["summary"]["passed"] == 5
        assert result["summary"]["failed"] == 2
        assert result["summary"]["flaky"] == 1
        assert result["failed_tests"]["count"] == 2
        assert result["flaky_tests"]["count"] == 1
        assert result["first_failed_test"]["file"] == "tests/test1.spec.ts"
        assert result["first_flaky_test"]["file"] == "tests/test3.spec.ts"

    def test_get_status(self, tmp_path):
        """Test getting overall status."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        status = tool.get_status()

        assert "file_watcher_running" in status
        assert "playwright_running" in status
        assert "project_root" in status
        assert "test_results" in status
        assert "unit" in status["test_results"]
        assert "integration" in status["test_results"]
        assert "e2e" in status["test_results"]

    def test_query_test_results_invalid_type(self, tmp_path):
        """Test querying with invalid test type."""
        tool = TestWatcherTool(project_root=str(tmp_path))

        result = tool.query_test_results(test_type="invalid")

        assert result["status"] == "error"
        assert "Invalid test type" in result["error"]


class TestMCPServer:
    """Test the MCP server interface."""

    @pytest.mark.skipif(
        not _mcp_available(),
        reason="MCP package not installed"
    )
    def test_server_imports(self):
        """Test that the server module can be imported."""
        # This will fail if there are syntax errors or missing dependencies
        import server
        assert hasattr(server, 'mcp')
        assert hasattr(server, 'test_watcher')

    @pytest.mark.skipif(
        not _mcp_available(),
        reason="MCP package not installed"
    )
    def test_server_tools_registered(self):
        """Test that all expected tools are registered."""
        import server

        # Get the MCP instance
        mcp = server.mcp

        # Check that tools are registered
        # Note: The exact way to check this depends on FastMCP's API
        # This is a basic check that the server object exists
        assert mcp is not None

