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
from typing import Any, Dict, List, Optional

from auto_coder.progress_decorators import progress_stage

from ..automation_config import AutomationConfig
from ..gh_logger import get_gh_logger
from ..github_client import GitHubClient
from ..logger_config import get_logger
from ..utils import CommandExecutor, log_action
from .gh_cache import get_ghapi_client
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
        cache = get_github_cache()
        cache_key = f"gh_actions_status:{repo_name}:{pr_number}:{current_head_sha}"
        cached_result = cache.get(cache_key)
        if cached_result:
            logger.info(f"Using cached GitHub Actions status for {repo_name} PR #{pr_number} ({current_head_sha[:8]})")
            return cached_result

        # Use gh API to get check runs for the commit
        # gh pr checks does not support --json, so we use the API directly
        try:
            token = GitHubClient.get_instance().token
            api = get_ghapi_client(token)
            owner, repo = repo_name.split("/")

            # API: api.checks.list_for_ref(owner, repo, ref)
            res = api.checks.list_for_ref(owner, repo, ref=current_head_sha)
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
        logger.info(f"pr_data keys: {list(pr_data.keys())}")
        logger.info(f"pr_data['head'] keys if exists: {list(pr_data.get('head', {}).keys()) if 'head' in pr_data else 'No head key'}")
        assert pr_number
        assert head_branch

        logger.info(f"Checking historical GitHub Actions status for PR #{pr_number} on branch '{head_branch}'")

        # Check cache first (using head_branch as part of key since we might not have exact SHA yet,
        # but ideally we should use SHA if possible. However, this function is a fallback when we might lack info.
        # Let's use a composite key including head_branch.)
        cache = get_github_cache()
        # Note: Historical check is more expensive, so caching is valuable.
        # But since it depends on "latest" state, it might change.
        # However, we clear cache at end of loop, so it's safe for one iteration.
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
        cache.set(cache_key, result)

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
    """Get detailed checks information from run IDs using gh run view.

    This function takes a GitHubActionsStatusResult with run IDs and fetches
    detailed information using gh run view to get jobs and their statuses.

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
    logger.info("Getting detailed checks from run IDs using gh run view")

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
                        "conclusion": conclusion if conclusion else status,
                        "details_url": (f"https://github.com/{repo_name}/actions/runs/{run_id}/job/{job_id}" if job_id else ""),
                        "run_id": run_id,
                        "job_id": job_id,
                        "status": status,
                    }

                    all_checks.append(check_info)

                    # Track overall status
                    if conclusion in ["failure", "failed", "error", "cancelled"]:
                        any_failed = True
                        all_failed_checks.append(check_info)
                    elif status in ["in_progress", "queued", "pending"]:
                        has_in_progress = True
                    elif not conclusion and status in [
                        "failure",
                        "failed",
                        "error",
                        "cancelled",
                    ]:
                        any_failed = True
                        all_failed_checks.append(check_info)

            except Exception as e:
                logger.warning(f"Failed to get jobs via GhApi for run {run_id}: {e}")
                # Fallback: create a check based on run conclusion
                gh_logger = get_gh_logger()
                run_result = gh_logger.execute_with_logging(
                    ["gh", "run", "view", str(run_id), "-R", repo_name, "--json", "conclusion,status"],
                    repo=repo_name,
                    timeout=60,
                    capture_output=True,
                )
                if run_result.success and run_result.stdout.strip():  # type: ignore[attr-defined]
                    try:
                        run_json = json.loads(run_result.stdout)
                        run_conclusion = run_json.get("conclusion", "").lower()
                        run_status = run_json.get("status", "").lower()

                        check_info = {
                            "name": f"Run {run_id}",
                            "conclusion": (run_conclusion if run_conclusion else run_status),
                            "details_url": f"https://github.com/{repo_name}/actions/runs/{run_id}",
                            "run_id": run_id,
                            "job_id": None,
                            "status": run_status,
                        }
                        all_checks.append(check_info)

                        if run_conclusion in ["failure", "failed", "error", "cancelled"]:
                            any_failed = True
                            all_failed_checks.append(check_info)
                        elif run_status in ["in_progress", "queued", "pending"]:
                            has_in_progress = True

                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse run JSON for run {run_id}: {e}")

        # Determine overall success
        # Success if no failures and no in-progress checks (and we found checks)
        # If no checks found at all, it's technically "success" in terms of "no failures",
        # but usually we want to know if checks passed.
        # However, keeping consistent with status_result.success:
        final_success = not any_failed and not has_in_progress

        return DetailedChecksResult(
            success=final_success,
            total_checks=len(all_checks),
            failed_checks=all_failed_checks,
            all_checks=all_checks,
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
        workflow_id: Workflow ID or filename (e.g., 'pr-tests.yml')
        ref: Git reference (branch or tag) to run the workflow on

    Returns:
        True if triggered successfully, False otherwise
    """
    try:
        logger.info(f"Triggering workflow '{workflow_id}' on '{ref}' for {repo_name}")

        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            [
                "gh",
                "workflow",
                "run",
                workflow_id,
                "--ref",
                ref,
                "--repo",
                repo_name,
            ],
            repo=repo_name,
            capture_output=True,
        )

        if result.returncode == 0:
            logger.info(f"Successfully triggered workflow '{workflow_id}'")
            return True
        else:
            logger.error(f"Failed to trigger workflow: {result.stderr}")
            return False

    except Exception as e:
        logger.error(f"Error triggering workflow '{workflow_id}': {e}")
        return False


