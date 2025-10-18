"""
Tests for process-issues CLI command.
"""

from unittest.mock import Mock, patch

import click
from click.testing import CliRunner

from src.auto_coder.backend_manager import BackendManager
from src.auto_coder.cli import process_issues


class TestCLIProcessIssues:
    """Test cases for process-issues CLI command."""

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_codex_with_model_warns_and_ignores(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
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

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_success_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """Test process-issues command with default codex backend."""
        mock_github_client = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client = Mock()
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine = Mock()
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
        mock_automation_engine.run.assert_called_once_with("test/repo", jules_mode=True)

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.GeminiClient")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_multiple_backends_preserves_order(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """Test that multiple backends are preserved in the order specified."""
        mock_github_client = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client = Mock()
        mock_codex_client_class.return_value = mock_codex_client
        mock_gemini_client = Mock()
        mock_gemini_client_class.return_value = mock_gemini_client
        mock_automation_engine = Mock()
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
                "--backend",
                "gemini",
                "--backend",
                "codex",
            ],
        )

        assert result.exit_code == 0
        mock_github_client_class.assert_called_once_with("test_token")
        # When multiple backends are specified, codex is not initialized
        mock_gemini_client_class.assert_called_once_with(model_name="gemini-2.5-pro")
        assert mock_automation_engine_class.call_count == 1
        args, kwargs = mock_automation_engine_class.call_args
        assert args[0] is mock_github_client
        manager = args[1]
        assert isinstance(manager, BackendManager)
        assert manager._default_backend == "gemini"
        assert manager._all_backends == ["gemini", "codex"]
        mock_automation_engine.run.assert_called_once_with("test/repo", jules_mode=True)

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_no_skip_main_update_flag(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """Test --no-skip-main-update flag sets config to False."""
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
                "--no-skip-main-update",
            ],
        )

        assert result.exit_code == 0
        # Verify config flag is False
        args, kwargs = mock_automation_engine_class.call_args
        assert getattr(kwargs["config"], "SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL") is False

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_jules_mode_default_codex(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """Test --jules-mode flag with default codex backend."""
        mock_github_client = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client = Mock()
        mock_codex_client_class.return_value = mock_codex_client
        mock_automation_engine = Mock()
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
                "--jules-mode",
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
        mock_automation_engine.run.assert_called_once_with("test/repo", jules_mode=True)

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_only_number_calls_single_auto(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """Test --only with number calls process_single_auto."""
        mock_github_client = Mock()
        mock_github_client_class.return_value = mock_github_client
        mock_codex_client = Mock()
        mock_codex_client_class.return_value = mock_codex_client
        mock_engine = Mock()
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
        mock_engine.process_single.assert_called_once_with(
            "test/repo", "auto", 123, jules_mode=True
        )
        mock_engine.run.assert_not_called()

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_only_url_issue(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
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

    def test_process_issues_missing_github_token(self):
        """Test process-issues command with missing GitHub token."""
        runner = CliRunner()

        result = runner.invoke(process_issues, ["--repo", "test/repo"])

        assert result.exit_code != 0
        assert "GitHub token is required" in result.output

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    def test_process_issues_missing_codex_cli(self, mock_check_cli, mock_initialize_graphrag):
        """Default backend codex missing should error."""
        mock_check_cli.side_effect = click.ClickException("Codex CLI missing")
        runner = CliRunner()

        result = runner.invoke(
            process_issues,
            ["--repo", "test/repo", "--github-token", "test_token"],
        )

        assert result.exit_code != 0
        assert "Codex CLI" in result.output

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.GeminiClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_backend_gemini_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
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
    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_with_env_vars(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
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

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_gemini_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.GeminiClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_custom_model(
        self,
        mock_github_client_class,
        mock_gemini_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
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

    @patch("src.auto_coder.cli_commands_main.initialize_graphrag")
    @patch("src.auto_coder.cli_helpers.check_codex_cli_or_fail")
    @patch("src.auto_coder.cli_commands_main.AutomationEngine")
    @patch("src.auto_coder.cli_helpers.CodexClient")
    @patch("src.auto_coder.cli_commands_main.GitHubClient")
    def test_process_issues_ignore_dependabot_flag_sets_config(
        self,
        mock_github_client_class,
        mock_codex_client_class,
        mock_automation_engine_class,
        mock_check_cli,
        mock_initialize_graphrag,
    ):
        """--ignore-dependabot-prs should set engine config flag to True and print it."""
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
                "--ignore-dependabot-prs",
            ],
        )

        assert result.exit_code == 0
        # Verify output
        assert "Ignore Dependabot PRs: True" in result.output
        # Engine config is set
        _, kwargs = mock_automation_engine_class.call_args
        assert getattr(kwargs["config"], "IGNORE_DEPENDABOT_PRS") is True

