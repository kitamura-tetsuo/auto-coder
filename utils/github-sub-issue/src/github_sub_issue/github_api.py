"""Sub-issue operation module using GitHub API."""

import json
import re
import subprocess
from typing import Any, Dict, List, Optional

from loguru import logger


class GitHubSubIssueAPI:
    """GitHub sub-issue API client."""

    def __init__(self, repo: Optional[str] = None):
        """Initialize.

        Args:
            repo: Repository name (owner/repo format). If None, get from current directory
        """
        self.repo = repo or self._get_current_repo()

    def _get_current_repo(self) -> str:
        """Get GitHub repository name from current directory.

        Returns:
            Repository name (owner/repo format)

        Raises:
            RuntimeError: When failing to get repository name
        """
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                capture_output=True,
                text=True,
                check=True,
            )
            repo = result.stdout.strip()
            logger.debug(f"Current repository: {repo}")
            return repo
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get current repository: {e.stderr}")

    def _parse_issue_reference(self, reference: str) -> tuple[str, int]:
        """Parse issue reference.

        Args:
            reference: Issue number or URL

        Returns:
            Tuple of (repository name, issue number)

        Raises:
            ValueError: When parsing fails
        """
        # If URL
        url_pattern = r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)"
        match = re.match(url_pattern, reference)
        if match:
            owner, repo, number = match.groups()
            return f"{owner}/{repo}", int(number)

        # If issue number
        try:
            number = int(reference)
            return self.repo, number
        except ValueError:
            raise ValueError(f"Invalid issue reference: {reference}")

    def _get_issue_id(self, repo: str, issue_number: int) -> str:
        """Get issue ID.

        Args:
            repo: Repository name (owner/repo format)
            issue_number: Issue number

        Returns:
            Issue ID

        Raises:
            RuntimeError: When failing to get ID
        """
        try:
            result = subprocess.run(
                ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "id", "--jq", ".id"],
                capture_output=True,
                text=True,
                check=True,
            )
            issue_id = result.stdout.strip()
            logger.debug(f"Issue #{issue_number} ID: {issue_id}")
            return issue_id
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get issue ID for #{issue_number}: {e.stderr}")

    def add_sub_issue(self, parent_ref: str, sub_issue_ref: str, replace_parent: bool = False) -> Dict[str, Any]:
        """Add existing issue as sub-issue.

        Args:
            parent_ref: Parent issue reference (number or URL)
            sub_issue_ref: Sub-issue reference (number or URL)
            replace_parent: Whether to replace existing parent

        Returns:
            API response

        Raises:
            RuntimeError: When API call fails
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)
        sub_issue_repo, sub_issue_number = self._parse_issue_reference(sub_issue_ref)

        parent_id = self._get_issue_id(parent_repo, parent_number)
        sub_issue_id = self._get_issue_id(sub_issue_repo, sub_issue_number)

        replace_str = "true" if replace_parent else "false"
        query = f"""
        mutation addSubIssue {{
            addSubIssue(input: {{ issueId: "{parent_id}", subIssueId: "{sub_issue_id}", replaceParent: {replace_str} }}) {{
                issue {{
                    number
                    title
                }}
                subIssue {{
                    number
                    title
                }}
            }}
        }}
        """

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            logger.info(f"Added sub-issue #{sub_issue_number} to parent #{parent_number}")
            return data
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to add sub-issue: {e.stderr}")

    def remove_sub_issue(self, parent_ref: str, sub_issue_ref: str) -> Dict[str, Any]:
        """Remove sub-issue.

        Args:
            parent_ref: Parent issue reference (number or URL)
            sub_issue_ref: Sub-issue reference (number or URL)

        Returns:
            API response

        Raises:
            RuntimeError: When API call fails
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)
        sub_issue_repo, sub_issue_number = self._parse_issue_reference(sub_issue_ref)

        parent_id = self._get_issue_id(parent_repo, parent_number)
        sub_issue_id = self._get_issue_id(sub_issue_repo, sub_issue_number)

        query = f"""
        mutation removeSubIssue {{
            removeSubIssue(input: {{ issueId: "{parent_id}", subIssueId: "{sub_issue_id}" }}) {{
                issue {{
                    number
                    title
                }}
                subIssue {{
                    number
                    title
                }}
            }}
        }}
        """

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            logger.info(f"Removed sub-issue #{sub_issue_number} from parent #{parent_number}")
            return data
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to remove sub-issue: {e.stderr}")

    def list_sub_issues(self, parent_ref: str, state: str = "OPEN") -> List[Dict[str, Any]]:
        """Get list of sub-issues.

        Args:
            parent_ref: Parent issue reference (number or URL)
            state: Issue state (OPEN, CLOSED, ALL)

        Returns:
            List of sub-issues

        Raises:
            RuntimeError: When API call fails
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)
        owner, repo = parent_repo.split("/")

        query = f"""
        {{
            repository(owner: "{owner}", name: "{repo}") {{
                issue(number: {parent_number}) {{
                    number
                    title
                    subIssues(first: 100) {{
                        nodes {{
                            number
                            title
                            state
                            url
                            assignees(first: 10) {{
                                nodes {{
                                    login
                                }}
                            }}
                        }}
                    }}
                    subIssuesSummary {{
                        total
                        completed
                        percentCompleted
                    }}
                }}
            }}
        }}
        """

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            
            issue_data = data.get("data", {}).get("repository", {}).get("issue", {})
            sub_issues = issue_data.get("subIssues", {}).get("nodes", [])

            # Filter by state
            if state != "ALL":
                sub_issues = [si for si in sub_issues if si.get("state") == state]
            
            logger.debug(f"Found {len(sub_issues)} sub-issues for #{parent_number}")
            return sub_issues
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to list sub-issues: {e.stderr}")

    def create_sub_issue(
        self,
        parent_ref: str,
        title: str,
        body: Optional[str] = None,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Create a new sub-issue.

        Args:
            parent_ref: Parent issue reference (number or URL)
            title: Issue title
            body: Issue body
            labels: List of labels
            assignees: List of users to assign

        Returns:
            Created issue information

        Raises:
            RuntimeError: When issue creation fails
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)

        # Create issue first
        cmd = ["gh", "issue", "create", "--repo", parent_repo, "--title", title]
        
        if body:
            cmd.extend(["--body", body])
        
        if labels:
            for label in labels:
                cmd.extend(["--label", label])
        
        if assignees:
            for assignee in assignees:
                cmd.extend(["--assignee", assignee])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            issue_url = result.stdout.strip()
            # Extract issue number from URL
            match = re.search(r"/issues/(\d+)", issue_url)
            if not match:
                raise RuntimeError(f"Failed to extract issue number from URL: {issue_url}")
            
            issue_number = int(match.group(1))
            logger.info(f"Created issue #{issue_number}: {title}")

            # Add as sub-issue
            self.add_sub_issue(parent_ref, str(issue_number))

            return {
                "number": issue_number,
                "title": title,
                "url": issue_url,
            }
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to create sub-issue: {e.stderr}")

