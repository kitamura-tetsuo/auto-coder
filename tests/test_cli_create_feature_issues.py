"""
Tests for create-feature-issues CLI command.
"""

from unittest.mock import Mock, patch

import click
from click.testing import CliRunner

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.cli import create_feature_issues


class TestCLICreateFeatureIssues:
    """Test cases for create-feature-issues CLI command."""

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_create_feature_issues_codex_with_model_warns_and_ignores(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """When backend=codex and --model is provided for create-feature-issues, warn and do not print Using model."""
        mock_github_client_class.return_value = Mock()
        mock_codex_client_class.return_value = Mock()
        automation_engine = Mock()
        automation_engine.create_feature_issues.return_value = []
        mock_automation_engine_class.return_value = automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            create_feature_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--model-gemini",
                "some-model",
            ],
        )

        assert result.exit_code == 0
        assert "Using backends: codex (default: codex)" in result.output
        assert "Warning:" not in result.output
        assert "Using model:" not in result.output

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_create_feature_issues_success_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """create-feature-issues uses codex by default."""
        mock_github_client = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client = Mock()
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine = Mock()
        mock_automation_engine.create_feature_issues.return_value = []
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            create_feature_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
            ],
        )

        assert result.exit_code == 0
        mock_github_client_class.assert_called_once_with("test_token")
        mock_codex_client_class.assert_called_once_with(model_name="codex")
        assert mock_automation_engine_class.call_count == 1
        args, kwargs = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        manager = args[1]
        assert isinstance(manager, BackendManager)
        assert manager._default_backend == "codex"
        assert manager._all_backends == ["codex"]
        mock_automation_engine.create_feature_issues.assert_called_once_with(
            "test/repo"
        )

    def test_create_feature_issues_missing_github_token(self):
        """Test create-feature-issues command with missing GitHub token."""
        runner = CliRunner()

        result = runner.invoke(create_feature_issues, ["--repo", "test/repo"])

        assert result.exit_code != 0
        assert "GitHub token is required" in result.output

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    def test_create_feature_issues_missing_codex_cli(self, mock_check_cli, mock_initialize_graphrag):
        """Default backend codex missing should error."""
        mock_check_cli.side_effect = click.ClickException("Codex CLI missing")
        runner = CliRunner()

        result = runner.invoke(
            create_feature_issues,
            ["--repo", "test/repo", "--github-token", "test_token"],
        )

        assert result.exit_code != 0
        assert "Codex CLI" in result.output

    @patch.dict("os.environ", {"GITHUB_TOKEN": "env_github_token"})
    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_create_feature_issues_with_env_vars_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """create-feature-issues uses env GITHUB_TOKEN and default codex backend."""
        # Setup
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_automation_engine = Mock()
        mock_automation_engine.create_feature_issues.return_value = []

        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()

        # Execute
        result = runner.invoke(create_feature_issues, ["--repo", "test/repo"])

        # Assert
        assert result.exit_code == 0
        mock_github_client_class.assert_called_once_with("env_github_token")
        mock_codex_client_class.assert_called_once()

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.GeminiClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_create_feature_issues_backend_gemini_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """When backend=gemini, model is passed for create-feature-issues."""
        mock_github_client_class.return_value = Mock()
        mock_gemini_client_class.return_value = Mock()
        automation_engine = Mock()
        automation_engine.create_feature_issues.return_value = []
        mock_automation_engine_class.return_value = automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            create_feature_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--backend",
                "gemini",
                "--model-gemini",
                "gemini-custom",
            ],
        )

        assert result.exit_code == 0
        mock_gemini_client_class.assert_called_once_with(model_name="gemini-custom")

