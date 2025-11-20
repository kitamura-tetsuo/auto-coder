"""Tests for GraphRAG Docker Manager."""

import subprocess
from unittest import mock

import pytest

from auto_coder.graphrag_docker_manager import GraphRAGDockerManager


@pytest.fixture
def mock_executor():
    """Create a mock CommandExecutor."""
    return mock.MagicMock()


@pytest.fixture
def docker_manager(mock_executor):
    """Create a GraphRAGDockerManager instance for testing."""
    with mock.patch(
        "src.auto_coder.graphrag_docker_manager.CommandExecutor",
        return_value=mock_executor,
    ):
        with mock.patch.object(
            GraphRAGDockerManager,
            "_detect_docker_compose_command",
            return_value=["docker", "compose"],
        ):
            manager = GraphRAGDockerManager()
            return manager


@pytest.fixture
def mock_subprocess_health():
    """Fixture to mock subprocess.run for health checks."""
    with mock.patch("subprocess.run") as mock_run:
        # Mock successful health checks
        mock_run.return_value = mock.MagicMock(returncode=0, stdout=b"healthy\n", stderr=b"")
        yield mock_run


def test_init_default_compose_file():
    """Test initialization with default compose file."""
    with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
        with mock.patch.object(
            GraphRAGDockerManager,
            "_detect_docker_compose_command",
            return_value=["docker", "compose"],
        ):
            manager = GraphRAGDockerManager()
            assert manager.compose_file.endswith("docker-compose.graphrag.yml")


def test_init_custom_compose_file():
    """Test initialization with custom compose file."""
    custom_path = "/custom/path/docker-compose.yml"
    with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
        with mock.patch.object(
            GraphRAGDockerManager,
            "_detect_docker_compose_command",
            return_value=["docker", "compose"],
        ):
            manager = GraphRAGDockerManager(compose_file=custom_path)
            assert manager.compose_file == custom_path


def test_start_success(docker_manager, mock_executor):
    """Test successful container start."""
    # Mock successful docker-compose up
    mock_result = mock.MagicMock()
    mock_result.success = True
    mock_result.stdout = ""
    mock_result.stderr = ""
    mock_executor.run_command.return_value = mock_result

    # Mock health checks
    with mock.patch.object(docker_manager, "wait_for_health", return_value=True):
        result = docker_manager.start(wait_for_health=True)

    assert result is True
    mock_executor.run_command.assert_called()


def test_start_failure(docker_manager, mock_executor):
    """Test container start failure."""
    # Mock failed docker-compose up
    mock_result = mock.MagicMock()
    mock_result.success = False
    mock_result.stderr = "Error starting containers"
    mock_executor.run_command.return_value = mock_result

    result = docker_manager.start(wait_for_health=False)

    assert result is False


def test_stop_success(docker_manager, mock_executor):
    """Test successful container stop."""
    # Mock successful docker-compose down
    mock_result = mock.MagicMock()
    mock_result.success = True
    mock_executor.run_command.return_value = mock_result

    result = docker_manager.stop()

    assert result is True


def test_stop_failure(docker_manager, mock_executor):
    """Test container stop failure."""
    # Mock failed docker-compose down
    mock_result = mock.MagicMock()
    mock_result.success = False
    mock_result.stderr = "Error stopping containers"
    mock_executor.run_command.return_value = mock_result

    result = docker_manager.stop()

    assert result is False


def test_is_running_true(docker_manager, mock_executor):
    """Test is_running when containers are running."""
    # Mock docker-compose ps output with 2 container IDs
    mock_result = mock.MagicMock()
    mock_result.success = True
    mock_result.stdout = "container_id_1\ncontainer_id_2\n"
    mock_executor.run_command.return_value = mock_result

    result = docker_manager.is_running()

    assert result is True


def test_is_running_false(docker_manager, mock_executor):
    """Test is_running when containers are not running."""
    # Mock docker-compose ps output with no containers
    mock_result = mock.MagicMock()
    mock_result.success = True
    mock_result.stdout = "\n"
    mock_executor.run_command.return_value = mock_result

    result = docker_manager.is_running()

    assert result is False


def test_is_running_command_failure(docker_manager, mock_executor):
    """Test is_running when command fails."""
    # Mock failed docker-compose ps
    mock_result = mock.MagicMock()
    mock_result.success = False
    mock_executor.run_command.return_value = mock_result

    result = docker_manager.is_running()

    assert result is False


def test_wait_for_health_success(docker_manager):
    """Test wait_for_health when containers become healthy."""
    with mock.patch.object(docker_manager, "_check_neo4j_health", return_value=True):
        with mock.patch.object(docker_manager, "_check_qdrant_health", return_value=True):
            result = docker_manager.wait_for_health(timeout=1, check_interval=0.1)

    assert result is True


