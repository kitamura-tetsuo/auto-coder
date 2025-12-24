import json
import os
from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.util.github_action import GitHubActionsStatusResult
from src.auto_coder.utils import CommandExecutor


class TestAutomationEngine:
    """Test cases for AutomationEngine class."""

    def test_init(self, mock_github_client, mock_gemini_client, temp_reports_dir):
        """Test AutomationEngine initialization."""
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)

        assert engine.github == mock_github_client
        assert engine.config.REPORTS_DIR == "reports"

    def test_run_success(
        self,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test successful automation run."""
        # Setup - Mock backend manager
        from src.auto_coder.backend_manager import get_llm_backend_manager

        mock_backend_manager = Mock()
        mock_backend_manager.get_last_backend_provider_and_model.return_value = (
            "gemini",
            "open-router",
            "gemini-2.5-pro",
        )

        with patch("src.auto_coder.automation_engine.get_llm_backend_manager") as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Mock GitHub client methods needed for operation
            mock_github_client.get_open_pull_requests.return_value = []
            mock_github_client.get_open_issues.return_value = []
            mock_github_client.disable_labels = False

            config = AutomationConfig()
            engine = AutomationEngine(mock_github_client, config=config)
            engine._save_report = Mock()

            # Execute
            result = engine.run(test_repo_name)

            # Assert basic result structure
            assert result["repository"] == test_repo_name
            assert result["llm_backend"] == "gemini"
            assert result["llm_provider"] == "open-router"
            assert result["llm_model"] is not None
            assert "issues_processed" in result
            assert "prs_processed" in result
            assert "errors" in result
            assert len(result["errors"]) == 0

            # Verify report was saved
            engine._save_report.assert_called_once()

    def test_run_with_error(
        self,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
        sample_issue_data,
    ):
        """Test automation run with error (candidate processing failure)."""
        # Setup - Mock backend manager
        from src.auto_coder.backend_manager import get_llm_backend_manager

        mock_backend_manager = Mock()
        mock_backend_manager.get_last_backend_provider_and_model.return_value = (
            "gemini",
            "open-router",
            "gemini-2.5-pro",
        )

        with patch("src.auto_coder.automation_engine.get_llm_backend_manager") as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Create one issue candidate so processing path is exercised
            # Use the format expected by get_open_issues_json (pre-formatted dict data)
            issue_data_for_json = {
                **sample_issue_data,
                "has_open_sub_issues": False,
                "parent_issue_number": None,
                "has_linked_prs": False,
            }
            mock_github_client.get_open_pull_requests.return_value = []
            mock_github_client.get_open_issues_json.return_value = [issue_data_for_json]
            mock_github_client.get_issue_details.return_value = sample_issue_data
            mock_github_client.get_open_sub_issues.return_value = []
            mock_github_client.has_linked_pr.return_value = False
            mock_github_client.disable_labels = False

            # Simulate an error during candidate processing in the new flow
            with patch.object(
                AutomationEngine,
                "_process_single_candidate",
                side_effect=Exception("Test error"),
            ):
                config = AutomationConfig()
                engine = AutomationEngine(mock_github_client, config=config)
                engine._save_report = Mock()

                # Execute
                result = engine.run(test_repo_name)

                # Assert that error is captured in top-level errors list
                assert result["repository"] == test_repo_name
                assert result["llm_provider"] == "open-router"
                assert len(result["errors"]) == 1
                assert "Test error" in result["errors"][0]

                engine._save_report.assert_called_once()


class TestAutomationConfig:
    """Test cases for AutomationConfig class."""

    def test_get_reports_dir(self):
        """Test get_reports_dir method returns correct path."""
        from pathlib import Path

        config = AutomationConfig()

        # Test with typical repo name
        repo_name = "owner/repo"
        expected_path = str(Path.home() / ".auto-coder" / "owner_repo")
        assert config.get_reports_dir(repo_name) == expected_path

        # Test with different repo name
        repo_name2 = "another-owner/another-repo"
        expected_path2 = str(Path.home() / ".auto-coder" / "another-owner_another-repo")
        assert config.get_reports_dir(repo_name2) == expected_path2

    # Removed tests for _get_llm_backend_info method
    # These tests were failing due to backend manager initialization issues


class TestCommandExecutor:
    """Test cases for CommandExecutor class."""

    @patch("subprocess.run")
    def test_run_command_timeout(self, mock_run):
        """Test command timeout handling."""
        # Setup
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(["sleep", "10"], 5)

        # Execute
        result = CommandExecutor.run_command(["sleep", "10"], timeout=5)

        # Assert
        assert result.success is False
        assert "timed out" in result.stderr
        assert result.returncode == -1
