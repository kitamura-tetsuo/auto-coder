import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

from src.auto_coder.graphrag_docker_manager import GraphRAGDockerManager

class TestGraphRAGDockerManagerSecurePermissions(unittest.TestCase):
    @patch("src.auto_coder.graphrag_docker_manager.resources.files")
    @patch("src.auto_coder.graphrag_docker_manager.Path")
    @patch("src.auto_coder.graphrag_docker_manager.os")
    @patch("src.auto_coder.graphrag_docker_manager.CommandExecutor")
    def test_get_compose_file_secure_permissions(self, mock_executor, mock_os, mock_path, mock_resources):
        # Mock resources
        mock_file = MagicMock()
        mock_file.read_text.return_value = "services:\n  neo4j:\n    ..."
        mock_resources.return_value.__truediv__.return_value = mock_file

        # Mock Path
        mock_home = MagicMock()
        mock_path.home.return_value = mock_home
        mock_temp_dir = MagicMock()
        mock_home.__truediv__.return_value.__truediv__.return_value = mock_temp_dir

        mock_compose_file = MagicMock()
        mock_temp_dir.__truediv__.return_value = mock_compose_file
        mock_compose_file.__str__.return_value = "/home/user/.auto-coder/graphrag/docker-compose.graphrag.yml"

        # Mock os.open and os.fdopen
        mock_fd = 123
        mock_os.open.return_value = mock_fd
        mock_file_handle = MagicMock()
        mock_os.fdopen.return_value.__enter__.return_value = mock_file_handle

        # Mock os constants
        mock_os.O_WRONLY = os.O_WRONLY
        mock_os.O_CREAT = os.O_CREAT
        mock_os.O_TRUNC = os.O_TRUNC

        # Mock subprocess to avoid real execution during init
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            # Initialize manager which calls _get_compose_file_from_package
            manager = GraphRAGDockerManager()

        # This part will fail initially because we use write_text, not os.open
        # Verify os.open called with 0o600
        mock_os.open.assert_called_with(str(mock_compose_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)

        # Verify content written
        mock_file_handle.write.assert_called_with("services:\n  neo4j:\n    ...")

        # Verify chmod called on the file
        mock_os.chmod.assert_any_call(mock_compose_file, 0o600)

if __name__ == "__main__":
    unittest.main()
