import functools
import json
import logging
import re
import subprocess
import threading
import time
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

import httpx
from ghapi.all import GhApi
from github import GithubException
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient

from ..logger_config import get_logger

logger = get_logger(__name__)


@dataclass
class ReviewThreadComment:
    """One comment within a PR review thread, in chronological order."""

    database_id: Optional[int] = None
    body: str = ""
    author_login: str = ""
    author_id: Optional[int] = None


@dataclass
class ReviewThread:
    id: str = ""
    is_resolved: bool = False
    is_outdated: bool = False
    comments: List["ReviewThreadComment"] = field(default_factory=list)
    comments_truncated: bool = False


# Safety bound for paginated comment listings (100 comments per page).
COMMENTS_MAX_PAGES = 50

# A bound prevents a malformed or cyclic Link header from looping forever. In
# strict mode, reaching the bound raises so lifecycle reconciliation fails
# closed rather than treating a partial timeline as authoritative.
TIMELINE_MAX_PAGES = 100

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


def parse_parent_issue_number(body: Optional[str], current_issue_number: Optional[int] = None) -> Optional[int]:
    """Parse 'Parent-Issue: #<number>' from issue body text.

    Args:
        body: Issue body markdown/text
        current_issue_number: Optional issue number of the current issue being parsed.
            If specified and matches the parsed parent number, it is ignored (an issue
            cannot be its own parent).

    Returns:
        Optional[int]: The parsed parent issue number if found, or None.
    """
    if not body or not isinstance(body, str):
        return None

    match = re.search(r"(?i)\bparent[-_ ]issue:\s*#?(\d+)\b", body)
    if match:
        try:
            parent_num = int(match.group(1))
            if current_issue_number is not None and parent_num == current_issue_number:
                return None
            return parent_num
        except ValueError:
            return None
    return None


import inspect


def _resolve_coroutine_synchronously(coro: Any, name: str) -> Any:
    """Resolve a coroutine returned in a synchronous context (e.g. an AsyncMock from tests).

    The coroutine is stepped exactly once. An exception raised inside the coroutine
    propagates unchanged; the coroutine is never awaited a second time, so the real
    error is not masked by "cannot reuse already awaited coroutine".
    """
    origin = getattr(coro, "cr_code", None)
    origin_desc = f"{coro!r} defined at {origin.co_filename}:{origin.co_firstlineno}" if origin else repr(coro)

    if inspect.getcoroutinestate(coro) != inspect.CORO_CREATED:
        # The same coroutine object was handed out for a second call (e.g. a Mock
        # with a fixed coroutine return_value). Its result is unrecoverable here.
        raise RuntimeError(f"{name}() returned an already-awaited coroutine ({origin_desc}); each call must return a fresh coroutine")

    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    # The coroutine suspended: it performs real async I/O and cannot be completed
    # in this synchronous context.
    coro.close()
    raise RuntimeError(f"{name}() returned a coroutine that requires a running event loop ({origin_desc}); it cannot be resolved synchronously")


class SafeGhApiProxy:
    """A proxy wrapper that intercepts GhApi calls and safely unwraps AsyncMock coroutines
    if they are accidentally returned in a synchronous context (e.g. from tests)."""

    def __init__(self, obj):
        self._obj = obj

    def __call__(self, *args, **kwargs):
        res = self._obj(*args, **kwargs)
        if inspect.iscoroutine(res):
            return _resolve_coroutine_synchronously(res, type(self._obj).__name__)
        return res

    def __getattr__(self, name):
        # Prevent infinite recursion for internal attributes
        if name in ("_obj",):
            raise AttributeError()

        attr = getattr(self._obj, name)

        # Don't wrap basic types or internal methods
        if type(attr) in (int, str, bool, list, dict, type(None)) or name.startswith("__"):
            return attr

        if callable(attr):

            def wrapper(*args, **kwargs):
                res = attr(*args, **kwargs)
                if inspect.iscoroutine(res):
                    return _resolve_coroutine_synchronously(res, name)
                return res

            return wrapper

        return SafeGhApiProxy(attr)


