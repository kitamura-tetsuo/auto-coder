import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_commit_run_list_includes_event_filter():
    """commit に対する gh run list で --event pull_request を付与することを検証"""
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

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "run", "list"] and "--commit" in cmd:
            # commit用の run list には --event pull_request を付与
            assert "--event" in cmd and "pull_request" in cmd, f"--event filter missing in commit run list: {cmd}"
            return _cmd_result(True, stdout=json.dumps(commit_runs_payload), stderr="", returncode=0)
        if cmd[:3] == ["gh", "run", "view"]:
            jobs_payload = {
                "jobs": [
                    {"databaseId": 1, "name": "CI", "conclusion": "failure", "status": "completed"}
                ]
            }
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        # fallback list should not be called in this case
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.pr_processor.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result["success"] is False
    checks = result.get("checks", [])
    assert checks, "checks should not be empty"


def test_fallback_run_list_includes_event_filter():
    """commit でヒットしない場合のフォールバック run list でも --event pull_request を付与することを検証"""
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

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "run", "list"] and "--commit" in cmd:
            return _cmd_result(True, stdout="[]", stderr="", returncode=0)
        if cmd[:3] == ["gh", "run", "list"] and "--commit" not in cmd:
            # フォールバックの run list にも --event pull_request を付与
            assert "--event" in cmd and "pull_request" in cmd, f"--event filter missing in fallback run list: {cmd}"
            return _cmd_result(True, stdout=json.dumps(run_list_payload), stderr="", returncode=0)
        if cmd[:3] == ["gh", "run", "view"]:
            jobs_payload = {
                "jobs": [
                    {"databaseId": 2, "name": "CI", "conclusion": "success", "status": "completed"}
                ]
            }
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.pr_processor.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result["success"] is True
    checks = result.get("checks", [])
    assert checks, "checks should not be empty"

