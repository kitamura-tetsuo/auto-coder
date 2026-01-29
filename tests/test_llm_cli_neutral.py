from unittest.mock import patch

from src.auto_coder.codex_client import CodexClient
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.gemini_client.shutil.which")
@patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
def test_gemini_client_run_llm_cli_delegates(mock_run_command, mock_which, mock_run, mock_gemini_api_key):
    mock_which.return_value = "/usr/bin/gemini"
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "gem ok\n", "", 0)

    client = GeminiClient(backend_name="gemini")
    out = client._run_llm_cli("hello")
    assert "gem ok" in out


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_run_llm_cli_delegates(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "qwen ok\n", "", 0)

    client = QwenClient(backend_name="qwen")
    out = client._run_llm_cli("hello")
    assert "qwen ok" in out

    # Verify qwen CLI is used (OAuth, no API key)
    args = mock_run_command.call_args[0][0]
    assert args[0] == "qwen"


@patch("subprocess.run")
@patch("src.auto_coder.codex_client.CommandExecutor.run_command")
def test_codex_client_run_llm_cli_delegates(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "codex ok\n", "", 0)

    client = CodexClient(backend_name="codex")
    out = client._run_llm_cli("hello")
    assert "codex ok" in out