def get_ghapi_client(token: str) -> GhApi:
    """
    Returns a GhApi instance configured with hishel caching for GET requests.
    """

    class CachedGhApi(GhApi):
        def __call__(self, path: str, verb: Optional[str] = None, headers: Optional[Dict[str, Any]] = None, route: Optional[Dict[str, Any]] = None, query: Optional[Dict[str, Any]] = None, data=None, timeout=None, decode=True):
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
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    logger.error("GitHub API authentication failed (401). Please update your GITHUB_TOKEN. (https://github.com/settings/tokens)")
                    raise
                else:
                    raise

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

    # Note: GhApi.__init__ has no client parameter; CachedGhApi.__call__ obtains the
    # thread-local caching client itself via get_caching_client().
    #
    # ghapi must stay pinned below 2.0 (see pyproject.toml): ghapi>=2.0 rebuilds
    # GhApi on top of the `fastspec` package and defaults to async transports
    # (GhApi(sync=False), httpx.AsyncClient). Every call site in this codebase
    # invokes GhApi methods synchronously and never awaits them, so on ghapi>=2.0
    # attribute-style calls (api.<group>.<op>()) return unresolved coroutines
    # whose async httpx internals need a real running event loop -- driving them
    # synchronously raises sniffio.AsyncLibraryNotFoundError ("unknown async
    # library, or not in async context"). ghapi<2.0's GhApi.__call__ is the single
    # dispatch point all verb calls funnel through, which is what CachedGhApi
    # overrides below to inject the caching client.
    # SafeGhApiProxy dynamically forwards attribute access/calls to the wrapped
    # GhApi instance rather than subclassing it, so it isn't a real GhApi for
    # mypy; cast to preserve the GhApi-shaped return type for callers.
    return cast(GhApi, SafeGhApiProxy(CachedGhApi(token=token)))


