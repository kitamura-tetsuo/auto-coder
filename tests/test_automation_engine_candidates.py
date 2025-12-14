from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine


class TestAutomationEngineCandidates:
    """Test cases for _get_candidates method in AutomationEngine."""

    @patch("auto_coder.automation_engine.get_gh_logger")
    @patch("auto_coder.pr_processor._is_jules_pr")
    def test_get_candidates_jules_draft_pr(self, mock_is_jules_pr, mock_get_gh_logger, mock_github_client):
        """Test that Jules draft PRs are marked as ready."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        # Mock PR data
        pr_mock = Mock()
        pr_data = {"number": 123, "title": "Jules PR", "draft": True, "head": {"ref": "jules-branch"}, "labels": [], "body": "Session ID: abc", "created_at": "2023-01-01T00:00:00Z"}

        mock_github_client.get_open_pull_requests.return_value = [pr_mock]
        mock_github_client.get_pr_details.return_value = pr_data

        # Mock _is_jules_pr to return True
        mock_is_jules_pr.return_value = True

        # Mock gh_logger
        mock_gh_logger_instance = Mock()
        mock_get_gh_logger.return_value = mock_gh_logger_instance
        mock_gh_logger_instance.execute_with_logging.return_value = Mock(success=True)

        # Mock other dependencies to avoid errors
        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_label_manager,
            patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress") as mock_check_actions,
            patch("auto_coder.util.github_action._check_github_actions_status") as mock_check_status,
            patch("auto_coder.pr_processor._should_skip_waiting_for_jules") as mock_skip_jules,
        ):

            mock_label_manager.return_value.__enter__.return_value = True
            mock_check_actions.return_value = True
            mock_check_status.return_value = Mock(success=True)
            mock_skip_jules.return_value = False

            # Execute
            candidates = engine._get_candidates(repo_name)

        # Assert
        # Verify gh pr ready was called
        mock_gh_logger_instance.execute_with_logging.assert_called_once()
        call_args = mock_gh_logger_instance.execute_with_logging.call_args[0][0]
        assert call_args == ["gh", "pr", "ready", "123", "--repo", repo_name]

        # Verify pr_data was updated
        assert pr_data["draft"] is False

        # Verify candidate was created
        assert len(candidates) == 1
        assert candidates[0].data["number"] == 123

    @patch("auto_coder.automation_engine.get_gh_logger")
    @patch("auto_coder.pr_processor._is_jules_pr")
    def test_get_candidates_jules_ready_pr(self, mock_is_jules_pr, mock_get_gh_logger, mock_github_client):
        """Test that Jules ready PRs are NOT marked as ready again."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        # Mock PR data
        pr_mock = Mock()
        pr_data = {"number": 123, "title": "Jules PR", "draft": False, "head": {"ref": "jules-branch"}, "labels": [], "body": "Session ID: abc", "created_at": "2023-01-01T00:00:00Z"}  # Already ready

        mock_github_client.get_open_pull_requests.return_value = [pr_mock]
        mock_github_client.get_pr_details.return_value = pr_data

        # Mock _is_jules_pr to return True
        mock_is_jules_pr.return_value = True

        # Mock gh_logger
        mock_gh_logger_instance = Mock()
        mock_get_gh_logger.return_value = mock_gh_logger_instance

        # Mock other dependencies
        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_label_manager,
            patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress") as mock_check_actions,
            patch("auto_coder.util.github_action._check_github_actions_status") as mock_check_status,
            patch("auto_coder.pr_processor._should_skip_waiting_for_jules") as mock_skip_jules,
        ):

            mock_label_manager.return_value.__enter__.return_value = True
            mock_check_actions.return_value = True
            mock_check_status.return_value = Mock(success=True)
            mock_skip_jules.return_value = False

            # Execute
            candidates = engine._get_candidates(repo_name)

        # Assert
        # Verify gh pr ready was NOT called
        mock_gh_logger_instance.execute_with_logging.assert_not_called()

        # Verify candidate was created
        assert len(candidates) == 1

    @patch("auto_coder.automation_engine.get_gh_logger")
    @patch("auto_coder.pr_processor._is_jules_pr")
    def test_get_candidates_non_jules_draft_pr(self, mock_is_jules_pr, mock_get_gh_logger, mock_github_client):
        """Test that non-Jules draft PRs are NOT marked as ready."""
        # Setup
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        repo_name = "owner/repo"

        # Mock PR data
        pr_mock = Mock()
        pr_data = {"number": 123, "title": "Regular PR", "draft": True, "head": {"ref": "feature-branch"}, "labels": [], "body": "Description", "created_at": "2023-01-01T00:00:00Z"}

        mock_github_client.get_open_pull_requests.return_value = [pr_mock]
        mock_github_client.get_pr_details.return_value = pr_data

        # Mock _is_jules_pr to return False
        mock_is_jules_pr.return_value = False

        # Mock gh_logger
        mock_gh_logger_instance = Mock()
        mock_get_gh_logger.return_value = mock_gh_logger_instance

        # Mock other dependencies
        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_label_manager,
            patch("auto_coder.util.github_action.check_github_actions_and_exit_if_in_progress") as mock_check_actions,
            patch("auto_coder.util.github_action._check_github_actions_status") as mock_check_status,
            patch("auto_coder.pr_processor._should_skip_waiting_for_jules") as mock_skip_jules,
        ):

            mock_label_manager.return_value.__enter__.return_value = True
            mock_check_actions.return_value = True
            mock_check_status.return_value = Mock(success=True)
            mock_skip_jules.return_value = False

            # Execute
            candidates = engine._get_candidates(repo_name)

        # Assert
        # Verify gh pr ready was NOT called
        mock_gh_logger_instance.execute_with_logging.assert_not_called()

        # Verify pr_data was NOT updated
        assert pr_data["draft"] is True

        # Verify candidate was created
        assert len(candidates) == 1
