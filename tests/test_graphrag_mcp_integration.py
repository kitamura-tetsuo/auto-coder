"""Tests for GraphRAG MCP Integration."""

from unittest import mock

import pytest

from src.auto_coder.graphrag_docker_manager import GraphRAGDockerManager
from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager
from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration


@pytest.fixture
def mock_docker_manager():
    """Create a mock GraphRAGDockerManager."""
    return mock.MagicMock(spec=GraphRAGDockerManager)


@pytest.fixture
def mock_index_manager():
    """Create a mock GraphRAGIndexManager."""
    return mock.MagicMock(spec=GraphRAGIndexManager)


@pytest.fixture
def integration(mock_docker_manager, mock_index_manager):
    """Create a GraphRAGMCPIntegration instance for testing."""
    return GraphRAGMCPIntegration(
        docker_manager=mock_docker_manager, index_manager=mock_index_manager
    )


def test_init_with_managers(mock_docker_manager, mock_index_manager):
    """Test initialization with provided managers."""
    integration = GraphRAGMCPIntegration(
        docker_manager=mock_docker_manager, index_manager=mock_index_manager
    )
    assert integration.docker_manager is mock_docker_manager
    assert integration.index_manager is mock_index_manager


def test_init_without_managers():
    """Test initialization without provided managers."""
    integration = GraphRAGMCPIntegration()
    assert isinstance(integration.docker_manager, GraphRAGDockerManager)
    assert isinstance(integration.index_manager, GraphRAGIndexManager)


def test_init_with_mcp_server_path():
    """Test initialization with MCP server path."""
    server_path = "/path/to/mcp/server"
    integration = GraphRAGMCPIntegration(mcp_server_path=server_path)
    assert integration.mcp_server_path == server_path


def test_init_with_env_mcp_server_path(monkeypatch):
    """Test initialization with MCP server path from environment."""
    server_path = "/env/path/to/mcp/server"
    monkeypatch.setenv("GRAPHRAG_MCP_SERVER_PATH", server_path)
    integration = GraphRAGMCPIntegration()
    assert integration.mcp_server_path == server_path


def test_ensure_ready_containers_not_running(integration, mock_docker_manager):
    """Test ensure_ready when containers are not running."""
    mock_docker_manager.is_running.return_value = False
    mock_docker_manager.start.return_value = True
    integration.index_manager.ensure_index_up_to_date.return_value = True

    result = integration.ensure_ready()

    assert result is True
    mock_docker_manager.is_running.assert_called_once()
    mock_docker_manager.start.assert_called_once_with(wait_for_health=True)
    integration.index_manager.ensure_index_up_to_date.assert_called_once()


def test_ensure_ready_containers_already_running(integration, mock_docker_manager):
    """Test ensure_ready when containers are already running."""
    mock_docker_manager.is_running.return_value = True
    integration.index_manager.ensure_index_up_to_date.return_value = True

    result = integration.ensure_ready()

    assert result is True
    mock_docker_manager.is_running.assert_called_once()
    mock_docker_manager.start.assert_not_called()
    integration.index_manager.ensure_index_up_to_date.assert_called_once()


def test_ensure_ready_container_start_failure(integration, mock_docker_manager):
    """Test ensure_ready when container start fails."""
    mock_docker_manager.is_running.return_value = False
    mock_docker_manager.start.return_value = False

    result = integration.ensure_ready()

    assert result is False
    # Should retry twice (max_retries=2)
    assert mock_docker_manager.start.call_count == 2
    mock_docker_manager.start.assert_called_with(wait_for_health=True)
    integration.index_manager.ensure_index_up_to_date.assert_not_called()


def test_ensure_ready_index_update_failure(integration, mock_docker_manager):
    """Test ensure_ready when index update fails."""
    mock_docker_manager.is_running.return_value = True
    integration.index_manager.ensure_index_up_to_date.return_value = False

    result = integration.ensure_ready()

    # Index update failure is now a fatal error
    assert result is False
    integration.index_manager.ensure_index_up_to_date.assert_called_once()


def test_ensure_ready_with_mcp_server(integration, mock_docker_manager):
    """Test ensure_ready with MCP server configured."""
    integration.mcp_server_path = "/path/to/server"
    mock_docker_manager.is_running.return_value = True
    integration.index_manager.ensure_index_up_to_date.return_value = True

    with mock.patch.object(integration, "is_mcp_server_running", return_value=False):
        with mock.patch.object(integration, "start_mcp_server", return_value=True):
            result = integration.ensure_ready()

    assert result is True


