import pytest
from unittest.mock import Mock, patch, MagicMock
from src.auto_coder.util.gh_cache import GitHubClient


class TestGitHubClientREST:
    """Test cases for GitHubClient REST API methods."""

    @pytest.fixture
    def mock_github_token(self):
        return "test_token"

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_open_prs_json_rest(self, mock_get_ghapi_client, mock_github_token):
        """Test get_open_prs_json uses REST API correctly."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api

        # Mock List PRs - return plain dicts as if from cache
        mock_pr_summary = {"number": 123}
        mock_api.pulls.list.return_value = [mock_pr_summary]

        # Mock Get PR Details - return plain dict
        mock_pr_detail = {
            "number": 123,
            "title": "Test PR",
            "node_id": "PR_kw...",
            "body": "Body",
            "state": "open",
            "html_url": "http://url",
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "draft": False,
            "mergeable": True,
            "head": {"ref": "head-branch", "sha": "head-sha"},
            "base": {"ref": "base-branch"},
            "user": {"login": "author"},
            "assignees": [{"login": "assignee"}],
            "labels": [{"name": "label"}],
            "comments": 1,
            "review_comments": 1,
            "commits": 5,
            "additions": 10,
            "deletions": 2,
            "changed_files": 1,
        }

        mock_api.pulls.get.return_value = mock_pr_detail

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_open_prs_json("owner/repo")

        # Assert
        assert len(result) == 1
        pr = result[0]
        assert pr["number"] == 123
        assert pr["title"] == "Test PR"
        assert pr["node_id"] == "PR_kw..."
        assert pr["mergeable"] is True
        assert pr["comments_count"] == 2  # 1 + 1
        assert pr["commits_count"] == 5

        mock_api.pulls.list.assert_called_once_with("owner", "repo", state="open", per_page=100)
        mock_api.pulls.get.assert_called_once_with("owner", "repo", 123)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_open_issues_json_rest(self, mock_get_ghapi_client, mock_github_token):
        """Test get_open_issues_json uses REST API correctly."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api

        # Mock List Issues - plain dicts
        issue_obj = {
            "number": 456,
            "title": "Test Issue",
            "body": "Issue Body",
            "state": "open",
            "labels": [{"name": "bug"}],
            "assignees": [{"login": "dev"}],
            "created_at": "2024-01-01",
            "updated_at": "2024-01-02",
            "html_url": "http://issue-url",
            "user": {"login": "user"},
            "comments": 3,
            "parent_issue_url": "https://api.github.com/repos/owner/repo/issues/999",
            # No 'pull_request' key
        }

        pr_obj = {"number": 789, "pull_request": {}, "title": "PR in disguise"}  # It's a PR

        mock_api.issues.list_for_repo.return_value = [issue_obj, pr_obj]

        client = GitHubClient.get_instance(mock_github_token)
        # Clear cache first to force fetch
        client._open_issues_cache = None

        # Mock the helper methods that are called for each issue
        with patch.object(client, "get_linked_prs", return_value=[1, 2]) as mock_linked, patch.object(client, "get_open_sub_issues", return_value=[10, 11]) as mock_sub:

            # Execute
            result = client.get_open_issues_json("owner/repo")

            # Assert
            assert len(result) == 1
            issue = result[0]
            assert issue["number"] == 456
            assert issue["linked_prs"] == [1, 2]
            assert issue["has_linked_prs"] is True
            assert issue["open_sub_issue_numbers"] == [10, 11]
            assert issue["has_open_sub_issues"] is True
            assert issue["parent_issue_number"] == 999

            mock_api.issues.list_for_repo.assert_called_once_with("owner", "repo", state="open", per_page=100)
            mock_linked.assert_called_once_with("owner/repo", 456)
            mock_sub.assert_called_once_with("owner/repo", 456)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_issue_rest(self, mock_get_ghapi_client, mock_github_token):
        """Test get_issue uses REST API correctly."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api

        # Mock Issue
        mock_issue = Mock()
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.body = "Body"
        mock_api.issues.get.return_value = mock_issue

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_issue("owner/repo", 123)

        # Assert
        assert result.number == 123
        assert result.title == "Test Issue"
        mock_api.issues.get.assert_called_once_with("owner", "repo", 123)

    @patch("src.auto_coder.util.gh_cache.get_ghapi_client")
    def test_get_parent_issue_details_rest(self, mock_get_ghapi_client, mock_github_token):
        """Test get_parent_issue_details uses REST API correctly."""
        # Setup
        mock_api = MagicMock()
        mock_get_ghapi_client.return_value = mock_api

        # Mock parent response
        mock_parent_response = {
            "number": 123,
            "title": "Parent Issue",
            "body": "Parent Body",
        }

        # The client calls api(path, verb="GET", ...)
        mock_api.return_value = mock_parent_response

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_parent_issue_details("owner/repo", 456)

        # Assert
        assert result is not None
        assert result["number"] == 123
        assert result["title"] == "Parent Issue"

        # Verify call args
        mock_api.assert_called_once()
        args, kwargs = mock_api.call_args
        assert args[0] == "/repos/owner/repo/issues/456/parent"
        assert kwargs["verb"] == "GET"
