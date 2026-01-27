import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from click import ClickException

from src.auto_coder.cli_helpers import check_cli_tool, check_gemini_cli_or_fail
from src.auto_coder.gemini_client import GeminiClient


def test_check_cli_tool_not_found():
    """Test that check_cli_tool raises error if tool is not in PATH."""
    with patch("shutil.which", return_value=None):
        with pytest.raises(ClickException) as exc_info:
            check_cli_tool("nonexistent-tool", "http://install.url")
        assert "nonexistent-tool CLI is not found in PATH" in str(exc_info.value)


def test_check_cli_tool_failed_with_diagnostics():
    """Test that check_cli_tool includes diagnostics on failure."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "some output"
    mock_result.stderr = "some error"

    with patch("shutil.which", return_value="/usr/bin/some-tool"), patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ClickException) as exc_info:
            check_cli_tool("some-tool", "http://install.url")
        assert "some-tool CLI found but" in str(exc_info.value)
        assert "exit code 1" in str(exc_info.value)
        assert "stdout: some output" in str(exc_info.value)
        assert "stderr: some error" in str(exc_info.value)


def test_check_gemini_cli_override():
    """Test that AUTOCODER_GEMINI_CLI override works."""
    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch.dict(os.environ, {"AUTOCODER_GEMINI_CLI": "/path/to/custom-gemini"}), patch("subprocess.run", return_value=mock_result) as mock_run, patch("click.echo") as mock_echo:
        check_gemini_cli_or_fail()
        # Verify it called the custom path
        mock_run.assert_called_with(["/path/to/custom-gemini", "--version"], capture_output=True, text=True, timeout=10)
        assert mock_echo.call_count >= 1
        # Check if "Using gemini CLI (override: /path/to/custom-gemini)" was echoed
        # Note: click.echo might be called multiple times, we just want to see if our message is there.
        # But we changed it to click.echo in my latest fix.
        args, _ = mock_echo.call_args
        assert "/path/to/custom-gemini" in args[0]


def test_gemini_client_init_with_override():
    """Test GeminiClient initialization with CLI override."""
    mock_result = MagicMock()
    mock_result.returncode = 0

    with patch.dict(os.environ, {"AUTOCODER_GEMINI_CLI": "custom-gemini-v2"}), patch("subprocess.run", return_value=mock_result) as mock_run, patch("shutil.which", return_value=None):  # Even if not in PATH, override should work
        client = GeminiClient()
        # Verify it called the custom path with --version
        mock_run.assert_any_call(["custom-gemini-v2", "--version"], capture_output=True, text=True, timeout=10)


def test_gemini_client_init_failure_diagnostics():
    """Test GeminiClient initialization failure with diagnostics."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "perm denied"
    mock_result.stderr = "crit error"

    with patch("shutil.which", return_value="/bin/gemini"), patch("subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError) as exc_info:
            GeminiClient()
        assert "Gemini CLI (gemini) found but version check failed" in str(exc_info.value)
        assert "stdout: perm denied" in str(exc_info.value)
        assert "stderr: crit error" in str(exc_info.value)
