import threading
from unittest.mock import patch

import pytest

from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.gemini_client import GeminiClient


class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)
        self._idx = 0
        self._killed = False
        self._lock = threading.Lock()

    def readline(self):
        with self._lock:
            if self._killed:
                return ""
            if self._idx < len(self._lines):
                line = self._lines[self._idx]
                self._idx += 1
                return line
            return ""

    def close(self):
        with self._lock:
            self._killed = True


class _FakePopen:
    def __init__(self, cmd, stdout=None, stderr=None, text=True, bufsize=1, cwd=None, env=None):
        # Provide a few lines to simulate Gemini streaming 429/RESOURCE_EXHAUSTED
        self.stdout = _FakeStream(
            [
                "config: {...}\n",
                "some info line\n",
                "done prelude\n",
            ]
        )
        self.stderr = _FakeStream(
            [
                'data: {"error": {"code": 429}}\n',
                "status: RESOURCE_EXHAUSTED\n",
            ]
        )
        self._killed = False
        self.returncode = None

    def poll(self):
        # When killed, pretend process is finished with non-zero code
        if self._killed:
            self.returncode = 1
            return self.returncode
        # If both streams are exhausted, complete successfully
        out_done = self.stdout._idx >= len(self.stdout._lines)
        err_done = self.stderr._idx >= len(self.stderr._lines)
        if out_done and err_done:
            self.returncode = 0
            return self.returncode
        return None

    def kill(self):
        self._killed = True
        self.stdout._killed = True
        self.stderr._killed = True

    def send_signal(self, sig):
        # No-op for test
        pass

    def wait(self, timeout=None):
        # Return current code or non-zero when killed
        if self.returncode is None:
            self.returncode = 1 if self._killed else 0
        return self.returncode


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="gemini 0.0.0", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@patch("subprocess.run")
@patch("subprocess.Popen", new=_FakePopen)
def test_streaming_detects_usage_limit_and_aborts_early(mock_run):
    mock_run.return_value = _FakeCompleted()
    client = GeminiClient(backend_name="gemini")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hello")
