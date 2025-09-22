from unittest.mock import patch

import pytest

from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.codex_client import CodexClient
from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.utils import CommandResult


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_raises_usage_limit_on_message(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "Some output...\nRate limit exceeded\nplease try later\n", "", 0)

    client = QwenClient(model_name="Qwen2.5-Coder-32B-Instruct")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello")


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_raises_usage_limit_on_nonzero_429(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(False, "HTTP 429 too many requests", "", 1)

    client = QwenClient(model_name="Qwen2.5-Coder-32B-Instruct")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello")


@patch("subprocess.run")
@patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
def test_gemini_raises_usage_limit_on_nonzero_429(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(False, "Error 429: Quota exceeded", "", 2)

    client = GeminiClient(model_name="gemini-2.5-pro")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hi")


@patch("subprocess.run")
@patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
def test_gemini_raises_usage_limit_on_message_even_zero(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "Quota reached for this project", "", 0)

    client = GeminiClient(model_name="gemini-2.5-pro")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hi")


@patch("subprocess.run")
@patch("src.auto_coder.codex_client.CommandExecutor.run_command")
def test_codex_raises_usage_limit_on_message_even_zero(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "Some output\nquota exceeded\nretry later", "", 0)

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hello")


@patch("subprocess.run")
@patch("src.auto_coder.codex_client.CommandExecutor.run_command")
def test_codex_raises_usage_limit_on_nonzero_429(mock_run_command, mock_run):
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(False, "HTTP 429 Too Many Requests", "", 1)

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_gemini_cli("hello")
