import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_history_handles_missing_pullRequests_field_gracefully():
    """run view's pullRequests field can be evaluated from job information even if missing."""
    config = AutomationConfig()

    pr_data = {
        "number": 42,
        "head": {
            "ref": "feature-branch",
            "sha": "abc123def456",
        },
    }

    # 1) --commit side will not hit
    commit_run_list = _cmd_result(True, stdout="[]", stderr="", returncode=0)

    # 2) Normal run list finds 1 PR event run for target branch
    run_id = 424242
    run_list_payload = [
        {
            "databaseId": run_id,
            "headBranch": "feature-branch",
            "conclusion": "success",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "headSha": "abc123def456",
            "event": "pull_request",
        }
    ]
    run_list_result = _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)

    # 3) run view does not return pullRequests field (missing)
    jobs_payload = {
        "jobs": [
            {
                "databaseId": 1001,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    call_count = {"list": 0}

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"] and "commits" in cmd:
            # Handle gh pr view command for commits
            return _cmd_result(
                True,
                stdout=json.dumps({"commits": [{"oid": "abc123def456"}]}),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            call_count["list"] += 1
            # Check if this is a branch-based call (-b option)
            if "-b" in cmd:
                # Branch-based run list - should return the test data
                return run_list_result
            elif call_count["list"] == 1:
                # 1st time (equivalent to commit) will not hit
                return commit_run_list
            else:
                # 2nd time (fallback) returns candidates
                return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("auto_coder.gh_logger.subprocess.run", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert run_id in result.ids


def test_history_handles_empty_pullRequests_list_gracefully():
    """run view can be evaluated from job information even if it returns pullRequests: []."""
    config = AutomationConfig()

    pr_data = {
        "number": 43,
        "head": {
            "ref": "topic-branch",
            "sha": "cafebabefeed",
        },
    }

    commit_run_list = _cmd_result(True, stdout="[]", stderr="", returncode=0)

    run_id = 434343
    run_list_payload = [
        {
            "databaseId": run_id,
            "headBranch": "topic-branch",
            "conclusion": "success",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "headSha": "cafebabefeed",
            "event": "pull_request",
        }
    ]
    run_list_result = _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)

    # pullRequests is empty array
    jobs_payload = {
        "pullRequests": [],
        "jobs": [
            {
                "databaseId": 2002,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ],
    }

    call_count = {"list": 0}

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"] and "commits" in cmd:
            # Handle gh pr view command for commits
            return _cmd_result(
                True,
                stdout=json.dumps({"commits": [{"oid": "cafebabefeed"}]}),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            call_count["list"] += 1
            # Check if this is a branch-based call (-b option)
            if "-b" in cmd:
                # Branch-based run list - should return the test data
                return run_list_result
            elif call_count["list"] == 1:
                return commit_run_list
            else:
                return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("auto_coder.gh_logger.subprocess.run", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert run_id in result.ids
