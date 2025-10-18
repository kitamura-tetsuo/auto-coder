"""Tests for GraphRAG Docker Manager."""

import subprocess
from unittest import mock

import pytest

from src.auto_coder.graphrag_docker_manager import GraphRAGDockerManager


@pytest.fixture
def mock_executor():
    """Create a mock CommandExecutor."""
    return mock.MagicMock()


@pytest.fixture
def docker_manager(mock_executor):
    """Create a GraphRAGDockerManager instance for testing."""
    with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor", return_value=mock_executor):
        manager = GraphRAGDockerManager()
        return manager


def test_init_default_compose_file():
    """Test initialization with default compose file."""
    with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
        manager = GraphRAGDockerManager()
        assert manager.compose_file.endswith("docker-compose.graphrag.yml")


def test_init_custom_compose_file():
    """Test initialization with custom compose file."""
    custom_path = "/custom/path/docker-compose.yml"
    with mock.patch("src.auto_coder.graphrag_docker_manager.CommandExecutor"):
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


def test_check_neo4j_health_success(docker_manager):
    """Test Neo4j health check success."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=0)
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


def test_check_qdrant_health_success(docker_manager):
    """Test Qdrant health check success."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.MagicMock(returncode=0)
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