def get_github_actions_logs_from_url(url: str) -> str:
    """Extract error blocks by fetching logs for the given GitHub Actions job URL directly.

    Accepted URL format:
    https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
    """
    try:
        # 1) Try to match Job URL first (specific job)
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
                    gh_logger = get_gh_logger()
                    jobs_res = gh_logger.execute_with_logging(
                        ["gh", "run", "view", run_id, "-R", owner_repo, "--json", "jobs"],
                        repo=owner_repo,
                        timeout=60,
                        capture_output=True,
                    )
                    if jobs_res.returncode == 0 and jobs_res.stdout.strip():
                        jobs_json = json.loads(jobs_res.stdout)
                        failed_jobs = [j for j in jobs_json.get("jobs", []) if j.get("conclusion") == "failure"]
                        
                        if failed_jobs:
                            logs_list = []
                            for job in failed_jobs:
                                # extracting job_id
                                # Note: 'databaseId' is consistent with how we use it later, but 'id' might be used loosely
                                j_id = job.get("databaseId") or job.get("id")
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

        # 1) Get job name if possible
        job_name = f"job-{job_id}"
        try:
            gh_logger = get_gh_logger()
            jobs_res = gh_logger.execute_with_logging(
                ["gh", "run", "view", run_id, "-R", owner_repo, "--json", "jobs"],
                repo=owner_repo,
                timeout=60,
                capture_output=True,
            )
            if jobs_res.returncode == 0 and jobs_res.stdout.strip():
                jobs_json = json.loads(jobs_res.stdout)
                for job in jobs_json.get("jobs", []):
                    if str(job.get("databaseId")) == str(job_id):
                        job_name = job.get("name") or job_name
                        break
        except Exception:
            pass

        # 1.5) Identify failing step names (if possible)
        failing_step_names: set = set()
        try:
            gh_logger = get_gh_logger()
            job_detail = gh_logger.execute_with_logging(
                ["gh", "api", f"repos/{owner_repo}/actions/jobs/{job_id}"],
                repo=owner_repo,
                timeout=60,
                capture_output=True,
            )
            if job_detail.returncode == 0 and job_detail.stdout.strip():
                job_json = json.loads(job_detail.stdout)
                steps = job_json.get("steps", []) or []
                for st in steps:
                    # steps[].conclusion: success|failure|cancelled|skipped|None
                    if (st.get("conclusion") == "failure") or (st.get("conclusion") is None and st.get("status") == "completed" and job_json.get("conclusion") == "failure"):
                        nm = st.get("name")
                        if nm:
                            failing_step_names.add(nm)
        except Exception:
            # Continue even if unable to get (extract using conventional heuristics)
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

        # 2) First, get job ZIP logs directly
        # GitHub API /logs endpoint returns binary (ZIP),
        # so it needs to be obtained as binary via subprocess
        api_cmd = ["gh", "api", f"repos/{owner_repo}/actions/jobs/{job_id}/logs"]
        try:
            gh_logger = get_gh_logger()
            # Use logged_subprocess for binary data
            with gh_logger.logged_subprocess(
                api_cmd,
                repo=owner_repo,
                capture_output=True,
                timeout=120,
            ) as result:
                if result.returncode == 0 and result.stdout:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        zip_path = os.path.join(tmpdir, "job_logs.zip")
                        with open(zip_path, "wb") as f:
                            f.write(result.stdout)
                    try:
                        with zipfile.ZipFile(zip_path, "r") as zf:
                            step_snippets = []
                            job_summary_lines = []
                            for name in zf.namelist():
                                if name.lower().endswith(".txt"):
                                    with zf.open(name, "r") as fp:
                                        try:
                                            content = fp.read().decode("utf-8", errors="ignore")
                                        except Exception:
                                            content = ""
                                    if not content:
                                        continue
                                    step_file_label = os.path.splitext(os.path.basename(name))[0]
                                    # Step filter: target only files from failing steps
                                    if not _file_matches_fail(step_file_label, content):
                                        continue
                                    # Collect job-wide summary candidates (maintain order)
                                    for ln in content.split("\n"):
                                        ll = ln.lower()
                                        if ((" failed" in ll) or (" passed" in ll) or (" skipped" in ll) or (" did not run" in ll)) and any(ch.isdigit() for ch in ln):
                                            job_summary_lines.append(ln)
                                    step_name = step_file_label
                                    # Extract important error-related information
                                    snippet = _extract_error_context(content)
                                    # Enhance with expected/received original lines (for strict matching)
                                    exp_lines = []
                                    for ln in content.split("\n"):
                                        if ("Expected substring:" in ln) or ("Received string:" in ln):
                                            exp_lines.append(ln)
                                    if exp_lines:
                                        # Also add normalized lines with backslash escapes removed
                                        norm_lines = [ln.replace('\\"', '"') for ln in exp_lines]
                                        if "--- Expectation Details ---" not in snippet:
                                            snippet = (snippet + "\n\n--- Expectation Details ---\n" if snippet else "") + "\n".join(norm_lines)
                                        else:
                                            snippet = snippet + "\n" + "\n".join(norm_lines)
                                    # Don't output steps without errors (stricter)
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
                                # Add job-wide summary at the end (up to max lines in order of last appearance)
                                summary_block = ""
                                summary_lines = []
                                if job_summary_lines:
                                    # Remove duplicates from back, reproduce latest order
                                    seen = set()
                                    uniq_rev = []
                                    for ln in reversed(job_summary_lines):
                                        if ln not in seen:
                                            seen.add(ln)
                                            uniq_rev.append(ln)
                                    summary_lines = list(reversed(uniq_rev))
                                # If can't get from ZIP, supplement summary from text logs
                                if not summary_lines:
                                    try:
                                        gh_logger = get_gh_logger()
                                        job_txt2 = gh_logger.execute_with_logging(
                                            [
                                                "gh",
                                                "run",
                                                "view",
                                                run_id,
                                                "-R",
                                                owner_repo,
                                                "--job",
                                                str(job_id),
                                                "--log",
                                            ],
                                            repo=owner_repo,
                                            timeout=120,
                                        )
                                        if job_txt2.returncode == 0 and job_txt2.stdout.strip():
                                            # Extract summary only from lines filtered by failing step name
                                            for ln in job_txt2.stdout.split("\n"):
                                                parts = ln.split("\t", 2)
                                                if len(parts) >= 3:
                                                    step_field = parts[1].strip().lower()
                                                    if any(n and (n in step_field or step_field in n) for n in norm_fail_names):
                                                        ll = ln.lower()
                                                        if (
                                                            (" failed" in ll)
                                                            or (" passed" in ll)
                                                            or (" skipped" in ll)
                                                            or (" did not run" in ll)
                                                            or ("notice" in ll)
                                                            or ("error was not a part of any test" in ll)
                                                            or ("command failed with exit code" in ll)
                                                            or ("process completed with exit code" in ll)
                                                        ):
                                                            summary_lines.append(ln)
                                    except Exception:
                                        pass
                                body_str = "\n\n".join(step_snippets)
                                if summary_lines:
                                    # Exclude lines contained in body from summary
                                    filtered = [ln for ln in summary_lines[-15:] if ln not in body_str]
                                    summary_block = ("\n\n--- Summary ---\n" + "\n".join(filtered)) if filtered else ""
                                else:
                                    summary_block = ""
                                body = body_str + summary_block
                                body = slice_relevant_error_window(body)
                                return f"=== Job {job_name} ({job_id}) ===\n" + body
                            # If step_snippets is empty, extract error context from all
                            all_text = []
                            for name in zf.namelist():
                                if name.lower().endswith(".txt"):
                                    with zf.open(name, "r") as fp:
                                        try:
                                            content = fp.read().decode("utf-8", errors="ignore")
                                        except Exception:
                                            content = ""
                                        all_text.append(content)
                            combined = "\n".join(all_text)
                            # Extract error context
                            important = _extract_error_context(combined)
                            if not important or not important.strip():
                                # If error context not found, return first 1000 characters
                                important = combined[:1000]
                            important = slice_relevant_error_window(important)
                            return f"=== Job {job_name} ({job_id}) ===\n{important}"
                    except zipfile.BadZipFile:
                        # If ZIP file not returned, but raw text log returned
                        try:
                            content = result.stdout.decode("utf-8", errors="ignore")
                            if content and content.strip():
                                # Extract only logs from failed steps
                                snippet = _extract_failed_step_logs(content, list(failing_step_names))
                                if snippet and snippet.strip():
                                    return f"=== Job {job_name} ({job_id}) ===\n{snippet}"
                                else:
                                    # Use conventional method if failed step not found
                                    snippet = _extract_error_context(content)
                                    if snippet and snippet.strip():
                                        snippet = slice_relevant_error_window(snippet)
                                        return f"=== Job {job_name} ({job_id}) ===\n{snippet}"
                                    else:
                                        # Return entire content even if error context not found
                                        snippet = slice_relevant_error_window(content)
                                        return f"=== Job {job_name} ({job_id}) ===\n{snippet}"
                        except Exception as e:
                            logger.warning(f"Failed to process raw text log: {e}")
        except Exception:
            pass

        # 3) Fallback: job text logs
        try:
            gh_logger = get_gh_logger()
            job_txt = gh_logger.execute_with_logging(
                [
                    "gh",
                    "run",
                    "view",
                    run_id,
                    "-R",
                    owner_repo,
                    "--job",
                    str(job_id),
                    "--log",
                ],
                repo=owner_repo,
                timeout=120,
            )
            if job_txt.returncode == 0 and job_txt.stdout.strip():
                text_output = job_txt.stdout
                # Fallback (text log) also filter to lines from failed steps (supports tab-separated format)
                text_for_extract = text_output
                try:
                    if norm_fail_names:
                        kept = []
                        parsed = 0
                        for ln in text_output.split("\n"):
                            parts = ln.split("\t", 2)
                            if len(parts) >= 3:
                                parsed += 1
                                step_field = parts[1].strip().lower()
                                if any(n and (n in step_field or step_field in n) for n in norm_fail_names):
                                    kept.append(ln)
                        # Only apply if sufficient lines can be parsed in tab format and filter results obtained
                        if parsed > 10 and kept:
                            text_for_extract = "\n".join(kept)
                            # Add header (concatenate if multiple failed steps)
                            if failing_step_names:
                                hdr = f"--- Step {', '.join(sorted(failing_step_names))} ---\n"
                                text_for_extract = hdr + text_for_extract
                except Exception:
                    pass

                # Split and output blocks for each failed step (text log path)
                blocks = []
                if norm_fail_names:
                    step_to_lines: Dict[str, List[str]] = {}
                    for ln in text_for_extract.split("\n"):
                        parts = ln.split("\t", 2)
                        if len(parts) >= 3:
                            step_field = parts[1].strip()
                            step_key = step_field
                            step_to_lines.setdefault(step_key, []).append(ln)
                    if step_to_lines:
                        for step_key in sorted(step_to_lines.keys()):
                            body_lines = step_to_lines[step_key]
                            # For each block, extract important parts & enhance expected/received
                            body_text = "\n".join(body_lines)
                            # blk_imp = _extract_important_errors({'success': False, 'output': body_text, 'errors': ''})
                            blk_imp = body_text[:500]  # Simplified for now
                            if ("Expected substring:" in body_text) or ("Received string:" in body_text) or ("expect(received)" in body_text):
                                extra = []
                                src_lines = body_text.split("\n")
                                for i, ln2 in enumerate(src_lines):
                                    if ("Expected substring:" in ln2) or ("Received string:" in ln2) or ("expect(received)" in ln2):
                                        s2 = max(0, i - 2)
                                        e2 = min(len(src_lines), i + 8)
                                        extra.extend(src_lines[s2:e2])
                                if extra:
                                    norm_extra = [ln.replace('"', '"') for ln in extra]
                                    if "--- Expectation Details ---" not in blk_imp:
                                        blk_imp = (blk_imp + ("\n\n--- Expectation Details ---\n" if blk_imp else "")) + "\n".join(norm_extra)
                                    else:
                                        blk_imp = blk_imp + "\n" + "\n".join(norm_extra)
                                if blk_imp and blk_imp.strip():
                                    blocks.append(f"--- Step {step_key} ---\n{blk_imp}")
                if blocks:
                    important = "\n\n".join(blocks)

                # Don't miss expected/received lines
                # important = _extract_important_errors({'success': False, 'output': text_for_extract, 'errors': ''})
                important = text_for_extract[:1000]  # Simplified for now
                if ("Expected substring:" in text_for_extract) or ("Received string:" in text_for_extract) or ("expect(received)" in text_for_extract):
                    extra = []
                    src_lines = text_for_extract.split("\n")
                    for i, ln in enumerate(src_lines):
                        if ("Expected substring:" in ln) or ("Received string:" in ln) or ("expect(received)" in ln):
                            s = max(0, i - 2)  # type: ignore[assignment]
                            e = min(len(src_lines), i + 8)  # type: ignore[misc]
                            extra.extend(src_lines[s:e])  # type: ignore[misc]
                    if extra:
                        norm_extra = [ln.replace('\\"', '"') for ln in extra]
                        if "--- Expectation Details ---" not in important:
                            important = (important + ("\n\n--- Expectation Details ---\n" if important else "")) + "\n".join(norm_extra)
                        else:
                            important = important + "\n" + "\n".join(norm_extra)
                else:
                    # If expected/received not found, extract error vicinity from filtered text
                    important = slice_relevant_error_window(text_for_extract)
                # Supplement Playwright summary (few lines) at end (full scan, maintain order)
                summary_lines = []
                for ln in text_output.split("\n"):
                    ll = ln.lower()
                    if (" failed" in ll) or (" passed" in ll) or (" skipped" in ll) or (" did not run" in ll) or ("notice:" in ll) or ("error was not a part of any test" in ll) or ("command failed with exit code" in ll) or ("process completed with exit code" in ll):
                        summary_lines.append(ln)
                if summary_lines:
                    # Supplement Playwright summary (few lines) at end (only failed step lines, exclude lines in body)
                    summary_lines = []
                    for ln in text_output.split("\n"):
                        parts = ln.split("\t", 2)
                        if len(parts) >= 3:
                            step_field = parts[1].strip().lower()
                            if any(n and (n in step_field or step_field in n) for n in norm_fail_names):
                                ll = ln.lower()
                                if (" failed" in ll) or (" passed" in ll) or (" skipped" in ll) or (" did not run" in ll) or ("notice:" in ll) or ("error was not a part of any test" in ll) or ("command failed with exit code" in ll) or ("process completed with exit code" in ll):
                                    summary_lines.append(ln)
                    if summary_lines:
                        body_now = important
                        filtered = [ln for ln in summary_lines[-15:] if ln not in body_now]
                        if filtered:
                            important = important + ("\n\n--- Summary ---\n" if "--- Summary ---" not in important else "\n") + "\n".join(filtered)
                # Truncate prelude (final formatting)
                important = slice_relevant_error_window(important)
                return f"=== Job {job_name} ({job_id}) ===\n{important}"
        except Exception:
            pass

        # 4) Further fallback: run-wide failure logs
        try:
            gh_logger = get_gh_logger()
            run_failed = gh_logger.execute_with_logging(
                ["gh", "run", "view", run_id, "-R", owner_repo, "--log-failed"],
                repo=owner_repo,
                timeout=120,
                capture_output=True,
            )
            if run_failed.returncode == 0 and run_failed.stdout.strip():
                # important = _extract_important_errors({'success': False, 'output': run_failed.stdout, 'errors': ''})
                important = run_failed.stdout[:1000]  # Simplified for now
                return f"=== Job {job_name} ({job_id}) ===\n{important}"
        except Exception:
            pass

        # 5) Last resort: run ZIP
        try:
            # GitHub API /logs endpoint returns binary (ZIP), so
            # it needs to be obtained as binary via subprocess
            gh_logger = get_gh_logger()
            # Use logged_subprocess for binary data
            with gh_logger.logged_subprocess(
                ["gh", "api", f"repos/{owner_repo}/actions/runs/{run_id}/logs"],
                repo=owner_repo,
                capture_output=True,
                timeout=120,
            ) as result2:
                if result2.returncode == 0 and result2.stdout:
                    with tempfile.TemporaryDirectory() as t2:
                        zp = os.path.join(t2, "run_logs.zip")
                        with open(zp, "wb") as wf:
                            wf.write(result2.stdout)
                    with zipfile.ZipFile(zp, "r") as zf2:
                        texts = []
                        for nm in zf2.namelist():
                            if nm.lower().endswith(".txt"):
                                with zf2.open(nm, "r") as fp2:
                                    try:
                                        texts.append(fp2.read().decode("utf-8", errors="ignore"))
                                    except Exception:
                                        pass
                        # imp = _extract_important_errors({'success': False, 'output': '\n'.join(texts), 'errors': ''})
                        imp = "\n".join(texts)[:1000]  # Simplified for now
                        imp = slice_relevant_error_window(imp)
                        return f"=== Job {job_name} ({job_id}) ===\n{url}\n{imp}"
        except Exception:
            pass

        return f"=== Job {job_name} ({job_id}) ===\n{url}\nNo detailed logs available"

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
                        logs = get_github_actions_logs_from_url(run_url)

                        if logs and "No detailed logs available" not in logs and "Invalid GitHub Actions job URL" not in logs:
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


