import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_history_prefers_pull_request_event_runs():
    """When event field exists, prefer pull_request runs. Also, only adopt runs where run view's pullRequests include the target PR."""
    config = AutomationConfig()

    pr_number = 321
    pr_data = {
        "number": pr_number,
        "head_branch": "feature-branch",
        "head": {
            "ref": "feature-branch",
            "sha": "abc123",
        },
    }

    # run list: new push run and old pull_request run (same branch)
    push_run_id = 4400
    pr_run_id = 4300
    run_list_payload = [
        {
            "databaseId": push_run_id,
            "headBranch": "feature-branch",
            "conclusion": "success",
            "createdAt": "2025-11-06T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{push_run_id}",
            "headSha": "abc123",
            "event": "push",
        },
        {
            "databaseId": pr_run_id,
            "headBranch": "feature-branch",
            "conclusion": "success",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{pr_run_id}",
            "headSha": "abc123",
            "event": "pull_request",
        },
    ]
    run_list_result = _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)

    # run view response: push does not include target PR / pull_request includes it
    jobs_payload = {
        "jobs": [
            {
                "databaseId": 999,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            # Return PR commit information
            return _cmd_result(
                True,
                stdout=json.dumps(
                    {
                        "commits": [
                            {
                                "oid": "abc123",
                            }
                        ]
                    }
                ),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            # Single call matching current implementation
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            run_id = int(cmd[3])
            if run_id == pr_run_id:
                return _cmd_result(
                    True,
                    stdout=json.dumps(
                        {
                            "jobs": jobs_payload["jobs"],
                            "pullRequests": [{"number": pr_number}],
                        }
                    ),
                    stderr="",
                    returncode=0,
                )
            if run_id == push_run_id:
                return _cmd_result(
                    True,
                    stdout=json.dumps({"jobs": jobs_payload["jobs"], "pullRequests": []}),
                    stderr="",
                    returncode=0,
                )
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("auto_coder.gh_logger.subprocess.run", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert pr_run_id in result.ids


def test_history_limits_to_runs_referencing_target_pr():
    """Skip runs that don't include target PR number in pullRequests, only adopt runs that do."""
    config = AutomationConfig()

    pr_number = 555
    pr_data = {
        "number": pr_number,
        "head_branch": "topic-branch",
        "head": {
            "ref": "topic-branch",
            "sha": "def456",
        },
    }

    run_a = 5100  # New but doesn't include target PR
    run_b = 5000  # Old but includes target PR
    run_list_payload = [
        {
            "databaseId": run_a,
            "headBranch": "topic-branch",
            "conclusion": "success",
            "createdAt": "2025-11-06T15:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{run_a}",
            "headSha": "def456",
            "event": "pull_request",
        },
        {
            "databaseId": run_b,
            "headBranch": "topic-branch",
            "conclusion": "success",
            "createdAt": "2025-11-05T15:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{run_b}",
            "headSha": "def456",
            "event": "pull_request",
        },
    ]
    run_list_result = _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)

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

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "view"]:
            return _cmd_result(
                True,
                stdout=json.dumps(
                    {
                        "commits": [
                            {
                                "oid": "def456",
                            }
                        ]
                    }
                ),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            run_id = int(cmd[3])
            if run_id == run_a:
                return _cmd_result(
                    True,
                    stdout=json.dumps(
                        {
                            "jobs": jobs_payload["jobs"],
                            "pullRequests": [{"number": 999}],
                        }
                    ),
                    stderr="",
                    returncode=0,
                )
            if run_id == run_b:
                return _cmd_result(
                    True,
                    stdout=json.dumps(
                        {
                            "jobs": jobs_payload["jobs"],
                            "pullRequests": [{"number": pr_number}],
                        }
                    ),
                    stderr="",
                    returncode=0,
                )
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("auto_coder.gh_logger.subprocess.run", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    assert run_b in result.ids
