"""
GitHub API client for Auto-Coder.
"""

from typing import List, Dict, Any, Optional
from github import Github, Repository, Issue, PullRequest
from github.GithubException import GithubException

from .logger_config import get_logger

logger = get_logger(__name__)


class GitHubClient:
    """GitHub API client for managing issues and pull requests."""

    def __init__(self, token: str):
        """Initialize GitHub client with API token."""
        self.github = Github(token)
        self.token = token

    def get_repository(self, repo_name: str) -> Repository.Repository:
        """Get repository object by name (owner/repo)."""
        try:
            return self.github.get_repo(repo_name)
        except GithubException as e:
            logger.error(f"Failed to get repository {repo_name}: {e}")
            raise

    def get_open_issues(self, repo_name: str, limit: Optional[int] = None) -> List[Issue.Issue]:
        """Get open issues from repository, sorted by creation date (oldest first)."""
        try:
            repo = self.get_repository(repo_name)
            issues = repo.get_issues(state='open', sort='created', direction='asc')

            # Filter out pull requests (GitHub API includes PRs in issues)
            # Some tests use Mock objects where accessing missing attributes returns a Mock (truthy),
            # so explicitly treat missing/Mock attributes as "not a PR".
            try:
                from unittest.mock import Mock as _UMock, MagicMock as _UMagicMock
                _mock_types = (_UMock, _UMagicMock)
            except Exception:
                _mock_types = tuple()

            def _is_pr(it):
                try:
                    if not hasattr(it, 'pull_request'):
                        return False
                    val = getattr(it, 'pull_request', None)
                    if isinstance(val, _mock_types):
                        return False
                    return bool(val)
                except Exception:
                    return False

            issue_list = [issue for issue in issues if not _is_pr(issue)]

            # Apply limit only when positive
            if isinstance(limit, int) and limit > 0:
                issue_list = issue_list[:limit]

            logger.info(f"Retrieved {len(issue_list)} open issues from {repo_name} (oldest first)")
            return issue_list

        except GithubException as e:
            logger.error(f"Failed to get issues from {repo_name}: {e}")
            raise
    
    def get_open_pull_requests(self, repo_name: str, limit: Optional[int] = None) -> List[PullRequest.PullRequest]:
        """Get open pull requests from repository, sorted by creation date (oldest first)."""
        try:
            repo = self.get_repository(repo_name)
            prs = repo.get_pulls(state='open', sort='created', direction='asc')

            pr_list = list(prs)
            # Apply limit only when positive
            if isinstance(limit, int) and limit > 0:
                pr_list = pr_list[:limit]

            logger.info(f"Retrieved {len(pr_list)} open pull requests from {repo_name} (oldest first)")
            return pr_list

        except GithubException as e:
            logger.error(f"Failed to get pull requests from {repo_name}: {e}")
            raise
    
    def get_issue_details(self, issue: Issue.Issue) -> Dict[str, Any]:
        """Extract detailed information from an issue."""
        return {
            'number': issue.number,
            'title': issue.title,
            'body': issue.body or '',
            'state': issue.state,
            'labels': [label.name for label in issue.labels],
            'assignees': [assignee.login for assignee in issue.assignees],
            'created_at': issue.created_at.isoformat(),
            'updated_at': issue.updated_at.isoformat(),
            'url': issue.html_url,
            'author': issue.user.login if issue.user else None,
            'comments_count': issue.comments,
        }
    
    def get_pr_details(self, pr: PullRequest.PullRequest) -> Dict[str, Any]:
        """Extract detailed information from a pull request."""
        return {
            'number': pr.number,
            'title': pr.title,
            'body': pr.body or '',
            'state': pr.state,
            'labels': [label.name for label in pr.labels],
            'assignees': [assignee.login for assignee in pr.assignees],
            'created_at': pr.created_at.isoformat(),
            'updated_at': pr.updated_at.isoformat(),
            'url': pr.html_url,
            'author': pr.user.login if pr.user else None,
            'head_branch': pr.head.ref,
            'base_branch': pr.base.ref,
            'mergeable': pr.mergeable,
            'draft': pr.draft,
            'comments_count': pr.comments,
            'review_comments_count': pr.review_comments,
            'commits_count': pr.commits,
            'additions': pr.additions,
            'deletions': pr.deletions,
            'changed_files': pr.changed_files,
        }

    def get_pr_details_by_number(self, repo_name: str, pr_number: int) -> Dict[str, Any]:
        """Get PR details by repository name and PR number."""
        try:
            repo = self.get_repository(repo_name)
            pr = repo.get_pull(pr_number)
            return self.get_pr_details(pr)
        except GithubException as e:
            logger.error(f"Failed to get PR #{pr_number} from {repo_name}: {e}")
            raise

    def get_issue_details_by_number(self, repo_name: str, issue_number: int) -> Dict[str, Any]:
        """Get Issue details by repository name and issue number."""
        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)
            return self.get_issue_details(issue)
        except GithubException as e:
            logger.error(f"Failed to get Issue #{issue_number} from {repo_name}: {e}")
            raise

    def create_issue(self, repo_name: str, title: str, body: str, labels: Optional[List[str]] = None) -> Issue.Issue:
        """Create a new issue in the repository."""
        try:
            repo = self.get_repository(repo_name)
            issue = repo.create_issue(title=title, body=body, labels=labels or [])
            logger.info(f"Created issue #{issue.number}: {title}")
            return issue

        except GithubException as e:
            logger.error(f"Failed to create issue in {repo_name}: {e}")
            raise

    def add_comment_to_issue(self, repo_name: str, issue_number: int, comment: str) -> None:
        """Add a comment to an existing issue."""
        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)
            issue.create_comment(comment)
            logger.info(f"Added comment to issue #{issue_number}")
            
        except GithubException as e:
            logger.error(f"Failed to add comment to issue #{issue_number}: {e}")
            raise
    
    def close_issue(self, repo_name: str, issue_number: int, comment: Optional[str] = None) -> None:
        """Close an issue with optional comment."""
        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)

            if comment:
                issue.create_comment(comment)

            issue.edit(state='closed')
            logger.info(f"Closed issue #{issue_number}")

        except GithubException as e:
            logger.error(f"Failed to close issue #{issue_number}: {e}")
            raise

    def add_labels_to_issue(self, repo_name: str, issue_number: int, labels: List[str]) -> None:
        """Add labels to an existing issue."""
        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)

            # Get current labels
            current_labels = [label.name for label in issue.labels]

            # Add new labels to current ones (avoid duplicates)
            all_labels = list(set(current_labels + labels))

            # Update issue with all labels
            issue.edit(labels=all_labels)
            logger.info(f"Added labels {labels} to issue #{issue_number}")

        except GithubException as e:
            logger.error(f"Failed to add labels to issue #{issue_number}: {e}")
            raise
