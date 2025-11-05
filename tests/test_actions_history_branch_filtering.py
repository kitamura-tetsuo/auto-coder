import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_history_uses_branch_filter_when_commit_runs_empty():
    """Verify that branch filtering works correctly even when --commit finds no runs.

    Regression: Prevent PR #73's history check from incorrectly referencing PR #133's Run.
    """
    config = AutomationConfig()

    pr_data = {
        "number": 73,
        "head_branch": "pr-73-branch",
        "head": {
            "ref": "pr-73-branch",
            "sha": "abcdef1234567890abcdef",
        },
    }

    # 1) --commit will not hit
    commit_run_list = _cmd_result(True, stdout="[]", stderr="", returncode=0)

    # 2) Normal run list mixes other PR's new Run and target PR's old Run
    other_pr_run_id = 19024332818  # Format close to real example
    target_pr_run_id = 18000000000

    run_list_payload = [
        {
            "databaseId": other_pr_run_id,
            "headBranch": "pr-133-branch",
            "conclusion": "success",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{other_pr_run_id}",
            "headSha": "133deadbeef",
        },
        {
            "databaseId": target_pr_run_id,
            "headBranch": "pr-73-branch",
            "conclusion": "success",
            "createdAt": "2025-11-04T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{target_pr_run_id}",
            "headSha": "73cafebabe",
        },
    ]
    run_list_result = _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)

    # 3) run view (jobs) should reference only target PR's Run
    jobs_payload_target = {
        "jobs": [
            {
                "databaseId": 54321000000,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    call_count = {"list": 0}

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            # Return PR commit information
            return _cmd_result(
                True,
                stdout=json.dumps(
                    {
                        "commits": [
                            {
                                "oid": "73cafebabe",
                            }
                        ]
                    }
                ),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            call_count["list"] += 1
            if call_count["list"] == 1:
                # 1st time (equivalent to commit) will not hit
                return commit_run_list
            # 2nd time (fallback) returns candidates
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            run_id = int(cmd[3])
            if run_id == target_pr_run_id:
                return _cmd_result(
                    True,
                    stdout=json.dumps(jobs_payload_target),
                    stderr="",
                    returncode=0,
                )
            # In case it fetches another PR's Run, return empty
            return _cmd_result(True, stdout=json.dumps({"jobs": []}), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.util.github_action.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # GitHubActionsStatusResult has only success and ids
    assert isinstance(result.ids, list)


def test_history_filters_to_branch_even_with_head_sha_present():
    """When head.sha exists but doesn't hit in commit, filter by branch for sure."""
    config = AutomationConfig()

    pr_data = {
        "number": 999,
        "head_branch": "fix-branch",
        "head": {
            "ref": "fix-branch",
            "sha": "abc123def456",
        },
    }

    commit_run_list = _cmd_result(True, stdout="[]", stderr="", returncode=0)

    # Mix same branch (push) and different branches (PR)
    run_list_payload = [
        {
            "databaseId": 3001,
            "headBranch": "other-branch",
            "conclusion": "failure",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": "https://github.com/owner/repo/actions/runs/3001",
            "headSha": "deadbeef",
        },
        {
            "databaseId": 3000,
            "headBranch": "fix-branch",
            "conclusion": "success",
            "createdAt": "2025-11-04T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": "https://github.com/owner/repo/actions/runs/3000",
            "headSha": "cafebabe",
        },
    ]
    run_list_result = _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)

    jobs_payload = {
        "jobs": [
            {
                "databaseId": 777,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    call_count = {"list": 0}

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            # Return PR commit information
            return _cmd_result(
                True,
                stdout=json.dumps(
                    {
                        "commits": [
                            {
                                "oid": "abc123def456",
                            }
                        ]
                    }
                ),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            call_count["list"] += 1
            if call_count["list"] == 1:
                # 1st time (equivalent to commit) will not hit
                return commit_run_list
            # 2nd time (fallback) returns candidates
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            # Should reference only 3000 filtered by branch
            run_id = int(cmd[3])
            assert run_id == 3000, f"unexpected run viewed: {run_id}"
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.util.github_action.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # GitHubActionsStatusResult has only success and ids
    assert isinstance(result.ids, list)