def _get_playwright_artifact_logs(repo_name: str, run_id: int) -> Optional[str]:
    """Download and parse Playwright JSON logs from GitHub Artifacts using direct API calls.

    Args:
        repo_name: Repository name (owner/repo)
        run_id: GitHub Action run ID

    Returns:
        Formatted log string if successful, None otherwise.
        Raises specific exceptions if download fails which should stop the process.
    """
    logger.info(f"Attempting to download Playwright artifacts for run {run_id}")

    try:
        # Use GitHubClient to get token and headers
        client = GitHubClient.get_instance()
        token = client.token
        
        # Use httpx for direct API calls (similar to client._caching_requester but we need raw access)
        # We can use a fresh client or try to reuse one if exposed, but a fresh one for this op is safe.
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
                    return None
                data = response.json()
        except ImportError:
            # Fallback if httpx not available (though it is used in GitHubClient)
             logger.error("httpx module required but not found (unexpected).")
             return None
        except Exception as e:
             logger.warning(f"Exception listing artifacts: {e}")
             return None

        artifacts = data.get("artifacts", [])
        
        # Filter for e2e-artifacts-*
        target_artifact = None
        for artifact in artifacts:
            name = artifact.get("name", "")
            if name.startswith("e2e-artifacts-") and not artifact.get("expired", False):
                target_artifact = artifact
                break
        
        if not target_artifact:
            logger.info("No active e2e-artifacts-* found for this run.")
            return None

        artifact_id = target_artifact.get("id")
        artifact_name = target_artifact.get("name")
        logger.info(f"Found artifact: {artifact_name} (ID: {artifact_id})")

        # 2. Download the artifact (zip)
        download_url = f"{api_base}/repos/{repo_name}/actions/artifacts/{artifact_id}/zip"
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            zip_path = os.path.join(tmp_dir, "logs.zip")
            
            try:
                with httpx.Client(follow_redirects=True) as h_client:
                    # Note: GitHub API redirects to blob storage for download
                    dl_response = h_client.get(download_url, headers=headers, timeout=300)
                    
                    if dl_response.status_code != 200:
                         logger.error(f"Failed to download artifact: {dl_response.status_code} {dl_response.text}")
                         raise RuntimeError(f"USER_STOP_REQUEST: Failed to download artifact {artifact_name}. Status: {dl_response.status_code}")
                    
                    with open(zip_path, "wb") as f:
                        for chunk in dl_response.iter_bytes():
                            f.write(chunk)
                            
            except Exception as e:
                 if "USER_STOP_REQUEST" in str(e):
                      raise e
                 logger.error(f"Exception downloading artifact: {e}")
                 raise RuntimeError(f"USER_STOP_REQUEST: Failed to download artifact {artifact_name}. Error: {e}")

            # 3. Extract and Parse
            extracted_dir = os.path.join(tmp_dir, "extracted")
            os.makedirs(extracted_dir, exist_ok=True)
            
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extracted_dir)
            except zipfile.BadZipFile:
                 raise RuntimeError(f"USER_STOP_REQUEST: Downloaded artifact {artifact_name} is not a valid zip file.")

            # Search for JSON logs in job_logs folder
            job_logs_dir = os.path.join(extracted_dir, "job_logs")
            json_files = []
            
            if os.path.exists(job_logs_dir):
                for root, _, files in os.walk(job_logs_dir):
                    for file in files:
                        if file.endswith(".json"):
                            json_files.append(os.path.join(root, file))
            else:
                # Fallback: search anywhere
                 for root, _, files in os.walk(extracted_dir):
                    for file in files:
                        if file.endswith(".json"):
                            json_files.append(os.path.join(root, file))
            
            if not json_files:
                logger.info("No JSON logs found in artifact.")
                return None
            
            log_output = []
            for jf in json_files:
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        content = json.load(f)
                    
                    if "suites" in content and "errors" in content:
                        parsed_text = _parse_playwright_json_content(content)
                        if parsed_text:
                            # Extract useful title for the block
                            # Maybe "New Test" vs "Core Test" based on file name or content?
                            log_output.append(f"--- Artifact Log: {os.path.basename(jf)} ---\n{parsed_text}")
                except Exception as e:
                    logger.warning(f"Failed to parse JSON log {jf}: {e}")
            
            if log_output:
                return "\n\n".join(log_output)
            
            return None

    except Exception as e:
        if "USER_STOP_REQUEST" in str(e):
             raise e
        logger.warning(f"Error handling Playwright artifacts: {e}")
        return None


