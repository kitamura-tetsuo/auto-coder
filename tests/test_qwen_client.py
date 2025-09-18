"""
Tests for Qwen client functionality.
"""
from unittest.mock import patch

import pytest

from src.auto_coder.qwen_client import QwenClient


class TestQwenClient:
    @patch("subprocess.run")
    def test_init_checks_cli(self, mock_run):
        mock_run.return_value.returncode = 0
        client = QwenClient()
        assert client.model_name.startswith("qwen")

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_run_prompt_success(self, mock_popen, mock_run):
        mock_run.return_value.returncode = 0

        class DummyPopen:
            def __init__(self):
                self._lines = ["ok line 1\n", "ok line 2\n"]
                self.stdout = iter(self._lines)
            def wait(self):
                return 0
        mock_popen.return_value = DummyPopen()

        client = QwenClient(model_name="qwen3-coder-plus")
        out = client._run_qwen_cli("hello")
        assert "ok line 1" in out and "ok line 2" in out

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_run_prompt_failure_nonzero(self, mock_popen, mock_run):
        mock_run.return_value.returncode = 0

        class DummyPopen:
            def __init__(self):
                self._lines = [""]
                self.stdout = iter(self._lines)
            def wait(self):
                return 2
        mock_popen.return_value = DummyPopen()

        client = QwenClient()
        with pytest.raises(RuntimeError):
            client._run_qwen_cli("oops")

