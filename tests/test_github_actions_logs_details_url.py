import io
import json
import zipfile
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.utils import CommandResult


def test_get_github_actions_logs_uses_details_url_from_failed_checks(
    mock_github_client, mock_gemini_client
):
    """failed_checks の details_url を使用して正しい GitHub Actions ログを取得することを検証する。"""
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    repo_name = "kitamura-tetsuo/outliner"
    pr_data = {
        "number": 743,
        "title": "Test PR",
        "head_branch": "test-branch",
    }

    # failed_checks に details_url が含まれている場合
    failed_checks = [
        {
            "name": "CI / test",
            "conclusion": "failure",
            "details_url": "https://github.com/kitamura-tetsuo/outliner/actions/runs/18828609259/job/53715705095",
        }
    ]

    # subprocess.run用のモック関数
    def fake_subprocess_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        if cmd[:2] == ["gh", "api"] and "actions/jobs/53715705095/logs" in cmd[2]:
            # 正しいjob_idのログを返す
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w") as zf:
                zf.writestr("1_log.txt", "INFO: ok\nERROR: correct job log!\nmore info")
            return Mock(returncode=0, stdout=bio.getvalue(), stderr=b"")
        if cmd[:3] == ["gh", "run", "view"] and "18828609259" in str(cmd):
            jobs_obj = {
                "jobs": [
                    {
                        "databaseId": 53715705095,
                        "name": "CI / test",
                        "conclusion": "failure",
                    }
                ]
            }
            return Mock(returncode=0, stdout=json.dumps(jobs_obj).encode(), stderr=b"")
        # デフォルト
        return Mock(returncode=1, stdout=b"", stderr=b"unknown command")

    # cmd.run_command用のモック関数（フォールバック用）
    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None, check_success=True):
        # このテストでは details_url を使用するため、gh run list は呼ばれないはず
        if cmd[:3] == ["gh", "run", "list"]:
            # 間違ったrunを返す（これが使われたらテストが失敗する）
            runs = [
                {
                    "databaseId": 18828620318,  # 間違ったrun_id
                    "headBranch": pr_data["head_branch"],
                    "conclusion": "failure",
                    "createdAt": "2025-08-09T00:00:00Z",
                    "status": "completed",
                    "displayTitle": "CI",
                    "url": "https://example.com/run/18828620318",
                }
            ]
            return CommandResult(
                success=True,
                returncode=0,
                stdout=json.dumps(runs),
                stderr="",
            )
        # デフォルト
        return CommandResult(
            success=False,
            returncode=1,
            stdout="",
            stderr="unknown command",
        )

    with patch("src.auto_coder.pr_processor.cmd.run_command", side_effect=fake_run):
        with patch("subprocess.run", side_effect=fake_subprocess_run):
            out = engine._get_github_actions_logs(
                repo_name, pr_data, failed_checks
            )

    # 正しいjob_idのログが取得されていることを確認
    assert "53715705095" in out
    assert "ERROR: correct job log!" in out
    # 間違ったrun_idが使われていないことを確認
    assert "18828620318" not in out

