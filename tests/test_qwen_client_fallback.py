from __future__ import annotations

from unittest.mock import patch

import pytest

from src.auto_coder.exceptions import AutoCoderUsageLimitError
from src.auto_coder.qwen_client import QwenClient
from src.auto_coder.utils import CommandResult
from tests.support.env import patch_environment


@pytest.mark.skip(reason="Tests deprecated QwenClient provider chain - provider rotation now handled by BackendManager")
@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_prefers_configured_api_keys_before_oauth(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    config_path = tmp_path / "qwen-providers.toml"
    config_path.write_text(
        """
        [[qwen.providers]]
        name = "modelstudio"
        api_key = "dashscope-xyz"

        [[qwen.providers]]
        name = "openrouter"
        api_key = "openrouter-123"
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    env = {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
    with patch_environment(env):
        mock_run_command.return_value = CommandResult(True, "ModelStudio OK", "", 0)

        client = QwenClient(model_name="qwen3-coder-plus")
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

    # After a successful call the active provider index should remain at zero.
    assert client._active_provider_index == 0
    assert client.model_name == "qwen3-coder-plus"


@pytest.mark.skip(reason="Tests deprecated QwenClient provider chain - provider rotation now handled by BackendManager")
@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_fallback_to_openrouter(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    config_path = tmp_path / "qwen-providers.toml"
    config_path.write_text(
        """
        [[qwen.providers]]
        name = "modelstudio"
        api_key = "dashscope-xyz"

        [[qwen.providers]]
        name = "openrouter"
        api_key = "openrouter-123"
        model = "qwen/qwen3-coder:free"
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    env = {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
    with patch_environment(env):
        mock_run_command.side_effect = [
            CommandResult(False, "Rate limit", "", 1),
            CommandResult(True, "OpenRouter OK", "", 0),
        ]

        client = QwenClient(model_name="qwen3-coder-plus")
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


@pytest.mark.skip(reason="Tests deprecated QwenClient provider chain - provider rotation now handled by BackendManager")
@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_fallbacks_to_oauth_after_api_keys(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    config_path = tmp_path / "qwen-providers.toml"
    config_path.write_text(
        """
        [[qwen.providers]]
        name = "modelstudio"
        api_key = "dashscope-xyz"

        [[qwen.providers]]
        name = "openrouter"
        api_key = "openrouter-123"
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    env = {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
    with patch_environment(env):
        mock_run_command.side_effect = [
            CommandResult(False, "Rate limit", "", 1),
            CommandResult(False, "Rate limit", "", 1),
            CommandResult(True, "OAuth OK", "", 0),
        ]

        client = QwenClient(model_name="qwen3-coder-plus")
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
    assert client._active_provider_index == 2


@pytest.mark.skip(reason="Tests deprecated QwenClient provider chain - provider rotation now handled by BackendManager")
@patch("subprocess.run")
@patch("src.auto_coder.qwen_client.CommandExecutor.run_command")
def test_qwen_client_all_limits_raise(mock_run_command, mock_run, tmp_path) -> None:
    mock_run.return_value.returncode = 0
    config_path = tmp_path / "qwen-providers.toml"
    config_path.write_text(
        """
        [[qwen.providers]]
        name = "modelstudio"
        api_key = "dashscope-xyz"

        [[qwen.providers]]
        name = "openrouter"
        api_key = "openrouter-123"
        """.strip()
        + "\n",
        encoding="utf-8",
    )

    env = {"AUTO_CODER_QWEN_CONFIG": str(config_path)}
    with patch_environment(env):
        mock_run_command.side_effect = [
            CommandResult(False, "Rate limit", "", 1),
            CommandResult(False, "Rate limit", "", 1),
            CommandResult(False, "Rate limit", "", 1),
        ]

        client = QwenClient(model_name="qwen3-coder-plus")
        with pytest.raises(AutoCoderUsageLimitError):
            client._run_qwen_cli("hello")

    # Final call should come from OAuth with no API key present.
    final_env = mock_run_command.call_args_list[-1].kwargs["env"]
    assert "OPENAI_API_KEY" not in final_env
