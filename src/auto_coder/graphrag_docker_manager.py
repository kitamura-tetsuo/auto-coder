"""
GraphRAG Docker Manager for Auto-Coder.

Manages Neo4j and Qdrant Docker containers for graphrag_mcp integration.
"""

import os
import subprocess
import tempfile
import time
from importlib import resources
from pathlib import Path
from typing import Optional

from .logger_config import get_logger
from .utils import CommandExecutor, CommandResult, is_running_in_container

logger = get_logger(__name__)


class GraphRAGDockerManager:
    """Manages Neo4j and Qdrant Docker containers for graphrag_mcp."""

    def __init__(self, compose_file: Optional[str] = None):
        """Initialize GraphRAG Docker Manager.

        Args:
            compose_file: Path to docker-compose file. If None, uses default location.

        Raises:
            RuntimeError: If docker compose command is not available
        """
        if compose_file is None:
            # Extract docker-compose.graphrag.yml from package resources
            compose_file = self._get_compose_file_from_package()

        self.compose_file = compose_file
        self.executor = CommandExecutor()
        self._docker_compose_cmd = self._detect_docker_compose_command()

    def _get_compose_file_from_package(self) -> str:
        """Get docker-compose.graphrag.yml from package resources.

        Returns:
            Path to docker-compose.graphrag.yml file

        Raises:
            FileNotFoundError: If docker-compose.graphrag.yml is not found in package
        """
        try:
            # Get compose file from package resources
            package_files = resources.files("auto_coder")
            compose_resource = package_files / "docker-compose.graphrag.yml"

            # Read the content and write to a temporary file
            compose_content = compose_resource.read_text()

            # Create a temporary file that persists
            temp_dir = Path(tempfile.gettempdir()) / "auto-coder"
            temp_dir.mkdir(exist_ok=True)
            compose_file = temp_dir / "docker-compose.graphrag.yml"
            compose_file.write_text(compose_content)

            logger.debug(f"Extracted docker-compose.graphrag.yml to {compose_file}")
            return str(compose_file)
        except Exception as e:
            logger.error(f"Failed to extract docker-compose.graphrag.yml from package: {e}")
            raise FileNotFoundError("docker-compose.graphrag.yml not found in package. " "Please ensure the package is installed correctly.") from e

    def _detect_docker_compose_command(self) -> list[str]:
        """Detect which docker compose command is available.

        Returns:
            List of command parts for docker compose (['docker', 'compose'])

        Raises:
            RuntimeError: If docker compose command is not available
        """
        # Try 'docker compose' command
        try:
            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug("Using 'docker compose' command")
                return ["docker", "compose"]
        except Exception:
            pass

        # Docker compose is not available
        error_msg = (
            "Docker Compose is not available.\n"
            "Please install Docker Compose:\n"
            "  - For Docker Desktop: Docker Compose is included\n"
            "  - For Docker Engine: Install docker-compose-plugin\n"
            "    Ubuntu/Debian: sudo apt-get install docker-compose-plugin"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    def _is_permission_error(self, stderr: str) -> bool:
        """Check if error message indicates a permission error.

        Args:
            stderr: Error message from command

        Returns:
            True if error is a permission error
        """
        permission_indicators = [
            "permission denied",
            "dial unix /var/run/docker.sock: connect: permission denied",
            "Got permission denied while trying to connect to the Docker daemon socket",
        ]
        stderr_lower = stderr.lower()
        return any(indicator in stderr_lower for indicator in permission_indicators)

    def _run_docker_compose(self, args: list[str], timeout: int = 60, retry_with_sudo: bool = True) -> CommandResult:
        """Run docker compose command.

        Args:
            args: Arguments to pass to docker compose
            timeout: Command timeout in seconds
            retry_with_sudo: If True, retry with sudo on permission error

        Returns:
            CommandResult with execution results
        """
        # The -f flag must come after 'compose'
        cmd = self._docker_compose_cmd + ["-f", self.compose_file] + args
        logger.debug(f"Running docker compose command: {' '.join(cmd)}")
        result = self.executor.run_command(cmd, timeout=timeout)

        # If permission error and retry is enabled, try with sudo
        if not result.success and retry_with_sudo and self._is_permission_error(result.stderr):
            logger.warning("Permission denied when accessing Docker. Retrying with sudo...")
            sudo_cmd = ["sudo"] + cmd
            logger.debug(f"Running docker compose command with sudo: {' '.join(sudo_cmd)}")
            result = self.executor.run_command(sudo_cmd, timeout=timeout)

        return result

    def start(self, wait_for_health: bool = True, timeout: int = 120) -> bool:
        """Start Neo4j and Qdrant containers.

        Args:
            wait_for_health: Wait for containers to be healthy
            timeout: Maximum time to wait for containers to be healthy

        Returns:
            True if containers started successfully, False otherwise
        """
        logger.info("Starting GraphRAG Docker containers (Neo4j and Qdrant)...")

        # Start containers
        result = self._run_docker_compose(["up", "-d"], timeout=timeout)
        if not result.success:
            logger.error(f"Failed to start containers: {result.stderr}")
            return False

        logger.info("Containers started successfully")

        # Connect current container to GraphRAG network if running in container
        self._connect_to_graphrag_network()

        if wait_for_health:
            logger.info("Waiting for containers to be healthy...")
            if not self.wait_for_health(timeout=timeout):
                logger.error("Containers failed to become healthy")
                return False
            logger.info("All containers are healthy")

        return True

    def stop(self, timeout: int = 60) -> bool:
        """Stop Neo4j and Qdrant containers.

        Args:
            timeout: Command timeout in seconds

        Returns:
            True if containers stopped successfully, False otherwise
        """
        logger.info("Stopping GraphRAG Docker containers...")

        result = self._run_docker_compose(["down"], timeout=timeout)
        if not result.success:
            logger.error(f"Failed to stop containers: {result.stderr}")
            return False

        logger.info("Containers stopped successfully")
        return True

    def restart(self, timeout: int = 120) -> bool:
        """Restart Neo4j and Qdrant containers.

        Args:
            timeout: Command timeout in seconds

        Returns:
            True if containers restarted successfully, False otherwise
        """
        logger.info("Restarting GraphRAG Docker containers...")

        if not self.stop(timeout=timeout // 2):
            return False

        return self.start(wait_for_health=True, timeout=timeout // 2)

    def is_running(self) -> bool:
        """Check if containers are running.

        Returns:
            True if both containers are running, False otherwise
        """
        result = self._run_docker_compose(["ps", "-q"], timeout=10)
        if not result.success:
            return False

        # Check if we have output (container IDs)
        container_ids = result.stdout.strip().split("\n")
        running_containers = [cid for cid in container_ids if cid]

        # We expect 2 containers (neo4j and qdrant)
        return len(running_containers) == 2

    def wait_for_health(self, timeout: int = 120, check_interval: int = 5) -> bool:
        """Wait for containers to be healthy.

        Args:
            timeout: Maximum time to wait in seconds
            check_interval: Time between health checks in seconds

        Returns:
            True if containers are healthy, False if timeout reached
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Check Neo4j health
            neo4j_healthy = self._check_neo4j_health()

            # Check Qdrant health
            qdrant_healthy = self._check_qdrant_health()

            if neo4j_healthy and qdrant_healthy:
                return True

            logger.debug(f"Waiting for containers to be healthy... " f"(Neo4j: {neo4j_healthy}, Qdrant: {qdrant_healthy})")
            time.sleep(check_interval)

        return False

    def _check_neo4j_health(self) -> bool:
        """Check if Neo4j is healthy.

        Returns:
            True if Neo4j is healthy, False otherwise
        """
        try:
            # Use docker inspect to check health status
            cmd = [
                "docker",
                "inspect",
                "--format={{.State.Health.Status}}",
                "auto-coder-neo4j",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                # Try with sudo if permission denied
                cmd = ["sudo"] + cmd
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                )

            if result.returncode == 0:
                status = result.stdout.decode().strip()
                return status == "healthy"
            return False
        except Exception as e:
            logger.debug(f"Neo4j health check failed: {e}")
            return False

    def _check_qdrant_health(self) -> bool:
        """Check if Qdrant is healthy.

        Returns:
            True if Qdrant is healthy, False otherwise
        """
        try:
            # Use docker inspect to check health status
            cmd = [
                "docker",
                "inspect",
                "--format={{.State.Health.Status}}",
                "auto-coder-qdrant",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                # Try with sudo if permission denied
                cmd = ["sudo"] + cmd
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=5,
                )

            if result.returncode == 0:
                status = result.stdout.decode().strip()
                return status == "healthy"
            return False
        except Exception as e:
            logger.debug(f"Qdrant health check failed: {e}")
            return False

    def _get_current_container_id(self) -> Optional[str]:
        """Get current container ID if running in a container.

        Returns:
            Container ID if running in container, None otherwise
        """
        # Check if running in container using robust detection
        if not is_running_in_container():
            return None

        try:
            # Try to get container ID from hostname
            with open("/etc/hostname", "r") as f:
                container_id = f.read().strip()
                if container_id:
                    return container_id
        except Exception as e:
            logger.debug(f"Failed to get container ID from hostname: {e}")

        return None

    def _get_graphrag_network_name(self) -> Optional[str]:
        """Get GraphRAG network name from docker-compose file.

        Returns:
            Network name if found, None otherwise
        """
        try:
            # The network name is typically prefixed with the project name
            # For docker-compose, it's usually <directory>_<network_name>
            # We'll use docker network ls to find it
            cmd = [
                "docker",
                "network",
                "ls",
                "--format",
                "{{.Name}}",
                "--filter",
                "name=graphrag",
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=5)

            if result.returncode != 0:
                # Try with sudo
                cmd = ["sudo"] + cmd
                result = subprocess.run(cmd, capture_output=True, timeout=5)

            if result.returncode == 0:
                networks = result.stdout.decode().strip().split("\n")
                # Find network containing 'graphrag'
                for network in networks:
                    if "graphrag" in network.lower():
                        return network
        except Exception as e:
            logger.debug(f"Failed to get GraphRAG network name: {e}")

        return None

    def _connect_to_graphrag_network(self) -> None:
        """Connect current container to GraphRAG network if running in container."""
        container_id = self._get_current_container_id()
        if not container_id:
            logger.debug("Not running in container, skipping network connection")
            return

        network_name = self._get_graphrag_network_name()
        if not network_name:
            logger.warning("Could not find GraphRAG network")
            return

        try:
            # Get container name
            cmd = ["docker", "inspect", container_id, "--format", "{{.Name}}"]
            result = subprocess.run(cmd, capture_output=True, timeout=5)

            if result.returncode != 0:
                # Try with sudo
                cmd = ["sudo"] + cmd
                result = subprocess.run(cmd, capture_output=True, timeout=5)

            container_name = result.stdout.decode().strip().lstrip("/") if result.returncode == 0 else None

            # Check if already connected
            cmd = [
                "docker",
                "network",
                "inspect",
                network_name,
                "--format",
                "{{range .Containers}}{{.Name}} {{end}}",
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=5)

            if result.returncode != 0:
                # Try with sudo
                cmd = ["sudo"] + cmd
                result = subprocess.run(cmd, capture_output=True, timeout=5)

            if result.returncode == 0:
                connected_containers = result.stdout.decode().strip()
                # Check both container ID and name
                if container_id in connected_containers or (container_name and container_name in connected_containers):
                    logger.debug(f"Container {container_id} ({container_name}) already connected to {network_name}")
                    return

            # Connect to network
            cmd = ["docker", "network", "connect", network_name, container_id]
            result = subprocess.run(cmd, capture_output=True, timeout=10)

            if result.returncode != 0:
                # Try with sudo
                cmd = ["sudo"] + cmd
                result = subprocess.run(cmd, capture_output=True, timeout=10)

            if result.returncode == 0:
                logger.info(f"Connected container {container_id} ({container_name}) to GraphRAG network {network_name}")
            else:
                stderr = result.stderr.decode()
                # Ignore "already exists in network" error
                if "already exists in network" in stderr:
                    logger.debug(f"Container {container_id} ({container_name}) already connected to {network_name}")
                else:
                    logger.warning(f"Failed to connect to GraphRAG network: {stderr}")

        except Exception as e:
            logger.warning(f"Failed to connect to GraphRAG network: {e}")

    def get_status(self) -> dict[str, bool]:
        """Get status of containers.

        Returns:
            Dictionary with container names as keys and running status as values
        """
        return {
            "neo4j": self._check_neo4j_health(),
            "qdrant": self._check_qdrant_health(),
        }
