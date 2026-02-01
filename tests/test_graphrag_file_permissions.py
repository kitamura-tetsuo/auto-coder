import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.auto_coder.cli_commands_graphrag import run_graphrag_setup_mcp_programmatically
from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager


class TestGraphRAGFilePermissions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir)

    def test_save_index_state_permissions(self):
        """Test that _save_index_state creates file with 0o600 permissions."""
        index_state_file = Path(self.temp_dir) / "index_state.json"

        # Create insecure file first to test permission fix
        with open(index_state_file, "w") as f:
            f.write("{}")
        os.chmod(index_state_file, 0o644)

        manager = GraphRAGIndexManager(repo_path=self.temp_dir, index_state_file=str(index_state_file))
        state = {"test": "data"}

        manager._save_index_state(state)

        self.assertTrue(index_state_file.exists())
        mode = os.stat(index_state_file).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"Expected 0o600 permissions, got {oct(mode)}")

    @patch("src.auto_coder.cli_commands_graphrag.subprocess.run")
    @patch("src.auto_coder.cli_commands_graphrag._add_codex_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_gemini_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_qwen_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_windsurf_claude_config")
    def test_setup_mcp_file_permissions(self, mock_windsurf, mock_qwen, mock_gemini, mock_codex, mock_run):
        """Test that setup_mcp creates files with secure permissions."""
        install_dir = Path(self.temp_dir) / "graphrag_mcp"
        install_dir.mkdir()  # Create dir so skip_clone works

        # Mock successful uv check/install
        mock_run.return_value = MagicMock(returncode=0, stdout="uv 0.1.0")

        # Mock config updates
        mock_codex.return_value = True
        mock_gemini.return_value = True
        mock_qwen.return_value = True
        mock_windsurf.return_value = True

        run_graphrag_setup_mcp_programmatically(install_dir=str(install_dir), skip_clone=True, silent=True)  # Skip cloning to avoid needing bundled files

        # Verify .env permissions (already handled securely, but checking regression)
        env_path = install_dir / ".env"
        self.assertTrue(env_path.exists())
        mode = os.stat(env_path).st_mode & 0o777
        self.assertEqual(mode, 0o600, f".env: Expected 0o600, got {oct(mode)}")

        # Verify run_server.sh permissions
        script_path = install_dir / "run_server.sh"
        self.assertTrue(script_path.exists())
        mode = os.stat(script_path).st_mode & 0o777
        self.assertEqual(mode, 0o755, f"run_server.sh: Expected 0o755, got {oct(mode)}")

        # Verify main.py permissions
        main_py_path = install_dir / "main.py"
        self.assertTrue(main_py_path.exists())
        mode = os.stat(main_py_path).st_mode & 0o777
        # We want to enforce 0o600 for main.py as well
        self.assertEqual(mode, 0o600, f"main.py: Expected 0o600, got {oct(mode)}")
