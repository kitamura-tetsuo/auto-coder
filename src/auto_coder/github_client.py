"""
GitHub API client for Auto-Coder.
"""

import json
import subprocess
import threading
import types
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from github import Github, Issue, PullRequest, Repository
from github.GithubException import GithubException

from .logger_config import get_logger
from .util.gh_cache import get_caching_client, get_ghapi_client

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
        self._initialized = True
        self._sub_issue_cache: Dict[Tuple[str, int], List[int]] = {}
        self._caching_client: Optional[httpx.Client] = None
        self._caching_client_lock = threading.Lock()

        # MONKEY-PATCHING PYGTIHUB FOR CACHING
        # -------------------------------------
        # WHAT: We are replacing PyGithub's internal `requestJsonAndCheck` method with our own
        # `_caching_requester`. This allows us to intercept all REST API calls.
        #
        # WHY: PyGithub does not provide a public API to substitute the underlying HTTP client or to add
        # caching middleware. To implement ETag-based caching for GET requests without forking the
        # library, monkey-patching is the most pragmatic approach.
        #
        # RISK: This implementation is tightly coupled to the internal structure of PyGithub. The
        # attributes `_Github__requester` and its method `requestJsonAndCheck` are private and could
        # change in any future version of the library, which would break this integration. This is a
        # calculated risk to gain significant performance benefits.
        self._original_requester = self.github._Github__requester.requestJsonAndCheck  # type: ignore
        self.github._Github__requester.requestJsonAndCheck = types.MethodType(lambda requester, verb, url, parameters=None, headers=None, input=None, cnx=None: self._caching_requester(requester, verb, url, parameters, headers, input, cnx), self.github._Github__requester)  # type: ignore

        # Memory cache for open issues to avoid re-fetching in loops
        self._open_issues_cache: Optional[List[Dict[str, Any]]] = None
        self._open_issues_cache_time: Optional[datetime] = None
        self._open_issues_cache_repo: Optional[str] = None
        self._open_issues_cache_lock = threading.Lock()

    def _caching_requester(self, requester, verb, url, parameters=None, headers=None, input=None, cnx=None):
        """
        A custom requester for PyGithub that uses httpx with caching for GET requests.
        """
        if verb.upper() == "GET":
            if self._caching_client is None:
                with self._caching_client_lock:
                    if self._caching_client is None:
                        self._caching_client = get_caching_client()

            # Construct the full URL if it's not already
            if not url.startswith("http"):
                url = f"{requester._Requester__base_url}{url}"

            # Prepare headers for httpx
            final_headers = {
                "Authorization": f"bearer {self.token}",
                "Accept": "application/vnd.github.v3+json",
            }
            if headers:
                final_headers.update(headers)

            response = self._caching_client.get(url, headers=final_headers, params=parameters, timeout=30)
            try:
                # We cannot use `response.raise_for_status()` for two reasons:
                # 1. It raises an error on 304 Not Modified, which is a success condition for caching.
                # 2. Responses from `hishel`'s cache may lack the `.request` attribute, causing a `RuntimeError`.
                if response.status_code >= 400:
                    # Manually trigger the exception handling path.
                    raise httpx.HTTPStatusError(
                        f"Error response {response.status_code} while requesting {response.url}",
                        request=httpx.Request("GET", url),  # Dummy request to satisfy the constructor
                        response=response,
                    )

                # PyGithub's requester returns a tuple (headers, data).
                # By calling .read(), we ensure the response body is consumed, which is necessary
                # for `hishel` to store the response in its cache.
                body = response.read()
                response_data = json.loads(body) if body else None

                # PyGithub's requester expects a case-insensitive dict-like object for headers.
                class HeaderWrapper(dict):
                    def getheader(self, name, default=None):
                        return self.get(name.lower(), default)

                # Normalize header keys to lowercase for consistent access.
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                return HeaderWrapper(response_headers), response_data
            except httpx.HTTPStatusError as e:
                # Convert httpx exception to GithubException
                raise GithubException(
                    status=e.response.status_code,
                    data=e.response.text,
                    headers=e.response.headers,
                )
            finally:
                # Ensure the response stream is closed to free up resources.
                response.close()
        else:
            # For non-GET requests, use the original requester
            return self._original_requester(verb, url, parameters, headers, input, cnx)

    def __new__(cls, *args: Any, **kwargs: Any) -> "GitHubClient":
        """Implement thread-safe singleton pattern.

        The actual singleton logic is implemented in get_instance().
        This method just creates the instance; get_instance() controls singleton behavior.
        """
        return super().__new__(cls)

    @classmethod
    def get_instance(cls, token: Optional[str] = None, disable_labels: bool = False) -> "GitHubClient":
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
                    if token is None:
                        raise ValueError("GitHub token is required on first call to get_instance()")
                    type(instance).__init__(instance, token, disable_labels)
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance.

        Use reset_singleton() in tests to clear the instance.
        """
        with cls._lock:
            cls._instance = None

    def graphql_query(self, query: str, variables: Optional[Dict[str, Any]] = None, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        Executes a GraphQL query against the GitHub API using a caching client.

        Args:
            query: The GraphQL query string.
            variables: A dictionary of variables for the query.
            extra_headers: Optional extra headers to include in the request.

        Returns:
            The JSON response from the API as a dictionary.

        Raises:
            httpx.HTTPStatusError: If the API returns a non-200 status code.
            ValueError: If the response contains GraphQL errors.
        """
        if self._caching_client is None:
            self._caching_client = get_caching_client()
        url = "https://api.github.com/graphql"
        headers = {
            "Authorization": f"bearer {self.token}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            response = self._caching_client.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                error_messages = [err.get("message", "Unknown error") for err in data["errors"]]
                logger.error(f"GraphQL query failed with errors: {', '.join(error_messages)}")
                # For cache debugging, log if the response was from cache
                if getattr(response, "from_cache", False):
                    logger.debug("GraphQL error response was served from cache.")
                raise ValueError(f"GraphQL query failed: {', '.join(error_messages)}")

            return data

        except httpx.HTTPStatusError as e:
            logger.error(f"GraphQL query failed with HTTP status {e.response.status_code}: {e.response.text}")
            raise
        except Exception as e:
            logger.error(f"An unexpected error occurred during GraphQL query: {e}")
            raise

    def clear_sub_issue_cache(self) -> None:
        """Clear the sub-issue cache."""
        self._sub_issue_cache.clear()

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

    def get_open_prs_json(self, repo_name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get open pull requests from repository using REST API (cached).

        Matches the output format expected by automation engine.
        Uses N+1 calls to fetch full details but leverages hishel cache to avoid rate limits
        on subsequent runs.
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # List PRs (automatically pages)
            # GhApi paged operations return a generator or we can just fetch pages manually or use max limit
            # For simplicity with 'limit', we can use per_page.
            # GhApi doesn't auto-page in simple calls usually unless using paged helper.
            # Let's use simple list for now, assuming < 100 PRs usually, or implement paging if needed.
            # limit defaults to 100.

            prs_summary = api.pulls.list(owner, repo, state="open", per_page=limit)

            all_prs: List[Dict[str, Any]] = []

            for pr_summary in prs_summary:
                # Fetch full details for mergeable status, etc.
                try:
                    pr_num = pr_summary["number"] if isinstance(pr_summary, dict) else pr_summary.number
                    pr_details = api.pulls.get(owner, repo, pr_num)
                except Exception as e:
                    logger.warning(f"Failed to fetch details for PR #{pr_summary.get('number', 'unknown')}: {e}")
                    continue

                # Map to required format (safely handling dict vs AttrDict)
                def get_val(obj, key, default=None):
                    if isinstance(obj, dict):
                        return obj.get(key, default)
                    return getattr(obj, key, default)

                # Helper since GhApi might return different types
                # Using bracket access is safer if we know it works for both (AttrDict is dict)
                # But let's use a safe accessor to be sure.
                d = pr_details

                pr_data: Dict[str, Any] = {
                    "number": d["number"],
                    "title": d["title"],
                    "node_id": d["node_id"],
                    "body": d["body"] or "",
                    "state": d["state"].lower(),
                    "url": d["html_url"],
                    "created_at": d["created_at"],
                    "updated_at": d["updated_at"],
                    "draft": d["draft"],
                    "mergeable": d["mergeable"],
                    "head_branch": d["head"]["ref"],
                    "head": {"ref": d["head"]["ref"], "sha": d["head"]["sha"]},
                    "base_branch": d["base"]["ref"],
                    "author": d["user"]["login"] if d["user"] else None,
                    "assignees": [a["login"] for a in d["assignees"]],
                    "labels": [lbl["name"] for lbl in d["labels"]],
                    "comments_count": d["comments"] + d["review_comments"],
                    "commits_count": d["commits"],
                    "additions": d["additions"],
                    "deletions": d["deletions"],
                    "changed_files": d["changed_files"],
                }
                all_prs.append(pr_data)

                if len(all_prs) >= limit:
                    break

            logger.info(f"Retrieved {len(all_prs)} open pull requests from {repo_name} via REST (cached)")
            return all_prs

        except Exception as e:
            logger.error(f"Failed to get open PRs via REST from {repo_name}: {e}")
            raise

    def _update_cached_issue(self, repo_name: str, issue_number: int, **kwargs: Any) -> None:
        """Update an issue in the memory cache.

        Args:
            repo_name: Repository name
            issue_number: Issue number to update
            **kwargs: Fields to update (if 'state' is 'closed', issue is removed)
        """
        with self._open_issues_cache_lock:
            # Only update if cache is valid and for the same repo
            if self._open_issues_cache is not None and self._open_issues_cache_repo == repo_name:
                # Find the issue
                for i, issue in enumerate(self._open_issues_cache):
                    if issue.get("number") == issue_number:
                        # If state is becoming closed, remove it
                        if kwargs.get("state") == "closed":
                            self._open_issues_cache.pop(i)
                            logger.debug(f"Removed closed issue #{issue_number} from cache")
                        else:
                            # Update fields
                            issue.update(kwargs)
                            logger.debug(f"Updated issue #{issue_number} in cache: {kwargs.keys()}")
                        return

    def get_open_issues_json(self, repo_name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get open issues from repository using REST API (cached).

        Matches the output format expected by automation engine.
        Uses N+1 calls if necessary, but tries to stay efficient.
        Note: Sub-issues and Linked PRs via timeline are expensive to fetch via REST for all issues.
        We return empty lists for those fields in this implementation to respect the REST/caching requirement.
        """
        # Check memory cache
        with self._open_issues_cache_lock:
            if self._open_issues_cache is not None and self._open_issues_cache_repo == repo_name and self._open_issues_cache_time and datetime.now() - self._open_issues_cache_time < timedelta(minutes=5):
                logger.info(f"Returning cached open issues for {repo_name} (age: {datetime.now() - self._open_issues_cache_time})")
                return list(self._open_issues_cache)

        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # List Issues (state=open)
            # per_page=limit. Note: GitHub treats PRs as Issues, so we must filter them out.
            issues_summary = api.issues.list_for_repo(owner, repo, state="open", per_page=limit)

            all_issues: List[Dict[str, Any]] = []

            for issue in issues_summary:
                # Filter out Pull Requests (which are returned in issues list by REST API)
                if "pull_request" in issue:
                    continue

                # For basic fields, the summary is often enough.
                # If we really need details (e.g. body if truncated?), we might get it.
                # But 'list_for_repo' usually returns full body.

                # Sub-issues and Linked PRs:
                # These require traversing timeline/events or specific beta endpoints.
                # To avoid excessive API calls (which even if cached are slow to populate initially),
                # we provide empty placeholders. If logic critically depends on them,
                # we might need a specific on-demand fetch in automation_engine.

                # Safe access (dict expected)
                i = issue

                issue_data: Dict[str, Any] = {
                    "number": i["number"],
                    "title": i["title"],
                    "body": i["body"] or "",
                    "state": i["state"],
                    "labels": [lbl["name"] for lbl in i["labels"]],
                    "assignees": [a["login"] for a in i["assignees"]],
                    "created_at": i["created_at"],
                    "updated_at": i["updated_at"],
                    "url": i["html_url"],
                    "author": i["user"]["login"] if i["user"] else None,
                    "comments_count": i["comments"],
                    # Extended fields
                    "linked_prs": [],  # Not efficiently available via REST list
                    "open_sub_issue_numbers": [],  # Not available via REST list
                    "parent_number": None,  # Not available via REST standard
                }

                # If we wanted linked PRs, we'd call api.issues.list_events(..., issue_number=issue.number)
                # and look for 'cross-referenced' events.

                all_issues.append(issue_data)

                if len(all_issues) >= limit:
                    break

            logger.info(f"Retrieved {len(all_issues)} open issues from {repo_name} via REST (cached)")

            # Update cache
            with self._open_issues_cache_lock:
                self._open_issues_cache = all_issues
                self._open_issues_cache_repo = repo_name
                self._open_issues_cache_time = datetime.now()

            return all_issues

        except Exception as e:
            logger.error(f"Failed to get open issues via REST from {repo_name}: {e}")
            raise

    def get_issue(self, repo_name: str, issue_number: int) -> Optional[Any]:
        """Get a single issue by number using REST API (cached).

        Returns an object compatible with dot usage (e.g. issue.title, issue.body)
        like GhApi's AttrDict.
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)
            return api.issues.get(owner, repo, issue_number)
        except Exception as e:
            logger.warning(f"Failed to get issue #{issue_number} from {repo_name}: {e}")
            return None

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

    def _get_issue_timeline(self, repo_name: str, issue_number: int) -> List[Dict[str, Any]]:
        """Get timeline for an issue using GitHub REST API.

        Endpoint: /repos/{owner}/{repo}/issues/{issue_number}/timeline
        """
        try:
            owner, repo = repo_name.split("/")
            if self._caching_client is None:
                self._caching_client = get_caching_client()

            # Use loose pagination or just get first page?
            # Usually recent events are what we want? The endpoint returns all or paginated.
            # Using standard per_page=100
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/timeline?per_page=100"
            headers = {
                "Authorization": f"bearer {self.token}",
                "Accept": "application/vnd.github.v3+json",
                # "X-GitHub-Api-Version": "2022-11-28" # Standard API
            }

            # Simple handling for now - assuming recent events are on first page or reasonable number.
            # If a PR is linked, it should be in the timeline.
            response = self._caching_client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()

        except Exception as e:
            logger.warning(f"Failed to get timeline for issue #{issue_number}: {e}")
            return []

    def get_linked_prs(self, repo_name: str, issue_number: int) -> List[int]:
        """Get PRs linked to this issue via REST Timeline.

        Replaces get_linked_prs_via_graphql.
        Look for 'connected' (closing) or 'cross-referenced' (mention) events.
        """
        try:
            timeline = self._get_issue_timeline(repo_name, issue_number)
            pr_numbers = set()

            for event in timeline:
                event_type = event.get("event")
                # 'connected' means it was linked as closing (fix/close keyword or sidebar)
                # 'cross-referenced' means it was mentioned
                if event_type in ["connected", "cross-referenced"]:
                    source = event.get("source", {})
                    # For cross-referenced, source implies who mentioned it.
                    # For connected, source is the PR that was connected.

                    # Structure for cross-referenced: source.issue.number (if from a PR/issue)
                    # Structure for connected: source.issue.number

                    # It might vary. Let's inspect 'source'.
                    # Usually source -> issue -> number
                    if "issue" in source:
                        # Check if it is a PR
                        issue_obj = source["issue"]
                        if "pull_request" in issue_obj:
                            pr_numbers.add(issue_obj["number"])

                # NOTE: Timeline logic can be complex.
                # cross-referenced source might be just the issue object directly in some API versions?
                # REST API docs say: source: { type: "issue", issue: { ... } }

            return list(pr_numbers)

        except Exception as e:
            logger.error(f"Failed to get linked PRs for issue #{issue_number}: {e}")
            return []

    # Deprecated/Removed: get_linked_prs_via_graphql

    def has_linked_pr(self, repo_name: str, issue_number: int) -> bool:
        """Check if an issue has a linked pull request.

        First tries REST Timeline API, then falls back to searching PR titles/bodies.

        Returns True if there is an open PR that references this issue.
        """
        try:
            # First try REST Timeline (replaces GraphQL)
            linked_prs = self.get_linked_prs(repo_name, issue_number)
            if linked_prs:
                # We need to check if any of these are OPEN.
                # get_linked_prs just returns numbers.
                for pr_num in linked_prs:
                    try:
                        pr_data = self.get_pr_details(self.get_repository(repo_name).get_pull(pr_num))
                        if pr_data.get("state") == "open":
                            return True
                    except:
                        continue

            # Fallback: Search for PRs that reference this issue in title/body
            # (Existing logic remains)
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

    def find_closing_pr(self, repo_name: str, issue_number: int) -> Optional[int]:
        """Find a PR that closes the given issue.

        Updated to use REST Timeline.
        """
        try:
            # Check timeline for 'connected' events (strongest link)
            timeline = self._get_issue_timeline(repo_name, issue_number)
            for event in timeline:
                if event.get("event") == "connected":
                    source = event.get("source", {})
                    if "issue" in source and "pull_request" in source["issue"]:
                        pr_num = source["issue"]["number"]
                        # Check if open
                        try:
                            pr_data = self.get_pr_details(self.get_repository(repo_name).get_pull(pr_num))
                            if pr_data.get("state") == "open":
                                logger.info(f"Found closing PR #{pr_num} via timeline 'connected' event")
                                return pr_num
                        except:
                            continue

            # Fallback: Search for PRs that reference this issue in title/body
            # (Existing logic remains)
            repo = self.get_repository(repo_name)
            prs = repo.get_pulls(state="open")

            issue_ref_patterns = [
                f"fixes #{issue_number}",
                f"fix issue #{issue_number}",
                f"close #{issue_number}",
                f"closes #{issue_number}",
                f"resolves #{issue_number}",
                f"#{issue_number}",  # Direct issue reference in title
                f"issue #{issue_number}",  # Issue reference in body
            ]

            for pr in prs:
                pr_text = f"{pr.title} {pr.body or ''}".lower()
                if any(pattern.lower() in pr_text for pattern in issue_ref_patterns):
                    logger.info(f"Found closing PR #{pr.number} for issue #{issue_number} via text search")
                    return pr.number

            logger.debug(f"No closing PR found for issue #{issue_number}")
            return None

        except GithubException as e:
            logger.error(f"Failed to find closing PR for issue #{issue_number}: {e}")
            return None

    def verify_pr_closes_issue(self, repo_name: str, pr_number: int, issue_number: int) -> bool:
        """Verify if a PR is linked to close an issue via REST Timeline.

        Replaces usage of get_pr_closing_issues for validation.
        """
        try:
            timeline = self._get_issue_timeline(repo_name, issue_number)
            # Check for 'connected' event from this PR
            for event in timeline:
                if event.get("event") == "connected":
                    source = event.get("source", {})
                    if "issue" in source and source["issue"].get("number") == pr_number:
                        return True

            # If not found in connected, it might be just text referenced but not yet 'connected'
            # (GitHub sometimes delays connecting, or if it's not a mergeable branch yet?)
            # But the caller usually wants to check if we SET IT UP correctly.
            # If we just created the PR, the event might not exist yet?
            # Actually, the user code waits 2 seconds.

            return False
        except Exception as e:
            logger.warning(f"Failed to verify PR closing link: {e}")
            return False

    # Deprecated/Removed: get_pr_closing_issues

    def get_parent_issue_details(self, repo_name: str, issue_number: int) -> Optional[Dict[str, Any]]:
        """Get details of the parent issue if it exists using GitHub REST API.

        This uses the REST API with the sub-issues preview header.
        """
        try:
            owner, repo = repo_name.split("/")

            if self._caching_client is None:
                self._caching_client = get_caching_client()

            # Fetch the issue itself with the version header that exposes 'parent'
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
            headers = {"Authorization": f"bearer {self.token}", "Accept": "application/vnd.github.v3+json", "X-GitHub-Api-Version": "2022-11-28"}

            response = self._caching_client.get(url, headers=headers)
            response.raise_for_status()
            issue_data = response.json()

            # The 'parent' field should be present if the feature is active and issue has a parent
            parent_issue = issue_data.get("parent")

            if parent_issue:
                logger.info(f"Issue #{issue_number} has parent issue #{parent_issue.get('number')}: {parent_issue.get('title')}")
                # Ensure we return a consistent dict, assuming API returns standard issue object subset
                return parent_issue

            return None

        except Exception as e:
            logger.error(f"Failed to get parent issue for issue #{issue_number}: {e}")
            return None

    def get_parent_issue(self, repo_name: str, issue_number: int) -> Optional[int]:
        """Get parent issue number for a given issue.

        Wrapper around get_parent_issue_details for backward compatibility.

        Args:
            repo_name: Repository name in format 'owner/repo'
            issue_number: Issue number to check for parent issue

        Returns:
            Parent issue number if exists, None otherwise.
        """
        parent_details = self.get_parent_issue_details(repo_name, issue_number)
        if parent_details:
            return int(parent_details["number"])
        return None

    def get_parent_issue_body(self, repo_name: str, issue_number: int) -> Optional[str]:
        """Get parent issue body content for a given issue using REST API.

        Args:
            repo_name: Repository name in format 'owner/repo'
            issue_number: Issue number to check for parent issue

        Returns:
            Parent issue body as a string if exists, None otherwise.
        """
        try:
            # First get parent issue details to check if parent exists
            parent_details = self.get_parent_issue_details(repo_name, issue_number)
            if not parent_details:
                logger.debug(f"Issue #{issue_number} has no parent issue")
                return None

            parent_number = parent_details.get("number")
            if not parent_number:
                logger.debug(f"Issue #{issue_number} parent has no number")
                return None

            logger.debug(f"Fetching body for parent issue #{parent_number} of issue #{issue_number}")

            # Use standard REST get_issue which is already migrated/available
            parent_issue = self.get_issue(repo_name, parent_number)
            if parent_issue:
                # parent_issue might be object or dict depending on get_issue impl (AttrDict usually)
                body = getattr(parent_issue, "body", None) or parent_issue.get("body")
                if body:
                    logger.info(f"Retrieved body for parent issue #{parent_number} ({len(body) if body else 0} chars)")
                    return body

            logger.debug(f"No body found for parent issue #{parent_number}")
            return None

        except Exception as e:
            logger.error(f"Failed to get parent issue body for issue #{issue_number}: {e}")
            return None

    def create_issue(self, repo_name: str, title: str, body: str, labels: Optional[List[str]] = None) -> Issue.Issue:
        """Create a new issue in the repository."""
        try:
            repo = self.get_repository(repo_name)
            issue = repo.create_issue(title=title, body=body, labels=labels or [])
            logger.info(f"Created issue #{issue.number}: {title}")

            # Invalidate cache as we can't easily append the full GraphQL structure
            with self._open_issues_cache_lock:
                self._open_issues_cache = None

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

            # Update cache
            self._update_cached_issue(repo_name, issue_number, state="closed")

        except GithubException as e:
            logger.error(f"Failed to close issue #{issue_number}: {e}")
            raise

    def reopen_issue(self, repo_name: str, issue_number: int, comment: Optional[str] = None) -> None:
        """Reopen a closed issue with optional comment.

        Args:
            repo_name: Repository name in format 'owner/repo'
            issue_number: Issue number to reopen
            comment: Optional comment to add when reopening
        """
        try:
            repo = self.get_repository(repo_name)
            issue = repo.get_issue(issue_number)

            if comment:
                issue.create_comment(comment)

            issue.edit(state="open")
            logger.info(f"Reopened issue #{issue_number}")

            # Invalidate cache as we don't have the full object to re-add
            with self._open_issues_cache_lock:
                self._open_issues_cache = None

        except GithubException as e:
            logger.error(f"Failed to reopen issue #{issue_number}: {e}")

    def create_commit_status(
        self,
        repo_name: str,
        sha: str,
        state: str,
        target_url: str = "",
        description: str = "",
        context: str = "default",
    ) -> None:
        """Create a commit status.

        Args:
            repo_name: Repository name in format 'owner/repo'
            sha: Commit SHA
            state: Status state (pending, success, error, failure)
            target_url: URL to link to
            description: Description of the status
            context: Context label for the status
        """
        try:
            repo = self.get_repository(repo_name)
            commit = repo.get_commit(sha)
            commit.create_status(
                state=state,
                target_url=target_url,
                description=description,
                context=context,
            )
            logger.info(f"Created commit status '{state}' for {sha[:8]} (context: {context})")

        except GithubException as e:
            logger.error(f"Failed to create commit status for {sha[:8]}: {e}")
            raise
            raise

    def close_pr(self, repo_name: str, pr_number: int, comment: Optional[str] = None) -> None:
        """Close a pull request with optional comment.

        Args:
            repo_name: Repository name in format 'owner/repo'
            pr_number: PR number to close
            comment: Optional comment to add when closing
        """
        try:
            repo = self.get_repository(repo_name)
            pr = repo.get_pull(pr_number)

            if comment:
                pr.create_issue_comment(comment)

            pr.edit(state="closed")
            logger.info(f"Closed PR #{pr_number}")

        except GithubException as e:
            logger.error(f"Failed to close PR #{pr_number}: {e}")
            raise

    def add_comment_to_pr(self, repo_name: str, pr_number: int, comment: str) -> None:
        """Add a comment to a pull request.

        Args:
            repo_name: Repository name in format 'owner/repo'
            pr_number: PR number to comment on
            comment: Comment body to add
        """
        try:
            repo = self.get_repository(repo_name)
            pr = repo.get_pull(pr_number)
            pr.create_issue_comment(comment)
            logger.info(f"Added comment to PR #{pr_number}")

        except GithubException as e:
            logger.error(f"Failed to add comment to PR #{pr_number}: {e}")
            raise

    def get_pr_comments(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get all comments for a pull request.

        Args:
            repo_name: Repository name in format 'owner/repo'
            pr_number: PR number to get comments for

        Returns:
            List of comments as dictionaries (containing body, created_at, user, etc.)
        """
        try:
            repo = self.get_repository(repo_name)
            pr = repo.get_pull(pr_number)
            comments = []
            for comment in pr.get_issue_comments():
                comments.append({"body": comment.body, "created_at": comment.created_at.isoformat(), "user": {"login": comment.user.login} if comment.user else None, "id": comment.id})
            return comments
        except GithubException as e:
            logger.error(f"Failed to get comments for PR #{pr_number}: {e}")
            return []

    def get_pr_commits(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get all commits for a pull request.

        Args:
            repo_name: Repository name in format 'owner/repo'
            pr_number: PR number to get commits for

        Returns:
            List of commits as dictionaries
        """
        try:
            repo = self.get_repository(repo_name)
            pr = repo.get_pull(pr_number)
            commits = []
            for commit in pr.get_commits():
                commits.append({"sha": commit.sha, "commit": {"message": commit.commit.message, "committer": {"date": commit.commit.committer.date.isoformat(), "name": commit.commit.committer.name}}})
            return commits
        except GithubException as e:
            logger.error(f"Failed to get commits for PR #{pr_number}: {e}")
            return []

    def get_all_sub_issues(self, repo_name: str, issue_number: int) -> List[int]:
        """Get list of all sub-issues for a given issue using GitHub GraphQL API.

        Fetches both open and closed sub-issues.

        Args:
            repo_name: Repository name in format 'owner/repo'
            issue_number: Issue number to check for sub-issues

        Returns:
            List of issue numbers that are linked to the issue (both open and closed).
        """

    def get_open_sub_issues(self, repo_name: str, issue_number: int) -> List[int]:
        """Get list of open sub-issues using GitHub REST API.

        Uses the sub-issues endpoint: /repos/{owner}/{repo}/issues/{issue_number}/sub_issues
        """
        all_sub_issues = self.get_all_sub_issues(repo_name, issue_number)
        open_sub_issues = []

        # We need to check the state of each sub-issue.
        # The list_sub_issues endpoint might return state, checking...
        # If the endpoint returns issue objects, they have a 'state' field.
        # Assuming get_all_sub_issues now returns objects or we fetch them.
        # To keep get_all_sub_issues returning List[int] as per signature,
        # we might need to fetch details here, OR update get_all_sub_issues to return dicts?
        # The current contract says List[int].

        # Let's verify what the endpoint returns. Usually list of issues.
        # For efficiency, let's have a private method that returns the full data.

        try:
            sub_issues_data = self._fetch_sub_issues_data(repo_name, issue_number)
            open_sub_issues = [i["number"] for i in sub_issues_data if i.get("state") == "open"]

            # Update cache for open sub-issues (compatibility)
            cache_key = (repo_name, issue_number)
            self._sub_issue_cache[cache_key] = open_sub_issues

            return open_sub_issues
        except Exception as e:
            logger.error(f"Failed to get open sub-issues for #{issue_number}: {e}")
            return []

    def get_all_sub_issues(self, repo_name: str, issue_number: int) -> List[int]:
        """Get all sub-issues (open and closed) using GitHub REST API."""
        try:
            sub_issues_data = self._fetch_sub_issues_data(repo_name, issue_number)
            return [i["number"] for i in sub_issues_data]
        except Exception as e:
            logger.error(f"Failed to get all sub-issues for issue #{issue_number}: {e}")
            return []

    def _fetch_sub_issues_data(self, repo_name: str, issue_number: int) -> List[Dict[str, Any]]:
        """Fetch raw sub-issues data from REST API."""
        try:
            owner, repo = repo_name.split("/")
            # Attempt to use GhApi if available, but it might not have the method yet
            # Endpoint: GET /repos/{owner}/{repo}/issues/{issue_number}/sub_issues
            # Using raw caching client for certainty and custom headers if needed

            if self._caching_client is None:
                self._caching_client = get_caching_client()

            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/sub_issues"
            headers = {"Authorization": f"bearer {self.token}", "Accept": "application/vnd.github.v3+json", "X-GitHub-Api-Version": "2022-11-28"}  # As hinted by user docs

            response = self._caching_client.get(url, headers=headers)

            # If 404, it might simply mean no sub-issues or feature not enabled, return empty
            if response.status_code == 404:
                return []

            response.raise_for_status()
            return response.json()

        except Exception as e:
            logger.debug(f"Failed to fetch sub-issues data via REST: {e}")
            return []

    def add_labels(self, repo_name: str, issue_number: int, labels: List[str], item_type: str = "issue") -> None:
        """Add labels to an existing issue or PR.

        Args:
            repo_name: Repository name (owner/repo)
            issue_number: Issue or PR number
            labels: List of labels to add
            item_type: Type of item ('issue' or 'pr'), defaults to 'issue'
        """
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping add labels {labels} to {item_type} #{issue_number}")
            return

        try:
            repo = self.get_repository(repo_name)
            if item_type.lower() == "pr":
                pr = repo.get_pull(issue_number)
                # Get current labels
                current_labels = [label.name for label in pr.labels]
                # If any of the requested labels already exist on the PR, skip entirely (consistent with try_add_labels)
                existing_labels = [lbl for lbl in labels if lbl in current_labels]
                if existing_labels:
                    logger.info(f"PR #{issue_number} already has label(s) {existing_labels} - skipping")
                else:
                    # For PRs, use add_to_labels method for each new label
                    for label in labels:
                        pr.add_to_labels(label)
                    logger.info(f"Added labels {labels} to PR #{issue_number}")
            else:
                issue = repo.get_issue(issue_number)
                # Get current labels
                current_labels = [label.name for label in issue.labels]
                # Add new labels to current ones (avoid duplicates)
                all_labels = list(set(current_labels + labels))
                # Update issue with all labels
                issue.edit(labels=all_labels)
                logger.info(f"Added labels {labels} to issue #{issue_number}")

                # Update cache
                self._update_cached_issue(repo_name, issue_number, labels=all_labels)

        except GithubException as e:
            logger.error(f"Failed to add labels to {item_type} #{issue_number}: {e}")
            raise

    def try_add_labels(self, repo_name: str, issue_number: int, labels: List[str], item_type: str = "issue") -> bool:
        """Add labels to an existing issue or PR.

        Args:
            repo_name: Repository name (owner/repo)
            issue_number: Issue or PR number
            labels: List of labels to add
            item_type: Type of item ('issue' or 'pr'), defaults to 'issue'

        Returns:
            True if labels were successfully added, False if they already exist
        """
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping add labels {labels} to {item_type} #{issue_number}")
            return True  # Return True to allow processing to continue

        try:
            # Check if any of the labels already exist
            repo = self.get_repository(repo_name)
            if item_type.lower() == "pr":
                pr = repo.get_pull(issue_number)
                current_labels = [label.name for label in pr.labels]

                # Check if any of the labels to add already exist
                existing_labels = [lbl for lbl in labels if lbl in current_labels]
                if existing_labels:
                    logger.info(f"PR #{issue_number} already has label(s) {existing_labels} - skipping")
                    return False

                # For PRs, use add_to_labels method
                for label in labels:
                    pr.add_to_labels(label)
                logger.info(f"Added labels {labels} to PR #{issue_number}")
            else:
                issue = repo.get_issue(issue_number)
                current_labels = [label.name for label in issue.labels]

                # Check if any of the labels to add already exist
                existing_labels = [lbl for lbl in labels if lbl in current_labels]
                if existing_labels:
                    logger.info(f"Issue #{issue_number} already has label(s) {existing_labels} - skipping")
                    return False

                # Add new labels to current ones (avoid duplicates)
                all_labels = list(set(current_labels + labels))
                # Update issue with all labels
                issue.edit(labels=all_labels)
                logger.info(f"Added labels {labels} to issue #{issue_number}")

                # Update cache
                self._update_cached_issue(repo_name, issue_number, labels=all_labels)

            return True

        except GithubException as e:
            logger.error(f"Failed to add labels to {item_type} #{issue_number}: {e}")
            raise

    def remove_labels(self, repo_name: str, item_number: int, labels: List[str], item_type: str = "issue") -> None:
        """Remove labels from an existing issue or PR.

        Args:
            repo_name: Repository name (owner/repo)
            item_number: Issue or PR number
            labels: List of labels to remove
            item_type: Type of item ('issue' or 'pr'), defaults to 'issue'
        """
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping remove labels {labels} from {item_type} #{item_number}")
            return

        try:
            repo = self.get_repository(repo_name)
            if item_type.lower() == "pr":
                pr = repo.get_pull(item_number)
                # For PRs, use remove_from_labels method
                for label in labels:
                    pr.remove_from_labels(label)
                logger.info(f"Removed labels {labels} from PR #{item_number}")
            else:
                issue = repo.get_issue(item_number)
                # Get current labels
                current_labels = [label.name for label in issue.labels]
                # Remove specified labels
                remaining_labels = [label for label in current_labels if label not in labels]
                # Update issue with remaining labels
                issue.edit(labels=remaining_labels)
                logger.info(f"Removed labels {labels} from issue #{item_number}")

                # Update cache
                self._update_cached_issue(repo_name, item_number, labels=remaining_labels)

        except GithubException as e:
            logger.error(f"Failed to remove labels from {item_type} #{item_number}: {e}")
            raise

    def has_label(self, repo_name: str, issue_number: int, label: str, item_type: str = "issue") -> bool:
        """Check if an issue or PR has a specific label.

        Args:
            repo_name: Repository name (owner/repo)
            issue_number: Issue or PR number
            label: Label name to check for
            item_type: Type of item ('issue' or 'pr'), defaults to 'issue'

        Returns:
            True if the label exists, False otherwise
        """
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping check for label '{label}' on {item_type} #{issue_number}")
            return False

        try:
            repo = self.get_repository(repo_name)
            if item_type.lower() == "pr":
                pr_item = repo.get_pull(issue_number)
                current_labels = [lbl.name for lbl in pr_item.labels]
            else:
                issue_item = repo.get_issue(issue_number)
                current_labels = [lbl.name for lbl in issue_item.labels]

            return label in current_labels

        except GithubException as e:
            logger.error(f"Failed to check labels for {item_type} #{issue_number}: {e}")
            raise

    def search_issues(self, query: str, sort: str = "updated", order: str = "desc") -> List[Any]:
        """Search issues using GitHub Search API.

        Args:
            query: Search query string
            sort: Sort field (default: updated)
            order: Sort order (default: desc)

        Returns:
            List of issue objects from PyGithub
        """
        try:
            logger.info(f"Searching issues with query: '{query}'")
            return list(self.github.search_issues(query, sort=sort, order=order))
        except GithubException as e:
            logger.error(f"Failed to search issues with query '{query}': {e}")
            return []

    def _search_issues_by_title(self, repo_name: str, search_title: str) -> Optional[int]:
        """Search for an open issue by title using fuzzy matching.

        Args:
            repo_name: Repository name in format 'owner/repo'
            search_title: The title to search for (case-insensitive)

        Returns:
            The issue number if found, None otherwise
        """
        try:
            issues = self.get_open_issues(repo_name)
            search_title_lower = search_title.lower()

            # First try exact match (case-insensitive)
            for issue in issues:
                if issue.title.lower() == search_title_lower:
                    logger.debug(f"Found exact match for title '{search_title}': issue #{issue.number}")
                    return issue.number

            # Then try partial match - check if search title is contained in issue title
            # or if issue title is contained in search title
            for issue in issues:
                issue_title_lower = issue.title.lower()
                # Check if search title is a significant part of the issue title
                # (at least 5 characters or 50% of the shorter title)
                min_length = min(len(search_title_lower), len(issue_title_lower))
                threshold = max(5, min_length * 0.5)

                if len(search_title_lower) >= threshold and search_title_lower in issue_title_lower:
                    logger.debug(f"Found partial match for title '{search_title}': issue #{issue.number} (title: '{issue.title}')")
                    return issue.number
                elif len(issue_title_lower) >= threshold and issue_title_lower in search_title_lower:
                    logger.debug(f"Found partial match for title '{search_title}': issue #{issue.number} (title: '{issue.title}')")
                    return issue.number

            logger.debug(f"No match found for title '{search_title}'")
            return None

        except Exception as e:
            logger.warning(f"Failed to search for issue by title '{search_title}': {e}")
            return None

    def get_issue_dependencies(self, issue_body: str, repo_name: Optional[str] = None, issue_number: Optional[int] = None) -> List[int]:
        """Extract issue numbers that this issue depends on from the issue body.

        Supports both number-based dependencies (e.g., "#123") and title-based dependencies
        (e.g., "Depends on: Sub Issue 1 (dataclass creation may be needed)").
        Also supports multi-line dependency declarations with indented lists.

        Implements fallback logic: when "Depends on:" exists but no clear targets are found,
        if a parent issue exists, treats this issue as dependent on all open sibling sub-issues.

        Args:
            issue_body: The body text of the issue
            repo_name: Repository name in format 'owner/repo' (required for title-based dependencies and fallback logic)
            issue_number: Issue number (required for fallback logic)

        Returns:
            List of issue numbers that this issue depends on

        Examples:
            "Depends on: #123" -> [123]
            "depends on #456" -> [456]
            "Depends on #789, #790" -> [789, 790]
            "Depends on: Sub Issue 1 (dataclass creation)" -> [123] (if issue #123 has that title)
            Multi-line:
                Depends on:
                    #456
                    #789
                    Sub Issue 3 (title-based)
        """
        if not issue_body:
            return []

        import re

        dependencies = []
        seen = set()

        # First pass: extract multi-line dependencies with indentation
        # This handles:
        # - "Depends on:" followed by indented lines (1+ spaces or tabs)
        # - Both numbered (#456) and titled (Some Title) dependencies
        # - Mixed formats in the same list
        # - Empty lines within the multi-line block
        # Use MULTILINE flag to make ^ match line starts
        multiline_pattern = r"(?im)^(?:depends\s+on|blocked\s+by)\s*:?\s*\n((?:^[ \t]+.*(?:\n|$)|^\n)+)"

        for match in re.finditer(multiline_pattern, issue_body):
            multiline_text = match.group(1)

            # Split into lines and process each indented line
            lines = multiline_text.split("\n")
            for line in lines:
                # Skip empty lines
                if not line.strip():
                    continue

                # Check if line starts with indentation (1+ spaces or tabs)
                if re.match(r"^[ \t]+", line):
                    # Strip indentation and leading/trailing punctuation
                    clean_line = re.sub(r"^[ \t]+", "", line).strip()
                    clean_line = re.sub(r"^[,\-\s]+|[,\-\s]+$", "", clean_line)

                    # Check if this looks like a number-based dependency (starts with # or is just a number)
                    # This ensures we don't treat titles with numbers in them as number-based
                    if re.match(r"^#\d+$", clean_line) or re.match(r"^\d+$", clean_line):
                        # Pure number or #number format - extract the issue number
                        numbers = re.findall(r"#?(\d+)", clean_line)
                        if numbers:
                            issue_num = int(numbers[0])
                            if issue_num not in seen:
                                dependencies.append(issue_num)
                                seen.add(issue_num)
                    else:
                        # Title-based dependency - search by title
                        if repo_name and len(clean_line) >= 3:
                            try:
                                title_issue_num: Optional[int] = self._search_issues_by_title(repo_name, clean_line)
                                if title_issue_num is not None and title_issue_num not in seen:
                                    dependencies.append(title_issue_num)
                                    seen.add(title_issue_num)
                                    logger.debug(f"Found multi-line title-based dependency: '{clean_line}' -> issue #{title_issue_num}")
                            except Exception as e:
                                logger.warning(f"Failed to search for title '{clean_line}': {e}")

        # Second pass: extract single-line number-based dependencies
        # This handles various formats like:
        # - "Depends on: #123"
        # - "depends on #123, #456, #789"
        # - "depends on #100 and #200 and #300"
        # - "blocked by #100"
        # Case-insensitive, with or without colon, supports comma-separated lists and "and"
        # Skip lines that are part of multi-line dependencies (followed by indented content)
        depends_pattern = r"(?i)(?:depends\s+on|blocked\s+by)\s*:?\s*([^\n#][^\n]*?)(?:\n|\r|$)"

        for match in re.finditer(depends_pattern, issue_body):
            depends_text = match.group(1).strip()

            # Skip if it looks like a multi-line header (no actual content)
            if not depends_text or depends_text in ["\n", "\r\n", "\r"]:
                continue

            # Check if this is followed by indented content (part of multi-line block)
            # If so, skip it as it will be handled by the multi-line pattern
            match_end = match.end()
            remaining_text = issue_body[match_end:]
            if remaining_text and re.match(r"^[ \t]", remaining_text):
                continue

            # Extract all issue numbers from the depends text
            numbers = re.findall(r"#?(\d+)", depends_text)
            for num_str in numbers:
                issue_num = int(num_str)
                if issue_num not in seen:
                    dependencies.append(issue_num)
                    seen.add(issue_num)

        # Third pass: handle single-line title-based dependencies
        # Pattern: "Depends on: Title (description)" or "blocked by: Title (description)"
        # We look for text that doesn't start with # but contains descriptive text
        if repo_name:
            title_depends_pattern = r"(?i)(?:depends\s+on|blocked\s+by)\s*:?\s*([^\n#][^\n]*?)(?:\s*\([^)]*\))?(?:\n|\r|$)"

            for match in re.finditer(title_depends_pattern, issue_body):
                depends_text = match.group(1).strip()

                # Skip if it contains issue numbers (already handled above)
                if re.search(r"#?\d+", depends_text):
                    continue

                # Skip if empty or too short (likely not a real title)
                if len(depends_text.strip()) < 3:
                    continue

                # Clean up the title - remove leading/trailing punctuation
                title = re.sub(r"^[,\-\s]+|[,\-\s]+$", "", depends_text)

                if title and len(title) >= 3:
                    try:
                        title_issue_num_2: Optional[int] = self._search_issues_by_title(repo_name, title)
                        if title_issue_num_2 is not None and title_issue_num_2 not in seen:
                            dependencies.append(title_issue_num_2)
                            seen.add(title_issue_num_2)
                            logger.debug(f"Found title-based dependency: '{title}' -> issue #{title_issue_num_2}")
                    except Exception as e:
                        logger.warning(f"Failed to search for title '{title}': {e}")

        # Fallback Logic: Check if "Depends on:" or "blocked by:" exists but no clear targets were found
        # This handles cases where the issue mentions dependencies but doesn't specify them clearly
        if not dependencies and issue_body and repo_name and issue_number:
            try:
                # Check if the issue body contains dependency-related keywords
                has_depends_on = re.search(r"(?i)\b(depends\s+on|blocked\s+by)\b", issue_body)

                if has_depends_on:
                    logger.debug(f"Found 'Depends on' or 'blocked by' in issue #{issue_number} but no clear targets - checking for fallback")
                    logger.debug(f"No clear dependency targets found for issue #{issue_number} - attempting fallback logic")

                    # Try to get parent issue
                    try:
                        parent_issue_number = self.get_parent_issue(repo_name, issue_number)

                        if parent_issue_number is not None:
                            logger.info(f"Issue #{issue_number} has parent issue #{parent_issue_number} - checking for sibling dependencies")
                            # Get all open sub-issues of the parent
                            try:
                                sibling_issues = self.get_open_sub_issues(repo_name, parent_issue_number)
                                # Filter out self (exclude current issue number)
                                sibling_issues = [num for num in sibling_issues if num != issue_number]

                                if sibling_issues:
                                    logger.info(f"Found {len(sibling_issues)} open sibling sub-issues for issue #{issue_number}: {sibling_issues}")
                                    dependencies.extend(sibling_issues)
                                    for sibling in sibling_issues:
                                        seen.add(sibling)
                                else:
                                    logger.debug(f"No open sibling sub-issues found for issue #{issue_number}")
                            except Exception as e:
                                logger.warning(f"Failed to get open sub-issues for parent issue #{parent_issue_number}: {e}")
                        else:
                            logger.debug(f"Issue #{issue_number} has no parent issue - treating as no dependencies")
                    except Exception as e:
                        logger.warning(f"Failed to get parent issue for issue #{issue_number}: {e}")
            except Exception as e:
                logger.warning(f"Error during fallback logic for issue #{issue_number}: {e}")

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
        repo = self.get_repository(repo_name)
        for issue_num in dependencies:
            try:
                issue = repo.get_issue(issue_num)
                issue_details = self.get_issue_details(issue)
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