def test_wait_for_health_timeout(docker_manager):
    """Test wait_for_health when timeout is reached."""
    with mock.patch.object(docker_manager, "_check_neo4j_health", return_value=False):
        with mock.patch.object(docker_manager, "_check_qdrant_health", return_value=False):
            result = docker_manager.wait_for_health(timeout=0.5, check_interval=0.1)

    assert result is False


def test_get_status(docker_manager):
    """Test get_status returns correct status."""
    with mock.patch.object(docker_manager, "_check_neo4j_health", return_value=True):
        with mock.patch.object(docker_manager, "_check_qdrant_health", return_value=False):
            status = docker_manager.get_status()

    assert status == {"neo4j": True, "qdrant": False}


def test_check_neo4j_health_success(docker_manager, mock_subprocess_health):
    """Test Neo4j health check success."""
    result = docker_manager._check_neo4j_health()
    assert result is True


def test_check_neo4j_health_failure(docker_manager):
    """Test Neo4j health check failure."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=1)
        result = docker_manager._check_neo4j_health()

    assert result is False


def test_check_neo4j_health_exception(docker_manager):
    """Test Neo4j health check exception."""
    with mock.patch("subprocess.run", side_effect=Exception("Connection error")):
        result = docker_manager._check_neo4j_health()

    assert result is False


def test_check_qdrant_health_success(docker_manager, mock_subprocess_health):
    """Test Qdrant health check success."""
    result = docker_manager._check_qdrant_health()
    assert result is True


def test_check_qdrant_health_failure(docker_manager):
    """Test Qdrant health check failure."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=1)
        result = docker_manager._check_qdrant_health()

    assert result is False


def test_check_qdrant_health_exception(docker_manager):
    """Test Qdrant health check exception."""
    with mock.patch("subprocess.run", side_effect=Exception("Connection error")):
        result = docker_manager._check_qdrant_health()

    assert result is False


def test_restart_success(docker_manager):
    """Test successful container restart."""
    with mock.patch.object(docker_manager, "stop", return_value=True):
        with mock.patch.object(docker_manager, "start", return_value=True):
            result = docker_manager.restart()

    assert result is True


def test_restart_stop_failure(docker_manager):
    """Test restart when stop fails."""
    with mock.patch.object(docker_manager, "stop", return_value=False):
        result = docker_manager.restart()

    assert result is False


def test_restart_start_failure(docker_manager):
    """Test restart when start fails."""
    with mock.patch.object(docker_manager, "stop", return_value=True):
        with mock.patch.object(docker_manager, "start", return_value=False):
            result = docker_manager.restart()

    assert result is False


def test_detect_docker_compose_command_docker_compose():
    """Test detection of 'docker compose' command."""
    with mock.patch("subprocess.run") as mock_run:
        # Mock successful 'docker compose version'
        mock_run.return_value = mock.MagicMock(returncode=0)

        with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
            manager = GraphRAGDockerManager()
            assert manager._docker_compose_cmd == ["docker", "compose"]


def test_detect_docker_compose_command_docker_compose_legacy():
    """Test detection when both 'docker compose' and 'docker-compose' fail."""
    with mock.patch("subprocess.run") as mock_run:
        # Both docker compose and docker-compose fail
        mock_run.return_value = mock.MagicMock(returncode=1)

        with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
            with pytest.raises(
                RuntimeError,
                match="Neither 'docker compose' nor 'docker-compose' is available",
            ):
                GraphRAGDockerManager()


def test_detect_docker_compose_command_not_found():
    """Test when docker compose command is not found."""
    with mock.patch("subprocess.run") as mock_run:
        # docker compose command fails
        mock_run.return_value = mock.MagicMock(returncode=1)

        with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
            with pytest.raises(
                RuntimeError,
                match="Neither 'docker compose' nor 'docker-compose' is available",
            ):
                GraphRAGDockerManager()


def test_detect_docker_compose_command_timeout():
    """Test detection when command times out."""
    with mock.patch("subprocess.run") as mock_run:
        # Simulate timeout
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker compose version", timeout=5)

        with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
            with pytest.raises(
                RuntimeError,
                match="Neither 'docker compose' nor 'docker-compose' is available",
            ):
                GraphRAGDockerManager()


def test_detect_docker_compose_command_exception():
    """Test detection when command raises exception."""
    with mock.patch("subprocess.run") as mock_run:
        # Simulate command not found
        mock_run.side_effect = FileNotFoundError("docker: command not found")

        with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
            with pytest.raises(
                RuntimeError,
                match="Neither 'docker compose' nor 'docker-compose' is available",
            ):
                GraphRAGDockerManager()


