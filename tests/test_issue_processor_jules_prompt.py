"""
Tests for the Jules prompt argument passing in the issue processor.
"""
import unittest
from unittest.mock import MagicMock, patch

from auto_coder.issue_processor import _process_issue_jules_mode


class TestIssueProcessorJulesPrompt(unittest.TestCase):
    """
    Tests for the Jules prompt argument passing in the issue processor.
    """

    @patch("auto_coder.issue_processor.JulesClient")
    @patch("auto_coder.issue_processor.get_commit_log")
    @patch("auto_coder.issue_processor.render_prompt")
    @patch("auto_coder.issue_processor.CloudManager")
    @patch("auto_coder.issue_processor.cmd")
    def test_process_issue_jules_mode_prompt_args(
        self,
        mock_cmd,
        mock_cloud_manager,
        mock_render_prompt,
        mock_get_commit_log,
        mock_jules_client,
    ):
        """
        Verify that render_prompt receives the correct arguments when
        _process_issue_jules_mode is called.
        """
        mock_github_client = MagicMock()
        mock_config = MagicMock()

        repo_name = "test/repo"
        issue_data = {
            "number": 123,
            "title": "Test Issue",
            "body": "This is a test issue.",
            "labels": [{"name": "bug"}, {"name": "enhancement"}],
            "state": "open",
            "user": {"login": "testuser"},
        }
        commit_log = "- commit1\n- commit2"
        parent_issue_details = {
            "number": 456,
            "title": "Parent Issue",
        }
        parent_issue_body = "This is the parent issue body."

        mock_get_commit_log.return_value = commit_log
        mock_github_client.get_parent_issue_details.return_value = parent_issue_details
        mock_github_client.get_parent_issue_body.return_value = parent_issue_body

        _process_issue_jules_mode(repo_name, issue_data, mock_config, mock_github_client)

        mock_render_prompt.assert_called_once_with(
            "issue.action",
            repo_name=repo_name,
            issue_number=issue_data["number"],
            issue_title=issue_data["title"],
            issue_body=issue_data["body"],
            issue_labels="bug, enhancement",
            issue_state=issue_data["state"],
            issue_author=issue_data["user"]["login"],
            parent_issue_number=parent_issue_details["number"],
            parent_issue_title=parent_issue_details["title"],
            parent_issue_body=parent_issue_body,
            commit_log=commit_log,
        )
