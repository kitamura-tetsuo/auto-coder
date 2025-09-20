from unittest.mock import patch
import pytest

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.codex_client import CodexClient
from src.auto_coder.exceptions import AutoCoderUsageLimitError


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_qwen_raises_usage_limit_on_message(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["Some output...", "Rate limit exceeded", "please try later"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 0
    mock_popen.return_value = DummyPopen()

    client = QwenClient(model_name="Qwen2.5-Coder-32B-Instruct")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello")


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_qwen_raises_usage_limit_on_nonzero_429(mock_popen, mock_run):
    mock_run.return_value.returncode = 0
    class DummyPopen:
        def __init__(self):
            self._lines = ["HTTP 429 too many requests"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 1
    mock_popen.return_value = DummyPopen()

    client = QwenClient(model_name="Qwen2.5-Coder-32B-Instruct")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello")


@patch("subprocess.Popen")
def test_gemini_raises_usage_limit_on_nonzero_429(mock_popen):
    class DummyPopen:
        def __init__(self):
            self._lines = ["Error 429: Quota exceeded"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 2
    mock_popen.return_value = DummyPopen()

    client = GeminiClient(model_name="gemini-2.5-pro")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hi")


@patch("subprocess.Popen")
def test_gemini_raises_usage_limit_on_message_even_zero(mock_popen):
    class DummyPopen:
        def __init__(self):
            self._lines = ["Quota reached for this project"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 0
    mock_popen.return_value = DummyPopen()

    client = GeminiClient(model_name="gemini-2.5-pro")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hi")



@patch("subprocess.run")
@patch("subprocess.Popen")
def test_codex_raises_usage_limit_on_message_even_zero(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["Some output", "quota exceeded", "retry later"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 0
    mock_popen.return_value = DummyPopen()

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hello")


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_codex_raises_usage_limit_on_nonzero_429(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["HTTP 429 Too Many Requests"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 1
    mock_popen.return_value = DummyPopen()

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hello")



@patch("subprocess.run")
@patch("subprocess.Popen")
def test_codex_raises_usage_limit_on_message_even_zero(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["Some output", "quota exceeded", "retry later"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 0
    mock_popen.return_value = DummyPopen()

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hello")


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_codex_raises_usage_limit_on_nonzero_429(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["HTTP 429 Too Many Requests"]
            self.stdout = iter(l + "\n" for l in self._lines)
        def wait(self):
            return 1
    mock_popen.return_value = DummyPopen()

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hello")

