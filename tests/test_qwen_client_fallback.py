from __future__ import annotations

from unittest.mock import patch

import pytest

from src.auto_coder.backend_provider_manager import BackendProviderManager
from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult


def make_provider_manager(tmp_path, body: str) -> BackendProviderManager:
    metadata_path = tmp_path / "provider_metadata.toml"
    metadata_path.write_text(body.strip() + "\n", encoding="utf-8")
    return BackendProviderManager(str(metadata_path))


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_prefers_configured_api_keys_before_oauth(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    manager = make_provider_manager(
        tmp_path,
        """
        [qwen.modelstudio]
        command = "codex"
        description = "ModelStudio"
        OPENAI_API_KEY = "dashscope-xyz"
        OPENAI_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

        [qwen.openrouter]
        command = "codex"
        description = "OpenRouter"
        OPENAI_API_KEY = "openrouter-123"
        OPENAI_BASE_URL = "https://openrouter.ai/api/v1"
        """,
    )

    mock_run_command.return_value = CommandResult(True, "ModelStudio OK", "", 0)

    client = QwenClient(model_name="qwen3-coder-plus", provider_manager=manager)
    output = client._run_qwen_cli("hello")

    assert output == "ModelStudio OK"
    assert mock_run_command.call_count == 1

    # The first provider should use codex CLI with ModelStudio configuration
    first_cmd = mock_run_command.call_args_list[0][0][0]
    first_env = mock_run_command.call_args_list[0].kwargs["env"]

    # Verify codex command is used for providers with API keys
    assert first_cmd[0] == "codex"
    assert "exec" in first_cmd
    assert "-s" in first_cmd
    assert "workspace-write" in first_cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in first_cmd

    # Verify environment variables are set
    assert first_env["OPENAI_API_KEY"] == "dashscope-xyz"
    assert first_env["OPENAI_BASE_URL"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

    assert manager.get_last_used_provider_name("qwen") == "modelstudio"
    assert client.model_name == "qwen3-coder-plus"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_fallback_to_openrouter(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    manager = make_provider_manager(
        tmp_path,
        """
        [qwen.modelstudio]
        command = "codex"
        description = "ModelStudio"
        OPENAI_API_KEY = "dashscope-xyz"
        OPENAI_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

        [qwen.openrouter]
        command = "codex"
        description = "OpenRouter"
        OPENAI_API_KEY = "openrouter-123"
        OPENAI_BASE_URL = "https://openrouter.ai/api/v1"
        OPENAI_MODEL = "qwen/qwen3-coder:free"
        """,
    )

    mock_run_command.side_effect = [
        CommandResult(False, "Rate limit", "", 1),
        CommandResult(True, "OpenRouter OK", "", 0),
    ]

    client = QwenClient(model_name="qwen3-coder-plus", provider_manager=manager)
    output = client._run_qwen_cli("hello")

    assert output == "OpenRouter OK"
    assert mock_run_command.call_count == 2

    # Both calls should use codex CLI (providers have API keys)
    first_cmd = mock_run_command.call_args_list[0][0][0]
    second_cmd = mock_run_command.call_args_list[1][0][0]

    assert first_cmd[0] == "codex"
    assert second_cmd[0] == "codex"

    # First call should target ModelStudio, second OpenRouter.
    first_env = mock_run_command.call_args_list[0].kwargs["env"]
    assert first_env["OPENAI_API_KEY"] == "dashscope-xyz"

    second_env = mock_run_command.call_args_list[1].kwargs["env"]
    assert second_env["OPENAI_API_KEY"] == "openrouter-123"
    assert second_env["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"

    assert client.model_name == "qwen/qwen3-coder:free"
    assert manager.get_last_used_provider_name("qwen") == "openrouter"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_fallbacks_to_oauth_after_api_keys(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    manager = make_provider_manager(
        tmp_path,
        """
        [qwen.modelstudio]
        command = "codex"
        description = "ModelStudio"
        OPENAI_API_KEY = "dashscope-xyz"
        OPENAI_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

        [qwen.openrouter]
        command = "codex"
        description = "OpenRouter"
        OPENAI_API_KEY = "openrouter-123"
        OPENAI_BASE_URL = "https://openrouter.ai/api/v1"
        """,
    )

    mock_run_command.side_effect = [
        CommandResult(False, "Rate limit", "", 1),
        CommandResult(False, "Rate limit", "", 1),
        CommandResult(True, "OAuth OK", "", 0),
    ]

    client = QwenClient(model_name="qwen3-coder-plus", provider_manager=manager)
    output = client._run_qwen_cli("hello")

    assert output == "OAuth OK"
    assert mock_run_command.call_count == 3

    # First two calls should use codex (providers with API keys)
    first_cmd = mock_run_command.call_args_list[0][0][0]
    second_cmd = mock_run_command.call_args_list[1][0][0]
    third_cmd = mock_run_command.call_args_list[2][0][0]

    assert first_cmd[0] == "codex"
    assert second_cmd[0] == "codex"
    # Third call should use qwen (OAuth, no API key)
    assert third_cmd[0] == "qwen"

    first_env = mock_run_command.call_args_list[0].kwargs["env"]
    second_env = mock_run_command.call_args_list[1].kwargs["env"]
    third_env = mock_run_command.call_args_list[2].kwargs["env"]

    assert first_env["OPENAI_API_KEY"] == "dashscope-xyz"
    assert second_env["OPENAI_API_KEY"] == "openrouter-123"
    assert "OPENAI_API_KEY" not in third_env
    assert manager.get_last_used_provider_name("qwen") == "qwen-oauth"


@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_all_limits_raise(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    manager = make_provider_manager(
        tmp_path,
        """
        [qwen.modelstudio]
        command = "codex"
        description = "ModelStudio"
        OPENAI_API_KEY = "dashscope-xyz"
        OPENAI_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

        [qwen.openrouter]
        command = "codex"
        description = "OpenRouter"
        OPENAI_API_KEY = "openrouter-123"
        OPENAI_BASE_URL = "https://openrouter.ai/api/v1"
        """,
    )

    mock_run_command.side_effect = [
        CommandResult(False, "Rate limit", "", 1),
        CommandResult(False, "Rate limit", "", 1),
        CommandResult(False, "Rate limit", "", 1),
    ]

    client = QwenClient(model_name="qwen3-coder-plus", provider_manager=manager)
    with pytest.raises(AutoCoderUsageLimitError):
        client._run_qwen_cli("hello")

    # Final call should come from OAuth with no API key present.
    final_env = mock_run_command.call_args_list[-1].kwargs["env"]
    assert "OPENAI_API_KEY" not in final_env