def test_ensure_ready_mcp_server_failure(integration, mock_docker_manager):
    """Test ensure_ready when MCP server start fails."""
    integration.mcp_server_path = "/path/to/server"
    mock_docker_manager.is_running.return_value = True
    integration.index_manager.ensure_index_up_to_date.return_value = True

    with mock.patch.object(integration, "is_mcp_server_running", return_value=False):
        with mock.patch.object(integration, "start_mcp_server", return_value=False):
            result = integration.ensure_ready()

    # MCP server failure is now a fatal error
    assert result is False


def test_start_mcp_server_success(integration):
    """Test successful MCP server start."""
    integration.mcp_server_path = "python -m graphrag_mcp"

    with mock.patch("subprocess.Popen") as mock_popen:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_proc.stderr = None
        mock_popen.return_value = mock_proc

        result = integration.start_mcp_server()

    assert result is True
    assert integration.mcp_process is mock_proc
    mock_popen.assert_called_once()


def test_start_mcp_server_no_path(integration):
    """Test MCP server start without configured path."""
    integration.mcp_server_path = None

    result = integration.start_mcp_server()

    assert result is False


def test_start_mcp_server_failure(integration):
    """Test MCP server start failure."""
    integration.mcp_server_path = "invalid_command"

    with mock.patch("subprocess.Popen", side_effect=Exception("Command not found")):
        result = integration.start_mcp_server()

    assert result is False


def test_is_mcp_server_running_true(integration):
    """Test is_mcp_server_running when server is running."""
    mock_proc = mock.MagicMock()
    mock_proc.poll.return_value = None  # Process is still running
    integration.mcp_process = mock_proc

    result = integration.is_mcp_server_running()

    assert result is True


def test_is_mcp_server_running_false(integration):
    """Test is_mcp_server_running when server is not running."""
    mock_proc = mock.MagicMock()
    mock_proc.poll.return_value = 0  # Process has exited
    integration.mcp_process = mock_proc

    result = integration.is_mcp_server_running()

    assert result is False


def test_is_mcp_server_running_no_process(integration):
    """Test is_mcp_server_running when no process exists."""
    integration.mcp_process = None

    result = integration.is_mcp_server_running()

    assert result is False


def test_stop_mcp_server_success(integration):
    """Test successful MCP server stop."""
    mock_proc = mock.MagicMock()
    integration.mcp_process = mock_proc

    integration.stop_mcp_server()

    mock_proc.terminate.assert_called_once()
    mock_proc.wait.assert_called_once()
    assert integration.mcp_process is None


def test_stop_mcp_server_no_process(integration):
    """Test MCP server stop when no process exists."""
    integration.mcp_process = None

    # Should not raise exception
    integration.stop_mcp_server()

    assert integration.mcp_process is None


def test_stop_mcp_server_timeout(integration):
    """Test MCP server stop with timeout."""
    import subprocess

    mock_proc = mock.MagicMock()
    mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
    integration.mcp_process = mock_proc

    integration.stop_mcp_server()

    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()
    assert integration.mcp_process is None


def test_get_mcp_config_for_llm_running(integration):
    """Test get_mcp_config_for_llm when server is running."""
    with mock.patch.object(integration, "is_mcp_server_running", return_value=True):
        config = integration.get_mcp_config_for_llm()

    assert config is not None
    assert "mcp_server" in config
    assert "mcp_tools" in config
    assert "mcp_resources" in config
    assert config["mcp_server"] == "graphrag"


def test_get_mcp_config_for_llm_not_running(integration):
    """Test get_mcp_config_for_llm when server is not running."""
    with mock.patch.object(integration, "is_mcp_server_running", return_value=False):
        config = integration.get_mcp_config_for_llm()

    assert config is None


def test_pump_stderr(integration):
    """Test _pump_stderr method."""
    mock_stderr = mock.MagicMock()
    mock_stderr.readline.side_effect = [b"line1\n", b"line2\n", b""]

    integration._pump_stderr(mock_stderr)

    # Should have read all lines
    assert mock_stderr.readline.call_count == 3


def test_pump_stderr_exception(integration):
    """Test _pump_stderr with exception."""
    mock_stderr = mock.MagicMock()
    mock_stderr.readline.side_effect = Exception("Read error")

    # Should not raise exception
    integration._pump_stderr(mock_stderr)

