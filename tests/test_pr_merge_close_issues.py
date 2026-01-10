"""Tests for automatic issue closing when PR is merged."""

from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.issue_context import extract_linked_issues_from_pr_body
from src.auto_coder.pr_processor import _close_linked_issues, _merge_pr


class TestExtractLinkedIssues:
    """Test extraction of linked issues from PR body."""

    def test_extract_closes_keyword(self):
        """Test extraction with 'closes' keyword."""
        pr_body = "This PR closes #123"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [123]

    def test_extract_fixes_keyword(self):
        """Test extraction with 'fixes' keyword."""
        pr_body = "This PR fixes #456"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [456]

    def test_extract_resolves_keyword(self):
        """Test extraction with 'resolves' keyword."""
        pr_body = "This PR resolves #789"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [789]

    def test_extract_multiple_keywords(self):
        """Test extraction with multiple keywords."""
        pr_body = "This PR closes #123 and fixes #456"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [123, 456]

    def test_extract_case_insensitive(self):
        """Test extraction is case insensitive."""
        pr_body = "This PR Closes #123 and FIXES #456"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [123, 456]

    def test_extract_all_keyword_variants(self):
        """Test all supported keyword variants."""
        pr_body = """
        close #1
        closes #2
        closed #3
        fix #4
        fixes #5
        fixed #6
        resolve #7
        resolves #8
        resolved #9
        """
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_extract_with_cross_repo_reference(self):
        """Test extraction with cross-repo reference (owner/repo#123)."""
        pr_body = "This PR closes owner/repo#123"
        result = extract_linked_issues_from_pr_body(pr_body)
        # We extract the issue number even for cross-repo references
        assert result == [123]

    def test_extract_removes_duplicates(self):
        """Test that duplicate issue numbers are removed."""
        pr_body = "This PR closes #123 and fixes #123"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [123]

    def test_extract_empty_body(self):
        """Test extraction with empty body."""
        result = extract_linked_issues_from_pr_body("")
        assert result == []

    def test_extract_none_body(self):
        """Test extraction with None body."""
        result = extract_linked_issues_from_pr_body(None)
        assert result == []

    def test_extract_no_keywords(self):
        """Test extraction with no keywords."""
        pr_body = "This is a regular PR description without any issue links"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == []

    def test_extract_preserves_order(self):
        """Test that issue order is preserved."""
        pr_body = "Closes #789, fixes #123, resolves #456"
        result = extract_linked_issues_from_pr_body(pr_body)
        assert result == [789, 123, 456]


class TestCloseLinkedIssues:
    """Test closing of linked issues."""

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_close_single_issue(self, mock_github_client, mock_get_ghapi_client):
        """Test closing a single linked issue."""
        # Mock API
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        # Mock PR body retrieval
        mock_api.pulls.get.return_value = {"body": "Closes #123"}

        _close_linked_issues("test/repo", 456)

        # Verify PR view
        mock_api.pulls.get.assert_called_once_with("test", "repo", 456)

        # Verify issue comment and close
        mock_api.issues.create_comment.assert_called_once_with(
            "test", "repo", 123, body="Closed by PR #456"
        )
        mock_api.issues.update.assert_called_once_with(
            "test", "repo", 123, state="closed"
        )

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_close_multiple_issues(self, mock_github_client, mock_get_ghapi_client):
        """Test closing multiple linked issues."""
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        # Mock PR body
        mock_api.pulls.get.return_value = {"body": "Closes #123 and fixes #456"}

        _close_linked_issues("test/repo", 789)

        # Verify both issues were processed
        assert mock_api.issues.create_comment.call_count == 2
        assert mock_api.issues.update.call_count == 2
        
        # Verify calls for #123
        mock_api.issues.create_comment.assert_any_call("test", "repo", 123, body="Closed by PR #789")
        mock_api.issues.update.assert_any_call("test", "repo", 123, state="closed")
        
        # Verify calls for #456
        mock_api.issues.create_comment.assert_any_call("test", "repo", 456, body="Closed by PR #789")
        mock_api.issues.update.assert_any_call("test", "repo", 456, state="closed")

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_no_linked_issues(self, mock_github_client, mock_get_ghapi_client):
        """Test when PR has no linked issues."""
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        mock_api.pulls.get.return_value = {"body": "Regular PR description"}

        _close_linked_issues("test/repo", 456)

        mock_api.issues.create_comment.assert_not_called()
        mock_api.issues.update.assert_not_called()

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_pr_view_failure(self, mock_github_client, mock_get_ghapi_client):
        """Test when PR view fails."""
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        mock_api.pulls.get.side_effect = Exception("PR not found")

        # Should not raise exception
        _close_linked_issues("test/repo", 456)

        mock_api.issues.create_comment.assert_not_called()

    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_issue_close_failure(self, mock_github_client, mock_get_ghapi_client):
        """Test when issue close fails."""
        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        mock_api.pulls.get.return_value = {"body": "Closes #123"}
        
        # Mock comment failure (should continue to try close)
        mock_api.issues.create_comment.side_effect = Exception("Comment failed")
        # Mock close failure
        mock_api.issues.update.side_effect = Exception("Close failed")

        # Should not raise exception
        _close_linked_issues("test/repo", 456)

        mock_api.issues.create_comment.assert_called_once()
        mock_api.issues.update.assert_called_once()


