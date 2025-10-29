from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_run_prompt_includes_model_flag_when_model_set(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "", "", 0)

    client = QwenClient(model_name="qwen3-coder-plus")
    _ = client._run_qwen_cli("hello world")

    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Verify qwen CLI is used (OAuth, no API key)
    assert args[0] == "qwen"
    assert "-m" in args and "qwen3-coder-plus" in args
    assert "-p" in args
    # The prompt should be present as a separate argument
    assert "hello world" in args
