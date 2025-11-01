"""Tests for MCP Manager."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from src.auto_coder.mcp_manager import (
    MCPServerConfig,
    MCPServerManager,
    get_mcp_manager,
)


class TestMCPServerConfig:
    """Tests for MCPServerConfig dataclass."""

    def test_basic_config(self):
        """Test basic server configuration."""
        config = MCPServerConfig(
            name="test-server",
            bundled_path=Path("/path/to/bundled"),
        )

        assert config.name == "test-server"
        assert config.bundled_path == Path("/path/to/bundled")
        assert config.install_dir is None
        assert config.requires_uv is True
        assert config.env_vars is None
        assert config.setup_callback is None

    def test_full_config(self):
        """Test full server configuration."""
        callback = lambda x: None
        config = MCPServerConfig(
            name="test-server",
            bundled_path=Path("/path/to/bundled"),
            install_dir=Path("/path/to/install"),
            requires_uv=False,
            env_vars={"KEY": "value"},
            setup_callback=callback,
        )

        assert config.name == "test-server"
        assert config.bundled_path == Path("/path/to/bundled")
        assert config.install_dir == Path("/path/to/install")
        assert config.requires_uv is False
        assert config.env_vars == {"KEY": "value"}
        assert config.setup_callback == callback


class TestMCPServerManager:
    """Tests for MCPServerManager."""

    def test_initialization(self):
        """Test manager initialization."""
        manager = MCPServerManager()

        # Should have built-in servers registered
        assert "graphrag" in manager.servers
        assert "test-watcher" in manager.servers

    def test_register_server(self):
        """Test registering a new server."""
        manager = MCPServerManager()

        config = MCPServerConfig(
            name="custom-server",
            bundled_path=Path("/path/to/custom"),
        )

        manager.register_server(config)

        assert "custom-server" in manager.servers
        assert manager.servers["custom-server"] == config

    def test_get_server_config(self):
        """Test getting server configuration."""
        manager = MCPServerManager()

        # Get existing server
        config = manager.get_server_config("graphrag")
        assert config is not None
        assert config.name == "graphrag"

        # Get non-existent server
        config = manager.get_server_config("non-existent")
        assert config is None

    def test_is_server_installed_not_installed(self):
        """Test checking if server is installed (not installed)."""
        manager = MCPServerManager()

        # Mock config with non-existent directory
        with patch.object(manager, "get_server_config") as mock_get:
            mock_config = MCPServerConfig(
                name="test-server",
                bundled_path=Path("/path/to/bundled"),
                install_dir=Path("/non/existent/path"),
            )
            mock_get.return_value = mock_config

            assert manager.is_server_installed("test-server") is False

    def test_is_server_installed_no_config(self):
        """Test checking if server is installed (no config)."""
        manager = MCPServerManager()

        assert manager.is_server_installed("non-existent") is False

    def test_get_server_path_not_installed(self):
        """Test getting server path (not installed)."""
        manager = MCPServerManager()

        # Mock config with non-existent directory
        with patch.object(manager, "get_server_config") as mock_get:
            mock_config = MCPServerConfig(
                name="test-server",
                bundled_path=Path("/path/to/bundled"),
                install_dir=Path("/non/existent/path"),
            )
            mock_get.return_value = mock_config

            assert manager.get_server_path("test-server") is None

    def test_get_server_path_no_config(self):
        """Test getting server path (no config)."""
        manager = MCPServerManager()

        assert manager.get_server_path("non-existent") is None

    @patch("subprocess.run")
    @patch("shutil.copytree")
    @patch("shutil.copy2")
    def test_setup_server_basic(self, mock_copy2, mock_copytree, mock_run, tmp_path):
        """Test basic server setup."""
        manager = MCPServerManager()

        # Create mock bundled directory
        bundled_path = tmp_path / "bundled"
        bundled_path.mkdir()
        (bundled_path / "main.py").write_text("# main")
        (bundled_path / "pyproject.toml").write_text("[project]")

        # Create mock install directory
        install_dir = tmp_path / "install"

        # Register test server
        config = MCPServerConfig(
            name="test-server",
            bundled_path=bundled_path,
            install_dir=install_dir,
            requires_uv=True,
            env_vars={"KEY": "value"},
        )
        manager.register_server(config)

        # Mock subprocess.run for uv sync
        mock_run.return_value = Mock(returncode=0)

        # Mock backend config methods
        with patch.object(manager, "_add_codex_config", return_value=True):
            # Setup server
            result = manager.setup_server(
                "test-server",
                backends=["codex"],
                silent=True,
            )

        assert result is True
        assert install_dir.exists()
        assert (install_dir / ".env").exists()

        # Check .env content
        env_content = (install_dir / ".env").read_text()
        assert "KEY=value" in env_content

    def test_setup_server_unknown(self):
        """Test setup of unknown server."""
        manager = MCPServerManager()

        result = manager.setup_server("non-existent")

        assert result is False

    @patch("subprocess.run")
    def test_setup_server_uv_failure(self, mock_run, tmp_path):
        """Test server setup with uv failure."""
        manager = MCPServerManager()

        # Create mock bundled directory
        bundled_path = tmp_path / "bundled"
        bundled_path.mkdir()
        (bundled_path / "main.py").write_text("# main")

        # Create mock install directory
        install_dir = tmp_path / "install"

        # Register test server
        config = MCPServerConfig(
            name="test-server",
            bundled_path=bundled_path,
            install_dir=install_dir,
            requires_uv=True,
        )
        manager.register_server(config)

        # Mock subprocess.run for uv sync (failure)
        mock_run.return_value = Mock(returncode=1, stderr="uv error")

        # Setup server
        result = manager.setup_server(
            "test-server",
            backends=[],
            silent=True,
        )

        assert result is False

    def test_add_backend_config_unknown_backend(self, tmp_path):
        """Test adding backend config for unknown backend."""
        manager = MCPServerManager()

        result = manager.add_backend_config(
            "test-server",
            "unknown-backend",
            tmp_path,
        )

        assert result is False


class TestGetMCPManager:
    """Tests for get_mcp_manager function."""

    def test_get_mcp_manager_singleton(self):
        """Test that get_mcp_manager returns singleton instance."""
        manager1 = get_mcp_manager()
        manager2 = get_mcp_manager()

        assert manager1 is manager2

    def test_get_mcp_manager_returns_manager(self):
        """Test that get_mcp_manager returns MCPServerManager instance."""
        manager = get_mcp_manager()

        assert isinstance(manager, MCPServerManager)
        assert "graphrag" in manager.servers
        assert "test-watcher" in manager.servers


class TestMCPServerManagerClaude:
    """Tests for MCPServerManager with Claude backend."""

    def test_add_backend_config_claude_success(self, tmp_path):
        manager = MCPServerManager()
        with patch.object(manager, "_add_claude_config", return_value=True) as mock_add:
            ok = manager.add_backend_config("graphrag", "claude", tmp_path)
            assert ok is True
            mock_add.assert_called_once()

    def test_add_backend_config_claude_failure(self, tmp_path):
        manager = MCPServerManager()
        with patch.object(
            manager, "_add_claude_config", return_value=False
        ) as mock_add:
            ok = manager.add_backend_config("graphrag", "claude", tmp_path)
            assert ok is False
            mock_add.assert_called_once()
