import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values

from src.auto_coder.cli_commands_graphrag import run_graphrag_setup_mcp_programmatically


class TestGraphRAGSecurity(unittest.TestCase):
    """Security tests for GraphRAG commands."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.install_dir = Path(self.temp_dir) / "graphrag_mcp"

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    @patch("subprocess.run")
    @patch("src.auto_coder.cli_commands_graphrag._add_codex_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_gemini_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_qwen_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_windsurf_claude_config")
    def test_env_injection_prevention(self, mock_windsurf, mock_qwen, mock_gemini, mock_codex, mock_run):
        """Test that .env injection via malicious input is prevented."""
        # Mock subprocess.run to return success
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "uv 0.1.0"
        mock_run.return_value = mock_result

        # Mock config helpers to return True
        mock_windsurf.return_value = True
        mock_qwen.return_value = True
        mock_gemini.return_value = True
        mock_codex.return_value = True

        # Malicious inputs
        malicious_inputs = {"neo4j_password": "password\nINJECTED_VAR=malicious_value", "neo4j_user": 'user" --injection', "qdrant_url": 'http://localhost:6333" # comment'}

        # Create directory
        self.install_dir.mkdir(parents=True, exist_ok=True)

        # Run setup
        success = run_graphrag_setup_mcp_programmatically(install_dir=str(self.install_dir), neo4j_password=malicious_inputs["neo4j_password"], neo4j_user=malicious_inputs["neo4j_user"], qdrant_url=malicious_inputs["qdrant_url"], silent=True, skip_clone=True)

        self.assertTrue(success, "Setup should succeed despite malicious input (safely handled)")

        env_path = self.install_dir / ".env"
        self.assertTrue(env_path.exists(), ".env file should be created")

        # Parse generated .env
        config = dotenv_values(env_path)

        # Verify no injection occurred
        self.assertNotIn("INJECTED_VAR", config, "Environment variable injection detected")

        # Verify values are correctly preserved (handled as multiline or quoted strings)
        self.assertEqual(config.get("NEO4J_PASSWORD"), malicious_inputs["neo4j_password"])
        self.assertEqual(config.get("NEO4J_USER"), malicious_inputs["neo4j_user"])
        self.assertEqual(config.get("QDRANT_URL"), malicious_inputs["qdrant_url"])


if __name__ == "__main__":
    unittest.main()
