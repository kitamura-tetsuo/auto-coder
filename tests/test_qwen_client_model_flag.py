import pytest
from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient


@patch("subprocess.run")
@patch("subprocess.Popen")
def test_run_prompt_includes_model_flag_when_model_set(mock_popen, mock_run):
    mock_run.return_value.returncode = 0

    class DummyPopen:
        def __init__(self):
            self._lines = []
            self.stdout = iter(self._lines)
        def wait(self):
            return 0

    mock_popen.return_value = DummyPopen()

    client = QwenClient(model_name="qwen3-coder-plus")
    _ = client._run_qwen_cli("hello world")

    # Verify Popen was called with -m <model> and -p <prompt>
    assert mock_popen.call_count == 1
    args = mock_popen.call_args[0][0]
    assert "-m" in args and "qwen3-coder-plus" in args
    assert "-p" in args
    # The prompt should be present as a separate argument
    assert "hello world" in args

