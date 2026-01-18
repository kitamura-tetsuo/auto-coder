"""
Utility functions for GitHub Actions processing.

This module provides functions to check GitHub Actions status,
fetch logs, and process historical runs when necessary.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import rapidfuzz
    from rapidfuzz import fuzz
except ImportError:
    rapidfuzz = None
    fuzz = None

from auto_coder.progress_decorators import progress_stage

from ..automation_config import AutomationConfig
from ..gh_logger import get_gh_logger
from ..logger_config import get_logger
from ..utils import CommandExecutor, log_action
from .gh_cache import GitHubClient, get_ghapi_client
from .github_cache import get_github_cache


def _get_repo_name_from_git(cwd: Optional[str] = None) -> Optional[str]:
    """Get repository name (owner/repo) from git config."""
    try:
        # Get remote URL
        result = cmd.run_command(["git", "remote", "get-url", "origin"], cwd=cwd)
        if not result.success:
            return None
        url = result.stdout.strip()
        # Parse owner/repo from URL
        # e.g. https://github.com/owner/repo.git or git@github.com:owner/repo.git
        match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return None
    except Exception:
        return None


cmd = CommandExecutor()

logger = get_logger(__name__)


@dataclass
class GitHubActionsCheck:
    """GitHub Actions check information."""

    name: str = ""
    conclusion: str = ""
    details_url: str = ""


@dataclass
class GitHubActionsStatusResult:
    """Return type for GitHub Actions status checking functions."""

    success: bool = True
    ids: List[int] = field(default_factory=list)
    in_progress: bool = False
    error: Optional[str] = None


@dataclass
class DetailedChecksResult:
    """Return type for detailed GitHub Actions checks from history."""

    success: bool = True
    total_checks: int = 0
    failed_checks: List[Dict[str, Any]] = field(default_factory=list)
    all_checks: List[Dict[str, Any]] = field(default_factory=list)
    has_in_progress: bool = False
    run_ids: List[int] = field(default_factory=list)


def parse_git_commit_history_for_actions(
    max_depth: int = 10,
    cwd: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Parse git commit history and identify commits that triggered GitHub Actions.

    This function retrieves recent commit history using git log --oneline and checks
    each commit to see if it has associated GitHub Actions runs. It skips commits
    that don't trigger Actions (e.g., documentation-only changes) and returns a list
    of commits that have Action logs available.

    Args:
        max_depth: Maximum number of commits to search (default: 10)
        cwd: Optional working directory for git command

    Returns:
        List of dictionaries, each containing:
        - 'sha': Full commit SHA
        - 'sha_short': Short commit SHA (first 8 chars)
        - 'message': Commit message
        - 'action_runs': List of GitHub Actions runs for this commit
          Each run dict contains: 'run_id', 'url', 'status', 'conclusion', 'created_at'
        - 'has_logs': Boolean indicating if Action logs are available

    Example:
        >>> commits = parse_git_commit_history_for_actions(max_depth=5)
        >>> for commit in commits:
        ...     print(f"Commit {commit['sha_short']}: {commit['message']}")
        ...     if commit['has_logs']:
        ...         print(f"  Has {len(commit['action_runs'])} Action run(s)")
    """
    commits_with_actions = []

    try:
        logger.info(f"Parsing git commit history to identify commits with GitHub Actions (depth: {max_depth})")

        # Get recent commit history using git log --oneline
        log_result = cmd.run_command(
            ["git", "log", "--oneline", f"-n {max_depth}"],
            cwd=cwd,
            timeout=30,
        )

        if not log_result.success:
            logger.error(f"Failed to get git log: {log_result.stderr}")
            return []

        # Parse commit log output
        # Format: "abc1234 Commit message"
        commit_lines = log_result.stdout.strip().split("\n")
        if not commit_lines or not commit_lines[0]:
            logger.info("No commits found in repository")
            return []

        logger.info(f"Found {len(commit_lines)} commit(s) to check")

        for line in commit_lines:
            line = line.strip()
            if not line:
                continue

            # Parse commit SHA and message
            parts = line.split(" ", 1)
            if len(parts) < 2:
                logger.warning(f"Skipping malformed commit line: {line}")
                continue

            commit_sha = parts[0]
            commit_message = parts[1]

            logger.debug(f"Checking commit {commit_sha[:8]}: {commit_message[:50]}...")

            try:
                # Check if this commit triggered GitHub Actions
                action_runs = _check_commit_for_github_actions(commit_sha, cwd=cwd, timeout=60)

                if action_runs:
                    # Commit has Action runs
                    commit_info = {
                        "sha": commit_sha,
                        "sha_short": commit_sha[:8],
                        "message": commit_message,
                        "action_runs": action_runs,
                        "has_logs": len(action_runs) > 0,
                    }
                    commits_with_actions.append(commit_info)
                    logger.info(f"✓ Commit {commit_sha[:8]} has {len(action_runs)} Action run(s)")
                else:
                    logger.debug(f"✗ Commit {commit_sha[:8]} has no GitHub Actions")

            except Exception as e:
                logger.warning(f"Error checking Actions for commit {commit_sha[:8]}: {e}")
                continue

        if commits_with_actions:
            logger.info(f"Found {len(commits_with_actions)} commit(s) with GitHub Actions out of {len(commit_lines)} checked")
        else:
            logger.info("No commits with GitHub Actions found in the specified depth")

        return commits_with_actions

    except Exception as e:
        logger.error(f"Error parsing git commit history: {e}")
        return []


