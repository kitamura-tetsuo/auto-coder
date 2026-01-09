from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _send_jules_error_feedback


class TestSendJulesErrorFeedback:
    @patch("src.auto_coder.pr_processor.get_gh_logger")
    @patch("src.auto_coder.jules_client.JulesClient")
    @patch("src.auto_coder.pr_processor._create_github_action_log_summary")
    def test_send_jules_error_feedback_success(self, mock_create_summary, mock_jules_client, mock_gh_logger):
        # Setup
        repo_name = "owner/repo"
        pr_data = {"number": 123, "title": "Test PR", "user": {"login": "test-user"}, "_jules_session_id": "session-123"}
        failed_checks = [{"name": "check1", "conclusion": "failure"}]
        config = AutomationConfig()
        github_client = MagicMock()

        # Mock return values
        mock_create_summary.return_value = ("Logs summary", None)
        mock_jules_instance = mock_jules_client.return_value
        mock_jules_instance.send_message.return_value = "Jules response"

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config, github_client)

        # Verify
        mock_create_summary.assert_called_once_with(repo_name, config, failed_checks, pr_data)
        mock_jules_instance.send_message.assert_called_once()
        assert "Logs summary" in mock_jules_instance.send_message.call_args[0][1]
        assert "Sent CI failure logs to Jules session" in actions[0]

    @patch("src.auto_coder.pr_processor.get_gh_logger")
    @patch("src.auto_coder.jules_client.JulesClient")
    @patch("src.auto_coder.pr_processor._create_github_action_log_summary")
    def test_send_jules_error_feedback_no_session_id(self, mock_create_summary, mock_jules_client, mock_gh_logger):
        # Setup
        repo_name = "owner/repo"
        pr_data = {
            "number": 123,
            "title": "Test PR",
            "user": {"login": "test-user"},
            # Missing session ID
        }
        failed_checks = []
        config = AutomationConfig()

        # Execute
        actions = _send_jules_error_feedback(repo_name, pr_data, failed_checks, config)

        # Verify
        mock_create_summary.assert_not_called()
        mock_jules_client.assert_not_called()
        assert "no session ID found" in actions[0]