def _parse_playwright_json_content(report: Dict[str, Any]) -> str:
    """Parse Playwright JSON report to extract failures."""
    output = []
    
    def _recurse_suites(suites: List[Dict[str, Any]]):
        for suite in suites:
            # Suite might have sub-suites
            if "suites" in suite:
                _recurse_suites(suite["suites"])
            
            # Suite might have specs
            for spec in suite.get("specs", []):
                title = spec.get("title", "Unknown Test")
                file_path = spec.get("file", "Unknown File")
                
                for test in spec.get("tests", []):
                    # Check results
                    for result in test.get("results", []):
                         # Status can be 'failed', 'timedOut', 'interrupted' etc.
                         # We want failed ones. 'passed' is obviously skipped.
                        if result.get("status") in ["failed", "timedOut", "interrupted"]:
                             # Extract errors
                             errors = result.get("errors", [])
                             # Sometimes errors list is empty but status is failed (e.g. timeout without specific error obj?)
                             if not errors and result.get("status") == "timedOut":
                                 errors = [{"message": f"Test timed out ({result.get('duration', '?')}ms)"}]
                                 
                             for error in errors:
                                 msg = error.get("message", "")
                                 stack = error.get("stack", "")
                                 location = error.get("location", {})
                                 loc_str = f"{file_path}:{location.get('line', '?')}:{location.get('column', '?')}"
                                 
                                 clean_msg = _clean_log_line(msg)
                                 
                                 output.append(f"FAILED: {title}")
                                 output.append(f"File: {loc_str}")
                                 output.append(f"Error: {clean_msg}")
                                 if stack:
                                      clean_stack = "\n".join([_clean_log_line(l) for l in stack.split("\n")][:10])
                                      output.append(f"Stack:\n{clean_stack}")
                                 output.append("-" * 40)
    
    _recurse_suites(report.get("suites", []))
    
    return "\n".join(output)


