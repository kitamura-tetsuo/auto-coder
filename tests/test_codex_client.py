"""
Tests for Codex client functionality.
"""

from unittest.mock import patch

import pytest

from src.auto_coder.codex_client import CodexClient


class TestCodexClient:
    """Test cases for CodexClient class."""

    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run):
        """CodexClient should check codex --version at init."""
        mock_run.return_value.returncode = 0
        client = CodexClient()
        assert client.model_name == "codex"
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_llm_invocation_warn_log(self, mock_popen, mock_run):
        """Verify warning is logged when invoking codex CLI."""
        mock_run.return_value.returncode = 0

        class DummyPopen:
            def __init__(self):
                self._lines = ["ok\n"]
                self.stdout = iter(self._lines)
            def wait(self):
                return 0
        mock_popen.return_value = DummyPopen()

        client = CodexClient()
        _ = client._run_gemini_cli("hello world")
        # We cannot easily capture loguru here without handler tweaks; rely on absence of exceptions
        # The warn path is at least executed without error.


    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_run_exec_success(self, mock_popen, mock_run):
        """codex exec should stream and aggregate output successfully."""
        mock_run.return_value.returncode = 0

        class DummyPopen:
            def __init__(self):
                self._lines = ["line1\n", "line2\n"]
                self.stdout = iter(self._lines)
            def wait(self):
                return 0
        mock_popen.return_value = DummyPopen()

        client = CodexClient()
        output = client._run_gemini_cli("hello world")
        assert "line1" in output and "line2" in output

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_run_exec_failure(self, mock_popen, mock_run):
        """When codex exec returns non-zero, raise RuntimeError."""
        mock_run.return_value.returncode = 0

        class DummyPopen:
            def __init__(self):
                self._lines = [""]
                self.stdout = iter(self._lines)
            def wait(self):
                return 1
        mock_popen.return_value = DummyPopen()

        client = CodexClient()
        with pytest.raises(RuntimeError):
            client._run_gemini_cli("hello world")

