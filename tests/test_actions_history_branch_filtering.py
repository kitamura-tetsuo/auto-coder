import json
from types import SimpleNamespace
from unittest.mock import patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.util.github_action import _check_github_actions_status_from_history


def _cmd_result(success: bool = True, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(success=success, stdout=stdout, stderr=stderr, returncode=returncode)


def test_history_uses_branch_filter_when_commit_runs_empty():
    """commit指定でランが見つからない場合でも、ブランチで正しくフィルタされることを確認する。

    回帰: PR #73 の履歴チェックが PR #133 の Run を誤って参照してしまう不具合の防止。
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

    # 1) --commit ではヒットしない
    commit_run_list = _cmd_result(True, stdout="[]", stderr="", returncode=0)

    # 2) 通常の run list は、他PRの新しいRunと、対象PRの古いRunが混在
    other_pr_run_id = 19024332818  # 実例に近い形式
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

    # 3) run view (jobs) は、対象PRのRunのみ参照されることを期待
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
            # PR のコミット情報を返す
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
                # 1回目（commit 相当）はヒットしない
                return commit_run_list
            # 2回目（フォールバック）は候補が返る
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
            # 万が一他PRのRunを取りに来ても空で返しておく
            return _cmd_result(True, stdout=json.dumps({"jobs": []}), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.util.github_action.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # GitHubActionsStatusResultにはsuccessとidsのみ存在
    assert isinstance(result.ids, list)


def test_history_filters_to_branch_even_with_head_sha_present():
    """head.sha が存在しても commit でヒットしない場合、ブランチで確実に絞り込む。"""
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

    # 同じブランチ(push)と異なるブランチ(PR)を混在させる
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
            # PR のコミット情報を返す
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
                # 1回目（commit 相当）はヒットしない
                return commit_run_list
            # 2回目（フォールバック）は候補が返る
            return run_list_result
        if cmd[:3] == ["gh", "run", "view"]:
            # ブランチで絞られた 3000 のみが参照されるはず
            run_id = int(cmd[3])
            assert run_id == 3000, f"unexpected run viewed: {run_id}"
            return _cmd_result(True, stdout=json.dumps(jobs_payload), stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("src.auto_coder.util.github_action.cmd.run_command", side_effect=side_effect):
        result = _check_github_actions_status_from_history("owner/repo", pr_data, config)

    assert result.success is True
    # GitHubActionsStatusResultにはsuccessとidsのみ存在
    assert isinstance(result.ids, list)
