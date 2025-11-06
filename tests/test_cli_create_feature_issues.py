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
    @patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup")
    def test_create_feature_issues_codex_with_model_warns_and_ignores(
        self,
        mock_check_github_sub_issue,
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
    @patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup")
    def test_create_feature_issues_success_default_codex(
        self,
        mock_check_github_sub_issue,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """create-feature-issues uses codex by default."""
        mock_github_client = Mock()
        mock_github_client_class.return_value = mock_github_client
        # Make get_instance return the same object
        mock_github_client_class.get_instance.return_value = mock_github_client
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
        # GitHubClient is now a singleton using get_instance()
        mock_github_client_class.get_instance.assert_called_once_with("test_token", disable_labels=False)
        mock_codex_client_class.assert_called_once_with(model_name="codex")
        assert mock_automation_engine_class.call_count == 1
        args, kwargs = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        # After refactor, AutomationEngine no longer receives the backend manager as a positional arg
        assert len(args) == 1
        mock_automation_engine.create_feature_issues.assert_called_once_with("test/repo")

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
    @patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup")
    def test_create_feature_issues_with_env_vars_default_codex(
        self,
        mock_check_github_sub_issue,
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
        # Make get_instance return the same object
        mock_github_client_class.get_instance.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()

        # Execute
        result = runner.invoke(create_feature_issues, ["--repo", "test/repo"])

        # Assert
        assert result.exit_code == 0
        # GitHubClient is now a singleton using get_instance()
        mock_github_client_class.get_instance.assert_called_once_with("env_github_token", disable_labels=False)
        mock_codex_client_class.assert_called_once()

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.GeminiClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    @patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup")
    def test_create_feature_issues_backend_gemini_custom_model(
        self,
        mock_check_github_sub_issue,
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

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    @patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup")
    def test_create_feature_issues_force_reindex_flag(
        self,
        mock_check_github_sub_issue,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """--force-reindex should call initialize_graphrag with force_reindex=True."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        automation_engine = Mock()
        automation_engine.create_feature_issues.return_value = []
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
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
                "--force-reindex",
            ],
        )

        assert result.exit_code == 0
        # Verify output
        assert "Force reindex: True" in result.output
        # Verify initialize_graphrag was called with force_reindex=True
        mock_initialize_graphrag.assert_called_once_with(force_reindex=True)

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    @patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup")
    def test_create_feature_issues_default_no_force_reindex(
        self,
        mock_check_github_sub_issue,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """Default should call initialize_graphrag with force_reindex=False."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        automation_engine = Mock()
        automation_engine.create_feature_issues.return_value = []
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
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
            ],
        )

        assert result.exit_code == 0
        # Verify output
        assert "Force reindex: False" in result.output
        # Verify initialize_graphrag was called with force_reindex=False
        mock_initialize_graphrag.assert_called_once_with(force_reindex=False)
