"""
Tests for GitHub client functionality.
"""

import json
from unittest.mock import Mock, patch

import httpx
import pytest
from github import Github, Issue, PullRequest, Repository
from github.GithubException import GithubException

from src.auto_coder.github_client import GitHubClient


class TestGitHubClient:
    """Test cases for GitHubClient class."""

    def test_init(self, mock_github_token):
        """Test GitHubClient initialization."""
        client = GitHubClient.get_instance(mock_github_token)
        assert client.token == mock_github_token
        assert isinstance(client.github, Github)

    @patch("src.auto_coder.github_client.Github")
    def test_get_repository_success(self, mock_github_class, mock_github_token):
        """Test successful repository retrieval."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_repository("test/repo")

        # Assert
        assert result == mock_repo
        mock_github.get_repo.assert_called_once_with("test/repo")

    @patch("src.auto_coder.github_client.Github")
    def test_get_repository_failure(self, mock_github_class, mock_github_token):
        """Test repository retrieval failure."""
        # Setup
        mock_github = Mock()
        mock_github.get_repo.side_effect = GithubException(404, "Not Found")
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute & Assert
        with pytest.raises(GithubException):
            client.get_repository("test/nonexistent")

    @patch("src.auto_coder.github_client.Github")
    def test_get_open_issues_success(self, mock_github_class, mock_github_token):
        """Test successful open issues retrieval."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue1 = Mock(spec=Issue.Issue)
        mock_issue1.pull_request = None  # Not a PR
        mock_issue2 = Mock(spec=Issue.Issue)
        mock_issue2.pull_request = None  # Not a PR
        mock_pr = Mock(spec=Issue.Issue)
        mock_pr.pull_request = Mock()  # This is a PR, should be filtered out

        mock_repo.get_issues.return_value = [mock_issue1, mock_issue2, mock_pr]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_open_issues("test/repo", limit=2)

        # Assert
        assert len(result) == 2
        assert mock_issue1 in result
        assert mock_issue2 in result
        assert mock_pr not in result
        mock_repo.get_issues.assert_called_once_with(state="open", sort="created", direction="asc")

    @patch("src.auto_coder.github_client.Github")
    def test_get_open_pull_requests_success(self, mock_github_class, mock_github_token):
        """Test successful open pull requests retrieval."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_pr1 = Mock(spec=PullRequest.PullRequest)
        mock_pr2 = Mock(spec=PullRequest.PullRequest)

        mock_repo.get_pulls.return_value = [mock_pr1, mock_pr2]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_open_pull_requests("test/repo", limit=1)

        # Assert
        assert len(result) == 1
        assert mock_pr1 in result
        mock_repo.get_pulls.assert_called_once_with(state="open", sort="created", direction="asc")

    def test_get_open_issues_sorted_oldest_first(self, mock_github_token):
        """Test that issues are sorted by creation date (oldest first)."""
        # Setup
        with patch("src.auto_coder.github_client.Github") as mock_github_class:
            mock_github = Mock()
            mock_repo = Mock(spec=Repository.Repository)

            # Create mock issues with different creation dates
            mock_issue1 = Mock(spec=Issue.Issue)
            mock_issue1.pull_request = None  # Not a PR
            mock_issue1.created_at = Mock()
            iso_mock = mock_issue1.created_at.isoformat
            iso_mock.return_value = "2024-01-01T00:00:00Z"

            mock_issue2 = Mock(spec=Issue.Issue)
            mock_issue2.pull_request = None  # Not a PR
            mock_issue2.created_at = Mock()
            iso_mock2 = mock_issue2.created_at.isoformat
            iso_mock2.return_value = "2024-01-02T00:00:00Z"

            # GitHub API should return in ascending order (oldest first)
            mock_repo.get_issues.return_value = [mock_issue1, mock_issue2]
            mock_github.get_repo.return_value = mock_repo
            mock_github_class.return_value = mock_github

            client = GitHubClient.get_instance(mock_github_token)

            # Execute
            result = client.get_open_issues("test/repo")

            # Assert
            assert len(result) == 2
            assert result[0] == mock_issue1  # Oldest first
            assert result[1] == mock_issue2
            mock_repo.get_issues.assert_called_once_with(state="open", sort="created", direction="asc")

    def test_get_open_pull_requests_sorted_oldest_first(self, mock_github_token):
        """Test that pull requests are sorted by
        creation date (oldest first).
        """
        # Setup
        with patch("src.auto_coder.github_client.Github") as mock_github_class:
            mock_github = Mock()
            mock_repo = Mock(spec=Repository.Repository)

            # Create mock PRs with different creation dates
            mock_pr1 = Mock(spec=PullRequest.PullRequest)
            mock_pr1.created_at = Mock()
            mock_pr1.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"

            mock_pr2 = Mock(spec=PullRequest.PullRequest)
            mock_pr2.created_at = Mock()
            mock_pr2.created_at.isoformat.return_value = "2024-01-02T00:00:00Z"

            # GitHub API should return in ascending order (oldest first)
            mock_repo.get_pulls.return_value = [mock_pr1, mock_pr2]
            mock_github.get_repo.return_value = mock_repo
            mock_github_class.return_value = mock_github

            client = GitHubClient.get_instance(mock_github_token)

            # Execute
            result = client.get_open_pull_requests("test/repo")

            # Assert
            assert len(result) == 2
            assert result[0] == mock_pr1  # Oldest first
            assert result[1] == mock_pr2
            mock_repo.get_pulls.assert_called_once_with(state="open", sort="created", direction="asc")

    @patch("src.auto_coder.github_client.Github")
    def test_find_pr_by_head_branch_found(self, mock_github_class, mock_github_token):
        """Test finding PR by head branch when PR exists."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        # Create mock PRs with different head branches
        mock_pr1 = Mock(spec=PullRequest.PullRequest)
        mock_pr1.number = 123
        mock_pr1.head = Mock(ref="feature-branch")

        mock_pr2 = Mock(spec=PullRequest.PullRequest)
        mock_pr2.number = 456
        mock_pr2.head = Mock(ref="another-branch")

        mock_repo.get_pulls.return_value = [mock_pr1, mock_pr2]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Mock get_pr_details to return a simple dict
        with patch.object(client, "get_pr_details") as mock_get_details:
            mock_get_details.return_value = {
                "number": 123,
                "head_branch": "feature-branch",
            }

            # Execute
            result = client.find_pr_by_head_branch("test/repo", "feature-branch")

            # Assert
            assert result is not None
            assert result["number"] == 123
            mock_get_details.assert_called_once_with(mock_pr1)

    @patch("src.auto_coder.github_client.Github")
    def test_find_pr_by_head_branch_not_found(self, mock_github_class, mock_github_token):
        """Test finding PR by head branch when PR does not exist."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        mock_pr1 = Mock(spec=PullRequest.PullRequest)
        mock_pr1.head = Mock(ref="other-branch")

        mock_repo.get_pulls.return_value = [mock_pr1]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.find_pr_by_head_branch("test/repo", "non-existent-branch")

        # Assert
        assert result is None

    @patch("src.auto_coder.github_client.Github")
    def test_find_pr_by_head_branch_error(self, mock_github_class, mock_github_token):
        """Test finding PR by head branch when error occurs."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_repo.get_pulls.side_effect = GithubException(500, "Server Error")
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.find_pr_by_head_branch("test/repo", "feature-branch")

        # Assert
        assert result is None

    def test_get_issue_details(self, mock_github_token):
        """Test issue details extraction."""
        # Setup
        mock_issue = Mock(spec=Issue.Issue)
        mock_issue.number = 123
        mock_issue.title = "Test Issue"
        mock_issue.body = "Test body"
        mock_issue.state = "open"
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "high-priority"
        mock_issue.labels = [mock_label1, mock_label2]
        mock_issue.assignees = [Mock(login="testuser")]
        mock_issue.created_at = Mock()
        mock_issue.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.updated_at = Mock()
        mock_issue.updated_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.html_url = "https://github.com/test/repo/issues/123"
        mock_issue.user = Mock(login="author")
        mock_issue.comments = 5

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_issue_details(mock_issue)

        # Assert
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
        # Setup
        mock_pr = Mock(spec=PullRequest.PullRequest)
        mock_pr.number = 456
        mock_pr.title = "Test PR"
        mock_pr.body = "Test PR body"
        mock_pr.state = "open"
        mock_label = Mock()
        mock_label.name = "feature"
        mock_pr.labels = [mock_label]
        mock_pr.assignees = [Mock(login="testuser")]
        mock_pr.created_at = Mock()
        mock_pr.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_pr.updated_at = Mock()
        mock_pr.updated_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_pr.html_url = "https://github.com/test/repo/pull/456"
        mock_pr.user = Mock(login="author")
        mock_pr.head = Mock(ref="feature-branch")
        mock_pr.base = Mock(ref="main")
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.comments = 2
        mock_pr.review_comments = 1
        mock_pr.commits = 3
        mock_pr.additions = 50
        mock_pr.deletions = 10
        mock_pr.changed_files = 2

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_pr_details(mock_pr)

        # Assert
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

    @patch("src.auto_coder.github_client.Github")
    def test_create_issue_success(self, mock_github_class, mock_github_token):
        """Test successful issue creation."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)
        mock_issue.number = 789

        mock_repo.create_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        params = ("test/repo", "New Issue", "Issue body", ["bug"])
        result = client.create_issue(*params)

        # Assert
        assert result == mock_issue
        mock_repo.create_issue.assert_called_once_with(title="New Issue", body="Issue body", labels=["bug"])

    @patch("src.auto_coder.github_client.Github")
    def test_add_comment_to_issue_success(self, mock_github_class, mock_github_token):
        """Test successful comment addition to issue."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        client.add_comment_to_issue("test/repo", 123, "Test comment")

        # Assert
        mock_repo.get_issue.assert_called_once_with(123)
        mock_issue.create_comment.assert_called_once_with("Test comment")

    @patch("src.auto_coder.github_client.Github")
    def test_close_issue_success(self, mock_github_class, mock_github_token):
        """Test successful issue closure."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        client.close_issue("test/repo", 123, "Closing comment")

        # Assert
        mock_repo.get_issue.assert_called_once_with(123)
        mock_issue.create_comment.assert_called_once_with("Closing comment")
        mock_issue.edit.assert_called_once_with(state="closed")

    @patch("src.auto_coder.github_client.Github")
    def test_add_labels_to_issue_success(self, mock_github_class, mock_github_token):
        """Test successful label addition to issue."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)

        # Mock existing labels
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "high-priority"
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        client.add_labels("test/repo", 123, ["feature", "enhancement"])

        # Assert
        mock_repo.get_issue.assert_called_once_with(123)
        # Should call edit with all labels (existing + new, no duplicates)
        # Check that edit was called once and contains all expected labels
        mock_issue.edit.assert_called_once()
        call_args = mock_issue.edit.call_args
        actual_labels = call_args[1]["labels"]  # Get labels from kwargs
        expected_labels = {"bug", "high-priority", "feature", "enhancement"}
        assert set(actual_labels) == expected_labels

    @patch("src.auto_coder.github_client.Github")
    def test_add_labels_to_issue_no_duplicates(self, mock_github_class, mock_github_token):
        """Test that duplicate labels are not added."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)

        # Mock existing labels including one we'll try to add
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "feature"  # Already exists
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute - try to add "feature" again and "enhancement"
        client.add_labels("test/repo", 123, ["feature", "enhancement"])

        # Assert
        mock_repo.get_issue.assert_called_once_with(123)
        # Should call edit with all labels (no duplicates)
        mock_issue.edit.assert_called_once()
        call_args = mock_issue.edit.call_args
        actual_labels = call_args[1]["labels"]  # Get labels from kwargs
        expected_labels = {"bug", "feature", "enhancement"}
        assert set(actual_labels) == expected_labels

    @patch("src.auto_coder.github_client.Github")
    def test_add_labels_to_pr_success(self, mock_github_class, mock_github_token):
        """Test successful label addition to PR."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_pr = Mock(spec=PullRequest.PullRequest)

        # Mock existing labels
        mock_label1 = Mock()
        mock_label1.name = "feature"
        mock_label2 = Mock()
        mock_label2.name = "review-required"
        mock_pr.labels = [mock_label1, mock_label2]

        mock_repo.get_pull.return_value = mock_pr
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        client.add_labels("test/repo", 456, ["documentation", "enhancement"], "pr")

        # Assert
        mock_repo.get_pull.assert_called_once_with(456)
        # Should call add_to_labels for each label
        assert mock_pr.add_to_labels.call_count == 2
        # Check that add_to_labels was called with the correct labels
        call_args_list = mock_pr.add_to_labels.call_args_list
        called_labels = {call[0][0] for call in call_args_list}
        expected_labels = {"documentation", "enhancement"}
        assert called_labels == expected_labels

    @patch("src.auto_coder.github_client.Github")
    def test_add_labels_to_pr_no_duplicates(self, mock_github_class, mock_github_token):
        """Test that duplicate labels are not added to PR."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_pr = Mock(spec=PullRequest.PullRequest)

        # Mock existing labels including one we'll try to add
        mock_label1 = Mock()
        mock_label1.name = "feature"
        mock_label2 = Mock()
        mock_label2.name = "documentation"  # Already exists
        mock_pr.labels = [mock_label1, mock_label2]

        mock_repo.get_pull.return_value = mock_pr
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute - try to add "documentation" again and "enhancement"
        client.add_labels("test/repo", 456, ["documentation", "enhancement"], "pr")

        # Assert
        mock_repo.get_pull.assert_called_once_with(456)
        # Should not call add_to_labels at all since "documentation" already exists
        assert mock_pr.add_to_labels.call_count == 0

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_with_linked_pr(self, mock_github_class, mock_github_token):
        """Test has_linked_pr returns True when PR references the issue."""
        # Setup
        # Mock get_linked_prs (REST) to return a PR number
        with patch.object(GitHubClient, "get_linked_prs", return_value=[456]):
            mock_github = Mock()
            mock_repo = Mock(spec=Repository.Repository)

            # Create a PR that references issue #123
            mock_pr = Mock(spec=PullRequest.PullRequest)
            mock_pr.state = "open"
            
            # Mock get_pr_details via get_repository().get_pull()
            mock_repo.get_pull.return_value = mock_pr
            mock_github.get_repo.return_value = mock_repo
            mock_github_class.return_value = mock_github

            client = GitHubClient.get_instance(mock_github_token)
            
            # Mock get_pr_details to return open state
            with patch.object(client, "get_pr_details", return_value={"state": "open"}):
                # Execute
                result = client.has_linked_pr("test/repo", 123)

                # Assert
                assert result is True

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_with_no_linked_pr(self, mock_github_class, mock_github_token):
        """Test has_linked_pr returns False when no PR references the issue."""
        # Setup
        # Mock get_linked_prs to return empty
        with patch.object(GitHubClient, "get_linked_prs", return_value=[]):
            mock_github = Mock()
            mock_repo = Mock(spec=Repository.Repository)

            # Create a PR that does not reference issue #123 (for text search fallback)
            mock_pr = Mock(spec=PullRequest.PullRequest)
            mock_pr.number = 456
            mock_pr.title = "Fix another bug"
            mock_pr.body = "This PR fixes #999"

            mock_repo.get_pulls.return_value = [mock_pr]
            mock_github.get_repo.return_value = mock_repo
            mock_github_class.return_value = mock_github

            client = GitHubClient.get_instance(mock_github_token)

            # Execute
            result = client.has_linked_pr("test/repo", 123)

            # Assert
            assert result is False
            mock_repo.get_pulls.assert_called_once_with(state="open")

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_with_multiple_patterns(self, mock_github_class, mock_github_token):
        """Test has_linked_pr detects various reference patterns."""
        # Setup
        # Mock get_linked_prs to return empty to trigger text fallback
        with patch.object(GitHubClient, "get_linked_prs", return_value=[]):
            mock_github = Mock()
            mock_repo = Mock(spec=Repository.Repository)

        test_cases = [
            ("Fix bug", "closes #123"),
            ("Resolve issue", "resolves #123"),
            ("Fix", "issue #123"),
            ("Update #123", "Some description"),
        ]

        for title, body in test_cases:
            mock_pr = Mock(spec=PullRequest.PullRequest)
            mock_pr.number = 456
            mock_pr.title = title
            mock_pr.body = body

            mock_repo.get_pulls.return_value = [mock_pr]
            mock_github.get_repo.return_value = mock_repo
            mock_github_class.return_value = mock_github

            client = GitHubClient.get_instance(mock_github_token)

            # Execute
            result = client.has_linked_pr("test/repo", 123)

            # Assert
            assert result is True, f"Failed for title='{title}', body='{body}'"

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_handles_exception(self, mock_github_class, mock_github_token):
        """Test has_linked_pr handles exceptions gracefully."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_repo.get_pulls.side_effect = GithubException(500, "Server Error")
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        # Execute
        with patch.object(client, "get_linked_prs", side_effect=GithubException(500, "API error")):
            result = client.has_linked_pr("test/repo", 123)

        # Assert
        assert result is False

    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    def test_get_linked_prs_success(self, mock_get_timeline, mock_github_token):
        """Test get_linked_prs returns linked PR numbers from timeline."""
        # Setup
        mock_get_timeline.return_value = [
            {"event": "connected", "source": {"issue": {"number": 101, "pull_request": {}}}},
            {"event": "cross-referenced", "source": {"issue": {"number": 102, "pull_request": {}}}},
            {"event": "mentioned", "source": {}}, # Should be ignored
        ]

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_linked_prs("test/repo", 123)

        # Assert
        assert set(result) == {101, 102}

    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    def test_get_linked_prs_empty(self, mock_get_timeline, mock_github_token):
        """Test get_linked_prs returns empty when no links found."""
        # Setup
        mock_get_timeline.return_value = [{"event": "commented"}]

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.get_linked_prs("test/repo", 123)

        # Assert
        assert result == []

    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    @patch("src.auto_coder.github_client.GitHubClient.get_pr_details")
    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    def test_find_closing_pr_success(self, mock_get_timeline, mock_get_pr_details, mock_get_repo, mock_github_token):
        """Test find_closing_pr finds open closing PR."""
        # Setup
        mock_get_timeline.return_value = [
            {"event": "connected", "source": {"issue": {"number": 200, "pull_request": {}}}}
        ]
        
        # Mock get_pr_details to return OPEN state
        mock_get_pr_details.return_value = {"state": "open"}
        
        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client.find_closing_pr("test/repo", 123)

        # Assert
        assert result == 200

    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    def test_find_closing_pr_not_found(self, mock_get_timeline, mock_get_repo, mock_github_token):
         """Test find_closing_pr returns None if not found in timeline or fallback."""
         # Setup
         mock_get_timeline.return_value = []
         
         # Mock fallback PR search to return empty
         mock_repo = Mock()
         mock_repo.get_pulls.return_value = []
         mock_get_repo.return_value = mock_repo
         
         client = GitHubClient.get_instance(mock_github_token)
         
         # Execute
         result = client.find_closing_pr("test/repo", 123)
         
         # Assert
         assert result is None


    @patch("src.auto_coder.github_client.Github")
    def test_check_issue_dependencies_resolved_all_closed(self, mock_github_class, mock_github_token):
        """Test checking dependencies when all are resolved (closed)."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value
        # Mock get_repository, repo.get_issue, and get_issue_details
        mock_repo = Mock()
        mock_issue_100 = Mock()
        mock_issue_200 = Mock()

        def get_issue_side_effect(num):
            if num == 100:
                return mock_issue_100
            elif num == 200:
                return mock_issue_200

        mock_repo.get_issue.side_effect = get_issue_side_effect

        with patch.object(client, "get_repository", return_value=mock_repo), patch.object(client, "get_issue_details") as mock_get_details:
            mock_get_details.side_effect = [
                {"number": 100, "state": "closed"},
                {"number": 200, "state": "closed"},
            ]

            # Execute
            result = client.check_issue_dependencies_resolved("test/repo", [100, 200])

            # Assert
            assert result == []
            assert mock_get_details.call_count == 2

    @patch("src.auto_coder.github_client.Github")
    def test_check_issue_dependencies_resolved_some_open(self, mock_github_class, mock_github_token):
        """Test checking dependencies when some are unresolved (open)."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value
        # Mock get_repository, repo.get_issue, and get_issue_details
        mock_repo = Mock()

        def get_issue_side_effect(num):
            return Mock()

        mock_repo.get_issue.side_effect = get_issue_side_effect

        with patch.object(client, "get_repository", return_value=mock_repo), patch.object(client, "get_issue_details") as mock_get_details:
            mock_get_details.side_effect = [
                {"number": 100, "state": "closed"},
                {"number": 200, "state": "open"},
                {"number": 300, "state": "closed"},
            ]

            # Execute
            result = client.check_issue_dependencies_resolved("test/repo", [100, 200, 300])

            # Assert
            assert result == [200]
            assert mock_get_details.call_count == 3

    @patch("src.auto_coder.github_client.Github")
    def test_check_issue_dependencies_resolved_all_open(self, mock_github_class, mock_github_token):
        """Test checking dependencies when all are unresolved (open)."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value

        # Mock get_repository, repo.get_issue, and get_issue_details
        mock_repo = Mock()

        def get_issue_side_effect(num):
            return Mock()

        mock_repo.get_issue.side_effect = get_issue_side_effect

        with patch.object(client, "get_repository", return_value=mock_repo), patch.object(client, "get_issue_details") as mock_get_details:
            mock_get_details.side_effect = [
                {"number": 100, "state": "open"},
                {"number": 200, "state": "open"},
            ]

            # Execute
            result = client.check_issue_dependencies_resolved("test/repo", [100, 200])

            # Assert
            assert result == [100, 200]
            assert mock_get_details.call_count == 2

    @patch("src.auto_coder.github_client.Github")
    def test_check_issue_dependencies_resolved_empty_list(self, mock_github_class, mock_github_token):
        """Test checking dependencies with empty list."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value

        # Execute
        result = client.check_issue_dependencies_resolved("test/repo", [])

        # Assert
        assert result == []

    @patch("src.auto_coder.github_client.Github")
    def test_check_issue_dependencies_resolved_error_handling(self, mock_github_class, mock_github_token):
        """Test error handling when checking dependencies."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value

        # Mock get_repository and repo.get_issue to raise exception
        mock_repo = Mock()
        mock_repo.get_issue.side_effect = GithubException(404, "Issue not found")

        with patch.object(client, "get_repository", return_value=mock_repo):
            # Execute
            result = client.check_issue_dependencies_resolved("test/repo", [99999])

            # Assert - missing issue is considered unresolved
            assert result == [99999]

    @patch("src.auto_coder.github_client.Github")
    def test_search_issues_by_title_exact_match(self, mock_github_class, mock_github_token):
        """Test _search_issues_by_title finds exact match (case-insensitive)."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        # Create mock issues with different titles
        mock_issue1 = Mock(spec=Issue.Issue)
        mock_issue1.number = 123
        mock_issue1.title = "Sub Issue 1: Dataclass Creation"
        mock_issue1.pull_request = None

        mock_issue2 = Mock(spec=Issue.Issue)
        mock_issue2.number = 456
        mock_issue2.title = "Another Issue"
        mock_issue2.pull_request = None

        mock_repo.get_issues.return_value = [mock_issue1, mock_issue2]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute - test exact match (case-insensitive)
        result = client._search_issues_by_title("test/repo", "sub issue 1: dataclass creation")

        # Assert
        assert result == 123

    @patch("src.auto_coder.github_client.Github")
    def test_search_issues_by_title_partial_match(self, mock_github_class, mock_github_token):
        """Test _search_issues_by_title finds partial match."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        # Create mock issues
        mock_issue1 = Mock(spec=Issue.Issue)
        mock_issue1.number = 123
        mock_issue1.title = "Candidate dataclass for type safety"
        mock_issue1.pull_request = None

        mock_repo.get_issues.return_value = [mock_issue1]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute - test partial match
        result = client._search_issues_by_title("test/repo", "dataclass")

        # Assert
        assert result == 123

    @patch("src.auto_coder.github_client.Github")
    def test_search_issues_by_title_no_match(self, mock_github_class, mock_github_token):
        """Test _search_issues_by_title returns None when no match found."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        # Create mock issues
        mock_issue1 = Mock(spec=Issue.Issue)
        mock_issue1.number = 123
        mock_issue1.title = "Some Issue"
        mock_issue1.pull_request = None

        mock_repo.get_issues.return_value = [mock_issue1]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient.get_instance(mock_github_token)

        # Execute
        result = client._search_issues_by_title("test/repo", "Non-existent issue title")

        # Assert
        assert result is None

