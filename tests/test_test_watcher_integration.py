"""Tests for Test Watcher Integration."""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.test_watcher_integration import TestWatcherIntegration


class TestTestWatcherIntegration:
    """Tests for TestWatcherIntegration."""

    def test_initialization_default(self):
        """Test initialization with default values."""
        integration = TestWatcherIntegration()

        assert integration.mcp_server_path is None or isinstance(integration.mcp_server_path, str)
        assert integration.project_root == str(Path.cwd())
        assert integration.mcp_process is None

    def test_initialization_with_params(self):
        """Test initialization with custom parameters."""
        integration = TestWatcherIntegration(
            mcp_server_path="/path/to/server",
            project_root="/path/to/project",
        )

        assert integration.mcp_server_path == "/path/to/server"
        assert integration.project_root == "/path/to/project"
        assert integration.mcp_process is None

    def test_is_mcp_server_installed_no_path(self):
        """Test checking if server is installed (no path)."""
        integration = TestWatcherIntegration(mcp_server_path=None)

        assert integration.is_mcp_server_installed() is False

    def test_is_mcp_server_installed_not_exists(self):
        """Test checking if server is installed (path doesn't exist)."""
        integration = TestWatcherIntegration(mcp_server_path="/non/existent/path")

        assert integration.is_mcp_server_installed() is False

    def test_is_mcp_server_installed_no_main_file(self, tmp_path):
        """Test checking if server is installed (no main.py or server.py)."""
        server_path = tmp_path / "server"
        server_path.mkdir()

        integration = TestWatcherIntegration(mcp_server_path=str(server_path))

        assert integration.is_mcp_server_installed() is False

    def test_is_mcp_server_installed_with_main_py(self, tmp_path):
        """Test checking if server is installed (with main.py)."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "main.py").write_text("# main")

        integration = TestWatcherIntegration(mcp_server_path=str(server_path))

        assert integration.is_mcp_server_installed() is True

    def test_is_mcp_server_installed_with_server_py(self, tmp_path):
        """Test checking if server is installed (with server.py)."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "server.py").write_text("# server")

        integration = TestWatcherIntegration(mcp_server_path=str(server_path))

        assert integration.is_mcp_server_installed() is True

    def test_is_mcp_server_running_no_process(self):
        """Test checking if server is running (no process)."""
        integration = TestWatcherIntegration()

        assert integration.is_mcp_server_running() is False

    def test_is_mcp_server_running_process_terminated(self):
        """Test checking if server is running (process terminated)."""
        integration = TestWatcherIntegration()

        # Mock terminated process
        mock_process = Mock()
        mock_process.poll.return_value = 0  # Process has terminated
        integration.mcp_process = mock_process

        assert integration.is_mcp_server_running() is False
        assert integration.mcp_process is None

    def test_is_mcp_server_running_process_alive(self):
        """Test checking if server is running (process alive)."""
        integration = TestWatcherIntegration()

        # Mock running process
        mock_process = Mock()
        mock_process.poll.return_value = None  # Process is still running
        integration.mcp_process = mock_process

        assert integration.is_mcp_server_running() is True

    @patch("subprocess.Popen")
    @patch("threading.Thread")
    def test_start_mcp_server_with_run_script(self, mock_thread, mock_popen, tmp_path):
        """Test starting MCP server with run_server.sh."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "main.py").write_text("# main")
        run_script = server_path / "run_server.sh"
        run_script.write_text("#!/bin/bash\necho test")

        integration = TestWatcherIntegration(
            mcp_server_path=str(server_path),
            project_root="/path/to/project",
        )

        # Mock Popen
        mock_process = Mock()
        mock_process.pid = 12345
        mock_process.stderr = Mock()
        mock_popen.return_value = mock_process

        result = integration.start_mcp_server()

        assert result is True
        assert integration.mcp_process == mock_process

        # Check command
        call_args = mock_popen.call_args
        assert str(run_script) in call_args[0][0]

        # Check environment
        env = call_args[1]["env"]
        assert env["TEST_WATCHER_PROJECT_ROOT"] == "/path/to/project"

    @patch("subprocess.Popen")
    @patch("threading.Thread")
    def test_start_mcp_server_with_uv(self, mock_thread, mock_popen, tmp_path):
        """Test starting MCP server with uv."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "main.py").write_text("# main")
        # No run_server.sh

        integration = TestWatcherIntegration(
            mcp_server_path=str(server_path),
            project_root="/path/to/project",
        )

        # Mock Popen
        mock_process = Mock()
        mock_process.pid = 12345
        mock_process.stderr = Mock()
        mock_popen.return_value = mock_process

        result = integration.start_mcp_server()

        assert result is True
        assert integration.mcp_process == mock_process

        # Check command
        call_args = mock_popen.call_args
        assert "uv" in call_args[0][0]
        assert "run" in call_args[0][0]

    def test_start_mcp_server_no_path(self):
        """Test starting MCP server with no path."""
        integration = TestWatcherIntegration(mcp_server_path=None)

        result = integration.start_mcp_server()

        assert result is False

    @patch("subprocess.Popen")
    def test_start_mcp_server_exception(self, mock_popen, tmp_path):
        """Test starting MCP server with exception."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "main.py").write_text("# main")

        integration = TestWatcherIntegration(mcp_server_path=str(server_path))

        # Mock Popen to raise exception
        mock_popen.side_effect = Exception("Test error")

        result = integration.start_mcp_server()

        assert result is False

    def test_stop_mcp_server_no_process(self):
        """Test stopping MCP server with no process."""
        integration = TestWatcherIntegration()

        # Should not raise exception
        integration.stop_mcp_server()

        assert integration.mcp_process is None

    def test_stop_mcp_server_normal(self):
        """Test stopping MCP server normally."""
        integration = TestWatcherIntegration()

        # Mock process
        mock_process = Mock()
        integration.mcp_process = mock_process

        integration.stop_mcp_server()

        mock_process.terminate.assert_called_once()
        mock_process.wait.assert_called_once_with(timeout=5)
        assert integration.mcp_process is None

    def test_stop_mcp_server_timeout(self):
        """Test stopping MCP server with timeout."""
        import subprocess

        integration = TestWatcherIntegration()

        # Mock process that times out
        mock_process = Mock()
        mock_process.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
        integration.mcp_process = mock_process

        integration.stop_mcp_server()

        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()
        assert integration.mcp_process is None

    def test_get_mcp_config_for_llm_not_running(self):
        """Test getting MCP config when server is not running."""
        integration = TestWatcherIntegration()

        config = integration.get_mcp_config_for_llm()

        assert config is None

    def test_get_mcp_config_for_llm_running(self):
        """Test getting MCP config when server is running."""
        integration = TestWatcherIntegration()

        # Mock running process
        mock_process = Mock()
        mock_process.poll.return_value = None
        integration.mcp_process = mock_process

        config = integration.get_mcp_config_for_llm()

        assert config is not None
        assert config["mcp_server"] == "test-watcher"
        assert "test-watcher://status" in config["mcp_resources"]
        assert "test-watcher://help" in config["mcp_resources"]

    def test_context_manager(self):
        """Test context manager usage."""
        integration = TestWatcherIntegration()

        # Mock process
        mock_process = Mock()
        integration.mcp_process = mock_process

        with integration as ctx:
            assert ctx is integration

        # Should have called cleanup
        mock_process.terminate.assert_called_once()

    def test_ensure_ready_not_installed(self):
        """Test ensure_ready when server is not installed."""
        integration = TestWatcherIntegration(mcp_server_path="/non/existent/path")

        result = integration.ensure_ready()

        assert result is False

    @patch.object(TestWatcherIntegration, "start_mcp_server")
    def test_ensure_ready_start_failure(self, mock_start, tmp_path):
        """Test ensure_ready when server start fails."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "main.py").write_text("# main")

        integration = TestWatcherIntegration(mcp_server_path=str(server_path))

        # Mock start_mcp_server to fail
        mock_start.return_value = False

        result = integration.ensure_ready()

        assert result is False

    @patch.object(TestWatcherIntegration, "start_mcp_server")
    def test_ensure_ready_success(self, mock_start, tmp_path):
        """Test ensure_ready when everything succeeds."""
        server_path = tmp_path / "server"
        server_path.mkdir()
        (server_path / "main.py").write_text("# main")

        integration = TestWatcherIntegration(mcp_server_path=str(server_path))

        # Mock start_mcp_server to succeed
        mock_start.return_value = True

        result = integration.ensure_ready()

        assert result is True
