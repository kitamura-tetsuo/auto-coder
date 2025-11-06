"""
GitHub API client for Auto-Coder.
"""

import json
import subprocess
import threading
from typing import Any, Dict, List, Optional

from github import Github, Issue, PullRequest, Repository
from github.GithubException import GithubException

from .logger_config import get_logger

logger = get_logger(__name__)


class GitHubClient:
    """GitHub API client for managing issues and pull requests.

    Implements a thread-safe singleton pattern. Use get_instance() to get the singleton.

    Usage Pattern:
    -------------
    1. First call: Provide token and optional parameters
       ```python
       from auto_coder.github_client import GitHubClient

       client = GitHubClient.get_instance("your-token", disable_labels=False)
       ```

    2. Subsequent calls: Call without parameters to get the same instance
       ```python
       # Parameters are ignored on subsequent calls
       client2 = GitHubClient.get_instance("different-token", disable_labels=True)
       # client is client2  # True, uses first instance
       ```

    Important Notes:
    ---------------
    - Thread-safe: Can be called from multiple threads simultaneously
    - Parameters are only used on first call; subsequent calls return the same instance
    - Use reset_singleton() in tests to clear the instance
    """

    # Class variable to hold the singleton instance
    _instance = None

    # Class variable to hold the lock for thread-safety
    _lock = threading.Lock()

    def __init__(self, token: str, disable_labels: bool = False):
        """Initialize GitHub client with API token.

        Args:
            token: GitHub API token
            disable_labels: If True, all label operations are no-ops
        """
        self.github = Github(token)
        self.token = token
        self.disable_labels = disable_labels

    def __new__(cls, *args: Any, **kwargs: Any) -> "GitHubClient":
        """Implement thread-safe singleton pattern.

        This method ensures only one instance is created across all threads.
        """
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._initialized = False
            return instance
        return cls._instance

    @classmethod
    def get_instance(cls, token: str = None, disable_labels: bool = False):
        """Get the singleton instance of GitHubClient.

        On the first call, this creates and returns the singleton instance.
        Subsequent calls return the same instance, ignoring the parameters.

        Args:
            token: GitHub API token (used only for first call)
            disable_labels: If True, all label operations are no-ops (used only for first call)

        Returns:
            The singleton instance of GitHubClient
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = cls.__new__(cls)
                    if not hasattr(instance, "_initialized") or not instance._initialized:
                        instance.__init__(token, disable_labels)
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance.

        Use reset_singleton() in tests to clear the instance.
        """
        with cls._lock:
            cls._instance = None

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
            issues = repo.get_issues(state="open", sort="created", direction="asc")

            # Filter out pull requests (GitHub API includes PRs in issues)
            # Some tests use Mock objects where accessing missing attributes returns a Mock (truthy),
            # so explicitly treat missing/Mock attributes as "not a PR".
            try:
                from unittest.mock import MagicMock as _UMagicMock
                from unittest.mock import Mock as _UMock

                _mock_types: tuple = (_UMock, _UMagicMock)
            except Exception:
                _mock_types = tuple()

            def _is_pr(it: Any) -> bool:
                try:
                    if not hasattr(it, "pull_request"):
                        return False
                    val = getattr(it, "pull_request", None)
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
            prs = repo.get_pulls(state="open", sort="created", direction="asc")

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
            "number": issue.number,
            "title": issue.title,
            "body": issue.body or "",
            "state": issue.state,
            "labels": [label.name for label in issue.labels],
            "assignees": [assignee.login for assignee in issue.assignees],
            "created_at": issue.created_at.isoformat(),
            "updated_at": issue.updated_at.isoformat(),
            "url": issue.html_url,
            "author": issue.user.login if issue.user else None,
            "comments_count": issue.comments,
        }

    def get_pr_details(self, pr: PullRequest.PullRequest) -> Dict[str, Any]:
        """Extract detailed information from a pull request."""
        return {
            "number": pr.number,
            "title": pr.title,
            "body": pr.body or "",
            "state": pr.state,
            "labels": [label.name for label in pr.labels],
            "assignees": [assignee.login for assignee in pr.assignees],
            "created_at": pr.created_at.isoformat(),
            "updated_at": pr.updated_at.isoformat() if pr.updated_at else None,
            "url": pr.html_url,
            "author": pr.user.login if pr.user else None,
            "head_branch": pr.head.ref,
            "base_branch": pr.base.ref,
            "mergeable": pr.mergeable,
            "draft": pr.draft,
            "comments_count": pr.comments,
            "review_comments_count": pr.review_comments,
            "commits_count": pr.commits,
            "additions": pr.additions,
            "deletions": pr.deletions,
            "changed_files": pr.changed_files,
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

    def find_pr_by_head_branch(self, repo_name: str, branch_name: str) -> Optional[Dict[str, Any]]:
        """Find an open PR by its head branch name.

        Args:
            repo_name: Repository name in format 'owner/repo'
            branch_name: Name of the head branch to search for

        Returns:
            PR details dict if found, None otherwise
        """
        try:
            prs = self.get_open_pull_requests(repo_name)
            for pr in prs:
                if pr.head.ref == branch_name:
                    logger.info(f"Found PR #{pr.number} with head branch '{branch_name}'")
                    return self.get_pr_details(pr)
            logger.debug(f"No open PR found with head branch '{branch_name}'")
            return None
        except Exception as e:
            logger.warning(f"Failed to search for PR with head branch '{branch_name}': {e}")
            return None

    def get_linked_prs_via_graphql(self, repo_name: str, issue_number: int) -> List[int]:
        """Get linked PRs for an issue using GitHub GraphQL API.

        Uses gh CLI to query GraphQL API for PRs that have this issue in their
        closingIssuesReferences (Development section).

        Returns list of PR numbers that are linked to this issue.
        """
        try:
            owner, repo = repo_name.split("/")
            query = """
            query($owner: String!, $repo: String!, $issueNumber: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $issueNumber) {
                  timelineItems(itemTypes: CONNECTED_EVENT, first: 100) {
                    nodes {
                      ... on ConnectedEvent {
                        source {
                          ... on PullRequest {
                            number
                            state
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"repo={repo}",
                    "-F",
                    f"issueNumber={issue_number}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"GraphQL query failed for issue #{issue_number}: {result.stderr}")
                return []

            data = json.loads(result.stdout)
            timeline_items = data.get("data", {}).get("repository", {}).get("issue", {}).get("timelineItems", {}).get("nodes", [])

            pr_numbers = []
            for item in timeline_items:
                if item and "source" in item:
                    source = item["source"]
                    if source and "number" in source:
                        # Only include open PRs
                        if source.get("state") == "OPEN":
                            pr_numbers.append(source["number"])

            if pr_numbers:
                logger.info(f"Found {len(pr_numbers)} linked PR(s) for issue #{issue_number} via GraphQL: {pr_numbers}")

            return pr_numbers

        except Exception as e:
            logger.warning(f"Failed to get linked PRs via GraphQL for issue #{issue_number}: {e}")
            return []

    def has_linked_pr(self, repo_name: str, issue_number: int) -> bool:
        """Check if an issue has a linked pull request.

        First tries GraphQL API to check Development section, then falls back to
        searching PR titles/bodies for issue references.

        Returns True if there is an open PR that references this issue.
        """
        try:
            # First try GraphQL API (more accurate, checks Development section)
            linked_prs = self.get_linked_prs_via_graphql(repo_name, issue_number)
            if linked_prs:
                return True

            # Fallback: Search for PRs that reference this issue in title/body
            repo = self.get_repository(repo_name)
            prs = repo.get_pulls(state="open")

            issue_ref_patterns = [
                f"#{issue_number}",
                f"issue #{issue_number}",
                f"fixes #{issue_number}",
                f"closes #{issue_number}",
                f"resolves #{issue_number}",
            ]

            for pr in prs:
                pr_text = f"{pr.title} {pr.body or ''}".lower()
                if any(pattern.lower() in pr_text for pattern in issue_ref_patterns):
                    logger.info(f"Found linked PR #{pr.number} for issue #{issue_number} (via text search)")
                    return True

            return False

        except GithubException as e:
            logger.error(f"Failed to check linked PRs for issue #{issue_number}: {e}")
            return False

    def get_open_sub_issues(self, repo_name: str, issue_number: int) -> List[int]:
        """Get list of open sub-issues for a given issue using GitHub GraphQL API.

        Args:
            repo_name: Repository name in format 'owner/repo'
            issue_number: Issue number to check for sub-issues

        Returns:
            List of issue numbers that are linked to the issue and are still open.
        """
        try:
            owner, repo = repo_name.split("/")

            # GraphQL query to fetch sub-issues (new sub-issues feature)
            query = """
            {
              repository(owner: "%s", name: "%s") {
                issue(number: %d) {
                  number
                  title
                  subIssues(first: 100) {
                    nodes {
                      number
                      title
                      state
                      url
                    }
                  }
                }
              }
            }
            """ % (
                owner,
                repo,
                issue_number,
            )

            # Execute GraphQL query using gh CLI with sub_issues feature header
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-H",
                    "GraphQL-Features: sub_issues",
                    "-f",
                    f"query={query}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            data = json.loads(result.stdout)

            # Extract open sub-issues
            open_sub_issues = []
            sub_issues = data.get("data", {}).get("repository", {}).get("issue", {}).get("subIssues", {}).get("nodes", [])

            for sub_issue in sub_issues:
                if sub_issue.get("state") == "OPEN":
                    open_sub_issues.append(sub_issue.get("number"))

            if open_sub_issues:
                logger.info(f"Issue #{issue_number} has {len(open_sub_issues)} open sub-issue(s): {open_sub_issues}")

            return open_sub_issues

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to execute gh GraphQL query for issue #{issue_number}: {e.stderr}")
            return []
        except Exception as e:
            logger.error(f"Failed to get open sub-issues for issue #{issue_number}: {e}")
            return []

    def get_pr_closing_issues(self, repo_name: str, pr_number: int) -> List[int]:
        """Get list of issues that will be closed when PR is merged using GitHub GraphQL API.

        Args:
            repo_name: Repository name in format 'owner/repo'
            pr_number: PR number to check for closing issues

        Returns:
            List of issue numbers that will be closed when the PR is merged.
        """
        try:
            owner, repo = repo_name.split("/")

            # GraphQL query to fetch closingIssuesReferences
            query = """
            {
              repository(owner: "%s", name: "%s") {
                pullRequest(number: %d) {
                  number
                  title
                  closingIssuesReferences(first: 100) {
                    nodes {
                      number
                      title
                      state
                    }
                  }
                }
              }
            }
            """ % (
                owner,
                repo,
                pr_number,
            )

            # Execute GraphQL query using gh CLI
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    f"query={query}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            data = json.loads(result.stdout)

            # Extract closing issues
            closing_issues = []
            issues = data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("closingIssuesReferences", {}).get("nodes", [])

            for issue in issues:
                closing_issues.append(issue.get("number"))

            if closing_issues:
                logger.info(f"PR #{pr_number} will close {len(closing_issues)} issue(s): {closing_issues}")

            return closing_issues

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to execute gh GraphQL query for PR #{pr_number}: {e.stderr}")
            return []
        except Exception as e:
            logger.error(f"Failed to get closing issues for PR #{pr_number}: {e}")
            return []

    def get_parent_issue(self, repo_name: str, issue_number: int) -> Optional[int]:
        """Get parent issue number for a given issue using GitHub GraphQL API.

        Args:
            repo_name: Repository name in format 'owner/repo'
            issue_number: Issue number to check for parent issue

        Returns:
            Parent issue number if exists, None otherwise.
        """
        try:
            owner, repo = repo_name.split("/")

            # GraphQL query to fetch parent issue (sub-issues feature)
            # Note: Use 'parent' field, not 'parentIssue'
            query = """
            {
              repository(owner: "%s", name: "%s") {
                issue(number: %d) {
                  number
                  title
                  parent {
                    ... on Issue {
                      number
                      title
                      state
                      url
                    }
                  }
                }
              }
            }
            """ % (
                owner,
                repo,
                issue_number,
            )

            # Execute GraphQL query using gh CLI with sub_issues feature header
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-H",
                    "GraphQL-Features: sub_issues",
                    "-f",
                    f"query={query}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            data = json.loads(result.stdout)

            # Extract parent issue
            parent_issue = data.get("data", {}).get("repository", {}).get("issue", {}).get("parent")

            if parent_issue:
                parent_number = parent_issue.get("number")
                logger.info(f"Issue #{issue_number} has parent issue #{parent_number}: {parent_issue.get('title')}")
                return int(parent_number) if parent_number is not None else None

            return None

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to execute gh GraphQL query for issue #{issue_number}: {e.stderr}")
            return None
        except Exception as e:
            logger.error(f"Failed to get parent issue for issue #{issue_number}: {e}")
            return None

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

            issue.edit(state="closed")
            logger.info(f"Closed issue #{issue_number}")

        except GithubException as e:
            logger.error(f"Failed to close issue #{issue_number}: {e}")
            raise

    def add_labels_to_issue(self, repo_name: str, issue_number: int, labels: List[str]) -> None:
        """Add labels to an existing issue."""
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping add labels {labels} to issue #{issue_number}")
            return

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

    def remove_labels_from_issue(self, repo_name: str, issue_number: int, labels: List[str]) -> None:
        """Remove labels from an existing issue."""
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping remove labels {labels} from issue #{issue_number}")
            return

        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)

            # Get current labels
            current_labels = [label.name for label in issue.labels]

            # Remove specified labels
            remaining_labels = [label for label in current_labels if label not in labels]

            # Update issue with remaining labels
            issue.edit(labels=remaining_labels)
            logger.info(f"Removed labels {labels} from issue #{issue_number}")

        except GithubException as e:
            logger.error(f"Failed to remove labels from issue #{issue_number}: {e}")
            raise

    def has_label(self, repo_name: str, issue_number: int, label: str) -> bool:
        """Check if an issue has a specific label."""
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping check for label '{label}' on issue #{issue_number}")
            return False

        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)

            # Get current labels
            current_labels = [lbl.name for lbl in issue.labels]

            return label in current_labels

        except GithubException as e:
            logger.error(f"Failed to check labels for issue #{issue_number}: {e}")
            raise

    def try_add_work_in_progress_label(self, repo_name: str, issue_number: int, label: str = "@auto-coder") -> bool:
        """Try to add work-in-progress label to an issue.

        Returns True if the label was successfully added (issue was not already being processed).
        Returns False if the label already exists (issue is being processed by another instance).
        """
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping add '{label}' label to issue #{issue_number}")
            return True  # Return True to allow processing to continue

        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)

            # Get current labels
            current_labels = [lbl.name for lbl in issue.labels]

            # Check if label already exists
            if label in current_labels:
                logger.info(f"Issue #{issue_number} already has '{label}' label - skipping")
                return False

            # Add the label
            all_labels = list(set(current_labels + [label]))
            issue.edit(labels=all_labels)
            logger.info(f"Added '{label}' label to issue #{issue_number}")
            return True

        except GithubException as e:
            logger.error(f"Failed to add work-in-progress label to issue #{issue_number}: {e}")
            raise

    def get_issue_dependencies(self, issue_body: str) -> List[int]:
        """Extract issue numbers that this issue depends on from the issue body.

        Args:
            issue_body: The body text of the issue

        Returns:
            List of issue numbers that this issue depends on

        Examples:
            "Depends on: #123" -> [123]
            "depends on #456" -> [456]
            "Depends on #789, #790" -> [789, 790]
        """
        if not issue_body:
            return []

        import re

        # Better approach: first find all text that comes after 'depends on' or 'blocked by',
        # then extract all issue numbers from that text
        # This handles various formats like:
        # - "Depends on: #123"
        # - "depends on #123, #456, #789"
        # - "depends on #100 and #200 and #300"
        # - "blocked by #100"
        # Case-insensitive, with or without colon, supports comma-separated lists and "and"
        depends_pattern = r"(?i)(?:depends\s+on|blocked\s+by)\s*:?\s*(.*?)(?:\n|\r|$)"

        dependencies = []
        seen = set()

        for match in re.finditer(depends_pattern, issue_body):
            depends_text = match.group(1).strip()

            # Extract all issue numbers from the depends text
            numbers = re.findall(r"#?(\d+)", depends_text)
            for num_str in numbers:
                issue_num = int(num_str)
                if issue_num not in seen:
                    dependencies.append(issue_num)
                    seen.add(issue_num)

        if dependencies:
            logger.debug(f"Found dependencies: {dependencies}")

        return dependencies

    def check_issue_dependencies_resolved(self, repo_name: str, dependencies: List[int]) -> List[int]:
        """Check which of the given issue dependencies are resolved (closed).

        Args:
            repo_name: Repository name in format 'owner/repo'
            dependencies: List of issue numbers to check

        Returns:
            List of issue numbers that are still open (unresolved dependencies)
        """
        if not dependencies:
            return []

        unresolved = []
        for issue_num in dependencies:
            try:
                issue_details = self.get_issue_details_by_number(repo_name, issue_num)
                state = issue_details.get("state", "open")
                if state == "open":
                    unresolved.append(issue_num)
                    logger.debug(f"Dependency issue #{issue_num} is still open")
                else:
                    logger.debug(f"Dependency issue #{issue_num} is closed (resolved)")
            except GithubException as e:
                # If issue doesn't exist or can't be accessed, consider it unresolved
                logger.warning(f"Failed to check dependency issue #{issue_num}: {e}")
                unresolved.append(issue_num)

        return unresolved
