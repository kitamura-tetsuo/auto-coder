from unittest.mock import patch

import pytest

from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.codex_client import CodexClient


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_gemini_client_run_llm_cli_delegates(mock_popen, mock_run, mock_gemini_api_key):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["gem ok\n"]
            self.stdout = iter(self._lines)
        def wait(self):
            return 0

    mock_popen.return_value = DummyPopen()

    client = GeminiClient(mock_gemini_api_key, model_name="gemini-2.5-pro")
    out = client._run_llm_cli("hello")
    assert "gem ok" in out


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_qwen_client_run_llm_cli_delegates(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self, *args, **kwargs):
            self._lines = ["qwen ok\n"]
            self.stdout = iter(self._lines)
        def wait(self):
            return 0

    mock_popen.return_value = DummyPopen()

    client = QwenClient(model_name="qwen3-coder-plus")
    out = client._run_llm_cli("hello")
    assert "qwen ok" in out


@patch("subprocess.Popen")
def test_codex_client_run_llm_cli_delegates(mock_popen):
    class DummyPopen:
        def __init__(self, *args, **kwargs):
            self._lines = ["codex ok\n"]
            self.stdout = iter(self._lines)
        def wait(self):
            return 0

    mock_popen.return_value = DummyPopen()

    client = CodexClient(model_name="codex")
    out = client._run_llm_cli("hello")
    assert "codex ok" in out