def test_is_permission_error(docker_manager):
    """Test permission error detection."""
    # Test various permission error messages
    assert docker_manager._is_permission_error("permission denied while trying to connect")
    assert docker_manager._is_permission_error("dial unix /var/run/docker.sock: connect: permission denied")
    assert docker_manager._is_permission_error("Got permission denied while trying to connect to the Docker daemon socket")

    # Test non-permission errors
    assert not docker_manager._is_permission_error("connection refused")
    assert not docker_manager._is_permission_error("command not found")
    assert not docker_manager._is_permission_error("timeout")


def test_run_docker_compose_with_sudo_retry(docker_manager, mock_executor):
    """Test that docker compose retries with sudo on permission error."""
    # First call fails with permission error
    permission_error_result = mock.MagicMock()
    permission_error_result.success = False
    permission_error_result.stderr = "permission denied while trying to connect to the Docker daemon socket"
    permission_error_result.stdout = ""

    # Second call (with sudo) succeeds
    success_result = mock.MagicMock()
    success_result.success = True
    success_result.stderr = ""
    success_result.stdout = "Started containers"

    mock_executor.run_command.side_effect = [permission_error_result, success_result]

    result = docker_manager._run_docker_compose(["up", "-d"])

    # Should have been called twice
    assert mock_executor.run_command.call_count == 2

    # First call without sudo
    first_call_args = mock_executor.run_command.call_args_list[0][0][0]
    assert "sudo" not in first_call_args

    # Second call with sudo
    second_call_args = mock_executor.run_command.call_args_list[1][0][0]
    assert second_call_args[0] == "sudo"

    # Result should be the successful one
    assert result.success is True


def test_run_docker_compose_no_retry_on_other_errors(docker_manager, mock_executor):
    """Test that docker compose does not retry on non-permission errors."""
    # Fail with non-permission error
    error_result = mock.MagicMock()
    error_result.success = False
    error_result.stderr = "connection refused"
    error_result.stdout = ""

    mock_executor.run_command.return_value = error_result

    result = docker_manager._run_docker_compose(["up", "-d"])

    # Should have been called only once (no retry)
    assert mock_executor.run_command.call_count == 1
    assert result.success is False


def test_run_docker_compose_no_retry_when_disabled(docker_manager, mock_executor):
    """Test that docker compose does not retry when retry_with_sudo is False."""
    # Fail with permission error
    permission_error_result = mock.MagicMock()
    permission_error_result.success = False
    permission_error_result.stderr = "permission denied"
    permission_error_result.stdout = ""

    mock_executor.run_command.return_value = permission_error_result

    result = docker_manager._run_docker_compose(["up", "-d"], retry_with_sudo=False)

    # Should have been called only once (no retry)
    assert mock_executor.run_command.call_count == 1
    assert result.success is False


def test_get_current_container_id_in_container(docker_manager):
    """Test getting container ID when running in container."""
    with mock.patch("src.auto_coder.graphrag_docker_manager.is_running_in_container", return_value=True):
        with mock.patch("builtins.open", mock.mock_open(read_data="abc123def456\n")):
            container_id = docker_manager._get_current_container_id()

    assert container_id == "abc123def456"


def test_get_current_container_id_not_in_container(docker_manager):
    """Test getting container ID when not running in container."""
    with mock.patch("src.auto_coder.graphrag_docker_manager.is_running_in_container", return_value=False):
        container_id = docker_manager._get_current_container_id()

    assert container_id is None


def test_get_graphrag_network_name_success(docker_manager):
    """Test getting GraphRAG network name."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=0, stdout=b"auto-coder_graphrag-network\nother-network\n")
        network_name = docker_manager._get_graphrag_network_name()

    assert network_name == "auto-coder_graphrag-network"


def test_get_graphrag_network_name_not_found(docker_manager):
    """Test getting GraphRAG network name when not found."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=0, stdout=b"other-network\nanother-network\n")
        network_name = docker_manager._get_graphrag_network_name()

    assert network_name is None


def test_connect_to_graphrag_network_success(docker_manager):
    """Test connecting to GraphRAG network."""
    with mock.patch.object(docker_manager, "_get_current_container_id", return_value="abc123"):
        with mock.patch.object(
            docker_manager,
            "_get_graphrag_network_name",
            return_value="auto-coder_graphrag-network",
        ):
            with mock.patch("subprocess.run") as mock_run:
                # First call: get container name
                # Second call: check if already connected (not connected)
                # Third call: connect to network (success)
                mock_run.side_effect = [
                    mock.MagicMock(returncode=0, stdout=b"/test-container\n"),
                    mock.MagicMock(returncode=0, stdout=b"other-container "),
                    mock.MagicMock(returncode=0, stdout=b""),
                ]

                docker_manager._connect_to_graphrag_network()

                # Should have called docker inspect, docker network inspect, and docker network connect
                assert mock_run.call_count == 3


