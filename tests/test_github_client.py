"""
Tests for GitHub client functionality.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from github import Github, Repository, Issue, PullRequest
from github.GithubException import GithubException

from src.auto_coder.github_client import GitHubClient


class TestGitHubClient:
    """Test cases for GitHubClient class."""
    
    def test_init(self, mock_github_token):
        """Test GitHubClient initialization."""
        client = GitHubClient(mock_github_token)
        assert client.token == mock_github_token
        assert isinstance(client.github, Github)
    
    @patch('src.auto_coder.github_client.Github')
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
    
    @patch('src.auto_coder.github_client.Github')
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
    
    @patch('src.auto_coder.github_client.Github')
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
        mock_repo.get_issues.assert_called_once_with(state='open', sort='created', direction='desc')
    
    @patch('src.auto_coder.github_client.Github')
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
        mock_repo.get_pulls.assert_called_once_with(state='open', sort='created', direction='desc')
    
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
            'number': 123,
            'title': "Test Issue",
            'body': "Test body",
            'state': "open",
            'labels': ["bug", "high-priority"],
            'assignees': ["testuser"],
            'created_at': "2024-01-01T00:00:00Z",
            'updated_at': "2024-01-01T00:00:00Z",
            'url': "https://github.com/test/repo/issues/123",
            'author': "author",
            'comments_count': 5
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
            'number': 456,
            'title': "Test PR",
            'body': "Test PR body",
            'state': "open",
            'labels': ["feature"],
            'assignees': ["testuser"],
            'created_at': "2024-01-01T00:00:00Z",
            'updated_at': "2024-01-01T00:00:00Z",
            'url': "https://github.com/test/repo/pull/456",
            'author': "author",
            'head_branch': "feature-branch",
            'base_branch': "main",
            'mergeable': True,
            'draft': False,
            'comments_count': 2,
            'review_comments_count': 1,
            'commits_count': 3,
            'additions': 50,
            'deletions': 10,
            'changed_files': 2
        }
        assert result == expected
    
    @patch('src.auto_coder.github_client.Github')
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
        result = client.create_issue("test/repo", "New Issue", "Issue body", ["bug"])
        
        # Assert
        assert result == mock_issue
        mock_repo.create_issue.assert_called_once_with(
            title="New Issue",
            body="Issue body",
            labels=["bug"]
        )
    
    @patch('src.auto_coder.github_client.Github')
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
    
    @patch('src.auto_coder.github_client.Github')
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
        mock_issue.edit.assert_called_once_with(state='closed')
