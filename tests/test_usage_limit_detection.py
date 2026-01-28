from unittest.mock import patch

import pytest

from src.auto_coder.codex_client import CodexClient
from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


@patch("src.auto_coder.gemini_client.get_llm_config")
@patch("src.auto_coder.qwen_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_qwen_raises_usage_limit_on_message(mock_run_command, mock_run, mock_qwen_config, mock_gemini_config):
    # Setup mock config to be empty to trigger default behavior
    mock_qwen_config.return_value.get_backend_config.return_value = None
    mock_gemini_config.return_value.get_backend_config.return_value = None

    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "Some output...\nRate limit exceeded\nplease try later\n", "", 0)

    client = QwenClient(backend_name="qwen")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello", None)


@patch("src.auto_coder.qwen_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_qwen_raises_usage_limit_on_nonzero_429(mock_run_command, mock_run, mock_config):
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(False, "HTTP 429 too many requests", "", 1)

    client = QwenClient(backend_name="qwen")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello", None)


@patch("src.auto_coder.gemini_client.shutil.which")
@patch("src.auto_coder.gemini_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_gemini_raises_usage_limit_on_nonzero_429(mock_run_command, mock_run, mock_config, mock_which, _use_custom_subprocess_mock):
    mock_which.return_value = "/usr/bin/gemini"
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(False, "Error 429: Too many requests", "", 2)

    client = GeminiClient(backend_name="gemini")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hi")


@patch("src.auto_coder.gemini_client.shutil.which")
@patch("src.auto_coder.gemini_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_gemini_raises_usage_limit_on_message_even_zero(mock_run_command, mock_run, mock_config, mock_which, _use_custom_subprocess_mock):
    mock_which.return_value = "/usr/bin/gemini"
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "Rate limit exceeded for this project", "", 0)

    client = GeminiClient(backend_name="gemini")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hi")


@patch("src.auto_coder.gemini_client.shutil.which")
@patch("src.auto_coder.gemini_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_gemini_raises_usage_limit_on_zero_with_429_only(mock_run_command, mock_run, mock_config, mock_which, _use_custom_subprocess_mock):
    mock_which.return_value = "/usr/bin/gemini"
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    # Exit code 0 but logs include 429 without explicit 'quota'/'rate limit'
    mock_run_command.return_value = CommandResult(True, "status: 429\nToo Many Requests\n", "", 0)

    client = GeminiClient(backend_name="gemini")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hi")


@patch("src.auto_coder.gemini_client.shutil.which")
@patch("src.auto_coder.gemini_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_gemini_raises_usage_limit_on_zero_with_resource_exhausted(mock_run_command, mock_run, mock_config, mock_which, _use_custom_subprocess_mock):
    mock_which.return_value = "/usr/bin/gemini"
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "error: RESOURCE_EXHAUSTED", "", 0)

    client = GeminiClient(backend_name="gemini")
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hi")


@patch("src.auto_coder.codex_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_codex_raises_usage_limit_on_message_even_zero(mock_run_command, mock_run, mock_config, _use_custom_subprocess_mock):
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(True, "Some output\nrate limit exceeded\nretry later", "", 0)

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hello")


@patch("src.auto_coder.codex_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_codex_raises_usage_limit_on_nonzero_429(mock_run_command, mock_run, mock_config, _use_custom_subprocess_mock):
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    mock_run_command.return_value = CommandResult(False, "HTTP 429 Too Many Requests", "", 1)

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_llm_cli("hello")


@patch("src.auto_coder.codex_client.get_llm_config")
@patch("subprocess.run")
@patch("src.auto_coder.utils.CommandExecutor.run_command")
def test_codex_raises_usage_limit_on_upgrade_to_pro_message(mock_run_command, mock_run, mock_config, _use_custom_subprocess_mock):
    mock_config.return_value.get_backend_config.return_value = None
    mock_run.return_value.returncode = 0
    message = "2025-09-24 16:13:47.530 | INFO     | auto_coder/utils.py:155 in _run_with_streaming - " "[2025-09-24T07:13:47] ERROR: You've hit your usage limit. Upgrade to Pro " "(https://openai.com/chatgpt/pricing) or try again in 2 hours 22 minutes."
    mock_run_command.return_value = CommandResult(False, "", message, 1)

    client = CodexClient()
    with pytest.raises(AutoCoderUsageLimitError) as excinfo:
        client._run_llm_cli("hello")

    assert "usage limit" in str(excinfo.value).lower()