def _check_commit_for_github_actions(commit_sha: str, cwd: Optional[str] = None, timeout: int = 60) -> List[Dict[str, Any]]:
    """Check if a specific commit triggered GitHub Actions.

    Args:
        commit_sha: Full or partial commit SHA to check
        cwd: Optional working directory for git command
        timeout: Timeout for GitHub Actions API calls

    Returns:
        List of Action run dictionaries with run metadata
    """
    action_runs = []

    try:
        # Get repository name
        repo_full_name = _get_repo_name_from_git(cwd)
        if not repo_full_name:
            logger.debug(f"Could not determine repository name for commit {commit_sha[:8]}")
            return []

        owner, repo = repo_full_name.split("/")

        # Get GhApi client
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
        except Exception as e:
            logger.debug(f"Failed to get GitHub client/token: {e}")
            return []

        # List workflow runs (cached)
        # equivalent to: gh run list --json ...
        # API: api.actions.list_workflow_runs_for_repo(owner, repo, ...)
        # We fetch latest 50 runs to match the limit in original code
        try:
            runs_resp = api.actions.list_workflow_runs_for_repo(owner, repo, per_page=50)
            runs = runs_resp.get("workflow_runs", [])
        except Exception as e:
            logger.debug(f"GhApi call failed: {e}")
            return []

        if not runs:
            return []

        # Narrow down to the target commit and PR event if available
        try:
            logger.debug(f"Filtering runs for commit {commit_sha[:7]}")
            # API uses snake_case keys
            logger.debug(f"Available runs: {[r.get('head_sha', '') for r in runs]}")
            runs = [r for r in runs if str(r.get("head_sha", "")).startswith(commit_sha[:7])]
            pr_runs = [r for r in runs if r.get("event") == "pull_request"]
            if pr_runs:
                runs = pr_runs
            logger.debug(f"After filtering for commit {commit_sha[:7]}: {len(runs)} runs remain")
            logger.debug(f"Filtered runs: {[r.get('head_sha', '') for r in runs]}")
        except Exception:
            pass

        # Convert to our format
        for run in runs:
            # API returns fields in snake_case, but gh CLI often uses camelCase in JSON
            # API mapping:
            # databaseId -> id
            # url -> html_url (usually)
            # displayTitle -> display_title
            # headBranch -> head_branch
            # headSha -> head_sha

            action_runs.append(
                {
                    "run_id": run.get("id"),
                    "url": run.get("html_url"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "created_at": run.get("created_at"),
                    "display_title": run.get("display_title"),
                    "head_branch": run.get("head_branch"),
                    "head_sha": (run.get("head_sha", "")[:8] if run.get("head_sha") else ""),
                }
            )

        logger.debug(f"Found {len(action_runs)} Action run(s) for commit {commit_sha[:8]}")
        return action_runs

    except Exception as e:
        logger.debug(f"Error checking Actions for commit {commit_sha[:8]}: {e}")
        return []


@progress_stage("Checking GitHub Actions")
def _check_github_actions_status(repo_name: str, pr_data: Dict[str, Any], config: AutomationConfig) -> GitHubActionsStatusResult:
    """Check GitHub Actions status for a PR.

    Verifies that the checks correspond to the current PR HEAD SHA.
    If checks are for an older commit, returns in_progress=True to wait for new checks.
    """
    pr_number = pr_data["number"]
    # Get the current HEAD SHA of the PR
    current_head_sha = pr_data.get("head", {}).get("sha")

    try:
        logger.debug(f"pr_data={pr_data}, head_sha={current_head_sha}")
        if not current_head_sha:
            logger.warning(f"No head SHA found for PR #{pr_number}, falling back to historical checks")
            return _check_github_actions_status_from_history(repo_name, pr_data, config)

        # Check cache first
        # Caching removed to ensure fresh status is always retrieved
        cache = get_github_cache()
        cache_key = f"gh_actions_status:{repo_name}:{pr_number}:{current_head_sha}"
        cached_result = cache.get(cache_key)
        if cached_result:
            logger.debug(f"Using cached GitHub Actions status for {repo_name} PR #{pr_number} ({current_head_sha[:8]})")
            return cached_result

        # Use gh API to get check runs for the commit
        # gh pr checks does not support --json, so we use the API directly
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            # API: api.checks.list_for_ref(owner, repo, ref)
            res = api.checks.list_for_ref(owner, repo, ref=current_head_sha, per_page=100)
            checks_data = res.get("check_runs", [])
        except Exception as e:
            api_error = str(e)
            log_action(f"Failed to get check runs for {current_head_sha[:8]}", False, api_error)
            logger.info(f"API call failed for #{pr_number}, attempting historical fallback...")
            fallback_result = _check_github_actions_status_from_history(repo_name, pr_data, config)
            if fallback_result.error:
                fallback_result.error = f"Primary check failed: {api_error}\nFallback check also failed: {fallback_result.error}"
            return fallback_result

        if not checks_data:
            # No checks found, checks might not have started yet
            # For a new commit, we expect at least some checks if CI is configured.
            # If 0 checks, it's ambiguous: either no CI, or CI hasn't started.
            # We'll treat it as success if we can't find anything, but log it.
            # Alternatively, if we expect checks, we should wait.
            # For now, preserving similar logic: if empty, return success (line 312 of original)
            gh_status_result = GitHubActionsStatusResult(
                success=True,
                ids=[],
                in_progress=False,
            )
            cache.set(cache_key, gh_status_result)
            return gh_status_result

        # Map API response matching_checks to the expected format
        # API fields: name, status, conclusion, html_url (as url), head_sha
        # content is already filtered by SHA by the API call nature
        matching_checks = []
        for check in checks_data:
            c = check.copy()
            # gh pr checks returned browser URL in 'url' field
            # API returns API URL in 'url' and browser URL in 'html_url'
            c["url"] = check.get("html_url", "")
            matching_checks.append(c)

        # Deduplicate checks by name, keeping only the latest run
        # Sort by completed_at (descending), then created_at (descending)
        # We want the most recent run for each name
        matching_checks.sort(key=lambda x: (x.get("completed_at") or x.get("created_at") or "", x.get("id") or 0), reverse=True)

        unique_checks = {}
        for check in matching_checks:
            name = check.get("name")
            if name and name not in unique_checks:
                unique_checks[name] = check

        # Use the deduplicated values
        matching_checks = list(unique_checks.values())

        checks = []
        failed_checks = []
        all_passed = True
        has_in_progress = False
        run_ids = []

        for check in matching_checks:
            name = check.get("name", "")
            status = (check.get("status") or "").lower()
            conclusion = (check.get("conclusion") or "").lower()
            url = check.get("url", "")

            # Extract run ID from URL
            if url and "/actions/runs/" in url:
                import re

                match = re.search(r"/actions/runs/(\d+)", url)
                if match:
                    run_ids.append(int(match.group(1)))

            if conclusion in ["success", "pass"]:
                checks.append({"name": name, "state": "completed", "conclusion": "success"})
            elif conclusion in ["failure", "failed", "error", "timed_out", "cancelled"]:
                all_passed = False
                checks.append({"name": name, "state": "completed", "conclusion": "failure"})
                failed_checks.append({"name": name, "conclusion": "failure", "details_url": url})
            elif status in ["in_progress", "queued", "pending", "waiting"]:
                has_in_progress = True
                all_passed = False
                checks.append({"name": name, "state": "pending", "conclusion": "pending"})
                failed_checks.append({"name": name, "conclusion": "pending", "details_url": url})
            elif conclusion in ["skipped", "neutral"]:
                checks.append({"name": name, "state": "completed", "conclusion": conclusion})
            else:
                # Unknown status
                all_passed = False
                checks.append({"name": name, "state": "completed", "conclusion": conclusion or status})
                failed_checks.append({"name": name, "conclusion": conclusion or status, "details_url": url})

        # Remove duplicates
        run_ids = list(set(run_ids))

        gh_status_result = GitHubActionsStatusResult(
            success=all_passed,
            ids=run_ids,
            in_progress=has_in_progress,
        )

        # Cache the result
        cache.set(cache_key, gh_status_result)

        return gh_status_result

    except Exception as e:
        logger.error(f"Error checking GitHub Actions for PR #{pr_number}: {e}")
        # Try historical search on exception
        logger.info(f"Exception during PR checks for #{pr_number}, attempting historical fallback...")
        return _check_github_actions_status_from_history(repo_name, pr_data, config)


# --- Common helpers for historical GitHub Actions processing ---


def _filter_runs_for_pr(runs: List[Dict[str, Any]], branch_name: str) -> List[Dict[str, Any]]:
    """Sort and filter runs by branch and prefer pull_request events when available.
    - Sorts by createdAt (newest first)
    - If branch_name is provided, keeps only runs matching headBranch when possible
    - If event field exists, prefers runs with event == "pull_request"
    """
    if not runs:
        return []
    # Sort runs by creation time (newest first)
    try:
        runs = sorted(runs, key=lambda r: r.get("created_at", r.get("createdAt", "")), reverse=True)
    except Exception as e:
        logger.debug(f"Could not sort runs by creation time: {e}")
    # Filter by branch when available
    if branch_name:
        filtered_runs = [r for r in runs if r.get("head_branch", r.get("headBranch")) == branch_name]
        if filtered_runs:
            runs = filtered_runs
            logger.info(f"Filtered to {len(runs)} runs for branch '{branch_name}'")
        else:
            logger.info(f"No runs found for branch '{branch_name}', using all recent runs")
    # Prefer pull_request event runs when available
    runs_with_event = [r for r in runs if "event" in r]
    pull_request_runs = [r for r in runs if r.get("event") == "pull_request"]
    if runs_with_event and pull_request_runs:
        runs = pull_request_runs
        logger.info(f"Filtered to {len(runs)} runs with event 'pull_request'")
    return runs


def _get_jobs_for_run_filtered_by_pr_number(run_id: int, pr_number: Optional[int], repo_name: str) -> List[Dict[str, Any]]:
    """Return jobs for a run within the specified repository. If pullRequests are available
    in the command output and pr_number is given, only return jobs if the run references
    that PR number. Returns empty list on failure.
    """
    try:
        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # Check PR number if provided
        if isinstance(pr_number, int):
            # API: api.actions.get_workflow_run(owner, repo, run_id)
            run_details = api.actions.get_workflow_run(owner, repo, run_id)
            pr_refs = run_details.get("pull_requests", [])

            if pr_refs:
                pr_numbers: List[int] = []
                for pr_ref in pr_refs:
                    # API returns objects with 'number'
                    num = pr_ref.get("number")
                    if isinstance(num, int):
                        pr_numbers.append(num)

                if pr_numbers and pr_number not in pr_numbers:
                    logger.debug(f"Run {run_id} does not reference PR #{pr_number}; skipping")
                    return []
            # Note: If no PR refs, we assume it's okay or maybe not?
            # Original code: if pr_refs is list and pr_refs (if truthy).
            # If pr_refs is empty, logic falls through to returning jobs.
            # So if API returns empty list, we continue.

        # Get jobs
        # API: api.actions.list_jobs_for_workflow_run(owner, repo, run_id)
        jobs_res = api.actions.list_jobs_for_workflow_run(owner, repo, run_id)
        return jobs_res.get("jobs", [])
    except json.JSONDecodeError as e:
        logger.debug(f"Failed to parse jobs JSON for run {run_id}: {e}")
        return []
    except Exception as e:
        logger.debug(f"Error processing run {run_id}: {e}")
        return []


def _check_github_actions_status_from_history(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
) -> GitHubActionsStatusResult:
    """Check GitHub Actions status from recent runs when current PR checks are not available.

    This function gets all PR commits/oid and matches with gh run list headSha to find runs.
    Then selects the latest commits' runs and determines overall success status.

    Args:
        repo_name: Repository name in format 'owner/repo'
        pr_data: PR data dictionary
        config: AutomationConfig instance

    Returns:
        GitHubActionsStatusResult with the most recent available status from historical runs
    """
    try:
        pr_number = pr_data["number"]
        head_branch = pr_data.get("head_branch") or pr_data.get("head", {}).get("ref")
        logger.info(f"PR number: {pr_number}, head_branch: {head_branch}")
        assert pr_number
        assert head_branch

        logger.info(f"Checking historical GitHub Actions status for PR #{pr_number} on branch '{head_branch}'")

        # Check cache first (using head_branch as part of key since we might not have exact SHA yet,
        # but ideally we should use SHA if possible. However, this function is a fallback when we might lack info.
        # Let's use a composite key including head_branch.)
        cache = get_github_cache()
        # Note: Historical check is more expensive, but relying on cache causes stale status issues.
        cache_key = f"gh_actions_history:{repo_name}:{pr_number}:{head_branch}"
        cached_result = cache.get(cache_key)
        if cached_result:
            logger.info(f"Using cached historical GitHub Actions status for {repo_name} PR #{pr_number}")
            return cached_result

        # 1. Get all PR commits/oid
        # 1. Get all PR commits/oid
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            # API: api.pulls.list_commits(owner, repo, pull_number)
            commits = api.pulls.list_commits(owner, repo, pull_number=pr_number)

            if not commits:
                logger.warning(f"No commits found in PR #{pr_number}")
                return GitHubActionsStatusResult(
                    success=True,
                    ids=[],
                    in_progress=False,
                )

            # Get all commit SHAs (sha in REST API, oid in GraphQL)
            commit_shas = [commit.get("sha", commit.get("oid", "")) for commit in commits if commit.get("sha") or commit.get("oid")]
            if not commit_shas:
                logger.warning(f"No sha/oid found in commits for PR #{pr_number}")
                return GitHubActionsStatusResult(
                    success=True,
                    ids=[],
                    in_progress=False,
                )

            logger.info(f"Found {len(commit_shas)} commits in PR, latest: {commit_shas[-1][:8]}")

        except Exception as e:
            error_message = f"Failed to get PR #{pr_number} commits: {e}"
            logger.warning(error_message)
            return GitHubActionsStatusResult(
                success=False,
                ids=[],
                in_progress=False,
                error=error_message,
            )

        # 2. Get GitHub Actions runs for the head branch
        # 2. Get GitHub Actions runs for the head branch
        try:
            # API: api.actions.list_workflow_runs_for_repo(owner, repo, branch=head_branch)
            runs_resp = api.actions.list_workflow_runs_for_repo(owner, repo, branch=head_branch, per_page=20)
            runs = runs_resp.get("workflow_runs", [])
        except Exception as e:
            error_message = f"Failed to get runs for branch {head_branch}: {e}"
            logger.warning(error_message)
            return GitHubActionsStatusResult(
                success=False,
                ids=[],
                in_progress=False,
                error=error_message,
            )

        # 3. Find runs with matching headSha for any of the PR commits
        logger.info(f"Looking for runs matching commits: {commit_shas}")
        logger.info(f"Available runs: {runs}")
        matching_runs = []
        for run in runs:
            run_head_sha = str(run.get("head_sha", run.get("headSha", "")))
            for sha in commit_shas:
                if run_head_sha.startswith(sha[:7]):
                    matching_runs.append(run)
                    logger.info(f"Match found: run headSha '{run_head_sha}' matches commit '{sha[:7]}'")
                    break
        logger.info(f"Matching runs found: {len(matching_runs)}")

        if not matching_runs:
            logger.info(f"No runs found matching any PR commit for branch {head_branch}")
            # If there are no runs at all, checks may not have started yet
            # Return in_progress=True to wait for checks to start
            if not runs:
                logger.info(f"No workflow runs found on branch {head_branch} - checks may not have started yet")
                return GitHubActionsStatusResult(
                    success=False,
                    ids=[],
                    in_progress=True,
                )
            # If there are runs but none match, the PR may be old/stale
            # In this case, assume success (legacy behavior)
            return GitHubActionsStatusResult(
                success=True,
                ids=[],
                in_progress=False,
            )

        # 4. Find the latest commit that has matching runs
        runs_with_commit_sha = []
        for run in matching_runs:
            run_sha = str(run.get("head_sha", run.get("headSha", "")))
            for i, commit_sha in enumerate(commit_shas):
                if run_sha.startswith(commit_sha[:7]):
                    runs_with_commit_sha.append((run, i, commit_sha))
                    break

        # Sort by commit index (latest first) then by run creation time
        runs_with_commit_sha.sort(key=lambda x: (x[1], x[0].get("created_at", x[0].get("createdAt", ""))), reverse=True)

        # Get the latest commit index that has matching runs
        latest_commit_index = max(idx for _, idx, _ in runs_with_commit_sha)
        latest_commit_sha = commit_shas[latest_commit_index]

        # Get all runs that match the latest commit
        latest_commit_runs = [run for run, idx, sha in runs_with_commit_sha if idx == latest_commit_index]

        logger.info(f"Found {len(latest_commit_runs)} runs matching latest commit {latest_commit_sha[:8]}")

        # 5. Determine overall status and count potential checks (without detailed checks)
        has_in_progress = any((run.get("status") or "").lower() in ["in_progress", "queued", "pending"] for run in latest_commit_runs)

        any_failed = any((run.get("conclusion") or "").lower() in ["failure", "failed", "error"] for run in latest_commit_runs)

        # Estimate total checks count based on runs found
        total_checks = len(latest_commit_runs)

        # Success if no failures and no in-progress runs
        final_success = not any_failed and not has_in_progress

        logger.info(f"Historical GitHub Actions check completed: " f"total_checks={total_checks}, failed_checks=0 (lazy loaded), " f"success={final_success}, has_in_progress={has_in_progress}")

        # Extract run IDs from matching runs
        run_ids = []
        for run in latest_commit_runs:
            # API uses 'id', gh uses 'databaseId'
            run_id = run.get("id") or run.get("databaseId")
            if run_id:
                run_ids.append(int(run_id))

        result = GitHubActionsStatusResult(
            success=final_success,
            ids=run_ids,
            in_progress=has_in_progress,
        )

        # Cache the result
        # cache.set(cache_key, result)

        return result

    except Exception as e:
        error_message = f"Error during historical GitHub Actions status check: {e}"
        logger.error(error_message)
        return GitHubActionsStatusResult(
            success=False,
            ids=[],
            in_progress=False,
            error=error_message,
        )


def get_detailed_checks_from_history(
    status_result: GitHubActionsStatusResult,
    repo_name: str,
) -> DetailedChecksResult:
    """Get detailed checks information from run IDs using API.

    This function takes a GitHubActionsStatusResult with run IDs and fetches
    detailed information using API to get jobs and their statuses.

    Args:
        status_result: GitHubActionsStatusResult containing run IDs
        repo_name: Repository name in format 'owner/repo'

    Returns:
        DetailedChecksResult with comprehensive checks information including:
        - success: Overall success status
        - total_checks: Total number of checks found
        - failed_checks: List of failed checks with details
        - all_checks: List of all checks with their status
        - has_in_progress: Whether any checks are still in progress
        - run_ids: List of processed run IDs
    """
    logger.info("Getting detailed checks from run IDs using API")

    try:
        # Get detailed checks from the provided run IDs
        all_checks = []
        all_failed_checks = []
        has_in_progress = False
        any_failed = False
        processed_run_ids = []

        for run_id in status_result.ids:
            logger.info(f"Processing run {run_id}")
            processed_run_ids.append(run_id)

            # Get jobs for this run using GhApi
            try:
                token = GitHubClient.get_instance().token
                api = get_ghapi_client(token)
                owner, repo = repo_name.split("/")

                # API: api.actions.list_jobs_for_workflow_run(owner, repo, run_id)
                jobs_res = api.actions.list_jobs_for_workflow_run(owner, repo, run_id)
                jobs = jobs_res.get("jobs", [])

                for job in jobs:
                    job_name = job.get("name", "")
                    job_conclusion = job.get("conclusion", "")
                    job_status = job.get("status", "")
                    # API 'id' is databaseId
                    job_id = job.get("id")

                    if not job_name:
                        continue

                    conclusion = (job_conclusion or "").lower()
                    status = (job_status or "").lower()

                    check_info = {
                        "name": f"{job_name} (run {run_id})",
                        "job_name": job_name,  # Store raw name for deduplication
                        "conclusion": conclusion if conclusion else status,
                        "details_url": (f"https://github.com/{repo_name}/actions/runs/{run_id}/job/{job_id}" if job_id else ""),
                        "run_id": run_id,
                        "job_id": job_id,
                        "status": status,
                        "completed_at": job.get("completed_at"),
                        "started_at": job.get("started_at"),
                    }

                    all_checks.append(check_info)

            except Exception as e:
                logger.warning(f"Failed to get jobs via GhApi for run {run_id}: {e}")
                continue

        # Deduplicate checks by job_name, keeping only the latest run
        # Sort by completed_at (descending), then started_at (descending), then job_id (descending)
        # We want the most recent run for each job name
        all_checks.sort(key=lambda x: (x.get("completed_at") or "", x.get("started_at") or "", x.get("job_id") or 0), reverse=True)

        unique_checks = {}
        for check in all_checks:
            name = check.get("job_name")
            if name and name not in unique_checks:
                unique_checks[name] = check

        # Use deduplicated checks
        final_checks = list(unique_checks.values())

        # Re-evaluate status based on deduplicated checks
        all_failed_checks = []
        has_in_progress = False
        any_failed = False

        for check in final_checks:
            conclusion = check.get("conclusion", "")
            status = check.get("status", "")

            if conclusion in ["failure", "failed", "error", "cancelled"]:
                any_failed = True
                all_failed_checks.append(check)
            elif status in ["in_progress", "queued", "pending"]:
                has_in_progress = True
            elif not conclusion and status in [
                "failure",
                "failed",
                "error",
                "cancelled",
            ]:
                any_failed = True
                all_failed_checks.append(check)

        # Determine overall success
        # Success if no failures and no in-progress checks (and we found checks)
        final_success = not any_failed and not has_in_progress

        return DetailedChecksResult(
            success=final_success,
            total_checks=len(final_checks),
            failed_checks=all_failed_checks,
            all_checks=final_checks,
            has_in_progress=has_in_progress,
            run_ids=processed_run_ids,
        )

    except Exception as e:
        logger.error(f"Error getting detailed checks from history: {e}")
        return DetailedChecksResult(
            success=False,
            total_checks=0,
            failed_checks=[],
            all_checks=[],
            has_in_progress=False,
            run_ids=[],
        )


def trigger_workflow_dispatch(repo_name: str, workflow_id: str, ref: str) -> bool:
    """Trigger a GitHub Actions workflow via workflow_dispatch.

    Args:
        repo_name: Repository name in format 'owner/repo'
        workflow_id: Workflow ID or filename (e.g., 'ci.yml')
        ref: Git reference (branch or tag) to run the workflow on

    Returns:
        True if triggered successfully, False otherwise
    """
    try:
        logger.info(f"Triggering workflow '{workflow_id}' on '{ref}' for {repo_name}")

        owner, repo = repo_name.split("/")
        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)

        # API: api.actions.create_workflow_dispatch(owner, repo, workflow_id, ref)
        # Note: input parameters are not currently supported by this wrapper function
        api.actions.create_workflow_dispatch(owner, repo, workflow_id, ref=ref)

        logger.info(f"Successfully triggered workflow '{workflow_id}'")
        return True

    except Exception as e:
        # Fallback for 422 error (missing workflow_dispatch trigger)
        import time

        try:
            if "422" in str(e):
                logger.warning(f"Failed to trigger {workflow_id} with 422. Attempting to add workflow_dispatch trigger...")

                # Get the file content
                try:
                    # Using GhApi to get file
                    file_path = f".github/workflows/{workflow_id}"
                    contents_res = api.repos.get_content(owner, repo, file_path, ref=ref)
                    content_encoded = contents_res.get("content", "")
                    sha = contents_res.get("sha")

                    import base64

                    content_decoded = base64.b64decode(content_encoded).decode("utf-8")

                    # Check for duplicate workflow_dispatch
                    dispatch_count = content_decoded.count("workflow_dispatch:")
                    new_content = None

                    if dispatch_count > 1:
                        logger.warning(f"Found {dispatch_count} 'workflow_dispatch' keys in {workflow_id}. Attempting to fix...")
                        # Remove all occurrences
                        # This naive replacement assumes 'workflow_dispatch:' is on its own line(s).
                        # We'll use a regex to remove lines containing 'workflow_dispatch:' and optional surrounding whitespace
                        # Be careful not to damage structure.
                        # Safer approach: replace all 'workflow_dispatch:' with nothing, then add one back.
                        # But we need to handle indentation and context.

                        # Let's try to remove all of them and then re-add one at the top of 'on:'.
                        temp_content = re.sub(r"^\s*workflow_dispatch:.*$\n?", "", content_decoded, flags=re.MULTILINE)

                        # Now add it back
                        if "on:" in temp_content:
                            new_content = re.sub(r"(on:\s*\n)", r"\1  workflow_dispatch:\n", temp_content, count=1)

                    elif dispatch_count == 0:
                        if "on:" in content_decoded:
                            # Replaces "on:\n" with "on:\n  workflow_dispatch:\n"
                            new_content = re.sub(r"(on:\s*\n)", r"\1  workflow_dispatch:\n", content_decoded, count=1)

                    if new_content and new_content != content_decoded:
                        logger.info(f"Updating {workflow_id} to ensure single workflow_dispatch trigger")

                        # Commit changes via API (faster/cleaner than checkout for just one file?)
                        # Or use existing helpers if we are local?
                        # automation_engine code usually runs local git.
                        # But here passing 'ref' implies we might not be checked out to it?
                        # The caller (pr_processor) usually has local repo.
                        # BUT, 'trigger_workflow_dispatch' is a utility.

                        # Let's use API commit to be safe and independent of local state
                        message = f"Auto-Coder: Add workflow_dispatch trigger to {workflow_id}"

                        api.repos.create_or_update_file_contents(owner=owner, repo=repo, path=file_path, message=message, content=base64.b64encode(new_content.encode("utf-8")).decode("utf-8"), sha=sha, branch=ref)

                        logger.info(f"Updated {workflow_id} on {ref}. Retrying trigger...")
                        time.sleep(2)  # Wait for propagation
                        api.actions.create_workflow_dispatch(owner, repo, workflow_id, ref=ref)
                        logger.info(f"Successfully triggered workflow '{workflow_id}' after fallback")
                        return True
                    else:
                        logger.warning(f"workflow_dispatch already present in {workflow_id}, 422 might be due to other reasons.")

                except Exception as inner_e:
                    logger.error(f"Failed to apply fallback for {workflow_id}: {inner_e}")

        except Exception as retry_e:
            logger.error(f"Fallback retry failed: {retry_e}")

        logger.error(f"Error triggering workflow '{workflow_id}': {e}")
        return False


