from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_env_injection_for_openai(mock_run_command, mock_run):
    # Pretend qwen --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
    )

    _ = client._run_qwen_cli("probe")

    # Ensure env passed to run_command contains the injected values
    assert mock_run_command.call_count == 1
    kwargs = mock_run_command.call_args.kwargs
    assert "env" in kwargs
    env = kwargs["env"]
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"
    # Also ensure OPENAI_MODEL is set when model_name provided
    assert env.get("OPENAI_MODEL") == "qwen3-coder-plus"
