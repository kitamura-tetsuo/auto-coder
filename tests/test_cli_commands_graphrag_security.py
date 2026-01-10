import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.auto_coder.cli_commands_graphrag import run_graphrag_setup_mcp_programmatically

class TestGraphRAGSecurity:

    @patch("subprocess.run")
    @patch("src.auto_coder.cli_commands_graphrag._add_codex_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_gemini_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_qwen_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_windsurf_claude_config")
    @patch("shutil.which", return_value="/usr/bin/uv")
    def test_env_injection_prevention(self, mock_which, mock_windsurf, mock_qwen, mock_gemini, mock_codex, mock_run):
        """Verify that environment variable injection is prevented in .env generation."""
        # Mock uv check success
        mock_run.return_value.returncode = 0

        # Create a temporary directory for installation
        with tempfile.TemporaryDirectory() as temp_dir:
            install_path = Path(temp_dir)

            # Malicious inputs attempting to inject variables
            malicious_password = 'password\nINJECTED_VAR=hacked'
            malicious_user = 'neo4j"\nINJECTED_USER=hacked'

            # Run the setup with the malicious password
            success = run_graphrag_setup_mcp_programmatically(
                install_dir=str(install_path),
                neo4j_user=malicious_user,
                neo4j_password=malicious_password,
                skip_clone=True, # Skip cloning to avoid needing the bundled mcp
                silent=True
            )

            # Check the generated .env file
            env_path = install_path / ".env"
            assert env_path.exists(), ".env file should be created"

            content = env_path.read_text()

            # Verify INJECTED_VAR is NOT present as a key (it should be quoted inside the value)
            # When quoted, it looks like NEO4J_PASSWORD="password\nINJECTED_VAR=hacked"
            # It should NOT be interpreted as a separate env var by dotenv

            # Check raw string content
            assert 'NEO4J_PASSWORD="password\nINJECTED_VAR=hacked"' in content
            assert 'NEO4J_USER="neo4j\\"\nINJECTED_USER=hacked"' in content

            # Also verify via dotenv loading (simulating how it's consumed)
            from dotenv import dotenv_values
            env_values = dotenv_values(env_path)

            assert env_values["NEO4J_PASSWORD"] == malicious_password
            assert env_values["NEO4J_USER"] == malicious_user
            assert "INJECTED_VAR" not in env_values
            assert "INJECTED_USER" not in env_values

    @patch("subprocess.run")
    @patch("src.auto_coder.cli_commands_graphrag._add_codex_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_gemini_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_qwen_config")
    @patch("src.auto_coder.cli_commands_graphrag._add_windsurf_claude_config")
    @patch("shutil.which", return_value="/usr/bin/uv")
    def test_quote_env_value_logic(self, mock_which, mock_windsurf, mock_qwen, mock_gemini, mock_codex, mock_run):
        """Test specific edge cases for value quoting."""
        mock_run.return_value.returncode = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            install_path = Path(temp_dir)

            # Test case with backslashes and quotes
            complex_password = 'pass\\word"with"quotes'

            success = run_graphrag_setup_mcp_programmatically(
                install_dir=str(install_path),
                neo4j_password=complex_password,
                skip_clone=True,
                silent=True
            )

            env_path = install_path / ".env"
            from dotenv import dotenv_values
            env_values = dotenv_values(env_path)

            assert env_values["NEO4J_PASSWORD"] == complex_password