class TestMergePRWithIssueClosing:
    """Test that _merge_pr closes linked issues after successful merge."""

    @patch("src.auto_coder.pr_processor._archive_jules_session")
    @patch("src.auto_coder.pr_processor._close_linked_issues")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_merge_pr_closes_issues_on_success(self, mock_github_client, mock_get_ghapi_client, mock_close_issues, mock_archive_session):
        """Test that successful merge triggers issue closing."""
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"

        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        # Mock successful merge
        mock_api.pulls.merge.return_value = {"merged": True}

        result = _merge_pr("test/repo", 123, {}, config)

        assert result is True
        mock_close_issues.assert_called_once_with("test/repo", 123)
        mock_archive_session.assert_called_once_with("test/repo", 123)

    @patch("src.auto_coder.pr_processor._archive_jules_session")
    @patch("src.auto_coder.pr_processor._close_linked_issues")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_merge_pr_does_not_close_issues_on_failure(self, mock_github_client, mock_get_ghapi_client, mock_close_issues, mock_archive_session):
        """Test that failed merge does not trigger issue closing."""
        config = AutomationConfig()
        config.MERGE_AUTO = False
        config.MERGE_METHOD = "--squash"

        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        # Mock failed merge (API failure)
        mock_api.pulls.merge.side_effect = Exception("Merge failed")
        
        # Mock conflict check (not conflict)
        mock_api.pulls.get.return_value = {"mergeable": True}

        result = _merge_pr("test/repo", 123, {}, config)

        assert result is False
        mock_close_issues.assert_not_called()
        mock_archive_session.assert_not_called()

    @patch("src.auto_coder.pr_processor._archive_jules_session")
    @patch("src.auto_coder.pr_processor._close_linked_issues")
    @patch("auto_coder.util.gh_cache.get_ghapi_client")
    @patch("src.auto_coder.pr_processor.GitHubClient")
    def test_merge_pr_auto_merge_closes_issues(self, mock_github_client, mock_get_ghapi_client, mock_close_issues, mock_archive_session):
        """Test that auto-merge success triggers issue closing."""
        config = AutomationConfig()
        config.MERGE_AUTO = True
        config.MERGE_METHOD = "--squash"

        mock_api = Mock()
        mock_get_ghapi_client.return_value = mock_api
        mock_github_client.get_instance.return_value.token = "token"

        # Mock successful merge (API handles it same as direct now)
        mock_api.pulls.merge.return_value = {"merged": True}

        result = _merge_pr("test/repo", 123, {}, config)

        assert result is True
        mock_close_issues.assert_called_once_with("test/repo", 123)
        mock_archive_session.assert_called_once_with("test/repo", 123)