def resolve_authoritative_item_type(github_client: Any, repo_name: str, item_number: int) -> str:
    """Establish an issue-like target's authoritative GitHub type, failing closed.

    This is the single implementation of the Issue-vs-PR dispatch guard. Every path
    that can start Issue implementation work (the shared candidate-dispatch boundary,
    explicit target_type="issue" requests, retry/resumption paths such as the stale
    Jules session fallback, and any other internal enqueue path) must call this before
    performing an Issue lifecycle side effect. A caller-supplied type is not
    authoritative on its own: GitHub's Issues API represents pull requests as
    issue-like objects. Only a genuinely cache-bypassing lookup
    (``get_item_type_strict``) counts as authoritative; a client that cannot perform
    one has not established the type, so this fails closed (raises) rather than
    falling back to a cached ``get_issue()`` response that could be stale.
    """
    strict_type_getter = getattr(github_client, "get_item_type_strict", None)
    if strict_type_getter is None:
        raise ValueError(f"GitHub client does not implement an authoritative, cache-bypassing item-type lookup for {repo_name}#{item_number}")

    item_type = strict_type_getter(repo_name, item_number)
    if item_type not in ("issue", "pr"):
        raise ValueError(f"GitHub item type lookup was ambiguous for {repo_name}#{item_number}")
    return item_type


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

    def clear_open_issues_cache(self) -> None:
        """Clear the open issues memory cache."""
        with self._open_issues_cache_lock:
            self._open_issues_cache = None
            self._open_issues_cache_repo = None
            self._open_issues_cache_time = None

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
            client = get_caching_client()
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            per_page = min(limit, 100) if limit else 100
            url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&sort=created&direction=asc&per_page={per_page}"

            resp = client.request("GET", url, headers=headers)
            resp.raise_for_status()
            pr_list = resp.json()

            if limit:
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
            client = get_caching_client()
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
            resp = client.request("GET", url, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Failed to get PR #{pr_number} from {repo_name}: {e}")
            return None

    @retry_with_backoff()
    def get_pull_request_head_sha_strict(self, repo_name: str, pr_number: int) -> str:
        """Fetch the current PR head SHA directly, bypassing every cache.

        Resolution-time safety checks must observe GitHub's live PR head and
        must fail closed on lookup or schema errors.  In particular, neither
        ``get_caching_client()`` nor ``get_ghapi_client()`` is suitable here,
        because both cache GET responses.
        """
        owner, repo = repo_name.split("/")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
        response = httpx.get(url, headers=headers, follow_redirects=False, timeout=30)
        response.raise_for_status()
        payload = response.json()
        head = payload.get("head") if isinstance(payload, dict) else None
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if not isinstance(head_sha, str) or not head_sha:
            raise RuntimeError(f"GitHub did not return a current head SHA for PR #{pr_number} in {repo_name}")
        return head_sha

    @retry_with_backoff()
    def get_open_prs_json(self, repo_name: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get open pull requests from repository using REST API (cached).

        Matches the output format expected by automation engine.
        Uses N+1 calls to fetch full details but leverages hishel cache to avoid rate limits
        on subsequent runs.
        """
        try:
            owner, repo = repo_name.split("/")
            client = get_caching_client()
            headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            per_page = min(limit, 100) if limit else 100
            list_url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page={per_page}"

            list_resp = client.request("GET", list_url, headers=headers)
            list_resp.raise_for_status()
            prs_summary = list_resp.json()

            all_prs: List[Dict[str, Any]] = []

            for pr_summary in prs_summary:
                try:
                    pr_num = pr_summary["number"] if isinstance(pr_summary, dict) else pr_summary.number
                    detail_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}"
                    detail_resp = client.request("GET", detail_url, headers=headers)
                    detail_resp.raise_for_status()
                    pr_details = detail_resp.json()
                except Exception as e:
                    logger.warning(f"Failed to fetch details for PR #{pr_summary.get('number', 'unknown') if isinstance(pr_summary, dict) else getattr(pr_summary, 'number', 'unknown')}: {e}")
                    continue

                d = pr_details

                pr_data: Dict[str, Any] = {
                    "number": d.get("number"),
                    "title": d.get("title"),
                    "node_id": d.get("node_id"),
                    "body": d.get("body") or "",
                    "state": d.get("state", "").lower(),
                    "url": d.get("html_url"),
                    "created_at": d.get("created_at"),
                    "updated_at": d.get("updated_at"),
                    "draft": d.get("draft"),
                    "mergeable": d.get("mergeable"),
                    "head_branch": d.get("head", {}).get("ref"),
                    "head": {"ref": d.get("head", {}).get("ref"), "sha": d.get("head", {}).get("sha")},
                    "base_branch": d.get("base", {}).get("ref"),
                    "author": d.get("user", {}).get("login") if d.get("user") else None,
                    "author_id": d.get("user", {}).get("id") if d.get("user") else None,
                    "assignees": [a.get("login") for a in d.get("assignees", [])],
                    "labels": [lbl.get("name") for lbl in d.get("labels", [])],
                    "comments_count": d.get("comments", 0) + d.get("review_comments", 0),
                    "commits_count": d.get("commits", 0),
                    "additions": d.get("additions", 0),
                    "deletions": d.get("deletions", 0),
                    "changed_files": d.get("changed_files", 0),
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

            # Filter out Pull Requests (which are returned in issues list by REST API)
            raw_open_issues = [issue for issue in issues_summary if "pull_request" not in issue]

            # Pass 1: Scan all open issues immediately for Parent-Issue relationships
            # This ensures parents with lower issue numbers (e.g. #10) know about their
            # sub-issues (e.g. #20, #30) before individual issue details are constructed.
            issue_parent_map: Dict[int, int] = {}
            parent_to_open_children: Dict[int, List[int]] = {}

            for i in raw_open_issues:
                nb = i.get("number")
                if not isinstance(nb, int):
                    continue

                parent_issue_id = None
                parent_issue_url = i.get("parent_issue_url")
                if parent_issue_url:
                    try:
                        parsed_id = int(parent_issue_url.split("/")[-1])
                        if parsed_id != nb:
                            parent_issue_id = parsed_id
                    except (ValueError, IndexError):
                        logger.warning(f"Failed to parse parent issue ID from URL: {parent_issue_url}")

                # If no native parent found, check body for Parent-Issue fallback metadata
                if parent_issue_id is None:
                    body_text = i.get("body") or ""
                    fallback_parent_id = parse_parent_issue_number(body_text, current_issue_number=nb)
                    if fallback_parent_id is not None and fallback_parent_id != nb:
                        try:
                            self.add_sub_issue(repo_name, fallback_parent_id, nb, sub_issue_id=i.get("id"))
                        except Exception as e:
                            logger.warning(f"Failed to promote fallback sub-issue #{nb} to parent #{fallback_parent_id}: {e}")
                        parent_issue_id = fallback_parent_id

                if parent_issue_id is not None and parent_issue_id != nb:
                    issue_parent_map[nb] = parent_issue_id
                    if parent_issue_id not in parent_to_open_children:
                        parent_to_open_children[parent_issue_id] = []
                    if nb not in parent_to_open_children[parent_issue_id]:
                        parent_to_open_children[parent_issue_id].append(nb)

            all_issues: List[Dict[str, Any]] = []

            # Pass 2: Construct extended issue data with pre-scanned parent/sub-issue information
            for i in raw_open_issues:
                nb = i["number"]

                # Fetch extended details via REST (N+1 calls, but cached via ETag)
                # linked_prs via timeline
                linked_prs_ids = self.get_linked_prs(repo_name, nb)

                # open_sub_issue_numbers via sub_issues endpoint + pre-scanned fallback sub-issues
                sub_issues_summary = i.get("sub_issues_summary")
                known_open_children = parent_to_open_children.get(nb, [])
                if sub_issues_summary and sub_issues_summary.get("total", 0) == 0 and not known_open_children:
                    open_sub_issues_ids = []
                else:
                    open_sub_issues_ids = self.get_open_sub_issues(repo_name, nb)

                # Merge pre-scanned children into open_sub_issues_ids
                if known_open_children:
                    open_sub_issues_ids = sorted(list(set(open_sub_issues_ids + known_open_children)))

                # Ensure open_sub_issues_ids never contains the issue itself
                open_sub_issues_ids = [sub_id for sub_id in open_sub_issues_ids if sub_id != nb]

                parent_issue_id = issue_parent_map.get(nb)

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
                    "author": i["user"]["login"] if i.get("user") else None,
                    "author_id": i["user"].get("id") if i.get("user") else None,
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

            # Synchronize parent <-> sub-issue relationships for all open issues
            issue_by_number = {item["number"]: item for item in all_issues if isinstance(item.get("number"), int)}
            for item in all_issues:
                p_num = item.get("parent_issue_number")
                if p_num is not None and p_num in issue_by_number and p_num != item["number"]:
                    parent_item = issue_by_number[p_num]
                    curr_open_sub_issues = list(parent_item.get("open_sub_issue_numbers") or [])
                    if item["number"] not in curr_open_sub_issues and item["number"] != p_num:
                        updated_sub_issues = sorted(list(set(curr_open_sub_issues + [item["number"]])))
                        updated_sub_issues = [s for s in updated_sub_issues if s != p_num]
                        parent_item["open_sub_issue_numbers"] = updated_sub_issues
                        parent_item["has_open_sub_issues"] = bool(updated_sub_issues)

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

    @retry_with_backoff()
    def get_issue_strict(self, repo_name: str, issue_number: int) -> Any:
        """Get an issue while preserving REST lookup failures for merge gates."""
        owner, repo = repo_name.split("/")
        api = get_ghapi_client(self.token)
        return api.issues.get(owner, repo, issue_number)

    @retry_with_backoff()
    def get_item_type_strict(self, repo_name: str, item_number: int) -> str:
        """Return GitHub's authoritative type ("issue" or "pr") for an issue-like item.

        This deliberately bypasses the shared hishel-backed caching client
        (``get_caching_client()`` / ``get_ghapi_client()``): it is a safety gate for
        starting Issue implementation work, so it must reflect GitHub's current state
        rather than a possibly-stale cached response. GitHub's Issues REST endpoint
        returns both issues and pull requests; a pull request is distinguished by the
        presence of the ``pull_request`` field.
        """
        owner, repo = repo_name.split("/")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{item_number}"
        with httpx.Client() as client:
            response = client.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            item = response.json()

        if not isinstance(item, dict) or item.get("number") != item_number:
            raise ValueError(f"GitHub returned an ambiguous item for {repo_name}#{item_number}")
        return "pr" if "pull_request" in item else "issue"

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
            "labels": [get(label, "name") for label in labels],
            "assignees": [get(a, "login") for a in assignees],
            "created_at": created_at,
            "updated_at": updated_at,
            "url": get(issue, "html_url"),
            "author": get(user, "login") if user else None,
            "author_id": get(user, "id") if user else None,
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
            "labels": [get(label, "name") for label in labels],
            "assignees": [get(a, "login") for a in assignees],
            "created_at": created_at,
            "updated_at": updated_at,
            "url": get(pr, "html_url"),
            "author": get(user, "login") if user else None,
            "author_id": get(user, "id") if user else None,
            "user": (
                {
                    "login": get(user, "login"),
                    "id": get(user, "id"),
                }
                if user
                else {}
            ),
            "head": {
                "ref": get(head, "ref"),
                "sha": get(head, "sha"),
            },
            "base": {
                "ref": get(base, "ref"),
                "sha": get(base, "sha"),
            },
            "head_branch": get(head, "ref"),
            "head_sha": get(head, "sha"),
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

    def _get_issue_timeline(
        self,
        repo_name: str,
        issue_number: int,
        raise_on_error: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get timeline for an issue using GitHub REST API.

        Endpoint: /repos/{owner}/{repo}/issues/{issue_number}/timeline
        """
        try:
            owner, repo = repo_name.split("/")
            client = get_caching_client()

            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/timeline?per_page=100"
            headers = {
                "Authorization": f"bearer {self.token}",
                "Accept": "application/vnd.github.v3+json",
                # "X-GitHub-Api-Version": "2022-11-28" # Standard API
            }

            events: List[Dict[str, Any]] = []
            visited_urls = set()
            for _page in range(TIMELINE_MAX_PAGES):
                if url in visited_urls:
                    raise RuntimeError(f"Issue #{issue_number} timeline pagination repeated URL: {url}")
                visited_urls.add(url)

                response = client.get(url, headers=headers)
                response.raise_for_status()
                page_events = response.json()
                if not isinstance(page_events, list) or not all(isinstance(event, dict) for event in page_events):
                    raise ValueError(f"Issue #{issue_number} timeline returned invalid page data")
                events.extend(page_events)

                links = response.links
                next_link = links.get("next") if isinstance(links, Mapping) else None
                next_url = next_link.get("url") if isinstance(next_link, Mapping) else None
                if not isinstance(next_url, str) or not next_url:
                    return events
                url = next_url

            raise RuntimeError(f"Issue #{issue_number} timeline exceeded {TIMELINE_MAX_PAGES} pages")

        except Exception as e:
            logger.warning(f"Failed to get timeline for issue #{issue_number}: {e}")
            if raise_on_error:
                raise
            return []

    def get_linked_prs(self, repo_name: str, issue_number: int, strict: bool = False) -> List[int]:
        """Get PRs linked to this issue via REST Timeline.

        Replaces get_linked_prs_via_graphql.
        Look for 'connected' (closing) or 'cross-referenced' (mention) events.
        """
        try:
            timeline = self._get_issue_timeline(repo_name, issue_number, raise_on_error=strict)
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
            if strict:
                raise
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

    def get_pr_review_threads(self, repo_name: str, pr_number: int) -> List[ReviewThread]:
        """Get review threads for a pull request via GraphQL.

        Args:
            repo_name: Repository name (owner/repo)
            pr_number: Pull request number

        Returns:
            List of ReviewThread dataclass instances.
        """
        try:
            return self._get_pr_review_threads(repo_name, pr_number)
        except Exception as e:
            logger.error(f"Failed to get review threads for PR #{pr_number}: {e}")
            return []

    def get_pr_review_threads_strict(self, repo_name: str, pr_number: int) -> List[ReviewThread]:
        """Get review threads while preserving lookup failures for merge gates."""
        return self._get_pr_review_threads(repo_name, pr_number)

    def get_authenticated_user_login(self) -> str:
        """Return the login proven by this client's GitHub credential.

        Marker-based safety state may only trust comments authored through the
        same credential Auto-Coder uses to write those markers.  Keep this a
        strict lookup: an absent or malformed identity must not widen the set
        of trusted comment authors.
        """
        api = get_ghapi_client(self.token)
        user = api.users.get_authenticated()
        login = user.get("login") if isinstance(user, dict) else getattr(user, "login", None)
        if not isinstance(login, str) or not login:
            raise RuntimeError("GitHub did not return the authenticated user's login")
        return login

    def _get_pr_review_threads(self, repo_name: str, pr_number: int) -> List[ReviewThread]:
        """Fetch all review-thread pages and let callers decide error handling."""
        owner, repo = repo_name.split("/")
        query = """
            query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  reviewThreads(first: 100, after: $cursor) {
                    pageInfo {
                      hasNextPage
                      endCursor
                    }
                    nodes {
                      id
                      isResolved
                      isOutdated
                      comments(first: 50) {
                        pageInfo {
                          hasNextPage
                        }
                        nodes {
                          databaseId
                          body
                          author {
                            login
                            ... on Bot {
                              databaseId
                            }
                            ... on EnterpriseUserAccount {
                              user {
                                databaseId
                              }
                            }
                            ... on Mannequin {
                              databaseId
                            }
                            ... on Organization {
                              databaseId
                            }
                            ... on User {
                              databaseId
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
        """

        threads: List[ReviewThread] = []
        cursor: Optional[str] = None

        while True:
            variables: Dict[str, Any] = {"owner": owner, "name": repo, "number": pr_number, "cursor": cursor}
            response = self.graphql_query(query, variables)

            if not response or "data" not in response:
                raise RuntimeError(f"Review-thread response for PR #{pr_number} did not contain data")

            pr_data = response.get("data", {}).get("repository", {}).get("pullRequest")
            if not pr_data:
                raise RuntimeError(f"Review-thread response for PR #{pr_number} did not contain the pull request")

            review_threads_data = pr_data.get("reviewThreads")
            if review_threads_data is None:
                raise RuntimeError(f"Review-thread response for PR #{pr_number} did not contain reviewThreads")

            nodes = review_threads_data.get("nodes") or []
            for node in nodes:
                if node:
                    thread_id = node.get("id")
                    is_resolved = node.get("isResolved")
                    if not isinstance(thread_id, str) or not thread_id:
                        raise RuntimeError(f"Review-thread response for PR #{pr_number} contained a thread without a valid ID")
                    if not isinstance(is_resolved, bool):
                        raise RuntimeError(f"Review-thread response for PR #{pr_number} contained thread {thread_id} without an explicit resolved state")
                    comments_data = node.get("comments") or {}
                    comment_nodes = comments_data.get("nodes") or []
                    comments: List[ReviewThreadComment] = []
                    for comment_node in comment_nodes:
                        if not comment_node:
                            continue
                        author = comment_node.get("author") or {}
                        raw_author_id = author.get("databaseId")
                        if raw_author_id is None:
                            raw_author_id = (author.get("user") or {}).get("databaseId")
                        author_id = raw_author_id if isinstance(raw_author_id, int) and not isinstance(raw_author_id, bool) and raw_author_id > 0 else None
                        comments.append(
                            ReviewThreadComment(
                                database_id=comment_node.get("databaseId"),
                                body=str(comment_node.get("body", "") or ""),
                                author_login=str(author.get("login") or ""),
                                author_id=author_id,
                            )
                        )
                    comments_truncated = bool((comments_data.get("pageInfo") or {}).get("hasNextPage"))
                    threads.append(
                        ReviewThread(
                            id=thread_id,
                            is_resolved=is_resolved,
                            is_outdated=bool(node.get("isOutdated", False)),
                            comments=comments,
                            comments_truncated=comments_truncated,
                        )
                    )

            page_info = review_threads_data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                raise RuntimeError(f"Review-thread response for PR #{pr_number} omitted the next-page cursor")

        return threads

    def has_unresolved_review_threads(self, repo_name: str, pr_number: int) -> bool:
        """Check if a pull request has any unresolved review threads.

        Args:
            repo_name: Repository name (owner/repo)
            pr_number: Pull request number

        Returns:
            True if at least one review thread is unresolved, False otherwise.
        """
        threads = self.get_pr_review_threads(repo_name, pr_number)
        if not threads:
            return False
        return any(not thread.is_resolved for thread in threads)

    def reply_to_review_thread(self, repo_name: str, pr_number: int, root_comment_database_id: int, body: str) -> None:
        """Post a reply into an existing PR review-comment thread.

        Raises on any failure so a caller cannot mistake a failed post for a
        recorded explanation (fail-closed for automatic thread resolution).
        """
        owner, repo = repo_name.split("/")
        api = get_ghapi_client(self.token)
        api.pulls.create_reply_for_review_comment(owner, repo, pr_number, root_comment_database_id, body=body)

    def resolve_review_thread(self, thread_id: str) -> None:
        """Resolve a GitHub PR review thread via the ``resolveReviewThread`` mutation.

        Raises on any failure, including a response that does not confirm
        ``isResolved``, so a caller cannot treat an unresolved thread as
        resolved (fail-closed for automatic thread resolution).
        """
        mutation = """
            mutation($threadId: ID!) {
              resolveReviewThread(input: {threadId: $threadId}) {
                thread {
                  id
                  isResolved
                }
              }
            }
        """
        response = self.graphql_query(mutation, {"threadId": thread_id})
        thread = (((response or {}).get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {}
        if thread.get("id") != thread_id or thread.get("isResolved") is not True:
            raise RuntimeError(f"GitHub did not confirm review thread {thread_id} as resolved")

    def unresolve_review_thread(self, thread_id: str) -> None:
        """Revert a GitHub PR review thread to unresolved via ``unresolveReviewThread``.

        Used to undo a resolution performed against a PR head that turned out
        to be stale by the time the mutation completed. Raises on any failure,
        including a response that does not confirm the thread is no longer
        resolved, so a caller cannot mistake a failed revert for success.
        """
        mutation = """
            mutation($threadId: ID!) {
              unresolveReviewThread(input: {threadId: $threadId}) {
                thread {
                  id
                  isResolved
                }
              }
            }
        """
        response = self.graphql_query(mutation, {"threadId": thread_id})
        thread = (((response or {}).get("data") or {}).get("unresolveReviewThread") or {}).get("thread") or {}
        # Fail closed on an ambiguous confirmation: a missing/empty payload or
        # a payload missing "isResolved" must not be mistaken for success just
        # because it happens to be falsy. Only an explicit match on this exact
        # thread ID with isResolved literally False counts as confirmed.
        if thread.get("id") != thread_id or thread.get("isResolved") is not False:
            raise RuntimeError(f"GitHub did not confirm review thread {thread_id} as unresolved")

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

            # Fallback: check Parent-Issue metadata in the issue body
            try:
                issue_obj = self.get_issue(repo_name, issue_number)
                if issue_obj:
                    body = getattr(issue_obj, "body", None) or (issue_obj.get("body") if isinstance(issue_obj, dict) else "") or ""
                    fallback_parent_number = parse_parent_issue_number(body, current_issue_number=issue_number)
                    if fallback_parent_number is not None and fallback_parent_number != issue_number:
                        # Attempt conversion to native sub-issue
                        sub_issue_id = getattr(issue_obj, "id", None) or (issue_obj.get("id") if isinstance(issue_obj, dict) else None)
                        self.add_sub_issue(repo_name, fallback_parent_number, issue_number, sub_issue_id=sub_issue_id)

                        # Retrieve parent issue details
                        parent_obj = self.get_issue(repo_name, fallback_parent_number)
                        if parent_obj:
                            parent_data = dict(parent_obj) if hasattr(parent_obj, "__iter__") and not isinstance(parent_obj, (str, bytes)) and isinstance(parent_obj, dict) else {}
                            if not parent_data:
                                parent_data = {
                                    "number": getattr(parent_obj, "number", fallback_parent_number),
                                    "title": getattr(parent_obj, "title", ""),
                                    "state": getattr(parent_obj, "state", ""),
                                    "body": getattr(parent_obj, "body", "") or "",
                                }
                            logger.info(f"Issue #{issue_number} has fallback parent issue #{fallback_parent_number}")
                            return parent_data
            except Exception as e:
                logger.warning(f"Fallback parent issue check failed for #{issue_number}: {e}")

        except Exception as e:
            # 404 is common for issues without parents if the endpoint returns 404.
            if "404" in str(e):
                return None
            logger.error(f"Failed to get parent issue for issue #{issue_number}: {e}")
            return None

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
            logger.debug(f"Added comment to issue #{issue_number}")

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

    def get_pr_comments_strict(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get PR conversation comments while preserving REST lookup failures."""
        return self._get_issue_comments(repo_name, pr_number, fresh=True)

    def get_issue_comments(self, repo_name: str, issue_number: int) -> List[Dict[str, Any]]:
        """Get all comments for an issue (or PR conversation).

        Every page is fetched: the REST API returns at most `per_page` comments per
        request, so a single call would silently truncate long conversations and make
        callers (e.g. attempt tracking) read a stale state.
        """
        try:
            return self._get_issue_comments(repo_name, issue_number)
        except Exception as e:
            logger.error(f"Failed to get comments for issue/PR #{issue_number}: {e}")
            return []

    def _get_issue_comments(self, repo_name: str, issue_number: int, fresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch every comment page and let callers decide how to handle errors."""
        owner, repo = repo_name.split("/")
        api = get_ghapi_client(self.token)

        per_page = 100
        result = []
        page = 1
        while page <= COMMENTS_MAX_PAGES:
            if fresh:
                comments = api(
                    f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
                    verb="GET",
                    headers={"Cache-Control": "no-cache"},
                    query={"per_page": per_page, "page": page},
                )
            else:
                comments = api.issues.list_comments(owner, repo, issue_number, per_page=per_page, page=page)
            if not comments:
                break

            for comment in comments:
                user = comment.get("user")
                created_at = comment.get("created_at")
                if hasattr(created_at, "isoformat"):
                    created_at = created_at.isoformat()

                result.append({"body": comment.get("body"), "created_at": created_at, "user": {"login": user.get("login")} if user else None, "id": comment.get("id")})

            if len(comments) < per_page:
                break
            page += 1
        else:
            logger.warning(f"Stopped fetching comments for issue/PR #{issue_number} after {COMMENTS_MAX_PAGES} pages")

        return result

    def update_comment_for_issue(self, repo_name: str, comment_id: int, body: str) -> None:
        """Update an existing comment."""
        try:
            owner, repo = repo_name.split("/")
            api = get_ghapi_client(self.token)
            api.issues.update_comment(owner, repo, comment_id, body=body)
            logger.info(f"Updated comment {comment_id} in {repo_name}")
        except Exception as e:
            logger.error(f"Failed to update comment {comment_id} in {repo_name}: {e}")
            raise

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

    @retry_with_backoff()
    def get_pr_changed_file_count(self, repo_name: str, pr_number: int) -> int:
        """Fetch the authoritative changed-file count independently of raw diff."""
        owner, repo = repo_name.split("/")
        api = get_ghapi_client(self.token)
        pr_data = api.pulls.get(owner, repo, pr_number)
        changed_files = pr_data.get("changed_files")
        if isinstance(changed_files, bool) or not isinstance(changed_files, int) or changed_files < 0:
            raise ValueError(f"PR #{pr_number} response did not contain a valid changed_files count")
        return changed_files

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
            return self._get_pr_reviews(repo_name, pr_number)
        except Exception as e:
            logger.error(f"Failed to get reviews for PR #{pr_number}: {e}")
            return []

    def get_pr_reviews_strict(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        """Get PR reviews while preserving REST lookup failures.

        Used for merge-critical persisted state lookups (e.g. adversarial
        validation) that must fail closed instead of treating an API error
        as "no reviews".
        """
        return self._get_pr_reviews(repo_name, pr_number)

    def _get_pr_reviews(self, repo_name: str, pr_number: int) -> List[Dict[str, Any]]:
        owner, repo = repo_name.split("/")
        api = get_ghapi_client(self.token)

        per_page = 100
        result: List[Dict[str, Any]] = []
        page = 1
        while page <= COMMENTS_MAX_PAGES:
            reviews = api.pulls.list_reviews(owner, repo, pr_number, per_page=per_page, page=page)
            if not reviews:
                break

            for r in reviews:
                submitted_at = r.get("submitted_at")
                if hasattr(submitted_at, "isoformat"):
                    submitted_at = submitted_at.isoformat()

                user = r.get("user")
                result.append(
                    {
                        "id": r.get("id"),
                        "state": r.get("state"),
                        "body": r.get("body"),
                        "submitted_at": submitted_at,
                        "user": {"login": user.get("login")} if user else None,
                    }
                )

            if len(reviews) < per_page:
                break
            page += 1
        else:
            logger.warning(f"Stopped fetching reviews for PR #{pr_number} after {COMMENTS_MAX_PAGES} pages")

        return result

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
            open_sub_issues = [i["number"] for i in sub_issues_data if i.get("state") == "open" and i.get("number") != issue_number]

            # Merge fallback sub-issues from memory cache if present
            with self._open_issues_cache_lock:
                if self._open_issues_cache is not None and self._open_issues_cache_repo == repo_name:
                    for cached_issue in self._open_issues_cache:
                        if cached_issue.get("parent_issue_number") == issue_number and cached_issue.get("state") == "open":
                            c_num = cached_issue.get("number")
                            if isinstance(c_num, int) and c_num not in open_sub_issues and c_num != issue_number:
                                open_sub_issues.append(c_num)

            open_sub_issues.sort()

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
            all_sub = [i["number"] for i in sub_issues_data if i.get("number") != issue_number]
            with self._open_issues_cache_lock:
                if self._open_issues_cache is not None and self._open_issues_cache_repo == repo_name:
                    for cached_issue in self._open_issues_cache:
                        if cached_issue.get("parent_issue_number") == issue_number:
                            c_num = cached_issue.get("number")
                            if isinstance(c_num, int) and c_num not in all_sub and c_num != issue_number:
                                all_sub.append(c_num)
            all_sub.sort()
            return all_sub
        except Exception as e:
            logger.error(f"Failed to get all sub-issues for issue #{issue_number}: {e}")
            return []

    def add_sub_issue(
        self,
        repo_name: str,
        parent_issue_number: int,
        sub_issue_number: int,
        sub_issue_id: Optional[int] = None,
    ) -> bool:
        """Register an issue as a native GitHub sub-issue of the parent issue.

        Args:
            repo_name: Repository name ('owner/repo')
            parent_issue_number: Issue number of the parent issue
            sub_issue_number: Issue number of the sub-issue to link
            sub_issue_id: Optional database ID of the sub-issue (if already known)

        Returns:
            bool: True if successfully registered or already registered, False otherwise.
        """
        try:
            owner, repo = repo_name.split("/")

            # Check if already a native sub-issue (idempotency)
            existing_sub_issues = self.get_all_sub_issues(repo_name, parent_issue_number)
            if sub_issue_number in existing_sub_issues:
                logger.debug(f"Issue #{sub_issue_number} is already a sub-issue of #{parent_issue_number}")
                return True

            if sub_issue_id is None:
                sub_issue = self.get_issue(repo_name, sub_issue_number)
                if not sub_issue:
                    logger.warning(f"Could not find issue #{sub_issue_number} to link as sub-issue")
                    return False
                sub_issue_id = getattr(sub_issue, "id", None) or (sub_issue.get("id") if isinstance(sub_issue, dict) else None)
                if not sub_issue_id:
                    logger.warning(f"Issue #{sub_issue_number} missing database ID for sub-issue linking")
                    return False

            client = get_caching_client()
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{parent_issue_number}/sub_issues"
            headers = {
                "Authorization": f"bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            payload = {"sub_issue_id": int(sub_issue_id)}

            response = client.post(url, headers=headers, json=payload)
            if response.status_code in (200, 201):
                logger.info(f"Successfully linked issue #{sub_issue_number} as sub-issue of #{parent_issue_number}")
                self.clear_sub_issue_cache()
                return True

            if response.status_code in (409, 422) and "already" in response.text.lower():
                logger.info(f"Issue #{sub_issue_number} is already linked to parent #{parent_issue_number}")
                self.clear_sub_issue_cache()
                return True

            logger.warning(f"Failed to add issue #{sub_issue_number} as sub-issue of #{parent_issue_number}: " f"status={response.status_code}, response={response.text}")
            return False
        except Exception as e:
            logger.warning(f"Failed to add issue #{sub_issue_number} as sub-issue of #{parent_issue_number}: {e}")
            return False

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

            logger.debug(f"Added labels {labels} to {item_type} #{issue_number}")

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
            current_labels = [label["name"] for label in issue_data.get("labels", [])]

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
