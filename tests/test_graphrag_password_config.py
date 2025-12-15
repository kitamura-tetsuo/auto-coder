import os
import unittest
from unittest.mock import patch, MagicMock
from src.auto_coder.cli_helpers import initialize_graphrag


class TestGraphRAGPasswordConfig(unittest.TestCase):
    # Patch where it is defined, because it is imported locally inside the function
    @patch("src.auto_coder.cli_commands_graphrag.run_graphrag_setup_mcp_programmatically")
    @patch("pathlib.Path.exists")
    def test_initialize_graphrag_uses_env_password(self, mock_exists, mock_setup):
        # Mock that mcp dir does not exist, so setup runs
        mock_exists.return_value = False
        mock_setup.return_value = True

        # Set env var
        with patch.dict(os.environ, {"NEO4J_PASSWORD": "secure_password_123"}):
            try:
                initialize_graphrag()
            except Exception as e:
                pass

        # Verify setup was called with the env password
        args, kwargs = mock_setup.call_args
        self.assertEqual(kwargs.get("neo4j_password"), "secure_password_123")

    @patch("src.auto_coder.cli_commands_graphrag.run_graphrag_setup_mcp_programmatically")
    @patch("pathlib.Path.exists")
    def test_initialize_graphrag_uses_default_password(self, mock_exists, mock_setup):
        # Mock that mcp dir does not exist
        mock_exists.return_value = False
        mock_setup.return_value = True

        with patch.dict(os.environ):
            if "NEO4J_PASSWORD" in os.environ:
                del os.environ["NEO4J_PASSWORD"]

            try:
                initialize_graphrag()
            except Exception:
                pass

        # Verify setup was called with default password
        args, kwargs = mock_setup.call_args
        self.assertEqual(kwargs.get("neo4j_password"), "password")
