import json
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from auto_coder.claude_client import ClaudeClient
from auto_coder.codex_client import CodexClient
from auto_coder.codex_mcp_client import CodexMCPClient


@pytest.fixture
def mock_subprocess_codex():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        yield mock_run


@pytest.fixture
def mock_subprocess_claude():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        yield mock_run


def test_codex_client_secure_write(mock_subprocess_codex):
    with patch("os.open") as mock_os_open, patch("os.fdopen") as mock_os_fdopen, patch("pathlib.Path.home") as mock_home, patch("builtins.open", mock_open(read_data="{}")):

        mock_home.return_value = MagicMock()
        mock_home.return_value.__truediv__.return_value.exists.return_value = True

        # We need to mock get_llm_config to avoid loading real config
        with patch("auto_coder.codex_client.get_llm_config") as mock_config:
            client = CodexClient()
            client.add_mcp_server_config("test-server", "echo", ["hello"])

            # Check if os.open was called with 0o600
            mock_os_open.assert_called_with(mock_home.return_value.__truediv__.return_value.__truediv__.return_value, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)


def test_claude_client_secure_write(mock_subprocess_claude):
    with patch("os.open") as mock_os_open, patch("os.fdopen") as mock_os_fdopen, patch("pathlib.Path.home") as mock_home, patch("builtins.open", mock_open(read_data="{}")):

        mock_home.return_value = MagicMock()
        mock_home.return_value.__truediv__.return_value.exists.return_value = True

        with patch("auto_coder.claude_client.get_llm_config") as mock_config:
            client = ClaudeClient()
            client.add_mcp_server_config("test-server", "echo", ["hello"])

            mock_os_open.assert_called_with(mock_home.return_value.__truediv__.return_value.__truediv__.return_value, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)


def test_codex_mcp_client_secure_write(mock_subprocess_codex):
    with patch("os.open") as mock_os_open, patch("os.fdopen") as mock_os_fdopen, patch("pathlib.Path.home") as mock_home, patch("builtins.open", mock_open(read_data="{}")):

        mock_home.return_value = MagicMock()
        mock_home.return_value.__truediv__.return_value.exists.return_value = True

        with patch("auto_coder.codex_mcp_client.get_llm_config") as mock_config:
            # CodexMCPClient spawns a subprocess, we need to mock it
            with patch("subprocess.Popen") as mock_popen:
                # It also waits for stderr pump thread, let's mock _is_running_under_pytest to avoid it
                # Actually the code checks _is_running_under_pytest(), so it should skip the thread if we are running pytest
                # But let's verify if _is_running_under_pytest works in our context.
                # The code: return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ

                client = CodexMCPClient()
                client.add_mcp_server_config("test-server", "echo", ["hello"])

                mock_os_open.assert_called_with(mock_home.return_value.__truediv__.return_value.__truediv__.return_value, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
