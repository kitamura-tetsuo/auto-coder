"""
Tests for CLI functionality.
"""

import click
from unittest.mock import Mock, patch
from click.testing import CliRunner

from src.auto_coder.cli import main, process_issues, create_feature_issues


class TestCLI:
    """Test cases for CLI functionality."""

    def test_main_command_help(self):
        """Test main command help output."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "Auto-Coder" in result.output
        assert "Automated application development" in result.output

    def test_main_command_version(self):
        """Test main command version output."""
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])

        assert result.exit_code == 0

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_success_normal_mode(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Test successful process-issues command with normal mode (default)."""
        mock_github_client = Mock()
        mock_gemini_client = Mock()
        mock_automation_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_gemini_client_class.return_value = mock_gemini_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0
        assert "Processing repository: test/repo" in result.output
        assert "Jules mode: False" in result.output
        assert "Dry run mode: True" in result.output

        mock_github_client_class.assert_called_once_with("test_token")
        mock_gemini_client_class.assert_called_once_with(
            model_name="gemini-2.5-pro"
        )
        mock_automation_engine_class.assert_called_once_with(
            mock_github_client, mock_gemini_client, dry_run=True
        )
        mock_automation_engine.run.assert_called_once_with("test/repo", jules_mode=False)

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_jules_mode(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Test process-issues command with jules mode."""
        mock_github_client = Mock()
        mock_gemini_client = Mock()
        mock_automation_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_gemini_client_class.return_value = mock_gemini_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo", "test/repo",
                "--github-token", "test-token",
                "--jules-mode"
            ]
        )

        assert result.exit_code == 0
        assert "Jules mode: True" in result.output

        # Verify clients were initialized correctly
        mock_github_client_class.assert_called_once_with("test-token")
        mock_gemini_client_class.assert_called_once_with(model_name="gemini-2.5-pro")

        # Verify automation engine was initialized correctly
        mock_automation_engine_class.assert_called_once_with(
            mock_github_client, mock_gemini_client, dry_run=False
        )

        # Verify run was called with jules_mode=True
        mock_automation_engine.run.assert_called_once_with("test/repo", jules_mode=True)

    def test_process_issues_missing_github_token(self):
        """Test process-issues command with missing GitHub token."""
        runner = CliRunner()

        result = runner.invoke(process_issues, ["--repo", "test/repo"])

        assert result.exit_code != 0
        assert "GitHub token is required" in result.output

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    def test_process_issues_missing_gemini_cli(self, mock_check_cli):
        """Test process-issues command when gemini CLI is not available."""
        mock_check_cli.side_effect = click.ClickException("Gemini CLI missing")
        runner = CliRunner()

        result = runner.invoke(
            process_issues,
            ["--repo", "test/repo", "--github-token", "test_token"],
        )

        assert result.exit_code != 0
        assert "Gemini CLI" in result.output

    @patch.dict("os.environ", {"GITHUB_TOKEN": "env_github_token"})
    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_with_env_vars(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Test process-issues command using environment variables."""
        # Setup
        mock_github_client = Mock()
        mock_gemini_client = Mock()
        mock_automation_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_gemini_client_class.return_value = mock_gemini_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()

        # Execute
        result = runner.invoke(process_issues, ["--repo", "test/repo"])

        # Assert
        assert result.exit_code == 0
        mock_github_client_class.assert_called_once_with("env_github_token")
        mock_gemini_client_class.assert_called_once()

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Custom model name is passed to GeminiClient for process-issues."""
        mock_github_client_class.return_value = Mock()
        mock_gemini_client_class.return_value = Mock()
        mock_automation_engine_class.return_value = Mock()
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--model",
                "gemini-custom",
            ],
        )

        assert result.exit_code == 0
        mock_gemini_client_class.assert_called_once_with(
            model_name="gemini-custom"
        )

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_success(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Test successful create-feature-issues command with default model."""
        mock_github_client = Mock()
        mock_gemini_client = Mock()
        mock_automation_engine = Mock()
        mock_automation_engine.create_feature_issues.return_value = [
            {
                "number": 123,
                "title": "New Feature",
                "url": "https://github.com/test/repo/issues/123",
            }
        ]

        mock_github_client_class.return_value = mock_github_client
        mock_gemini_client_class.return_value = mock_gemini_client
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
        assert (
            "Analyzing repository for feature opportunities: "
            "test/repo" in result.output
        )

        mock_github_client_class.assert_called_once_with("test_token")
        mock_gemini_client_class.assert_called_once_with(
            model_name="gemini-2.5-pro"
        )
        mock_automation_engine_class.assert_called_once_with(
            mock_github_client, mock_gemini_client
        )
        mock_automation_engine.create_feature_issues.assert_called_once_with(
            "test/repo"
        )


    def test_create_feature_issues_missing_github_token(self):
        """Test create-feature-issues command with missing GitHub token."""
        runner = CliRunner()

        result = runner.invoke(create_feature_issues, ["--repo", "test/repo"])

        assert result.exit_code != 0
        assert "GitHub token is required" in result.output

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    def test_create_feature_issues_missing_gemini_cli(self, mock_check_cli):
        """Test create-feature-issues when gemini CLI is not available."""
        mock_check_cli.side_effect = click.ClickException("Gemini CLI missing")
        runner = CliRunner()

        result = runner.invoke(
            create_feature_issues,
            ["--repo", "test/repo", "--github-token", "test_token"],
        )

        assert result.exit_code != 0
        assert "Gemini CLI" in result.output

    @patch.dict("os.environ", {"GITHUB_TOKEN": "env_github_token"})
    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_with_env_vars(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Test create-feature-issues command using environment variables."""
        # Setup
        mock_github_client = Mock()
        mock_gemini_client = Mock()
        mock_automation_engine = Mock()
        mock_automation_engine.create_feature_issues.return_value = []

        mock_github_client_class.return_value = mock_github_client
        mock_gemini_client_class.return_value = mock_gemini_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()

        # Execute
        result = runner.invoke(create_feature_issues, ["--repo", "test/repo"])

        # Assert
        assert result.exit_code == 0
        mock_github_client_class.assert_called_once_with("env_github_token")
        mock_gemini_client_class.assert_called_once()

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Custom model name is passed to GeminiClient for create-feature-issues."""
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
                "--model",
                "gemini-custom",
            ],
        )

        assert result.exit_code == 0
        mock_gemini_client_class.assert_called_once_with(
            model_name="gemini-custom"
        )

    def test_process_issues_help(self):
        """Test process-issues command help."""
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])

        assert result.exit_code == 0
        assert (
            "Process GitHub issues and PRs using Gemini CLI" in result.output
        )
        assert "--repo" in result.output
        assert "--github-token" in result.output
        assert "--model" in result.output
        assert "--dry-run" in result.output

    def test_create_feature_issues_help(self):
        """Test create-feature-issues command help."""
        runner = CliRunner()
        result = runner.invoke(create_feature_issues, ["--help"])

        assert result.exit_code == 0
        assert (
            "Analyze repository and create feature enhancement issues"
            in result.output
        )
        assert "--repo" in result.output
        assert "--github-token" in result.output
        assert "--model" in result.output
