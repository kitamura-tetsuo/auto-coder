from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_run_qwen_cli_alias_delegates_and_returns_output(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(
        True, "alias line 1\nalias line 2\n", "", 0
    )

    client = QwenClient(model_name="qwen3-coder-plus")
    out = client._run_qwen_cli("probe")

    assert "alias line 1" in out and "alias line 2" in out

    # Verify qwen CLI is used (OAuth, no API key)
    args = mock_run_command.call_args[0][0]
    assert args[0] == "qwen"
