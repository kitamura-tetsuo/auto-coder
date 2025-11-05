import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_commit_search_prefers_pull_request_runs_without_event_flag():
    """Commit search filters on Python side, and can prioritize pull_request runs even without --event/--commit flag."""
    config = AutomationConfig()

    pr_data = {
        "number": 101,
        "head": {"ref": "feat-commit-event", "sha": "feedc0ffee"},
    }

    run_id = 777001
    commit_runs_payload = [
        {
            "databaseId": run_id,
            "headBranch": "feat-commit-event",
            "conclusion": "failure",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "headSha": "feedc0ffee",
            "event": "pull_request",
        }
    ]

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
                                "oid": "feedc0ffee",
                            }
                        ]
                    }
                ),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            call_count["list"] += 1
            # Don't use --event/--commit flags for any call
            assert "--event" not in cmd and "--commit" not in cmd, f"unexpected flags in command: {cmd}"
            # Return commit-equivalent filter result on 1st list
            return _cmd_result(True, stdout=json.dumps(commit_runs_payload), stderr="", returncode=0)
        if cmd[:3] == ["gh", "run", "view"]:
            jobs_payload = {
                "jobs": [
                    {
                        "databaseId": 1,
                        "name": "CI",
                        "conclusion": "failure",
                        "status": "completed",
                    }
                ]
            }
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.util.github_action.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is False
    # Confirm checks are empty due to lazy retrieval
    assert result.ids == [777001], f"expected [777001] but got {result.ids}"
    # failed_checks is not available in GitHubActionsStatusResult


def test_fallback_search_works_without_event_flag():
    """Even if commit-equivalent search doesn't hit, can prioritize pull_request in fallback run list without --event."""
    config = AutomationConfig()

    pr_data = {
        "number": 102,
        "head": {"ref": "feat-fallback-event", "sha": "abc123"},
    }

    run_id = 777002
    run_list_payload = [
        {
            "databaseId": run_id,
            "headBranch": "feat-fallback-event",
            "conclusion": "success",
            "createdAt": "2025-11-05T10:00:00Z",
            "status": "completed",
            "displayTitle": "CI",
            "url": f"https://github.com/owner/repo/actions/runs/{run_id}",
            "headSha": "abc123",
            "event": "pull_request",
        }
    ]

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
                                "oid": "abc123",
                            }
                        ]
                    }
                ),
                stderr="",
                returncode=0,
            )
        if cmd[:3] == ["gh", "run", "list"]:
            call_count["list"] += 1
            # Don't use --event/--commit flags for any call
            assert "--event" not in cmd and "--commit" not in cmd, f"unexpected flags in command: {cmd}"
            # Check if this is a branch-based call (-b option)
            if "-b" in cmd:
                # Branch-based run list - should return the test data
                return _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)
            elif call_count["list"] == 1:
                # 1st time (equivalent to commit) will not hit
                return _cmd_result(True, stdout="[]", stderr="", returncode=0)
            else:
                # 2nd time (fallback) will hit
                return _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)
        if cmd[:3] == ["gh", "run", "view"]:
            jobs_payload = {
                "jobs": [
                    {
                        "databaseId": 2,
                        "name": "CI",
                        "conclusion": "success",
                        "status": "completed",
                    }
                ]
            }
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.util.github_action.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # Confirm checks are empty due to lazy retrieval
    assert result.ids == [777002], f"expected [777002] but got {result.ids}"
    # checks and failed_checks are not available in GitHubActionsStatusResult
