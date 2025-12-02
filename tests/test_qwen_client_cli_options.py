from unittest.mock import patch

import pytest

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@pytest.mark.skip(reason="Tests deprecated QwenClient codex fallback - QwenClient now only uses OAuth path")
@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_options_parameter(mock_run_command, mock_run):
    """Test that options parameter is properly passed to CLI commands."""
    # Pretend both codex and qwen --version work
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    # Test with options parameter in codex mode
    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
        options=["-o", "yolo", "true", "--debug"],
    )

    _ = client._run_qwen_cli("probe", None)

    # Ensure codex command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Check that codex CLI is used with proper flags
    assert args[0] == "codex"
    assert "exec" in args

    # Check that custom options are included in the command
    assert "-o" in args
    assert "yolo" in args
    assert "true" in args
    assert "--debug" in args

    # Check environment variables are set
    kwargs = mock_run_command.call_args.kwargs
    env = kwargs["env"]
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
@patch("src.auto_coder.qwen_client.get_llm_config")
def test_qwen_client_options_parameter_qwen_mode(mock_get_config, mock_run_command, mock_run):
    """Test that options parameter is properly passed to qwen OAuth CLI commands."""
    # Pretend qwen --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    # Mock config to provide options
    from unittest.mock import Mock

    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.model = "qwen3-coder-plus"
    mock_backend_config.options = ["-o", "yolo", "true", "--debug"]
    mock_backend_config.api_key = None
    mock_backend_config.base_url = None
    mock_backend_config.openai_api_key = None
    mock_backend_config.openai_base_url = None
    mock_backend_config.validate_required_options.return_value = []
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config

    # Test with options from config in qwen OAuth mode (no API key)
    client = QwenClient(
        backend_name="qwen",
    )

    _ = client._run_qwen_cli("probe", None)

    # Ensure qwen command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Check that qwen CLI is used
    assert args[0] == "qwen"
    assert "-y" in args

    # Check that custom options are included in the command
    assert "-o" in args
    assert "yolo" in args
    assert "true" in args
    assert "--debug" in args


@pytest.mark.skip(reason="Tests deprecated QwenClient codex fallback - QwenClient now only uses OAuth path")
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

    _ = client._run_qwen_cli("probe", None)

    # Ensure codex command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Check that codex CLI is used with proper flags
    assert args[0] == "codex"
    assert "exec" in args

    # Check environment variables are set
    kwargs = mock_run_command.call_args.kwargs
    env = kwargs["env"]
    assert env.get("OPENAI_API_KEY") == "sk-test-123"
    assert env.get("OPENAI_BASE_URL") == "https://api.example.com"


@pytest.mark.skip(reason="Tests deprecated QwenClient codex fallback - QwenClient now only uses OAuth path")
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

    _ = client._run_qwen_cli("probe", None)

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


@pytest.mark.skip(reason="Tests deprecated QwenClient codex fallback - QwenClient now only uses OAuth path")
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

    _ = client._run_qwen_cli("probe", None)

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


@pytest.mark.skip(reason="Tests deprecated QwenClient codex fallback - QwenClient now only uses OAuth path")
@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_codex_mode_with_options(mock_run_command, mock_run):
    """Test that options are passed to codex CLI when API key and base URL are provided."""
    # Pretend codex --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        model_name="qwen3-coder-plus",
        openai_api_key="sk-test-123",
        openai_base_url="https://api.example.com",
        use_env_vars=True,
        options=["-o", "yolo", "true", "--debug"],
    )

    _ = client._run_qwen_cli("probe", None)

    # Ensure codex command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]

    # Check that custom options are included in the command
    assert "-o" in args
    assert "yolo" in args
    assert "true" in args
    assert "--debug" in args


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
@patch("src.auto_coder.qwen_client.get_llm_config")
def test_qwen_client_oauth_mode_with_options(mock_get_config, mock_run_command, mock_run):
    """Test that options are passed to qwen OAuth CLI when no API key is provided."""
    # Pretend qwen --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    # Mock config to provide options
    from unittest.mock import Mock

    mock_config = Mock()
    mock_backend_config = Mock()
    mock_backend_config.model = "qwen3-coder-plus"
    mock_backend_config.options = ["-o", "stream", "false"]
    mock_backend_config.api_key = None
    mock_backend_config.base_url = None
    mock_backend_config.openai_api_key = None
    mock_backend_config.openai_base_url = None
    mock_backend_config.validate_required_options.return_value = []
    mock_config.get_backend_config.return_value = mock_backend_config
    mock_get_config.return_value = mock_config

    client = QwenClient(
        backend_name="qwen",
        use_env_vars=True,
    )

    _ = client._run_qwen_cli("probe", None)

    # Ensure qwen command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]
    assert args[0] == "qwen"
    assert "-y" in args

    # Check that custom options are included in the command
    assert "-o" in args
    assert "stream" in args
    assert "false" in args


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_options_empty_by_default(mock_run_command, mock_run):
    """Test that no extra options are passed when options list is empty."""
    # Pretend qwen --version works
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "done\n", "", 0)

    client = QwenClient(
        backend_name="qwen",
        use_env_vars=True,
        # No options provided
    )

    _ = client._run_qwen_cli("probe", None)

    # Ensure qwen command is used
    assert mock_run_command.call_count == 1
    args = mock_run_command.call_args[0][0]
    assert args[0] == "qwen"
    assert "-y" in args

    # Check that no custom options are in the command (beyond defaults)
    # The command should only have: qwen, -y, -m, model, -p, prompt
    command_str = " ".join(args)
    # Should not contain custom option flags
    assert "-o" not in command_str or "stream" not in args
