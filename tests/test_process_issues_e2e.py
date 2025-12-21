import os
import sys
from unittest.mock import MagicMock, patch

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
            patch("time.sleep", side_effect=RuntimeError("Stop loop")),
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

            # Setup mock Automation Engine
            mock_engine_instance = MagicMock()
            MockAutomationEngine.return_value = mock_engine_instance
            mock_engine_instance.run.return_value = {"issues_processed": [], "prs_processed": []}

            runner = CliRunner()

            # Invoke command
            # We expect RuntimeError because we mock time.sleep to raise it to break the while True loop
            try:
                runner.invoke(process_issues, ["--repo", target_repo, "--github-token", "dummy_token", "--disable-graphrag"], catch_exceptions=False)
            except RuntimeError as e:
                if str(e) != "Stop loop":
                    raise  # Re-raise unexpected RuntimeErrors

            # Verifications
            MockGitHubClient.get_instance.assert_called()
            MockAutomationEngine.assert_called()
            mock_engine_instance.run.assert_called_with(target_repo)
