"""
GraphRAG Docker Manager for Auto-Coder.

Manages Neo4j and Qdrant Docker containers for graphrag_mcp integration.
"""

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .logger_config import get_logger
from .utils import CommandExecutor, CommandResult

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
            # Default to docker-compose.graphrag.yml in repository root
            repo_root = Path(__file__).parent.parent.parent
            compose_file = str(repo_root / "docker-compose.graphrag.yml")

        self.compose_file = compose_file
        self.executor = CommandExecutor()
        self._docker_compose_cmd = self._detect_docker_compose_command()

    def _detect_docker_compose_command(self) -> list[str]:
        """Detect which docker compose command is available.

        Returns:
            List of command parts for docker compose (either ['docker', 'compose'] or ['docker-compose'])

        Raises:
            RuntimeError: If neither docker compose command is available
        """
        # Try 'docker compose' (newer Docker CLI plugin)
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

        # Try 'docker-compose' (legacy standalone)
        try:
            result = subprocess.run(
                ["docker-compose", "version"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.debug("Using 'docker-compose' command")
                return ["docker-compose"]
        except Exception:
            pass

        # Neither command is available
        error_msg = (
            "Neither 'docker compose' nor 'docker-compose' is available.\n"
            "Please install Docker Compose:\n"
            "  - For Docker Desktop: Docker Compose is included\n"
            "  - For Docker Engine: Install docker-compose-plugin\n"
            "    Ubuntu/Debian: sudo apt-get install docker-compose-plugin\n"
            "    Or install standalone: https://docs.docker.com/compose/install/"
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

    def _run_docker_compose(
        self, args: list[str], timeout: int = 60, retry_with_sudo: bool = True
    ) -> CommandResult:
        """Run docker-compose command.

        Args:
            args: Arguments to pass to docker-compose
            timeout: Command timeout in seconds
            retry_with_sudo: If True, retry with sudo on permission error

        Returns:
            CommandResult with execution results
        """
        # For 'docker compose', the -f flag must come after 'compose'
        # For 'docker-compose', the -f flag comes after 'docker-compose'
        # Both cases are handled the same way: cmd + ["-f", file] + args
        cmd = self._docker_compose_cmd + ["-f", self.compose_file] + args
        logger.debug(f"Running docker compose command: {' '.join(cmd)}")
        result = self.executor.run_command(cmd, timeout=timeout)

        # If permission error and retry is enabled, try with sudo
        if (
            not result.success
            and retry_with_sudo
            and self._is_permission_error(result.stderr)
        ):
            logger.warning(
                "Permission denied when accessing Docker. Retrying with sudo..."
            )
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

            logger.debug(
                f"Waiting for containers to be healthy... "
                f"(Neo4j: {neo4j_healthy}, Qdrant: {qdrant_healthy})"
            )
            time.sleep(check_interval)

        return False

    def _check_neo4j_health(self) -> bool:
        """Check if Neo4j is healthy.

        Returns:
            True if Neo4j is healthy, False otherwise
        """
        try:
            result = subprocess.run(
                ["curl", "-f", "http://localhost:7474"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Neo4j health check failed: {e}")
            return False

    def _check_qdrant_health(self) -> bool:
        """Check if Qdrant is healthy.

        Returns:
            True if Qdrant is healthy, False otherwise
        """
        try:
            result = subprocess.run(
                ["curl", "-f", "http://localhost:6333/healthz"],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Qdrant health check failed: {e}")
            return False

    def get_status(self) -> dict[str, bool]:
        """Get status of containers.

        Returns:
            Dictionary with container names as keys and running status as values
        """
        return {
            "neo4j": self._check_neo4j_health(),
            "qdrant": self._check_qdrant_health(),
        }