def _get_github_actions_logs(
    repo_name: str,
    config: AutomationConfig,
    *args: Any,
    search_history: Optional[bool] = None,
    **kwargs: Any,
) -> str:
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
        String containing GitHub Actions logs

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
            return "No detailed logs available"

        # Try historical search first
        historical_logs = _search_github_actions_logs_from_history(repo_name, config, failed_checks, pr_data, max_runs=10)

        if historical_logs:
            logger.info("Historical search succeeded: Found logs from commit history")
            return historical_logs

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
        return "No detailed logs available"

    logs: List[str] = []

    try:
        # 1) Try to get Playwright artifact logs first if we can identify a run ID
        # We need a run ID. failed_checks items usually have 'details_url' which contains run_id.
        # details_url structure: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
        run_ids = set()
        for check in failed_checks:
            details_url = check.get("details_url", "")
            match = re.search(r"/actions/runs/(\d+)", details_url)
            if match:
                run_ids.add(match.group(1))
        
        # If we found run IDs, try artifacts
        for run_id in run_ids:
            try:
                artifact_logs = _get_playwright_artifact_logs(repo_name, int(run_id))
                if artifact_logs:
                     logs.append(artifact_logs)
            except Exception as e:
                if "USER_STOP_REQUEST" in str(e):
                    # Propagate this specific error up
                    return f"STOP: {str(e)}"
                # Otherwise, continue to other methods
                logger.warning(f"Could not get artifact logs: {e}")

        # If we successfully got artifact logs, we might rely entirely on them for Playwright tests?
        # But we might have other failures too. So let's APPEND existing log methods if we didn't fill everything?
        # Or maybe priority?
        # If we got clean artifact logs, they are usually much better.
        
        # NOTE: If we found enough info, maybe return? But safer to combine with textual logs just in case.
        
        # 2) First extract run_id and job_id directly from failed_checks details_url (Existing method)        # details_url format: https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>
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

    return "\n\n".join(logs) if logs else "No detailed logs available"


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
    lines = [_clean_log_line(ln) for ln in text.split("\n")]
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
        for i in range(len(lines)):
            low = lines[i].lower()
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
        gh_logger = get_gh_logger()
        # Fetch recent runs
        # Limit 100 should cover most active PRs.
        run_list_result = gh_logger.execute_with_logging(
            [
                "gh",
                "run",
                "list",
                "--limit",
                "100",
                "--json",
                "databaseId,url,status,conclusion,headSha,name",
            ],
            repo=repo_name,
            capture_output=True,
        )

        if run_list_result.returncode != 0:
            logger.warning(f"Failed to preload GitHub Actions status: {run_list_result.stderr}")
            return

        try:
            runs = json.loads(run_list_result.stdout)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from gh run list during preload")
            return

        # Group runs by SHA
        runs_by_sha = {}
        for run in runs:
            head_sha = run.get("headSha")
            if head_sha in sha_to_pr:
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
                if run.get("databaseId"):
                    run_ids.append(int(run["databaseId"]))

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
        detailed_checks = get_detailed_checks_from_history(github_checks, repo_name)

        # If GitHub Actions are still in progress
        if detailed_checks.has_in_progress:
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
            repo = github_client.get_repository(repo_name)
            if item_type == "pr":
                pr = repo.get_pull(item_number)
                current_item = github_client.get_pr_details(pr)
            elif item_type == "issue":
                issue = repo.get_issue(item_number)
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
