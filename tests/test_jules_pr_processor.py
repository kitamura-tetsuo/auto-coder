import pytest
from unittest.mock import MagicMock, patch, ANY
from auto_coder.pr_processor import _update_jules_pr_body, _find_issue_by_session_id_in_comments, _link_jules_pr_to_issue


class TestJulesPRProcessor:

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("auto_coder.util.gh_cache.GitHubClient")
    def test_update_jules_pr_body(self, mock_gh_client_cls, mock_get_ghapi_client):
        # Setup
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "Original PR body"
        issue_number = 456

        # Mock GitHubClient instance
        mock_client_instance = MagicMock()
        mock_client_instance.token = "fake_token"

        # Mock GhApi client
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, mock_client_instance)

        # Verify
        assert result is True

        # Check if api.pulls.update was called correctly
        expected_body = f"{pr_body}\n\nclose #{issue_number}\n\nRelated issue: https://github.com/{repo_name}/issues/{issue_number}"
        mock_api.pulls.update.assert_called_once_with("owner", "repo", pr_number, body=expected_body)

    @patch("auto_coder.pr_processor.get_ghapi_client")
    def test_update_jules_pr_body_already_exists(self, mock_get_ghapi_client):
        # Setup
        repo_name = "owner/repo"
        pr_number = 123
        pr_body = "Original PR body\n\nclose #456"
        issue_number = 456
        mock_client = MagicMock()

        # Execute
        result = _update_jules_pr_body(repo_name, pr_number, pr_body, issue_number, mock_client)

        # Verify
        assert result is True
        mock_get_ghapi_client.assert_not_called()

    def test_find_issue_by_session_id_in_comments(self):
        # Setup
        repo_name = "owner/repo"
        session_id = "session_abc123"
        mock_client = MagicMock()

        # Mock search_issues return value
        # It's expected to be a list of dicts/objects
        # Case 1: Session ID in issue body
        issue1 = {"number": 101, "body": f"This is related to {session_id}"}
        issue2 = {"number": 102, "body": "Nothing here"}

        mock_client.search_issues.return_value = [issue1, issue2]

        # Execute
        result = _find_issue_by_session_id_in_comments(repo_name, session_id, mock_client)

        # Verify
        assert result == 101

    def test_find_issue_by_session_id_in_comments_via_comment(self):
        # Setup
        repo_name = "owner/repo"
        session_id = "session_xyz789"
        mock_client = MagicMock()

        # Mock search_issues
        issue1 = {"number": 201, "body": "No session id here"}
        mock_client.search_issues.return_value = [issue1]

        # Mock get_issue_comments
        mock_client.get_issue_comments.return_value = [{"body": "Just a comment"}, {"body": f"Here is the session: {session_id}"}]

        # Execute
        result = _find_issue_by_session_id_in_comments(repo_name, session_id, mock_client)

        # Verify
        assert result == 201
        mock_client.get_issue_comments.assert_called_once_with(repo_name, 201)

    @patch("auto_coder.pr_processor._extract_session_id_from_pr_body")
    @patch("auto_coder.pr_processor._is_jules_pr")
    def test_process_jules_pr_skips_special_prefixes(self, mock_is_jules, mock_extract_session):
        # Setup
        repo_name = "owner/repo"
        mock_client = MagicMock()
        mock_is_jules.return_value = True

        # Test cases for prefixes
        prefixes = ["🛡️ Sentinel: ", "🎨 Palette: ", "⚡ Bolt: "]

        for prefix in prefixes:
            pr_data = {"number": 123, "title": f"{prefix} Some Title", "body": "Some body", "user": {"login": "google-labs-jules"}}

            # Execute
            result = _link_jules_pr_to_issue(repo_name, pr_data, mock_client)

            # Verify
            assert result is True
            # Should NOT call extract session or proceed
            mock_extract_session.assert_not_called()

    @patch("auto_coder.pr_processor._extract_session_id_from_pr_body")
    @patch("auto_coder.pr_processor._is_jules_pr")
    def test_process_jules_pr_normal_title(self, mock_is_jules, mock_extract_session):
        """Verify normal titles still proceed to extraction."""
        # Setup
        repo_name = "owner/repo"
        mock_client = MagicMock()
        mock_is_jules.return_value = True
        mock_extract_session.return_value = None  # Stop early after extraction

        pr_data = {"number": 124, "title": "Normal Title", "body": "Some body", "user": {"login": "google-labs-jules"}}

        # Execute
        result = _link_jules_pr_to_issue(repo_name, pr_data, mock_client)

        # Verify
        # It returns False because extraction returns None (simulating failure to find session)
        # But importantly, it DID call extraction
        assert result is False
        mock_extract_session.assert_called_once()
