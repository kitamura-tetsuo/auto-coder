"""
Tests for GitHub client functionality.
"""

import json
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

import pytest

from src.auto_coder.util.gh_cache import GitHubClient


class AttrDict(dict):
    """Helper class to mock GhApi's AttrDict behavior."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value


class TestGitHubClient:
    """Test cases for GitHubClient class."""

    def test_init(self, mock_github_token):
        """Test GitHubClient initialization."""
        client = GitHubClient.get_instance(mock_github_token)
        assert client.token == mock_github_token

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_repository_success(self, mock_get_client, mock_github_token):
        """Test successful repository retrieval."""
        # Setup
        mock_api = Mock()
        mock_repo = AttrDict({"name": "repo", "owner": {"login": "test"}})
        mock_api.repos.get.return_value = mock_repo
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_repository("test/repo")

        # Assert
        assert result == mock_repo
        mock_api.repos.get.assert_called_once_with("test", "repo")

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_repository_failure(self, mock_get_client, mock_github_token):
        """Test repository retrieval failure."""
        # Setup
        mock_api = Mock()
        mock_api.repos.get.side_effect = Exception("Not Found")
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        # Execute & Assert
        with pytest.raises(Exception):
            client.get_repository("test/nonexistent")

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_open_issues_success(self, mock_get_client, mock_github_token):
        """Test successful open issues retrieval."""
        # Setup
        mock_api = Mock()

        # Issue 1 - Regular issue
        issue1 = AttrDict({"number": 1, "title": "Issue 1", "created_at": "2024-01-01T00:00:00Z"})

        # Issue 2 - Pull Request (should be filtered)
        issue2 = AttrDict({"number": 2, "title": "PR 1", "pull_request": {}, "created_at": "2024-01-02T00:00:00Z"})

        # Issue 3 - Regular issue
        issue3 = AttrDict({"number": 3, "title": "Issue 3", "created_at": "2024-01-03T00:00:00Z"})

        mock_api.issues.list_for_repo.return_value = [issue1, issue2, issue3]
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_open_issues("test/repo", limit=10)

        # Assert
        assert len(result) == 2
        assert issue1 in result
        assert issue3 in result
        assert issue2 not in result
        mock_api.issues.list_for_repo.assert_called_once_with("test", "repo", state="open", sort="created", direction="asc", per_page=10)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_open_pull_requests_success(self, mock_get_client, mock_github_token):
        """Test successful open pull requests retrieval."""
        # Setup
        mock_api = Mock()
        pr1 = AttrDict({"number": 1, "title": "PR 1"})
        pr2 = AttrDict({"number": 2, "title": "PR 2"})

        mock_api.pulls.list.return_value = [pr1, pr2]
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_open_pull_requests("test/repo", limit=1)

        # Assert
        # Limit handled manually in python
        assert len(result) == 1
        assert result[0] == pr1
        mock_api.pulls.list.assert_called_once_with("test", "repo", state="open", sort="created", direction="asc", per_page=1)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_find_pr_by_head_branch_found(self, mock_get_client, mock_github_token):
        """Test finding PR by head branch when PR exists."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)

        # Mock open PRs
        pr1 = AttrDict({"number": 123, "title": "Feature", "head": AttrDict({"ref": "feature-branch"}), "base": AttrDict({"ref": "main"}), "state": "open", "body": "", "html_url": "url", "user": AttrDict({"login": "user"}), "created_at": "date", "updated_at": "date"})

        pr2 = AttrDict({"number": 456, "title": "Other", "head": AttrDict({"ref": "other"}), "base": AttrDict({"ref": "main"}), "state": "open"})

        # Mock get_open_pull_requests calls
        with patch.object(client, "get_open_pull_requests", return_value=[pr1, pr2]):

            # Execute
            result = client.find_pr_by_head_branch("test/repo", "feature-branch")

            # Assert
            assert result is not None
            assert result["number"] == 123
            assert result["head_branch"] == "feature-branch"

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_find_pr_by_head_branch_not_found(self, mock_get_client, mock_github_token):
        """Test finding PR by head branch when PR does not exist."""
        client = GitHubClient.get_instance(mock_github_token)

        with patch.object(client, "get_open_pull_requests", return_value=[]):
            result = client.find_pr_by_head_branch("test/repo", "non-existent")
            assert result is None

    def test_get_issue_details(self, mock_github_token):
        """Test issue details extraction."""
        client = GitHubClient.get_instance(mock_github_token)

        mock_issue = AttrDict(
            {
                "number": 123,
                "title": "Test Issue",
                "body": "Test body",
                "state": "open",
                "labels": [AttrDict({"name": "bug"}), AttrDict({"name": "high-priority"})],
                "assignees": [AttrDict({"login": "testuser"})],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/test/repo/issues/123",
                "user": AttrDict({"login": "author"}),
                "comments": 5,
            }
        )

        result = client.get_issue_details(mock_issue)

        expected = {
            "number": 123,
            "title": "Test Issue",
            "body": "Test body",
            "state": "open",
            "labels": ["bug", "high-priority"],
            "assignees": ["testuser"],
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "url": "https://github.com/test/repo/issues/123",
            "author": "author",
            "comments_count": 5,
        }
        assert result == expected

    def test_get_pr_details(self, mock_github_token):
        """Test PR details extraction."""
        client = GitHubClient.get_instance(mock_github_token)

        mock_pr = AttrDict(
            {
                "number": 456,
                "title": "Test PR",
                "body": "Test PR body",
                "state": "open",
                "labels": [AttrDict({"name": "feature"})],
                "assignees": [AttrDict({"login": "testuser"})],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "html_url": "https://github.com/test/repo/pull/456",
                "user": AttrDict({"login": "author"}),
                "head": AttrDict({"ref": "feature-branch"}),
                "base": AttrDict({"ref": "main"}),
                "mergeable": True,
                "draft": False,
                "comments": 2,
                "review_comments": 1,
                "commits": 3,
                "additions": 50,
                "deletions": 10,
                "changed_files": 2,
            }
        )

        result = client.get_pr_details(mock_pr)

        expected = {
            "number": 456,
            "title": "Test PR",
            "body": "Test PR body",
            "state": "open",
            "labels": ["feature"],
            "assignees": ["testuser"],
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "url": "https://github.com/test/repo/pull/456",
            "author": "author",
            "head_branch": "feature-branch",
            "base_branch": "main",
            "mergeable": True,
            "draft": False,
            "comments_count": 2,
            "review_comments_count": 1,
            "commits_count": 3,
            "additions": 50,
            "deletions": 10,
            "changed_files": 2,
        }
        assert result == expected

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_create_issue_success(self, mock_get_client, mock_github_token):
        """Test successful issue creation."""
        mock_api = Mock()
        created_issue = AttrDict({"number": 789, "title": "New Issue"})
        mock_api.issues.create.return_value = created_issue
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        result = client.create_issue("test/repo", "New Issue", "Issue body", ["bug"])

        assert result == created_issue
        mock_api.issues.create.assert_called_once_with("test", "repo", title="New Issue", body="Issue body", labels=["bug"])

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_add_comment_to_issue_success(self, mock_get_client, mock_github_token):
        """Test successful comment addition to issue."""
        mock_api = Mock()
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        client.add_comment_to_issue("test/repo", 123, "Test comment")

        mock_api.issues.create_comment.assert_called_once_with("test", "repo", 123, body="Test comment")

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_close_issue_success(self, mock_get_client, mock_github_token):
        """Test successful issue closure."""
        mock_api = Mock()
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        client.close_issue("test/repo", 123, "Closing comment")

        # Verify comment created
        mock_api.issues.create_comment.assert_called_once_with("test", "repo", 123, body="Closing comment")
        # Verify issue closed
        mock_api.issues.update.assert_called_once_with("test", "repo", 123, state="closed")

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_add_labels(self, mock_get_client, mock_github_token):
        """Test successful label addition."""
        mock_api = Mock()
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        client.add_labels("test/repo", 123, ["feature", "priority"])

        mock_api.issues.add_labels.assert_called_once_with("test", "repo", 123, labels=["feature", "priority"])

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_try_add_labels_success(self, mock_get_client, mock_github_token):
        """Test try_add_labels adds labels if new."""
        mock_api = Mock()
        # Mock current labels
        mock_api.issues.get.return_value = AttrDict({"labels": [{"name": "bug"}]})
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        result = client.try_add_labels("test/repo", 123, ["feature"])

        assert result is True
        mock_api.issues.add_labels.assert_called_once_with("test", "repo", 123, labels=["feature"])

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_try_add_labels_skip_existing(self, mock_get_client, mock_github_token):
        """Test try_add_labels skips if label exists."""
        mock_api = Mock()
        # Mock current labels
        mock_api.issues.get.return_value = AttrDict({"labels": [{"name": "feature"}]})
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        result = client.try_add_labels("test/repo", 123, ["feature"])

        assert result is False
        mock_api.issues.add_labels.assert_not_called()

    @patch("src.auto_coder.util.gh_cache.GitHubClient.get_linked_prs")
    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_has_linked_pr_with_linked_pr(self, mock_get_client, mock_get_linked, mock_github_token):
        """Test has_linked_pr returns True when PR references issue."""
        mock_get_linked.return_value = [456]

        mock_api = Mock()
        # Mock PR details check (must be open)
        mock_api.pulls.get.return_value = AttrDict({"state": "open"})
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        result = client.has_linked_pr("test/repo", 123)

        assert result is True
        mock_api.pulls.get.assert_called_once_with("test", "repo", 456)

    @patch("src.auto_coder.util.gh_cache.GitHubClient.get_linked_prs")
    @patch("src.auto_coder.util.gh_cache.GitHubClient.get_open_pull_requests")
    def test_has_linked_pr_via_text_fallback(self, mock_get_open_prs, mock_get_linked, mock_github_token):
        """Test has_linked_pr via text fallback."""
        mock_get_linked.return_value = []

        pr = AttrDict({"number": 789, "title": "Fixes #123", "body": "description"})
        mock_get_open_prs.return_value = [pr]

        client = GitHubClient.get_instance(mock_github_token)

        result = client.has_linked_pr("test/repo", 123)

        assert result is True

    @patch("src.auto_coder.util.gh_cache.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_find_closing_pr_success(self, mock_get_client, mock_get_timeline, mock_github_token):
        """Test find_closing_pr finds open closing PR."""
        mock_get_timeline.return_value = [{"event": "connected", "source": {"issue": {"number": 200, "pull_request": {}}}}]

        mock_api = Mock()
        # Check if PR open
        mock_api.pulls.get.return_value = AttrDict({"state": "open"})
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        result = client.find_closing_pr("test/repo", 123)

        assert result == 200

    @patch("src.auto_coder.util.gh_cache.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.util.gh_cache.GitHubClient.get_open_pull_requests")
    def test_find_closing_pr_fallback(self, mock_get_open_prs, mock_get_timeline, mock_github_token):
        """Test find_closing_pr falls back to text search."""
        mock_get_timeline.return_value = []

        pr = AttrDict({"number": 789, "title": "Closes #123"})
        mock_get_open_prs.return_value = [pr]

        client = GitHubClient.get_instance(mock_github_token)

        result = client.find_closing_pr("test/repo", 123)

        assert result == 789

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_search_issues(self, mock_get_client, mock_github_token):
        """Test search_issues call."""
        mock_api = Mock()
        mock_api.search.issues_and_pull_requests.return_value = {"items": [AttrDict({"number": 1, "title": "Found"})]}
        mock_get_client.return_value = mock_api

        client = GitHubClient.get_instance(mock_github_token)

        result = client.search_issues("query")

        assert len(result) == 1
        assert result[0].number == 1
        mock_api.search.issues_and_pull_requests.assert_called_once_with(q="query", sort="updated", order="desc")

    @patch("src.auto_coder.util.gh_cache.GitHubClient.get_issue")
    @patch("src.auto_coder.util.gh_cache.GitHubClient.get_issue_details")
    def test_check_issue_dependencies_resolved(self, mock_details, mock_get_issue, mock_github_token):
        """Test check_issue_dependencies_resolved."""
        client = GitHubClient.get_instance(mock_github_token)

        # Mock get_issue to return something
        mock_get_issue.return_value = Mock()

        # Mock details: 100 closed, 200 open
        mock_details.side_effect = [{"number": 100, "state": "closed"}, {"number": 200, "state": "open"}]

        result = client.check_issue_dependencies_resolved("test/repo", [100, 200])

        assert result == [200]
