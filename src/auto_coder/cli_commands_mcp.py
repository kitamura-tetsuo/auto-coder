"""
CLI commands for MCP server management.

Provides commands to setup and manage MCP servers.
"""

import subprocess
from pathlib import Path
from typing import Optional

import click

from .logger_config import get_logger, setup_logger
from .mcp_manager import get_mcp_manager

logger = get_logger(__name__)


@click.group(name="mcp")
def mcp_group():
    """MCP server management commands."""
    pass


@mcp_group.command(name="setup")
@click.argument("server_name", type=str)
@click.option(
    "--install-dir",
    type=click.Path(),
    default=None,
    help="Installation directory (default: ~/mcp_servers/{server_name})",
)
@click.option(
    "--backend",
    "-b",
    multiple=True,
    type=click.Choice(["codex", "gemini", "qwen", "auggie", "all"]),
    default=["all"],
    help="Backend(s) to configure (default: all)",
)
@click.option(
    "--env",
    "-e",
    multiple=True,
    type=str,
    help="Environment variables in KEY=VALUE format",
)
@click.option(
    "--silent",
    is_flag=True,
    help="Suppress user prompts and run automatically",
)
def mcp_setup(
    server_name: str,
    install_dir: Optional[str],
    backend: tuple,
    env: tuple,
    silent: bool,
) -> None:
    """Setup an MCP server.
    
    Examples:
    
        # Setup test-watcher with default settings
        auto-coder mcp setup test-watcher
        
        # Setup with custom installation directory
        auto-coder mcp setup test-watcher --install-dir ~/my-mcp-servers/test-watcher
        
        # Setup for specific backends only
        auto-coder mcp setup test-watcher -b codex -b gemini
        
        # Setup with custom environment variables
        auto-coder mcp setup test-watcher -e TEST_WATCHER_PROJECT_ROOT=/path/to/project
    """
    setup_logger()
    
    # Get MCP manager
    manager = get_mcp_manager()
    
    # Check if server is registered
    config = manager.get_server_config(server_name)
    if not config:
        logger.error(f"Unknown MCP server: {server_name}")
        logger.info("Available servers:")
        for name in manager.servers.keys():
            logger.info(f"  - {name}")
        return
    
    # Parse environment variables
    env_vars = {}
    for env_str in env:
        if "=" not in env_str:
            logger.warning(f"Invalid environment variable format: {env_str}")
            continue
        key, value = env_str.split("=", 1)
        env_vars[key] = value
    
    # Determine backends
    backends = list(backend)
    if "all" in backends:
        backends = ["codex", "gemini", "qwen", "auggie"]
    
    # Setup server
    install_path = Path(install_dir) if install_dir else None
    success = manager.setup_server(
        server_name,
        install_dir=install_path,
        env_vars=env_vars,
        backends=backends,
        silent=silent,
    )
    
    if success:
        logger.info(f"✅ Successfully setup {server_name} MCP server")
        
        # Show next steps
        server_path = manager.get_server_path(server_name)
        if server_path:
            logger.info("\nNext steps:")
            logger.info(f"1. Review configuration: {server_path}/.env")
            logger.info(f"2. Test the server: cd {server_path} && ./run_server.sh")
            logger.info(f"3. The server is now configured for your LLM backends")
    else:
        logger.error(f"Failed to setup {server_name} MCP server")
        raise click.ClickException(f"Setup failed for {server_name}")


@mcp_group.command(name="list")
def mcp_list() -> None:
    """List available MCP servers."""
    setup_logger()
    
    manager = get_mcp_manager()
    
    logger.info("Available MCP servers:")
    for name, config in manager.servers.items():
        installed = manager.is_server_installed(name)
        status = "✅ installed" if installed else "❌ not installed"
        logger.info(f"  - {name}: {status}")
        if installed:
            server_path = manager.get_server_path(name)
            logger.info(f"    Path: {server_path}")


@mcp_group.command(name="status")
@click.argument("server_name", type=str)
def mcp_status(server_name: str) -> None:
    """Show status of an MCP server."""
    setup_logger()
    
    manager = get_mcp_manager()
    
    config = manager.get_server_config(server_name)
    if not config:
        logger.error(f"Unknown MCP server: {server_name}")
        return
    
    installed = manager.is_server_installed(server_name)
    logger.info(f"Server: {server_name}")
    logger.info(f"Status: {'✅ installed' if installed else '❌ not installed'}")
    
    if installed:
        server_path = manager.get_server_path(server_name)
        logger.info(f"Path: {server_path}")
        
        # Check if .env file exists
        env_file = server_path / ".env"
        if env_file.exists():
            logger.info(f"Configuration: {env_file}")
        
        # Check if run_server.sh exists
        run_script = server_path / "run_server.sh"
        if run_script.exists():
            logger.info(f"Run script: {run_script}")
    else:
        logger.info(f"Run 'auto-coder mcp setup {server_name}' to install")


@mcp_group.command(name="test")
@click.argument("server_name", type=str)
def mcp_test(server_name: str) -> None:
    """Test an MCP server by running it briefly."""
    setup_logger()
    
    manager = get_mcp_manager()
    
    config = manager.get_server_config(server_name)
    if not config:
        logger.error(f"Unknown MCP server: {server_name}")
        return
    
    if not manager.is_server_installed(server_name):
        logger.error(f"MCP server {server_name} is not installed")
        logger.info(f"Run 'auto-coder mcp setup {server_name}' to install")
        return
    
    server_path = manager.get_server_path(server_name)
    if not server_path:
        logger.error(f"Failed to get server path for {server_name}")
        return
    
    # Try to run the server
    run_script = server_path / "run_server.sh"
    if run_script.exists():
        cmd = [str(run_script)]
    else:
        cmd = ["uv", "run", str(server_path / "main.py")]
    
    logger.info(f"Testing {server_name} MCP server...")
    logger.info(f"Command: {' '.join(cmd)}")
    logger.info("Press Ctrl+C to stop")
    
    try:
        result = subprocess.run(
            cmd,
            cwd=str(server_path),
            timeout=10,
        )
        if result.returncode == 0:
            logger.info(f"✅ {server_name} MCP server test successful")
        else:
            logger.error(f"❌ {server_name} MCP server test failed with code {result.returncode}")
    except subprocess.TimeoutExpired:
        logger.info(f"✅ {server_name} MCP server is running (stopped after 10s)")
    except KeyboardInterrupt:
        logger.info(f"✅ {server_name} MCP server test stopped by user")
    except Exception as e:
        logger.error(f"❌ Failed to test {server_name} MCP server: {e}")


def setup_mcp_programmatically(
    server_name: str,
    install_dir: Optional[str] = None,
    env_vars: Optional[dict] = None,
    backends: Optional[list] = None,
    silent: bool = False,
) -> bool:
    """Setup an MCP server programmatically.
    
    Args:
        server_name: Name of the MCP server
        install_dir: Installation directory (default: ~/mcp_servers/{server_name})
        env_vars: Environment variables to set
        backends: List of backends to configure (default: all)
        silent: Suppress user prompts
        
    Returns:
        True if setup was successful, False otherwise
    """
    manager = get_mcp_manager()
    
    # Check if server is registered
    config = manager.get_server_config(server_name)
    if not config:
        logger.error(f"Unknown MCP server: {server_name}")
        return False
    
    # Setup server
    install_path = Path(install_dir) if install_dir else None
    return manager.setup_server(
        server_name,
        install_dir=install_path,
        env_vars=env_vars,
        backends=backends,
        silent=silent,
    )