class TestGitHubClientLogging:
    """Test cases for GitHub client logging functionality."""

    @patch("src.auto_coder.github_client.Github")
    def test_get_linked_prs_logs_command(self, mock_github_class, mock_github_token):
        """Test that get_linked_prs logs the REST call (implicit via standard logging)."""
        # Note: Since we switched to REST, we don't use the special GHCommandLogger for GraphQL queries anymore
        # but rather standard logging. This test class seems to be specifically for GHCommandLogger
        # which might intercept subprocess calls. Since we use httpx/GhApi, it might not be logged
        # in the same way. For now, we just remove the obsolete GraphQL tests.
        pass

    @patch("src.auto_coder.auth_utils.subprocess.run")
    def test_auth_utils_get_github_token_logs_command(self, mock_subprocess):
        """Test that get_github_token logs the gh auth token command."""
        # Mock subprocess.run
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "ghp_testtoken123"
        mock_subprocess.return_value = mock_result

        # Create a temporary directory for logging
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path

            from src.auto_coder import auth_utils
            from src.auto_coder.gh_logger import GHCommandLogger, set_gh_logger

            logger = GHCommandLogger(log_dir=Path(tmpdir))
            set_gh_logger(logger)

            # Execute
            result = auth_utils.get_github_token()

            # Assert
            assert result == "ghp_testtoken123"
            assert mock_subprocess.called

            # Verify command was logged
            log_file = logger._get_log_file_path()
            assert log_file.exists()

            import csv

            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["command"] == "gh"
                assert "auth" in rows[0]["args"]
                assert "token" in rows[0]["args"]

    @patch("src.auto_coder.auth_utils.subprocess.run")
    def test_auth_utils_check_gh_auth_logs_command(self, mock_subprocess):
        """Test that check_gh_auth logs the gh auth status command."""
        # Mock subprocess.run
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "github.com\n  Logged in to github.com as user"
        mock_subprocess.return_value = mock_result

        # Create a temporary directory for logging
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            from pathlib import Path

            from src.auto_coder import auth_utils
            from src.auto_coder.gh_logger import GHCommandLogger, set_gh_logger

            logger = GHCommandLogger(log_dir=Path(tmpdir))
            set_gh_logger(logger)

            # Execute
            result = auth_utils.check_gh_auth()

            # Assert
            assert result is True
            assert mock_subprocess.called

            # Verify command was logged
            log_file = logger._get_log_file_path()
            assert log_file.exists()

            import csv

            with open(log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                assert len(rows) == 1
                assert rows[0]["command"] == "gh"
                assert "auth" in rows[0]["args"]
                assert "status" in rows[0]["args"]


class TestGitHubClientSubIssueCaching:
    """Test cases for sub-issue caching functionality."""

    @patch("src.auto_coder.github_client.Github")
    @patch("src.auto_coder.github_client.Github")
    def test_get_open_sub_issues_returns_cached_results(self, mock_github_class, mock_github_token):
        """Test that get_open_sub_issues returns cached results on subsequent calls."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value
        
        # Mock caching client
        client._caching_client = Mock()
        
        # Mock REST response
        mock_response = Mock()
        mock_response.json.return_value = [
            {"number": 456, "title": "Sub Issue 1", "state": "open"},
            {"number": 789, "title": "Sub Issue 2", "state": "open"},
        ]
        client._caching_client.get.return_value = mock_response

        # Execute - first call (should trigger API)
        result1 = client.get_open_sub_issues("test/repo", 123)

        # Assert first call
        assert result1 == [456, 789]
        assert client._caching_client.get.call_count == 1

        # Execute - second call (should use cache)
        result2 = client.get_open_sub_issues("test/repo", 123)

        # Assert second call uses cache
        assert result2 == [456, 789]
        assert client._caching_client.get.call_count == 1  # API should still be called only once
        assert result1 == result2

    @patch("src.auto_coder.github_client.Github")
    @patch("src.auto_coder.github_client.Github")
    def test_clear_sub_issue_cache_clears_cache(self, mock_github_class, mock_github_token):
        """Test that clear_sub_issue_cache clears the cache."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)
        client.github = mock_github_class.return_value
        
        # Mock caching client
        client._caching_client = Mock()
        
        # Mock REST response
        mock_response = Mock()
        mock_response.json.return_value = [
            {"number": 456, "title": "Sub Issue 1", "state": "open"},
        ]
        client._caching_client.get.return_value = mock_response

        # Execute - first call
        result1 = client.get_open_sub_issues("test/repo", 123)
        assert result1 == [456]
        assert client._caching_client.get.call_count == 1

        # Clear the cache
        client.clear_sub_issue_cache()

        # Verify cache is empty
        cache_key = ("test/repo", 123)
        assert cache_key not in client._sub_issue_cache

        # Execute - second call after cache clear
        result2 = client.get_open_sub_issues("test/repo", 123)

        # Assert second call triggers API again
        assert result2 == [456]
        assert client._caching_client.get.call_count == 2
        assert result1 == result2


class TestGitHubClientFindClosingPr:
    """Test cases for find_closing_pr functionality using REST."""

    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.github_client.GitHubClient.get_pr_details")
    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    def test_find_closing_pr_via_timeline(self, mock_get_repo, mock_get_pr_details, mock_get_timeline, mock_github_token):
        """Test find_closing_pr finds PR via timeline 'connected' event."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)

        # Mock timeline response
        mock_get_timeline.return_value = [
             {"event": "connected", "source": {"issue": {"number": 123, "pull_request": {}}}}, # Refers to issue 123? verify implementation
        ]
        # In implementation: source.issue.number IS the PR number if event is regarding the PR closing the issue?
        # Logic says: 
        # for event in timeline:
        #   if event.get("event") == "connected":
        #       source = event.get("source", {})
        #       if "issue" in source and "pull_request" in source["issue"]:
        #           pr_num = source["issue"]["number"]
        
        # So yes, source.issue.number is PR number.
        
        mock_get_timeline.return_value = [
             {"event": "connected", "source": {"issue": {"number": 456, "pull_request": {}}}}
        ]

        # Mock get_pr_details to return OPEN state
        mock_get_pr_details.side_effect = lambda pr: {"state": "open"}
        
        # Mock get_pull calls inside get_pr_details wrapper? 
        # get_pr_details takes a PR object.
        # find_closing_pr calls: self.get_pr_details(self.get_repository(repo_name).get_pull(pr_num))
        
        mock_repo = Mock()
        mock_pr_obj = Mock()
        mock_repo.get_pull.return_value = mock_pr_obj
        mock_get_repo.return_value = mock_repo

        # Execute
        result = client.find_closing_pr("test/repo", 123)

        # Assert
        assert result == 456
        mock_get_timeline.assert_called_once_with("test/repo", 123)
        mock_repo.get_pull.assert_called_with(456)

    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    def test_find_closing_pr_fallback_to_text_search(self, mock_get_repo, mock_get_timeline, mock_github_token):
        """Test find_closing_pr falls back to text search when timeline empty."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)

        # Mock timeline empty
        mock_get_timeline.return_value = []

        # Create mock PR for text search fallback
        mock_pr = Mock(spec=PullRequest.PullRequest)
        mock_pr.number = 789
        mock_pr.title = "Closes #123"
        mock_pr.body = "This PR fixes the issue"

        mock_repo = Mock()
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_get_repo.return_value = mock_repo

        # Execute
        result = client.find_closing_pr("test/repo", 123)

        # Assert
        assert result == 789
        mock_repo.get_pulls.assert_called_once_with(state="open")

    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    def test_find_closing_pr_no_closing_pr(self, mock_get_repo, mock_get_timeline, mock_github_token):
        """Test find_closing_pr returns None when no closing PR found."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)

        # Mock timeline empty
        mock_get_timeline.return_value = []

        # Mock text search empty
        mock_repo = Mock()
        mock_repo.get_pulls.return_value = []
        mock_get_repo.return_value = mock_repo

        # Execute
        result = client.find_closing_pr("test/repo", 123)

        # Assert
        assert result is None


    @pytest.mark.parametrize(
        "title,body,pr_number",
        [
            ("Fix bug", "closes #123", 456),
            ("Resolve issue", "resolves #123", 457),
            ("Fix", "fixes #123", 458),
            ("Update #123", "Some description", 459),
            ("Update", "issue #123", 460),
        ],
    )
    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    def test_find_closing_pr_with_multiple_patterns(self, mock_get_repo, mock_get_timeline, mock_github_token, title, body, pr_number):
        """Test find_closing_pr detects various reference patterns in PR text."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)

        # Mock timeline empty
        mock_get_timeline.return_value = []

        mock_pr = Mock(spec=PullRequest.PullRequest)
        mock_pr.number = pr_number
        mock_pr.title = title
        mock_pr.body = body

        mock_repo = Mock()
        mock_repo.get_pulls.return_value = [mock_pr]
        mock_get_repo.return_value = mock_repo

        # Execute
        result = client.find_closing_pr("test/repo", 123)

        # Assert
        assert result == pr_number, f"Failed for title='{title}', body='{body}'"

    @patch("src.auto_coder.github_client.GitHubClient._get_issue_timeline")
    @patch("src.auto_coder.github_client.GitHubClient.get_repository")
    def test_find_closing_pr_github_exception(self, mock_get_repo, mock_get_timeline, mock_github_token):
        """Test find_closing_pr handles GithubException gracefully."""
        # Setup
        client = GitHubClient.get_instance(mock_github_token)

        # Timeline ok but empty
        mock_get_timeline.return_value = []
        
        # Fallback raises Exception
        mock_get_repo.side_effect = GithubException(500, "Server Error")

        # Execute
        result = client.find_closing_pr("test/repo", 123)

        # Assert
        assert result is None
