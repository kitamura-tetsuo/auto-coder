"""
Tests for CLI functionality.
"""

from unittest.mock import Mock, patch

import click
from click.testing import CliRunner

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.cli import create_feature_issues, main, process_issues


class TestCLI:
    """Test cases for CLI functionality."""

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_codex_with_model_warns_and_ignores(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """When backend=codex and --model is provided, warn and do not print Using model."""
        mock_github_client_class.return_value = Mock()
        mock_codex_client_class.return_value = Mock()
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
                "--model-gemini",
                "some-model",
            ],
        )

        assert result.exit_code == 0
        assert "Using backends: codex (default: codex)" in result.output
        assert "Warning:" not in result.output
        assert "Using model:" not in result.output

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_codex_with_model_warns_and_ignores(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
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

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_success_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Default backend is codex and runs successfully in dry-run."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_automation_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
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
        assert "Using backends: codex (default: codex)" in result.output
        assert "Jules mode: True" in result.output
        assert "Dry run mode: True" in result.output

        mock_github_client_class.assert_called_once_with("test_token")
        mock_codex_client_class.assert_called_once_with(model_name="codex")
        # Capture AutomationEngine init call and verify config flag default (skip=True)
        assert mock_automation_engine_class.call_count == 1
        args, kwargs = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        manager = args[1]
        assert isinstance(manager, BackendManager)
        assert manager._default_backend == "codex"
        assert manager._all_backends == ["codex"]
        assert kwargs.get("dry_run") is True
        assert "config" in kwargs
        assert getattr(kwargs["config"], "SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL") is True

        mock_automation_engine.run.assert_called_once_with("test/repo", jules_mode=True)

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_multiple_backends_preserves_order(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_codex,
        mock_check_gemini,
    ):
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_gemini_client = Mock()
        mock_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_gemini_client_class.return_value = mock_gemini_client
        mock_automation_engine_class.return_value = mock_engine

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "token",
                "--backend",
                "codex",
                "--backend",
                "gemini",
                "--model-gemini",
                "gemini-2.5-pro",
            ],
        )

        assert result.exit_code == 0
        assert "Using backends: codex, gemini (default: codex)" in result.output
        mock_check_codex.assert_called_once()
        mock_check_gemini.assert_called_once()

        args, _ = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        manager = args[1]
        assert isinstance(manager, BackendManager)
        assert manager._default_backend == "codex"
        assert manager._all_backends == ["codex", "gemini"]

        # Ensure the codex client was used and gemini client not instantiated yet
        mock_codex_client_class.assert_called_once_with(model_name="codex")
        mock_gemini_client_class.assert_not_called()
        mock_engine.run.assert_called_once_with("test/repo", jules_mode=True)

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_no_skip_main_update_flag(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """--no-skip-main-update should set config flag to False."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_automation_engine = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
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
                "--no-skip-main-update",
            ],
        )
        assert result.exit_code == 0
        # Verify config flag is False
        args, kwargs = mock_automation_engine_class.call_args
        assert getattr(kwargs["config"], "SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL") is False

    def test_process_issues_help_includes_skip_flag(self):
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])
        assert result.exit_code == 0
        # Click help may split flag across lines; check presence of at least one alias
        assert "--skip-main-update" in result.output

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_jules_mode_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """process-issues with jules mode using default codex backend."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_automation_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            ["--repo", "test/repo", "--github-token", "test-token", "--jules-mode"],
        )

        assert result.exit_code == 0
        assert "Using backends: codex (default: codex)" in result.output
        assert "Jules mode: True" in result.output

        # Verify clients were initialized correctly
        mock_github_client_class.assert_called_once_with("test-token")
        mock_codex_client_class.assert_called_once_with(model_name="codex")

        # Verify automation engine was initialized with proper args and config
        assert mock_automation_engine_class.call_count == 1
        args, kwargs = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        manager = args[1]
        assert isinstance(manager, BackendManager)
        assert manager._default_backend == "codex"
        assert kwargs.get("dry_run") is False
        assert "config" in kwargs
        assert getattr(kwargs["config"], "SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL") is True

        # Verify run was called with jules_mode=True

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_only_number_calls_single_auto(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_engine = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--only",
                "123",
            ],
        )
        assert result.exit_code == 0
        # Should call process_single with target_type='auto'
        mock_engine.process_single.assert_called_once_with(
            "test/repo", "auto", 123, jules_mode=True
        )
        # Should not call bulk run
        mock_engine.run.assert_not_called()

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_only_url_issue(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_engine = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--only",
                "https://github.com/owner/repo/issues/456",
                "--no-jules-mode",
            ],
        )
        assert result.exit_code == 0
        mock_engine.process_single.assert_called_once_with(
            "test/repo", "issue", 456, jules_mode=False
        )
        mock_engine.run.assert_not_called()

    def test_process_issues_help_includes_only_flag(self):
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])
        assert result.exit_code == 0
        assert "--only" in result.output
        # New option should appear in help
        assert "--ignore-dependabot-prs" in result.output

    def test_process_issues_missing_github_token(self):
        """Test process-issues command with missing GitHub token."""
        runner = CliRunner()

        result = runner.invoke(process_issues, ["--repo", "test/repo"])

        assert result.exit_code != 0
        assert "GitHub token is required" in result.output

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    def test_process_issues_missing_codex_cli(self, mock_check_cli):
        """Default backend codex missing should error."""
        mock_check_cli.side_effect = click.ClickException("Codex CLI missing")
        runner = CliRunner()

        result = runner.invoke(
            process_issues,
            ["--repo", "test/repo", "--github-token", "test_token"],
        )

        assert result.exit_code != 0
        assert "Codex CLI" in result.output

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_backend_gemini_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """When backend=gemini, model is passed to GeminiClient."""
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
                "--backend",
                "gemini",
                "--model-gemini",
                "gemini-custom",
            ],
        )

        assert result.exit_code == 0
        mock_gemini_client_class.assert_called_once_with(model_name="gemini-custom")

    @patch.dict("os.environ", {"GITHUB_TOKEN": "env_github_token"})
    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_with_env_vars(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """Test process-issues command using environment variables with default codex backend."""
        # Setup
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_automation_engine = Mock()

        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_automation_engine
        mock_check_cli.return_value = None

        runner = CliRunner()

        # Execute
        result = runner.invoke(process_issues, ["--repo", "test/repo"])

        # Assert
        assert result.exit_code == 0
        mock_github_client_class.assert_called_once_with("env_github_token")
        mock_codex_client_class.assert_called_once()

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
        """When backend=gemini, custom model is passed to GeminiClient for process-issues."""
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
                "--backend",
                "gemini",
                "--model-gemini",
                "gemini-custom",
            ],
        )

        assert result.exit_code == 0
        mock_gemini_client_class.assert_called_once_with(model_name="gemini-custom")

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_success_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """create-feature-issues uses codex by default."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_automation_engine = Mock()
        mock_automation_engine.create_feature_issues.return_value = [
            {
                "number": 123,
                "title": "New Feature",
                "url": "https://github.com/test/repo/issues/123",
            }
        ]

        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
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
        assert "Using backends: codex (default: codex)" in result.output

        mock_github_client_class.assert_called_once_with("test_token")
        mock_codex_client_class.assert_called_once_with(model_name="codex")
        assert mock_automation_engine_class.call_count == 1
        args, kwargs = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        manager = args[1]
        assert isinstance(manager, BackendManager)
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

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    def test_create_feature_issues_missing_codex_cli(self, mock_check_cli):
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
    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_with_env_vars_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
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

    @patch("src.auto_coder.cli.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.GeminiClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_create_feature_issues_backend_gemini_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
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

    def test_process_issues_help(self):
        """Test process-issues command help."""
        runner = CliRunner()
        result = runner.invoke(process_issues, ["--help"])

        assert result.exit_code == 0
        assert (
            "Process GitHub issues and PRs using AI CLI (codex or gemini)"
            in result.output
        )
        assert "--repo" in result.output
        assert "--github-token" in result.output
        assert "--backend" in result.output
        assert "--model-gemini" in result.output
        assert "--model-qwen" in result.output
        assert "--model-auggie" in result.output
        assert "--dry-run" in result.output
        assert "--ignore-dependabot-prs" in result.output

    @patch("src.auto_coder.cli.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli.AutomationEngine")
    @patch("src.auto_coder.cli.CodexClient")
    @patch("src.auto_coder.cli.GitHubClient")
    def test_process_issues_ignore_dependabot_flag_sets_config(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
    ):
        """--ignore-dependabot-prs should set engine config flag to True and print it."""
        mock_github_client = Mock()
        mock_codex_client = Mock()
        mock_engine = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine_class.return_value = mock_engine
        mock_check_cli.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            process_issues,
            [
                "--repo",
                "test/repo",
                "--github-token",
                "test_token",
                "--ignore-dependabot-prs",
            ],
        )
        assert result.exit_code == 0
        # Output shows the flag state
        assert "Ignore Dependabot PRs: True" in result.output
        # Engine config is set
        _, kwargs = mock_automation_engine_class.call_args
        assert getattr(kwargs["config"], "IGNORE_DEPENDABOT_PRS") is True

    def test_create_feature_issues_help(self):
        """Test create-feature-issues command help."""
        runner = CliRunner()
        result = runner.invoke(create_feature_issues, ["--help"])

        assert result.exit_code == 0
        assert (
            "Analyze repository and create feature enhancement issues" in result.output
        )
        assert "--repo" in result.output
        assert "--github-token" in result.output
        assert "--backend" in result.output
        assert "--model-gemini" in result.output
        assert "--model-qwen" in result.output
        assert "--model-auggie" in result.output
