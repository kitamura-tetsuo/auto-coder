import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.pr_processor import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_history_handles_missing_pullRequests_field_gracefully():
    """run view の pullRequests フィールドが欠落していても、ジョブ情報から評価できること。"""
    config = AutomationConfig()

    pr_data = {
        "number": 42,
        "head": {
            "ref": "feature-branch",
            "sha": "abc123def456",
        },
    }

    # 1) --commit 側はヒットしない
    commit_run_list = _cmd_result(True, stdout="[]", stderr="", returncode=0)

    # 2) 通常の run list で対象ブランチの PR イベントの run が1件見つかる
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

    # 3) run view は pullRequests フィールドを返さない（欠落）
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
        if cmd[:3] == ["gh", "run", "list"] and "--commit" in cmd:
            return commit_run_list
        if cmd[:3] == ["gh", "run", "list"] and "--commit" not in cmd:
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.pr_processor.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result["success"] is True
    assert result.get("historical_fallback") is True
    assert result.get("total_checks", 0) == 1
    checks = result.get("checks", [])
    assert checks, "checks が空ではいけません"
    assert f"/actions/runs/{run_id}/" in checks[0]["details_url"], checks[0]["details_url"]


def test_history_handles_empty_pullRequests_list_gracefully():
    """run view が pullRequests: [] を返しても、ジョブ情報から評価できること。"""
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

    # pullRequests は空配列
    jobs_payload = {
        "pullRequests": [],
        "jobs": [
            {
                "databaseId": 2002,
                "name": "CI",
                "conclusion": "success",
                "status": "completed",
            }
        ]
    }

    def side_effect(cmd, **kwargs):
        if cmd[:3] == ["gh", "run", "list"] and "--commit" in cmd:
            return commit_run_list
        if cmd[:3] == ["gh", "run", "list"] and "--commit" not in cmd:
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.pr_processor.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result["success"] is True
    assert result.get("total_checks", 0) == 1
    checks = result.get("checks", [])
    assert checks, "checks が空ではいけません"
    assert f"/actions/runs/{run_id}/" in checks[0]["details_url"], checks[0]["details_url"]

