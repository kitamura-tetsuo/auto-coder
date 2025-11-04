import json
import os
from unittest.mock import Mock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.util.github_action import GitHubActionsStatusResult
from src.auto_coder.utils import CommandExecutor


def test_create_pr_prompt_is_action_oriented_no_comments(
    mock_github_client, mock_gemini_client, sample_pr_data, test_repo_name
):
    engine = AutomationEngine(mock_github_client, dry_run=True)
    prompt = engine._create_pr_analysis_prompt(
        test_repo_name, sample_pr_data, pr_diff="diff..."
    )

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


def test_apply_pr_actions_directly_does_not_post_comments(
    mock_github_client, mock_gemini_client, sample_pr_data, test_repo_name
):
    # Initialize backend manager for proper LLM client handling
    from src.auto_coder.backend_manager import (
        LLMBackendManager,
        get_llm_backend_manager,
    )
    from src.auto_coder.pr_processor import _apply_pr_actions_directly

    # Reset singleton and initialize properly
    LLMBackendManager.reset_singleton()
    manager = get_llm_backend_manager(
        default_backend="codex",
        default_client=mock_gemini_client,
        factories={"codex": lambda: mock_gemini_client},
    )

    # For dry_run=True, the function should not call LLM but should still function
    engine = AutomationEngine(mock_github_client, dry_run=True)

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
        engine = AutomationEngine(mock_github_client, dry_run=True)

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

        with patch(
            "src.auto_coder.automation_engine.get_llm_backend_manager"
        ) as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Mock the actual functions that get imported inside run method
            with (
                patch(
                    "src.auto_coder.pr_processor.process_pull_requests"
                ) as mock_process_prs,
                patch(
                    "src.auto_coder.issue_processor.process_issues"
                ) as mock_process_issues,
                patch("src.auto_coder.automation_engine.datetime") as mock_datetime,
            ):

                # Setup - Mock GitHub client methods needed for _get_candidates
                mock_github_client.get_open_pull_requests.return_value = [
                    Mock(number=1)
                ]
                mock_github_client.get_open_issues.return_value = [Mock(number=1)]
                mock_github_client.get_pr_details.return_value = {
                    "number": 1,
                    "title": "Test PR",
                    "body": "",
                    "head": {"ref": "test"},
                    "labels": [],
                    "mergeable": True,
                }
                mock_github_client.get_issue_details.return_value = {
                    "number": 1,
                    "title": "Test Issue",
                    "body": "",
                    "labels": [],
                    "state": "open",
                }
                mock_github_client.disable_labels = False
                mock_github_client.get_open_sub_issues.return_value = []
                mock_github_client.has_linked_pr.return_value = False
                mock_github_client.try_add_work_in_progress_label.return_value = True
                mock_github_client.remove_labels_from_issue.return_value = True

                mock_datetime.now.return_value.isoformat.return_value = (
                    "2024-01-01T00:00:00"
                )

                # Mock functions return expected structure for external functions
                mock_process_issues.return_value = [
                    {"issue_data": {"number": 1}, "actions_taken": ["Test action"]}
                ]
                mock_process_prs.return_value = [
                    {"pr_data": {"number": 1}, "actions_taken": ["Test action"]}
                ]

                engine = AutomationEngine(mock_github_client, dry_run=True)
                engine._save_report = Mock()

                # Execute
                result = engine.run(test_repo_name)

                # Assert
                assert result["repository"] == test_repo_name
                assert result["dry_run"] is True
                assert result["llm_backend"] == "gemini"  # GeminiClientから推測
                assert result["llm_model"] is not None
                assert len(result["issues_processed"]) == 1
                assert len(result["prs_processed"]) == 1
                assert len(result["errors"]) == 0

                # Verify functions were called with correct arguments
                mock_process_issues.assert_called_once_with(
                    mock_github_client,
                    engine.config,
                    True,  # dry_run
                    test_repo_name,
                    False,  # jules_mode
                )
                mock_process_prs.assert_called_once_with(
                    mock_github_client, engine.config, True, test_repo_name  # dry_run
                )
                engine._save_report.assert_called_once()

    @patch("src.auto_coder.issue_processor.process_issues")
    @patch("src.auto_coder.pr_processor.process_pull_requests")
    @patch("src.auto_coder.automation_engine.datetime")
    def test_run_jules_mode_success(
        self,
        mock_datetime,
        mock_process_prs,
        mock_process_issues,
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

        with patch(
            "src.auto_coder.automation_engine.get_llm_backend_manager"
        ) as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Mock GitHub client methods needed for _get_candidates
            mock_github_client.get_open_pull_requests.return_value = [Mock(number=1)]
            mock_github_client.get_open_issues.return_value = [Mock(number=1)]
            mock_github_client.get_pr_details.return_value = {
                "number": 1,
                "title": "Test PR",
                "body": "",
                "head": {"ref": "test"},
                "labels": [],
                "mergeable": True,
            }
            mock_github_client.get_issue_details.return_value = {
                "number": 1,
                "title": "Test Issue",
                "body": "",
                "labels": [],
                "state": "open",
            }
            mock_github_client.disable_labels = False
            mock_github_client.get_open_sub_issues.return_value = []
            mock_github_client.has_linked_pr.return_value = False
            mock_github_client.try_add_work_in_progress_label.return_value = True
            mock_github_client.remove_labels_from_issue.return_value = True

            mock_datetime.now.return_value.isoformat.return_value = (
                "2024-01-01T00:00:00"
            )
            mock_process_issues.return_value = [{"issue": "labeled"}]
            mock_process_prs.return_value = [{"pr": "processed"}]

            engine = AutomationEngine(mock_github_client, dry_run=True)
            engine._save_report = Mock()

            # Execute
            result = engine.run(test_repo_name, jules_mode=True)

            # Assert
            assert result["repository"] == test_repo_name
            assert result["dry_run"] is True
            assert result["jules_mode"] is True
            assert result["llm_backend"] == "gemini"  # GeminiClientから推測
            assert result["llm_model"] is not None
            assert result["issues_processed"] == [{"issue": "labeled"}]
            assert result["prs_processed"] == [
                {"pr": "processed"}
            ]  # PRs still processed normally
            assert len(result["errors"]) == 0

            mock_process_issues.assert_called_once()
            mock_process_prs.assert_called_once()
            engine._save_report.assert_called_once()

    @patch("src.auto_coder.issue_processor.process_issues")
    def test_run_with_error(
        self,
        mock_process_issues,
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

        with patch(
            "src.auto_coder.automation_engine.get_llm_backend_manager"
        ) as mock_get_manager:
            mock_get_manager.return_value = mock_backend_manager

            # Setup - Mock GitHub client methods needed for _get_candidates
            mock_github_client.get_open_pull_requests.return_value = [Mock(number=1)]
            mock_github_client.get_open_issues.return_value = [Mock(number=1)]
            mock_github_client.get_pr_details.return_value = {
                "number": 1,
                "title": "Test PR",
                "body": "",
                "head": {"ref": "test"},
                "labels": [],
                "mergeable": True,
            }
            mock_github_client.get_issue_details.return_value = {
                "number": 1,
                "title": "Test Issue",
                "body": "",
                "labels": [],
                "state": "open",
            }
            mock_github_client.disable_labels = False
            mock_github_client.get_open_sub_issues.return_value = []
            mock_github_client.has_linked_pr.return_value = False
            mock_github_client.try_add_work_in_progress_label.return_value = True
            mock_github_client.remove_labels_from_issue.return_value = True

            # Mock function to raise an exception
            mock_process_issues.side_effect = Exception("Test error")

            engine = AutomationEngine(mock_github_client, dry_run=True)
            engine._save_report = Mock()

            # Execute
            result = engine.run(test_repo_name)

            # Assert
            assert result["repository"] == test_repo_name
            assert result["dry_run"] is True
            assert len(result["errors"]) == 1
            assert "Test error" in result["errors"][0]

            mock_process_issues.assert_called_once()
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

    def test_get_llm_backend_info_with_gemini_client(
        self, mock_github_client, mock_gemini_client
    ):
        """Test _get_llm_backend_info with GeminiClient."""
        # Initialize backend manager with gemini client
        from src.auto_coder.backend_manager import LLMBackendManager

        # Reset singleton to ensure clean state
        LLMBackendManager.reset_singleton()

        # Initialize with proper parameters
        manager = LLMBackendManager.get_llm_instance(
            default_backend="gemini",
            default_client=mock_gemini_client,
            factories={"gemini": lambda: mock_gemini_client},
        )

        engine = AutomationEngine(mock_github_client)

        info = engine._get_llm_backend_info()

        assert info["backend"] == "gemini"
        assert info["model"] is not None

    def test_get_llm_backend_info_with_backend_manager(self, mock_github_client):
        """Test _get_llm_backend_info with BackendManager."""
        # Initialize backend manager with mock client
        from src.auto_coder.backend_manager import LLMBackendManager

        # Reset singleton to ensure clean state
        LLMBackendManager.reset_singleton()

        mock_backend_client = Mock()
        mock_backend_client.get_last_backend_and_model.return_value = (
            "codex",
            "codex-model",
        )

        # Initialize with proper parameters
        manager = LLMBackendManager.get_llm_instance(
            default_backend="codex",
            default_client=mock_backend_client,
            factories={"codex": lambda: mock_backend_client},
        )

        engine = AutomationEngine(mock_github_client)

        info = engine._get_llm_backend_info()

        assert info["backend"] == "codex"
        assert info["model"] == "codex-model"

    def test_get_llm_backend_info_with_no_client(self, mock_github_client):
        """Test _get_llm_backend_info with no LLM client."""
        # Reset backend manager to ensure it's not initialized
        from src.auto_coder.backend_manager import LLMBackendManager

        LLMBackendManager.reset_singleton()

        engine = AutomationEngine(mock_github_client)

        info = engine._get_llm_backend_info()

        assert info["backend"] is None
        assert info["model"] is None


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
