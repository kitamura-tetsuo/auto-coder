import functools
import json
import logging
import subprocess
import threading
import time
import types
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from ghapi.all import GhApi
from github import GithubException
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient

from ..logger_config import get_logger

logger = get_logger(__name__)

_local_storage = threading.local()


def get_caching_client() -> httpx.Client:
    """
    Returns a thread-local instance of a caching httpx client using hishel.
    This ensures that the SQLite connection (inside SyncSqliteStorage) is only used
    by the thread that created it.
    """
    if not hasattr(_local_storage, "client"):
        # Create a new storage and client for this thread
        storage = SyncSqliteStorage(database_path=".cache/gh_cache.db")
        _local_storage.client = SyncCacheClient(storage=storage)
    return _local_storage.client


def retry_with_backoff(retries=3, backoff_in_seconds=1):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            x = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except (httpx.RequestError, httpx.StreamError, httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                    if x == retries:
                        raise
                    sleep = backoff_in_seconds * 2**x
                    logger.warning(f"Network error in {func.__name__} ({e}), retrying in {sleep}s...")
                    time.sleep(sleep)
                    x += 1

        return wrapper

    return decorator


def get_ghapi_client(token: str) -> GhApi:
    """
    Returns a GhApi instance configured with hishel caching for GET requests.
    """

    class CachedGhApi(GhApi):
        def __call__(self, path: str, verb: str = None, headers: dict = None, route: dict = None, query: dict = None, data=None, timeout=None, decode=True):
            # Use the shared caching client
            client = get_caching_client()

            if verb is None:
                verb = "POST" if data else "GET"

            # Build URL
            if path.startswith("http"):
                url = path
            else:
                url = f"{self.gh_host}{path}"

            # Merge headers
            headers = {**self.headers, **(headers or {})}

            if route:
                import urllib.parse

                for k, v in route.items():
                    # value quoting
                    v_str = urllib.parse.quote(str(v), safe="")
                    path = path.replace(f"{{{k}}}", v_str)
                # Re-evaluate URL after path interpolation
                if not path.startswith("http"):
                    url = f"{self.gh_host}{path}"
                else:
                    url = path

            # Handle data arg for httpx (json vs content)
            json_data = None
            content_data = None
            if data is not None:
                if isinstance(data, dict):
                    json_data = data
                else:
                    content_data = data

            # Use params=query for GET params
            resp = client.request(method=verb, url=url, headers=headers, content=content_data, json=json_data, params=query, follow_redirects=True, timeout=timeout)

            # Raise for status to ensure errors are caught (e.g. 404, 422)
            resp.raise_for_status()

            # Update last headers
            try:
                self.recv_hdrs = dict(resp.headers)
            except:
                pass

            # ghapi expects parsed JSON or None
            if resp.status_code == 204 or (not resp.text and not resp.content):
                return None

            # Use GhApi-like return logic
            content_type = resp.headers.get("content-type", "")

            if decode:
                if "application/zip" in content_type or "application/octet-stream" in content_type:
                    return resp.content

                try:
                    return resp.json()
                except Exception:
                    pass
                return resp.text

            return resp

    return CachedGhApi(token=token)


class GitHubClient:
    """GitHub API client for managing issues and pull requests using GhApi.

    Implements a thread-safe singleton pattern. Use get_instance() to get the singleton.
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
        self.token = token
        self.disable_labels = disable_labels
        self._initialized = True
        self._sub_issue_cache: Dict[Tuple[str, int], List[int]] = {}

        # Memory cache for open issues to avoid re-fetching in loops
        self._open_issues_cache: Optional[List[Dict[str, Any]]] = None
        self._open_issues_cache_time: Optional[datetime] = None
        self._open_issues_cache_repo: Optional[str] = None
        self._open_issues_cache_lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "GitHubClient":
        """Implement thread-safe singleton pattern."""
        return super().__new__(cls)

    @classmethod
    def get_instance(cls, token: Optional[str] = None, disable_labels: bool = False) -> "GitHubClient":
        """Get the singleton instance of GitHubClient."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = cls.__new__(cls)
                    if token is None:
                        # Allow existing instance return without token if already initialized (for tests primarily?)
                        # But for safety, require token on first call.
                        raise ValueError("GitHub token is required on first call to get_instance()")
                    type(instance).__init__(instance, token, disable_labels)
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance."""
        with cls._lock:
            cls._instance = None

    @retry_with_backoff()
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
        client = get_caching_client()
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
            response = client.post(url, headers=headers, json=payload, timeout=30)
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

    def get_repository(self, repo_name: str) -> Any:
        """Get repository object by name (owner/repo).

        DEPRECATED: Returns a dict-like object from GhApi instead of PyGithub Repository.
        Prefer using direct API calls in other methods.
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)
            return api.repos.get(owner, repo)
        except Exception as e:
            logger.error(f"Failed to get repository {repo_name}: {e}")
            raise

    @retry_with_backoff()
    def get_open_issues(self, repo_name: str, limit: Optional[int] = None) -> List[Any]:
        """Get open issues from repository, sorted by creation date (oldest first).

        Returns a list of issue dicts (GhApi AttrDicts).
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # Using list_for_repo to get issues.
            # Note: API returns PRs as issues too, need to filter if desired but standard list usually returns them?
            # get_issues in PyGithub does exclude PRs? No, it often includes them.
            # But the existing implementation filtered them intentionally!

            # GhApi doesn't support "oldest first" directly in list_for_repo arguments?
            # 'sort' param: created, updated, comments. Default: created.
            # 'direction' param: asc, desc. Default: desc.
            # We want asc (oldest first).

            # Pagination: we used to get all? or just some?
            # If limit is None, we might want all?
            # PyGithub's get_issues returns a PaginatedList.

            per_page = 100
            if limit and limit < 100:
                per_page = limit

            issues = api.issues.list_for_repo(owner, repo, state="open", sort="created", direction="asc", per_page=per_page)

            # Filter out PRs if they are present
            # Issues endpoint returns PRs with a "pull_request" key

            # GhApi returns a list-like object (L) or generator if pages?
            # list_for_repo is a simple call, returns one page unless paged() is used.
            # But we might need more pages?
            # For strict compatibility, we should probably fetch more if needed, but for now let's just use what we get or standard paging.
            # To simulate PyGithub's behavior of getting "all" (iterating), we'd need to loop or use paged.
            # Let's assume one page (100) is reasonable for "open" issues in most contexts, or implement simple paging.

            # Actually, `ghapi`'s `paged` utility is cleaner.
            # But let's start with simple fetch to avoid complexity if usage is low.
            # Implementation plan said "replace with GhApi equivalents".

            final_issues = []
            for issue in issues:
                if "pull_request" not in issue:
                    final_issues.append(issue)

            if limit and limit > 0:
                final_issues = final_issues[:limit]

            logger.info(f"Retrieved {len(final_issues)} open issues from {repo_name} (oldest first)")
            return final_issues

        except Exception as e:
            logger.error(f"Failed to get issues from {repo_name}: {e}")
            raise

    @retry_with_backoff()
    def get_open_pull_requests(self, repo_name: str, limit: Optional[int] = None) -> List[Any]:
        """Get open pull requests from repository, sorted by creation date (oldest first).

        Returns a list of PR dicts (GhApi AttrDicts).
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            per_page = 100
            if limit and limit < 100:
                per_page = limit

            prs = api.pulls.list(owner, repo, state="open", sort="created", direction="asc", per_page=per_page)

            pr_list = list(prs)
            if limit and limit > 0:
                pr_list = pr_list[:limit]

            logger.info(f"Retrieved {len(pr_list)} open pull requests from {repo_name} (oldest first)")
            return pr_list

        except Exception as e:
            logger.error(f"Failed to get pull requests from {repo_name}: {e}")
            raise

    def get_pull_request(self, repo_name: str, pr_number: int) -> Optional[Any]:
        """Get a single pull request by number using REST API (cached).

        Returns an object compatible with dot usage (AttrDict).
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)
            return api.pulls.get(owner, repo, pr_number)
        except Exception as e:
            logger.warning(f"Failed to get PR #{pr_number} from {repo_name}: {e}")
            return None

    @retry_with_backoff()
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

    @retry_with_backoff()
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

                # Safe access (dict expected)
                i = issue
                nb = i["number"]

                # Fetch extended details via REST (N+1 calls, but cached via ETag)
                # linked_prs via timeline
                linked_prs_ids = self.get_linked_prs(repo_name, nb)

                # open_sub_issue_numbers via sub_issues endpoint
                # Optimization: Check sub_issues_summary from issue object first
                sub_issues_summary = i.get("sub_issues_summary")
                if sub_issues_summary and sub_issues_summary.get("total", 0) == 0:
                    open_sub_issues_ids = []
                else:
                    open_sub_issues_ids = self.get_open_sub_issues(repo_name, nb)

                # parent_issue via parent_issue_url
                # Optimization: Extract from URL if available
                parent_issue_id = None
                parent_issue_url = i.get("parent_issue_url")
                if parent_issue_url:
                    try:
                        parent_issue_id = int(parent_issue_url.split("/")[-1])
                    except (ValueError, IndexError):
                        logger.warning(f"Failed to parse parent issue ID from URL: {parent_issue_url}")
                        # Fallback if parsing fails? Or just leave as None?
                        # Original logic would try to fetch. Let's stick to parsing or None to avoid N+1.

                issue_data: Dict[str, Any] = {
                    "number": nb,
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
                    # Extended fields populated via REST
                    "linked_prs": linked_prs_ids,
                    "has_linked_prs": bool(linked_prs_ids),
                    "open_sub_issue_numbers": open_sub_issues_ids,
                    "has_open_sub_issues": bool(open_sub_issues_ids),
                    "parent_number": parent_issue_id,
                    "parent_issue_number": parent_issue_id,
                    "linked_pr_numbers": linked_prs_ids,
                }

                all_issues.append(issue_data)

                if len(all_issues) >= limit:
                    break

            logger.info(f"Retrieved {len(all_issues)} open issues from {repo_name} via REST (cached) with extended details")

            # Update cache
            with self._open_issues_cache_lock:
                self._open_issues_cache = all_issues
                self._open_issues_cache_repo = repo_name
                self._open_issues_cache_time = datetime.now()

            return all_issues

        except Exception as e:
            logger.error(f"Failed to get open issues via REST from {repo_name}: {e}")
            raise

    @retry_with_backoff()
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

    def get_issue_details(self, issue: Any) -> Dict[str, Any]:
        """Extract detailed information from an issue.

        Args:
            issue: GhApi issue object (AttrDict) or dict.
        """

        def get(obj, key, default=None):
            return getattr(obj, key, default) if not isinstance(obj, dict) else obj.get(key, default)

        # Handle nested objects which might be AttrDicts or dicts
        user = get(issue, "user")
        labels = get(issue, "labels") or []
        assignees = get(issue, "assignees") or []

        # Parse dates if needed, or pass through strings (GhApi returns strings)
        created_at = get(issue, "created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()

        updated_at = get(issue, "updated_at")
        if hasattr(updated_at, "isoformat"):
            updated_at = updated_at.isoformat()

        return {
            "number": get(issue, "number"),
            "title": get(issue, "title"),
            "body": get(issue, "body") or "",
            "state": get(issue, "state"),
            "labels": [get(lbl, "name") for lbl in labels],
            "assignees": [get(a, "login") for a in assignees],
            "created_at": created_at,
            "updated_at": updated_at,
            "url": get(issue, "html_url"),
            "author": get(user, "login") if user else None,
            "comments_count": get(issue, "comments"),
        }

    def get_pr_details(self, pr: Any) -> Dict[str, Any]:
        """Extract detailed information from a pull request.

        Args:
            pr: GhApi PR object (AttrDict) or dict.
        """

        def get(obj, key, default=None):
            if obj is None:
                return default
            return getattr(obj, key, default) if not isinstance(obj, dict) else obj.get(key, default)

        user = get(pr, "user")
        labels = get(pr, "labels") or []
        assignees = get(pr, "assignees") or []
        head = get(pr, "head")
        base = get(pr, "base")

        created_at = get(pr, "created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()

        updated_at = get(pr, "updated_at")
        if hasattr(updated_at, "isoformat"):
            updated_at = updated_at.isoformat()

        return {
            "number": get(pr, "number"),
            "title": get(pr, "title"),
            "body": get(pr, "body") or "",
            "state": get(pr, "state"),
            "labels": [get(lbl, "name") for lbl in labels],
            "assignees": [get(a, "login") for a in assignees],
            "created_at": created_at,
            "updated_at": updated_at,
            "url": get(pr, "html_url"),
            "author": get(user, "login") if user else None,
            "head_branch": get(head, "ref"),
            "base_branch": get(base, "ref"),
            "mergeable": get(pr, "mergeable"),
            "draft": get(pr, "draft"),
            "comments_count": get(pr, "comments"),
            "review_comments_count": get(pr, "review_comments"),
            "commits_count": get(pr, "commits"),
            "additions": get(pr, "additions"),
            "deletions": get(pr, "deletions"),
            "changed_files": get(pr, "changed_files"),
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
                # Handle both AttrDict and dict
                head_ref = pr.get("head", {}).get("ref") if isinstance(pr, dict) else pr.head.ref

                if head_ref == branch_name:
                    pr_number = pr.get("number") if isinstance(pr, dict) else pr.number
                    logger.info(f"Found PR #{pr_number} with head branch '{branch_name}'")
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
            client = get_caching_client()

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
            response = client.get(url, headers=headers)
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

    def get_pr_closing_issues(self, repo_name: str, pr_number: int) -> List[int]:
        """Get issues that will be closed by this PR via GraphQL.

        Note:
            GitHub REST API (v3) cannot retrieve closing issues directly, so we use GraphQL.

        Args:
            repo_name: Repository name (owner/repo)
            pr_number: Pull request number

        Returns:
            List of issue numbers that this PR closes.
        """
        try:
            owner, repo = repo_name.split("/")
            query = """
            query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  closingIssuesReferences(first: 20) {
                    nodes {
                      number
                    }
                  }
                }
              }
            }
            """

            variables = {"owner": owner, "name": repo, "number": pr_number}

            response = self.graphql_query(query, variables)

            if not response or "data" not in response:
                return []

            pr_data = response.get("data", {}).get("repository", {}).get("pullRequest", {})
            if not pr_data:
                return []

            closing_issues = pr_data.get("closingIssuesReferences", {}).get("nodes", [])
            return [issue["number"] for issue in closing_issues if issue]

        except Exception as e:
            logger.error(f"Failed to get closing issues for PR #{pr_number}: {e}")
            return []

    def has_linked_pr(self, repo_name: str, issue_number: int) -> bool:
        """Check if an issue has a linked pull request.

        First tries REST Timeline API, then falls back to searching PR titles/bodies.

        Returns True if there is an open PR that references this issue.
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # First try REST Timeline (replaces GraphQL)
            linked_prs = self.get_linked_prs(repo_name, issue_number)
            if linked_prs:
                # We need to check if any of these are OPEN.
                for pr_num in linked_prs:
                    try:
                        pr_data = api.pulls.get(owner, repo, pr_num)
                        if pr_data.get("state") == "open":
                            return True
                    except:
                        continue

            # Fallback: Search for PRs that reference this issue in title/body
            # Use already migrated get_open_pull_requests
            try:
                prs = self.get_open_pull_requests(repo_name)
            except Exception:
                prs = []

            issue_ref_patterns = [
                f"#{issue_number}",
                f"issue #{issue_number}",
                f"fixes #{issue_number}",
                f"closes #{issue_number}",
                f"resolves #{issue_number}",
            ]

            for pr in prs:
                # pr is AttrDict or dict
                title = pr.get("title", "")
                body = pr.get("body", "") or ""
                pr_text = f"{title} {body}".lower()

                if any(pattern.lower() in pr_text for pattern in issue_ref_patterns):
                    logger.info(f"Found linked PR #{pr.get('number')} for issue #{issue_number} (via text search)")
                    return True

            return False

        except Exception as e:
            logger.error(f"Failed to check linked PRs for issue #{issue_number}: {e}")
            return False

    def find_closing_pr(self, repo_name: str, issue_number: int) -> Optional[int]:
        """Find a PR that closes the given issue.

        Updated to use REST Timeline.
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # Check timeline for 'connected' events (strongest link)
            timeline = self._get_issue_timeline(repo_name, issue_number)
            for event in timeline:
                if event.get("event") == "connected":
                    source = event.get("source", {})
                    if "issue" in source and "pull_request" in source["issue"]:
                        pr_num = source["issue"]["number"]
                        # Check if open
                        try:
                            # Use api.pulls.get
                            pr_data = api.pulls.get(owner, repo, pr_num)
                            if pr_data.get("state") == "open":
                                logger.info(f"Found closing PR #{pr_num} via timeline 'connected' event")
                                return pr_num
                        except:
                            continue

            # Fallback: Search for PRs that reference this issue in title/body
            # Use already migrated get_open_pull_requests
            try:
                prs = self.get_open_pull_requests(repo_name)
            except Exception:
                prs = []

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
                title = pr.get("title", "")
                body = pr.get("body", "") or ""
                pr_text = f"{title} {body}".lower()

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

    @retry_with_backoff()
    def get_parent_issue_details(self, repo_name: str, issue_number: int) -> Optional[Dict[str, Any]]:
        """Get details of the parent issue if it exists using GitHub REST API.

        This uses the REST API with the sub-issues preview header.
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # Fetch parent issue using dedicated endpoint via GhApi
            # Endpoint: GET /repos/{owner}/{repo}/issues/{issue_number}/parent
            # Note: We use GhApi generic call string method because 'get_parent_issue' might not be in the installed spec.

            try:
                # Use raw path call with GhApi
                parent_issue = api(f"/repos/{owner}/{repo}/issues/{issue_number}/parent", verb="GET", headers={"X-GitHub-Api-Version": "2022-11-28", "Accept": "application/vnd.github+json"})
                if parent_issue:
                    # Check if response is wrapped in 'parent' key
                    if not parent_issue.get("number") and parent_issue.get("parent"):
                        parent_issue = parent_issue.get("parent")

                    if parent_issue.get("number"):
                        # Use .get() method to be safe if parent_issue is a dict or AttrDict
                        logger.info(f"Issue #{issue_number} has parent issue #{parent_issue.get('number')}: {parent_issue.get('title')}")
                        return parent_issue

                    if parent_issue.get("status") == "404":
                        logger.warning(f"Dedicated parent endpoint returned 404 for issue #{issue_number}. Attempting fallback.")
                    else:
                        logger.warning(f"Parent issue response missing number: {parent_issue}")

            except Exception as e:
                # Log but continue to fallback
                logger.warning(f"Dedicated parent endpoint failed: {e}")

        except Exception as e:
            # 404 is common for issues without parents if the endpoint returns 404.
            if "404" in str(e):
                return None
            logger.error(f"Failed to get parent issue for issue #{issue_number}: {e}")
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

    def create_issue(self, repo_name: str, title: str, body: str, labels: Optional[List[str]] = None) -> Any:
        """Create a new issue in the repository.

        Returns:
            GhApi issue object (AttrDict).
        """
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # api.issues.create(owner, repo, title, body=None, ... labels=None)
            issue = api.issues.create(owner, repo, title=title, body=body, labels=labels or [])

            logger.info(f"Created issue #{issue.number}: {title}")

            # Invalidate cache
            with self._open_issues_cache_lock:
                self._open_issues_cache = None

            return issue

        except Exception as e:
            logger.error(f"Failed to create issue in {repo_name}: {e}")
            raise

    def add_comment_to_issue(self, repo_name: str, issue_number: int, comment: str) -> None:
        """Add a comment to an existing issue."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            api.issues.create_comment(owner, repo, issue_number, body=comment)
            logger.info(f"Added comment to issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to add comment to issue #{issue_number}: {e}")
            raise

    def close_issue(self, repo_name: str, issue_number: int, comment: Optional[str] = None) -> None:
        """Close an issue with optional comment."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            if comment:
                api.issues.create_comment(owner, repo, issue_number, body=comment)

            api.issues.update(owner, repo, issue_number, state="closed")
            logger.info(f"Closed issue #{issue_number}")

            # Update cache
            self._update_cached_issue(repo_name, issue_number, state="closed")

        except Exception as e:
            logger.error(f"Failed to close issue #{issue_number}: {e}")
            raise

    def reopen_issue(self, repo_name: str, issue_number: int, comment: Optional[str] = None) -> None:
        """Reopen a closed issue with optional comment."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            if comment:
                api.issues.create_comment(owner, repo, issue_number, body=comment)

            api.issues.update(owner, repo, issue_number, state="open")
            logger.info(f"Reopened issue #{issue_number}")

            # Invalidate cache
            with self._open_issues_cache_lock:
                self._open_issues_cache = None

        except Exception as e:
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
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # api.repos.create_commit_status(owner, repo, sha, state, target_url, description, context)
            api.repos.create_commit_status(owner, repo, sha, state=state, target_url=target_url, description=description, context=context)
            logger.info(f"Created commit status '{state}' for {sha[:8]} (context: {context})")

        except Exception as e:
            logger.error(f"Failed to create commit status for {sha[:8]}: {e}")
            raise

    def close_pr(self, repo_name: str, pr_number: int, comment: Optional[str] = None) -> None:
        """Close a pull request with optional comment."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            if comment:
                # PRs are issues for comments
                api.issues.create_comment(owner, repo, pr_number, body=comment)

            api.pulls.update(owner, repo, pr_number, state="closed")
            logger.info(f"Closed PR #{pr_number}")

        except Exception as e:
            logger.error(f"Failed to close PR #{pr_number}: {e}")
            raise

    def add_comment_to_pr(self, repo_name: str, pr_number: int, comment: str) -> None:
        """Add a comment to a pull request."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # PRs are issues for comments
            api.issues.create_comment(owner, repo, pr_number, body=comment)
            logger.info(f"Added comment to PR #{pr_number}")

        except Exception as e:
            logger.error(f"Failed to add comment to PR #{pr_number}: {e}")
            raise

    def get_pr_comments(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get all comments for a pull request.

        Fetches issue comments (conversation), not code review comments.
        """
        return self.get_issue_comments(repo_name, pr_number)

    def get_issue_comments(self, repo_name: str, issue_number: int) -> List[Dict[str, Any]]:
        """Get all comments for an issue (or PR conversation)."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # api.issues.list_comments(owner, repo, issue_number)
            comments = api.issues.list_comments(owner, repo, issue_number)

            result = []
            for comment in comments:
                user = comment.get("user")
                created_at = comment.get("created_at")
                # GhApi returns strings for dates, pass through
                if hasattr(created_at, "isoformat"):
                    created_at = created_at.isoformat()

                result.append({"body": comment.get("body"), "created_at": created_at, "user": {"login": user.get("login")} if user else None, "id": comment.get("id")})
            return result
        except Exception as e:
            logger.error(f"Failed to get comments for issue/PR #{issue_number}: {e}")
            return []

    def get_pr_diff(self, repo_name: str, pr_number: int) -> str:
        """Get PR diff content (raw text)."""
        try:
            owner, repo = repo_name.split("/")
            # Use low-level client to get raw text, bypassing GhApi's json assumption
            client = get_caching_client()
            url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
            headers = {"Authorization": f"bearer {self.token}", "Accept": "application/vnd.github.v3.diff", "X-GitHub-Api-Version": "2022-11-28"}
            response = client.get(url, headers=headers)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.error(f"Failed to get PR diff for #{pr_number}: {e}")
            return ""

    def get_pr_commits(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get all commits for a pull request."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            commits = api.pulls.list_commits(owner, repo, pr_number)

            result = []
            for c in commits:
                commit_info = c.get("commit", {})
                committer = commit_info.get("committer", {})
                date = committer.get("date")
                if hasattr(date, "isoformat"):
                    date = date.isoformat()

                result.append({"sha": c.get("sha"), "commit": {"message": commit_info.get("message"), "committer": {"date": date, "name": committer.get("name")}}, "author": {"login": c.get("author", {}).get("login")} if c.get("author") else None})
            return result
        except Exception as e:
            logger.error(f"Failed to get commits for PR #{pr_number}: {e}")
            return []

    def get_pr_reviews(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get all reviews for a pull request."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            reviews = api.pulls.list_reviews(owner, repo, pr_number)

            result = []
            for r in reviews:
                submitted_at = r.get("submitted_at")
                if hasattr(submitted_at, "isoformat"):
                    submitted_at = submitted_at.isoformat()

                result.append({"state": r.get("state"), "submitted_at": submitted_at, "user": {"login": r.get("user", {}).get("login")} if r.get("user") else None, "id": r.get("id")})
            return result
        except Exception as e:
            logger.error(f"Failed to get reviews for PR #{pr_number}: {e}")
            return []

    def get_open_sub_issues(self, repo_name: str, issue_number: int) -> List[int]:
        """Get list of open sub-issues using GitHub REST API.

        Uses the sub-issues endpoint: /repos/{owner}/{repo}/issues/{issue_number}/sub_issues
        """
        open_sub_issues = []

        # Check cache first
        cache_key = (repo_name, issue_number)
        if cache_key in self._sub_issue_cache:
            return self._sub_issue_cache[cache_key]

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

            client = get_caching_client()

            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/sub_issues"
            headers = {"Authorization": f"bearer {self.token}", "Accept": "application/vnd.github.v3+json", "X-GitHub-Api-Version": "2022-11-28"}  # As hinted by user docs

            response = client.get(url, headers=headers)

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
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # Using add_labels endpoint which appends to existing labels
            # works for both Issues and PRs
            api.issues.add_labels(owner, repo, issue_number, labels=labels)

            logger.info(f"Added labels {labels} to {item_type} #{issue_number}")

            # Update cache if issue
            if item_type != "pr":
                # We can't easily know current labels without fetching, but we can try to update blind or invalidate?
                # Invalidating cache is safer.
                with self._open_issues_cache_lock:
                    self._open_issues_cache = None

        except Exception as e:
            logger.error(f"Failed to add labels to {item_type} #{issue_number}: {e}")
            raise

    def try_add_labels(self, repo_name: str, issue_number: int, labels: List[str], item_type: str = "issue") -> bool:
        """Add labels to an existing issue or PR.

        Returns:
            True if labels were successfully added, False if they already exist
        """
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping add labels {labels} to {item_type} #{issue_number}")
            return True

        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            # Use basic get issue to check current labels
            issue_data = api.issues.get(owner, repo, issue_number)
            current_labels = [lbl["name"] for lbl in issue_data.get("labels", [])]

            existing_labels = [lbl for lbl in labels if lbl in current_labels]
            if existing_labels:
                logger.info(f"{item_type} #{issue_number} already has label(s) {existing_labels} - skipping")
                return False

            api.issues.add_labels(owner, repo, issue_number, labels=labels)
            logger.info(f"Added labels {labels} to {item_type} #{issue_number}")

            # Invalidate cache
            if item_type != "pr":
                with self._open_issues_cache_lock:
                    self._open_issues_cache = None

            return True

        except Exception as e:
            logger.error(f"Failed to add labels to {item_type} #{issue_number}: {e}")
            raise

    def remove_labels(self, repo_name: str, item_number: int, labels: List[str], item_type: str = "issue") -> None:
        """Remove labels from an existing issue or PR."""
        if self.disable_labels:
            logger.debug(f"Labels disabled - skipping remove labels {labels} from {item_type} #{item_number}")
            return

        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            for label in labels:
                try:
                    api.issues.remove_label(owner, repo, item_number, name=label)
                except Exception as e:
                    # Ignore 404 (label not present)
                    logger.debug(f"Failed to remove label {label} (maybe not present?): {e}")

            logger.info(f"Removed labels {labels} from {item_type} #{item_number}")

            # Invalidate cache
            if item_type != "pr":
                with self._open_issues_cache_lock:
                    self._open_issues_cache = None

        except Exception as e:
            logger.error(f"Failed to remove labels from {item_type} #{item_number}: {e}")
            raise

    def has_label(self, repo_name: str, issue_number: int, label: str, item_type: str = "issue") -> bool:
        """Check if an issue or PR has a specific label."""
        if self.disable_labels:
            return False

        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)

            issue_data = api.issues.get(owner, repo, issue_number)
            current_labels = [lbl["name"] for lbl in issue_data.get("labels", [])]
            return label in current_labels

        except Exception as e:
            logger.error(f"Failed to check labels for {item_type} #{issue_number}: {e}")
            raise

    def search_issues(self, query: str, sort: str = "updated", order: str = "desc") -> List[Any]:
        """Search issues using GitHub Search API.

        Returns:
            List of issue dicts.
        """
        try:
            logger.info(f"Searching issues with query: '{query}'")
            api = get_ghapi_client(self.token)
            # api.search.issues_and_pull_requests(q, sort, order, ...)
            # returns { 'total_count': ..., 'incomplete_results': ..., 'items': [...] }
            result = api.search.issues_and_pull_requests(q=query, sort=sort, order=order)
            return result.get("items", [])

        except Exception as e:
            logger.error(f"Failed to search issues with query '{query}': {e}")
            return []

    def _search_issues_by_title(self, repo_name: str, search_title: str) -> Optional[int]:
        """Search for an open issue by title using fuzzy matching."""
        try:
            issues = self.get_open_issues(repo_name)
            search_title_lower = search_title.lower()

            def get_title(issue):
                return getattr(issue, "title", None) or issue.get("title", "")

            def get_number(issue):
                return getattr(issue, "number", None) or issue.get("number")

            # First try exact match (case-insensitive)
            for issue in issues:
                t = get_title(issue)
                if t.lower() == search_title_lower:
                    logger.debug(f"Found exact match for title '{search_title}': issue #{get_number(issue)}")
                    return get_number(issue)

            # Then try partial match
            for issue in issues:
                issue_title_lower = get_title(issue).lower()

                min_length = min(len(search_title_lower), len(issue_title_lower))
                threshold = max(5, min_length * 0.5)

                if len(search_title_lower) >= threshold and search_title_lower in issue_title_lower:
                    logger.debug(f"Found partial match for title '{search_title}': issue #{get_number(issue)}")
                    return get_number(issue)
                elif len(issue_title_lower) >= threshold and issue_title_lower in search_title_lower:
                    logger.debug(f"Found partial match for title '{search_title}': issue #{get_number(issue)}")
                    return get_number(issue)

            logger.debug(f"No match found for title '{search_title}'")
            return None

        except Exception as e:
            logger.warning(f"Failed to search for issue by title '{search_title}': {e}")
            return None

    def check_issue_dependencies_resolved(self, repo_name: str, dependencies: List[int]) -> List[int]:
        """Check which of the given issue dependencies are resolved (closed)."""
        if not dependencies:
            return []

        unresolved = []
        for issue_num in dependencies:
            try:
                # Use get_issue which is already updated to use GhApi
                issue = self.get_issue(repo_name, issue_num)
                if not issue:
                    # Can't find it, assume unresolved
                    unresolved.append(issue_num)
                    continue

                issue_details = self.get_issue_details(issue)
                state = issue_details.get("state", "open")
                if state == "open":
                    unresolved.append(issue_num)
                    logger.debug(f"Dependency issue #{issue_num} is still open")
                else:
                    logger.debug(f"Dependency issue #{issue_num} is closed (resolved)")
            except Exception as e:
                logger.warning(f"Failed to check dependency issue #{issue_num}: {e}")
                unresolved.append(issue_num)

        return unresolved
