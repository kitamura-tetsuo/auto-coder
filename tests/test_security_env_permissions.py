import os
import stat
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.mcp_manager import MCPServerConfig, MCPServerManager


class TestMCPSecurity:
    """Security tests for MCPServerManager."""

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
