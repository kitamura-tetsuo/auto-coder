import os
import shutil
import pytest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

from src.auto_coder.cli_commands_graphrag import run_graphrag_setup_mcp_programmatically


@pytest.fixture
def mock_subprocess():
    with patch("subprocess.run") as mock_run:
        # Mock successful executions
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "uv 0.1.0"
        yield mock_run


@pytest.fixture
def mock_os_open():
    with patch("os.open") as mock_open_func:
        mock_open_func.return_value = 123  # Fake file descriptor
        yield mock_open_func


@pytest.fixture
def mock_os_fdopen():
    with patch("os.fdopen") as mock_fdopen_func:
        yield mock_fdopen_func


@pytest.fixture
def mock_shutil():
    with patch("shutil.which") as mock_which, patch("shutil.copytree") as mock_copytree, patch("shutil.rmtree") as mock_rmtree:
        mock_which.return_value = "/usr/bin/uv"
        yield mock_which


def test_graphrag_setup_mcp_env_injection_prevented(mock_subprocess, mock_os_open, mock_os_fdopen, mock_shutil):
    """
    Test that a malicious password cannot inject environment variables into the .env file.
    """
    install_dir = "/tmp/fake_install_dir"
    neo4j_password = "password\nINJECTED_VAR=hacked"

    # Custom exists side effect
    def exists_side_effect(self):
        # install_path does not exist
        if str(self) == install_dir:
            return False
        # bundled_mcp exists
        if "mcp_servers/graphrag_mcp" in str(self):
            return True
        return False

    with patch("pathlib.Path.exists", autospec=True) as mock_exists, patch("pathlib.Path.mkdir"), patch("builtins.open", mock_open()):

        mock_exists.side_effect = exists_side_effect

        # We need to capture what is written to the file via os.fdopen
        mock_file_handle = MagicMock()
        mock_os_fdopen.return_value.__enter__.return_value = mock_file_handle

        # Mock other dependencies
        with (
            patch("src.auto_coder.cli_commands_graphrag._add_codex_config", return_value=True),
            patch("src.auto_coder.cli_commands_graphrag._add_gemini_config", return_value=True),
            patch("src.auto_coder.cli_commands_graphrag._add_qwen_config", return_value=True),
            patch("src.auto_coder.cli_commands_graphrag._add_windsurf_claude_config", return_value=True),
        ):

            run_graphrag_setup_mcp_programmatically(install_dir=install_dir, neo4j_password=neo4j_password, silent=True, skip_clone=False)

            # Reconstruct written content
            written_content = ""
            for call in mock_file_handle.write.call_args_list:
                written_content += call.args[0]

            print(f"\nWritten content:\n{written_content}")

            # Verify injection is prevented
            # The password should be quoted: "password\nINJECTED_VAR=hacked"
            expected_password_line = 'NEO4J_PASSWORD="password\nINJECTED_VAR=hacked"'

            assert expected_password_line in written_content

            # Verify that INJECTED_VAR is NOT on its own line as a key
            # It should be part of the string
            assert "\nINJECTED_VAR=hacked" not in written_content.replace(expected_password_line, "")
