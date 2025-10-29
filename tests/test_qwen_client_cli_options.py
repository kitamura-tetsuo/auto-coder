from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_cli_options_mode(mock_run_command, mock_run):
    """Test that codex CLI is used when API key and base URL are provided."""
    # Pretend codex --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
        use_env_vars=False,  # Use CLI options instead of env vars
    )

    _ = client._run_qwen_cli("probe")

    # Ensure codex command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Check that codex CLI is used with proper flags
    assert args[0] == "codex"
    assert "exec" in args
    assert "-s" in args
    assert "workspace-write" in args
    assert "--dangerously-bypass-approvals-and-sandbox" in args

    # Check environment variables are set
    kwargs = mock_run_command.call_args.kwargs
    env = kwargs["env"]
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_preserve_env_mode(mock_run_command, mock_run):
    """Test that codex CLI is used with environment variables when API key is provided."""
    # Pretend codex --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
        use_env_vars=True,
        preserve_existing_env=True,  # Preserve existing env vars
    )

    _ = client._run_qwen_cli("probe")

    # Ensure codex command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]
    assert args[0] == "codex"

    # Ensure env passed to run_command contains the injected values
    kwargs = mock_run_command.call_args.kwargs
    assert "env" in kwargs
    env = kwargs["env"]

    # New values should be set
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_default_env_mode(mock_run_command, mock_run):
    """Test default behavior with codex CLI when API key is provided."""
    # Pretend codex --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
        # use_env_vars=True (default)
        # preserve_existing_env=False (default)
    )

    _ = client._run_qwen_cli("probe")

    # Ensure codex command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]
    assert args[0] == "codex"
    assert "exec" in args

    # Ensure env passed to run_command contains the injected values
    kwargs = mock_run_command.call_args.kwargs
    assert "env" in kwargs
    env = kwargs["env"]

    # Values should be set via env vars
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"

