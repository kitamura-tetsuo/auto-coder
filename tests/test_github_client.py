"""
Tests for GitHub client functionality.
"""

import json
from unittest.mock import Mock, patch

import pytest
from github import Github, Issue, PullRequest, Repository
from github.GithubException import GithubException

from src.auto_coder.github_client import GitHubClient


class TestGitHubClient:
    """Test cases for GitHubClient class."""

    def test_init(self, mock_github_token):
        """Test GitHubClient initialization."""
        client = GitHubClient(mock_github_token)
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

        client = GitHubClient(mock_github_token)

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

        client = GitHubClient(mock_github_token)

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

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_open_issues("test/repo", limit=2)

        # Assert
        assert len(result) == 2
        assert mock_issue1 in result
        assert mock_issue2 in result
        assert mock_pr not in result
        mock_repo.get_issues.assert_called_once_with(
            state="open", sort="created", direction="asc"
        )

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

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_open_pull_requests("test/repo", limit=1)

        # Assert
        assert len(result) == 1
        assert mock_pr1 in result
        mock_repo.get_pulls.assert_called_once_with(
            state="open", sort="created", direction="asc"
        )

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

            client = GitHubClient(mock_github_token)

            # Execute
            result = client.get_open_issues("test/repo")

            # Assert
            assert len(result) == 2
            assert result[0] == mock_issue1  # Oldest first
            assert result[1] == mock_issue2
            mock_repo.get_issues.assert_called_once_with(
                state="open", sort="created", direction="asc"
            )

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

            client = GitHubClient(mock_github_token)

            # Execute
            result = client.get_open_pull_requests("test/repo")

            # Assert
            assert len(result) == 2
            assert result[0] == mock_pr1  # Oldest first
            assert result[1] == mock_pr2
            mock_repo.get_pulls.assert_called_once_with(
                state="open", sort="created", direction="asc"
            )

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

        client = GitHubClient(mock_github_token)

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

        client = GitHubClient(mock_github_token)

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
    def test_get_pr_details_by_number_success(
        self, mock_github_class, mock_github_token
    ):
        """Test successful PR details retrieval by number."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_pr = Mock(spec=PullRequest.PullRequest)

        # Configure mock PR
        mock_pr.number = 123
        mock_pr.title = "Test PR"
        mock_pr.body = "Test PR body"
        mock_pr.state = "open"
        mock_pr.labels = []
        mock_pr.assignees = []
        mock_pr.created_at = Mock()
        mock_pr.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_pr.updated_at = Mock()
        mock_pr.updated_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_pr.html_url = "https://github.com/test/repo/pull/123"
        mock_pr.user = Mock(login="author")
        mock_pr.head = Mock(ref="feature-branch")
        mock_pr.base = Mock(ref="main")
        mock_pr.mergeable = True
        mock_pr.draft = False
        mock_pr.comments = 0
        mock_pr.review_comments = 0
        mock_pr.commits = 1
        mock_pr.additions = 10
        mock_pr.deletions = 5
        mock_pr.changed_files = 1

        mock_repo.get_pull.return_value = mock_pr
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_pr_details_by_number("test/repo", 123)

        # Assert
        assert result["number"] == 123
        assert result["title"] == "Test PR"
        assert result["body"] == "Test PR body"

    @patch("src.auto_coder.github_client.Github")
    def test_get_issue_details_by_number_success(
        self, mock_github_class, mock_github_token
    ):
        """Test successful Issue details retrieval by number."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)

        mock_issue.number = 456
        mock_issue.title = "Test Issue"
        mock_issue.body = "Test Issue body"
        mock_issue.state = "open"
        mock_label = Mock()
        mock_label.name = "bug"
        mock_issue.labels = [mock_label]
        mock_issue.assignees = []
        mock_issue.created_at = Mock()
        mock_issue.created_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.updated_at = Mock()
        mock_issue.updated_at.isoformat.return_value = "2024-01-01T00:00:00Z"
        mock_issue.html_url = "https://github.com/test/repo/issues/456"
        mock_issue.user = Mock(login="user")
        mock_issue.comments = 0

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_issue_details_by_number("test/repo", 456)

        # Assert
        assert result["number"] == 456
        assert result["title"] == "Test Issue"
        assert result["body"] == "Test Issue body"
        assert result["state"] == "open"
        assert result["labels"] == ["bug"]
        assert result["url"] == "https://github.com/test/repo/issues/456"

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

        client = GitHubClient(mock_github_token)

        # Execute
        params = ("test/repo", "New Issue", "Issue body", ["bug"])
        result = client.create_issue(*params)

        # Assert
        assert result == mock_issue
        mock_repo.create_issue.assert_called_once_with(
            title="New Issue", body="Issue body", labels=["bug"]
        )

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

        client = GitHubClient(mock_github_token)

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

        client = GitHubClient(mock_github_token)

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

        client = GitHubClient(mock_github_token)

        # Execute
        client.add_labels_to_issue("test/repo", 123, ["jules", "enhancement"])

        # Assert
        mock_repo.get_issue.assert_called_once_with(123)
        # Should call edit with all labels (existing + new, no duplicates)
        # Check that edit was called once and contains all expected labels
        mock_issue.edit.assert_called_once()
        call_args = mock_issue.edit.call_args
        actual_labels = call_args[1]["labels"]  # Get labels from kwargs
        expected_labels = {"bug", "high-priority", "jules", "enhancement"}
        assert set(actual_labels) == expected_labels

    @patch("src.auto_coder.github_client.Github")
    def test_add_labels_to_issue_no_duplicates(
        self, mock_github_class, mock_github_token
    ):
        """Test that duplicate labels are not added."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_issue = Mock(spec=Issue.Issue)

        # Mock existing labels including one we'll try to add
        mock_label1 = Mock()
        mock_label1.name = "bug"
        mock_label2 = Mock()
        mock_label2.name = "jules"  # Already exists
        mock_issue.labels = [mock_label1, mock_label2]

        mock_repo.get_issue.return_value = mock_issue
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute - try to add "jules" again and "enhancement"
        client.add_labels_to_issue("test/repo", 123, ["jules", "enhancement"])

        # Assert
        mock_repo.get_issue.assert_called_once_with(123)
        # Should call edit with all labels (no duplicates)
        mock_issue.edit.assert_called_once()
        call_args = mock_issue.edit.call_args
        actual_labels = call_args[1]["labels"]  # Get labels from kwargs
        expected_labels = {"bug", "jules", "enhancement"}
        assert set(actual_labels) == expected_labels

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_with_linked_pr(self, mock_github_class, mock_github_token):
        """Test has_linked_pr returns True when PR references the issue."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        # Create a PR that references issue #123
        mock_pr = Mock(spec=PullRequest.PullRequest)
        mock_pr.number = 456
        mock_pr.title = "Fix bug"
        mock_pr.body = "This PR fixes #123"

        mock_repo.get_pulls.return_value = [mock_pr]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.has_linked_pr("test/repo", 123)

        # Assert
        assert result is True
        mock_repo.get_pulls.assert_called_once_with(state="open")

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_with_no_linked_pr(
        self, mock_github_class, mock_github_token
    ):
        """Test has_linked_pr returns False when no PR references the issue."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)

        # Create a PR that does not reference issue #123
        mock_pr = Mock(spec=PullRequest.PullRequest)
        mock_pr.number = 456
        mock_pr.title = "Fix another bug"
        mock_pr.body = "This PR fixes #999"

        mock_repo.get_pulls.return_value = [mock_pr]
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.has_linked_pr("test/repo", 123)

        # Assert
        assert result is False
        mock_repo.get_pulls.assert_called_once_with(state="open")

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_with_multiple_patterns(
        self, mock_github_class, mock_github_token
    ):
        """Test has_linked_pr detects various reference patterns."""
        # Setup
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

            client = GitHubClient(mock_github_token)

            # Execute
            result = client.has_linked_pr("test/repo", 123)

            # Assert
            assert result is True, f"Failed for title='{title}', body='{body}'"

    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_handles_exception(
        self, mock_github_class, mock_github_token
    ):
        """Test has_linked_pr handles exceptions gracefully."""
        # Setup
        mock_github = Mock()
        mock_repo = Mock(spec=Repository.Repository)
        mock_repo.get_pulls.side_effect = GithubException(500, "Server Error")
        mock_github.get_repo.return_value = mock_repo
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.has_linked_pr("test/repo", 123)

        # Assert
        assert result is False

    @patch("subprocess.run")
    def test_get_linked_prs_via_graphql_success(self, mock_run, mock_github_token):
        """Test get_linked_prs_via_graphql returns linked PR numbers."""
        # Setup
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "timelineItems": {
                            "nodes": [
                                {"source": {"number": 456, "state": "OPEN"}},
                                {"source": {"number": 789, "state": "CLOSED"}},
                            ]
                        }
                    }
                }
            }
        }

        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(graphql_response)
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_linked_prs_via_graphql("test/repo", 123)

        # Assert
        assert result == [456]  # Only OPEN PRs
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "gh"
        assert call_args[1] == "api"
        assert call_args[2] == "graphql"

    @patch("subprocess.run")
    def test_get_linked_prs_via_graphql_no_linked_prs(
        self, mock_run, mock_github_token
    ):
        """Test get_linked_prs_via_graphql returns
        empty list when no PRs linked.
        """
        # Setup
        graphql_response = {
            "data": {"repository": {"issue": {"timelineItems": {"nodes": []}}}}
        }

        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(graphql_response)
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_linked_prs_via_graphql("test/repo", 123)

        # Assert
        assert result == []

    @patch("subprocess.run")
    def test_get_linked_prs_via_graphql_handles_error(
        self, mock_run, mock_github_token
    ):
        """Test get_linked_prs_via_graphql handles subprocess errors."""
        # Setup
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "GraphQL error"
        mock_run.return_value = mock_result

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.get_linked_prs_via_graphql("test/repo", 123)

        # Assert
        assert result == []

    @patch("subprocess.run")
    @patch("src.auto_coder.github_client.Github")
    def test_has_linked_pr_uses_graphql_first(
        self, mock_github_class, mock_run, mock_github_token
    ):
        """Test has_linked_pr uses GraphQL API first."""
        # Setup GraphQL to return a linked PR
        graphql_response = {
            "data": {
                "repository": {
                    "issue": {
                        "timelineItems": {
                            "nodes": [{"source": {"number": 456, "state": "OPEN"}}]
                        }
                    }
                }
            }
        }

        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(graphql_response)
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        mock_github = Mock()
        mock_github_class.return_value = mock_github

        client = GitHubClient(mock_github_token)

        # Execute
        result = client.has_linked_pr("test/repo", 123)

        # Assert
        assert result is True
        # Should not call get_pulls since GraphQL found a PR
        mock_github.get_repo.assert_not_called()
