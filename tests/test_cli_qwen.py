from click.testing import CliRunner
from unittest.mock import Mock, patch

from src.auto_coder.cli import process_issues, create_feature_issues, fix_to_pass_tests_command


@patch("src.auto_coder.cli.check_qwen_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.QwenClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_process_issues_backend_qwen_prints_model_and_uses_qwen(
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
            "test/repo",
            "--github-token",
            "token",
            "--backend",
            "qwen",
            "--model",
            "qwen3-coder-plus",
        ],
    )

    assert result.exit_code == 0
    assert "Using backend: qwen" in result.output
    assert "Using model: qwen3-coder-plus" in result.output
    mock_qwen_client_class.assert_called_once()
    _, kwargs = mock_qwen_client_class.call_args
    assert kwargs.get("model_name") == "qwen3-coder-plus"


@patch("src.auto_coder.cli.check_qwen_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.QwenClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_create_feature_issues_backend_qwen(
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
        create_feature_issues,
        [
            "--repo",
            "test/repo",
            "--github-token",
            "token",
            "--backend",
            "qwen",
            "--model",
            "qwen3-coder-plus",
        ],
    )

    assert result.exit_code == 0
    assert "Using backend: qwen" in result.output


@patch("src.auto_coder.cli.check_qwen_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.QwenClient")
def test_fix_to_pass_tests_backend_qwen(
    mock_qwen_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    engine = Mock()
    engine.fix_to_pass_tests.return_value = {"success": True, "attempts": 1}
    mock_automation_engine_class.return_value = engine
    mock_check_cli.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        fix_to_pass_tests_command,
        ["--backend", "qwen", "--model", "qwen3-coder-plus"],
    )

    assert result.exit_code == 0
    assert "Using backend: qwen" in result.output
    assert "Using model: qwen3-coder-plus" in result.output

