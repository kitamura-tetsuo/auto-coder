import os
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, mock_open, patch

from src.auto_coder.graphrag_docker_manager import GraphRAGDockerManager


class TestGraphRAGDockerManagerSecurePermissions(unittest.TestCase):
    @patch("src.auto_coder.graphrag_docker_manager.resources.files")
    @patch("src.auto_coder.graphrag_docker_manager.Path")
    @patch("src.auto_coder.graphrag_docker_manager.os")
    @patch("src.auto_coder.graphrag_docker_manager.CommandExecutor")
    @patch("builtins.open", new_callable=mock_open)
    def test_get_compose_file_secure_permissions(self, mock_builtin_open, mock_executor, mock_os, mock_path, mock_resources):
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

        # Mock os constants
        mock_os.O_WRONLY = os.O_WRONLY
        mock_os.O_CREAT = os.O_CREAT
        mock_os.O_TRUNC = os.O_TRUNC

        # Mock subprocess to avoid real execution during init
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            # Initialize manager which calls _get_compose_file_from_package
            manager = GraphRAGDockerManager()

        # Verify builtins.open called with 'w' and an opener
        mock_builtin_open.assert_called_with(mock_compose_file, "w", opener=ANY)

        # Verify content written
        mock_builtin_open.return_value.write.assert_called_with("services:\n  neo4j:\n    ...")

        # Extract the opener and verify its behavior
        kwargs = mock_builtin_open.call_args.kwargs
        opener = kwargs["opener"]

        # Test the opener calls os.open with correct permissions
        test_path = "/test/path"
        test_flags = 123
        opener(test_path, test_flags)

        mock_os.open.assert_called_with(test_path, test_flags, 0o600)

        # Verify chmod called on the file
        mock_os.chmod.assert_any_call(mock_compose_file, 0o600)


if __name__ == "__main__":
    unittest.main()
