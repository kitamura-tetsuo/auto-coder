"""
MCP Server Manager - Common management for multiple MCP servers.

This module provides a unified interface for managing multiple MCP servers
(graphrag_mcp, test_watcher, etc.) with automatic setup and configuration.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""

    name: str
    """Server name (e.g., 'graphrag', 'test-watcher')"""

    bundled_path: Path
    """Path to bundled server in auto_coder package"""

    install_dir: Optional[Path] = None
    """Installation directory (default: ~/mcp_servers/{name})"""

    requires_uv: bool = True
    """Whether the server requires uv for dependency management"""

    env_vars: Optional[Dict[str, str]] = None
    """Environment variables to set in .env file"""

    setup_callback: Optional[callable] = None
    """Optional callback for custom setup logic"""


class MCPServerManager:
    """Manager for multiple MCP servers."""

    def __init__(self):
        """Initialize MCP Server Manager."""
        self.servers: Dict[str, MCPServerConfig] = {}
        self._register_builtin_servers()

    def _register_builtin_servers(self):
        """Register built-in MCP servers."""
        # Get auto_coder package directory
        try:
            import auto_coder

            package_dir = Path(auto_coder.__file__).parent
        except ImportError:
            # Development mode
            package_dir = Path(__file__).parent

        # Register graphrag_mcp
        self.register_server(
            MCPServerConfig(
                name="graphrag",
                bundled_path=package_dir / "mcp_servers" / "graphrag_mcp",
                install_dir=Path.home() / "graphrag_mcp",
                requires_uv=True,
                env_vars={
                    "NEO4J_URI": "bolt://localhost:7687",
                    "NEO4J_USER": "neo4j",
                    "NEO4J_PASSWORD": "password",
                    "QDRANT_HOST": "localhost",
                    "QDRANT_PORT": "6333",
                    "QDRANT_COLLECTION": "document_chunks",
                },
            )
        )

        # Register test_watcher
        self.register_server(
            MCPServerConfig(
                name="test-watcher",
                bundled_path=package_dir / "mcp_servers" / "test_watcher",
                install_dir=Path.home() / "mcp_servers" / "test_watcher",
                requires_uv=True,
                env_vars={
                    "TEST_WATCHER_PROJECT_ROOT": str(Path.cwd()),
                },
            )
        )

    def register_server(self, config: MCPServerConfig):
        """Register an MCP server.

        Args:
            config: Server configuration
        """
        self.servers[config.name] = config
        logger.debug(f"Registered MCP server: {config.name}")

    def get_server_config(self, name: str) -> Optional[MCPServerConfig]:
        """Get server configuration by name.

        Args:
            name: Server name

        Returns:
            Server configuration or None if not found
        """
        return self.servers.get(name)

    def setup_server(
        self,
        name: str,
        install_dir: Optional[Path] = None,
        env_vars: Optional[Dict[str, str]] = None,
        backends: Optional[List[str]] = None,
        silent: bool = False,
    ) -> bool:
        """Setup an MCP server.

        Args:
            name: Server name
            install_dir: Installation directory (overrides default)
            env_vars: Environment variables (overrides default)
            backends: List of backends to configure (default: all)
            silent: Suppress user prompts

        Returns:
            True if setup was successful, False otherwise
        """
        config = self.get_server_config(name)
        if not config:
            logger.error(f"Unknown MCP server: {name}")
            return False

        # Use provided install_dir or default
        target_dir = install_dir or config.install_dir
        if not target_dir:
            logger.error(f"No installation directory specified for {name}")
            return False

        # Check if bundled server exists
        if not config.bundled_path.exists():
            logger.error(f"Bundled server not found: {config.bundled_path}")
            return False

        # Create installation directory
        target_dir.mkdir(parents=True, exist_ok=True)

        # Copy bundled server to installation directory
        logger.info(f"Copying {name} server to {target_dir}...")
        try:
            # Copy all files from bundled location
            for item in config.bundled_path.iterdir():
                if item.name in [".venv", "__pycache__", ".git", "server.log"]:
                    continue

                dest = target_dir / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            logger.info(f"✅ Copied {name} server files")
        except Exception as e:
            logger.error(f"Failed to copy server files: {e}")
            return False

        # Create .env file if env_vars provided
        merged_env_vars = {**(config.env_vars or {}), **(env_vars or {})}
        if merged_env_vars:
            env_file = target_dir / ".env"
            try:
                with open(env_file, "w") as f:
                    for key, value in merged_env_vars.items():
                        f.write(f"{key}={value}\n")
                logger.info(f"✅ Created .env file")
            except Exception as e:
                logger.error(f"Failed to create .env file: {e}")
                return False

        # Install dependencies with uv if required
        if config.requires_uv:
            logger.info(f"Installing dependencies with uv...")
            try:
                result = subprocess.run(
                    ["uv", "sync"],
                    cwd=str(target_dir),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    logger.error(f"Failed to install dependencies: {result.stderr}")
                    return False
                logger.info(f"✅ Installed dependencies")
            except Exception as e:
                logger.error(f"Failed to install dependencies: {e}")
                return False

        # Make run_server.sh executable
        run_script = target_dir / "run_server.sh"
        if run_script.exists():
            try:
                run_script.chmod(0o755)
                logger.info(f"✅ Made run_server.sh executable")
            except Exception as e:
                logger.warning(f"Failed to make run_server.sh executable: {e}")

        # Run custom setup callback if provided
        if config.setup_callback:
            try:
                config.setup_callback(target_dir)
            except Exception as e:
                logger.error(f"Custom setup callback failed: {e}")
                return False

        # Configure backends
        if backends is None:
            backends = ["codex", "gemini", "qwen", "auggie", "claude"]

        success = True
        for backend in backends:
            if not self.add_backend_config(name, backend, target_dir):
                logger.warning(f"Failed to configure {backend} backend for {name}")
                success = False

        return success

    def add_backend_config(
        self,
        server_name: str,
        backend: str,
        install_path: Path,
    ) -> bool:
        """Add MCP server configuration to a backend.

        Args:
            server_name: MCP server name
            backend: Backend name (codex, gemini, qwen, auggie)
            install_path: Path to installed server

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            if backend == "codex":
                return self._add_codex_config(server_name, install_path)
            elif backend == "gemini":
                return self._add_gemini_config(server_name, install_path)
            elif backend == "qwen":
                return self._add_qwen_config(server_name, install_path)
            elif backend == "auggie":
                return self._add_auggie_config(server_name, install_path)
            elif backend == "claude":
                return self._add_claude_config(server_name, install_path)
            else:
                logger.warning(f"Unknown backend: {backend}")
                return False
        except Exception as e:
            logger.error(f"Failed to add {backend} config for {server_name}: {e}")
            return False

    def _add_codex_config(self, server_name: str, install_path: Path) -> bool:
        """Add MCP server configuration to Codex CLI config.

        Args:
            server_name: MCP server name
            install_path: Path to installed server

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            from .codex_client import CodexClient

            client = CodexClient()
            result = client.add_mcp_server_config(
                server_name, "uv", ["run", str(install_path / "main.py")]
            )

            if result:
                logger.info(f"✅ Codex設定を更新しました ({server_name})")
            else:
                logger.error(f"Codex設定の更新に失敗しました ({server_name})")

            return result
        except Exception as e:
            logger.error(f"Failed to add Codex config for {server_name}: {e}")
            return False

    def _add_gemini_config(self, server_name: str, install_path: Path) -> bool:
        """Add MCP server configuration to Gemini CLI config.

        Args:
            server_name: MCP server name
            install_path: Path to installed server

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            from .gemini_client import GeminiClient

            client = GeminiClient()

            # Use run_server.sh if it exists
            run_script = install_path / "run_server.sh"
            if run_script.exists():
                result = client.add_mcp_server_config(server_name, str(run_script), [])
            else:
                # Fallback to uv
                result = client.add_mcp_server_config(
                    server_name,
                    "uv",
                    ["--directory", str(install_path), "run", "main.py"],
                )

            if result:
                logger.info(f"✅ Gemini設定を更新しました ({server_name})")
            else:
                logger.error(f"Gemini設定の更新に失敗しました ({server_name})")

            return result
        except Exception as e:
            logger.error(f"Failed to add Gemini config for {server_name}: {e}")
            return False

    def _add_qwen_config(self, server_name: str, install_path: Path) -> bool:
        """Add MCP server configuration to Qwen CLI config.

        Args:
            server_name: MCP server name
            install_path: Path to installed server

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            from .qwen_client import QwenClient

            client = QwenClient()

            # Use run_server.sh if it exists
            run_script = install_path / "run_server.sh"
            if run_script.exists():
                result = client.add_mcp_server_config(server_name, str(run_script), [])
            else:
                # Fallback to uv
                result = client.add_mcp_server_config(
                    server_name,
                    "uv",
                    ["--directory", str(install_path), "run", "main.py"],
                )

            if result:
                logger.info(f"✅ Qwen設定を更新しました ({server_name})")
            else:
                logger.error(f"Qwen設定の更新に失敗しました ({server_name})")

            return result
        except Exception as e:
            logger.error(f"Failed to add Qwen config for {server_name}: {e}")
            return False

    def _add_auggie_config(self, server_name: str, install_path: Path) -> bool:
        """Add MCP server configuration to Auggie CLI config (Windsurf/Claude).

        Args:
            server_name: MCP server name
            install_path: Path to installed server

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            from .auggie_client import AuggieClient

            client = AuggieClient()

            # Use run_server.sh if it exists
            run_script = install_path / "run_server.sh"
            if run_script.exists():
                result = client.add_mcp_server_config(server_name, str(run_script), [])
            else:
                # Fallback to uv
                result = client.add_mcp_server_config(
                    server_name,
                    "uv",
                    ["--directory", str(install_path), "run", "main.py"],
                )

            if result:
                logger.info(f"✅ Windsurf/Claude設定を更新しました ({server_name})")
            else:
                logger.error(f"Windsurf/Claude設定の更新に失敗しました ({server_name})")

            return result
        except Exception as e:
            logger.error(f"Failed to add Windsurf/Claude config for {server_name}: {e}")

    def _add_claude_config(self, server_name: str, install_path: Path) -> bool:
        """Add MCP server configuration to Claude CLI config.

        Args:
            server_name: MCP server name
            install_path: Path to installed server

        Returns:
            True if configuration was added successfully, False otherwise
        """
        try:
            from .claude_client import ClaudeClient

            client = ClaudeClient()

            # Use run_server.sh if it exists
            run_script = install_path / "run_server.sh"
            if run_script.exists():
                result = client.add_mcp_server_config(server_name, str(run_script), [])
            else:
                # Fallback to uv
                result = client.add_mcp_server_config(
                    server_name,
                    "uv",
                    ["--directory", str(install_path), "run", "main.py"],
                )

            if result:
                logger.info(f"✅ Claude設定を更新しました ({server_name})")
            else:
                logger.error(f"Claude設定の更新に失敗しました ({server_name})")

            return result
        except Exception as e:
            logger.error(f"Failed to add Claude config for {server_name}: {e}")
            return False

    def is_server_installed(self, name: str) -> bool:
        """Check if an MCP server is installed.

        Args:
            name: Server name

        Returns:
            True if server is installed, False otherwise
        """
        config = self.get_server_config(name)
        if not config or not config.install_dir:
            return False

        # Check if installation directory exists and has required files
        if not config.install_dir.exists():
            return False

        # Check for main.py or server.py
        if (
            not (config.install_dir / "main.py").exists()
            and not (config.install_dir / "server.py").exists()
        ):
            return False

        return True

    def get_server_path(self, name: str) -> Optional[Path]:
        """Get the installation path of an MCP server.

        Args:
            name: Server name

        Returns:
            Installation path or None if not installed
        """
        config = self.get_server_config(name)
        if not config or not config.install_dir:
            return None

        if self.is_server_installed(name):
            return config.install_dir

        return None


# Global instance
_mcp_manager: Optional[MCPServerManager] = None


def get_mcp_manager() -> MCPServerManager:
    """Get the global MCP manager instance.

    Returns:
        Global MCP manager instance
    """
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPServerManager()
    return _mcp_manager
