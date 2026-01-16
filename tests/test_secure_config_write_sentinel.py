import json
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from auto_coder.claude_client import ClaudeClient
from auto_coder.codex_client import CodexClient
from auto_coder.codex_mcp_client import CodexMCPClient


class TestSecureConfigWrite:

    @patch("auto_coder.codex_client.subprocess.run")
    @patch("auto_coder.codex_client.get_llm_config")
    @patch("os.open")
    @patch("os.fdopen")
    def test_codex_client_add_mcp_server_config_secure(self, mock_fdopen, mock_os_open, mock_get_llm_config, mock_subprocess):
        # Setup mocks
        mock_subprocess.return_value.returncode = 0
        mock_config = MagicMock()
        mock_config.get_backend_config.return_value = MagicMock(options=[], options_for_noedit=[])
        mock_get_llm_config.return_value = mock_config

        mock_fd = 123
        mock_os_open.return_value = mock_fd
        mock_file = MagicMock()
        mock_fdopen.return_value.__enter__.return_value = mock_file

        # Initialize client
        client = CodexClient()

        # Patch Path.exists and open within the method scope if possible, or globally
        with patch("pathlib.Path.exists", return_value=False), patch("builtins.open", mock_open()) as mock_builtin_open:

            # Call the method
            result = client.add_mcp_server_config("test_server", "test_cmd", ["arg1"])

            # Verify result
            assert result is True

            # Verify os.open was called with correct permissions (0o600)
            # os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            expected_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC

            # Check if os.open was called at all
            if mock_os_open.call_count == 0:
                pytest.fail("os.open was not called! Likely still using builtins.open")

            args, kwargs = mock_os_open.call_args
            assert args[1] == expected_flags
            assert args[2] == 0o600

            # Verify os.fdopen was called
            mock_fdopen.assert_called_with(mock_fd, "w", encoding="utf-8")

            # Verify json dump happened (write called on the file handle)
            mock_file.write.assert_called()

    @patch("auto_coder.claude_client.subprocess.run")
    @patch("auto_coder.claude_client.get_llm_config")
    @patch("os.open")
    @patch("os.fdopen")
    def test_claude_client_add_mcp_server_config_secure(self, mock_fdopen, mock_os_open, mock_get_llm_config, mock_subprocess):
        # Setup mocks
        mock_subprocess.return_value.returncode = 0
        mock_config = MagicMock()
        mock_config.get_backend_config.return_value = MagicMock(options=[], options_for_noedit=[])
        mock_get_llm_config.return_value = mock_config

        mock_fd = 123
        mock_os_open.return_value = mock_fd
        mock_file = MagicMock()
        mock_fdopen.return_value.__enter__.return_value = mock_file

        # Initialize client
        client = ClaudeClient()

        with patch("pathlib.Path.exists", return_value=False), patch("builtins.open", mock_open()) as mock_builtin_open:

            result = client.add_mcp_server_config("test_server", "test_cmd", ["arg1"])

            assert result is True

            if mock_os_open.call_count == 0:
                pytest.fail("os.open was not called! Likely still using builtins.open")

            args, kwargs = mock_os_open.call_args
            assert args[1] == os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            assert args[2] == 0o600

            mock_fdopen.assert_called_with(mock_fd, "w", encoding="utf-8")
            mock_file.write.assert_called()

    @patch("auto_coder.codex_mcp_client.subprocess.Popen")
    @patch("auto_coder.codex_mcp_client.subprocess.run")
    @patch("auto_coder.codex_mcp_client.get_llm_config")
    @patch("os.open")
    @patch("os.fdopen")
    def test_codex_mcp_client_add_mcp_server_config_secure(self, mock_fdopen, mock_os_open, mock_get_llm_config, mock_run, mock_popen):
        # Setup mocks
        mock_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_config.get_backend_config.return_value = MagicMock(options=[], options_for_noedit=[])
        mock_get_llm_config.return_value = mock_config

        # Mock Popen for CodexMCPClient init
        mock_process = MagicMock()
        mock_process.stderr = MagicMock()
        mock_process.stdin = MagicMock()
        mock_process.stdout = MagicMock()
        mock_popen.return_value = mock_process

        mock_fd = 123
        mock_os_open.return_value = mock_fd
        mock_file = MagicMock()
        mock_fdopen.return_value.__enter__.return_value = mock_file

        # Initialize client
        client = CodexMCPClient()

        with patch("pathlib.Path.exists", return_value=False), patch("builtins.open", mock_open()) as mock_builtin_open:

            result = client.add_mcp_server_config("test_server", "test_cmd", ["arg1"])

            assert result is True

            if mock_os_open.call_count == 0:
                pytest.fail("os.open was not called! Likely still using builtins.open")

            args, kwargs = mock_os_open.call_args
            assert args[1] == os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            assert args[2] == 0o600

            mock_fdopen.assert_called_with(mock_fd, "w", encoding="utf-8")
            mock_file.write.assert_called()
