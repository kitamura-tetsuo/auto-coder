"""GraphRAG-related CLI commands."""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import click

from .logger_config import get_logger, setup_logger

logger = get_logger(__name__)


def run_graphrag_setup_mcp_programmatically(
    install_dir: Optional[str] = None,
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "password"),
    qdrant_url: str = "http://localhost:6333",
    skip_clone: bool = False,
    silent: bool = False,
) -> bool:
    """Programmatically set up the GraphRAG MCP server.

    Args:
        install_dir: Installation directory (default: ~/graphrag_mcp)
        neo4j_uri: Neo4j connection URI
        neo4j_user: Neo4j username
        neo4j_password: Neo4j password
        qdrant_url: Qdrant connection URL
        skip_clone: Use existing directory (skip copy)
        backends: List of backends to configure (default: all)
        silent: If True, skip user confirmations and run automatically

    Returns:
        True if setup was successful, False otherwise
    """
    # Use new MCP manager for setup
    try:
        # Determine installation directory
        if install_dir is None:
            install_dir = str(Path.home() / "graphrag_mcp")

        install_path = Path(install_dir)

        if not silent:
            logger.info("Starting GraphRAG MCP server setup...")
            logger.info(f"Install path: {install_path}")

        # Check if uv is available, install if not found
        uv_available = False
        try:
            result = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                uv_available = True
                if not silent:
                    logger.info(f"✅ uv is available: {result.stdout.strip()}")
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            logger.error("uv command timed out")
            return False

        # Auto-install uv if not available
        if not uv_available:
            if not silent:
                logger.warning("uv not found. Attempting automatic installation...")

            import tempfile
            import urllib.request

            installer_path = None
            try:
                # Download uv installer securely
                url = "https://astral.sh/uv/install.sh"
                if not silent:
                    logger.info(f"Downloading uv installer from {url}...")

                with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp_file:
                    installer_path = tmp_file.name
                    with urllib.request.urlopen(url, timeout=30) as response:
                        shutil.copyfileobj(response, tmp_file)

                # Make executable
                os.chmod(installer_path, 0o700)

                # Run installer without shell=True
                if not silent:
                    logger.info("Running uv installer...")

                # Use explicit shell to be more robust across environments
                result = subprocess.run(
                    ["sh", installer_path],
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minutes timeout for installation
                )

                if result.returncode != 0:
                    logger.error("Failed to automatically install uv.")
                    logger.error(f"Error: {result.stderr}")
                    logger.error("Please install manually: https://docs.astral.sh/uv/")
                    return False

            except Exception as e:
                logger.error(f"Error occurred during uv installation: {e}")
                logger.error("Please install manually: https://docs.astral.sh/uv/")
                return False
            finally:
                # Cleanup temporary file
                if installer_path and os.path.exists(installer_path):
                    try:
                        os.unlink(installer_path)
                    except Exception as e:
                        logger.warning(f"Failed to remove temporary installer file: {e}")

            try:
                # Verify installation
                # Add common uv installation paths to PATH for this session
                uv_bin_paths = [
                    str(Path.home() / ".local" / "bin"),
                    str(Path.home() / ".cargo" / "bin"),
                ]
                current_path = os.environ.get("PATH", "")
                os.environ["PATH"] = ":".join(uv_bin_paths + [current_path])

                # Check if uv is now available
                try:
                    result = subprocess.run(
                        ["uv", "--version"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0:
                        if not silent:
                            logger.info(f"✅ Automatically installed uv: {result.stdout.strip()}")
                    else:
                        logger.error("uv installation completed, but it cannot be executed.")
                        logger.error("Please restart your shell and try again.")
                        return False
                except FileNotFoundError:
                    logger.error("uv was installed, but not found in PATH.")
                    logger.error("Please restart your shell and try again.")
                    logger.error(f"Alternatively, add the following paths to PATH: {':'.join(uv_bin_paths)}")
                    return False

            except subprocess.TimeoutExpired:
                logger.error("uv installation timed out")
                return False
            except Exception as e:
                logger.error(f"Error occurred during uv installation: {e}")
                logger.error("Please install manually: https://docs.astral.sh/uv/")
                return False

        # Copy bundled MCP server if needed
        if not skip_clone:
            if install_path.exists():
                if not silent:
                    logger.warning(f"Directory {install_path} already exists.")
                    logger.info("Using existing directory (--skip-clone)")
                skip_clone = True
            else:
                if not silent:
                    logger.info("Copying bundled MCP server...")
                try:
                    # Find the bundled MCP server in the package
                    import auto_coder

                    package_dir = Path(auto_coder.__file__).parent
                    bundled_mcp = package_dir / "mcp_servers" / "graphrag_mcp"

                    if not bundled_mcp.exists():
                        logger.error(f"Bundled MCP server not found: {bundled_mcp}")
                        logger.error("The package may not be installed correctly.")
                        return False

                    # Copy the bundled MCP server to install directory
                    shutil.copytree(
                        bundled_mcp,
                        install_path,
                        symlinks=False,
                        ignore_dangling_symlinks=True,
                    )

                    if not silent:
                        logger.info("✅ Copied MCP server")
                        logger.info(f"   Source: {bundled_mcp}")
                        logger.info(f"   Destination: {install_path}")
                except Exception as e:
                    logger.error(f"Failed to copy MCP server: {e}")
                    return False
        else:
            if not install_path.exists():
                logger.error(f"Directory {install_path} does not exist. When using --skip-clone, set up the MCP server beforehand.")
                return False
            if not silent:
                logger.info(f"Using existing directory: {install_path}")

        # Install dependencies with uv
        if not silent:
            logger.info("Installing dependencies...")
        try:
            result = subprocess.run(
                ["uv", "sync"],
                cwd=str(install_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.error(f"Failed to install dependencies:\n{result.stderr}")
                return False
            if not silent:
                logger.info("✅ Installed dependencies")
        except subprocess.TimeoutExpired:
            logger.error("uv sync timed out")
            return False

        # Create .env file
        env_path = install_path / ".env"
        env_content = f"""# GraphRAG MCP Server Configuration
# Auto-generated by auto-coder graphrag setup-mcp

# Neo4j Configuration
NEO4J_URI={neo4j_uri}
NEO4J_USER={neo4j_user}
NEO4J_PASSWORD={neo4j_password}

# Qdrant Configuration
QDRANT_URL={qdrant_url}

# Optional: OpenAI API Key for embeddings (if using OpenAI)
# OPENAI_API_KEY=your-api-key-here
"""

        try:
            # Use os.open to ensure file is created with 600 permissions
            fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(env_content)

            if not silent:
                logger.info(f"✅ Created .env file: {env_path}")
        except Exception as e:
            logger.error(f"Failed to create .env file: {e}")
            return False

        # Create run_server.sh script
        run_script_path = install_path / "run_server.sh"

        # Find uv executable path for the script
        uv_path = shutil.which("uv")
        if not uv_path:
            # Try common locations
            common_paths = [
                Path.home() / ".local" / "bin" / "uv",
                Path.home() / ".cargo" / "bin" / "uv",
                Path("/usr/local/bin/uv"),
            ]
            for path in common_paths:
                if path.exists():
                    uv_path = str(path)
                    break

        if not uv_path:
            logger.error("uv executable not found in PATH or common locations")
            return False

        # Create a robust run_server.sh that can find uv even in pipx environments
        run_script_content = f"""#!/bin/bash
# GraphRAG MCP Server startup script
# This script ensures the .env file is loaded from the correct directory

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

# Change to the script directory to ensure .env is loaded correctly
cd "$SCRIPT_DIR"

# Clear VIRTUAL_ENV to avoid conflicts with other projects
unset VIRTUAL_ENV

# Add common uv installation paths to PATH
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"

# Try to find uv executable
UV_CMD=""

# First, try the path we found during setup
if [ -x "{uv_path}" ]; then
    UV_CMD="{uv_path}"
# Then try common locations
elif command -v uv >/dev/null 2>&1; then
    UV_CMD="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_CMD="$HOME/.local/bin/uv"
elif [ -x "$HOME/.cargo/bin/uv" ]; then
    UV_CMD="$HOME/.cargo/bin/uv"
elif [ -x "/usr/local/bin/uv" ]; then
    UV_CMD="/usr/local/bin/uv"
else
    echo "Error: uv executable not found" >&2
    echo "Please install uv: https://docs.astral.sh/uv/" >&2
    exit 1
fi

# Run the MCP server with uv
exec "$UV_CMD" run main.py
"""

        try:
            with open(run_script_path, "w", encoding="utf-8") as f:
                f.write(run_script_content)
            # Make the script executable
            os.chmod(run_script_path, 0o755)
            if not silent:
                logger.info(f"✅ Created run_server.sh script: {run_script_path}")
        except Exception as e:
            logger.error(f"Failed to create run_server.sh script: {e}")
            return False

        # Patch main.py to load .env from script directory
        main_py_path = install_path / "main.py"
        main_py_content = """import os
from pathlib import Path
from dotenv import load_dotenv

def main():
    # Get the directory where this script is located
    script_dir = Path(__file__).parent.resolve()
    env_path = script_dir / ".env"

    # Load environment variables from .env file in script directory
    load_dotenv(env_path)

    # Import here to avoid circular imports
    from server import mcp

    # Run the MCP server directly
    mcp.run()

if __name__ == "__main__":
    main()
"""

        try:
            with open(main_py_path, "w", encoding="utf-8") as f:
                f.write(main_py_content)
            if not silent:
                logger.info("✅ Modified main.py (explicitly specified .env path)")
        except Exception as e:
            logger.error(f"Failed to modify main.py: {e}")
            return False

        if not silent:
            logger.info("=" * 60)
            logger.info("✅ GraphRAG MCP server setup completed!")
            logger.info("=" * 60)

        # Automatically configure all backends
        backends_to_configure = ["codex", "gemini", "qwen", "windsurf"]

        if not silent:
            logger.info("Automatically updating configuration files for each backend...")

        success_count = 0
        total_count = len(backends_to_configure)

        # Configure each backend
        for backend in backends_to_configure:
            if backend == "codex":
                if _add_codex_config(install_path):
                    success_count += 1
            elif backend == "gemini":
                if _add_gemini_config(install_path):
                    success_count += 1
            elif backend == "qwen":
                if _add_qwen_config(install_path):
                    success_count += 1
            elif backend == "windsurf":
                if _add_windsurf_claude_config(install_path):
                    success_count += 1

        if not silent:
            logger.info(f"Configuration complete: {success_count}/{total_count} backends")
            logger.info("")
            logger.info("Next steps:")
            logger.info("1. Start Neo4j and Qdrant:")
            logger.info("   auto-coder graphrag start")
            logger.info("")
            logger.info("2. Process code using GraphRAG:")
            logger.info("   auto-coder process-issues --repo owner/repo")

        return success_count > 0

    except Exception as e:
        logger.error(f"An error occurred during setup: {e}")
        return False


def _add_codex_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Codex CLI config using CodexClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .codex_client import CodexClient

        client = CodexClient()
        result = client.add_mcp_server_config("graphrag", "uv", ["run", str(install_path / "main.py")])

        if result:
            logger.info("✅ Updated Codex configuration")
        else:
            logger.error("Failed to update Codex configuration")

        return result
    except Exception as e:
        logger.error(f"Failed to add Codex config: {e}")
        return False


def _add_gemini_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Gemini CLI config using GeminiClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .gemini_client import GeminiClient

        # GeminiClient requires API key, but we only need add_mcp_server_config
        # which uses CLI commands, so we can pass None
        client = GeminiClient()

        # Use uv with --directory option to ensure correct working directory
        result = client.add_mcp_server_config("graphrag", "uv", ["--directory", str(install_path), "run", "main.py"])

        if result:
            logger.info("✅ Updated Gemini configuration")
        else:
            logger.error("Failed to update Gemini configuration")

        return result
    except Exception as e:
        logger.error(f"Failed to add Gemini config: {e}")
        return False


def _add_qwen_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Qwen CLI config using QwenClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .qwen_client import QwenClient

        client = QwenClient()

        # Use run_server.sh if it exists (for compatibility and to avoid VIRTUAL_ENV issues)
        # Qwen supports shell scripts directly
        run_script = install_path / "run_server.sh"
        if run_script.exists():
            result = client.add_mcp_server_config("graphrag", str(run_script), [])
        else:
            # Fallback to uv with --directory option
            result = client.add_mcp_server_config("graphrag", "uv", ["--directory", str(install_path), "run", "main.py"])

        if result:
            logger.info("✅ Updated Qwen configuration")
        else:
            logger.error("Failed to update Qwen configuration")

        return result
    except Exception as e:
        logger.error(f"Failed to add Qwen config: {e}")
        return False


def _add_windsurf_claude_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Windsurf/Claude config using AuggieClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .auggie_client import AuggieClient

        client = AuggieClient()

        # Use run_server.sh if it exists (for compatibility)
        # Windsurf/Claude supports shell scripts directly
        run_script = install_path / "run_server.sh"
        if run_script.exists():
            result = client.add_mcp_server_config("graphrag", str(run_script), [])
        else:
            # Fallback to uv with --directory option
            result = client.add_mcp_server_config("graphrag", "uv", ["--directory", str(install_path), "run", "main.py"])

        if result:
            logger.info("✅ Updated Windsurf/Claude configuration")
        else:
            logger.error("Failed to update Windsurf/Claude configuration")

        return result
    except Exception as e:
        logger.error(f"Failed to add Windsurf/Claude config: {e}")
        return False


@click.group(name="graphrag")
def graphrag_group() -> None:
    """GraphRAG (Neo4j + Qdrant) management commands.

    - start: Start Docker containers
    - stop: Stop Docker containers
    - status: Show container status
    - update-index: Update the codebase index
    - cleanup: Apply snapshot retention policy and remove stale data
    - setup-mcp: Automatically set up the GraphRAG MCP server
    """
    pass


@graphrag_group.command("start")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="Wait until containers become healthy",
)
@click.option(
    "--timeout",
    type=int,
    default=120,
    help="Health check timeout (seconds)",
)
def graphrag_start(wait: bool, timeout: int) -> None:
    """Start Neo4j and Qdrant Docker containers."""
    from .graphrag_docker_manager import GraphRAGDockerManager

    setup_logger()

    click.echo("Starting GraphRAG Docker containers (Neo4j and Qdrant)...")

    try:
        manager = GraphRAGDockerManager()
    except RuntimeError as e:
        click.echo()
        click.echo(f"❌ {e}")
        raise click.ClickException("Docker Compose is not available")

    try:
        success = manager.start(wait_for_health=wait, timeout=timeout)
        if success:
            click.echo("✅ GraphRAG containers started successfully")
            if wait:
                status = manager.get_status()
                click.echo(f"   Neo4j: {'✅ healthy' if status['neo4j'] else '❌ unhealthy'}")
                click.echo(f"   Qdrant: {'✅ healthy' if status['qdrant'] else '❌ unhealthy'}")

                # Check if any container is unhealthy
                if not status["neo4j"] or not status["qdrant"]:
                    click.echo()
                    click.echo("⚠️  Some containers are unhealthy. Troubleshooting tips:")
                    click.echo("   1. Check Docker logs: docker compose -f docker-compose.graphrag.yml logs")
                    click.echo("   2. Verify ports are not in use: lsof -i :7474 -i :7687 -i :6333")
                    click.echo("   3. Try restarting: auto-coder graphrag stop && auto-coder graphrag start")
        else:
            click.echo()
            click.echo("❌ Failed to start GraphRAG containers")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Ensure Docker is running: docker ps")
            click.echo("   2. Check Docker permissions: sudo usermod -aG docker $USER")
            click.echo("      (then logout and login again)")
            click.echo("   3. Check docker-compose.graphrag.yml exists in repository root")
            click.echo("   4. Check Docker logs: docker compose -f docker-compose.graphrag.yml logs")
            click.echo("   5. Try manual start: docker compose -f docker-compose.graphrag.yml up -d")
            raise click.ClickException("Failed to start GraphRAG containers")
    except click.ClickException:
        raise
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error starting containers: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Ensure Docker is installed and running")
        click.echo("   2. Check if docker compose is available: docker compose version")
        click.echo("   3. Check Docker permissions: sudo usermod -aG docker $USER")
        click.echo("      (then logout and login again)")
        click.echo("   4. Verify docker-compose.graphrag.yml exists")
        raise click.ClickException(f"Error starting containers: {e}")


@graphrag_group.command("stop")
@click.option(
    "--timeout",
    type=int,
    default=60,
    help="Command timeout (seconds)",
)
def graphrag_stop(timeout: int) -> None:
    """Stop Neo4j and Qdrant Docker containers."""
    from .graphrag_docker_manager import GraphRAGDockerManager

    setup_logger()
    manager = GraphRAGDockerManager()

    click.echo("Stopping GraphRAG Docker containers...")

    try:
        success = manager.stop(timeout=timeout)
        if success:
            click.echo("✅ GraphRAG containers stopped successfully")
        else:
            raise click.ClickException("Failed to stop GraphRAG containers")
    except Exception as e:
        raise click.ClickException(f"Error stopping containers: {e}")


@graphrag_group.command("status")
def graphrag_status() -> None:
    """Show status of Neo4j and Qdrant Docker containers."""
    from .graphrag_docker_manager import GraphRAGDockerManager

    setup_logger()
    manager = GraphRAGDockerManager()

    click.echo("Checking GraphRAG Docker containers status...")
    click.echo()

    try:
        is_running = manager.is_running()
        if is_running:
            click.echo("📦 Containers: ✅ Running")
            status = manager.get_status()
            click.echo(f"   Neo4j: {'✅ healthy' if status['neo4j'] else '❌ unhealthy'}")
            click.echo(f"   Qdrant: {'✅ healthy' if status['qdrant'] else '❌ unhealthy'}")
        else:
            click.echo("📦 Containers: ❌ Not running")
            click.echo("   Run 'auto-coder graphrag start' to start containers")
    except Exception as e:
        raise click.ClickException(f"Error checking status: {e}")


@graphrag_group.command("update-index")
@click.option(
    "--force",
    is_flag=True,
    help="Force update even if index is up to date",
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Repository path to index (default: current directory)",
)
def graphrag_update_index(force: bool, repo_path: Optional[str]) -> None:
    """Update codebase index in Neo4j and Qdrant."""
    from .graphrag_docker_manager import GraphRAGDockerManager
    from .graphrag_index_manager import GraphRAGIndexManager

    setup_logger()

    # Ensure Docker containers are running
    docker_manager = GraphRAGDockerManager()
    if not docker_manager.is_running():
        click.echo("⚠️  GraphRAG containers are not running")
        if click.confirm("Start containers now?"):
            click.echo("Starting containers...")
            if not docker_manager.start(wait_for_health=True):
                raise click.ClickException("Failed to start containers")
            click.echo("✅ Containers started")
        else:
            raise click.ClickException("Containers must be running to update index. Run 'auto-coder graphrag start' first.")

    # Update index
    try:
        index_manager = GraphRAGIndexManager(repo_path=repo_path)
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error initializing index manager: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Verify the repository path exists and is accessible")
        click.echo("   2. Check file permissions in the repository")
        raise click.ClickException(f"Error initializing index manager: {e}")

    # Check if indexed path matches current path
    try:
        path_matches, indexed_path = index_manager.check_indexed_path()
        if indexed_path is not None and not path_matches:
            click.echo()
            click.echo("⚠️  Indexed directory differs:")
            click.echo(f"   Indexed: {indexed_path}")
            click.echo(f"   Current directory: {index_manager.repo_path.resolve()}")
            click.echo()
            if not force and not click.confirm("Update index for the current directory?"):
                click.echo("Canceled index update")
                return
            force = True  # Force update when path changes
    except Exception as e:
        click.echo(f"⚠️  Warning: Could not check indexed path: {e}")

    if not force:
        try:
            if index_manager.is_index_up_to_date():
                click.echo("✅ Index is already up to date")
                click.echo("   Use --force to update anyway")
                return
        except Exception as e:
            click.echo(f"⚠️  Warning: Could not check index status: {e}")
            click.echo("   Proceeding with update...")

    try:
        from .cli_ui import Spinner

        success = False
        try:
            with Spinner("Updating GraphRAG index...", show_timer=True) as spinner:
                success = index_manager.update_index(force=force)
                if success:
                    spinner.success_message = "Index updated successfully"
                else:
                    spinner.error_message = "Failed to update index"
                    # Raise an exception to trigger the spinner's error state (red cross)
                    raise RuntimeError("Update failed")
        except RuntimeError:
            pass

        if success:
            click.echo()
            click.echo("Note: Current implementation uses hash-based change detection.")
            click.echo("      Full semantic indexing (embeddings, Neo4j/Qdrant storage)")
            click.echo("      is planned for future enhancement.")
        else:
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Check if containers are healthy: auto-coder graphrag status")
            click.echo("   2. Verify repository contains Python files")
            click.echo("   3. Check logs for detailed error messages")
            raise click.ClickException("Failed to update index")
    except click.ClickException:
        raise
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error updating index: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Check if containers are running and healthy")
        click.echo("   2. Verify file permissions in repository")
        click.echo("   3. Check available disk space")
        raise click.ClickException(f"Error updating index: {e}")


@graphrag_group.command("cleanup")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be deleted without performing cleanup",
)
@click.option(
    "--retention-days",
    type=int,
    default=None,
    help="Override GRAPHRAG_RETENTION_DAYS (default: 7)",
)
@click.option(
    "--max-per-repo",
    type=int,
    default=None,
    help="Override GRAPHRAG_MAX_SNAPSHOTS_PER_REPO (default: 9)",
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="Repository path whose GraphRAG snapshots to clean (default: current directory)",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose logging for cleanup details",
)
def graphrag_cleanup(
    dry_run: bool,
    retention_days: Optional[int],
    max_per_repo: Optional[int],
    repo_path: Optional[str],
    verbose: bool,
) -> None:
    """Run GraphRAG snapshot cleanup for the given repository."""

    from .graphrag_index_manager import GraphRAGIndexManager

    log_level = "DEBUG" if verbose else None
    setup_logger(log_level=log_level)

    click.echo("Running GraphRAG snapshot cleanup...")

    try:
        index_manager = GraphRAGIndexManager(repo_path=repo_path)
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error initializing index manager for cleanup: {e}")
        raise click.ClickException(f"Error initializing index manager for cleanup: {e}")

    try:
        result = index_manager.cleanup_snapshots(
            dry_run=dry_run,
            retention_days=retention_days,
            max_snapshots_per_repo=max_per_repo,
        )
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error during GraphRAG cleanup: {e}")
        raise click.ClickException(f"Error during GraphRAG cleanup: {e}")

    deleted_count = len(result.deleted)
    if result.dry_run:
        click.echo(f"✅ Dry-run complete: would delete {deleted_count} snapshot(s); " f"{result.total_snapshots_before} snapshot(s) currently recorded.")
    else:
        click.echo(f"✅ Cleanup complete: deleted {deleted_count} snapshot(s); " f"{result.total_snapshots_after} snapshot(s) remain.")


@graphrag_group.command("setup-mcp")
@click.option(
    "--install-dir",
    type=click.Path(file_okay=False, dir_okay=True, resolve_path=True),
    default=None,
    help="Installation directory for GraphRAG MCP server (default: ~/graphrag_mcp)",
)
@click.option(
    "--neo4j-uri",
    default="bolt://localhost:7687",
    help="Neo4j connection URI (default: bolt://localhost:7687)",
)
@click.option(
    "--neo4j-user",
    default="neo4j",
    help="Neo4j username (default: neo4j)",
)
@click.option(
    "--neo4j-password",
    default=lambda: os.environ.get("NEO4J_PASSWORD", "password"),
    show_default="env: NEO4J_PASSWORD or 'password'",
    help="Neo4j password",
)
@click.option(
    "--qdrant-url",
    default="http://localhost:6333",
    help="Qdrant connection URL (default: http://localhost:6333)",
)
@click.option(
    "--skip-clone",
    is_flag=True,
    help="Use existing directory (skip clone)",
)
def graphrag_setup_mcp(
    install_dir: Optional[str],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    qdrant_url: str,
    skip_clone: bool,
) -> None:
    """Automatically set up the GraphRAG MCP server.

    This command performs the following:
    1. Copy the bundled custom MCP server (a code-analysis fork)
    2. Install dependencies using uv
    3. Create a .env file and configure connection settings
    4. Automatically update backend configs (Codex, Gemini, Qwen, Windsurf/Claude)

    Note: This MCP server is a custom fork of rileylemm/graphrag_mcp,
    specialized for TypeScript/JavaScript code analysis.
    """
    setup_logger()

    # Handle interactive confirmation for existing directory
    if install_dir is None:
        install_dir = str(Path.home() / "graphrag_mcp")

    install_path = Path(install_dir)

    # Interactive confirmation if directory exists and not skip_clone
    if not skip_clone and install_path.exists():
        if not click.confirm(f"Directory {install_path} already exists. Delete and re-copy?"):
            click.echo("Setup cancelled")
            return

        # Remove existing directory
        try:
            shutil.rmtree(install_path)
            click.echo(f"Removed existing directory: {install_path}")
        except Exception as e:
            raise click.ClickException(f"Failed to delete directory: {e}")

    # Call the programmatic setup function
    success = run_graphrag_setup_mcp_programmatically(
        install_dir=install_dir,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
        qdrant_url=qdrant_url,
        skip_clone=skip_clone,
        silent=False,
    )

    if not success:
        raise click.ClickException("Setup failed")