def get_github_actions_logs_from_url(url: str) -> str:
    """Extract error blocks by fetching logs for the given GitHub Actions job URL directly.

    Accepted URL format:
    https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
    """
    try:
        m = re.match(
            r"https://github\.com/([^/]+)/([^/]+)/actions/runs/([0-9]+)/job/([0-9]+)",
            url,
        )
        if m:
            owner, repo, run_id, job_id = m.groups()
            owner_repo = f"{owner}/{repo}"
        else:
            # 2) Try to match Run URL (entire run) -> find failed jobs and recurse
            m_run = re.match(
                r"https://github\.com/([^/]+)/([^/]+)/actions/runs/([0-9]+)",
                url,
            )
            if m_run:
                owner, repo, run_id = m_run.groups()
                owner_repo = f"{owner}/{repo}"

                # Fetch jobs to find failed ones
                try:
                    token = GitHubClient.get_instance().token
                    api = get_ghapi_client(token)

                    jobs_res = api.actions.list_jobs_for_workflow_run(owner=owner, repo=repo, run_id=run_id)
                    jobs = jobs_res.get("jobs", [])

                    failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

                    if failed_jobs:
                        logs_list = []
                        for job in failed_jobs:
                            # extracting job_id
                            j_id = job.get("id")
                            if j_id:
                                # specific job url
                                j_url = f"https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{j_id}"
                                logs_list.append(get_github_actions_logs_from_url(j_url))

                        if logs_list:
                            return "\n\n".join(logs_list)

                    # If no failed jobs found or no logs
                    return f"No failed jobs found in run {run_id}"
                except Exception as e:
                    logger.warning(f"Error expanding run URL {url}: {e}")
                    pass

                return "Invalid GitHub Actions job URL (Run expansion failed)"

            return "Invalid GitHub Actions job URL"

        # Prepare GhApi
        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)

        # 1) Get job details to get name and identifying failing steps
        job_name = f"job-{job_id}"
        failing_step_names: set = set()

        try:
            job_detail = api.actions.get_job_for_workflow_run(owner=owner, repo=repo, job_id=job_id)
            job_name = job_detail.get("name", job_name)

            steps = job_detail.get("steps", [])
            for st in steps:
                if (st.get("conclusion") == "failure") or (st.get("conclusion") is None and st.get("status") == "completed" and job_detail.get("conclusion") == "failure"):
                    nm = st.get("name")
                    if nm:
                        failing_step_names.add(nm)
        except Exception as e:
            logger.warning(f"Error getting job details for {job_id}: {e}")
            pass

        def _norm(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

        norm_fail_names = {_norm(n) for n in failing_step_names}

        def _file_matches_fail(step_file_label: str, content: str) -> bool:
            if not norm_fail_names:
                return True  # Allow all if no filter info (conventional behavior)
            lbl = _norm(step_file_label)
            if any(n and (n in lbl or lbl in n) for n in norm_fail_names):
                return True
            # Simple check if step name is included in header near content start
            head = "\n".join(content.split("\n")[:8]).lower()
            return any(n and (n in head) for n in norm_fail_names)

        # 2) Get job ZIP logs
        # api.actions.download_job_logs_for_workflow_run returns the redirect response or content
        # gh_cache now handles binary content for zip
        try:
            # The API call might return bytes (zip) or text depending on endpoint/headers
            # But the 'download_job_logs_for_workflow_run' usually redirects to a zip location
            log_content = api.actions.download_job_logs_for_workflow_run(owner=owner, repo=repo, job_id=job_id)

            # log_content should be bytes if it's a zip
            if isinstance(log_content, bytes):
                with tempfile.TemporaryDirectory() as tmpdir:
                    zip_path = os.path.join(tmpdir, "job_logs.zip")
                    with open(zip_path, "wb") as f:
                        f.write(log_content)

                    try:
                        with zipfile.ZipFile(zip_path, "r") as zf:
                            step_snippets = []
                            job_summary_lines = []
                            for name in zf.namelist():
                                if name.lower().endswith(".txt"):
                                    with zf.open(name, "r") as fp:
                                        try:
                                            # Using errors='ignore' as in original
                                            content = fp.read().decode("utf-8", errors="ignore")
                                        except Exception:
                                            content = ""
                                    if not content:
                                        continue

                                    step_file_label = os.path.splitext(os.path.basename(name))[0]

                                    # Step filter
                                    if not _file_matches_fail(step_file_label, content):
                                        continue

                                    # Collect job-wide summary candidates
                                    for ln in content.split("\n"):
                                        ll = ln.lower()
                                        if ((" failed" in ll) or (" passed" in ll) or (" skipped" in ll) or (" did not run" in ll)) and any(ch.isdigit() for ch in ln):
                                            job_summary_lines.append(ln)

                                    step_name = step_file_label

                                    # Extract important error-related information
                                    if "eslint" in job_name.lower() or "lint" in job_name.lower():
                                        snippet = _filter_eslint_log(content)
                                    else:
                                        snippet = _extract_error_context(content)

                                    # Enhance with expected/received
                                    exp_lines = []
                                    for ln in content.split("\n"):
                                        if ("Expected substring:" in ln) or ("Received string:" in ln):
                                            exp_lines.append(ln)
                                    if exp_lines:
                                        norm_lines = [ln.replace('\\"', '"') for ln in exp_lines]
                                        if "--- Expectation Details ---" not in snippet:
                                            snippet = (snippet + "\n\n--- Expectation Details ---\n" if snippet else "") + "\n".join(norm_lines)
                                        else:
                                            snippet = snippet + "\n" + "\n".join(norm_lines)

                                    if snippet and snippet.strip():
                                        s = snippet
                                        s_lower = s.lower()
                                        if (
                                            ".spec.ts" in s
                                            or "expect(received)" in s
                                            or "Expected substring:" in s
                                            or "error was not a part of any test" in s
                                            or "Command failed with exit code" in s
                                            or "Process completed with exit code" in s
                                            or "error" in s_lower
                                            or "failed" in s_lower
                                            or "##[error]" in s
                                        ):
                                            step_snippets.append(f"--- Step {step_name} ---\n{s}")

                            if step_snippets:
                                # Add job-wide summary at the end
                                summary_block = ""
                                if job_summary_lines:
                                    seen = set()
                                    uniq_rev = []
                                    for ln in reversed(job_summary_lines):
                                        if ln not in seen:
                                            seen.add(ln)
                                            uniq_rev.append(ln)
                                    summary_lines = list(reversed(uniq_rev))
                                    body_str = "\n\n".join(step_snippets)
                                    filtered = [ln for ln in summary_lines[-15:] if ln not in body_str]
                                    summary_block = ("\n\n--- Summary ---\n" + "\n".join(filtered)) if filtered else ""

                                body = "\n\n".join(step_snippets) + summary_block
                                if "eslint" not in job_name.lower() and "lint" not in job_name.lower():
                                    body = slice_relevant_error_window(body)
                                return f"=== Job: {job_name} ===\n" + body

                    except zipfile.BadZipFile:
                        pass

            elif isinstance(log_content, str) and log_content:
                # Handle text content (likely standard log text if not a zip)
                # This happens if GhApi returns text for download_job_logs_for_workflow_run

                snippet_parts = []

                # 1. Try to extract logs for specific failed steps (good for context if it works)
                if failing_step_names:
                    step_log = _extract_failed_step_logs(log_content, list(failing_step_names))
                    if step_log:
                        snippet_parts.append(step_log)

                # 2. Always extract error context to ensure we catch actual errors
                # (e.g. printed outside of groups, or if step extraction was too aggressive)
                error_ctx = _extract_error_context(log_content)
                if error_ctx:
                    # Deduplicate if possible, but for now just appending is safer
                    if not snippet_parts or error_ctx not in snippet_parts[0]:
                        snippet_parts.append("--- Additional Error Context ---\n" + error_ctx)

                if snippet_parts:
                    return f"=== Job: {job_name} ===\n" + "\n\n".join(snippet_parts)
                else:
                    # If no error context found but we have logs, return tail
                    snippet = slice_relevant_error_window(log_content)
                    return f"=== Job: {job_name} ===\n" + snippet
        except Exception as e:
            logger.warning(f"Error processing job zip for {job_id}: {e}")
            pass

        # Falback to text log if zip failed or returned text (though GhApi download_job_logs usually is zip for finished jobs)
        # Note: GhApi might return text if we used a different header, but we are using standard method.
        # If we failed to get zip or extract useful info, we can try matching 'Run failed' logs if available via other means?
        # But 'gh run view --log' effectively gets the text log.
        # api.actions.download_job_logs_for_workflow_run is the equivalent.

        return f"=== Job: {job_name} ===\n{url}\nNo detailed logs available (GhApi retrieval failed or no errors found)"

    except Exception as e:
        logger.error(f"Error fetching GitHub Actions logs from URL: {e}")
        return f"Error getting logs: {e}"


def _search_github_actions_logs_from_history(
    repo_name: str,
    config: AutomationConfig,
    failed_checks: List[Dict[str, Any]],
    pr_data: Optional[Dict[str, Any]] = None,
    max_runs: int = 10,
) -> Optional[str]:
    """Search for GitHub Actions logs from recent runs.

    This function retrieves recent GitHub Actions runs (not git commit history) and
    attempts to get logs from them. It searches through failed jobs from the most
    recent runs backwards, returning the first set of detailed logs found.

    Args:
        repo_name: Repository name in format 'owner/repo'
        config: AutomationConfig instance
        failed_checks: List of failed check dictionaries with details_url
        pr_data: Optional PR data dictionary for commit-specific filtering
        max_runs: Maximum number of recent runs to search (default: 10)

    Returns:
        String containing the first successful logs found from historical runs,
        or None if not found. The returned logs include metadata about which run
        they came from.

    Note:
        This searches through recent GitHub Actions runs, not git commit history.
        This is more practical since GitHub Actions runs are directly queryable
        via the GitHub API, while matching commits to Action runs requires
        additional metadata that may not always be available.
    """
    try:
        logger.info(f"Starting historical search for GitHub Actions logs (searching through {max_runs} recent runs)")

        runs = []
        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # If PR data is available, try to get runs for the specific PR commit first
        if pr_data:
            head_sha = pr_data.get("head", {}).get("sha", "")
            pr_number = pr_data.get("number", "unknown")

            logger.info(f"Attempting to find runs for PR #{pr_number} (commit {head_sha[:8] if head_sha else 'unknown'})")

            if head_sha:
                try:
                    # Get recent runs
                    commit_run_list = api.actions.list_workflow_runs_for_repo(owner=owner, repo=repo, per_page=max_runs)
                    all_runs = commit_run_list.get("workflow_runs", [])

                    candidate_runs = [r for r in all_runs if str(r.get("head_sha", "")).startswith(head_sha[:7])]
                    pr_candidate_runs = [r for r in candidate_runs if r.get("event") == "pull_request"]
                    runs = pr_candidate_runs or candidate_runs

                    if runs:
                        logger.info(f"Found {len(runs)} runs matching PR #{pr_number} commit from recent history")
                    else:
                        logger.info("No runs found matching PR commit, falling back to recent runs")
                except Exception as e:
                    logger.warning(f"Failed to fetch runs for PR commit: {e}")
                    runs = []
            else:
                runs = []

        # If no runs found (or no PR data), fall back to recent runs
        if not runs:
            try:
                # Get recent GitHub Actions runs
                run_list = api.actions.list_workflow_runs_for_repo(owner=owner, repo=repo, per_page=max_runs)
                runs = run_list.get("workflow_runs", [])
                logger.info(f"Retrieved {len(runs)} recent runs")
            except Exception as e:
                logger.warning(f"Failed to fetch recent runs: {e}")
                runs = []

        if not runs:
            logger.info("No recent runs found")
            return None

        # Process found runs to find failed ones and get logs
        for run in runs:
            run_id = run.get("id")
            conclusion = run.get("conclusion")

            # Skip runs that are not failures or don't have an ID
            if not run_id or conclusion != "failure":
                continue

            # Check if this run contains any of the failing checks we are interested in
            logger.info(f"Checking run {run_id} ({run.get('display_title', 'No Title')}) for failed jobs...")

            try:
                # Get jobs for this run
                jobs_data = api.actions.list_jobs_for_workflow_run(owner=owner, repo=repo, run_id=run_id)
                jobs = jobs_data.get("jobs", [])

                # Check if any job matches our failed checks
                matching_failed_job = None

                # Create set of failed check names for faster lookup
                failed_check_names = {check.get("name") for check in failed_checks if check.get("name")}

                for job in jobs:
                    if job.get("name") in failed_check_names and job.get("conclusion") == "failure":
                        matching_failed_job = job
                        break

                if matching_failed_job:
                    logger.info(f"Found match: Job '{matching_failed_job.get('name')}' failed in run {run_id}")

                    # Try to get logs for this job/run
                    # Ideally we want the log for the specific job, but getting the log URL for a job is tricky via API sometimes
                    # api.actions.download_job_logs_for_workflow_run redirects to a URL.
                    # But we use get_github_actions_logs_from_url which takes a HTML URL?

                    # The original implementation called:
                    # logs = get_github_actions_logs_from_url(run.get("url"), config, failed_checks)
                    # run.get("url") from gh json returned the api url or html url?
                    # "url": "https://github.com/test/repo/actions/runs/1001" (HTML URL usually in 'url' field for gh json? No, gh json 'url' is usually HTML URL).
                    # GhApi returns 'html_url' and 'url' (API URL).
                    # 'get_github_actions_logs_from_url' logic likely scrapes or uses gh run view.

                    # Let's check what url we should pass. 'html_url' is safe for existing logic usually.

                    run_url = run.get("html_url")
                    if not run_url:
                        # Fallback to constructing it or using 'url'
                        run_url = run.get("url")

                    if run_url:
                        logs = get_github_actions_logs_from_url(run_url, config, failed_checks)

                        if logs and "No detailed logs available" not in logs:
                            # Prepend some metadata about where these logs came from
                            run_date = run.get("created_at", "unknown date")
                            run_branch = run.get("head_branch", "unknown branch")
                            run_sha = run.get("head_sha", "unknown sha")

                            metadata = f"[From run {run_id} on {run_branch} at {run_date} (commit {run_sha})]\n"
                            return metadata + logs
            except Exception as e:
                logger.warning(f"Error checking run {run_id}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error during historical log search: {e}")
        return None

    return None


def _get_github_actions_logs(
    repo_name: str,
    config: AutomationConfig,
    *args: Any,
    search_history: Optional[bool] = None,
    **kwargs: Any,
) -> List[str]:
    """Get GitHub Actions failed job logs via gh api and return extracted error locations.

    Args:
        repo_name: Repository name in format 'owner/repo'
        config: AutomationConfig instance
        *args: Arguments (failed_checks list)
        search_history: Optional parameter to enable historical search.
                       If None, uses config.SEARCH_GITHUB_ACTIONS_HISTORY.
                       If True, searches through commit history for logs.
                       If False, uses current state only.
        **kwargs: Additional keyword arguments (for future use)

    Returns:
        List of strings containing GitHub Actions logs

    Call compatibility:
    - _get_github_actions_logs(repo, config, failed_checks)
    - _get_github_actions_logs(repo, config, failed_checks, pr_data)
    """
    # Determine search_history value
    if search_history is None:
        search_history = config.SEARCH_GITHUB_ACTIONS_HISTORY

    # Extract failed_checks and optional pr_data from args
    failed_checks: List[Dict[str, Any]] = []
    pr_data: Optional[Dict[str, Any]] = None
    if len(args) >= 1 and isinstance(args[0], list):
        failed_checks = args[0]
    if len(args) >= 2 and isinstance(args[1], dict):
        pr_data = args[1]

    # Handle the case where historical search is explicitly enabled
    if search_history:
        logger.info("Historical search enabled: Searching through commit history for GitHub Actions logs")

        if not failed_checks:
            # No failed_checks provided
            return ["No detailed logs available"]

        # Try historical search first
        historical_logs = _search_github_actions_logs_from_history(repo_name, config, failed_checks, pr_data, max_runs=10)

        if historical_logs:
            logger.info("Historical search succeeded: Found logs from commit history")
            return [historical_logs]

        logger.info("Historical search failed or found no logs, falling back to current behavior")

    # Default behavior (or fallback from historical search)
    # Resolve argument patterns
    # (failed_checks and pr_data may have been set in the historical search block above)
    if not failed_checks:
        failed_checks = []
    if not pr_data:
        pr_data = None
    if len(args) >= 1 and isinstance(args[0], list):
        failed_checks = args[0]
    if len(args) >= 2 and isinstance(args[1], dict):
        pr_data = args[1]
    if not failed_checks:
        # Unknown call
        return ["No detailed logs available"]

    logs: List[str] = []

    try:
        # 1) First extract run_id and job_id directly from failed_checks details_url
        # details_url format: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
        # or https://github.com/<owner>/<repo>/runs/<job_id>
        url_to_fetch: List[str] = []
        for check in failed_checks:
            details_url = check.get("details_url", "")
            if details_url and "github.com" in details_url and "/actions/runs/" in details_url:
                # Use directly if correct format URL is included
                url_to_fetch.append(details_url)
                logger.debug(f"Using details_url from failed_checks: {details_url}")

        # 2) If can get from details_url, use it to get logs
        if url_to_fetch:
            for url in url_to_fetch:
                unified = get_github_actions_logs_from_url(url)
                logs.append(unified)
        else:
            # 3) If details_url not usable, use conventional method (get failed run from PR branch)
            logger.debug("No valid details_url found in failed_checks, falling back to GhApi run list")
            # Get PR branch and get only runs from that branch (search commit history)
            branch_name = None
            if pr_data:
                head = pr_data.get("head", {})
                branch_name = head.get("ref")
                if branch_name:
                    logger.debug(f"Using PR branch: {branch_name}")

            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            try:
                # API: list_workflow_runs_for_repo(owner, repo, branch=..., per_page=...)
                kwargs = {"owner": owner, "repo": repo, "per_page": 50}
                if branch_name:
                    kwargs["branch"] = branch_name

                run_list = api.actions.list_workflow_runs_for_repo(**kwargs)
                runs = run_list.get("workflow_runs", [])
            except Exception as e:
                logger.warning(f"Failed to fetch runs via GhApi: {e}")
                runs = []

            run_id = None
            if runs:
                # Find first failed run?
                # The original code logic iterates. Let's replicate original logic which used json.loads
                # Original logic:
                # runs = json.loads(...)
                # for run in runs:
                #    if run.get("conclusion") == "failure": ...

                for run in runs:
                    if run.get("conclusion") == "failure":
                        run_id = run.get("id")
                        # Also get title/url for logging or usage?
                        # The original code didn't use them except to find ID.
                        if run_id:
                            logger.info(f"Found recent failed run {run_id} ({run.get('display_title')})")
                            break
            else:
                logger.info("No runs found via GhApi")

            if run_id:
                try:
                    jobs = _get_jobs_for_run_filtered_by_pr_number(run_id, pr_data.get("number") if pr_data else None, repo_name)

                    for job in jobs:
                        conclusion = job.get("conclusion", "")
                        if conclusion and conclusion.lower() in ["failure", "failed", "error"]:
                            job_id = job.get("id")
                            if job_id:
                                url = f"https://github.com/{repo_name}/actions/runs/{run_id}/job/{job_id}"
                                asyncio_mode = config.SEARCH_GITHUB_ACTIONS_HISTORY  # Dummy use of config if needed or log
                                unified = get_github_actions_logs_from_url(url)
                                logs.append(unified)
                except Exception as e:
                    logger.warning(f"Error getting jobs/logs for run {run_id}: {e}")

        # 6) Fallback: if run/job cannot be retrieved, format failed_checks as is
        if not logs:
            for check in failed_checks:
                check_name = check.get("name", "Unknown")
                conclusion = check.get("conclusion", "unknown")
                details_url = check.get("details_url", "")
                url_str = f"\n\n{details_url}" if details_url else ""
                logs.append(f"=== {check_name} ===\nStatus: {conclusion}\nNo detailed logs available{url_str}")

    except Exception as e:
        logger.error(f"Error getting GitHub Actions logs: {e}")
        logs.append(f"Error getting logs: {e}")

    return logs if logs else ["No detailed logs available"]


def _extract_failed_step_logs(log_content: str, failed_step_names: list) -> str:
    """Extract only the logs for failed steps from the full log content.

    Args:
        log_content: Full log content
        failed_step_names: List of failed step names

    Returns:
        Concatenated logs for the failed steps
    """
    if not failed_step_names:
        # Use conventional method if failed step cannot be identified
        return _extract_error_context(log_content)

    lines = log_content.split("\n")
    result_sections = []

    for step_name in failed_step_names:
        # Extract step logs
        step_lines = []
        in_step = False
        step_found = False

        # Extract keywords from step name
        step_name_lower = step_name.lower()
        keywords = []

        # Mapping for specific step names
        if "lint" in step_name_lower and "functions" in step_name_lower:
            keywords = ["cd functions", "npm run lint", "eslint"]
        elif "cat log" in step_name_lower:
            keywords = ["cat", "log"]
        elif "build" in step_name_lower and "client" in step_name_lower:
            keywords = ["cd client", "npm run build"]
        elif "test" in step_name_lower:
            keywords = ["test", "vitest", "jest", "playwright"]
        else:
            # General case: use words from step name
            keywords = [word for word in step_name_lower.split() if len(word) > 3]

        for i, line in enumerate(lines):
            # Detect step start
            if "##[group]Run" in line and not step_found:
                # End previous step
                if in_step:
                    break

                # Check if this is step start
                line_lower = line.lower()
                if any(keyword in line_lower for keyword in keywords):
                    in_step = True
                    step_found = True
                    step_lines.append(line)
                    continue

            # Collect lines in step
            if in_step:
                step_lines.append(line)

                # Detect next step start
                if i > 0 and "##[group]Run" in line:
                    # This line is next step, exclude
                    step_lines.pop()
                    break

        if step_lines:
            # Remove ANSI escape sequences and timestamps
            cleaned_lines = [_clean_log_line(line) for line in step_lines]
            section = f"=== Step: {step_name} ===\n" + "\n".join(cleaned_lines)
            result_sections.append(section)

    if result_sections:
        return "\n\n".join(result_sections)
    else:
        # Use conventional method if step not found
        return _extract_error_context(log_content)


def _filter_eslint_log(content: str) -> str:
    """Filter and extract important information from ESLint logs.

    Args:
        content: ESLint log content

    Returns:
        Extracted ESLint error context
    """
    if not content:
        return ""

    # Use the existing error context extraction with focus on ESLint patterns
    return _extract_error_context(content, max_lines=100)


def _extract_error_context(content: str, max_lines: int = 500) -> str:
    """Extract important information from error logs.

    Args:
        content: Log content
        max_lines: Maximum number of lines to include (default: 500)

    Returns:
        Extracted error context
    """
    if not content:
        return ""

    lines = content.split("\n")

    # Error-related keywords
    error_keywords = [
        "error:",
        "failed",
        "failure",
        "expect(received)",
        "expected substring:",
        "received string:",
        ".spec.ts",
        "command failed with exit code",
        "process completed with exit code",
        "error was not a part of any test",
        "eslint",  # Treat ESLint errors specially
        "##[error]",
        "##[warning]",
        "error ts",
        "build failed",
        "module not found",
        "syntaxerror",
        "referenceerror",
        "typeerror",
    ]

    # Collect error-related lines
    important_indices = []
    eslint_blocks = []  # Collect ESLint blocks specially

    for i, line in enumerate(lines):
        line_lower = line.lower()

        # Detect ESLint command execution
        if "eslint" in line_lower and (">" in line or "run" in line_lower):
            # Record ESLint block start
            eslint_start = i
            # Find end of ESLint errors (up to "✖ N problems")
            eslint_end = i
            for j in range(i + 1, min(len(lines), i + 50)):
                if "problems" in lines[j].lower() or "##[error]process completed" in lines[j].lower():
                    eslint_end = j
                    break
            eslint_blocks.append((eslint_start, eslint_end))

        if any(keyword in line_lower for keyword in error_keywords):
            important_indices.append(i)

    if not important_indices and not eslint_blocks:
        # If error keywords not found, return entire content (max max_lines lines)
        cleaned_lines = [_clean_log_line(line) for line in lines[:max_lines]]
        return "\n".join(cleaned_lines)

    # Extract including before/after error lines
    context_lines = set()

    # Include entire ESLint block
    for start, end in eslint_blocks:
        for i in range(start, end + 1):
            context_lines.add(i)

    # Include before/after other error lines
    for idx in important_indices:
        # Include 10 lines before/after each error line
        start = max(0, idx - 10)
        end = min(len(lines), idx + 10)
        for i in range(start, end):
            context_lines.add(i)

    # Sort and combine by line number
    sorted_indices = sorted(context_lines)
    result_lines = [_clean_log_line(lines[i]) for i in sorted_indices]

    # Limit to max lines
    if len(result_lines) > max_lines:
        # Include first and last parts
        half = max_lines // 2
        result_lines = result_lines[:half] + ["... (omitted) ..."] + result_lines[-half:]

    return "\n".join(result_lines)


def slice_relevant_error_window(text: str) -> str:
    """Return only the necessary parts related to errors (discard prelude, emphasize and shorten latter half).
    Policy:
    - Search for priority triggers and return from the earliest position to the end
    - If not found, limit to the last several hundred lines
    """
    if not text:
        return text
    lines = [_clean_log_line(line) for line in text.split("\n")]
    # Group by priority from highest to lowest
    priority_groups = [
        ["Expected substring:", "Received string:", "expect(received)"],
        ["Error:   ", ".spec.ts", "##[error]", "##[warning]", "error ts", "build failed", "module not found", "syntaxerror", "referenceerror", "typeerror"],
        ["Command failed with exit code", "Process completed with exit code"],
        ["error was not a part of any test", "Notice:", "##[notice]", "notice"],
    ]
    start_idx = None
    # Search for the earliest priority trigger (from front)
    for group in priority_groups:
        for i, line in enumerate(lines):
            low = line.lower()
            if any(g.lower() in low for g in group):
                start_idx = max(0, i - 30)
                break
        if start_idx is not None:
            break
    if start_idx is None:
        # If no trigger found, use only the end (max 500 lines)
        return "\n".join(lines[-500:])
    # Keep the end as-is. Also limit to max 1500 lines
    sliced = lines[start_idx:]
    if len(sliced) > 1500:
        sliced = sliced[:1500]
    return "\n".join(sliced)


def _clean_log_line(line: str) -> str:
    """Remove ANSI escape sequences and timestamps from a log line.

    Args:
        line: Log line

    Returns:
        Cleaned log line
    """
    import re

    # Remove ANSI escape sequences
    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")
    line = ansi_escape.sub("", line)

    # Remove timestamp (example: 2025-10-27T03:26:24.5806020Z)
    # Also remove potential prefix like "test\tBuild client\t"
    timestamp_pattern = re.compile(r"^.*?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s")
    line = timestamp_pattern.sub("", line)

    return line


def preload_github_actions_status(repo_name: str, prs: List[Dict[str, Any]]) -> None:
    """
    Preload GitHub Actions status for multiple PRs to avoid N+1 API calls.
    Fetches recent workflow runs and populates the GitHub cache.
    """
    if not prs:
        return

    # Collect head SHAs to look for
    sha_to_pr = {}
    for pr in prs:
        sha = pr.get("head", {}).get("sha")
        number = pr.get("number")
        if sha and number:
            sha_to_pr[sha] = number

    if not sha_to_pr:
        return

    try:
        from ..util.gh_cache import get_ghapi_client

        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)
        owner, repo = repo_name.split("/")

        # Fetch recent runs
        # API: api.actions.list_workflow_runs_for_repo(owner, repo, per_page=100)
        runs_resp = api.actions.list_workflow_runs_for_repo(owner, repo, per_page=100)
        runs = runs_resp.get("workflow_runs", [])

        # Group runs by SHA
        runs_by_sha = {}
        for run in runs:
            # API returns snake_case keys (head_sha), gh CLI returned camelCase (headSha)
            # Support both just in case, utilizing the broader check pattern
            head_sha = run.get("head_sha", run.get("headSha"))
            if head_sha and head_sha in sha_to_pr:
                if head_sha not in runs_by_sha:
                    runs_by_sha[head_sha] = []
                runs_by_sha[head_sha].append(run)

        # Update cache for each PR found
        cache = get_github_cache()

        for sha, pr_runs in runs_by_sha.items():
            pr_number = sha_to_pr[sha]

            run_ids = []
            has_in_progress = False
            all_passed = True

            for run in pr_runs:
                # API returns 'id', gh CLI returned 'databaseId'
                rid = run.get("id") or run.get("databaseId")
                if rid:
                    run_ids.append(int(rid))

                status = (run.get("status") or "").lower()
                conclusion = (run.get("conclusion") or "").lower()

                if conclusion in ["failure", "failed", "error", "cancelled", "timed_out"]:
                    all_passed = False
                elif status in ["in_progress", "queued", "pending", "waiting"]:
                    has_in_progress = True
                    all_passed = False

            run_ids = list(set(run_ids))

            result = GitHubActionsStatusResult(
                success=all_passed,
                ids=run_ids,
                in_progress=has_in_progress,
            )

            cache_key = f"gh_actions_status:{repo_name}:{pr_number}:{sha}"
            cache.set(cache_key, result)
            logger.debug(f"Preloaded cache for PR #{pr_number} ({sha[:8]})")

    except Exception as e:
        logger.error(f"Error in preload_github_actions_status: {e}")


