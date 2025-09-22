from click.testing import CliRunner
from unittest.mock import Mock, patch

from src.auto_coder.cli import (
    process_issues,
    create_feature_issues,
    fix_to_pass_tests_command,
)


@patch("src.auto_coder.cli.check_auggie_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.AuggieClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_process_issues_backend_auggie_defaults_to_gpt5(
    mock_github_client_class,
    mock_auggie_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    mock_github_client_class.return_value = Mock()
    mock_auggie_client_class.return_value = Mock()
    engine = Mock()
    mock_automation_engine_class.return_value = engine
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
            "auggie",
        ],
    )

    assert result.exit_code == 0
    assert "Using backends: auggie (default: auggie)" in result.output
    assert "Using model: GPT-5" in result.output
    mock_auggie_client_class.assert_called_once()
    _, kwargs = mock_auggie_client_class.call_args
    assert kwargs.get("model_name") == "GPT-5"
    assert engine.run.called


@patch("src.auto_coder.cli.check_auggie_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.AuggieClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_process_issues_backend_auggie_respects_model_override(
    mock_github_client_class,
    mock_auggie_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    mock_github_client_class.return_value = Mock()
    mock_auggie_client_class.return_value = Mock()
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
            "auggie",
            "--model",
            "custom-model",
        ],
    )

    assert result.exit_code == 0
    assert "Using model: custom-model" in result.output
    mock_auggie_client_class.assert_called_once()
    _, kwargs = mock_auggie_client_class.call_args
    assert kwargs.get("model_name") == "custom-model"


@patch("src.auto_coder.cli.check_auggie_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.AuggieClient")
@patch("src.auto_coder.cli.GitHubClient")
def test_create_feature_issues_backend_auggie(
    mock_github_client_class,
    mock_auggie_client_class,
    mock_automation_engine_class,
    mock_check_cli,
):
    github_client = Mock()
    engine = Mock()
    mock_github_client_class.return_value = github_client
    mock_auggie_client_class.return_value = Mock()
    mock_automation_engine_class.return_value = engine
    mock_check_cli.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        create_feature_issues,
        [
            "--repo",
            "owner/repo",
            "--github-token",
            "token",
            "--backend",
            "auggie",
        ],
    )

    assert result.exit_code == 0
    assert "Using backends: auggie (default: auggie)" in result.output
    assert "Using model: GPT-5" in result.output
    engine.create_feature_issues.assert_called_once()


@patch("src.auto_coder.cli.check_auggie_cli_or_fail")
@patch("src.auto_coder.cli.AutomationEngine")
@patch("src.auto_coder.cli.AuggieClient")
def test_fix_to_pass_tests_backend_auggie_default_model(
    mock_auggie_client_class,
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
        ["--backend", "auggie"],
    )

    assert result.exit_code == 0
    assert "Using backends: auggie (default: auggie)" in result.output
    assert "Using model: GPT-5" in result.output
    mock_auggie_client_class.assert_called_once()
    engine.fix_to_pass_tests.assert_called_once()

