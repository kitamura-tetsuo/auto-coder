import pytest
from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_run_qwen_cli_alias_delegates_and_returns_output(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = ["alias line 1\n", "alias line 2\n"]
            self.stdout = iter(self._lines)
        def wait(self):
            return 0

    mock_popen.return_value = DummyPopen()

    client = QwenClient(model_name="qwen3-coder-plus")
    out = client._run_qwen_cli("probe")

    assert "alias line 1" in out and "alias line 2" in out

