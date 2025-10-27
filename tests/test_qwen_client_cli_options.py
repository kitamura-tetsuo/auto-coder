from unittest.mock import patch

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_cli_options_mode(mock_run_command, mock_run):
    """Test that credentials are passed via CLI options when use_env_vars=False."""
    # Pretend qwen --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
        use_env_vars=False,  # Use CLI options instead of env vars
    )

    _ = client._run_qwen_cli("probe")

    # Ensure command includes CLI options
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Check that CLI options are present
    assert "--openai-api-key" in args
    assert "sk-test-123" in args
    assert "--openai-base-url" in args
    assert "https://api.example.com" in args
    assert "-m" in args
    assert "qwen3-coder-plus" in args


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_preserve_env_mode(mock_run_command, mock_run):
    """Test that existing env vars are preserved when preserve_existing_env=True."""
    # Pretend qwen --version works
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

    # Ensure env passed to run_command contains the injected values
    assert mock_run_command.call_count == 1
    kwargs = mock_run_command.call_args.kwargs
    assert "env" in kwargs
    env = kwargs["env"]
    
    # New values should be set
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"
    assert env.get("OPENAI_MODEL") == "qwen3-coder-plus"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_default_env_mode(mock_run_command, mock_run):
    """Test default behavior (use_env_vars=True, preserve_existing_env=False)."""
    # Pretend qwen --version works
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

    # Ensure env passed to run_command contains the injected values
    assert mock_run_command.call_count == 1
    kwargs = mock_run_command.call_args.kwargs
    assert "env" in kwargs
    env = kwargs["env"]
    
    # Values should be set via env vars
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"
    assert env.get("OPENAI_MODEL") == "qwen3-coder-plus"
    
    # Command should still have model flag
    args = mock_run_command.call_args[0][0]
    assert "-m" in args
    assert "qwen3-coder-plus" in args

