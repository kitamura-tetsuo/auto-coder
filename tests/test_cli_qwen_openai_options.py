from click.testing import CliRunner
from unittest.mock import Mock, patch

from src.auto_coder.cli import process_issues


@patch("src.auto_coder.cli.check_qwen_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.QwenClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_process_issues_qwen_receives_openai_options(
    mock_github_client_class,
    mock_qwen_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    mock_github_client_class.return_value = Mock()
    mock_qwen_client_class.return_value = Mock()
    mock_automation_engine_class.return_value = Mock()
    mock_check_cli.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        process_issues,
        [
            "--repo",
            "owner/repo",
            "--github-token",
            "token",
            "--backend",
            "qwen",
            "--model-qwen",
            "qwen3-coder-plus",
            "--openai-api-key",
            "sk-cli-xyz",
            "--openai-base-url",
            "https://api.local",
        ],
    )

    assert result.exit_code == 0
    # Verify QwenClient was constructed with the OpenAI options
    assert mock_qwen_client_class.call_count == 1
    _, kwargs = mock_qwen_client_class.call_args
    assert kwargs.get("model_name") == "qwen3-coder-plus"
    assert kwargs.get("openai_api_key") == "sk-cli-xyz"
    assert kwargs.get("openai_base_url") == "https://api.local"

