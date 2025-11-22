from unittest.mock import patch

import pytest

from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.utils import CommandResult


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_streaming_detects_usage_limit_and_aborts_early(mock_run_command, mock_run, _use_custom_subprocess_mock):
    # Mock subprocess.run for __init__ to bypass CLI check
    mock_run.return_value = _FakeCompleted(returncode=0)
    # Simulate a usage limit error in stderr for the actual command
    mock_run_command.return_value = CommandResult(
        success=False,
        stdout="",
        stderr='data: {"error": {"code": 429}}\nstatus: RESOURCE_EXHAUSTED\n',
        returncode=1,
    )
    client = GeminiClient(model_name="gemini-2.5-pro")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hello")