def check_github_actions_and_exit_if_in_progress(
    repo_name: str,
    pr_data: Dict[str, Any],
    config: AutomationConfig,
    github_client: Optional[GitHubClient] = None,
    switch_branch_on_in_progress: bool = True,
    item_number: Optional[int] = None,
    item_type: str = "PR",
) -> bool:
    """
    Check GitHub Actions status and exit if checks are still in progress.
    Returns True if processing should continue, False if early exit occurred.

    Args:
        repo_name: Repository name in format 'owner/repo'
        pr_data: PR data dictionary
        config: AutomationConfig instance
        github_client: GitHubClient instance
        switch_branch_on_in_progress: If True, switch to main branch and exit when checks are in progress
                                      If False, just return early (for pr_processor.py pattern)
        item_number: Optional item number for logging (uses pr_data['number'] if not provided)
        item_type: Type of item being processed (default: "PR")

    Returns:
        True if processing should continue, False if early exit occurred
    """
    try:
        # Get item number
        number = item_number if item_number is not None else pr_data.get("number")
        if not number:
            logger.warning(f"No item number found, cannot check GitHub Actions status")
            return True

        # Check GitHub Actions status
        github_checks = _check_github_actions_status(repo_name, pr_data, config)
        # Optimized: Use the in_progress status from the summary check instead of fetching detailed checks.
        # _check_github_actions_status already correctly identifies in-progress runs from check-runs or workflow runs.
        # This avoids N+1 API calls (one per workflow run) incurred by get_detailed_checks_from_history.

        # If GitHub Actions are still in progress
        if github_checks.in_progress:
            if switch_branch_on_in_progress:
                # Issue processor pattern: switch to main and return to main loop
                logger.info(f"GitHub Actions checks are still in progress for {item_type} #{number}, switching to main branch")

                # Switch to main branch with pull
                from ..git_branch import switch_to_branch

                switch_result = switch_to_branch(branch_name=config.MAIN_BRANCH, pull_after_switch=True)
                if not switch_result.success:
                    logger.warning(f"Failed to switch to {config.MAIN_BRANCH}: {switch_result.stderr}")
                else:
                    logger.info(f"Successfully switched to {config.MAIN_BRANCH} branch")
                # Return to allow main loop to continue processing
                logger.info(f"Continuing to next item after {item_type} #{number} (GitHub Actions in progress)")
                return False
            else:
                # PR processor pattern: just return early
                logger.info(f"GitHub Actions checks are still in progress for {item_type} #{number}, skipping to next {item_type}")
                return False

        return True

    except Exception as e:
        logger.error(f"Error checking GitHub Actions status: {e}")
        return True  # Continue processing on error


