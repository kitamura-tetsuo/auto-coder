"""Reproduction test for GraphRAG sudo password issue."""

import pytest
from unittest import mock
import os
from src.auto_coder.graphrag_docker_manager import GraphRAGDockerManager

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
            # Also patch extracting compose file to avoid file system issues
            with mock.patch.object(
                GraphRAGDockerManager,
                "_get_compose_file_from_package",
                return_value="/tmp/docker-compose.yml",
            ):
                manager = GraphRAGDockerManager()
                return manager

def test_run_docker_compose_preserves_password_with_sudo(docker_manager, mock_executor):
    """Test that docker compose preserves NEO4J_PASSWORD when retrying with sudo."""
    # Set the environment variable
    with mock.patch.dict(os.environ, {"NEO4J_PASSWORD": "secure_password"}, clear=False):
        # Mock permission error on first call
        permission_error_result = mock.MagicMock()
        permission_error_result.success = False
        permission_error_result.stderr = "permission denied while trying to connect to the Docker daemon socket"
        permission_error_result.stdout = ""

        # Mock success on second call
        success_result = mock.MagicMock()
        success_result.success = True
        success_result.stderr = ""
        success_result.stdout = "Started containers"

        mock_executor.run_command.side_effect = [permission_error_result, success_result]

        # Run command
        docker_manager._run_docker_compose(["up", "-d"])

        # Check second call (retry with sudo)
        assert mock_executor.run_command.call_count == 2
        second_call_args = mock_executor.run_command.call_args_list[1][0][0]

        # Assert that we are preserving the environment variable
        # We expect something like ['sudo', '--preserve-env=NEO4J_PASSWORD', 'docker', 'compose', ...]

        # Check if either -E or --preserve-env=NEO4J_PASSWORD is present
        # This confirms we are attempting to preserve the environment
        has_preserve = any(arg == "-E" or arg == "--preserve-env=NEO4J_PASSWORD" for arg in second_call_args)

        if not has_preserve:
             pytest.fail(f"Sudo command does not preserve environment variables: {second_call_args}")