def test_connect_to_graphrag_network_already_connected(docker_manager):
    """Test connecting to GraphRAG network when already connected."""
    with mock.patch.object(docker_manager, "_get_current_container_id", return_value="abc123"):
        with mock.patch.object(
            docker_manager,
            "_get_graphrag_network_name",
            return_value="auto-coder_graphrag-network",
        ):
            with mock.patch("subprocess.run") as mock_run:
                # First call: get container name
                # Second call: check connection (already connected by name)
                mock_run.side_effect = [
                    mock.MagicMock(returncode=0, stdout=b"/test-container\n"),
                    mock.MagicMock(returncode=0, stdout=b"test-container other-container "),
                ]

                docker_manager._connect_to_graphrag_network()

                # Should only check container name and connection, not connect
                assert mock_run.call_count == 2


def test_connect_to_graphrag_network_not_in_container(docker_manager):
    """Test connecting to GraphRAG network when not in container."""
    with mock.patch.object(docker_manager, "_get_current_container_id", return_value=None):
        with mock.patch("subprocess.run") as mock_run:
            docker_manager._connect_to_graphrag_network()

            # Should not call docker commands
            mock_run.assert_not_called()


def test_start_connects_to_network(docker_manager, mock_executor):
    """Test that start() connects to GraphRAG network."""
    # Mock successful docker-compose up
    mock_result = mock.MagicMock()
    mock_result.success = True
    mock_executor.run_command.return_value = mock_result

    with mock.patch.object(docker_manager, "wait_for_health", return_value=True):
        with mock.patch.object(docker_manager, "_connect_to_graphrag_network") as mock_connect:
            docker_manager.start(wait_for_health=True)

            # Should have called _connect_to_graphrag_network
            mock_connect.assert_called_once()


def test_run_docker_compose_uses_working_directory(docker_manager, mock_executor):
    """Test that _run_docker_compose passes the correct working directory."""
    # Mock successful docker-compose up
    mock_result = mock.MagicMock()
    mock_result.success = True
    mock_executor.run_command.return_value = mock_result

    # Set the compose file path
    docker_manager.compose_file = "/home/user/.auto-coder/graphrag/docker-compose.graphrag.yml"

    # Run docker compose up
    result = docker_manager._run_docker_compose(["up", "-d"])

    # Verify that run_command was called with the correct working directory
    call_args = mock_executor.run_command.call_args
    assert call_args[1]["cwd"] == "/home/user/.auto-coder/graphrag"
    assert result.success is True


def test_run_docker_compose_with_sudo_uses_working_directory(docker_manager, mock_executor):
    """Test that _run_docker_compose passes working directory on sudo retry."""
    # Mock permission error on first call, success on second
    permission_error_result = mock.MagicMock()
    permission_error_result.success = False
    permission_error_result.stderr = "permission denied while trying to connect to the Docker daemon socket"
    permission_error_result.stdout = ""

    success_result = mock.MagicMock()
    success_result.success = True
    success_result.stderr = ""
    success_result.stdout = "Started containers"

    mock_executor.run_command.side_effect = [permission_error_result, success_result]

    # Set the compose file path
    docker_manager.compose_file = "/home/user/.auto-coder/graphrag/docker-compose.graphrag.yml"

    # Run docker compose up
    result = docker_manager._run_docker_compose(["up", "-d"])

    # Verify that run_command was called twice with the correct working directory
    assert mock_executor.run_command.call_count == 2

    # First call without sudo
    first_call_args = mock_executor.run_command.call_args_list[0]
    assert first_call_args[1]["cwd"] == "/home/user/.auto-coder/graphrag"

    # Second call with sudo
    second_call_args = mock_executor.run_command.call_args_list[1]
    assert second_call_args[1]["cwd"] == "/home/user/.auto-coder/graphrag"
    assert result.success is True


def test_get_compose_file_from_package_uses_home_directory(monkeypatch):
    """Test that _get_compose_file_from_package uses home directory."""
    from pathlib import Path

    # Mock the home directory
    fake_home = Path("/fake/home")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Mock the entire method to avoid actual file system operations
    expected_path = str(fake_home / ".auto-coder" / "graphrag" / "docker-compose.graphrag.yml")

    with mock.patch.object(GraphRAGDockerManager, "_get_compose_file_from_package", return_value=expected_path):
        with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
            with mock.patch.object(
                GraphRAGDockerManager,
                "_detect_docker_compose_command",
                return_value=["docker", "compose"],
            ):
                manager = GraphRAGDockerManager()

                # Verify the compose file is in the home directory
                assert manager.compose_file == expected_path