def check_and_handle_closed_state(
    repo_name: str,
    item_type: str,
    item_number: int,
    config: AutomationConfig,
    github_client: GitHubClient,
    current_item: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Check if processed item is closed and handle main branch restoration.
    Returns True if should exit, False if processing should continue.

    Args:
        repo_name: Repository name in format 'owner/repo'
        item_type: Type of item ("issue" or "pr")
        item_number: Item number
        config: AutomationConfig instance
        github_client: GitHubClient instance
        current_item: Optional already-fetched item data to avoid refetching

    Returns:
        True if processing should exit, False if processing should continue
    """
    try:
        # Get current item state if not provided
        if current_item is None:
            if item_type == "pr":
                pr = github_client.get_pull_request(repo_name, item_number)
                current_item = github_client.get_pr_details(pr)
            elif item_type == "issue":
                issue = github_client.get_issue(repo_name, item_number)
                current_item = github_client.get_issue_details(issue)
            else:
                logger.warning(f"Unknown item type: {item_type}")
                return False

        # Check if item is closed
        if current_item.get("state") == "closed":
            logger.info(f"{item_type.capitalize()} #{item_number} is closed, switching to main branch")

            # Switch to main branch with pull
            from ..git_branch import switch_to_branch

            switch_result = switch_to_branch(branch_name=config.MAIN_BRANCH, pull_after_switch=True)
            if not switch_result.success:
                logger.warning(f"Failed to switch to {config.MAIN_BRANCH}: {switch_result.stderr}")
                logger.warning(f"Exiting due to failed switch to {config.MAIN_BRANCH}. Item {item_type} #{item_number} is closed.")
                sys.exit(0)
            else:
                logger.info(f"Successfully switched to {config.MAIN_BRANCH} branch")
            # Return True to indicate exit should occur
            logger.info(f"Item {item_type} #{item_number} is closed, returning to main loop")
            return True

        return False  # Don't exit, continue processing

    except Exception as e:
        logger.warning(f"Failed to check/handle closed item state: {e}")
        return False  # Continue on error


def _normalize_gh_path(line: str) -> str:
    """Normalize GitHub Actions absolute paths to relative paths.

    Converts:
    - /__w/repo/repo/file -> file
    - /home/runner/work/repo/repo/file -> file
    """
    import re

    if "/__w/" in line:
        line = re.sub(r"^/__w/[^/]+/[^/]+/", "", line)
        line = re.sub(r"/__w/[^/]+/[^/]+/", "", line)

    if "/home/runner/work/" in line:
        line = re.sub(r"^/home/runner/work/[^/]+/[^/]+/", "", line)
        line = re.sub(r"/home/runner/work/[^/]+/[^/]+/", "", line)

    return line


def _sort_jobs_by_workflow(jobs: list, owner: str, repo: str, run_id: int, token: str) -> list:
    """Sort jobs based on the order defined in the workflow file."""
    try:
        import yaml

        api = get_ghapi_client(token)

        # Get run details to find workflow file path
        run = api.actions.get_workflow_run(owner, repo, run_id)
        workflow_path = run.get("path")  # e.g. .github/workflows/ci.yml

        if not workflow_path:
            return jobs

        # Check if file exists locally
        # We assume the user is running this in the repo
        # (or at least has access to the workflow file we care about)
        if not os.path.exists(workflow_path):
            # Try absolute path from workspace root if implied
            possible_path = os.path.join(os.getcwd(), workflow_path)
            if os.path.exists(possible_path):
                workflow_path = possible_path
            else:
                # Check if path starts with .github, maybe we are in root
                if workflow_path.startswith(".github"):
                    if os.path.exists(workflow_path):
                        pass
                    else:
                        return jobs
                else:
                    return jobs

        with open(workflow_path, "r") as f:
            workflow_data = yaml.safe_load(f)

        if not workflow_data or "jobs" not in workflow_data:
            return jobs

        # Create map of job name/key to index
        job_order = {}
        for idx, (job_key, job_def) in enumerate(workflow_data["jobs"].items()):
            # Map key
            job_order[job_key] = idx
            # Map name if present
            if isinstance(job_def, dict) and "name" in job_def:
                job_order[job_def["name"]] = idx

        # Sort jobs
        def get_sort_index(job):
            name = job.get("name")
            # Try exact match
            if name in job_order:
                return job_order[name]

            # Try clean match (sometimes API returns "Job / Key" or similar?)
            # Usually API 'name' is the 'name' property or key.
            # But for matrix, it might be "test (3.11)".
            # Let's try to match start of string if not exact?
            # Or splitting by parentheses.

            # Check against keys
            for key, idx in job_order.items():
                if name == key:
                    return idx
                # Basic fuzzy match: if key is word-bounded in name?
                # e.g. "e2e-test" in "e2e-test / chrome"
                if key in name:
                    return idx

            return 9999

        return sorted(jobs, key=get_sort_index)

    except Exception as e:
        logger.warning(f"Warning: Failed to sort jobs by workflow: {e}")
        return jobs


def _get_playwright_artifact_logs(repo_name: str, run_id: int) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """Download and parse Playwright JSON logs from GitHub Artifacts using direct API calls.
    aggregating results from all matching 'e2e-artifacts-*'.

    Args:
        repo_name: Repository name (owner/repo)
        run_id: GitHub Action run ID

    Returns:
        Tuple containing:
        - Formatted log string if successful, None otherwise.
        - List of raw JSON artifact contents (dicts) if successful, None otherwise.
        Raises specific exceptions if download fails which should stop the process.
    """
    logger.info(f"Attempting to download Playwright artifacts for run {run_id}")

    try:
        # Use GitHubClient to get token and headers
        client = GitHubClient.get_instance()
        token = client.token

        # Use httpx for direct API calls
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        api_base = "https://api.github.com"

        # 1. List artifacts
        list_url = f"{api_base}/repos/{repo_name}/actions/runs/{run_id}/artifacts"

        try:
            import httpx

            with httpx.Client() as h_client:
                response = h_client.get(list_url, headers=headers, timeout=30)
                if response.status_code != 200:
                    logger.warning(f"Failed to list artifacts: {response.status_code} {response.text}")
                    return None, None
                data = response.json()
        except ImportError:
            logger.error("httpx module required but not found (unexpected).")
            return "Error: httpx module required but not found", None
        except Exception as e:
            logger.warning(f"Exception listing artifacts: {e}")
            return None, None

        artifacts = data.get("artifacts", [])

        # Filter for e2e-artifacts-*
        target_artifacts = []
        for artifact in artifacts:
            name = artifact.get("name", "")
            if name.startswith("e2e-artifacts-") and not artifact.get("expired", False):
                target_artifacts.append(artifact)

        if not target_artifacts:
            logger.info("No active e2e-artifacts-* found for this run.")
            return None, None

        logger.info(f"Found {len(target_artifacts)} artifact(s) matching e2e-artifacts-*")

        all_raw_artifacts = []

        # 2. Download and extract all artifacts
        import httpx

        # We'll use a single temp directory for all downloads/extractions
        with tempfile.TemporaryDirectory() as tmp_dir:

            with httpx.Client(follow_redirects=True) as h_client:

                for i, artifact in enumerate(target_artifacts):
                    artifact_id = artifact.get("id")
                    artifact_name = artifact.get("name")
                    details_str = f"{artifact_name} ({artifact_id})"

                    download_url = f"{api_base}/repos/{repo_name}/actions/artifacts/{artifact_id}/zip"
                    zip_path = os.path.join(tmp_dir, f"logs_{i}.zip")

                    try:
                        dl_response = h_client.get(download_url, headers=headers, timeout=300)

                        if dl_response.status_code != 200:
                            logger.error(f"Failed to download artifact {details_str}: {dl_response.status_code}")
                            continue

                        with open(zip_path, "wb") as f:
                            for chunk in dl_response.iter_bytes():
                                f.write(chunk)

                    except Exception as e:
                        if "USER_STOP_REQUEST" in str(e):
                            raise e
                        logger.warning(f"Error downloading artifact {details_str}: {e}")
                        continue

                    # Extract
                    extract_subdir = os.path.join(tmp_dir, f"extracted_{i}")
                    os.makedirs(extract_subdir, exist_ok=True)

                    try:
                        with zipfile.ZipFile(zip_path, "r") as zf:
                            zf.extractall(extract_subdir)
                    except zipfile.BadZipFile:
                        logger.warning(f"Invalid zip file for artifact {details_str}")
                        continue

                    # Search for JSON logs
                    json_files = []
                    # Standard Playwright location relative to artifact root?
                    # Or just search recursively
                    for root, _, files in os.walk(extract_subdir):
                        for file in files:
                            if file.endswith(".json"):
                                json_path = os.path.join(root, file)
                                json_files.append(json_path)

                    # Sort for determinism per artifact
                    json_files.sort()

                    for jf in json_files:
                        try:
                            with open(jf, "r", encoding="utf-8") as f:
                                content = json.load(f)
                            # Verify simple heuristic that it's a playwright report
                            if "suites" in content and "errors" in content:
                                all_raw_artifacts.append(content)
                        except Exception as e:
                            logger.warning(f"Failed to parse JSON {jf}: {e}")

            if not all_raw_artifacts:
                logger.info("No valid Playwright JSON reports found in downloaded artifacts.")
                return None, None

            # 3. Generate Merged Report
            summary_text = generate_merged_playwright_report(all_raw_artifacts)

            return f"=== Job: Playwright Report ===\n{summary_text}", all_raw_artifacts

    except Exception as e:
        if "USER_STOP_REQUEST" in str(e):
            raise e
        logger.warning(f"Error handling Playwright artifacts: {e}")
        return f"Error downloading/parsing Playwright artifacts: {e}", None


def generate_merged_playwright_report(reports: List[Dict[str, Any]]) -> str:
    """Merge, summarize, and format multiple Playwright JSON reports.

    Includes deduplication of error logs based on location and optionally includes
    stdout/stderr logs if available.
    Also counts skipped, passed, flaky, and interrupted tests.
    """

    total_failures = 0
    total_passed = 0
    total_skipped = 0
    total_flaky = 0
    total_interrupted = 0

    # Map: file_path -> List of failure descriptions
    failed_specs: Dict[str, List[str]] = {}
    detailed_output = []

    # Track unique error locations to avoid duplicates in detailed output
    visited_locations: set = set()

    has_unknown_location = False

    # Helper to traverse
    def _recurse_suites(suites: List[Dict[str, Any]]):
        nonlocal total_failures, total_passed, total_skipped, total_flaky, total_interrupted, has_unknown_location
        for suite in suites:
            if "suites" in suite:
                _recurse_suites(suite["suites"])

            # Try to determine file context
            suite_file = suite.get("file")

            for spec in suite.get("specs", []):
                title = spec.get("title", "Unknown Test")
                spec_file = spec.get("file", suite_file or "Unknown File")

                # Check for flaky at spec level if available or verify via results
                # Playwright JSON often marks specs as flaky if retries occurred and eventually passed
                # But we'll count via results or check standard properties

                for test in spec.get("tests", []):
                    # Expected status
                    expected = test.get("expectedStatus", "passed")

                    # Check outcome of the test
                    # "outcome": "unexpected", "flaky", "expected", "skipped"
                    outcome = test.get("outcome")

                    if outcome == "skipped":
                        total_skipped += 1
                        continue
                    elif outcome == "flaky":
                        total_flaky += 1
                        # Flaky usually means it eventually passed, so we might not want to treat as failure
                        # But we want to count it.
                        continue
                    elif outcome == "expected" and expected == "passed":
                        total_passed += 1
                        continue

                    # If we are here, likely failure or unexpected
                    # But let's rely on individual results for precise error extraction

                    # Logic: one test can have multiple results (retries)
                    # If ANY result is failed, we might want to log it, unless it's flaky (handled above).
                    # If outcome is unexpected, it's a failure.

                    if outcome == "unexpected":
                        # Iterate results to find the failures
                        for result in test.get("results", []):
                            status = result.get("status")
                            if status in ["failed", "timedOut", "interrupted"]:
                                if status == "interrupted":
                                    total_interrupted += 1

                                # It is a failure
                                # We count it towards total failures IF it's the final result?
                                # Or counts per result?
                                # Usually "unexpected" implies the Test Unit failed.
                                # Let's count it once per Test if possible, but extracting errors from all results is ok.
                                # Let's increment total_failures per Error Block we generate to be consistent with previous logic,
                                # OR we should increment per Test.
                                # Let's increment per Test for the stats, but list all errors.
                                pass  # We will increment in the error loop below if distinct?
                                # Actually, simpler:
                                # total_failures refers to number of Failing Tests or number of Errors?
                                # User asks for "counts". Usually "X tests failed".
                                pass

                    # Re-iterate for exact error extraction logic (similar to before)
                    # We use the previous logic to find failed results and extract errors.

                    # Extract stdout/stderr once per test
                    std_out = []
                    if "stdout" in test:
                        std_out = [f"STDOUT: {entry.get('text', '')}" for entry in test.get("stdout", []) if entry.get("text")]
                    if "stderr" in test:
                        std_out.extend([f"STDERR: {entry.get('text', '')}" for entry in test.get("stderr", []) if entry.get("text")])

                    test_failed = False

                    for result in test.get("results", []):
                        if result.get("status") in ["failed", "timedOut", "interrupted"]:
                            test_failed = True

                            errors = result.get("errors", [])
                            if not errors and result.get("status") == "timedOut":
                                errors = [{"message": f"Test timed out ({result.get('duration', '?')}ms)"}]

                            current_failure_block = []

                            for error in errors:
                                msg = error.get("message", "")
                                stack = error.get("stack", "")

                                # Location
                                location = error.get("location", {})
                                loc_file = location.get("file", spec_file)
                                loc_file = _normalize_gh_path(loc_file)
                                loc_line = location.get("line", "?")
                                loc_col = location.get("column", "?")

                                if loc_line == "?" or loc_col == "?":
                                    has_unknown_location = True

                                loc_str = f"{loc_file}:{loc_line}:{loc_col}"

                                # Determine if this location has been reported
                                is_duplicate = loc_str in visited_locations

                                if not is_duplicate:
                                    visited_locations.add(loc_str)

                                # Update failed_specs for summary (always count for stats)
                                # We use spec_file (the test file) rather than loc_file (where error happened)
                                # because we want to know which TEST file failed.
                                clean_spec_file = _normalize_gh_path(spec_file)
                                if clean_spec_file not in failed_specs:
                                    failed_specs[clean_spec_file] = []
                                failed_specs[clean_spec_file].append(title)

                                # Only add details if not duplicate
                                if not is_duplicate:
                                    clean_msg = _clean_log_line(msg)

                                    # Use spec location for the File: field as requested
                                    spec_line = spec.get("line", "")
                                    spec_col = spec.get("column", "")
                                    if spec_line and spec_col:
                                        display_loc = f"{_normalize_gh_path(spec_file)}:{spec_line}:{spec_col}"
                                    else:
                                        display_loc = _normalize_gh_path(spec_file)

                                    current_failure_block.append(f"FAILED: {title}")
                                    current_failure_block.append(f"File: {display_loc}")
                                    current_failure_block.append(f"Error: {clean_msg}")

                                    if stack:
                                        clean_stack = "\n".join([_clean_log_line(line_item) for line_item in stack.split("\n")][:10])
                                        current_failure_block.append(f"Stack:\n{clean_stack}")

                                    if std_out:
                                        log_text = "\n".join(std_out)
                                        if len(log_text) > 1000:
                                            log_text = log_text[:1000] + "... (truncated)"

                                        current_failure_block.append("Logs:")
                                        current_failure_block.append(log_text)

                                    current_failure_block.append("-")

                            if current_failure_block:
                                detailed_output.extend(current_failure_block)

                    if test_failed:
                        total_failures += 1

    for report in reports:
        _recurse_suites(report.get("suites", []))

    if total_failures == 0 and total_flaky == 0 and total_interrupted == 0 and total_passed > 0 and total_skipped == 0:
        return f"All {total_passed} Playwright tests passed."

    # Build Summary
    summary_lines = []
    summary_lines.append(f"Total Playwright Failures: {total_failures}")
    if total_passed > 0:
        summary_lines.append(f"Passed: {total_passed}")
    if total_skipped > 0:
        summary_lines.append(f"Skipped: {total_skipped}")
    if total_flaky > 0:
        summary_lines.append(f"Flaky: {total_flaky}")
    if total_interrupted > 0:
        summary_lines.append(f"Interrupted: {total_interrupted}")

    if failed_specs:
        summary_lines.append("Failed Files:")
        sorted_files = sorted(failed_specs.keys())
        for f in sorted_files:
            titles = failed_specs[f]
            count = len(titles)
            summary_lines.append(f"- {f} ({count} failures)")

    summary_lines.append("\n--- Details ---")

    output_str = "\n".join(summary_lines) + "\n" + "\n".join(detailed_output)

    if has_unknown_location:
        output_str += "\n\nNote: Failures without specific location are likely the same as other failures in the same context."

    return output_str


def parse_playwright_json_report(report: Dict[str, Any]) -> str:
    """Wrapper for backward compatibility.
    Parse a single Playwright JSON report to extract failures.
    """
    return generate_merged_playwright_report([report])


def _extract_failed_tests_from_playwright_reports(reports: List[Dict[str, Any]]) -> List[str]:
    """Extract list of failed test files from Playwright JSON reports."""
    failed_tests = set()

    def _recurse(suites):
        for suite in suites:
            if "suites" in suite:
                _recurse(suite["suites"])

            suite_file = suite.get("file")

            for spec in suite.get("specs", []):
                spec_file = spec.get("file", suite_file)

                # Check tests
                for test in spec.get("tests", []):
                    # Check if failed
                    outcome = test.get("outcome")  # unexpected, flaky, expected, skipped
                    # If outcome is unexpected, it failed.
                    if outcome == "unexpected":
                        if spec_file:
                            failed_tests.add(spec_file)

    for report in reports:
        _recurse(report.get("suites", []))

    return sorted(list(failed_tests))


def _create_github_action_log_summary(
    repo_name: str,
    config: AutomationConfig,
    *args: Any,
    search_history: Optional[bool] = None,
    **kwargs: Any,
) -> Tuple[str, Optional[List[str]]]:
    """Create a formatted summary string from a list of log chunks."""
    # Extract failed_checks and optional pr_data from args
    failed_checks: List[Dict[str, Any]] = []
    pr_data: Optional[Dict[str, Any]] = None
    if len(args) >= 1 and isinstance(args[0], list):
        failed_checks = args[0]
    if len(args) >= 2 and isinstance(args[1], dict):
        pr_data = args[1]

    logs: List[str] = []
    artifacts_list: List[Dict[str, Any]] = []

    try:
        # Resolve owner/repo
        owner, repo = repo_name.split("/")
        token = GitHubClient.get_instance().token
        api = get_ghapi_client(token)

        # 1. Inspect failed_checks to verify if we have enough info
        # If no failed_checks, use fallback historical search if not already done?
        # But here assuming we have failed_checks from caller if not searching history.

        # If failed_checks are present but missing Run ID, we might need to find it?
        # But usually they come from _check_github_actions_status or similar which has details_url.

        # Group checks by Run ID
        run_checks_map: Dict[int, List[Dict[str, Any]]] = {}
        ungrouped_checks: List[Dict[str, Any]] = []

        for check in failed_checks:
            details_url = check.get("details_url", "")
            run_id = None
            if details_url:
                match = re.search(r"/actions/runs/(\d+)", details_url)
                if match:
                    run_id = int(match.group(1))

            if run_id:
                if run_id not in run_checks_map:
                    run_checks_map[run_id] = []
                run_checks_map[run_id].append(check)
            else:
                ungrouped_checks.append(check)

        # If no run IDs found, but we have PR data, try to find the latest failed run for this PR
        if not run_checks_map and not ungrouped_checks and pr_data:
            logger.info("No Run IDs in failed_checks, attempting to find failed run for PR")
            # Similar logic to _search_github_actions_logs_from_history but specifically for current state
            # Check logic:
            # run_list = api.actions.list_workflow_runs_for_repo(...)
            # ...
            # For now, let's rely on fallback logic below (lines 2033+) if map is empty.
            pass

        # 2. Process each Run
        if run_checks_map:
            for run_id, checks in run_checks_map.items():
                logger.info(f"Processing logs for Run {run_id}")

                # A. Get Playwright Artifacts
                playwright_summary, raw_artifacts = _get_playwright_artifact_logs(repo_name, run_id)
                if raw_artifacts:
                    artifacts_list.extend(raw_artifacts)

                # B. Get all jobs for this run (to get names, ids, order)
                try:
                    jobs_data = api.actions.list_jobs_for_workflow_run(owner=owner, repo=repo, run_id=run_id)
                    jobs = jobs_data.get("jobs", [])

                    # C. Sort jobs by workflow
                    jobs = _sort_jobs_by_workflow(jobs, owner, repo, run_id, token)

                    playwright_summary_printed = False

                    for job in jobs:
                        # Only process if failed
                        if job.get("conclusion") == "failure":
                            job_name = job.get("name", "").lower()
                            html_url = job.get("html_url")

                            is_playwright = "playwright" in job_name or "e2e" in job_name

                            if is_playwright:
                                if playwright_summary and "=== Job: Playwright Report ===" in playwright_summary:
                                    if not playwright_summary_printed:
                                        logs.append(playwright_summary)
                                        playwright_summary_printed = True
                                    continue
                                elif playwright_summary:
                                    # If summary exists but is likely an error message, log it but fall back to standard logs
                                    if not playwright_summary_printed:
                                        logs.append(f"Playwright Artifact Warning: {playwright_summary}")
                                        playwright_summary_printed = True
                                # If no summary, proceed to fetch logs normally

                            # Fetch logs for this job
                            if html_url:
                                logger.info(f"Fetching logs for failed job: {job.get('name')}")
                                job_log = get_github_actions_logs_from_url(html_url)
                                if job_log:
                                    logs.append(job_log)

                    # Fallback: if summary exists but wasn't printed (e.g. e2e job not found in failed list?), print it at end
                    if playwright_summary and not playwright_summary_printed:
                        logs.append(playwright_summary)

                except Exception as e:
                    logger.warning(f"Error processing jobs for run {run_id}: {e}")
                    # Fallback to appending check logs without sorting if run processing fails?
                    # For now, rely on ungrouped fallback/error logging?
                    pass

        # 3. Process Ungrouped Checks (Fallback for checks with non-standard URLs)
        if ungrouped_checks:
            logger.info(f"Processing {len(ungrouped_checks)} ungrouped check(s)")
            for check in ungrouped_checks:
                check_name = check.get("name", "Unknown")
                details_url = check.get("details_url", "")

                if details_url:
                    logger.info(f"Fetching logs for ungrouped check: {check_name}")
                    job_log = get_github_actions_logs_from_url(details_url)
                    if job_log:
                        logs.append(job_log)
                    else:
                        logs.append(f"=== {check_name} ===\nFailed to fetch logs from {details_url}")
                else:
                    logs.append(f"=== {check_name} ===\nNo details URL available.")

        # 4. Fallback / Legacy Logic (if nothing processed)
        if not logs and not run_checks_map and not ungrouped_checks:
            # Try old logic of iterating failed_checks directly or finding run via API
            # ... (Keep existing fallback logic if needed or simplify?)
            pass

    except Exception as e:
        logger.error(f"Error getting GitHub Actions logs: {e}")
        logs.append(f"Error getting logs: {e}")

    # Deduplicate similar logs
    if len(logs) > 1:
        try:
            final_logs = []
            kept_logs = []  # Stores only the full logs that were kept

            for i, log in enumerate(logs):
                # Parse job name for fallback
                match = re.search(r"=== Job: (.*?) ===", log)
                job_name = match.group(1) if match else "Unknown"

                # Check against kept logs
                is_duplicate = False

                # Extract body for comparison (skip header)
                log_body = log.split("\n", 1)[1] if "\n" in log else log

                # If log is too short (just "No detailed logs available"), don't deduplicate it aggressively?
                # Or maybe we WANT to deduplicate "No detailed logs available"?
                # Let's deduplicate everything.

                for kept_log in kept_logs:
                    if fuzz is None:
                        break

                    kept_body = kept_log.split("\n", 1)[1] if "\n" in kept_log else kept_log

                    # basic fuzz.ratio is Levenshtein distance based
                    ratio = fuzz.ratio(log_body, kept_body)
                    if ratio > 95:
                        is_duplicate = True
                        break

                if is_duplicate:
                    final_logs.append(f"=== Job: {job_name} ===\nFailure is similar to others (omitted).")
                else:
                    final_logs.append(log)
                    kept_logs.append(log)

            logs = final_logs
        except Exception as e:
            logger.warning(f"Error during log deduplication: {e}")

    failed_test_files = []
    if artifacts_list:
        failed_test_files = _extract_failed_tests_from_playwright_reports(artifacts_list)

    return "\n\n".join(logs) if logs else "No detailed logs available", failed_test_files if failed_test_files else None
