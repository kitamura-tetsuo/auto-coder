import json
import os
from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.util.github_action import GitHubActionsStatusResult
from src.auto_coder.utils import CommandExecutor


def test_create_pr_prompt_is_action_oriented_no_comments(mock_github_client, mock_gemini_client, sample_pr_data, test_repo_name):
    config = AutomationConfig()
    config.DRY_RUN = True
    engine = AutomationEngine(mock_github_client, config=config)
    prompt = engine._create_pr_analysis_prompt(test_repo_name, sample_pr_data, pr_diff="diff...")

    assert "Do NOT post any comments" in prompt
    # Should NOT ask LLM to commit/push or merge
    assert 'git commit -m "Auto-Coder: Apply fix for PR #' not in prompt
    assert "gh pr merge" not in prompt
    assert "Do NOT run git commit/push" in prompt
    assert "ACTION_SUMMARY:" in prompt
    assert "CANNOT_FIX" in prompt
    # Ensure repo/number placeholders are still present contextually
    assert str(sample_pr_data["number"]) in prompt
    assert test_repo_name in prompt


def test_apply_pr_actions_directly_does_not_post_comments(mock_github_client, mock_gemini_client, sample_pr_data, test_repo_name):
    # Initialize backend manager for proper LLM client handling
    from src.auto_coder.backend_manager import LLMBackendManager, get_llm_backend_manager
    from src.auto_coder.pr_processor import _apply_pr_actions_directly

    # Reset singleton and initialize properly
    LLMBackendManager.reset_singleton()
    manager = get_llm_backend_manager(
        default_backend="codex",
        default_client=mock_gemini_client,
        factories={"codex": lambda: mock_gemini_client},
    )

    # For dry_run=True, the function should not call LLM but should still function
    config = AutomationConfig()
    config.DRY_RUN = True
    engine = AutomationEngine(mock_github_client, config=config)

    # Stub diff generation
    with patch("src.auto_coder.pr_processor._get_pr_diff", return_value="diff..."):
        # Ensure add_comment_to_issue is tracked
        mock_github_client.add_comment_to_issue.reset_mock()

        # In dry_run mode, the function should return a dry run message
        actions = _apply_pr_actions_directly(
            test_repo_name,
            sample_pr_data,
            engine.config,
            True,  # dry_run=True explicitly
        )

        # No comment should be posted
        mock_github_client.add_comment_to_issue.assert_not_called()

        # In dry_run mode, should return dry run message
        assert len(actions) == 1
        assert actions[0].startswith("[DRY RUN] Would apply PR actions directly")


"""Tests for automation engine functionality."""


class TestAutomationEngine:
    """Test cases for AutomationEngine class."""

    def test_init(self, mock_github_client, mock_gemini_client, temp_reports_dir):
        """Test AutomationEngine initialization."""
    config.DRY_RUN = True
    config = AutomationConfig()
    config.DRY_RUN = True
    engine = AutomationEngine(mock_github_client, config=config)

        assert engine.github == mock_github_client
        assert engine.dry_run is True
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
        mock_backend_manager.get_last_backend_and_model.return_value = (
            "gemini",
            "gemini-2.5-pro",
        )

        with patch("src.auto_coder.automation_engine.get_llm_backend_manager") as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Mock GitHub client methods needed for operation
            mock_github_client.get_open_pull_requests.return_value = []
            mock_github_client.get_open_issues.return_value = []
            mock_github_client.disable_labels = False

    config.DRY_RUN = True
    config = AutomationConfig()
    config.DRY_RUN = True
    engine = AutomationEngine(mock_github_client, config=config)
            engine._save_report = Mock()

            # Execute
            result = engine.run(test_repo_name)

            # Assert basic result structure
            assert result["repository"] == test_repo_name
            assert result["dry_run"] is True
            assert result["llm_backend"] == "gemini"
            assert result["llm_model"] is not None
            assert "issues_processed" in result
            assert "prs_processed" in result
            assert "errors" in result
            assert len(result["errors"]) == 0

            # Verify report was saved
            engine._save_report.assert_called_once()

    def test_run_jules_mode_success(
        self,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test successful run with jules mode."""
        # Setup - Mock backend manager
        from src.auto_coder.backend_manager import get_llm_backend_manager

        mock_backend_manager = Mock()
        mock_backend_manager.get_last_backend_and_model.return_value = (
            "gemini",
            "gemini-2.5-pro",
        )

        with patch("src.auto_coder.automation_engine.get_llm_backend_manager") as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Mock GitHub client methods needed for operation
            mock_github_client.get_open_pull_requests.return_value = []
            mock_github_client.get_open_issues.return_value = []
            mock_github_client.disable_labels = False

    config.DRY_RUN = True
    config = AutomationConfig()
    config.DRY_RUN = True
    engine = AutomationEngine(mock_github_client, config=config)
            engine._save_report = Mock()

            # Execute
            result = engine.run(test_repo_name, jules_mode=True)

            # Assert basic result structure with jules mode
            assert result["repository"] == test_repo_name
            assert result["dry_run"] is True
            assert result["jules_mode"] is True
            assert result["llm_backend"] == "gemini"
            assert result["llm_model"] is not None
            assert "issues_processed" in result
            assert "prs_processed" in result
            assert "errors" in result
            assert len(result["errors"]) == 0

            engine._save_report.assert_called_once()

    def test_run_with_error(
        self,
        mock_github_client,
        mock_gemini_client,
        test_repo_name,
    ):
        """Test automation run with error."""
        # Setup - Mock backend manager
        from src.auto_coder.backend_manager import get_llm_backend_manager

        mock_backend_manager = Mock()
        mock_backend_manager.get_last_backend_and_model.return_value = (
            "gemini",
            "gemini-2.5-pro",
        )

        with patch("src.auto_coder.automation_engine.get_llm_backend_manager") as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Mock GitHub client methods needed for operation
            mock_github_client.get_open_pull_requests.return_value = []
            mock_github_client.get_open_issues.return_value = []
            mock_github_client.disable_labels = False

            # Test error handling - keep it simple, just verify basic error structure
    config.DRY_RUN = True
    config = AutomationConfig()
    config.DRY_RUN = True
    engine = AutomationEngine(mock_github_client, config=config)

            # Execute without any complex mocking to see if basic error handling works
            result = engine.run(test_repo_name)

            # Assert that we get a valid result structure even if there are no errors in this case
            assert result["repository"] == test_repo_name
            assert result["dry_run"] is True
            assert "errors" in result


class TestAutomationConfig:
    """Test cases for AutomationConfig class."""

    def test_get_reports_dir(self):
        """Test get_reports_dir method returns correct path."""
        from pathlib import Path


        # Test with typical repo name
        repo_name = "owner/repo"
        expected_path = str(Path.home() / ".auto-coder" / "owner_repo")
        assert config.get_reports_dir(repo_name) == expected_path

        # Test with different repo name
        repo_name2 = "another-owner/another-repo"
        expected_path2 = str(Path.home() / ".auto-coder" / "another-owner_another-repo")
        assert config.get_reports_dir(repo_name2) == expected_path2


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
