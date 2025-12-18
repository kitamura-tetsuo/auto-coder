import os
import shutil
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.cli_commands_graphrag import run_graphrag_setup_mcp_programmatically
from src.auto_coder.mcp_manager import MCPServerConfig, MCPServerManager


class TestMCPSecurity:
    """Security tests for MCPServerManager and GraphRAG setup."""

    @patch("subprocess.run")
    def test_setup_server_env_permissions(self, mock_run, tmp_path):
        """Test that the .env file created by setup_server has secure permissions (0600)."""
        manager = MCPServerManager()

        # Create mock bundled directory
        bundled_path = tmp_path / "bundled"
        bundled_path.mkdir()
        (bundled_path / "main.py").write_text("# main")

        # Create mock install directory
        install_dir = tmp_path / "install"

        # Register test server with sensitive env vars
        config = MCPServerConfig(
            name="test-server-secure",
            bundled_path=bundled_path,
            install_dir=install_dir,
            requires_uv=False,  # Skip uv to avoid subprocess calls
            env_vars={"SECRET_PASSWORD": "super_secret_value"},
        )
        manager.register_server(config)

        # Setup server
        # We need to mock add_backend_config to avoid external dependencies
        with patch.object(manager, "add_backend_config", return_value=True):
            result = manager.setup_server(
                "test-server-secure",
                backends=[],
                silent=True,
            )

        assert result is True

        env_file = install_dir / ".env"
        assert env_file.exists()

        # Check file content
        content = env_file.read_text()
        assert "SECRET_PASSWORD=super_secret_value" in content

        # Check file permissions
        st_mode = os.stat(env_file).st_mode
        permissions = st_mode & 0o777

        # We expect 0600 (rw-------)
        # Verify that group and others have NO permissions
        assert permissions & 0o077 == 0, f"Permissions are too open: {oct(permissions)}"
        assert permissions == 0o600, f"Expected 0600, got {oct(permissions)}"

    @patch("subprocess.run")
    @patch("src.auto_coder.cli_commands_graphrag._add_codex_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_gemini_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_qwen_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_windsurf_claude_config")
    def test_graphrag_setup_env_permissions(self, mock_windsurf, mock_qwen, mock_gemini, mock_codex, mock_run, tmp_path):
        """Test that the .env file created by GraphRAG setup has secure permissions (0600)."""
        # Create mock bundled directory structure
        # The function looks for bundled mcp in auto_coder package

        # Since we can't easily mock the package path import logic inside the function without more patches,
        # we will use --skip-clone and manually create the directory

        install_dir = tmp_path / "graphrag_mcp"
        install_dir.mkdir()

        # Mock subprocess.run to pretend uv is installed
        mock_run.side_effect = lambda cmd, **kwargs: MagicMock(returncode=0, stdout="uv 0.1.0")

        # Mock backend adders to return True
        mock_codex.return_value = True
        mock_gemini.return_value = True
        mock_qwen.return_value = True
        mock_windsurf.return_value = True

        result = run_graphrag_setup_mcp_programmatically(install_dir=str(install_dir), skip_clone=True, silent=True, neo4j_password="secret_graph_password")

        assert result is True

        env_file = install_dir / ".env"
        assert env_file.exists()

        # Check file content
        content = env_file.read_text()
        assert "NEO4J_PASSWORD=secret_graph_password" in content

        # Check file permissions
        st_mode = os.stat(env_file).st_mode
        permissions = st_mode & 0o777

        assert permissions & 0o077 == 0, f"Permissions are too open: {oct(permissions)}"
        assert permissions == 0o600, f"Expected 0600, got {oct(permissions)}"
