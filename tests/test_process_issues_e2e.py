import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from src.auto_coder.cli import process_issues


class TestProcessIssuesE2E:
    """End-to-end tests for process-issues command."""

    def test_process_issues_target_repo(self):
        """Test process-issues command targeting a specific repository."""
        target_repo = "kitamura-tetsuo/auto-coder-test"

        # Mock dependencies to prevent real API calls and infinite loops
        with (
            patch("src.auto_coder.cli_commands_main.GitHubClient") as MockGitHubClient,
            patch("src.auto_coder.cli_commands_main.AutomationEngine") as MockAutomationEngine,
            patch("src.auto_coder.cli_commands_main.get_llm_config") as mock_get_llm_config,
            patch("src.auto_coder.cli_commands_main.build_message_backend_manager"),
            patch("src.auto_coder.cli.LockManager"),
            patch("src.auto_coder.cli_commands_main.check_backend_prerequisites"),
            patch("src.auto_coder.cli_commands_main.initialize_graphrag"),
            patch("src.auto_coder.cli_commands_main.check_graphrag_mcp_for_backends"),
            patch("src.auto_coder.cli_commands_main.ensure_test_script_or_fail"),
            patch("src.auto_coder.cli_commands_main.setup_progress_footer_logging"),
            patch("src.auto_coder.cli_commands_main.check_github_sub_issue_or_setup"),
            patch("src.auto_coder.cli_commands_main.get_current_branch"),
            patch("src.auto_coder.webhook_server.create_app"),
            patch("uvicorn.Server.serve"),
        ):

            # Setup mock Config
            mock_config = MagicMock()
            mock_config.default_backend = "codex"
            mock_config.get_active_backends.return_value = ["codex"]
            mock_config.backend_order = ["codex"]
            mock_get_llm_config.return_value = mock_config

            # Setup mock GitHub client
            mock_gh_instance = MagicMock()
            MockGitHubClient.get_instance.return_value = mock_gh_instance

            # Setup mock Automation Engine with async start_automation
            mock_engine_instance = MagicMock()
            MockAutomationEngine.return_value = mock_engine_instance

            # Create an async mock that we can await
            mock_start_automation = AsyncMock()
            mock_engine_instance.start_automation = mock_start_automation

            runner = CliRunner()

            # Invoke command
            runner.invoke(process_issues, ["--repo", target_repo, "--github-token", "dummy_token", "--disable-graphrag"], catch_exceptions=False)

            # Verifications
            MockGitHubClient.get_instance.assert_called()
            MockAutomationEngine.assert_called()
            mock_start_automation.assert_called_once_with(target_repo)
